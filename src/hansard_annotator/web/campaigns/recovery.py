"""Immutable artifact resolution for targeted Phase 4D recovery attempts."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class RecoveryArtifactError(ValueError):
    """A campaign or recovery artifact cannot be resolved safely."""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RecoveryArtifactError(f"{path.name} line {number} is not an object")
        rows.append(value)
    return rows


def _unique_by_record_id(rows: Iterable[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or not record_id.startswith("turn:"):
            raise RecoveryArtifactError(f"{label} contains an invalid record ID")
        if record_id in indexed:
            raise RecoveryArtifactError(f"{label} contains duplicate record ID {record_id}")
        indexed[record_id] = row
    return indexed


def effective_results(
    original_directory: Path,
    collected_recoveries: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve original rows with the latest successful recovery for each record."""
    original = read_jsonl(original_directory / "predictions.jsonl")
    indexed = _unique_by_record_id(original, "original predictions")
    original_order = {
        str(row["record_id"]): int(row.get("request_index", position))
        for position, row in enumerate(original)
    }
    for recovery in sorted(collected_recoveries, key=lambda row: int(row["attempt_number"])):
        reference = recovery.get("artifact_reference")
        if not isinstance(reference, str):
            continue
        recovered = _unique_by_record_id(
            read_jsonl(Path(reference) / "predictions.jsonl"),
            f"recovery attempt {recovery['attempt_number']}",
        )
        for record_id, row in recovered.items():
            if record_id not in indexed:
                raise RecoveryArtifactError("recovery contains a record outside the campaign")
            if row.get("status") == "success":
                replacement = copy.deepcopy(row)
                replacement["request_index"] = original_order[record_id]
                replacement["effective_source"] = "recovery"
                replacement["recovery_attempt_number"] = int(
                    recovery["attempt_number"]
                )
                replacement["recovery_provider_job_id"] = recovery.get(
                    "provider_job_id"
                )
                indexed[record_id] = replacement
    return sorted(indexed.values(), key=lambda row: original_order[str(row["record_id"])])


def unresolved_results(
    original_directory: Path,
    collected_recoveries: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        row
        for row in effective_results(original_directory, collected_recoveries)
        if row.get("status") in {"invalid", "provider_failure"}
    ]


def artifact_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in ("operational_summary.json", "predictions.jsonl"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((directory / name).read_bytes())
    return digest.hexdigest()


def _sum_metric(
    original: dict[str, Any], recoveries: Iterable[dict[str, Any]], key: str
) -> float:
    total = float(original.get(key) or 0)
    for recovery in recoveries:
        metadata = recovery.get("operational_metadata")
        section = metadata.get("cost", {}) if isinstance(metadata, dict) else {}
        total += float(section.get(key) or 0) if isinstance(section, dict) else 0
    return total


def build_effective_artifact(
    original_directory: Path,
    collected_recoveries: list[dict[str, Any]],
    output_directory: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Write a derived import artifact without modifying any source attempt."""
    if output_directory.exists():
        raise RecoveryArtifactError("effective artifact already exists")
    original_summary = json.loads(
        (original_directory / "operational_summary.json").read_text(encoding="utf-8")
    )
    if not isinstance(original_summary, dict):
        raise RecoveryArtifactError("original operational summary is not an object")
    rows = effective_results(original_directory, collected_recoveries)
    valid = sum(row.get("status") == "success" for row in rows)
    invalid = len(rows) - valid
    provider_failures = sum(row.get("status") == "provider_failure" for row in rows)
    summary = copy.deepcopy(original_summary)
    coverage = summary.setdefault("coverage", {})
    planned = int(coverage.get("planned_requests") or len(rows))
    coverage.update(
        {
            "provider_request_count": planned,
            "collected_responses": len(rows),
            "missing_responses": max(0, planned - len(rows)),
            "duplicate_mappings": 0,
            "successful_provider_responses": planned - provider_failures,
            "failed_provider_responses": provider_failures,
            "valid_structured_predictions": valid,
            "invalid_structured_predictions": invalid,
            "provider_failures_collected": provider_failures,
            "accounting_invariant_ok": planned == len(rows),
        }
    )
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    tokens = summary.setdefault("tokens", {})
    for field in token_fields:
        tokens[field] = int(tokens.get(field) or 0) + sum(
            int(metadata.get("tokens", {}).get(field) or 0)
            for recovery in collected_recoveries
            if isinstance((metadata := recovery.get("operational_metadata")), dict)
            and isinstance(metadata.get("tokens"), dict)
        )
    tokens["uncached_input_tokens"] = max(
        0, int(tokens.get("input_tokens") or 0) - int(tokens.get("cached_input_tokens") or 0)
    )
    cost = summary.setdefault("cost", {})
    for field in (
        "batch_uncached_input_cost_usd",
        "cached_input_cost_usd",
        "output_including_reasoning_cost_usd",
        "token_processing_cost_usd",
        "explicit_cache_storage_cost_usd",
        "total_pilot_cost_usd",
    ):
        cost[field] = _sum_metric(cost, collected_recoveries, field)
    cost["cost_per_valid_annotation_usd"] = (
        float(cost["total_pilot_cost_usd"]) / valid if valid else None
    )
    provenance = summary.setdefault("provenance", {})
    provenance["recovery_batch_ids"] = [
        recovery.get("provider_job_id") for recovery in collected_recoveries
    ]
    summary["recovery"] = {
        "attempt_count": len(collected_recoveries),
        "attempts": [
            {
                "attempt_number": int(recovery["attempt_number"]),
                "provider_job_id": recovery.get("provider_job_id"),
                "artifact_sha256": recovery.get("artifact_sha256"),
                "valid_count": int(recovery.get("valid_count") or 0),
                "invalid_count": int(recovery.get("invalid_count") or 0),
            }
            for recovery in collected_recoveries
        ],
        "resolution_rule": "latest successful recovery, otherwise original result",
    }
    output_directory.mkdir(parents=True)
    (output_directory / "predictions.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )
    (output_directory / "operational_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary, rows, artifact_sha256(output_directory)
