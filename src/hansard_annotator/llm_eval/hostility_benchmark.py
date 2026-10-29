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
from hansard_annotator.llm_eval.operations import canonical_hash
from hansard_annotator.llm_eval.package import load_evaluation_package
from hansard_annotator.llm_eval.prompt import build_user_prompt

BASELINE_RUN_ID = "fe145bdb-ed2d-44da-ab5e-620efc2abfa0"
CANDIDATE_MODEL = "gemini-3.1-flash-lite"
LUNA_MODEL = "gpt-6-luna"
SCHEMA_SHA256 = "959a51a987e0777cb72dc7ad12cd5b896429bef400b79f386764a20ddc3d5669"
CANONICAL_CORPUS_RUN = "phase1-full-20260723-v4"
BASELINE_CAMPAIGN_ID = "25f2f6da-e698-4a34-871f-3dc377544495"


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


def _request_text(entry: dict[str, Any], key: str) -> str:
    request = entry.get("request")
    contents = request.get("contents") if isinstance(request, dict) else None
    if not isinstance(contents, list) or len(contents) != 1:
        raise ValueError(f"frozen campaign request for {key} must contain one contents item")
    item = contents[0]
    parts = item.get("parts") if isinstance(item, dict) else None
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        raise ValueError(f"frozen campaign request for {key} must contain one text part")
    text = parts[0].get("text")
    if not isinstance(text, str):
        raise ValueError(f"frozen campaign request for {key} has no text payload")
    return text


def _record_from_plan(entry: dict[str, Any], key: str) -> dict[str, Any]:
    text = _request_text(entry, key)
    prefix = "METADATA:\n"
    separator = "\n\nSPEECH:\n"
    if not text.startswith(prefix) or separator not in text[len(prefix) :]:
        raise ValueError(f"frozen campaign request for {key} has an unexpected prompt structure")
    metadata_text, speech_text = text[len(prefix) :].split(separator, 1)
    try:
        metadata = json.loads(metadata_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"frozen campaign request for {key} has invalid METADATA JSON") from error
    metadata_keys = {
        "date",
        "chamber",
        "speaker_name",
        "major_heading",
        "minor_heading",
        "business_type",
    }
    if not isinstance(metadata, dict) or set(metadata) != metadata_keys:
        raise ValueError(f"frozen campaign request for {key} has unexpected metadata fields")
    record = {
        "record_id": f"turn:{key}",
        "speech": {"turn_key": key, **metadata, "text": speech_text},
    }
    if build_user_prompt(record) != text:
        raise ValueError(f"frozen campaign request for {key} does not round-trip exactly")
    return record


def _plan_entries(path: Path, expected_keys: set[str]) -> tuple[dict[str, dict[str, Any]], str, dict[str, Any]]:
    raw = path.read_bytes()
    plan_sha256 = _sha256(raw)
    try:
        plan = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("baseline campaign plan is not valid UTF-8 JSON") from error
    provenance = plan.get("provenance") if isinstance(plan, dict) else None
    request = plan.get("request") if isinstance(plan, dict) else None
    batch = request.get("batch") if isinstance(request, dict) else None
    input_config = batch.get("input_config") if isinstance(batch, dict) else None
    wrapped = input_config.get("requests") if isinstance(input_config, dict) else None
    entries = wrapped.get("requests") if isinstance(wrapped, dict) else None
    if not isinstance(provenance, dict) or not isinstance(entries, list):
        raise ValueError("baseline campaign plan is missing provenance or request entries")
    if provenance.get("prompt_version") != PROMPT_VERSION:
        raise ValueError("baseline campaign plan prompt version mismatch")
    if provenance.get("assembled_prompt_sha256") != PROMPT_SHA256:
        raise ValueError("baseline campaign plan prompt hash mismatch")
    if provenance.get("prediction_contract_sha256") != CONTRACT_SHA256:
        raise ValueError("baseline campaign plan prediction contract mismatch")
    if provenance.get("canonical_schema_sha256") != SCHEMA_SHA256:
        raise ValueError("baseline campaign plan canonical schema mismatch")
    if provenance.get("model") != BASELINE_MODEL or provenance.get("provider") != "gemini":
        raise ValueError("baseline campaign plan model/provider mismatch")
    selected: dict[str, dict[str, Any]] = {}
    baseline_request_provenance: dict[str, Any] | None = None
    for entry in entries:
        metadata = entry.get("metadata") if isinstance(entry, dict) else None
        key_value = metadata.get("key") if isinstance(metadata, dict) else None
        if not isinstance(key_value, str) or not key_value.startswith("turn:"):
            continue
        key = key_value.removeprefix("turn:")
        if key not in expected_keys:
            continue
        if key in selected:
            raise ValueError(f"baseline campaign plan has duplicate selected turn {key}")
        selected_request = entry.get("request")
        if not isinstance(selected_request, dict):
            raise ValueError(f"baseline campaign request for {key} is malformed")
        request_provenance = selected_request.get("generationConfig")
        if not isinstance(request_provenance, dict):
            raise ValueError(f"baseline campaign request for {key} has no generation configuration")
        if baseline_request_provenance is None:
            baseline_request_provenance = request_provenance
        elif request_provenance != baseline_request_provenance:
            raise ValueError("baseline campaign plan has inconsistent request configuration")
        selected[key] = entry
    missing = sorted(expected_keys - set(selected))
    if missing:
        raise ValueError(f"baseline campaign plan is missing selected turns: {missing[:3]}")
    if baseline_request_provenance is None:
        raise ValueError("baseline campaign plan has no selected request configuration")
    if baseline_request_provenance.get("thinkingConfig") != {"thinkingBudget": 0}:
        raise ValueError("baseline campaign plan thinking configuration mismatch")
    response_schema = baseline_request_provenance.get("responseSchema")
    if (
        not isinstance(response_schema, dict)
        or provenance.get("provider_schema_sha256") != canonical_hash(response_schema)
    ):
        raise ValueError("baseline campaign plan response schema hash mismatch")
    provenance = dict(provenance)
    provenance["request_configuration"] = baseline_request_provenance
    return selected, plan_sha256, provenance


def build_records(
    turns: list[dict[str, Any]],
    audit_rows: list[dict[str, str]],
    plan_entries: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join immutable plan inputs and final reviewed labels by frozen turn key."""
    expected_keys = [str(row["turn_key"]) for row in turns]
    if len(expected_keys) != 100:
        raise ValueError("frozen hostility benchmark must contain exactly 100 turn keys")
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
        key = str(turn["turn_key"])
        source_record = _record_from_plan(plan_entries[key], key)
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
    baseline_plan: Path,
) -> dict[str, Any]:
    run = _verified_run(audit_summary)
    identity = validate_heldout_definition(sample_path)
    if identity.sample_id != HELDOUT_SAMPLE_ID or identity.sample_sha256 != HELDOUT_SAMPLE_SHA256:
        raise ValueError("frozen sample identity does not match the approved definition")
    turns_doc = json.loads(sample_path.read_text(encoding="utf-8"))
    turns = turns_doc.get("turns")
    if not isinstance(turns, list):
        raise ValueError("frozen sample has no turns list")
    plan_entries, plan_sha256, plan_provenance = _plan_entries(
        baseline_plan, {str(turn["turn_key"]) for turn in turns}
    )
    records = build_records(turns, _audit_rows(audit_csv), plan_entries)
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
            "source_type": "original_campaign_plan",
            "source_baseline_campaign_id": BASELINE_CAMPAIGN_ID,
            "human_reference_run_id": BASELINE_RUN_ID,
            "human_labels": "final human-reviewed annotation versions",
            "source_plan_sha256": plan_sha256,
            "selected_record_count": len(records),
            "turn_key_set_sha256": canonical_hash(sorted(str(turn["turn_key"]) for turn in turns)),
            "prompt_version": plan_provenance["prompt_version"],
            "prompt_sha256": PROMPT_SHA256,
            "canonical_schema_sha256": SCHEMA_SHA256,
            "prediction_contract_sha256": CONTRACT_SHA256,
            "baseline_provider_schema_sha256": plan_provenance["provider_schema_sha256"],
            "baseline_generation": plan_provenance["request_configuration"],
            "candidate_generation": {
                "model": CANDIDATE_MODEL,
                "temperature": 0,
                "max_output_tokens": 1000,
                "thinking_level": "minimal",
            },
            "active_provider_configuration": {
                "provider": "gemini",
                "model": CANDIDATE_MODEL,
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": PROMPT_SHA256,
                "schema_sha256": SCHEMA_SHA256,
                "generation": {
                    "temperature": 0,
                    "max_output_tokens": 1000,
                    "thinking_level": "minimal",
                },
            },
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
        if valid:
            prediction = _parse_json(row.get("ai_prediction_values", ""), "ai_prediction_values")
            if not isinstance(prediction, dict):
                raise ValueError("valid baseline prediction must be a JSON object")
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
            outcome = str(row.get("ai_outcome_status") or "")
            if outcome in {"", "PREDICTION"}:
                raise ValueError(
                    f"baseline turn {row.get('record_id')} is marked invalid without a failure outcome"
                )
            diagnostic = str(
                row.get("ai_failure_diagnostic") or row.get("ai_failure_reason") or outcome
            )
            results.append(
                {
                    "record_id": row["record_id"],
                    "status": "invalid",
                    "prediction": None,
                    "error_type": outcome,
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
    human_presence = human["hostility_present"]
    model_presence = prediction["hostility_present"]
    human_target, model_target = human.get("target"), prediction.get("target")
    if human_presence == model_presence and (
        human_presence != "YES" or human_target == model_target
    ):
        return "AGREEMENT"
    if model_presence == "YES" and human_presence == "NO":
        return "YES_TO_NO"
    if model_presence == "NO" and human_presence == "YES":
        return "NO_TO_YES"
    if model_presence == human_presence == "YES" and human_target != model_target:
        return "TARGET_ONLY_DISAGREEMENT"
    if human_presence != model_presence and human_target != model_target:
        return "HOSTILITY_AND_TARGET_DISAGREEMENT"
    return "OTHER_VALID_DISAGREEMENT"


def _load_candidate(
    candidate_dir: Path,
    package: Any,
    *,
    model: str,
    provider: str,
    expected_generation: dict[str, Any],
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
        "provider": provider,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "schema_sha256": SCHEMA_SHA256,
        "package_snapshot_sha256": package.manifest["snapshot_sha256"],
    }.items():
        if configuration.get(key) != expected:
            raise ValueError(f"candidate run provenance mismatch: {key}")
    if configuration.get("prediction_contract_sha256") != CONTRACT_SHA256:
        raise ValueError("candidate run provenance mismatch: prediction_contract_sha256")
    if provider == "openai" and configuration.get(
        "provider_prediction_schema_sha256"
    ) != CONTRACT_SHA256:
        raise ValueError("Luna run provider structured-output schema hash mismatch")
    generation = configuration.get("generation")
    if generation != expected_generation:
        raise ValueError(f"candidate run generation configuration mismatch for {model}")
    results = []
    for number, line in enumerate(predictions_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            result = _parse_json(line, f"candidate predictions line {number}")
            if not isinstance(result, dict):
                raise ValueError(f"candidate prediction line {number} is not an object")
            results.append(result)
    return metadata, results


def _luna_quality_gate(
    metrics: dict[str, Any] | None, reliability: dict[str, Any] | None
) -> dict[str, Any] | None:
    if metrics is None or reliability is None:
        return None
    binary = metrics["binary_yes_vs_rest"]
    conditions = {
        "recall_at_least_0_95": binary["recall"] >= 0.95,
        "hostility_f1_at_least_0_93": binary["f1"] >= 0.93,
        "partisan_hostility_f1_at_least_0_92": metrics["partisan_hostility"]["f1"] >= 0.92,
        "false_negatives_at_most_2": binary["false_negative"] <= 2,
        "coverage_at_least_0_98": reliability["coverage"] >= 0.98,
        "structured_output_failures_at_most_2": reliability["invalid_structured_output"] <= 2,
        "no_evidence_resolution_failures": reliability["evidence_resolution_failures"] == 0,
        "no_provider_api_failures": reliability["provider_api_failures"] == 0,
    }
    return {
        "conditions": conditions,
        "passed": all(conditions.values()),
        "decision_basis": "quality and operational reliability only; cost does not affect this gate",
    }


def compare_benchmark(
    audit_csv: Path,
    audit_summary: Path,
    package_path: Path,
    candidate_dir: Path,
    output: Path,
    pricing_path: Path,
    baseline_plan: Path,
    luna_dir: Path | None = None,
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
    plan_entries, plan_sha256, _plan_provenance = _plan_entries(
        baseline_plan, {str(turn["turn_key"]) for turn in sample_doc["turns"]}
    )
    benchmark_provenance = package.manifest.get("benchmark", {})
    expected_plan_provenance = {
        "source_type": "original_campaign_plan",
        "source_baseline_campaign_id": BASELINE_CAMPAIGN_ID,
        "human_reference_run_id": BASELINE_RUN_ID,
        "source_plan_sha256": plan_sha256,
        "selected_record_count": 100,
        "turn_key_set_sha256": canonical_hash(
            sorted(str(turn["turn_key"]) for turn in sample_doc["turns"])
        ),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "canonical_schema_sha256": SCHEMA_SHA256,
        "prediction_contract_sha256": CONTRACT_SHA256,
    }
    for name, expected in expected_plan_provenance.items():
        if benchmark_provenance.get(name) != expected:
            raise ValueError(f"benchmark package original campaign provenance mismatch: {name}")
    rebuilt_records = build_records(sample_doc["turns"], rows, plan_entries)
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
    candidate_meta, candidate = _load_candidate(
        candidate_dir,
        package,
        model=CANDIDATE_MODEL,
        provider="gemini",
        expected_generation={
            "max_output_tokens": 1000,
            "temperature": 0.0,
            "thinking_mode": "explicit_level",
            "thinking_level": "minimal",
        },
    )
    luna_meta: dict[str, Any] | None = None
    luna_results: list[dict[str, Any]] = []
    if luna_dir is not None:
        luna_meta, luna_results = _load_candidate(
            luna_dir,
            package,
            model=LUNA_MODEL,
            provider="openai",
            expected_generation={"max_output_tokens": 1000, "reasoning_effort": "low"},
        )
    baseline_by_id = {str(result["record_id"]): result for result in baseline}
    candidate_by_id = {str(result["record_id"]): result for result in candidate}
    luna_by_id = {str(result["record_id"]): result for result in luna_results}
    for label, results in (("candidate", candidate), ("Luna", luna_results)):
        result_ids = [str(result["record_id"]) for result in results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError(f"{label} run contains duplicate record IDs")
        if not set(result_ids).issubset(expected_ids):
            raise ValueError(f"{label} run contains records outside the frozen benchmark set")
    ordered_ids = [str(record["record_id"]) for record in package.records]
    baseline = [baseline_by_id[key] for key in ordered_ids]
    candidate = [candidate_by_id[key] for key in ordered_ids if key in candidate_by_id]
    baseline_metrics = compute_metrics(package.records, baseline)
    candidate_metrics = compute_metrics(package.records, candidate)
    baseline_metrics["binary_yes_vs_rest"] = _binary_yes_metrics(package.records, baseline)
    candidate_metrics["binary_yes_vs_rest"] = _binary_yes_metrics(package.records, candidate)
    luna_metrics = compute_metrics(package.records, luna_results) if luna_meta else None
    if luna_metrics is not None:
        luna_metrics["binary_yes_vs_rest"] = _binary_yes_metrics(package.records, luna_results)
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
    luna_reliability = _reliability(package.records, luna_results) if luna_meta else None
    categories: dict[str, dict[str, int]] = {}
    transition_rows = []
    for record in package.records:
        record_id = str(record["record_id"])
        candidate_result = candidate_by_id.get(record_id)
        baseline_result = baseline_by_id[record_id]
        transition_models: list[tuple[str, dict[str, Any] | None]] = [
            (BASELINE_MODEL, baseline_result),
            (CANDIDATE_MODEL, candidate_result),
        ]
        if luna_meta:
            transition_models.append((LUNA_MODEL, luna_by_id.get(record_id)))
        for model_name, result in transition_models:
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
                    if baseline_result.get("status") == "success"
                    and baseline_result.get("prediction")
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
                    if candidate_result
                    and candidate_result.get("status") == "success"
                    and candidate_result.get("prediction")
                    else None
                ),
                "candidate_target": (
                    candidate_result.get("prediction", {}).get("target")
                    if candidate_result
                    and candidate_result.get("status") == "success"
                    and candidate_result.get("prediction")
                    else None
                ),
                "candidate_error": candidate_result.get("error_message")
                if candidate_result
                else "missing result",
                "luna_category": _transition(record, luna_by_id.get(record_id))
                if luna_meta
                else "NOT_RUN",
                "luna_hostility": (
                    luna_by_id[record_id].get("prediction", {}).get("hostility_present")
                    if luna_by_id.get(record_id)
                    and luna_by_id[record_id].get("status") == "success"
                    and luna_by_id[record_id].get("prediction")
                    else None
                ),
                "luna_target": (
                    luna_by_id[record_id].get("prediction", {}).get("target")
                    if luna_by_id.get(record_id)
                    and luna_by_id[record_id].get("status") == "success"
                    and luna_by_id[record_id].get("prediction")
                    else None
                ),
                "luna_error": luna_by_id.get(record_id, {}).get("error_message")
                if luna_meta
                else "not run",
                "baseline_error": reference_rows[record_id].get("ai_failure_diagnostic"),
            }
        )

    pricing = load_pricing(pricing_path)
    candidate_cost = aggregate_costs(candidate, "gemini", CANDIDATE_MODEL, pricing)
    candidate_batch_cost = aggregate_costs(
        candidate, "gemini", CANDIDATE_MODEL, pricing, mode="batch"
    )
    luna_standard_cost = (
        aggregate_costs(luna_results, "openai", LUNA_MODEL, pricing)
        if luna_meta
        else None
    )
    luna_batch_cost = (
        aggregate_costs(luna_results, "openai", LUNA_MODEL, pricing, mode="batch")
        if luna_meta
        else None
    )
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
    baseline_standard_estimate = aggregate_costs(
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
        mode="standard",
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
        "baseline_standard_pricing_estimate": baseline_standard_estimate,
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
        "baseline_projected_standard_cost_160k_usd": baseline_standard_estimate[
            "projected_cost_160k_attempts_usd"
        ],
        "baseline_projected_batch_cost_160k_usd": baseline_estimate[
            "projected_cost_160k_attempts_usd"
        ],
        "candidate_model": CANDIDATE_MODEL,
        "candidate_generation": candidate_meta["configuration"]["generation"],
        "candidate_pricing_mode": "standard",
        "candidate_cost_report": candidate_cost,
        "candidate_batch_pricing_estimate": candidate_batch_cost,
        "candidate_cost_per_attempt_usd": candidate_cost["cost_per_attempt_usd"],
        "candidate_cost_per_valid_annotation_usd": candidate_cost["cost_per_valid_annotation_usd"],
        "candidate_projected_cost_160k_attempts_usd": candidate_cost[
            "projected_cost_160k_attempts_usd"
        ],
        "candidate_projected_cost_160k_valid_annotations_usd": candidate_cost[
            "projected_cost_160k_valid_annotations_usd"
        ],
        "candidate_projected_batch_cost_160k_usd": candidate_batch_cost[
            "projected_cost_160k_attempts_usd"
        ],
        "candidate_batch_projected_cost_160k_attempts_usd": candidate_batch_cost[
            "projected_cost_160k_attempts_usd"
        ],
        "luna_model": LUNA_MODEL if luna_meta else None,
        "luna_run_provenance": (
            {
                "requested_model": LUNA_MODEL,
                "returned_models": sorted(
                    {
                        str(result["returned_model"])
                        for result in luna_results
                        if result.get("returned_model")
                    }
                ),
                "generation": luna_meta["configuration"]["generation"],
                "package_snapshot_sha256": luna_meta["configuration"][
                    "package_snapshot_sha256"
                ],
                "provider_prediction_schema_sha256": luna_meta["configuration"].get(
                    "provider_prediction_schema_sha256"
                ),
                "prediction_contract_sha256": luna_meta["configuration"].get(
                    "prediction_contract_sha256"
                ),
            }
            if luna_meta
            else None
        ),
        "luna_standard_api_usage_cost": luna_standard_cost,
        "luna_batch_pricing_estimate": luna_batch_cost,
        "luna_actual_100_cost_usd": (
            luna_standard_cost["estimated_cost_usd"] if luna_standard_cost else None
        ),
        "luna_standard_projected_cost_160k_usd": (
            luna_standard_cost["projected_cost_160k_attempts_usd"]
            if luna_standard_cost
            else None
        ),
        "luna_batch_projected_cost_160k_usd": (
            luna_batch_cost["projected_cost_160k_attempts_usd"] if luna_batch_cost else None
        ),
        "luna_quality_gate": _luna_quality_gate(luna_metrics, luna_reliability),
        "baseline_note": "Imported run total_cost_usd from immutable prediction-run metadata.",
    }
    model_metrics = {
        BASELINE_MODEL: baseline_metrics,
        CANDIDATE_MODEL: candidate_metrics,
    }
    model_reliability = {
        BASELINE_MODEL: baseline_reliability,
        CANDIDATE_MODEL: candidate_reliability,
    }
    if luna_metrics is not None and luna_reliability is not None:
        model_metrics[LUNA_MODEL] = luna_metrics
        model_reliability[LUNA_MODEL] = luna_reliability
    if luna_meta is not None:
        cost_comparison["luna"] = {
            "model": LUNA_MODEL,
            "execution_mode": "standard",
            "generation": luna_meta["configuration"]["generation"],
            "requested_model": LUNA_MODEL,
            "returned_models": cost_comparison["luna_run_provenance"]["returned_models"],
            "actual_100_usage_based_standard_cost_usd": cost_comparison[
                "luna_actual_100_cost_usd"
            ],
            "standard_cost_report": luna_standard_cost,
            "batch_pricing_estimate": luna_batch_cost,
            "standard_projected_160k_cost_usd": cost_comparison[
                "luna_standard_projected_cost_160k_usd"
            ],
            "batch_projected_160k_cost_usd": cost_comparison[
                "luna_batch_projected_cost_160k_usd"
            ],
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
        "metrics": model_metrics,
        "reliability": model_reliability,
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
    model_headers = [
        (BASELINE_MODEL, "Gemini 2.5 Flash"),
        (CANDIDATE_MODEL, "Gemini 3.1 Flash-Lite"),
    ]
    if luna_meta:
        model_headers.append((LUNA_MODEL, "GPT-6 Luna"))
    lines = [
        "# Frozen comparative hostility benchmark",
        "",
        f"Set: `{HELDOUT_SAMPLE_ID}` ({len(package.records)} records; `{HELDOUT_SAMPLE_SHA256}`).",
        f"Reference labels: final human-reviewed annotations from `{BASELINE_RUN_ID}`.",
        f"Prompt: `{PROMPT_VERSION}` / `{PROMPT_SHA256}`; schema: `{SCHEMA_SHA256}`.",
        "",
        "Human YES is positive; NO and UNSURE are negative. Invalid outputs are excluded from label metrics and reported in reliability.",
        "",
        "| Measure | " + " | ".join(label for _key, label in model_headers) + " |",
        "|" + "|".join(["---"] + ["---:"] * len(model_headers)) + "|",
    ]
    for label, metric, path in (
        ("Operational coverage", "reliability", ("coverage",)),
        ("Valid predictions", "reliability", ("valid",)),
        ("False negatives", "metrics", ("binary_yes_vs_rest", "false_negative")),
        ("False positives", "metrics", ("binary_yes_vs_rest", "false_positive")),
        ("Recall", "metrics", ("binary_yes_vs_rest", "recall")),
        ("Precision", "metrics", ("binary_yes_vs_rest", "precision")),
        ("Accuracy", "metrics", ("binary_yes_vs_rest", "accuracy")),
        ("Hostility F1", "metrics", ("binary_yes_vs_rest", "f1")),
        ("Target exact agreement", "metrics", ("target_classification", "exact_target_accuracy")),
        ("Partisan-hostility F1", "metrics", ("partisan_hostility", "f1")),
        ("Evidence failures", "reliability", ("evidence_resolution_failures",)),
    ):
        values = []
        for model_key, _model_label in model_headers:
            value: Any = (
                model_reliability[model_key] if metric == "reliability" else model_metrics[model_key]
            )
            for key in path:
                value = value[key]
            values.append(f"{value:.1%}" if label == "Operational coverage" else f"{value:.3f}" if isinstance(value, float) else str(value))
        lines.append(f"| {label} | " + " | ".join(values) + " |")
    cost_rows = (
        ("Actual 100-record cost", "luna_actual_100_cost_usd"),
        ("Cost per attempted annotation", "luna_cost_per_attempt_usd"),
        ("Cost per valid annotation", "luna_cost_per_valid_annotation_usd"),
        ("Standard projected 160k cost", "luna_standard_projected_cost_160k_usd"),
        ("Batch projected 160k cost", "luna_batch_projected_cost_160k_usd"),
    )
    if luna_meta:
        for label, cost_key in cost_rows:
            values = []
            for model_key, _model_label in model_headers:
                if model_key == BASELINE_MODEL:
                    value = (
                        cost_comparison["baseline_actual_cost_usd"]
                        if label == "Actual 100-record cost"
                        else baseline_standard_estimate["estimated_cost_usd"] / baseline_attempts
                        if label == "Cost per attempted annotation" and baseline_attempts
                        else baseline_standard_estimate["estimated_cost_usd"] / baseline_valid
                        if label == "Cost per valid annotation" and baseline_valid
                        else cost_comparison[
                            "baseline_projected_standard_cost_160k_usd"
                            if label == "Standard projected 160k cost"
                            else "baseline_projected_batch_cost_160k_usd"
                        ]
                    )
                elif model_key == CANDIDATE_MODEL:
                    value = (
                        candidate_cost["estimated_cost_usd"]
                        if label == "Actual 100-record cost"
                        else candidate_cost["cost_per_attempt_usd"]
                        if label == "Cost per attempted annotation"
                        else candidate_cost["cost_per_valid_annotation_usd"]
                        if label == "Cost per valid annotation"
                        else candidate_cost["projected_cost_160k_attempts_usd"]
                        if label == "Standard projected 160k cost"
                        else candidate_batch_cost["projected_cost_160k_attempts_usd"]
                    )
                else:
                    assert luna_standard_cost is not None
                    value = (
                        cost_comparison[cost_key]
                        if cost_key in cost_comparison
                        else luna_standard_cost[
                            "cost_per_attempt_usd"
                            if label == "Cost per attempted annotation"
                            else "cost_per_valid_annotation_usd"
                        ]
                    )
                values.append(f"${value:.2f}" if isinstance(value, int | float) else "—")
            lines.append(f"| {label} | " + " | ".join(values) + " |")
    lines.extend(
        [
            "",
            "## Reliability and transitions",
            "",
            f"- Gemini 2.5 Flash: {baseline_reliability['valid']}/{baseline_reliability['attempted']} valid; {baseline_reliability['terminal_failures']} terminal failures.",
            f"- Gemini 3.1 Flash-Lite: {candidate_reliability['valid']}/{candidate_reliability['attempted']} valid; {candidate_reliability['terminal_failures']} terminal failures.",
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
    for model_key, model_label in model_headers:
        lines.append(
            f"- AI prediction -> human label `YES_TO_NO` (false-positive direction): "
            f"{categories[model_key].get('YES_TO_NO', 0)} for {model_label}; "
            f"AI prediction -> human label `NO_TO_YES` (false-negative direction): "
            f"{categories[model_key].get('NO_TO_YES', 0)}."
        )
    if luna_meta and luna_reliability:
        lines.append(
            f"- GPT-6 Luna: {luna_reliability['valid']}/{luna_reliability['attempted']} valid; "
            f"{luna_reliability['terminal_failures']} terminal failures."
        )
        lines.append(
            f"- GPT-6 Luna requested model `{LUNA_MODEL}`; returned model values: "
            f"{cost_comparison['luna_run_provenance']['returned_models']}."
        )
        lines.append(
            "- GPT-6 Luna generation configuration: "
            f"`{json.dumps(cost_comparison['luna_run_provenance']['generation'], sort_keys=True)}`."
        )
        lines.append(
            "- GPT-6 Luna standard cost is calculated from actual API usage and current centralized "
            "pricing metadata; the Batch value is a hypothetical estimate using Batch rates."
        )
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return comparison


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="build the frozen benchmark evaluator package")
    prepare.add_argument("--audit-csv", type=Path, required=True)
    prepare.add_argument("--audit-summary", type=Path, required=True)
    prepare.add_argument("--baseline-plan", type=Path, required=True)
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
    compare.add_argument("--luna-run", type=Path)
    compare.add_argument("--baseline-plan", type=Path, required=True)
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
                arguments.baseline_plan,
            )
        else:
            result = compare_benchmark(
                arguments.audit_csv,
                arguments.audit_summary,
                arguments.package,
                arguments.candidate_run,
                arguments.output,
                arguments.pricing,
                arguments.baseline_plan,
                arguments.luna_run,
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
