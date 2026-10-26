"""Strict reader for deterministic Phase 3 AI-codebook export packages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZipFile

SUPPORTED_FORMAT = "hansard-ai-codebook-input"
SUPPORTED_FORMAT_VERSION = 1
REQUIRED_FILES = ("annotations.jsonl", "CODEBOOK_CONTEXT.json", "manifest.json")


@dataclass(frozen=True)
class EvaluationPackage:
    path: Path
    manifest: dict[str, Any]
    context: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    domain_codes: tuple[str, ...]
    task: str = "policy"

    @property
    def schema(self) -> dict[str, Any]:
        value = self.context["schema"]
        assert isinstance(value, dict)
        return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_files(path: Path) -> dict[str, bytes]:
    if path.is_dir():
        missing = [name for name in REQUIRED_FILES if not (path / name).is_file()]
        if missing:
            raise ValueError(f"evaluation package is missing: {', '.join(missing)}")
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        names = set(REQUIRED_FILES)
        if isinstance(manifest, dict) and isinstance(manifest.get("files"), dict):
            names.update(str(name) for name in manifest["files"])
        return {name: (path / name).read_bytes() for name in names if (path / name).is_file()}
    if path.is_file() and path.suffix.lower() == ".zip":
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            missing = [name for name in REQUIRED_FILES if name not in names]
            if missing:
                raise ValueError(f"evaluation package is missing: {', '.join(missing)}")
            if any(name.startswith(("/", "\\")) or ".." in Path(name).parts for name in names):
                raise ValueError("evaluation package contains an unsafe path")
            return {name: archive.read(name) for name in names if not name.endswith("/")}
    raise ValueError("evaluation package must be an export directory or .zip file")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _json_lines(content: bytes) -> Iterator[dict[str, Any]]:
    for number, raw_line in enumerate(content.decode("utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            yield _object(json.loads(raw_line), f"annotations.jsonl line {number}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError(f"malformed annotations.jsonl line {number}") from error


def load_evaluation_package(path: Path) -> EvaluationPackage:
    files = _read_files(path)
    try:
        manifest = _object(json.loads(files["manifest.json"]), "manifest")
        context = _object(json.loads(files["CODEBOOK_CONTEXT.json"]), "codebook context")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("evaluation package contains malformed JSON") from error
    if manifest.get("format") != SUPPORTED_FORMAT:
        raise ValueError(f"unsupported package format: {manifest.get('format')!r}")
    if manifest.get("format_version") != SUPPORTED_FORMAT_VERSION:
        raise ValueError(f"unsupported package format version: {manifest.get('format_version')!r}")
    file_metadata = _object(manifest.get("files"), "manifest files")
    for name, metadata_value in file_metadata.items():
        metadata = _object(metadata_value, f"manifest file {name}")
        if name not in files:
            raise ValueError(f"manifest file is missing: {name}")
        if metadata.get("bytes") != len(files[name]) or metadata.get("sha256") != _sha256(
            files[name]
        ):
            raise ValueError(f"checksum or size mismatch for {name}")
    snapshot_material = b"".join(
        name.encode("utf-8") + b"\0" + files[name] for name in sorted(file_metadata)
    )
    if manifest.get("snapshot_sha256") != _sha256(snapshot_material):
        raise ValueError("package snapshot checksum mismatch")
    schema = _object(context.get("schema"), "codebook schema")
    for key in ("slug", "version", "content_sha256", "fields"):
        if key not in schema:
            raise ValueError(f"codebook schema is missing {key}")
    taxonomies = context.get("taxonomies")
    if not isinstance(taxonomies, list) or not taxonomies:
        raise ValueError("codebook context has no taxonomies")
    domain_codes: list[str] = []
    for taxonomy_value in taxonomies:
        taxonomy = _object(taxonomy_value, "taxonomy")
        if taxonomy.get("slug") != "australian_policy_domains":
            continue
        labels = taxonomy.get("labels")
        if not isinstance(labels, list):
            raise ValueError("policy-domain taxonomy labels must be a list")
        domain_codes = [str(_object(label, "taxonomy label")["code"]) for label in labels]
    schema_slug = str(schema["slug"])
    task = "hostility" if schema_slug == "australian_partisan_hostility" else "policy"
    if task == "policy" and not domain_codes:
        raise ValueError("Australian policy-domain taxonomy is missing")
    if task == "hostility":
        field_keys = {
            str(_object(field, "schema field").get("field_key")) for field in schema["fields"]
        }
        if field_keys != {"hostility_present", "target", "evidence_span"}:
            raise ValueError("hostility schema fields are missing or incompatible")
        taxonomy_codes = {
            str(taxonomy["slug"]): {
                str(_object(label, "taxonomy label")["code"])
                for label in taxonomy.get("labels", [])
            }
            for taxonomy in (_object(value, "taxonomy") for value in taxonomies)
        }
        if taxonomy_codes.get("hostility_presence") != {"YES", "NO", "UNSURE"}:
            raise ValueError("hostility presence taxonomy is missing or incompatible")
        if taxonomy_codes.get("hostility_target") != {
            "OPPOSING_PARTISAN_TARGET",
            "OTHER_POLITICAL_TARGET",
            "NON_POLITICAL_TARGET",
            "UNSURE",
        }:
            raise ValueError("hostility target taxonomy is missing or incompatible")
    records = tuple(_json_lines(files["annotations.jsonl"]))
    if manifest.get("record_count") != len(records):
        raise ValueError("manifest record_count does not match annotations.jsonl")
    seen: set[str] = set()
    for index, record in enumerate(records, 1):
        record_id = record.get("record_id")
        speech = _object(record.get("speech"), f"record {index} speech")
        human = _object(record.get("human_annotation"), f"record {index} human_annotation")
        values = _object(human.get("values"), f"record {index} human values")
        if not isinstance(record_id, str) or not record_id or record_id in seen:
            raise ValueError(f"record {index} has a missing or duplicate record_id")
        seen.add(record_id)
        if not isinstance(speech.get("turn_key"), str) or not isinstance(speech.get("text"), str):
            raise ValueError(f"record {record_id} has invalid speech identifiers/text")
        canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if human.get("values_sha256") != _sha256(canonical.encode("utf-8")):
            raise ValueError(f"record {record_id} human annotation hash mismatch")
        if task == "policy":
            if not isinstance(values.get("is_non_policy"), bool):
                raise ValueError(f"record {record_id} has invalid is_non_policy")
            primary = values.get("primary_australian_domain")
            if primary is not None and primary not in domain_codes:
                raise ValueError(f"record {record_id} has an unknown primary domain")
        else:
            _validate_hostility_values(record_id, values, str(speech["text"]))
    if task == "hostility":
        if schema.get("version") != "0.2.0":
            raise ValueError("hostility evaluation requires schema version 0.2.0")
        corpus = _object(manifest.get("corpus"), "manifest corpus")
        if corpus.get("run_name") != "phase1-full-20260723-v4":
            raise ValueError("hostility package has unexpected corpus run")
    return EvaluationPackage(path, manifest, context, records, tuple(domain_codes), task)


def _validate_hostility_values(record_id: str, values: dict[str, Any], text: str) -> None:
    if set(values) != {"hostility_present", "target", "evidence_span"}:
        raise ValueError(f"record {record_id} has invalid hostility fields")
    presence, target, spans = (
        values["hostility_present"],
        values["target"],
        values["evidence_span"],
    )
    if presence not in {"YES", "NO", "UNSURE"}:
        raise ValueError(f"record {record_id} has invalid hostility_present")
    allowed_targets = {
        "OPPOSING_PARTISAN_TARGET",
        "OTHER_POLITICAL_TARGET",
        "NON_POLITICAL_TARGET",
        "UNSURE",
    }
    if presence == "NO" and target is not None:
        raise ValueError(f"record {record_id} NO label must have null target")
    if presence != "NO" and target not in allowed_targets:
        raise ValueError(f"record {record_id} has invalid hostility target")
    if (
        not isinstance(spans, list)
        or (presence == "NO" and spans)
        or (presence != "NO" and not spans)
    ):
        raise ValueError(f"record {record_id} has invalid hostility evidence cardinality")
    for span in spans:
        value = _object(span, f"record {record_id} evidence span")
        start, end, evidence = value.get("start_offset"), value.get("end_offset"), value.get("text")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(evidence, str)
            or start < 0
            or end <= start
            or end > len(text)
            or text[start:end] != evidence
        ):
            raise ValueError(f"record {record_id} has invalid hostility evidence offsets/text")
