"""Immutable artifact resolution for targeted Phase 4D recovery attempts."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from hansard_annotator.llm_eval.validation import InvalidPrediction, validate_prediction

STRUCTURAL_REPAIR_RULE = "remove_primary_from_secondary_v1"
STRUCTURAL_REPAIR_VERSION = 1
STRUCTURAL_REPAIR_DIRECTORY = f"structural-repair-{STRUCTURAL_REPAIR_RULE}"
PROVIDER_REFUSAL_RULE = "prohibited_content_no_candidate_v1"
PROVIDER_REFUSAL_VERSION = 1
PROVIDER_REFUSAL_DIRECTORY = f"provider-refusal-{PROVIDER_REFUSAL_RULE}"


class RecoveryArtifactError(ValueError):
    """A campaign or recovery artifact cannot be resolved safely."""


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RecoveryArtifactError(
                    f"{path.name} line {number} is not an object"
                )
            yield value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


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
        if row.get("status") in {"invalid", "provider_failure", "missing"}
    ]


def artifact_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in ("operational_summary.json", "predictions.jsonl"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((directory / name).read_bytes())
    return digest.hexdigest()


def structural_repair_directory(source_directory: Path) -> Path:
    """Return the deterministic append-only repair location for an effective artifact."""
    return source_directory.parent / f"{source_directory.name}-{STRUCTURAL_REPAIR_DIRECTORY}"


def provider_refusal_directory(source_directory: Path) -> Path:
    """Return the append-only location for terminal provider-refusal resolutions."""
    return source_directory.parent / f"{source_directory.name}-{PROVIDER_REFUSAL_DIRECTORY}"


def _provider_text(row: dict[str, Any]) -> str | None:
    response = row.get("provider_response")
    candidates = response.get("candidates") if isinstance(response, dict) else None
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text.strip():
                return text
    return None


def is_prohibited_content_refusal(row: dict[str, Any]) -> bool:
    """Recognize only the confirmed Gemini abstention shape."""
    if row.get("status") == "success" or row.get("prediction") is not None:
        return False
    response = row.get("provider_response")
    feedback = response.get("promptFeedback") if isinstance(response, dict) else None
    return (
        isinstance(feedback, dict)
        and feedback.get("blockReason") == "PROHIBITED_CONTENT"
        and _provider_text(row) is None
    )


def provider_refusal_count(rows: Iterable[dict[str, Any]]) -> int:
    return sum(is_prohibited_content_refusal(row) for row in rows)


def _provider_prediction(row: dict[str, Any]) -> object:
    """Recover the exact JSON value retained in a Gemini campaign result."""
    response = row.get("provider_response")
    candidates = response.get("candidates") if isinstance(response, dict) else None
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    content = candidate.get("content") if isinstance(candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return None
    for part in parts:
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return None
    return None


def _repair_primary_in_secondary(
    row: dict[str, Any], domain_codes: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if row.get("status") != "invalid":
        return None
    before = _provider_prediction(row)
    if not isinstance(before, dict):
        return None
    primary = before.get("primary_australian_domain")
    secondary = before.get("secondary_australian_domains")
    if (
        not isinstance(primary, str)
        or primary not in domain_codes
        or not isinstance(secondary, list)
        or primary not in secondary
    ):
        return None
    try:
        validate_prediction(before, domain_codes)
    except InvalidPrediction:
        pass
    else:
        return None
    after = copy.deepcopy(before)
    after["secondary_australian_domains"] = [
        value for value in secondary if value != primary
    ]
    try:
        validated_prediction = validate_prediction(after, domain_codes)
    except InvalidPrediction:
        return None
    validated = validated_prediction.to_dict()
    validated["secondary_australian_domains"] = list(
        validated_prediction.secondary_australian_domains
    )
    replacement = copy.deepcopy(row)
    lineage = {
        "record_id": row.get("record_id"),
        "rule_id": STRUCTURAL_REPAIR_RULE,
        "rule_version": STRUCTURAL_REPAIR_VERSION,
        "source_status": row.get("status"),
        "source_validation_error": row.get("validation_error"),
        "source_request_index": row.get("request_index"),
        "source_response_index": row.get("response_index"),
        "source_recovery_attempt_number": row.get("recovery_attempt_number"),
        "before_prediction": before,
        "after_prediction": validated,
    }
    replacement.update(
        status="success",
        prediction=validated,
        validation_error=None,
        effective_source="structural_repair",
        structural_repair=copy.deepcopy(lineage),
    )
    return replacement, lineage


def apply_structural_repairs(
    rows: Iterable[dict[str, Any]], domain_codes: tuple[str, ...]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply only allow-listed repairs; every accepted replacement passes strict validation."""
    effective: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    for row in rows:
        repaired = _repair_primary_in_secondary(row, domain_codes)
        if repaired is None:
            effective.append(copy.deepcopy(row))
            continue
        replacement, repair_lineage = repaired
        effective.append(replacement)
        lineage.append(repair_lineage)
    return effective, lineage


def structural_repair_count(
    rows: Iterable[dict[str, Any]], domain_codes: tuple[str, ...]
) -> int:
    return sum(
        _repair_primary_in_secondary(row, domain_codes) is not None for row in rows
    )


def build_structurally_repaired_artifact(
    source_directory: Path,
    output_directory: Path,
    domain_codes: tuple[str, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Write an append-only derived artifact and exact deterministic repair lineage."""
    if output_directory.exists():
        raise RecoveryArtifactError("structural repair artifact already exists")
    summary = json.loads(
        (source_directory / "operational_summary.json").read_text(encoding="utf-8")
    )
    if not isinstance(summary, dict):
        raise RecoveryArtifactError("source operational summary is not an object")
    source_rows = read_jsonl(source_directory / "predictions.jsonl")
    rows, lineage = apply_structural_repairs(source_rows, domain_codes)
    if not lineage:
        raise RecoveryArtifactError("no approved deterministic structural repair applies")
    valid = sum(row.get("status") == "success" for row in rows)
    invalid = len(rows) - valid
    coverage = summary.setdefault("coverage", {})
    planned = int(coverage.get("planned_requests") or len(rows))
    coverage.update(
        valid_structured_predictions=valid,
        invalid_structured_predictions=invalid,
        accounting_invariant_ok=planned == len(rows),
    )
    lineage_text = "".join(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        for value in lineage
    )
    lineage_sha256 = hashlib.sha256(lineage_text.encode("utf-8")).hexdigest()
    provenance = summary.setdefault("provenance", {})
    provenance["structural_repair_rules"] = [STRUCTURAL_REPAIR_RULE]
    summary["structural_repair"] = {
        "append_only": True,
        "rule_id": STRUCTURAL_REPAIR_RULE,
        "rule_version": STRUCTURAL_REPAIR_VERSION,
        "repaired_count": len(lineage),
        "unresolved_count": invalid,
        "source_artifact_sha256": artifact_sha256(source_directory),
        "lineage_file": "structural_repairs.jsonl",
        "lineage_sha256": lineage_sha256,
        "validation": "existing strict Phase 4A prediction validator",
    }
    output_directory.mkdir(parents=True)
    (output_directory / "structural_repairs.jsonl").write_text(
        lineage_text, encoding="utf-8"
    )
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


def build_provider_refusal_artifact(
    source_directory: Path,
    output_directory: Path,
    attempt_sources: list[dict[str, Any]],
) -> tuple[dict[str, Any], int, str]:
    """Resolve confirmed refusals while retaining only refusal evidence in memory."""
    if output_directory.exists():
        raise RecoveryArtifactError("provider-refusal artifact already exists")
    summary = json.loads(
        (source_directory / "operational_summary.json").read_text(encoding="utf-8")
    )
    if not isinstance(summary, dict):
        raise RecoveryArtifactError("source operational summary is not an object")
    candidate_ids = {
        str(row["record_id"])
        for row in iter_jsonl(source_directory / "predictions.jsonl")
        if isinstance(row.get("record_id"), str)
        and is_prohibited_content_refusal(row)
    }
    if not candidate_ids:
        raise RecoveryArtifactError("no confirmed provider refusal applies")
    evidence: dict[str, list[dict[str, Any]]] = {}
    for source in attempt_sources:
        reference = source.get("artifact_reference")
        if not isinstance(reference, str):
            raise RecoveryArtifactError("refusal evidence has no artifact reference")
        directory = Path(reference)
        source_hash = source.get("artifact_sha256") or artifact_sha256(directory)
        for row in iter_jsonl(directory / "predictions.jsonl"):
            record_id = row.get("record_id")
            if not isinstance(record_id, str):
                raise RecoveryArtifactError("refusal evidence has an invalid record ID")
            if record_id not in candidate_ids:
                continue
            evidence.setdefault(record_id, []).append(
                {
                    "source_kind": source.get("source_kind"),
                    "recovery_attempt_number": source.get("recovery_attempt_number"),
                    "provider_job_id": source.get("provider_job_id"),
                    "artifact_reference": reference,
                    "artifact_sha256": source_hash,
                    "request_index": row.get("request_index"),
                    "response_index": row.get("response_index"),
                    "chunk_number": row.get("chunk_number"),
                    "chunk_request_index": row.get("chunk_request_index"),
                    "status": row.get("status"),
                    "validation_error": row.get("validation_error"),
                    "provider_error": row.get("provider_error"),
                    "returned_model": row.get("returned_model"),
                    "usage": copy.deepcopy(row.get("usage")),
                    "provider_response": copy.deepcopy(row.get("provider_response")),
                    "qualifies": is_prohibited_content_refusal(row),
                }
            )

    refusal_exports: list[dict[str, Any]] = []
    provenance = copy.deepcopy(summary.get("provenance", {}))
    source_hash = artifact_sha256(source_directory)
    qualifying_ids = {
        record_id
        for record_id in candidate_ids
        if evidence.get(record_id)
        and all(
            attempt.get("qualifies") is True for attempt in evidence[record_id]
        )
    }
    if not qualifying_ids:
        raise RecoveryArtifactError(
            "not every observed attempt confirms the provider refusal"
        )
    output_directory.mkdir(parents=True)
    valid = repaired = refused = failed = missing_rows = invalid = row_count = 0
    with (output_directory / "predictions.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as target:
        for row in iter_jsonl(source_directory / "predictions.jsonl"):
            record_id = str(row.get("record_id"))
            attempts = evidence.get(record_id, [])
            if (
                record_id in qualifying_ids
                and attempts
            ):
                refusal_exports.append(
                    {
                        "record_id": record_id,
                        "resolution": "PROVIDER_REFUSAL",
                        "reason": "PROHIBITED_CONTENT",
                        "rule_id": PROVIDER_REFUSAL_RULE,
                        "rule_version": PROVIDER_REFUSAL_VERSION,
                        "source_artifact_sha256": source_hash,
                        "configuration_provenance": provenance,
                        "attempts": [
                            {
                                key: value
                                for key, value in attempt.items()
                                if key != "qualifies"
                            }
                            for attempt in attempts
                        ],
                    }
                )
                row = copy.deepcopy(row)
                row.update(
                    status="provider_refusal",
                    prediction=None,
                    validation_error=None,
                    effective_source="provider_refusal_resolution",
                    terminal_resolution={
                        "type": "PROVIDER_REFUSAL",
                        "reason": "PROHIBITED_CONTENT",
                        "rule_id": PROVIDER_REFUSAL_RULE,
                        "rule_version": PROVIDER_REFUSAL_VERSION,
                    },
                )
            status = row.get("status")
            valid += status == "success"
            repaired += row.get("effective_source") == "structural_repair"
            refused += status == "provider_refusal"
            failed += status == "provider_failure"
            missing_rows += status == "missing"
            invalid += status not in {
                "success",
                "provider_refusal",
                "provider_failure",
                "missing",
            }
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            row_count += 1
    coverage = summary.setdefault("coverage", {})
    planned = int(coverage.get("planned_requests") or row_count)
    missing = max(missing_rows, int(coverage.get("missing_responses") or 0))
    duplicate = int(coverage.get("duplicate_mappings") or 0)
    resolved = valid + refused
    coverage.update(
        {
            "valid_structured_predictions": valid,
            "structurally_repaired": repaired,
            "provider_refused": refused,
            "invalid_structured_predictions": invalid,
            "valid": valid - repaired,
            "invalid": invalid,
            "failed": failed,
            "missing": missing,
            "duplicate": duplicate,
            "failed_provider_responses": failed,
            "provider_failures_collected": failed,
            "missing_responses": missing,
            "duplicate_mappings": duplicate,
            "resolved_records": resolved,
            "accounting_invariant_ok": (
                planned == row_count == resolved
                and invalid == failed == missing == duplicate == 0
            ),
        }
    )
    refusal_text = "".join(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        for value in refusal_exports
    )
    refusal_sha256 = hashlib.sha256(refusal_text.encode("utf-8")).hexdigest()
    refusal_ids = [str(value["record_id"]) for value in refusal_exports]
    summary["provider_refusals"] = {
        "append_only": True,
        "resolution": "PROVIDER_REFUSAL",
        "reason": "PROHIBITED_CONTENT",
        "rule_id": PROVIDER_REFUSAL_RULE,
        "rule_version": PROVIDER_REFUSAL_VERSION,
        "count": refused,
        "record_ids": refusal_ids,
        "record_ids_sha256": hashlib.sha256(
            json.dumps(refusal_ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "lineage_file": "provider_refusals.jsonl",
        "lineage_sha256": refusal_sha256,
        "source_artifact_sha256": source_hash,
    }
    (output_directory / "provider_refusals.jsonl").write_text(
        refusal_text, encoding="utf-8", newline="\n"
    )
    (output_directory / "operational_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    refusal_lines = "\n".join(f"- `{record_id}`" for record_id in refusal_ids)
    (output_directory / "summary.md").write_text(
        "# Phase 4E effective campaign summary\n\n"
        f"- Valid predictions: {valid}\n"
        f"- Structurally repaired predictions: {repaired}\n"
        f"- Provider refusals: {refused}\n"
        f"- Unresolved invalid: {invalid}\n"
        f"- Provider failures: {failed}\n"
        f"- Missing: {missing}\n"
        f"- Duplicate mappings: {duplicate}\n\n"
        "## Provider-refusal record IDs\n\n"
        f"{refusal_lines}\n",
        encoding="utf-8",
        newline="\n",
    )
    return summary, row_count, artifact_sha256(output_directory)


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
    refused = sum(row.get("status") == "provider_refusal" for row in rows)
    provider_failures = sum(row.get("status") == "provider_failure" for row in rows)
    missing = sum(row.get("status") == "missing" for row in rows)
    invalid = len(rows) - valid - refused - provider_failures - missing
    summary = copy.deepcopy(original_summary)
    coverage = summary.setdefault("coverage", {})
    planned = int(coverage.get("planned_requests") or len(rows))
    coverage.update(
        {
            "provider_request_count": planned,
            "collected_responses": len(rows),
            "missing_responses": missing + max(0, planned - len(rows)),
            "duplicate_mappings": 0,
            "successful_provider_responses": planned - provider_failures,
            "failed_provider_responses": provider_failures,
            "valid_structured_predictions": valid,
            "invalid_structured_predictions": invalid,
            "provider_refused": refused,
            "provider_failures_collected": provider_failures,
            "valid": valid
            - sum(row.get("effective_source") == "structural_repair" for row in rows),
            "structurally_repaired": sum(
                row.get("effective_source") == "structural_repair" for row in rows
            ),
            "invalid": invalid,
            "failed": provider_failures,
            "missing": missing,
            "duplicate": 0,
            "accounting_invariant_ok": planned == len(rows) and missing == 0,
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


def build_effective_artifact_streaming(
    original_directory: Path,
    collected_recoveries: list[dict[str, Any]],
    output_directory: Path,
) -> tuple[dict[str, Any], int, str]:
    """Build the Phase 4E effective artifact without materializing original rows."""
    if output_directory.exists():
        raise RecoveryArtifactError("effective artifact already exists")
    original_summary = json.loads(
        (original_directory / "operational_summary.json").read_text(encoding="utf-8")
    )
    if not isinstance(original_summary, dict):
        raise RecoveryArtifactError("original operational summary is not an object")
    replacements: dict[str, dict[str, Any]] = {}
    for recovery in sorted(collected_recoveries, key=lambda row: int(row["attempt_number"])):
        reference = recovery.get("artifact_reference")
        if not isinstance(reference, str):
            continue
        for row in iter_jsonl(Path(reference) / "predictions.jsonl"):
            record_id = row.get("record_id")
            if not isinstance(record_id, str):
                raise RecoveryArtifactError("recovery contains an invalid record ID")
            if row.get("status") == "success":
                replacement = copy.deepcopy(row)
                replacement["effective_source"] = "recovery"
                replacement["recovery_attempt_number"] = int(
                    recovery["attempt_number"]
                )
                replacement["recovery_provider_job_id"] = recovery.get(
                    "provider_job_id"
                )
                replacements[record_id] = replacement

    output_directory.mkdir(parents=True)
    output_path = output_directory / "predictions.jsonl"
    row_count = 0
    valid = 0
    provider_failures = 0
    refused = 0
    missing = 0
    invalid = 0
    seen_replacements: set[str] = set()
    with output_path.open("w", encoding="utf-8", newline="\n") as target:
        for original in iter_jsonl(original_directory / "predictions.jsonl"):
            record_id = original.get("record_id")
            if not isinstance(record_id, str):
                raise RecoveryArtifactError("original predictions contain an invalid record ID")
            row = replacements.get(record_id, original)
            if record_id in replacements:
                seen_replacements.add(record_id)
                row["request_index"] = original.get("request_index", row_count)
            status = row.get("status")
            valid += status == "success"
            refused += status == "provider_refusal"
            provider_failures += status == "provider_failure"
            missing += status == "missing"
            invalid += status not in {
                "success",
                "provider_refusal",
                "provider_failure",
                "missing",
            }
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            row_count += 1
    outside = set(replacements) - seen_replacements
    if outside:
        raise RecoveryArtifactError("recovery contains a record outside the campaign")

    summary = copy.deepcopy(original_summary)
    coverage = summary.setdefault("coverage", {})
    planned = int(coverage.get("planned_requests") or row_count)
    coverage.update(
        {
            "provider_request_count": planned,
            "collected_responses": row_count - missing,
            "missing_responses": missing + max(0, planned - row_count),
            "duplicate_mappings": 0,
            "successful_provider_responses": row_count - provider_failures - missing,
            "failed_provider_responses": provider_failures,
            "valid_structured_predictions": valid,
            "invalid_structured_predictions": invalid,
            "provider_refused": refused,
            "provider_failures_collected": provider_failures,
            "valid": valid,
            "structurally_repaired": 0,
            "invalid": invalid,
            "failed": provider_failures,
            "missing": missing,
            "duplicate": 0,
            "accounting_invariant_ok": planned == row_count and missing == 0,
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
        0,
        int(tokens.get("input_tokens") or 0)
        - int(tokens.get("cached_input_tokens") or 0),
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
    (output_directory / "operational_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary, row_count, artifact_sha256(output_directory)
