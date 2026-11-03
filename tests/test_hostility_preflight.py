from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Any

import pytest

from hansard_annotator.llm_eval.hostility_preflight import (
    PreflightError,
    fits_enqueued_token_budget,
    reconcile_sets,
)


def test_set_reconciliation_keeps_partial_pilot_membership_exact() -> None:
    result = reconcile_sets(
        ["turn:1", "turn:2", "turn:3", "turn:4"],
        ["turn:1", "turn:3"],
        ["turn:2"],
    )
    assert result["remaining_ids"] == ["turn:4"]
    assert result["completed_count"] == 2
    assert result["terminal_outcome_count"] == 1


def test_set_reconciliation_rejects_conflicting_ai_outcomes() -> None:
    with pytest.raises(PreflightError, match="both imported predictions"):
        reconcile_sets(["turn:1"], ["turn:1"], ["turn:1"])


def test_set_reconciliation_rejects_duplicate_prediction_membership() -> None:
    with pytest.raises(PreflightError, match="duplicate imported"):
        reconcile_sets(["turn:1"], ["turn:1", "turn:1"], [])


def test_set_reconciliation_rejects_duplicate_canonical_ids() -> None:
    with pytest.raises(PreflightError, match="duplicate eligible"):
        reconcile_sets(["turn:1", "turn:1"], [], [])


def test_active_membership_is_not_planned_again() -> None:
    result = reconcile_sets(
        ["turn:1", "turn:2", "turn:3"], ["turn:1"], [], active_ids=["turn:2"]
    )
    assert result["remaining_ids"] == ["turn:3"]


def test_duplicate_active_campaign_membership_is_reported() -> None:
    result = reconcile_sets(
        ["turn:1", "turn:2"], [], [], active_ids=["turn:1", "turn:1"]
    )
    assert result["duplicate_active_ids"] == ["turn:1"]
    assert result["remaining_ids"] == ["turn:2"]


def test_provider_quota_accounts_for_active_and_unknown_jobs() -> None:
    assert fits_enqueued_token_budget(300, 400, 1000)
    assert not fits_enqueued_token_budget(700, 400, 1000)
    assert not fits_enqueued_token_budget(0, 400, 1000, unknown_jobs=1)


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None, row: dict[str, Any] | None = None):
        self.rows = rows or []
        self.row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self.row

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query: str, params: Any = None) -> _Cursor:
        self.statements.append(query.strip())
        if query.startswith("SET TRANSACTION"):
            return _Cursor()
        if "FROM corpora WHERE public_id::text" in query:
            return _Cursor(row={"id": 1, "public_id": "corpus-1", "slug": "house",
                                "name": "House", "current_preprocessing_run_id": 2})
        if "count(*)::bigint AS total FROM speaker_turns" in query:
            return _Cursor(row={"total": 10})
        if "FROM speaker_turns turn" in query and "selection_hash" in query:
            return _Cursor(rows=[])
        if "count(DISTINCT prediction.id)" in query:
            return _Cursor(rows=[])
        if "WITH active_batches" in query:
            return _Cursor(row={"tokens": 0, "unknown_legacy_jobs": 0})
        if "record.speaker_turn_id::text AS turn_id" in query:
            return _Cursor(rows=[])
        if "sum(run.output_tokens)" in query:
            return _Cursor(row={"output_tokens": 100_000, "requested_count": 1000,
                                "input_tokens": 5_000_000, "cached_input_tokens": 0,
                                "cache_storage_cost_usd": 0.0, "actual_pilot_cost_usd": 1.0,
                                "pilot_terminal_invalid_count": 3,
                                "actual_pilot_recovery_cost_usd": 0.25})
        raise AssertionError(f"unexpected query: {query}")


def test_preflight_uses_read_only_queries_and_is_deterministic(tmp_path) -> None:
    from hansard_annotator.llm_eval.hostility_preflight import build_report

    connection = _Connection()
    first = build_report(connection, corpus_public_id="corpus-1", seed="full-v1",
                         google_enqueued_token_limit=2_000_000)
    second = build_report(connection, corpus_public_id="corpus-1", seed="full-v1",
                          google_enqueued_token_limit=2_000_000)
    assert first == second
    assert first["remaining_turn_ids"] == []
    assert first["readiness"]["database_writes"] is False
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in connection.statements
    assert all(statement.startswith(("SELECT", "WITH", "SET TRANSACTION"))
               for statement in connection.statements)
    selection_queries = [
        statement for statement in connection.statements if "selection_hash" in statement
    ]
    assert len(selection_queries) == 2
    assert all(
        'ORDER BY selection_hash,turn.turn_key COLLATE "C"' in statement
        for statement in selection_queries
    )
    hash_payload = {key: value for key, value in first.items() if key != "sha256"}
    expected_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert first["sha256"] == expected_hash
