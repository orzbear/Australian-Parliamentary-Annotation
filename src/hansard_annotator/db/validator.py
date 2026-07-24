"""Read-only validation of the canonical accepted Phase 1 package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from hansard_annotator.db.exceptions import ImportValidationError
from hansard_annotator.db.import_models import (
    ACCEPTED_COUNTS,
    ACCEPTED_INVENTORY_SHA256,
    ACCEPTED_RUN_NAME,
    ACCEPTED_SOURCE_BYTES,
    EXPECTED_FIELDS,
    ValidatedRun,
)

REQUIRED_REPORTS = {
    "continuation_anomalies.csv",
    "corpus_summary.csv",
    "duplicate_ids.csv",
    "file_processing_report.csv",
    "image_elements.csv",
    "malformed_files.csv",
    "missing_speakers.csv",
    "reconciliation_failures.csv",
    "source_url_exceptions.csv",
    "unknown_headings.csv",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def csv_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as source:
        return max(0, sum(1 for _ in source) - 1)


def _schema_fields(path: Path) -> list[tuple[str, str]]:
    return [
        (field.name, str(field.type))
        for field in pq.read_schema(path)  # type: ignore[no-untyped-call]
    ]


def validate_run_directory(run_dir: Path, processed_root: Path) -> ValidatedRun:
    root = processed_root.resolve()
    resolved = run_dir.resolve()
    if not resolved.is_relative_to(root):
        raise ImportValidationError("run directory is outside the processed-data root")
    if resolved.name != ACCEPTED_RUN_NAME:
        raise ImportValidationError(
            f"Phase 2 accepts only {ACCEPTED_RUN_NAME}, got {resolved.name}"
        )
    manifest_path = resolved / "run_manifest.json"
    if not manifest_path.is_file():
        raise ImportValidationError("run_manifest.json is missing")
    try:
        manifest_raw = manifest_path.read_bytes()
        manifest: dict[str, Any] = json.loads(manifest_raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ImportValidationError(f"invalid run manifest: {error}") from error
    if manifest.get("run_id") != ACCEPTED_RUN_NAME:
        raise ImportValidationError("manifest run identity is not the accepted run")
    if manifest.get("overall_status") != "completed":
        raise ImportValidationError("manifest is not completed")
    if manifest.get("acceptance_gate_passed") is not True:
        raise ImportValidationError("manifest acceptance gate did not pass")
    if manifest.get("input_inventory_sha256") != ACCEPTED_INVENTORY_SHA256:
        raise ImportValidationError("source inventory SHA-256 differs from accepted value")
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list) or len(source_files) != ACCEPTED_COUNTS["source_files"]:
        raise ImportValidationError("manifest source file count differs from accepted value")
    if sum(int(item["byte_size"]) for item in source_files) != ACCEPTED_SOURCE_BYTES:
        raise ImportValidationError("manifest source byte count differs from accepted value")
    if manifest.get("record_counts") != ACCEPTED_COUNTS:
        raise ImportValidationError("manifest record counts differ from accepted values")

    outputs = manifest.get("output_files")
    if not isinstance(outputs, list) or not outputs:
        raise ImportValidationError("manifest output_files is missing or invalid")
    output_paths: set[str] = set()
    total_bytes = 0
    for entry in outputs:
        relative = entry.get("path")
        if not isinstance(relative, str) or relative in output_paths:
            raise ImportValidationError("invalid or duplicate manifest artefact path")
        path = (resolved / relative).resolve()
        if not path.is_relative_to(resolved) or not path.is_file():
            raise ImportValidationError(f"missing or unsafe artefact: {relative}")
        expected_bytes = entry.get("bytes")
        expected_sha = entry.get("sha256")
        if path.stat().st_size != expected_bytes:
            raise ImportValidationError(f"artefact byte-size mismatch: {relative}")
        if sha256_file(path) != expected_sha:
            raise ImportValidationError(f"artefact checksum mismatch: {relative}")
        output_paths.add(relative)
        total_bytes += int(expected_bytes)

    report_names = {
        Path(path).name for path in output_paths if path.startswith("reports/")
    }
    if report_names != REQUIRED_REPORTS:
        raise ImportValidationError("manifested report set is incomplete or unexpected")

    dataset_rows: dict[str, int] = {}
    for dataset_name, expected_fields in EXPECTED_FIELDS.items():
        parts = sorted((resolved / dataset_name).rglob("*.parquet"))
        if not parts:
            raise ImportValidationError(f"missing Parquet dataset: {dataset_name}")
        rows = 0
        for part in parts:
            relative = part.relative_to(resolved).as_posix()
            if relative not in output_paths:
                raise ImportValidationError(f"unmanifested Parquet artefact: {relative}")
            actual_fields = _schema_fields(part)
            if actual_fields != expected_fields:
                raise ImportValidationError(
                    f"Parquet schema mismatch: {relative}: {actual_fields!r}"
                )
            rows += pq.ParquetFile(part).metadata.num_rows  # type: ignore[no-untyped-call]
        if rows != ACCEPTED_COUNTS[dataset_name]:
            raise ImportValidationError(
                f"{dataset_name} rows {rows} != {ACCEPTED_COUNTS[dataset_name]}"
            )
        dataset_rows[dataset_name] = rows

    image_rows = csv_row_count(resolved / "reports" / "image_elements.csv")
    if image_rows != ACCEPTED_COUNTS["image_elements"]:
        raise ImportValidationError("image anomaly report count differs from accepted value")
    for empty_report in (
        "duplicate_ids.csv",
        "malformed_files.csv",
        "reconciliation_failures.csv",
        "source_url_exceptions.csv",
    ):
        if csv_row_count(resolved / "reports" / empty_report) != 0:
            raise ImportValidationError(f"accepted zero-row report is non-empty: {empty_report}")

    return ValidatedRun(
        run_dir=resolved,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        artefact_count=len(outputs),
        artefact_bytes=total_bytes,
        dataset_rows=dataset_rows,
    )
