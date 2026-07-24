"""Deterministic, read-only source-file discovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from hansard_annotator.corpus.models import DiscoveredFile

FILENAME_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\.xml$")


class DiscoveryError(ValueError):
    """Raised when a requested source path violates the archive contract."""


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def validate_relative_path(relative_path: Path) -> date:
    if len(relative_path.parts) != 2:
        raise DiscoveryError("EXPECTED_YEAR_AND_FILENAME")
    folder_year, filename = relative_path.parts
    match = FILENAME_RE.fullmatch(filename)
    if match is None:
        raise DiscoveryError("MALFORMED_FILENAME")
    if folder_year != match.group("year"):
        raise DiscoveryError("FOLDER_FILENAME_YEAR_MISMATCH")
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as error:
        raise DiscoveryError("INVALID_CALENDAR_DATE") from error


def _walk_xml(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        dirnames[:] = sorted(
            name for name in dirnames if not (directory_path / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = directory_path / filename
            if path.suffix.lower() == ".xml" and path.is_file() and not path.is_symlink():
                paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def ensure_within_root(path: Path, root: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise DiscoveryError("PATH_OUTSIDE_CORPUS_ROOT") from error
    return resolved_path


def _select_sample(paths: list[Path], sample_size: int) -> list[Path]:
    if sample_size <= 0:
        raise DiscoveryError("SAMPLE_SIZE_MUST_BE_POSITIVE")
    if sample_size >= len(paths):
        return paths
    if sample_size == 1:
        return [paths[len(paths) // 2]]
    indexes = {
        round(position * (len(paths) - 1) / (sample_size - 1))
        for position in range(sample_size)
    }
    return [paths[index] for index in sorted(indexes)]


def load_prior_hashes(manifest_path: Path) -> dict[str, str]:
    with manifest_path.open("r", encoding="utf-8") as source:
        manifest: dict[str, Any] = json.load(source)
    records = manifest.get("source_files", [])
    return {
        str(record["source_file"]): str(record["source_file_sha256"])
        for record in records
    }


def discover_source_files(
    corpus_root: Path,
    *,
    year: int | None = None,
    one_file: str | None = None,
    sample_size: int | None = None,
    prior_hashes: dict[str, str] | None = None,
) -> list[DiscoveredFile]:
    root = corpus_root.resolve(strict=True)
    if not root.is_dir():
        raise DiscoveryError("CORPUS_ROOT_NOT_DIRECTORY")

    if one_file is not None:
        requested = ensure_within_root(root / Path(one_file), root)
        paths = [requested]
    else:
        paths = _walk_xml(root)

    if year is not None:
        paths = [
            path for path in paths if path.relative_to(root).parts[0] == f"{year:04d}"
        ]
    if sample_size is not None:
        paths = _select_sample(paths, sample_size)

    discovered: list[DiscoveredFile] = []
    for path in paths:
        safe_path = ensure_within_root(path, root)
        relative = safe_path.relative_to(root)
        sitting_date = validate_relative_path(relative)
        stat = safe_path.stat()
        source_hash = sha256_file(safe_path)
        relative_posix = relative.as_posix()
        if prior_hashes is not None and prior_hashes.get(relative_posix) == source_hash:
            continue
        discovered.append(
            DiscoveredFile(
                absolute_path=safe_path,
                relative_path=relative_posix,
                source_file_sha256=source_hash,
                byte_size=stat.st_size,
                modification_time_ns=stat.st_mtime_ns,
                sitting_date=sitting_date,
            )
        )
    return discovered


def inventory_sha256(files: Iterable[DiscoveredFile]) -> str:
    digest = hashlib.sha256()
    for source in files:
        digest.update(source.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(source.byte_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(source.source_file_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()

