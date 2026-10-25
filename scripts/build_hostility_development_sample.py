"""Build and freeze the first 100-turn hostility development sample."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from hansard_annotator.web.tasks.frozen_samples import FrozenTurn, selection_sha256

SEED = 20261025
ALLOCATION = {"AU01": 8, "AU02": 8, **{f"AU{i:02d}": 7 for i in range(3, 15)}}
ALGORITHM = "sha256(turn_key + ':' + seed), turn_key; per-domain; v1"
SAMPLE_ID = "hostility-development-100"
OUTPUT_COLUMNS = [
    "sample_order",
    "turn_key",
    "primary_domain",
    "policy_status",
    "corpus_order",
    "prediction_outcome",
    "source_campaign_id",
    "source_provider",
    "source_model",
    "source_prompt_version",
    "source_prediction_run_id",
    "selection_seed",
    "stratum_population_n",
    "stratum_sample_n",
]


def select_rows(frame: pa.Table) -> list[dict[str, Any]]:
    rows = frame.to_pylist()
    populations = Counter(
        str(row["primary_domain"])
        for row in rows
        if row["policy_status"] == "POLICY"
        and row["prediction_outcome"] == "PREDICTION"
        and row["primary_domain"] in ALLOCATION
    )
    selected: list[dict[str, Any]] = []
    for domain, count in ALLOCATION.items():
        eligible = [
            row
            for row in rows
            if row["policy_status"] == "POLICY"
            and row["prediction_outcome"] == "PREDICTION"
            and row["primary_domain"] == domain
        ]
        eligible.sort(
            key=lambda row: (
                hashlib.sha256(f"{row['turn_key']}:{SEED}".encode()).hexdigest(),
                str(row["turn_key"]),
            )
        )
        if len(eligible) < count:
            raise ValueError(f"{domain} has only {len(eligible)} eligible turns")
        for row in eligible[:count]:
            selected.append(
                {
                    **{name: row.get(name) for name in OUTPUT_COLUMNS if name in row},
                    "selection_seed": SEED,
                    "stratum_population_n": populations[domain],
                    "stratum_sample_n": count,
                }
            )
    for order, row in enumerate(selected, 1):
        row["sample_order"] = order
    return selected


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    processed = Path("data/processed")
    frame_path = processed / "policy_sampling_frame.parquet"
    frame_manifest_path = processed / "policy_sampling_frame.manifest.json"
    frame_manifest = json.loads(frame_manifest_path.read_text(encoding="utf-8"))
    frame_sha = file_sha256(frame_path)
    if frame_sha != frame_manifest["output"]["sha256"]:
        raise ValueError("policy frame hash does not match its manifest")
    frame = pq.read_table(frame_path)  # type: ignore[no-untyped-call]
    rows = select_rows(frame)
    if len(rows) != 100 or len({row["turn_key"] for row in rows}) != 100:
        raise ValueError("development sample must contain exactly 100 unique turns")
    csv_path = processed / "hostility_development_100.csv"
    parquet_path = processed / "hostility_development_100.parquet"
    manifest_path = processed / "hostility_development_100.manifest.json"
    with csv_path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    pq.write_table(  # type: ignore[no-untyped-call]
        pa.Table.from_pylist(rows).select(OUTPUT_COLUMNS), parquet_path, compression="zstd"
    )
    frozen_turns = [
        FrozenTurn(
            sample_order=int(row["sample_order"]),
            turn_key=str(row["turn_key"]),
            primary_domain=str(row["primary_domain"]),
        )
        for row in rows
    ]
    sample_hash = selection_sha256(frozen_turns)
    created_at = datetime.now(UTC).isoformat()
    frozen = {
        "schema_version": "1",
        "sample_id": SAMPLE_ID,
        "purpose": "HOSTILITY_DEVELOPMENT",
        "version": "1.0.0",
        "source_frame_sha256": frame_sha,
        "source_canonical_sha256": frame_manifest["source"]["sha256"],
        "seed": SEED,
        "allocation": ALLOCATION,
        "selection_algorithm": ALGORITHM,
        "sample_size": 100,
        "sample_sha256": sample_hash,
        "created_at": created_at,
        "turns": [turn.model_dump() for turn in frozen_turns],
    }
    frozen_path = Path("config/research_samples/hostility-development-100.json")
    frozen_path.parent.mkdir(parents=True, exist_ok=True)
    if frozen_path.exists():
        existing = json.loads(frozen_path.read_text(encoding="utf-8"))
        if existing.get("turns") != frozen["turns"]:
            raise ValueError("refusing to replace an existing different frozen sample")
        frozen["created_at"] = existing["created_at"]
    frozen_path.write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    manifest = {
        **{key: frozen[key] for key in frozen if key != "turns"},
        "source_frame": {
            "filename": frame_path.name,
            "row_count": frame.num_rows,
            "sha256": frame_sha,
        },
        "eligibility": [
            "policy_status == POLICY",
            "prediction_outcome == PREDICTION",
            "primary_domain in AU01-AU14",
        ],
        "selected_turn_count": len(rows),
        "domain_counts": dict(Counter(str(row["primary_domain"]) for row in rows)),
        "generated_files": {
            csv_path.name: {"bytes": csv_path.stat().st_size, "sha256": file_sha256(csv_path)},
            parquet_path.name: {
                "bytes": parquet_path.stat().st_size,
                "sha256": file_sha256(parquet_path),
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
