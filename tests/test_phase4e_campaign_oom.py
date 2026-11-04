from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from hansard_annotator.web.campaigns import service


class _ScaleConnection:
    def __init__(self) -> None:
        self.validation_batches: list[int] = []
        self.export_batches: list[int] = []

    def execute(self, query: str, params: tuple[Any, ...]) -> Any:
        record_ids = list(params[1])
        if "AS canonical_text" in query and "turn.turn_key" not in query:
            self.validation_batches.append(len(record_ids))
            assert len(record_ids) <= service.CAMPAIGN_VALIDATION_BATCH_SIZE
            return _Rows(
                [
                    {"record_id": record_id, "canonical_text": "speech"}
                    for record_id in record_ids
                ]
            )
        if "turn.turn_key" in query:
            self.export_batches.append(len(record_ids))
            assert len(record_ids) <= service.CAMPAIGN_UNRESOLVED_BATCH_SIZE
            return _Rows(
                [
                    {
                        "record_id": record_id,
                        "turn_key": record_id.removeprefix("turn:"),
                        "speech_date": "2025-01-01",
                        "speaker_name_raw": "Speaker",
                        "canonical_text": "speech",
                    }
                    for record_id in record_ids
                ]
            )
        if "ai_annotation_campaign_recoveries" in query:
            return _Rows([])
        raise AssertionError(f"unexpected SQL in scale fixture: {query}")


class _Rows:
    def __init__(self, values: list[dict[str, Any]]) -> None:
        self.values = values

    def fetchall(self) -> list[dict[str, Any]]:
        return self.values


def test_production_scale_validation_and_unresolved_streaming_are_bounded(
    tmp_path: Path,
) -> None:
    total = 116_793
    valid = 116_031
    unresolved = 762
    directory = tmp_path / "effective"
    directory.mkdir()
    path = directory / "predictions.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for index in range(valid):
            output.write(
                json.dumps(
                    {
                        "record_id": f"turn:{index:06d}",
                        "status": "success",
                        "prediction": {
                            "hostility_present": "NO",
                            "target": "N/A",
                            "evidence_spans": [],
                            "reasoning": "No hostility is present.",
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        for index in range(valid, total):
            output.write(
                json.dumps(
                    {
                        "record_id": f"turn:{index:06d}",
                        "status": "invalid",
                        "prediction": None,
                        "failure_category": "EVIDENCE_RESOLUTION_FAILURE",
                        "validation_error": "evidence did not resolve",
                        "provider_response": {"candidates": []},
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    connection = _ScaleConnection()
    process = psutil.Process(os.getpid())
    baseline_rss = process.memory_info().rss
    peak_rss = [baseline_rss]
    stop = threading.Event()

    def sample_rss() -> None:
        while not stop.is_set():
            peak_rss[0] = max(peak_rss[0], process.memory_info().rss)
            time.sleep(0.01)

    sampler = threading.Thread(target=sample_rss, daemon=True)
    sampler.start()
    started = time.perf_counter()
    service._validate_hostility_artifact_evidence(
        connection, 1, directory, "hostility-prompt-v2"
    )
    terminal_ids = service._terminal_invalid_record_ids(directory, "hostility")
    exported = list(service._iter_artifact_unresolved_records(connection, 1, directory))
    elapsed = time.perf_counter() - started
    stop.set()
    sampler.join()
    peak_rss[0] = max(peak_rss[0], process.memory_info().rss)

    assert len(connection.validation_batches) == 233
    assert max(connection.validation_batches) <= 500
    assert sum(connection.validation_batches) == valid
    assert len(terminal_ids) == unresolved
    assert len(exported) == unresolved
    assert [row["record_id"] for row in exported] == [
        f"turn:{index:06d}" for index in range(valid, total)
    ]
    assert len(connection.export_batches) == 4
    assert max(connection.export_batches) <= 200
    assert peak_rss[0] - baseline_rss < 128 * 1024 * 1024
    assert elapsed > 0
    print(
        "Phase 4E 116,793-record validation/export: "
        f"peak_rss_delta={peak_rss[0] - baseline_rss} bytes, "
        f"elapsed={elapsed:.2f}s, artifact={path.stat().st_size} bytes"
    )


def test_terminal_recovery_history_reads_each_attempt_artifact_once(
    tmp_path: Path, monkeypatch: Any
) -> None:
    rows: list[dict[str, Any]] = []
    for attempt in (1, 2):
        directory = tmp_path / f"attempt-{attempt}"
        directory.mkdir()
        (directory / "predictions.jsonl").write_text(
            json.dumps(
                {
                    "record_id": "turn:unresolved",
                    "status": "invalid",
                    "recovery_attempt_number": attempt,
                    "recovery_provider_job_id": f"jobs/{attempt}",
                    "recovery_mode": "evidence_only",
                    "original_validation_error": "evidence did not resolve",
                    "validation_error": f"attempt {attempt} did not resolve",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        rows.append(
            {
                "record_id": "turn:unresolved",
                "attempt_number": attempt,
                "recovery_id": f"recovery-{attempt}",
                "provider_job_id": f"jobs/{attempt}",
                "recovery_mode": "evidence_only",
                "artifact_reference": str(directory),
                "original_validation_error": "evidence did not resolve",
            }
        )

    class _Connection:
        def execute(self, _query: str, _params: tuple[Any, ...]) -> _Rows:
            return _Rows(rows)

    read_paths: list[Path] = []
    iter_jsonl = service.iter_jsonl

    def _counted_iter_jsonl(path: Path):
        read_paths.append(path)
        yield from iter_jsonl(path)

    monkeypatch.setattr(service, "iter_jsonl", _counted_iter_jsonl)
    history = service._terminal_recovery_history(
        _Connection(), 42, record_ids={"turn:unresolved"}  # type: ignore[arg-type]
    )

    assert [entry["attempt_number"] for entry in history["turn:unresolved"]] == [1, 2]
    assert all(entry["lineage_consistent"] for entry in history["turn:unresolved"])
    assert [
        entry["replacement_validation_error"]
        for entry in history["turn:unresolved"]
    ] == ["attempt 1 did not resolve", "attempt 2 did not resolve"]
    assert len(read_paths) == 2
