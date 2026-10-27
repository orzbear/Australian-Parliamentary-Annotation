from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from hansard_annotator.llm_eval.review_audit import (
    ReviewAuditError,
    _load_rows,
    _write_csv,
    analyse_review_rows,
    choose_run,
    full_review_rows,
    render_markdown,
)


def _run(count: int) -> dict[str, Any]:
    return {
        "id": 7,
        "public_id": UUID("00000000-0000-0000-0000-000000000007"),
        "provider": "gemini",
        "configured_model": "gemini-2.5-flash",
        "returned_model": "gemini-2.5-flash",
        "prompt_version": "phase4a-policy-v2",
        "prompt_sha256": "a" * 64,
        "canonical_schema_sha256": "b" * 64,
        "prediction_contract_sha256": "c" * 64,
        "provider_schema_sha256": "d" * 64,
        "generation_config": {"thinking_budget": 0},
        "execution_mode": "batch",
        "provider_job_id": "batches/test",
        "artifact_sha256": "e" * 64,
        "record_set_sha256": "f" * 64,
        "imported_at": datetime(2026, 10, 10, tzinfo=UTC),
        "valid_count": count,
    }


def _row(
    number: int,
    *,
    status: str | None = "accepted",
    ai_non_policy: bool = False,
    ai_primary: str | None = "AU07",
    ai_secondary: list[str] | None = None,
    human_non_policy: bool | None = False,
    human_primary: str | None = "AU07",
    human_secondary: list[str] | None = None,
    flag_notes: list[str] | None = None,
) -> dict[str, Any]:
    human_values = (
        {
            "is_non_policy": human_non_policy,
            "primary_australian_domain": human_primary,
            "secondary_australian_domains": human_secondary or [],
            "annotation_notes": None,
            "fallback_explanation": None,
        }
        if status in {"accepted", "edited"}
        else None
    )
    notes = flag_notes or []
    return {
        "id": number,
        "prediction_id": f"prediction-{number}",
        "run_id": 7,
        "run_public_id": "00000000-0000-0000-0000-000000000007",
        "record_id": f"turn:record-{number}",
        "speaker_turn_id": number,
        "ai_is_non_policy": ai_non_policy,
        "ai_primary_domain": ai_primary,
        "ai_secondary_domains": ai_secondary or [],
        "ai_reasoning": "Stored model reasoning.",
        "turn_key": f"record-{number}",
        "speaker": "Research Speaker",
        "sitting_date": "2026-01-01",
        "chamber": "House",
        "major_heading": "Debate",
        "minor_heading": "Topic",
        "business_type": "debate",
        "speech_text": "Full speech text.",
        "review_status": status,
        "review_note": "review note" if status else None,
        "annotation_version_id": number if status in {"accepted", "edited"} else None,
        "human_values": human_values,
        "human_speaker_turn_id": number if human_values else None,
        "codebook_flag_count": len(notes),
        "codebook_flag_notes": [
            {"note": note, "reviewed_at": "2026-01-02", "reviewer_user_id": 1} for note in notes
        ],
    }


def test_accepted_only_review_is_counted_without_an_edit() -> None:
    summary, edited, flagged = analyse_review_rows(_run(1), [_row(1)])
    assert summary["counts"]["accepted"] == 1
    assert summary["counts"]["edited"] == 0
    assert edited == []
    assert flagged == []


@pytest.mark.parametrize(
    ("row", "category", "changed"),
    [
        (
            _row(1, status="edited", human_primary="AU08"),
            "PRIMARY_DOMAIN_CHANGED",
            ["primary_australian_domain"],
        ),
        (
            _row(1, status="edited", human_non_policy=True, human_primary=None),
            "POLICY_TO_NON_POLICY",
            ["is_non_policy", "primary_australian_domain"],
        ),
        (
            _row(1, status="edited", ai_secondary=["AU02"], human_secondary=["AU03"]),
            "SECONDARY_DOMAINS_CHANGED",
            ["secondary_australian_domains"],
        ),
    ],
)
def test_edit_types_are_deterministic(
    row: dict[str, Any], category: str, changed: list[str]
) -> None:
    summary, edited, _ = analyse_review_rows(_run(1), [row])
    assert edited[0]["edit_category"] == category
    assert edited[0]["changed_fields"] == changed
    assert summary["edit_analysis"]["edit_category_counts"] == {category: 1}


def test_accepted_and_edited_flags_overlap_without_replacing_outcomes() -> None:
    rows = [
        _row(1, flag_notes=["Boundary unclear"]),
        _row(2, status="edited", human_primary="AU08", flag_notes=["Boundary unclear"]),
    ]
    summary, edited, flagged = analyse_review_rows(_run(2), rows)
    assert summary["counts"] == {
        "total_predictions": 2,
        "reviewed_predictions": 2,
        "unreviewed_predictions": 0,
        "accepted": 1,
        "edited": 1,
        "skipped": 0,
        "codebook_flagged": 2,
        "accepted_and_flagged": 1,
        "edited_and_flagged": 1,
        "skipped_and_flagged": 0,
        "accepted_not_flagged": 0,
        "edited_not_flagged": 0,
    }
    assert len(edited) == 1
    assert len(flagged) == 2
    assert summary["flag_analysis"]["unique_flag_notes"] == {"Boundary unclear": 2}


def test_terminal_hostility_failure_stays_distinct_in_review_audit() -> None:
    row = _row(1, status=None)
    row.update(
        prediction_task="hostility",
        ai_valid_prediction=False,
        ai_outcome_status="INVALID_OUTPUT",
        ai_failure_reason="EVIDENCE_RESOLUTION_FAILED",
        ai_prediction_values=None,
        ai_is_non_policy=None,
        ai_primary_domain=None,
        ai_secondary_domains=None,
        ai_reasoning=None,
        review_status="human_annotated_after_ai_failure",
        annotation_version_id=123,
        human_speaker_turn_id=1,
        human_values={"hostility_present": "YES", "target": "PARTY_OR_COALITION", "evidence_span": []},
    )
    run = _run(0)
    run["invalid_count"] = 1
    run["prediction_task"] = "hostility"
    summary, edited, flagged = analyse_review_rows(run, [row])
    exported = full_review_rows([row])[0]
    assert not edited and not flagged
    assert summary["counts"]["terminal_ai_failures"] == 1
    assert summary["counts"]["coverage"] == 0
    assert exported["ai_valid_prediction"] is False
    assert exported["ai_outcome_status"] == "INVALID_OUTPUT"
    assert exported["ai_prediction_values"] is None
    assert exported["ai_hostility_present"] is None
    assert exported["reviewed_hostility"] == "YES"
    assert exported["terminal_review_action"] == "human_annotated_after_ai_failure"


def test_flag_without_terminal_decision_is_still_exported() -> None:
    row = _row(1, status=None, flag_notes=["Needs codebook clarification"])
    summary, edited, flagged = analyse_review_rows(_run(1), [row])
    assert summary["counts"]["unreviewed_predictions"] == 1
    assert summary["counts"]["reviewed_predictions"] == 0
    assert summary["counts"]["codebook_flagged"] == 1
    assert edited == []
    assert flagged[0]["human_label"] == ""
    assert summary["flag_analysis"]["by_final_human_primary_label"] == {
        "No terminal annotation": 1
    }


def test_full_review_results_preserve_ai_human_and_review_provenance() -> None:
    row = _row(1, status="edited", human_primary="AU08", flag_notes=["Boundary unclear"])
    row.update(
        terminal_reviewer_user_id=42,
        terminal_reviewer="Researcher",
        terminal_reviewed_at="2026-10-12T10:00:00Z",
    )
    result = full_review_rows([row])[0]
    assert result["ai_primary_domain"] == "AU07"
    assert result["human_primary_domain"] == "AU08"
    assert result["terminal_review_action"] == "edited"
    assert result["reviewer_user_id"] == 42
    assert result["annotation_version_id"] == 1
    assert result["codebook_flagged"] is True
    assert result["codebook_flag_note"] == "Boundary unclear"


def test_missing_or_mismatched_human_annotation_fails_loudly() -> None:
    missing = _row(1, status="edited", human_primary="AU08")
    missing["human_values"] = None
    with pytest.raises(ReviewAuditError, match="no resulting human annotation"):
        analyse_review_rows(_run(1), [missing])
    mismatched = _row(1)
    mismatched["human_speaker_turn_id"] = 99
    with pytest.raises(ReviewAuditError, match="another turn"):
        analyse_review_rows(_run(1), [mismatched])


def test_ambiguous_or_implicit_run_selection_fails() -> None:
    first = _run(1)
    second = {**_run(1), "id": 8, "public_id": UUID(int=8)}
    second["imported_at"] = first["imported_at"] + timedelta(seconds=1)
    with pytest.raises(ReviewAuditError, match="exactly one"):
        choose_run([first, second], run_id=None, latest=False)
    assert choose_run([first, second], run_id=None, latest=True)["id"] == 8
    assert choose_run([first, second], run_id="7", latest=False)["id"] == 7


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _RecordingConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.parameters: tuple[object, ...] = ()

    def execute(self, query: str, parameters: tuple[object, ...]) -> _Result:
        self.queries.append(query)
        self.parameters = parameters
        return _Result([])


def test_latest_effective_terminal_review_is_selected_without_duplicate_rows() -> None:
    connection = _RecordingConnection()
    assert _load_rows(connection, 7) == []  # type: ignore[arg-type]
    assert any("review.review_status IN ('accepted','edited','skipped')" in query for query in connection.queries)
    assert any("ORDER BY reviewed_at DESC,id DESC LIMIT 1" in query for query in connection.queries)
    assert any("resolution_type='INVALID_OUTPUT'" in query for query in connection.queries)
    assert connection.parameters == (7,)


def test_csv_json_and_markdown_outputs_are_deterministic(tmp_path: Path) -> None:
    rows = [_row(2, status="edited", human_primary="AU08"), _row(1)]
    summary, edited, _ = analyse_review_rows(_run(2), rows)
    reversed_summary, reversed_edited, _ = analyse_review_rows(_run(2), list(reversed(rows)))
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    columns = ["record_id", "changed_fields", "edit_category"]
    _write_csv(first, edited, columns)
    _write_csv(second, reversed_edited, columns)
    assert first.read_bytes() == second.read_bytes()
    assert json.dumps(summary, sort_keys=True, default=str) == json.dumps(
        reversed_summary, sort_keys=True, default=str
    )
    markdown = render_markdown(summary)
    assert "Accepted rate" in markdown
    assert "does not modify V2" in markdown


def test_audit_module_declares_database_transaction_read_only() -> None:
    source = (
        Path(__file__).parents[1] / "src/hansard_annotator/llm_eval/review_audit.py"
    ).read_text(encoding="utf-8")
    assert 'connection.execute("SET TRANSACTION READ ONLY")' in source
    assert "INSERT INTO" not in source
    assert "UPDATE model_" not in source
    assert "DELETE FROM" not in source
