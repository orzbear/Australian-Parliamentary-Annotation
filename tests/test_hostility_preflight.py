from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from typing import Any

import psutil
import pytest

from hansard_annotator.llm_eval.hostility_preflight import (
    PreflightError,
    _input_tokens_from_utf8_bytes,
    _report_sha256,
    fits_enqueued_token_budget,
    reconcile_sets,
)
from hansard_annotator.web.campaigns.phase4e import (
    CHARACTERS_PER_TOKEN,
    INVARIANT_INPUT_TOKENS,
    deterministic_chunks,
    estimate_record_input_tokens,
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


class _ServerCursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.result: _Cursor | None = None
        self.itersize = 0

    def execute(self, query: str, params: Any = None) -> _ServerCursor:
        self.result = self.connection.execute(query, params)
        return self

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        assert self.result is not None
        rows = self.result.rows[:size]
        self.result.rows = self.result.rows[size:]
        return rows

    def close(self) -> None:
        return None


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
            return _Cursor(row={
                "completed_ids": [], "terminal_ids": [], "conflict_count": 0,
                "conflict_ids": [], "terminal_invalid": 0, "provider_refusals": 0,
                "manual_reviews": 0,
            })
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

    def cursor(self, *, name: str) -> _ServerCursor:
        assert name.startswith("hostility_preflight_")
        return _ServerCursor(self)


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
    assert all("canonical_text_utf8_bytes" in statement for statement in selection_queries)
    assert all("octet_length(convert_to(turn.text_clean,'UTF8'))" in statement
               for statement in selection_queries)
    assert all("turn.text_clean,turn.text_raw" not in statement
               for statement in selection_queries)
    hash_payload = {key: value for key, value in first.items() if key != "sha256"}
    expected_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert first["sha256"] == expected_hash
    assert _report_sha256(hash_payload) == expected_hash


@pytest.mark.parametrize("text", ["a", "abc", "é", "kanji 漢字", "", "😀" * 100])
def test_utf8_byte_token_estimate_matches_existing_text_estimator(text: str) -> None:
    assert _input_tokens_from_utf8_bytes(len(text.encode("utf-8"))) == (
        estimate_record_input_tokens(text)
    )


def test_byte_metadata_planning_matches_original_text_based_chunks() -> None:
    texts = ["first speech", "", "é漢字😀", "x" * 20_000, "short"]
    original = [
        {"selection_order": index, "record_id": f"turn:{index}",
         "estimated_input_tokens": estimate_record_input_tokens(text)}
        for index, text in enumerate(texts)
    ]
    byte_based = [
        {"selection_order": index, "record_id": f"turn:{index}",
         "estimated_input_tokens": _input_tokens_from_utf8_bytes(len(text.encode("utf-8")))}
        for index, text in enumerate(texts)
    ]
    original_chunks = list(
        deterministic_chunks(original, max_records=2, max_input_tokens=20_000)
    )
    byte_chunks = list(
        deterministic_chunks(byte_based, max_records=2, max_input_tokens=20_000)
    )
    assert byte_chunks == original_chunks


def test_full_corpus_chunk_planning_has_bounded_rss_and_exact_accounting() -> None:
    record_count = 160_000
    process = psutil.Process(os.getpid())
    baseline_rss = process.memory_info().rss
    peak_rss = baseline_rss
    estimated_total = 0
    source_row_bytes = 0
    started = time.perf_counter()
    turn_ids: list[str] = []
    turn_keys: list[str] = []
    record_ids: list[str] = []

    def records():
        nonlocal peak_rss, estimated_total, source_row_bytes
        for index in range(record_count):
            text_bytes = 80 + (index * 7919 % 24_000)
            estimated = INVARIANT_INPUT_TOKENS + max(
                1, (text_bytes + CHARACTERS_PER_TOKEN - 1) // CHARACTERS_PER_TOKEN
            )
            estimated_total += estimated
            record_id = f"turn:{index:06d}"
            turn_id = f"{index:064x}"
            turn_key = f"synthetic-turn-{index:06d}"
            turn_ids.append(turn_id)
            turn_keys.append(turn_key)
            record_ids.append(record_id)
            source_row_bytes += len(turn_id) + len(turn_key) + text_bytes
            if index % 2_000 == 0:
                peak_rss = max(peak_rss, process.memory_info().rss)
            yield {
                "selection_order": index,
                "record_id": record_id,
                "estimated_input_tokens": estimated,
            }

    chunks = list(deterministic_chunks(records(), max_records=1_000, max_input_tokens=10_000_000))
    elapsed_seconds = time.perf_counter() - started
    peak_rss = max(peak_rss, process.memory_info().rss)
    assert len(set(turn_ids)) == record_count
    report = {
        "remaining_turn_ids": turn_ids,
        "remaining_turn_keys": turn_keys,
        "remaining_record_ids": record_ids,
        "chunks": [
            {"number": chunk.chunk_number, "records": chunk.record_count,
             "input_tokens": chunk.estimated_input_tokens,
             "record_set_sha256": chunk.record_set_sha256}
            for chunk in chunks
        ],
    }
    report_size_bytes = sum(
        len(segment.encode("utf-8"))
        for segment in json.JSONEncoder(
            sort_keys=True, ensure_ascii=False, indent=2
        ).iterencode(report)
    )
    peak_rss = max(peak_rss, process.memory_info().rss)

    assert sum(chunk.record_count for chunk in chunks) == record_count
    assert len(chunks) == 160
    assert sum(chunk.estimated_input_tokens for chunk in chunks) == estimated_total
    assert all(chunk.record_count <= 1_000 for chunk in chunks)
    assert all(chunk.estimated_input_tokens <= 10_000_000 for chunk in chunks)
    assert [chunk.chunk_number for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert elapsed_seconds > 0
    assert source_row_bytes > 1_000_000_000
    assert report_size_bytes > 1_000_000
    assert peak_rss - baseline_rss < 64 * 1024 * 1024


def test_report_file_write_is_atomic_and_leaves_no_temporary_file(tmp_path) -> None:
    from hansard_annotator.llm_eval.hostility_preflight import build_report

    output = tmp_path / "preflight.json"
    report = build_report(
        _Connection(), corpus_public_id="corpus-1", seed="full-v1",
        google_enqueued_token_limit=2_000_000, output_path=output,
    )
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved == report
    assert list(tmp_path.iterdir()) == [output]
