"""Build and freeze the blind 100-turn hostility held-out validation sample."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from math import floor
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from hansard_annotator.web.tasks.frozen_samples import FrozenTurn, selection_sha256

SEED = 20261026
SAMPLE_ID = "hostility-heldout-100"
SAMPLE_SIZE = 100
DOMAINS = tuple(f"AU{number:02d}" for number in range(1, 15))
POLICY_POPULATIONS = {
    "AU01": 13949,
    "AU02": 17071,
    "AU03": 9378,
    "AU04": 9615,
    "AU05": 10721,
    "AU06": 8107,
    "AU07": 13738,
    "AU08": 4474,
    "AU09": 4533,
    "AU10": 4353,
    "AU11": 2235,
    "AU12": 9723,
    "AU13": 8530,
    "AU14": 1466,
}
ALGORITHM = (
    "Hamilton largest-remainder allocation from frozen AU01-AU14 policy populations; "
    "within-domain sha256(turn_key + ':' + seed), turn_key; v1"
)
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


def proportional_allocation(
    populations: dict[str, int], sample_size: int = SAMPLE_SIZE
) -> dict[str, int]:
    if sample_size < 1 or set(populations) != set(DOMAINS) or any(
        value < 1 for value in populations.values()
    ):
        raise ValueError("positive AU01-AU14 populations and sample size are required")
    total = sum(populations.values())
    quotas = {domain: sample_size * populations[domain] / total for domain in DOMAINS}
    allocation = {domain: floor(quotas[domain]) for domain in DOMAINS}
    remaining = sample_size - sum(allocation.values())
    ranked = sorted(DOMAINS, key=lambda domain: (-(quotas[domain] % 1), domain))
    for domain in ranked[:remaining]:
        allocation[domain] += 1
    return allocation


def select_rows(
    frame: pa.Table,
    development_turn_keys: set[str],
    allocation: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    selected_allocation = allocation or proportional_allocation(POLICY_POPULATIONS)
    eligible = [
        row
        for row in frame.to_pylist()
        if row["policy_status"] == "POLICY"
        and row["prediction_outcome"] == "PREDICTION"
        and row["primary_domain"] in DOMAINS
        and str(row["turn_key"]) not in development_turn_keys
    ]
    populations = Counter(str(row["primary_domain"]) for row in eligible)
    selected: list[dict[str, Any]] = []
    for domain in DOMAINS:
        candidates = [row for row in eligible if row["primary_domain"] == domain]
        candidates.sort(
            key=lambda row: (
                hashlib.sha256(f"{row['turn_key']}:{SEED}".encode()).hexdigest(),
                str(row["turn_key"]),
            )
        )
        count = selected_allocation[domain]
        if len(candidates) < count:
            raise ValueError(f"{domain} has only {len(candidates)} eligible turns")
        for row in candidates[:count]:
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
    development_path = Path("config/research_samples/hostility-development-100.json")
    frame_manifest = json.loads(frame_manifest_path.read_text(encoding="utf-8"))
    development = json.loads(development_path.read_text(encoding="utf-8"))
    development_keys = {str(turn["turn_key"]) for turn in development["turns"]}
    if len(development_keys) != 100:
        raise ValueError("development exclusion must contain 100 unique turn keys")
    frame_sha = file_sha256(frame_path)
    if frame_sha != frame_manifest["output"]["sha256"]:
        raise ValueError("policy frame hash does not match its manifest")
    frame = pq.read_table(frame_path)  # type: ignore[no-untyped-call]
    allocation = proportional_allocation(POLICY_POPULATIONS)
    rows = select_rows(frame, development_keys, allocation)
    keys = [str(row["turn_key"]) for row in rows]
    if len(rows) != SAMPLE_SIZE or len(set(keys)) != SAMPLE_SIZE:
        raise ValueError("held-out sample must contain exactly 100 unique turns")
    if development_keys.intersection(keys):
        raise ValueError("held-out sample overlaps the development sample")
    csv_path = processed / "hostility_heldout_100.csv"
    parquet_path = processed / "hostility_heldout_100.parquet"
    manifest_path = processed / "hostility_heldout_100.manifest.json"
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
        "sample_version": "1.0.0",
        "sample_role": "heldout_validation",
        "purpose": "HOSTILITY_HELDOUT_VALIDATION",
        "source_frame_path": str(frame_path).replace("\\", "/"),
        "source_frame_sha256": frame_sha,
        "source_canonical_sha256": frame_manifest["source"]["sha256"],
        "eligible_population": [
            "policy_status == POLICY",
            "prediction_outcome == PREDICTION",
            "primary_domain in AU01-AU14",
            "turn_key not in hostility-development-100",
        ],
        "development_sample_exclusion": str(development_path).replace("\\", "/"),
        "seed": SEED,
        "allocation": allocation,
        "selection_algorithm": ALGORITHM,
        "algorithm_version": "1",
        "sample_size": SAMPLE_SIZE,
        "sample_sha256": sample_hash,
        "created_at": created_at,
        "turns": [turn.model_dump() for turn in frozen_turns],
    }
    frozen_path = Path("config/research_samples/hostility-heldout-100.json")
    if frozen_path.exists():
        existing = json.loads(frozen_path.read_text(encoding="utf-8"))
        if existing.get("turns") != frozen["turns"]:
            raise ValueError("refusing to replace an existing different frozen sample")
        frozen["created_at"] = existing["created_at"]
    frozen_path.write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    eligible_rows = [
        row
        for row in frame.to_pylist()
        if row["policy_status"] == "POLICY"
        and row["prediction_outcome"] == "PREDICTION"
        and row["primary_domain"] in DOMAINS
        and str(row["turn_key"]) not in development_keys
    ]
    manifest = {
        **{key: value for key, value in frozen.items() if key != "turns"},
        "source_frame": {
            "filename": frame_path.name,
            "row_count": frame.num_rows,
            "sha256": frame_sha,
        },
        "eligible_population_count": len(eligible_rows),
        "eligible_domain_counts": dict(
            sorted(Counter(str(row["primary_domain"]) for row in eligible_rows).items())
        ),
        "selected_turn_count": len(rows),
        "unique_turn_count": len(set(keys)),
        "development_overlap_count": len(development_keys.intersection(keys)),
        "domain_counts": dict(Counter(str(row["primary_domain"]) for row in rows)),
        "generated_files": {
            csv_path.name: {"bytes": csv_path.stat().st_size, "sha256": file_sha256(csv_path)},
            parquet_path.name: {
                "bytes": parquet_path.stat().st_size,
                "sha256": file_sha256(parquet_path),
            },
            frozen_path.name: {
                "bytes": frozen_path.stat().st_size,
                "sha256": file_sha256(frozen_path),
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
