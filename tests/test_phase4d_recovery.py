from __future__ import annotations

import json
from pathlib import Path

import pytest

from hansard_annotator.llm_eval.import_predictions import load_prediction_artifact
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_MODEL,
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
    canonical_hash,
)
from hansard_annotator.llm_eval.validation import InvalidPrediction, validate_prediction
from hansard_annotator.web.campaigns.recovery import (
    STRUCTURAL_REPAIR_RULE,
    apply_structural_repairs,
    build_effective_artifact,
    build_structurally_repaired_artifact,
    effective_results,
    structural_repair_directory,
    unresolved_results,
)

DOMAIN_CODES = (*tuple(f"AU{number:02d}" for number in range(1, 15)), "AU_OTHER_REVIEW")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _provider_response(prediction: dict[str, object]) -> dict[str, object]:
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps(prediction)}]}}
        ]
    }


def _repairable_prediction() -> dict[str, object]:
    return {
        "is_non_policy": False,
        "primary_australian_domain": "AU07",
        "secondary_australian_domains": ["AU08", "AU07"],
        "reasoning": "The policy concerns AU07 with a secondary AU08 dimension.",
    }


def _original_artifact(path: Path, *, records: int = 1_000) -> bytes:
    path.mkdir()
    rows: list[dict[str, object]] = [
        {
            "record_id": f"turn:{index}",
            "request_index": index,
            "status": "success",
            "prediction": {
                "is_non_policy": True,
                "primary_australian_domain": None,
                "secondary_australian_domains": [],
                "reasoning": "Non-policy.",
            },
        }
        for index in range(records - 1)
    ]
    rows.append(
        {
            "record_id": f"turn:{records - 1}",
            "request_index": records - 1,
            "status": "invalid",
            "prediction": None,
            "validation_error": "primary domain cannot also be secondary",
            "provider_response": _provider_response(_repairable_prediction()),
        }
    )
    _write_jsonl(path / "predictions.jsonl", rows)
    summary = {
        "provenance": {
            "provider": "gemini",
            "model": APPROVED_PRODUCTION_MODEL,
            "prompt_version": FROZEN_PROMPT_VERSION,
            "assembled_prompt_sha256": FROZEN_PROMPT_SHA256,
            "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
            "execution_mode": "batch",
            "thinking_level": "minimal",
            "batch_id": "batches/original",
            "record_ids_sha256": canonical_hash(
                [f"turn:{index}" for index in range(records)]
            ),
        },
        "coverage": {
            "planned_requests": records,
            "provider_request_count": records,
            "collected_responses": records,
            "missing_responses": 0,
            "duplicate_mappings": 0,
            "successful_provider_responses": records,
            "failed_provider_responses": 0,
            "valid_structured_predictions": records - 1,
            "invalid_structured_predictions": 1,
            "provider_failures_collected": 0,
            "accounting_invariant_ok": True,
        },
        "tokens": {
            "input_tokens": 10_000,
            "cached_input_tokens": 8_000,
            "output_tokens": 1_000,
            "reasoning_tokens": 0,
            "total_tokens": 11_000,
        },
        "cost": {
            "batch_uncached_input_cost_usd": 0.1,
            "cached_input_cost_usd": 0.2,
            "output_including_reasoning_cost_usd": 0.7,
            "token_processing_cost_usd": 1.0,
            "explicit_cache_storage_cost_usd": 0.0,
            "total_pilot_cost_usd": 1.0,
        },
    }
    (path / "operational_summary.json").write_text(
        json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
    )
    return (path / "predictions.jsonl").read_bytes()


def _recovery(
    path: Path, *, attempt: int, status: str
) -> dict[str, object]:
    path.mkdir()
    row: dict[str, object] = {
        "record_id": "turn:999",
        "request_index": 0,
        "status": status,
        "prediction": (
            {
                "is_non_policy": False,
                "primary_australian_domain": "AU07",
                "secondary_australian_domains": ["AU08"],
                "reasoning": "Valid recovery.",
            }
            if status == "success"
            else None
        ),
        "validation_error": None if status == "success" else "still invalid",
        "provider_response": (
            None if status == "success" else _provider_response(_repairable_prediction())
        ),
    }
    _write_jsonl(path / "predictions.jsonl", [row])
    return {
        "attempt_number": attempt,
        "provider_job_id": f"batches/recovery-{attempt}",
        "artifact_reference": str(path),
        "artifact_sha256": str(attempt) * 64,
        "valid_count": int(status == "success"),
        "invalid_count": int(status != "success"),
        "operational_metadata": {
            "tokens": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "reasoning_tokens": 2,
                "total_tokens": 112,
            },
            "cost": {
                "batch_uncached_input_cost_usd": 0.01,
                "cached_input_cost_usd": 0.02,
                "output_including_reasoning_cost_usd": 0.07,
                "token_processing_cost_usd": 0.1,
                "explicit_cache_storage_cost_usd": 0.0,
                "total_pilot_cost_usd": 0.1,
            },
        },
    }


def test_999_valid_one_invalid_targets_only_invalid_and_never_valid(tmp_path: Path) -> None:
    original = tmp_path / "original"
    _original_artifact(original)
    retry = unresolved_results(original, [])
    assert [row["record_id"] for row in retry] == ["turn:999"]
    assert all(row["record_id"] != "turn:0" for row in retry)


def test_successful_recovery_resolves_effective_set_without_rewriting_original(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    original_bytes = _original_artifact(original)
    recovery = _recovery(tmp_path / "attempt-001", attempt=1, status="success")
    summary, rows, artifact_hash = build_effective_artifact(
        original, [recovery], tmp_path / "effective"
    )
    assert len(rows) == 1_000
    assert summary["coverage"]["valid_structured_predictions"] == 1_000
    assert summary["coverage"]["invalid_structured_predictions"] == 0
    assert summary["cost"]["total_pilot_cost_usd"] == pytest.approx(1.1)
    assert summary["tokens"]["input_tokens"] == 10_100
    assert len(artifact_hash) == 64
    loaded_summary, loaded_rows, loaded_hash = load_prediction_artifact(
        tmp_path / "effective"
    )
    assert loaded_summary["coverage"]["invalid_structured_predictions"] == 0
    assert len(loaded_rows) == 1_000
    assert loaded_hash == artifact_hash
    assert (original / "predictions.jsonl").read_bytes() == original_bytes


def test_invalid_recovery_remains_retryable_and_later_success_wins_deterministically(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    _original_artifact(original)
    first = _recovery(tmp_path / "attempt-001", attempt=1, status="invalid")
    assert [row["record_id"] for row in unresolved_results(original, [first])] == [
        "turn:999"
    ]
    second = _recovery(tmp_path / "attempt-002", attempt=2, status="success")
    forward = effective_results(original, [first, second])
    reverse = effective_results(original, [second, first])
    assert forward == reverse
    assert forward[-1]["status"] == "success"
    assert forward[-1]["request_index"] == 999
    assert unresolved_results(original, [first, second]) == []


def test_local_validation_still_rejects_primary_as_secondary() -> None:
    with pytest.raises(InvalidPrediction, match="primary domain cannot also be secondary"):
        validate_prediction(
            {
                "is_non_policy": False,
                "primary_australian_domain": "AU07",
                "secondary_australian_domains": ["AU08", "AU07"],
                "reasoning": "Synthetic production regression.",
            },
            DOMAIN_CODES,
        )


def test_append_only_structural_repair_resolves_current_case_and_allows_import(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    _original_artifact(original)
    recovery_directory = tmp_path / "attempt-001"
    recovery = _recovery(recovery_directory, attempt=1, status="invalid")
    original_before = {
        path.name: path.read_bytes() for path in original.iterdir() if path.is_file()
    }
    recovery_before = {
        path.name: path.read_bytes()
        for path in recovery_directory.iterdir()
        if path.is_file()
    }
    original_entries_before = sorted(
        path.relative_to(original).as_posix() for path in original.rglob("*")
    )
    recovery_entries_before = sorted(
        path.relative_to(recovery_directory).as_posix()
        for path in recovery_directory.rglob("*")
    )
    effective = tmp_path / "effective"
    build_effective_artifact(original, [recovery], effective)
    repaired_directory = structural_repair_directory(effective)
    summary, rows, artifact_hash = build_structurally_repaired_artifact(
        effective, repaired_directory, DOMAIN_CODES
    )

    repaired = rows[-1]
    assert repaired["status"] == "success"
    assert repaired["prediction"] == {
        "is_non_policy": False,
        "primary_australian_domain": "AU07",
        "secondary_australian_domains": ["AU08"],
        "reasoning": "The policy concerns AU07 with a secondary AU08 dimension.",
    }
    assert summary["coverage"]["valid_structured_predictions"] == 1_000
    assert summary["coverage"]["invalid_structured_predictions"] == 0
    assert summary["structural_repair"]["rule_id"] == STRUCTURAL_REPAIR_RULE
    assert summary["structural_repair"]["repaired_count"] == 1
    lineage = json.loads(
        (repaired_directory / "structural_repairs.jsonl").read_text(encoding="utf-8")
    )
    assert lineage["before_prediction"] == _repairable_prediction()
    assert lineage["after_prediction"] == repaired["prediction"]
    assert lineage["rule_version"] == 1

    loaded_summary, loaded_rows, loaded_hash = load_prediction_artifact(
        repaired_directory
    )
    assert len(loaded_rows) == 1_000
    assert loaded_summary["coverage"]["invalid_structured_predictions"] == 0
    assert loaded_hash == artifact_hash
    repeated_directory = tmp_path / "repeated-repair"
    _repeated_summary, _repeated_rows, repeated_hash = (
        build_structurally_repaired_artifact(
            effective, repeated_directory, DOMAIN_CODES
        )
    )
    assert repeated_hash == artifact_hash
    assert (repeated_directory / "structural_repairs.jsonl").read_bytes() == (
        repaired_directory / "structural_repairs.jsonl"
    ).read_bytes()
    assert (repeated_directory / "predictions.jsonl").read_bytes() == (
        repaired_directory / "predictions.jsonl"
    ).read_bytes()
    assert {
        path.name: path.read_bytes() for path in original.iterdir() if path.is_file()
    } == original_before
    assert sorted(
        path.relative_to(original).as_posix() for path in original.rglob("*")
    ) == original_entries_before
    assert {
        path.name: path.read_bytes()
        for path in recovery_directory.iterdir()
        if path.is_file()
    } == recovery_before
    assert sorted(
        path.relative_to(recovery_directory).as_posix()
        for path in recovery_directory.rglob("*")
    ) == recovery_entries_before


def test_structural_repair_does_not_broaden_permitted_changes() -> None:
    raw = _repairable_prediction()
    raw["reasoning"] = "x" * 1_001
    row = {
        "record_id": "turn:1",
        "request_index": 0,
        "status": "invalid",
        "prediction": None,
        "validation_error": "reasoning must be a short string",
        "provider_response": _provider_response(raw),
    }
    before = json.loads(json.dumps(row))
    rows, lineage = apply_structural_repairs([row], DOMAIN_CODES)
    assert rows == [before]
    assert lineage == []


def test_recovery_migration_enforces_active_set_idempotency_and_lineage() -> None:
    migration = Path(
        "migrations/versions/20261017_08_campaign_recovery.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "20261013_07"' in migration
    assert "CREATE UNIQUE INDEX uq_ai_campaign_recovery_active_set" in migration
    assert "ai_annotation_campaign_recovery_records" in migration
    assert "original_request_index" in migration
    assert "original_validation_error" in migration
    assert "campaign recovery provider job identity is immutable" in migration
    assert "downgrade refused" in migration


def test_recovery_ui_resets_progress_state_and_labels_paid_action() -> None:
    detail = Path(
        "src/hansard_annotator/web/templates/campaigns/detail.html"
    ).read_text(encoding="utf-8")
    javascript = Path("src/hansard_annotator/web/static/src/app.js").read_text(
        encoding="utf-8"
    )
    assert "Retry {{ campaign.invalid_count }} invalid record" in detail
    assert "paid provider action" in detail
    assert "RETRY INVALID RECORDS" in detail
    assert "remove_primary_from_secondary_v1" in detail
    assert "Apply deterministic structural repair" in detail
    assert "/repair-structural" in detail
    assert 'window.addEventListener("pageshow", resetProgressForms)' in javascript
    assert "delete progressForm.dataset.submitting" in javascript
    assert "button.disabled = false" in javascript


def test_recovery_submission_can_find_existing_provider_job_before_submit() -> None:
    service = Path("src/hansard_annotator/web/campaigns/service.py").read_text(
        encoding="utf-8"
    )
    find_position = service.index("finder(display_name")
    submit_position = service.index("provider.submit(", find_position)
    assert find_position < submit_position
    assert "an unresolved-record recovery job is already active" in service
