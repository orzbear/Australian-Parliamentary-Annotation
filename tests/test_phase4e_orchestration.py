from __future__ import annotations

import inspect
import json
import os
import threading
import time
import tracemalloc
from decimal import Decimal
from pathlib import Path
from typing import Any

import psutil

from hansard_annotator.llm_eval.import_predictions import (
    IMPORT_BATCH_SIZE,
    _batched_rows,
    load_prediction_artifact,
)
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_MODEL,
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
    canonical_hash,
)
from hansard_annotator.web.campaigns.phase4e import (
    CHUNK_PLANNER_VERSION,
    PHASE4E_ADVISORY_LOCK_NAMESPACE,
    _cache_receipt_for_chunk,
    _orchestration_tick_locked,
    active_estimated_tokens,
    build_campaign_effective_artifact,
    committed_spend,
    deterministic_chunks,
    estimate_record_input_tokens,
    orchestration_tick,
    projected_spend_with_candidate,
    submission_fits,
)
from hansard_annotator.web.campaigns.recovery import (
    build_effective_artifact_streaming,
)
from hansard_annotator.web.campaigns.service import (
    GEMINI_BATCH_SCHEMA_SHA256,
    PREDICTION_CONTRACT_SHA256,
)


def test_phase4e_preserves_all_frozen_production_identities() -> None:
    assert FROZEN_PROMPT_SHA256 == (
        "a41027b001f10b61074c196996606c86b5dac4c0788ae185a05ff68fcf3b7cc6"
    )
    assert FROZEN_CANONICAL_SCHEMA_SHA256 == (
        "9c94bab9cd2976559c79b1c31991aef4138a2071a5d3b3d6186a26448f117126"
    )
    assert PREDICTION_CONTRACT_SHA256 == (
        "99cafa6c473293dc076793ee6baeca44b6961d7cb11de8f9174517defc4a0879"
    )
    assert GEMINI_BATCH_SCHEMA_SHA256 == (
        "980b0ed6d6002a8eadd326f84942ba60dcdf8f2abd9e70ea6aaecf7d78de3c22"
    )


def _records(count: int, *, token_count: int = 10) -> list[dict[str, object]]:
    return [
        {
            "selection_order": index,
            "record_id": f"turn:{index}",
            "estimated_input_tokens": token_count,
        }
        for index in range(count)
    ]


def test_chunk_boundaries_hashes_are_stable_contiguous_and_complete() -> None:
    first = list(deterministic_chunks(_records(11), max_records=4, max_input_tokens=35))
    second = list(deterministic_chunks(_records(11), max_records=4, max_input_tokens=35))
    assert first == second
    assert [(item.first_selection_order, item.last_selection_order) for item in first] == [
        (0, 2),
        (3, 5),
        (6, 8),
        (9, 10),
    ]
    assert sum(item.record_count for item in first) == 11
    assert first[0].record_set_sha256 == canonical_hash(["turn:0", "turn:1", "turn:2"])
    assert [order for item in first for order in range(item.first_selection_order, item.last_selection_order + 1)] == list(range(11))


def test_long_record_forces_token_boundary_without_using_average_length() -> None:
    records = _records(3, token_count=10)
    records[1]["estimated_input_tokens"] = 81
    chunks = list(deterministic_chunks(records, max_records=100, max_input_tokens=90))
    assert [(item.first_selection_order, item.last_selection_order) for item in chunks] == [
        (0, 0),
        (1, 1),
        (2, 2),
    ]
    assert estimate_record_input_tokens("x" * 30_000) > estimate_record_input_tokens("short")


def test_active_token_quota_and_safety_margin_allow_multiple_chunks() -> None:
    chunks = [
        {"status": "RUNNING", "estimated_input_tokens": 3_000_000},
        {"status": "SUBMITTED", "estimated_input_tokens": 2_000_000},
        {"status": "PENDING", "estimated_input_tokens": 9_000_000},
    ]
    active = active_estimated_tokens(chunks)
    assert active == 5_000_000
    assert submission_fits(
        active_tokens=active,
        candidate_tokens=3_500_000,
        ceiling=10_000_000,
        safety_factor=0.9,
    )
    assert not submission_fits(
        active_tokens=active,
        candidate_tokens=4_000_001,
        ceiling=10_000_000,
        safety_factor=0.9,
    )


def test_spend_ceiling_reserves_active_work_and_uses_higher_actual_cost() -> None:
    chunks = [
        {"status": "COLLECTED", "record_count": 25, "actual_cost_usd": "3.00"},
        {"status": "RUNNING", "record_count": 25, "actual_cost_usd": "0"},
        {"status": "PENDING", "record_count": 25, "actual_cost_usd": "0"},
    ]
    assert projected_spend_with_candidate(
        chunks=chunks,
        campaign_estimate=Decimal("8.00"),
        planned_count=100,
        candidate_record_count=25,
    ) == Decimal("7.00")
    assert committed_spend(
        chunks=[
            {"status": "COLLECTED", "record_count": 25, "actual_cost_usd": "1.00"},
            {"status": "RUNNING", "record_count": 25, "actual_cost_usd": "0"},
        ],
        campaign_estimate=Decimal("8.00"),
        planned_count=100,
    ) == Decimal("3.00")


def test_worker_holds_cross_transaction_lock_and_uses_lost_receipt_lookup() -> None:
    source = inspect.getsource(orchestration_tick)
    locked_source = inspect.getsource(_orchestration_tick_locked)
    module_source = Path(
        "src/hansard_annotator/web/campaigns/phase4e.py"
    ).read_text(encoding="utf-8")
    assert "pg_try_advisory_lock" in source
    assert "pg_advisory_unlock" in source
    assert "PHASE4E_ADVISORY_LOCK_NAMESPACE" in source
    assert "PHASE4E_ADVISORY_LOCK_NAMESPACE" in locked_source
    assert PHASE4E_ADVISORY_LOCK_NAMESPACE == 0x50483445
    assert module_source.index("finder.find_batch") < module_source.index(
        "provider.submit(plan"
    )
    assert "status='PENDING',failure_metadata=failure_metadata" in module_source
    assert 'return TickResult("quota-refused"' in module_source
    assert '"awaiting-submission-reconciliation"' in module_source
    assert '"submission_outcome_uncertain": True' in module_source
    assert locked_source.index("_campaign_committed_spend(") < locked_source.index(
        "ensure_campaign_cache("
    )
    assert "summary_coverage[\"missing_responses\"]" in locked_source


def test_cache_storage_receipt_is_attributed_only_to_the_creating_chunk() -> None:
    chunk = {
        "cache_created_at": "2026-10-18T00:00:00Z",
        "cache_expires_at": "2026-10-19T00:00:00Z",
        "cache_metadata": {
            "created_for_phase4e_chunk": True,
            "usageMetadata": {"totalTokenCount": 4131},
        },
    }
    assert _cache_receipt_for_chunk(chunk) == {
        "response": {
            "createTime": "2026-10-18T00:00:00Z",
            "expireTime": "2026-10-19T00:00:00Z",
            "usageMetadata": {"totalTokenCount": 4131},
        }
    }
    chunk["cache_metadata"] = {"created_for_phase4e_chunk": False}
    assert _cache_receipt_for_chunk(chunk) is None


def _success(record_id: str, index: int) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "turn_key": record_id.removeprefix("turn:"),
        "request_index": index,
        "status": "success",
        "prediction": {
            "is_non_policy": True,
            "primary_australian_domain": None,
            "secondary_australian_domains": [],
            "reasoning": "Non-policy.",
        },
        "usage": {},
    }


def _repairable(record_id: str, index: int) -> dict[str, Any]:
    prediction = {
        "is_non_policy": False,
        "primary_australian_domain": "AU07",
        "secondary_australian_domains": ["AU08", "AU07"],
        "reasoning": "AU07 with AU08.",
    }
    return {
        "record_id": record_id,
        "turn_key": record_id.removeprefix("turn:"),
        "request_index": index,
        "status": "invalid",
        "prediction": None,
        "validation_error": "primary domain cannot also be secondary",
        "provider_response": {
            "candidates": [{"content": {"parts": [{"text": json.dumps(prediction)}]}}]
        },
        "usage": {},
    }


def _chunk(path: Path, rows: list[dict[str, Any]], planned: int) -> None:
    path.mkdir()
    for name, value in (
        ("plan.json", {}),
        ("provider_response.json", {}),
    ):
        (path / name).write_text(json.dumps(value), encoding="utf-8")
    (path / "requests.jsonl").write_text("", encoding="utf-8")
    (path / "failures.jsonl").write_text("", encoding="utf-8")
    (path / "predictions.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (path / "summary.json").write_text(
        json.dumps(
            {
                "coverage": {"planned_requests": planned},
                "tokens": {},
                "cost": {},
            }
        ),
        encoding="utf-8",
    )


def _provenance() -> dict[str, Any]:
    return {
        "provider": "gemini",
        "model": APPROVED_PRODUCTION_MODEL,
        "prompt_version": FROZEN_PROMPT_VERSION,
        "assembled_prompt_sha256": FROZEN_PROMPT_SHA256,
        "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
        "prediction_contract_sha256": PREDICTION_CONTRACT_SHA256,
        "provider_schema_sha256": GEMINI_BATCH_SCHEMA_SHA256,
        "thinking_level": "minimal",
        "execution_mode": "batch",
        "batch_id": "phase4e:test",
        "pricing_format_version": 1,
        "pricing_effective_date": "2026-10-01",
    }


def test_aggregate_effective_coverage_and_structural_repair_across_chunks(
    tmp_path: Path,
) -> None:
    first = tmp_path / "chunk-1"
    second = tmp_path / "chunk-2"
    _chunk(first, [_success("turn:0", 0), _repairable("turn:1", 1)], 2)
    _chunk(second, [_success("turn:2", 0)], 1)
    output = tmp_path / "effective"
    summary = build_campaign_effective_artifact(
        chunk_directories=[first, second],
        expected_record_ids=["turn:0", "turn:1", "turn:2"],
        output_directory=output,
        provenance=_provenance(),
    )
    coverage = summary["coverage"]
    assert coverage["original_valid"] == 2
    assert coverage["structurally_repaired"] == 1
    assert coverage["valid_structured_predictions"] == 3
    assert coverage["unresolved_invalid"] == 0
    assert coverage["missing"] == 0
    assert coverage["duplicate"] == 0
    assert coverage["accounting_invariant_ok"] is True
    _loaded_summary, rows, _hash = load_prediction_artifact(output)
    assert rows[1]["prediction"]["secondary_australian_domains"] == ["AU08"]
    assert [row["request_index"] for row in rows] == [0, 1, 2]


def test_aggregate_detects_missing_and_duplicate_identities(tmp_path: Path) -> None:
    chunk = tmp_path / "chunk"
    duplicate = _success("turn:0", 0)
    _chunk(chunk, [_success("turn:0", 0), duplicate], 2)
    summary = build_campaign_effective_artifact(
        chunk_directories=[chunk],
        expected_record_ids=["turn:0", "turn:1"],
        output_directory=tmp_path / "effective",
        provenance=_provenance(),
    )
    assert summary["coverage"]["missing"] == 1
    assert summary["coverage"]["duplicate"] == 1
    assert summary["coverage"]["accounting_invariant_ok"] is False
    rows = [json.loads(line) for line in (tmp_path / "effective" / "predictions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[1]["record_id"] == "turn:1"
    assert rows[1]["status"] == "missing"


def test_streaming_recovery_replaces_missing_without_materializing_campaign(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    original.mkdir()
    rows = [_success("turn:0", 0), {"record_id": "turn:1", "request_index": 1, "status": "missing"}]
    (original / "predictions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    summary = {
        "coverage": {"planned_requests": 2},
        "tokens": {},
        "cost": {"total_pilot_cost_usd": 0},
        "provenance": {},
    }
    (original / "operational_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    (recovery / "predictions.jsonl").write_text(
        json.dumps(_success("turn:1", 0)) + "\n", encoding="utf-8"
    )
    result, count, _digest = build_effective_artifact_streaming(
        original,
        [{
            "attempt_number": 1,
            "provider_job_id": "batches/recovery",
            "artifact_reference": str(recovery),
            "operational_metadata": {"tokens": {}, "cost": {}},
        }],
        tmp_path / "recovered",
    )
    assert count == 2
    assert result["coverage"]["valid_structured_predictions"] == 2
    assert result["coverage"]["missing_responses"] == 0
    assert result["coverage"]["accounting_invariant_ok"] is True


def test_160k_chunk_planning_and_jsonl_traversal_are_bounded_memory(tmp_path: Path) -> None:
    count = 160_000

    def records() -> Any:
        for index in range(count):
            yield {
                "selection_order": index,
                "record_id": f"turn:{index}",
                "estimated_input_tokens": 5_000,
            }

    tracemalloc.start()
    chunks = list(
        deterministic_chunks(records(), max_records=1_000, max_input_tokens=9_000_000)
    )
    _current, planning_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(chunks) == 160
    assert planning_peak < 20 * 1024 * 1024

    chunk_root = tmp_path / "chunks"
    chunk_root.mkdir()
    chunk_directories: list[Path] = []
    for chunk_number in range(160):
        start = chunk_number * 1_000
        directory = chunk_root / f"chunk-{chunk_number + 1:06d}"
        _chunk(
            directory,
            [_success(f"turn:{start + offset}", offset) for offset in range(1_000)],
            1_000,
        )
        chunk_directories.append(directory)
    process = psutil.Process(os.getpid())
    baseline_rss = process.memory_info().rss
    peak_rss = [baseline_rss]
    stop_sampling = threading.Event()

    def sample_rss() -> None:
        while not stop_sampling.is_set():
            peak_rss[0] = max(peak_rss[0], process.memory_info().rss)
            time.sleep(0.01)

    sampler = threading.Thread(target=sample_rss, daemon=True)
    sampler.start()
    build_campaign_effective_artifact(
        chunk_directories=chunk_directories,
        expected_record_ids=(f"turn:{index}" for index in range(count)),
        output_directory=tmp_path / "scale",
        provenance=_provenance(),
    )
    stop_sampling.set()
    sampler.join()
    aggregation_peak_delta = peak_rss[0] - baseline_rss
    assert aggregation_peak_delta < 80 * 1024 * 1024

    artifact = tmp_path / "scale"
    traversal_baseline_rss = process.memory_info().rss
    traversal_peak_rss = [traversal_baseline_rss]
    traversal_stop = threading.Event()

    def sample_traversal_rss() -> None:
        while not traversal_stop.is_set():
            traversal_peak_rss[0] = max(
                traversal_peak_rss[0], process.memory_info().rss
            )
            time.sleep(0.01)

    traversal_sampler = threading.Thread(target=sample_traversal_rss, daemon=True)
    traversal_sampler.start()
    traversed = 0
    largest_batch = 0
    for batch in _batched_rows(artifact):
        traversed += len(batch)
        largest_batch = max(largest_batch, len(batch))
    traversal_stop.set()
    traversal_sampler.join()
    traversal_peak_delta = traversal_peak_rss[0] - traversal_baseline_rss
    print(
        "Phase 4E 160k peaks: "
        f"planning_tracemalloc={planning_peak} aggregation_rss_delta={aggregation_peak_delta} "
        f"import_traversal_rss_delta={traversal_peak_delta} bytes"
    )
    assert traversed == count
    assert largest_batch == IMPORT_BATCH_SIZE
    assert traversal_peak_delta < 80 * 1024 * 1024


def test_migration_preserves_legacy_and_freezes_child_provenance() -> None:
    migration = Path(
        "migrations/versions/20261018_09_full_corpus_orchestration.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "20261017_08"' in migration
    assert "orchestration_mode text NOT NULL DEFAULT 'LEGACY'" in migration
    assert "CREATE TABLE ai_annotation_campaign_batches" in migration
    assert "campaign child Batch provenance is immutable" in migration
    assert "full-production authorization is immutable" in migration
    assert "Phase 4E downgrade refused" in migration
    assert CHUNK_PLANNER_VERSION == "phase4e-full-input-v1"


def test_importer_streams_and_uses_bounded_executemany() -> None:
    source = Path("src/hansard_annotator/llm_eval/import_predictions.py").read_text(
        encoding="utf-8"
    )
    assert ".read_text(encoding=\"utf-8\").splitlines()" not in source
    assert "IMPORT_BATCH_SIZE = 1_000" in source
    assert "insert_cursor.executemany" in source
