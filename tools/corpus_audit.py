#!/usr/bin/env python3
"""Read-only, deterministic Phase 0 audit of a Hansard XML archive.

This is deliberately an inventory/schema audit, not the Phase 1 production parser.
It never writes to the corpus and emits results only to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

FILENAME_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\.xml$")
TIME_PATTERNS = {
    "HH:MM": re.compile(r"^\d{2}:\d{2}$"),
    "HH:MM:SS": re.compile(r"^\d{2}:\d{2}:\d{2}$"),
}
SPEECH_FIELDS = (
    "id",
    "speakerid",
    "speakername",
    "talktype",
    "time",
    "approximate_wordcount",
    "approximate_duration",
    "url",
    "nospeaker",
)
TOP_LEVEL_TAGS = ("major-heading", "minor-heading", "speech", "division", "bills")
DOCTYPE_SCAN_BYTES = 65_536


@dataclass(frozen=True)
class PathValidation:
    valid: bool
    date_value: str | None
    errors: tuple[str, ...]


def local_name(tag: str) -> str:
    """Return an XML local name without requiring namespace registration."""
    return tag.rsplit("}", 1)[-1]


def discover_xml_files(root: Path) -> list[Path]:
    """Discover regular XML files without following directory symlinks."""
    root = root.resolve()
    discovered: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not (Path(directory) / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if filename.lower().endswith(".xml") and path.is_file() and not path.is_symlink():
                discovered.append(path)
    return sorted(discovered, key=lambda path: path.relative_to(root).as_posix())


def validate_archive_path(path: Path, root: Path) -> PathValidation:
    """Validate YYYY/YYYY-MM-DD.xml layout and calendar date."""
    errors: list[str] = []
    relative = path.resolve().relative_to(root.resolve())
    if len(relative.parts) != 2:
        errors.append("unexpected_path_depth")
        folder_year = relative.parts[-2] if len(relative.parts) >= 2 else ""
    else:
        folder_year = relative.parts[0]

    match = FILENAME_RE.fullmatch(path.name)
    parsed_date: date | None = None
    if match is None:
        errors.append("malformed_filename")
    else:
        try:
            parsed_date = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            errors.append("invalid_calendar_date")
        if folder_year != match.group("year"):
            errors.append("folder_filename_year_mismatch")

    if not re.fullmatch(r"\d{4}", folder_year):
        errors.append("malformed_year_folder")

    return PathValidation(
        valid=not errors,
        date_value=parsed_date.isoformat() if parsed_date else None,
        errors=tuple(sorted(set(errors))),
    )


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file using read-only binary access and bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def classify_time(raw: str | None) -> str:
    if raw is None:
        return "missing"
    if raw == "":
        return "empty"
    if raw == "unknown":
        return "unknown"
    for label, pattern in TIME_PATTERNS.items():
        if pattern.fullmatch(raw):
            return label
    return "other"


def select_stratified_sample(files: Iterable[Path], root: Path) -> list[str]:
    """Select first, middle, last path in each year deterministically."""
    by_year: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        relative = path.relative_to(root)
        year = relative.parts[0] if len(relative.parts) > 1 else "<invalid>"
        by_year[year].append(path)

    selected: list[str] = []
    for year in sorted(by_year):
        year_files = sorted(
            by_year[year], key=lambda path: path.relative_to(root).as_posix()
        )
        indexes = sorted({0, len(year_files) // 2, len(year_files) - 1})
        selected.extend(year_files[index].relative_to(root).as_posix() for index in indexes)
    return selected


def audit_xml_file(path: Path) -> dict[str, Any]:
    """Stream structural counts from one file and report errors safely."""
    with path.open("rb") as source:
        prefix = source.read(DOCTYPE_SCAN_BYTES).upper()
    if b"<!DOCTYPE" in prefix:
        return {"ok": False, "error": "unsafe_doctype"}

    tag_counts: Counter[str] = Counter()
    top_level_counts: Counter[str] = Counter()
    talktypes: Counter[str] = Counter()
    speech_attributes: set[str] = set()
    missing_speech_attributes: Counter[str] = Counter()
    time_formats: Counter[str] = Counter()
    nested_speech_tags: Counter[str] = Counter()
    source_ids: Counter[str] = Counter()
    root_tag: str | None = None
    depth = 0
    in_speech_depth: int | None = None

    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            tag = local_name(element.tag)
            if event == "start":
                depth += 1
                if root_tag is None:
                    root_tag = tag
                if depth == 2:
                    top_level_counts[tag] += 1
                if tag == "speech":
                    in_speech_depth = depth
                    speech_attributes.update(element.attrib)
                    for field in SPEECH_FIELDS:
                        if field not in element.attrib:
                            missing_speech_attributes[field] += 1
                    talktypes[element.attrib.get("talktype", "<missing>")] += 1
                    time_formats[classify_time(element.attrib.get("time"))] += 1
                    source_id = element.attrib.get("id")
                    if source_id:
                        source_ids[source_id] += 1
                elif in_speech_depth is not None and depth > in_speech_depth:
                    nested_speech_tags[tag] += 1
            else:
                tag_counts[tag] += 1
                if tag == "speech":
                    in_speech_depth = None
                element.clear()
                depth -= 1
    except (ET.ParseError, OSError, UnicodeError, ValueError) as error:
        return {
            "ok": False,
            "error": type(error).__name__,
            "message": str(error)[:300],
        }

    duplicate_excess = sum(count - 1 for count in source_ids.values() if count > 1)
    return {
        "ok": True,
        "root_tag": root_tag,
        "tag_counts": dict(sorted(tag_counts.items())),
        "top_level_counts": dict(sorted(top_level_counts.items())),
        "talktypes": dict(sorted(talktypes.items())),
        "speech_attributes": sorted(speech_attributes),
        "missing_speech_attributes": dict(sorted(missing_speech_attributes.items())),
        "time_formats": dict(sorted(time_formats.items())),
        "nested_speech_tags": dict(sorted(nested_speech_tags.items())),
        "duplicate_source_id_excess": duplicate_excess,
        "source_ids": source_ids,
    }


def add_counts(target: Counter[str], values: dict[str, int]) -> None:
    target.update(values)


def audit_corpus(root: Path) -> dict[str, Any]:
    """Audit a corpus without writing to it."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"corpus root is not a directory: {root}")

    files = discover_xml_files(root)
    sample_paths = set(select_stratified_sample(files, root))
    years: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "bytes": 0})
    path_errors: list[dict[str, Any]] = []
    malformed_xml: list[dict[str, str]] = []
    file_records: list[dict[str, Any]] = []
    sample_records: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    top_totals: Counter[str] = Counter()
    talktype_totals: Counter[str] = Counter()
    time_totals: Counter[str] = Counter()
    nested_totals: Counter[str] = Counter()
    missing_attr_totals: Counter[str] = Counter()
    speech_attributes: set[str] = set()
    global_source_ids: Counter[str] = Counter()
    inventory_digest = hashlib.sha256()
    mutation_detected: list[str] = []

    for path in files:
        relative = path.relative_to(root).as_posix()
        before = path.stat()
        validation = validate_archive_path(path, root)
        if not validation.valid:
            path_errors.append({"path": relative, "errors": list(validation.errors)})

        file_hash = sha256_file(path)
        xml_result = audit_xml_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            mutation_detected.append(relative)

        year = path.relative_to(root).parts[0]
        years[year]["files"] += 1
        years[year]["bytes"] += before.st_size
        inventory_digest.update(relative.encode("utf-8"))
        inventory_digest.update(b"\0")
        inventory_digest.update(str(before.st_size).encode("ascii"))
        inventory_digest.update(b"\0")
        inventory_digest.update(file_hash.encode("ascii"))
        inventory_digest.update(b"\n")

        record = {
            "path": relative,
            "bytes": before.st_size,
            "sha256": file_hash,
            "path_validation": asdict(validation),
            "xml_ok": bool(xml_result["ok"]),
        }
        file_records.append(record)

        if not xml_result["ok"]:
            malformed_xml.append(
                {
                    "path": relative,
                    "error": str(xml_result.get("error", "unknown")),
                    "message": str(xml_result.get("message", "")),
                }
            )
            if relative in sample_paths:
                sample_records.append({**record, "xml": xml_result})
            continue

        add_counts(totals, xml_result["tag_counts"])
        add_counts(top_totals, xml_result["top_level_counts"])
        add_counts(talktype_totals, xml_result["talktypes"])
        add_counts(time_totals, xml_result["time_formats"])
        add_counts(nested_totals, xml_result["nested_speech_tags"])
        add_counts(missing_attr_totals, xml_result["missing_speech_attributes"])
        speech_attributes.update(xml_result["speech_attributes"])
        global_source_ids.update(xml_result.pop("source_ids"))
        if relative in sample_paths:
            sample_records.append({**record, "xml": xml_result})

    duplicate_global_excess = sum(
        count - 1 for count in global_source_ids.values() if count > 1
    )
    duplicate_global_values = sum(1 for count in global_source_ids.values() if count > 1)

    return {
        "audit_version": 1,
        "corpus_root_name": root.name,
        "read_only_contract": True,
        "discovery_order": "relative_posix_path_ordinal",
        "file_count": len(files),
        "total_bytes": sum(record["bytes"] for record in file_records),
        "corpus_inventory_sha256": inventory_digest.hexdigest(),
        "years": dict(sorted(years.items())),
        "path_validation_errors": path_errors,
        "malformed_xml": malformed_xml,
        "source_mutation_detected": mutation_detected,
        "exact_counts_successful_files": {
            "tags": dict(sorted(totals.items())),
            "top_level_tags": dict(sorted(top_totals.items())),
            "talktypes": dict(sorted(talktype_totals.items())),
            "time_formats": dict(sorted(time_totals.items())),
            "nested_speech_tags": dict(sorted(nested_totals.items())),
            "missing_speech_attributes": dict(sorted(missing_attr_totals.items())),
            "speech_attributes_seen": sorted(speech_attributes),
            "duplicate_source_id_values": duplicate_global_values,
            "duplicate_source_id_excess": duplicate_global_excess,
        },
        "sample_method": "first_middle_last_filename_per_year",
        "sample_file_count": len(sample_records),
        "sample_files": sample_records,
        "files": file_records,
    }


def human_summary(result: dict[str, Any]) -> str:
    counts = result["exact_counts_successful_files"]
    lines = [
        f"Files: {result['file_count']}",
        f"Bytes: {result['total_bytes']}",
        f"Inventory SHA-256: {result['corpus_inventory_sha256']}",
        f"Years: {', '.join(result['years'])}",
        f"Speech fragments: {counts['tags'].get('speech', 0)}",
        f"Talk types: {json.dumps(counts['talktypes'], sort_keys=True)}",
        f"Divisions: {counts['tags'].get('division', 0)}",
        f"Bills containers: {counts['tags'].get('bills', 0)}",
        f"Malformed XML files: {len(result['malformed_xml'])}",
        f"Path validation errors: {len(result['path_validation_errors'])}",
        f"Mutation detected: {len(result['source_mutation_detected'])}",
        f"Sample files: {result['sample_file_count']}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "corpus_root",
        nargs="?",
        type=Path,
        default=Path("hansard_xml_files"),
        help="Read-only Hansard archive root (default: hansard_xml_files)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit deterministic JSON to stdout instead of a summary",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = audit_corpus(args.corpus_root)
    except (OSError, ValueError) as error:
        print(f"corpus audit failed: {error}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(human_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
