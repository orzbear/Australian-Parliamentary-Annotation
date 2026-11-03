"""Benchmark canonical export serialization with deterministic synthetic rows.

This isolates Python transformation and artifact writing. It does not measure
PostgreSQL selection or row fetching and is not a substitute for a representative
database benchmark.
"""

from __future__ import annotations

import argparse
import csv
import json
import tempfile
import time
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from hansard_annotator.web.campaigns.service import production_configuration
from hansard_annotator.web.research_exports.canonical_ai import (
    EXPORT_VERSION,
    FIELDS,
    OUTCOME_PREDICTION,
    PARQUET_SCHEMA,
    ROW_BATCH_SIZE,
    _csv_row,
    _output_row,
    _sha256,
    _write_deterministic_zip,
)


def _synthetic_rows(size: int, configuration: dict[str, Any]) -> Iterator[dict[str, Any]]:
    speech = "A representative synthetic Hansard sentence for export profiling. " * 8
    for order in range(1, size + 1):
        yield {
            "corpus_public_id": "00000000-0000-0000-0000-000000000001",
            "corpus_slug": "synthetic-house",
            "corpus_name": "Synthetic House",
            "turn_id": order,
            "turn_key": f"turn-{order:08d}",
            "speech_date": date(2026, 1, 1),
            "chamber": "House of Representatives",
            "source_file": f"2026/{order // 100:05d}.xml",
            "source_url": None,
            "turn_sequence": order,
            "speaker_id_raw": f"speaker-{order % 1000}",
            "speaker_name_raw": f"Synthetic Speaker {order % 1000}",
            "major_heading_original": "Debate",
            "minor_heading_original": "Synthetic business",
            "business_type": "Debate",
            "calculated_word_count": 72,
            "warning_codes": [],
            "text_clean": speech,
            "text_raw": speech,
            "usable_text": True,
            "prediction_public_id": f"00000000-0000-0000-0000-{order:012d}",
            "prediction_match_count": 1,
            "abstention_public_id": None,
            "abstention_match_count": None,
            "prediction_task": "policy",
            "is_non_policy": False,
            "primary_australian_domain": "AU01",
            "secondary_australian_domains": ["AU02"],
            "reasoning": "A concise synthetic policy rationale.",
            "prediction_returned_model": configuration["configured_model"],
            "prediction_artifact_reference": "synthetic/predictions.jsonl#1",
            "prediction_created_at": datetime(2026, 1, 2, tzinfo=UTC),
            "provider": configuration["provider"],
            "configured_model": configuration["configured_model"],
            "run_returned_model": configuration["configured_model"],
            "prompt_version": configuration["prompt_version"],
            "prompt_sha256": configuration["prompt_sha256"],
            "canonical_schema_sha256": configuration["canonical_schema_sha256"],
            "prediction_contract_sha256": configuration[
                "prediction_contract_sha256"
            ],
            "provider_schema_sha256": configuration["provider_schema_sha256"],
            "generation_config": dict(configuration["generation_config"]),
            "execution_mode": configuration["execution_mode"],
            "run_public_id": "00000000-0000-0000-0000-000000000002",
            "run_artifact_sha256": "a" * 64,
            "run_record_set_sha256": "b" * 64,
            "provider_job_id": "synthetic-batch",
            "run_imported_at": datetime(2026, 1, 3, tzinfo=UTC),
            "campaign_public_id": None,
            "latest_flagged_at": None,
        }


def _run(size: int, *, reuse_configuration: bool = True) -> dict[str, Any]:
    started = time.perf_counter()
    configuration = production_configuration()
    timings = {"transform_seconds": 0.0, "csv_seconds": 0.0, "parquet_seconds": 0.0}
    counts = {OUTCOME_PREDICTION: 0, "PROVIDER_REFUSAL": 0, "NO_USABLE_TEXT": 0}
    run_ids: set[str] = set()
    parquet_batch: list[dict[str, Any]] = []
    tracemalloc.start()

    with tempfile.TemporaryDirectory(prefix=f"canonical-ai-bench-{size}-") as root_name:
        root = Path(root_name)
        csv_path = root / "canonical_full_corpus_ai.csv"
        parquet_path = root / "canonical_full_corpus_ai.parquet"
        manifest_path = root / "manifest.json"
        zip_path = root / "canonical_full_corpus_ai.zip"
        parquet_started = time.perf_counter()
        with csv_path.open("w", encoding="utf-8", newline="") as csv_target:
            writer = csv.DictWriter(csv_target, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            with pq.ParquetWriter(
                parquet_path,
                PARQUET_SCHEMA,
                compression="zstd",
                version="2.6",
                write_statistics=True,
            ) as parquet_writer:
                timings["parquet_seconds"] += time.perf_counter() - parquet_started
                for order, raw in enumerate(_synthetic_rows(size, configuration), 1):
                    transform_started = time.perf_counter()
                    row = _output_row(
                        raw, order, configuration if reuse_configuration else None
                    )
                    timings["transform_seconds"] += time.perf_counter() - transform_started
                    counts[str(row["outcome_status"])] += 1
                    if row["prediction_run_id"]:
                        run_ids.add(str(row["prediction_run_id"]))

                    csv_started = time.perf_counter()
                    writer.writerow(_csv_row(row))
                    timings["csv_seconds"] += time.perf_counter() - csv_started
                    parquet_batch.append(row)
                    if len(parquet_batch) == ROW_BATCH_SIZE:
                        parquet_started = time.perf_counter()
                        parquet_writer.write_table(
                            pa.Table.from_pylist(parquet_batch, schema=PARQUET_SCHEMA)
                        )
                        timings["parquet_seconds"] += time.perf_counter() - parquet_started
                        parquet_batch.clear()
                if parquet_batch:
                    parquet_started = time.perf_counter()
                    parquet_writer.write_table(
                        pa.Table.from_pylist(parquet_batch, schema=PARQUET_SCHEMA)
                    )
                    timings["parquet_seconds"] += time.perf_counter() - parquet_started
                    parquet_batch.clear()

        generated = datetime(2026, 1, 1, tzinfo=UTC)
        checksum_started = time.perf_counter()
        files = {
            csv_path.name: {"sha256": _sha256(csv_path), "bytes": csv_path.stat().st_size},
            parquet_path.name: {
                "sha256": _sha256(parquet_path),
                "bytes": parquet_path.stat().st_size,
            },
        }
        checksum_seconds = time.perf_counter() - checksum_started

        manifest = {
            "export_version": EXPORT_VERSION,
            "generated_at": generated.isoformat(),
            "configuration": configuration,
            "corpus": {
                "public_id": "00000000-0000-0000-0000-000000000001",
                "slug": "synthetic-house",
                "name": "Synthetic House",
            },
            "ordering": "speech_date, source_file bytewise, turn_sequence, turn_key bytewise",
            "row_count": size,
            "outcome_counts": counts,
            "prediction_run_ids": sorted(run_ids),
            "campaign_ids": [],
            "fields": FIELDS,
            "parquet_schema": str(PARQUET_SCHEMA),
            "files": files,
        }
        manifest_started = time.perf_counter()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest_seconds = time.perf_counter() - manifest_started
        zip_started = time.perf_counter()
        _write_deterministic_zip(zip_path, [csv_path, parquet_path, manifest_path])
        zip_seconds = time.perf_counter() - zip_started
        total_seconds = time.perf_counter() - started
        _, peak_python_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "rows": size,
            "configuration_reused": reuse_configuration,
            "total_seconds": round(total_seconds, 3),
            "rows_per_second": round(size / total_seconds, 1),
            "transform_seconds": round(timings["transform_seconds"], 3),
            "csv_seconds": round(timings["csv_seconds"], 3),
            "parquet_seconds": round(timings["parquet_seconds"], 3),
            "checksum_seconds": round(checksum_seconds, 3),
            "manifest_seconds": round(manifest_seconds, 3),
            "zip_seconds": round(zip_seconds, 3),
            "peak_python_tracemalloc_bytes": peak_python_bytes,
            "csv_bytes": csv_path.stat().st_size,
            "parquet_bytes": parquet_path.stat().st_size,
            "zip_bytes": zip_path.stat().st_size,
            "csv_sha256": files[csv_path.name]["sha256"],
            "parquet_sha256": files[parquet_path.name]["sha256"],
            "manifest_sha256": _sha256(manifest_path),
            "zip_sha256": _sha256(zip_path),
            "database_selection_seconds": None,
            "database_fetch_seconds": None,
            "database_query_count": None,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000, 50_000])
    parser.add_argument(
        "--compare-configuration-cache",
        action="store_true",
        help="run uncached-per-row and cached-per-export variants for direct comparison",
    )
    args = parser.parse_args()
    if any(size <= 0 for size in args.sizes):
        parser.error("all sizes must be positive")
    results = [
        _run(size, reuse_configuration=reuse)
        for size in args.sizes
        for reuse in (
            (False, True) if args.compare_configuration_cache else (True,)
        )
    ]
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
