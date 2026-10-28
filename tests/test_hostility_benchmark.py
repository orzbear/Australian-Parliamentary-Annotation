from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from hansard_annotator.llm_eval.cli import main as evaluate_main
from hansard_annotator.llm_eval.hostility import write_reports
from hansard_annotator.llm_eval.hostility_benchmark import (
    BASELINE_RUN_ID,
    CANDIDATE_MODEL,
    SCHEMA_SHA256,
    compare_benchmark,
    prepare_package,
)
from hansard_annotator.llm_eval.hostility_validation import (
    CONTRACT_SHA256,
    HELDOUT_SAMPLE_SHA256,
    MODEL,
    PROMPT_SHA256,
    PROMPT_VERSION,
)
from hansard_annotator.llm_eval.operations import canonical_hash, production_record_from_turn

ROOT = Path(__file__).resolve().parents[1]


def _evidence(text: str) -> list[dict[str, Any]]:
    start = text.index("attacked")
    return [{"start_offset": start, "end_offset": start + len("attacked"), "text": "attacked"}]


def _audit_data() -> tuple[list[str], list[dict[str, Any]]]:
    turns_doc = json.loads(
        (ROOT / "config/research_samples/hostility-heldout-100.json").read_text(encoding="utf-8")
    )
    rows: list[dict[str, Any]] = []
    for index, frozen in enumerate(turns_doc["turns"]):
        text = f"Speech {index} attacked the opposing side."
        frozen_record = production_record_from_turn(
            {
                "turn_key": frozen["turn_key"],
                "date": "2026-01-01",
                "chamber": "House",
                "speaker_name_raw": "Speaker",
                "major_heading_original": "Debate",
                "minor_heading_original": None,
                "business_type": None,
                "text_clean": text,
            }
        )
        presence = "NO" if index % 2 == 0 else "YES"
        values = {
            "hostility_present": presence,
            "target": None if presence == "NO" else "OPPOSING_PARTISAN_TARGET",
            "evidence_span": [] if presence == "NO" else _evidence(text),
        }
        prediction = {
            "hostility_present": presence,
            "target": "N/A" if presence == "NO" else "OPPOSING_PARTISAN_TARGET",
            "evidence_spans": [] if presence == "NO" else [{"text": "attacked"}],
            "reasoning": "synthetic fixture",
        }
        rows.append(
            {
                "run_id": BASELINE_RUN_ID,
                "record_id": f"turn:{frozen['turn_key']}",
                "turn_key": frozen["turn_key"],
                "canonical_record_sha256": canonical_hash(frozen_record),
                "prediction_task": "hostility",
                "terminal_review_action": "accepted",
                "ai_valid_prediction": "true",
                "human_values": json.dumps(values),
                "ai_prediction_values": json.dumps(prediction),
                "ai_failure_diagnostic": "",
                "ai_failure_reason": "",
                "speech_text": text,
                "speaker": "Speaker",
                "sitting_date": "2026-01-01",
                "chamber": "House",
                "major_heading": "Debate",
                "minor_heading": "",
                "business_type": "",
            }
        )
    return [str(row["turn_key"]) for row in turns_doc["turns"]], rows


def _write_audit(tmp_path: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    _, rows = _audit_data()
    csv_path = tmp_path / "full_review_results.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "run": {
            "public_id": BASELINE_RUN_ID,
            "prediction_task": "hostility",
            "annotation_schema_slug": "australian_partisan_hostility",
            "annotation_schema_version": "0.2.0",
            "provider": "gemini",
            "configured_model": MODEL,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": PROMPT_SHA256,
            "canonical_schema_sha256": SCHEMA_SHA256,
            "prediction_contract_sha256": CONTRACT_SHA256,
            "record_set_sha256": HELDOUT_SAMPLE_SHA256,
            "generation_config": {"thinking_budget": 0},
            "execution_mode": "batch",
            "requested_count": 100,
            "valid_count": 100,
            "input_tokens": 20000,
            "cached_input_tokens": 10000,
            "output_tokens": 5000,
            "reasoning_tokens": 0,
            "total_cost_usd": 0.012,
        }
    }
    summary_path = tmp_path / "review_summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    return csv_path, summary_path, rows


def test_prepare_reuses_exact_frozen_sample_and_reviewed_reference(
    tmp_path: Path, capsys: Any
) -> None:
    audit_csv, audit_summary, _ = _write_audit(tmp_path)
    package_path = tmp_path / "benchmark-input"
    result = prepare_package(
        audit_csv,
        audit_summary,
        ROOT / "config/research_samples/hostility-heldout-100.json",
        ROOT / "config/llm_eval/hostility-v2-codebook-context.json",
        package_path,
    )
    manifest = json.loads((package_path / "manifest.json").read_text(encoding="utf-8"))
    annotations = (package_path / "annotations.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(annotations[0])

    assert result["records"] == 100
    assert result["set_sha256"] == HELDOUT_SAMPLE_SHA256
    assert manifest["benchmark"]["human_reference_run_id"] == BASELINE_RUN_ID
    assert len(annotations) == 100
    assert (
        first["speech"]["turn_key"]
        == json.loads(
            (ROOT / "config/research_samples/hostility-heldout-100.json").read_text(
                encoding="utf-8"
            )
        )["turns"][0]["turn_key"]
    )
    assert first["human_annotation"]["values"]["hostility_present"] == "NO"
    assert (
        evaluate_main(
            [
                "--input",
                str(package_path),
                "--provider",
                "gemini",
                "--model",
                CANDIDATE_MODEL,
                "--prompt-version",
                PROMPT_VERSION,
                "--temperature",
                "0",
                "--max-output-tokens",
                "1000",
                "--thinking-level",
                "minimal",
                "--output",
                str(tmp_path / "never-launched"),
                "--dry-run",
            ]
        )
        == 0
    )
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["records"] == 100
    assert dry_run["model"] == CANDIDATE_MODEL
    assert dry_run["prompt_sha256"] == PROMPT_SHA256
    assert dry_run["generation"]["thinking_level"] == "minimal"


def test_prepare_rejects_text_that_differs_from_immutable_campaign_input(
    tmp_path: Path,
) -> None:
    audit_csv, audit_summary, rows = _write_audit(tmp_path)
    rows[0]["speech_text"] += " changed"
    with audit_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValueError, match="canonical text/metadata differs"):
        prepare_package(
            audit_csv,
            audit_summary,
            ROOT / "config/research_samples/hostility-heldout-100.json",
            ROOT / "config/llm_eval/hostility-v2-codebook-context.json",
            tmp_path / "invalid-input",
        )


def test_compare_reports_metrics_transitions_and_actual_baseline_cost(tmp_path: Path) -> None:
    audit_csv, audit_summary, rows = _write_audit(tmp_path)
    package_path = tmp_path / "benchmark-input"
    prepare_package(
        audit_csv,
        audit_summary,
        ROOT / "config/research_samples/hostility-heldout-100.json",
        ROOT / "config/llm_eval/hostility-v2-codebook-context.json",
        package_path,
    )
    package_manifest = json.loads((package_path / "manifest.json").read_text(encoding="utf-8"))
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    configuration = {
        "provider": "gemini",
        "model": CANDIDATE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "schema_sha256": SCHEMA_SHA256,
        "package_snapshot_sha256": package_manifest["snapshot_sha256"],
        "generation": {
            "max_output_tokens": 1000,
            "temperature": 0.0,
            "thinking_mode": "explicit_level",
            "thinking_level": "minimal",
        },
    }
    (candidate_dir / "run_metadata.json").write_text(
        json.dumps({"configuration": configuration}), encoding="utf-8"
    )
    candidate_results = []
    for index, row in enumerate(rows):
        prediction = json.loads(row["ai_prediction_values"])
        if index == 1:
            prediction = {
                **prediction,
                "hostility_present": "NO",
                "target": "N/A",
                "evidence_spans": [],
            }
        result: dict[str, Any] = {
            "record_id": row["record_id"],
            "status": "success",
            "prediction": prediction,
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 50,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "total_tokens": 120,
            },
            "diagnostics": {"evidence_valid": True},
        }
        if index == 3:
            result.update(
                {
                    "status": "invalid",
                    "prediction": None,
                    "error_type": "InvalidPrediction",
                    "error_message": "evidence text was unmatched",
                    "diagnostics": {"evidence_unmatched": ["not in speech"]},
                }
            )
        if index == 5:
            result.update(
                {
                    "status": "api_failure",
                    "prediction": None,
                    "error_type": "ProviderError",
                    "error_message": "provider request failed",
                    "diagnostics": {},
                }
            )
        candidate_results.append(result)
    (candidate_dir / "predictions.jsonl").write_text(
        "".join(json.dumps(result) + "\n" for result in candidate_results), encoding="utf-8"
    )

    output = tmp_path / "comparison"
    report = compare_benchmark(
        audit_csv,
        audit_summary,
        package_path,
        candidate_dir,
        output,
        ROOT / "config/llm_eval/pricing.yaml",
    )
    candidate_metrics = report["metrics"][CANDIDATE_MODEL]
    assert candidate_metrics["binary_yes_vs_rest"]["false_negative"] == 1
    assert report["ai_to_human_transitions"][CANDIDATE_MODEL]["YES_TO_NO"] == 1
    assert report["ai_to_human_transitions"][CANDIDATE_MODEL]["EVIDENCE_RESOLUTION_FAILURE"] == 1
    assert report["ai_to_human_transitions"][CANDIDATE_MODEL]["PROVIDER_API_FAILURE"] == 1
    assert report["reliability"][CANDIDATE_MODEL]["evidence_resolution_failures"] == 1
    assert report["reliability"][CANDIDATE_MODEL]["provider_api_failures"] == 1
    assert report["reliability"][CANDIDATE_MODEL]["terminal_failures"] == 2
    assert report["cost"]["baseline_actual_cost_usd"] == 0.012
    assert report["cost"]["baseline_pricing_estimate"]["estimated_cost_usd"] == 0.00805
    assert report["cost"]["baseline_cost_basis_source"] == "imported_run_total_cost_usd"
    assert report["cost"]["candidate_generation"]["thinking_level"] == "minimal"
    assert report["recommendation"] is None
    assert (output / "comparison.md").is_file()
    assert (output / "record_transitions.csv").is_file()


def test_comparative_prediction_report_uses_correct_benchmark_label(tmp_path: Path) -> None:
    write_reports(
        tmp_path,
        {
            "evaluated_records": 1,
            "total_records": 1,
            "hostility_identification": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
            "target_classification": {"human_yes_support": 1, "exact_target_accuracy": 1.0},
            "partisan_hostility": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
            "evidence_quality": {
                "classification_valid": 1,
                "valid": 1,
                "unmatched": 0,
                "ambiguous": 0,
            },
        },
        {},
        [],
        comparative_benchmark=True,
    )
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Frozen comparative hostility candidate report" in report
    assert "not a new untouched held-out set" in report
