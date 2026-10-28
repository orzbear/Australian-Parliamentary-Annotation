"""Prepare and compare the frozen hostility model benchmark without provider calls."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, TextIO

from hansard_annotator.llm_eval.costs import aggregate_costs, load_pricing
from hansard_annotator.llm_eval.hostility import compute_metrics
from hansard_annotator.llm_eval.hostility_validation import (
    CONTRACT_SHA256,
    HELDOUT_SAMPLE_ID,
    HELDOUT_SAMPLE_SHA256,
    PROMPT_SHA256,
    PROMPT_VERSION,
    validate_heldout_definition,
)
from hansard_annotator.llm_eval.hostility_validation import (
    MODEL as BASELINE_MODEL,
)
from hansard_annotator.llm_eval.operations import canonical_hash, production_record_from_turn
from hansard_annotator.llm_eval.package import load_evaluation_package

BASELINE_RUN_ID = "fe145bdb-ed2d-44da-ab5e-620efc2abfa0"
CANDIDATE_MODEL = "gemini-3.1-flash-lite"
SCHEMA_SHA256 = "959a51a987e0777cb72dc7ad12cd5b896429bef400b79f386764a20ddc3d5669"
CANONICAL_CORPUS_RUN = "phase1-full-20260723-v4"


def _parse_json(value: object, label: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{label} is not valid JSON") from error
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _audit_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("review audit CSV is empty")
    return rows


def _verified_run(summary_path: Path) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    run = summary.get("run")
    if not isinstance(run, dict):
        raise ValueError("review audit summary has no run provenance")
    if run.get("public_id") != BASELINE_RUN_ID:
        raise ValueError("review audit is not the frozen Gemini 2.5 Flash baseline run")
    expected = {
        "prediction_task": "hostility",
        "annotation_schema_slug": "australian_partisan_hostility",
        "annotation_schema_version": "0.2.0",
        "provider": "gemini",
        "configured_model": BASELINE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "canonical_schema_sha256": SCHEMA_SHA256,
        "prediction_contract_sha256": CONTRACT_SHA256,
        "generation_config": {"thinking_budget": 0},
    }
    for key, value in expected.items():
        if run.get(key) != value:
            raise ValueError(f"baseline run provenance mismatch: {key}")
    return run


def build_records(
    turns: list[dict[str, Any]], audit_rows: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Join final reviewed labels and speech text to the frozen selection by turn key."""
    expected_keys = [str(row["turn_key"]) for row in turns]
    if len(set(expected_keys)) != len(expected_keys):
        raise ValueError("frozen sample contains duplicate turn keys")
    indexed: dict[str, dict[str, str]] = {}
    for row in audit_rows:
        if row.get("run_id") != BASELINE_RUN_ID:
            raise ValueError("audit CSV contains a row from another prediction run")
        if row.get("prediction_task") != "hostility":
            raise ValueError("audit CSV contains a non-hostility prediction")
        key = row.get("turn_key", "")
        if not key or key in indexed:
            raise ValueError("audit CSV has a missing or duplicate turn key")
        if row.get("terminal_review_action") not in {
            "accepted",
            "edited",
            "human_annotated_after_ai_failure",
        }:
            raise ValueError(f"turn {key} has no final human-reviewed label")
        if row.get("ai_valid_prediction", "").lower() not in {"true", "false"}:
            raise ValueError(f"turn {key} has an invalid AI validity marker")
        if not row.get("record_id") or not row.get("speech_text"):
            raise ValueError(f"turn {key} is missing record ID or canonical speech text")
        indexed[key] = row
    if set(indexed) != set(expected_keys):
        missing = sorted(set(expected_keys) - set(indexed))
        extra = sorted(set(indexed) - set(expected_keys))
        raise ValueError(f"audit/frozen sample mismatch; missing={missing[:3]}, extra={extra[:3]}")

    output: list[dict[str, Any]] = []
    for turn in turns:
        row = indexed[str(turn["turn_key"])]
        source_record = production_record_from_turn(
            {
                "turn_key": str(turn["turn_key"]),
                "date": row.get("sitting_date"),
                "chamber": row.get("chamber") or None,
                "speaker_name_raw": row.get("speaker") or None,
                "major_heading_original": row.get("major_heading") or None,
                "minor_heading_original": row.get("minor_heading") or None,
                "business_type": row.get("business_type") or None,
                "text_clean": row["speech_text"],
            }
        )
        expected_record_hash = row.get("canonical_record_sha256")
        if not expected_record_hash:
            raise ValueError(
                f"turn {turn['turn_key']} has no immutable campaign input hash to verify canonical text"
            )
        if row["record_id"] != source_record["record_id"]:
            raise ValueError(f"turn {turn['turn_key']} has an unexpected model record ID")
        if canonical_hash(source_record) != expected_record_hash:
            raise ValueError(
                f"turn {turn['turn_key']} canonical text/metadata differs from the frozen model input"
            )
        values = _parse_json(row.get("human_values", ""), "human_values")
        if not isinstance(values, dict):
            raise ValueError(f"turn {turn['turn_key']} has no final human values")
        speech = source_record["speech"]
        canonical_values = json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        output.append(
            {
                "record_id": row["record_id"],
                "speech": speech,
                "human_annotation": {
                    "status": "submitted",
                    "revision": 1,
                    "values": values,
                    "values_sha256": _sha256(canonical_values.encode("utf-8")),
                },
            }
        )
    if len({str(record["record_id"]) for record in output}) != len(output):
        raise ValueError("review audit has duplicate record IDs")
    return output


def prepare_package(
    audit_csv: Path,
    audit_summary: Path,
    sample_path: Path,
    context_path: Path,
    output: Path,
) -> dict[str, Any]:
    run = _verified_run(audit_summary)
    identity = validate_heldout_definition(sample_path)
    if identity.sample_id != HELDOUT_SAMPLE_ID or identity.sample_sha256 != HELDOUT_SAMPLE_SHA256:
        raise ValueError("frozen sample identity does not match the approved definition")
    turns_doc = json.loads(sample_path.read_text(encoding="utf-8"))
    turns = turns_doc.get("turns")
    if not isinstance(turns, list):
        raise ValueError("frozen sample has no turns list")
    records = build_records(turns, _audit_rows(audit_csv))
    context = json.loads(context_path.read_text(encoding="utf-8"))
    schema = context.get("schema")
    if not isinstance(schema, dict) or schema.get("content_sha256") != SCHEMA_SHA256:
        raise ValueError("hostility evaluation codebook context has changed")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite benchmark input: {output}")

    output.mkdir(parents=True)
    annotations = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")
    context_bytes = (
        json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    files = {
        "annotations.jsonl": annotations,
        "CODEBOOK_CONTEXT.json": context_bytes,
    }
    for name, content in files.items():
        (output / name).write_bytes(content)
    file_metadata = {
        name: {"bytes": len(content), "sha256": _sha256(content)} for name, content in files.items()
    }
    snapshot = _sha256(
        b"".join(name.encode("utf-8") + b"\0" + files[name] for name in sorted(files))
    )
    manifest = {
        "format": "hansard-ai-codebook-input",
        "format_version": 1,
        "record_count": len(records),
        "corpus": {"run_name": CANONICAL_CORPUS_RUN},
        "selection": "frozen comparative benchmark set; no resampling",
        "benchmark": {
            "set_id": identity.sample_id,
            "set_sha256": identity.sample_sha256,
            "human_reference_run_id": BASELINE_RUN_ID,
            "human_labels": "final human-reviewed annotation versions",
        },
        "files": file_metadata,
        "snapshot_sha256": snapshot,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    # Read through the production package validator before making this usable by evaluation.
    package = load_evaluation_package(output)
    return {
        "records": len(records),
        "set_id": identity.sample_id,
        "set_sha256": identity.sample_sha256,
        "reference_run_id": run["public_id"],
        "package_snapshot_sha256": package.manifest["snapshot_sha256"],
        "output": str(output),
    }


def _bool(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _baseline_results(audit_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in audit_rows:
        valid = _bool(row.get("ai_valid_prediction"))
        prediction = _parse_json(row.get("ai_prediction_values", ""), "ai_prediction_values")
        if valid and isinstance(prediction, dict):
            results.append(
                {
                    "record_id": row["record_id"],
                    "status": "success",
                    "prediction": prediction,
                    "diagnostics": {"evidence_valid": True},
                    "usage": {},
                }
            )
        else:
            diagnostic = str(row.get("ai_failure_diagnostic") or row.get("ai_failure_reason") or "")
            results.append(
                {
                    "record_id": row["record_id"],
                    "status": "invalid",
                    "prediction": None,
                    "error_type": "InvalidPrediction",
                    "error_message": diagnostic,
                    "diagnostics": {
                        "evidence_unmatched": [diagnostic]
                        if "unmatched" in diagnostic.lower()
                        else [],
                        "evidence_ambiguous": [diagnostic]
                        if "ambiguous" in diagnostic.lower()
                        else [],
                    },
                    "usage": {},
                }
            )
    return results


def _reliability(
    records: tuple[dict[str, Any], ...], results: list[dict[str, Any]]
) -> dict[str, Any]:
    by_id = {str(result["record_id"]): result for result in results}
    statuses: Counter[str] = Counter()
    evidence_failures = 0
    for record in records:
        result = by_id.get(str(record["record_id"]))
        status = str(result.get("status")) if result else "missing"
        statuses[status] += 1
        if result and (
            result.get("diagnostics", {}).get("evidence_unmatched")
            or result.get("diagnostics", {}).get("evidence_ambiguous")
            or "evidence" in str(result.get("error_message", "")).lower()
        ):
            evidence_failures += 1
    attempted = len(records)
    valid = statuses["success"]
    return {
        "attempted": attempted,
        "valid": valid,
        "invalid_structured_output": statuses["invalid"] - evidence_failures,
        "evidence_resolution_failures": evidence_failures,
        "provider_api_failures": statuses["api_failure"],
        "missing": statuses["missing"],
        "terminal_failures": statuses["invalid"] + statuses["api_failure"] + statuses["missing"],
        "coverage": valid / attempted if attempted else 0.0,
        "status_counts": {
            key: statuses[key] for key in ("success", "invalid", "api_failure", "missing")
        },
    }


def _binary_yes_metrics(
    records: tuple[dict[str, Any], ...], results: list[dict[str, Any]]
) -> dict[str, Any]:
    by_id = {str(result["record_id"]): result for result in results}
    tp = tn = fp = fn = 0
    for record in records:
        result = by_id.get(str(record["record_id"]))
        prediction = result.get("prediction") if result else None
        if result is None or result.get("status") != "success" or not isinstance(prediction, dict):
            continue
        human_yes = record["human_annotation"]["values"]["hostility_present"] == "YES"
        model_yes = prediction.get("hostility_present") == "YES"
        if human_yes and model_yes:
            tp += 1
        elif human_yes:
            fn += 1
        elif model_yes:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    total = tp + tn + fp + fn
    return {
        "positive_label": "YES",
        "negative_labels": ["NO", "UNSURE"],
        "scored_valid_outputs": total,
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "accuracy": (tp + tn) / total if total else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": (2 * precision * recall / (precision + recall)) if precision + recall else 0.0,
    }


def _transition(record: dict[str, Any], result: dict[str, Any] | None) -> str:
    if result is None:
        return "MISSING_OUTPUT"
    if result.get("status") == "api_failure":
        return "PROVIDER_API_FAILURE"
    if result.get("status") != "success":
        message = str((result or {}).get("error_message", "")).lower()
        if (
            "evidence" in message
            or (result or {}).get("diagnostics", {}).get("evidence_unmatched")
            or (result or {}).get("diagnostics", {}).get("evidence_ambiguous")
        ):
            return "EVIDENCE_RESOLUTION_FAILURE"
        return "OTHER_INVALID_OUTPUT"
    human = record["human_annotation"]["values"]
    prediction = result["prediction"]
    hp, mp = human["hostility_present"], prediction["hostility_present"]
    ht, mt = human.get("target"), prediction.get("target")
    if hp == mp and (hp != "YES" or ht == mt):
        return "AGREEMENT"
    if hp == "YES" and mp == "NO":
        return "YES_TO_NO"
    if hp == "NO" and mp == "YES":
        return "NO_TO_YES"
    if hp == mp and hp == "YES" and ht != mt:
        return "TARGET_ONLY_DISAGREEMENT"
    if hp != mp and ht != mt:
        return "HOSTILITY_AND_TARGET_DISAGREEMENT"
    return "OTHER_VALID_DISAGREEMENT"


def _load_candidate(
    candidate_dir: Path, package: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata_path = candidate_dir / "run_metadata.json"
    predictions_path = candidate_dir / "predictions.jsonl"
    if not metadata_path.is_file() or not predictions_path.is_file():
        raise ValueError("candidate directory must contain run_metadata.json and predictions.jsonl")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    configuration = metadata.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("candidate run metadata has no configuration")
    for key, expected in {
        "provider": "gemini",
        "model": CANDIDATE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "schema_sha256": SCHEMA_SHA256,
        "package_snapshot_sha256": package.manifest["snapshot_sha256"],
    }.items():
        if configuration.get(key) != expected:
            raise ValueError(f"candidate run provenance mismatch: {key}")
    generation = configuration.get("generation")
    if (
        not isinstance(generation, dict)
        or generation.get("thinking_level") != "minimal"
        or generation.get("temperature") != 0.0
        or generation.get("max_output_tokens") != 1000
    ):
        raise ValueError(
            "candidate run must use Gemini 3.1 Flash-Lite temperature=0, "
            "max_output_tokens=1000, thinking_level=minimal"
        )
    results = []
    for number, line in enumerate(predictions_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            result = _parse_json(line, f"candidate predictions line {number}")
            if not isinstance(result, dict):
                raise ValueError(f"candidate prediction line {number} is not an object")
            results.append(result)
    return metadata, results


def compare_benchmark(
    audit_csv: Path,
    audit_summary: Path,
    package_path: Path,
    candidate_dir: Path,
    output: Path,
    pricing_path: Path,
) -> dict[str, Any]:
    baseline_run = _verified_run(audit_summary)
    package = load_evaluation_package(package_path)
    if package.manifest.get("benchmark", {}).get("set_sha256") != HELDOUT_SAMPLE_SHA256:
        raise ValueError("benchmark package is not the frozen comparative set")
    rows = _audit_rows(audit_csv)
    sample_path = (
        Path(__file__).resolve().parents[3] / "config/research_samples/hostility-heldout-100.json"
    )
    identity = validate_heldout_definition(sample_path)
    sample_doc = json.loads(sample_path.read_text(encoding="utf-8"))
    rebuilt_records = build_records(sample_doc["turns"], rows)
    if identity.sample_sha256 != package.manifest.get("benchmark", {}).get("set_sha256"):
        raise ValueError("benchmark sample identity does not match the committed frozen definition")
    if [_canonical(row) for row in rebuilt_records] != [_canonical(row) for row in package.records]:
        raise ValueError("benchmark input package does not match the frozen reference rows")
    # Rejoin against the package to verify exact reference identity and order.
    expected_ids = {str(record["record_id"]) for record in package.records}
    if {row.get("record_id") for row in rows} != expected_ids:
        raise ValueError("audit CSV and benchmark input record IDs differ")
    reference_rows = {row["record_id"]: row for row in rows}
    baseline = _baseline_results(rows)
    candidate_meta, candidate = _load_candidate(candidate_dir, package)
    baseline_by_id = {str(result["record_id"]): result for result in baseline}
    candidate_by_id = {str(result["record_id"]): result for result in candidate}
    candidate_ids = [str(result["record_id"]) for result in candidate]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate run contains duplicate record IDs")
    if not set(candidate_ids).issubset(expected_ids):
        raise ValueError("candidate run contains records outside the frozen benchmark set")
    ordered_ids = [str(record["record_id"]) for record in package.records]
    baseline = [baseline_by_id[key] for key in ordered_ids]
    candidate = [candidate_by_id[key] for key in ordered_ids if key in candidate_by_id]
    baseline_metrics = compute_metrics(package.records, baseline)
    candidate_metrics = compute_metrics(package.records, candidate)
    baseline_metrics["binary_yes_vs_rest"] = _binary_yes_metrics(package.records, baseline)
    candidate_metrics["binary_yes_vs_rest"] = _binary_yes_metrics(package.records, candidate)
    baseline_reliability = _reliability(package.records, baseline)
    baseline_attempted = int(baseline_run.get("requested_count") or len(package.records))
    baseline_valid = int(baseline_run.get("valid_count") or baseline_reliability["valid"])
    baseline_invalid = int(baseline_run.get("invalid_count") or 0)
    baseline_api_failures = int(baseline_run.get("provider_failure_count") or 0)
    baseline_reliability.update(
        {
            "attempted": baseline_attempted,
            "valid": baseline_valid,
            "invalid_structured_output": max(
                0, baseline_invalid - baseline_reliability["evidence_resolution_failures"]
            ),
            "provider_api_failures": baseline_api_failures,
            "missing": max(
                0, baseline_attempted - baseline_valid - baseline_invalid - baseline_api_failures
            ),
            "terminal_failures": baseline_invalid + baseline_api_failures,
            "coverage": baseline_valid / baseline_attempted if baseline_attempted else 0.0,
            "status_counts": {
                "success": baseline_valid,
                "invalid": baseline_invalid,
                "api_failure": baseline_api_failures,
                "missing": max(
                    0,
                    baseline_attempted - baseline_valid - baseline_invalid - baseline_api_failures,
                ),
            },
        }
    )
    candidate_reliability = _reliability(package.records, candidate)
    categories: dict[str, dict[str, int]] = {}
    transition_rows = []
    for record in package.records:
        record_id = str(record["record_id"])
        candidate_result = candidate_by_id.get(record_id)
        baseline_result = baseline_by_id[record_id]
        for model_name, result in (
            (BASELINE_MODEL, baseline_result),
            (CANDIDATE_MODEL, candidate_result),
        ):
            category = _transition(record, result)
            counts = categories.setdefault(model_name, Counter())
            counts[category] = counts.get(category, 0) + 1
        transition_rows.append(
            {
                "record_id": record_id,
                "turn_key": record["speech"]["turn_key"],
                "human_hostility": record["human_annotation"]["values"]["hostility_present"],
                "human_target": record["human_annotation"]["values"].get("target"),
                "baseline_category": _transition(record, baseline_result),
                "baseline_hostility": (
                    baseline_result.get("prediction", {}).get("hostility_present")
                    if baseline_result.get("prediction")
                    else None
                ),
                "baseline_target": (
                    baseline_result.get("prediction", {}).get("target")
                    if baseline_result.get("prediction")
                    else None
                ),
                "candidate_category": _transition(record, candidate_result),
                "candidate_hostility": (
                    candidate_result.get("prediction", {}).get("hostility_present")
                    if candidate_result and candidate_result.get("prediction")
                    else None
                ),
                "candidate_target": (
                    candidate_result.get("prediction", {}).get("target")
                    if candidate_result and candidate_result.get("prediction")
                    else None
                ),
                "candidate_error": candidate_result.get("error_message")
                if candidate_result
                else "missing result",
                "baseline_error": reference_rows[record_id].get("ai_failure_diagnostic"),
            }
        )

    pricing = load_pricing(pricing_path)
    candidate_cost = aggregate_costs(candidate, "gemini", CANDIDATE_MODEL, pricing)
    baseline_actual = baseline_run.get("total_cost_usd")
    baseline_pricing_mode = str(baseline_run.get("execution_mode") or "batch").lower()
    baseline_estimate = aggregate_costs(
        [
            {
                "status": "success",
                "usage": {
                    "input_tokens": baseline_run.get("input_tokens") or 0,
                    "cached_input_tokens": baseline_run.get("cached_input_tokens") or 0,
                    "output_tokens": baseline_run.get("output_tokens") or 0,
                    "reasoning_tokens": baseline_run.get("reasoning_tokens") or 0,
                },
            }
        ],
        "gemini",
        BASELINE_MODEL,
        pricing,
        mode=baseline_pricing_mode,
    )
    baseline_cost_basis = (
        float(baseline_actual)
        if baseline_actual is not None
        else baseline_estimate["estimated_cost_usd"]
    )
    baseline_attempts = int(baseline_run.get("requested_count") or len(baseline))
    baseline_valid = int(baseline_run.get("valid_count") or 0)
    baseline_cost_per_attempt = (
        float(baseline_cost_basis) / baseline_attempts
        if baseline_cost_basis is not None and baseline_attempts
        else None
    )
    baseline_cost_per_valid = (
        float(baseline_cost_basis) / baseline_valid
        if baseline_cost_basis is not None and baseline_valid
        else None
    )
    cost_comparison = {
        "baseline_model": BASELINE_MODEL,
        "baseline_execution_mode": baseline_run.get("execution_mode"),
        "baseline_pricing_estimate": baseline_estimate,
        "baseline_tokens": {
            "input_tokens": baseline_run.get("input_tokens"),
            "cached_input_tokens": baseline_run.get("cached_input_tokens"),
            "output_tokens": baseline_run.get("output_tokens"),
            "reasoning_tokens": baseline_run.get("reasoning_tokens"),
        },
        "baseline_actual_cost_usd": float(baseline_actual) if baseline_actual is not None else None,
        "baseline_cost_basis_usd": baseline_cost_basis,
        "baseline_cost_basis_source": (
            "imported_run_total_cost_usd"
            if baseline_actual is not None
            else "centralized_pricing_estimate"
        ),
        "baseline_attempts": baseline_attempts,
        "baseline_valid_annotations": baseline_valid,
        "baseline_cost_per_attempt_usd": baseline_cost_per_attempt,
        "baseline_cost_per_valid_annotation_usd": baseline_cost_per_valid,
        "baseline_projected_cost_160k_attempts_usd": (
            baseline_cost_per_attempt * 160_000 if baseline_cost_per_attempt is not None else None
        ),
        "baseline_projected_cost_160k_valid_annotations_usd": (
            baseline_cost_per_valid * 160_000 if baseline_cost_per_valid is not None else None
        ),
        "candidate_model": CANDIDATE_MODEL,
        "candidate_generation": candidate_meta["configuration"]["generation"],
        "candidate_pricing_mode": "standard",
        "candidate_cost_report": candidate_cost,
        "candidate_cost_per_attempt_usd": candidate_cost["cost_per_attempt_usd"],
        "candidate_cost_per_valid_annotation_usd": candidate_cost["cost_per_valid_annotation_usd"],
        "candidate_projected_cost_160k_attempts_usd": candidate_cost[
            "projected_cost_160k_attempts_usd"
        ],
        "candidate_projected_cost_160k_valid_annotations_usd": candidate_cost[
            "projected_cost_160k_valid_annotations_usd"
        ],
        "baseline_note": "Imported run total_cost_usd from immutable prediction-run metadata.",
    }
    comparison = {
        "benchmark_name": "Frozen comparative benchmark set",
        "benchmark_set_id": HELDOUT_SAMPLE_ID,
        "benchmark_set_sha256": HELDOUT_SAMPLE_SHA256,
        "reference_run_id": BASELINE_RUN_ID,
        "reference_labels": "Final human-reviewed annotations from the reference run.",
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "schema_sha256": SCHEMA_SHA256,
        "records": len(package.records),
        "binary_convention": "Human YES is positive; NO and UNSURE are negative; only valid model outputs are scored.",
        "metrics": {
            BASELINE_MODEL: baseline_metrics,
            CANDIDATE_MODEL: candidate_metrics,
        },
        "reliability": {
            BASELINE_MODEL: baseline_reliability,
            CANDIDATE_MODEL: candidate_reliability,
        },
        "ai_to_human_transitions": categories,
        "cost": cost_comparison,
        "interpretation_order": [
            "false negatives",
            "reliability and coverage",
            "recall",
            "false positives",
            "partisan-hostility metrics",
            "cost",
        ],
        "recommendation": None,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    with (output / "record_transitions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(transition_rows[0]))
        writer.writeheader()
        writer.writerows(transition_rows)
    lines = [
        "# Frozen comparative hostility benchmark",
        "",
        f"Set: `{HELDOUT_SAMPLE_ID}` ({len(package.records)} records; `{HELDOUT_SAMPLE_SHA256}`).",
        f"Reference labels: final human-reviewed annotations from `{BASELINE_RUN_ID}`.",
        f"Prompt: `{PROMPT_VERSION}` / `{PROMPT_SHA256}`; schema: `{SCHEMA_SHA256}`.",
        "",
        "Human YES is positive; NO and UNSURE are negative. Invalid outputs are excluded from label metrics and reported in reliability.",
        "",
        "| Measure | Gemini 2.5 Flash | Gemini 3.1 Flash-Lite |",
        "|---|---:|---:|",
    ]
    for label, path in (
        ("False negatives", ("binary_yes_vs_rest", "false_negative")),
        ("Recall", ("binary_yes_vs_rest", "recall")),
        ("False positives", ("binary_yes_vs_rest", "false_positive")),
        ("Accuracy", ("binary_yes_vs_rest", "accuracy")),
        ("Target exact agreement", ("target_classification", "exact_target_accuracy")),
        ("Partisan-hostility F1", ("partisan_hostility", "f1")),
    ):
        values = []
        for _model_name, model_metrics in (
            (BASELINE_MODEL, baseline_metrics),
            (CANDIDATE_MODEL, candidate_metrics),
        ):
            value: Any = model_metrics
            for key in path:
                value = value[key]
            values.append(f"{value:.3f}" if isinstance(value, float) else str(value))
        lines.append(f"| {label} | {values[0]} | {values[1]} |")
    lines.extend(
        [
            "",
            "## Reliability and transitions",
            "",
            f"- Gemini 2.5 Flash: {baseline_reliability['valid']}/{baseline_reliability['attempted']} valid; {baseline_reliability['terminal_failures']} terminal failures.",
            f"- Gemini 3.1 Flash-Lite: {candidate_reliability['valid']}/{candidate_reliability['attempted']} valid; {candidate_reliability['terminal_failures']} terminal failures.",
            f"- YES → NO: {categories[BASELINE_MODEL].get('YES_TO_NO', 0)} baseline; {categories[CANDIDATE_MODEL].get('YES_TO_NO', 0)} candidate.",
            f"- NO → YES: {categories[BASELINE_MODEL].get('NO_TO_YES', 0)} baseline; {categories[CANDIDATE_MODEL].get('NO_TO_YES', 0)} candidate.",
            "- Detailed deterministic per-record transitions are in `record_transitions.csv`.",
            "",
            "## Cost",
            "",
            f"- Baseline imported run cost: {cost_comparison['baseline_actual_cost_usd']} USD ({cost_comparison['baseline_execution_mode']} mode); centralized token-pricing estimate: {baseline_estimate['estimated_cost_usd']} USD.",
            f"- Baseline cost per attempt / valid annotation: {cost_comparison['baseline_cost_per_attempt_usd']} / {cost_comparison['baseline_cost_per_valid_annotation_usd']} USD.",
            f"- Baseline projected 160k attempts / valid annotations: {cost_comparison['baseline_projected_cost_160k_attempts_usd']} / {cost_comparison['baseline_projected_cost_160k_valid_annotations_usd']} USD.",
            f"- Candidate measured usage estimate: {candidate_cost['estimated_cost_usd']} USD (standard API pricing).",
            f"- Candidate cost per attempt / valid annotation: {candidate_cost['cost_per_attempt_usd']} / {candidate_cost['cost_per_valid_annotation_usd']} USD.",
            f"- Candidate projected 160k attempts: {candidate_cost['projected_cost_160k_attempts_usd']} USD.",
            f"- Candidate projected 160k valid annotations: {candidate_cost['projected_cost_160k_valid_annotations_usd']} USD.",
            "- Projections are linear estimates from this 100-record run; they are not production forecasts.",
            "",
            "This is a frozen comparative benchmark set, not a new untouched held-out set. Metrics are descriptive; no model recommendation is made.",
        ]
    )
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return comparison


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="build the frozen benchmark evaluator package")
    prepare.add_argument("--audit-csv", type=Path, required=True)
    prepare.add_argument("--audit-summary", type=Path, required=True)
    prepare.add_argument(
        "--sample", type=Path, default=Path("config/research_samples/hostility-heldout-100.json")
    )
    prepare.add_argument(
        "--context", type=Path, default=Path("config/llm_eval/hostility-v2-codebook-context.json")
    )
    prepare.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare", help="write side-by-side metrics and cost report")
    compare.add_argument("--audit-csv", type=Path, required=True)
    compare.add_argument("--audit-summary", type=Path, required=True)
    compare.add_argument("--package", type=Path, required=True)
    compare.add_argument("--candidate-run", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--pricing", type=Path, default=Path("config/llm_eval/pricing.yaml"))
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = prepare_package(
                arguments.audit_csv,
                arguments.audit_summary,
                arguments.sample,
                arguments.context,
                arguments.output,
            )
        else:
            result = compare_benchmark(
                arguments.audit_csv,
                arguments.audit_summary,
                arguments.package,
                arguments.candidate_run,
                arguments.output,
                arguments.pricing,
            )
        print(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            file=stdout,
        )
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
