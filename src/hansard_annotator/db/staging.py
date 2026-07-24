"""Bounded PyArrow-to-PostgreSQL JSONB staging through psycopg COPY."""

from __future__ import annotations

import gc
import hashlib
import json
from collections.abc import Callable
from datetime import date, time
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.dataset as ds
from psycopg.types.json import Jsonb


def _json_default(value: object) -> str:
    if isinstance(value, (date, time)):
        return value.isoformat()
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_record(row: dict[str, Any]) -> str:
    return json.dumps(
        row,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )


def record_sha256(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_record(row).encode("utf-8")).hexdigest()


def copy_parquet_payloads(
    connection: psycopg.Connection[Any],
    dataset_path: Path,
    *,
    columns: list[str],
    batch_size: int,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> int:
    dataset = ds.dataset(  # type: ignore[no-untyped-call]
        dataset_path, format="parquet", partitioning="hive"
    )
    scanner = dataset.scanner(
        columns=columns,
        batch_size=batch_size,
        batch_readahead=1,
        fragment_readahead=1,
        use_threads=False,
    )
    total = 0
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE stage_payload")
        with cursor.copy(
            "COPY stage_payload (payload, record_sha256) FROM STDIN"
        ) as copy:
            for batch in scanner.to_batches():
                for row in batch.to_pylist():
                    if transform is not None:
                        row = transform(row)
                    canonical = canonical_record(row)
                    copy.write_row(
                        (
                            Jsonb(json.loads(canonical)),
                            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                        )
                    )
                    total += 1
    del scanner, dataset
    gc.collect()
    pa.default_memory_pool().release_unused()
    return total


def copy_dict_payloads(
    connection: psycopg.Connection[Any], rows: list[dict[str, Any]]
) -> int:
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE stage_payload")
        with cursor.copy(
            "COPY stage_payload (payload, record_sha256) FROM STDIN"
        ) as copy:
            for row in rows:
                canonical = canonical_record(row)
                copy.write_row(
                    (
                        Jsonb(json.loads(canonical)),
                        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    )
                )
    return len(rows)
