from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scripts.build_hostility_heldout_sample import (
    DOMAINS,
    POLICY_POPULATIONS,
    SAMPLE_SIZE,
    SEED,
    proportional_allocation,
    select_rows,
)

from hansard_annotator.web.tasks.frozen_samples import load_frozen_sample

ROOT = Path(__file__).parents[1]
EXPECTED_ALLOCATION = {
    "AU01": 12,
    "AU02": 14,
    "AU03": 8,
    "AU04": 8,
    "AU05": 9,
    "AU06": 7,
    "AU07": 12,
    "AU08": 4,
    "AU09": 4,
    "AU10": 4,
    "AU11": 2,
    "AU12": 8,
    "AU13": 7,
    "AU14": 1,
}


def _frame() -> pa.Table:
    rows: list[dict[str, object]] = []
    for domain in DOMAINS:
        for number in range(25):
            rows.append(
                {
                    "corpus_order": len(rows) + 1,
                    "turn_key": f"{domain}-turn-{number:02d}",
                    "policy_status": "POLICY",
                    "primary_domain": domain,
                    "prediction_outcome": "PREDICTION",
                }
            )
    rows.extend(
        [
            {**rows[0], "turn_key": "non-policy", "policy_status": "NON_POLICY"},
            {**rows[0], "turn_key": "unavailable", "policy_status": "UNAVAILABLE"},
            {**rows[0], "turn_key": "refusal", "prediction_outcome": "PROVIDER_REFUSAL"},
            {**rows[0], "turn_key": "invalid-domain", "primary_domain": "AU_OTHER_REVIEW"},
        ]
    )
    return pa.Table.from_pylist(rows)


def test_hamilton_allocation_is_exact_and_deterministic() -> None:
    assert proportional_allocation(POLICY_POPULATIONS) == EXPECTED_ALLOCATION
    assert proportional_allocation(dict(reversed(POLICY_POPULATIONS.items()))) == (
        EXPECTED_ALLOCATION
    )
    assert sum(EXPECTED_ALLOCATION.values()) == SAMPLE_SIZE


def test_selection_is_blind_disjoint_and_deterministic() -> None:
    excluded = {"AU01-turn-00", "AU02-turn-00", "AU14-turn-00"}
    first = select_rows(_frame(), excluded)
    second = select_rows(_frame(), excluded)
    keys = [str(row["turn_key"]) for row in first]
    assert keys == [str(row["turn_key"]) for row in second]
    assert len(keys) == len(set(keys)) == 100
    assert not excluded.intersection(keys)
    assert Counter(str(row["primary_domain"]) for row in first) == Counter(
        EXPECTED_ALLOCATION
    )
    assert all(row["policy_status"] == "POLICY" for row in first)
    assert all(row["prediction_outcome"] == "PREDICTION" for row in first)
    assert all(row["primary_domain"] in DOMAINS for row in first)
    assert all(row["selection_seed"] == SEED for row in first)


def test_committed_heldout_definition_and_development_disjointness() -> None:
    heldout_path = ROOT / "config/research_samples/hostility-heldout-100.json"
    if not heldout_path.exists():
        pytest.skip("held-out frozen definition has not been generated yet")
    sample = load_frozen_sample(heldout_path, "hostility-heldout-100")
    development = json.loads(
        (ROOT / "config/research_samples/hostility-development-100.json").read_text(
            encoding="utf-8"
        )
    )
    development_keys = {str(turn["turn_key"]) for turn in development["turns"]}
    keys = [turn.turn_key for turn in sample.turns]
    assert sample.sample_role == "heldout_validation"
    assert sample.effective_version == "1.0.0"
    assert sample.seed == SEED
    assert Counter(turn.primary_domain for turn in sample.turns) == Counter(
        EXPECTED_ALLOCATION
    )
    assert len(keys) == len(set(keys)) == 100
    assert not development_keys.intersection(keys)
    assert development["sample_sha256"] == (
        "c69219f8d28104366db0825d6b92ca7be4c4819683a088a7b12168d61a564d12"
    )


def test_local_frame_reproduces_all_heldout_artifacts() -> None:
    frame_path = ROOT / "data/processed/policy_sampling_frame.parquet"
    frozen_path = ROOT / "config/research_samples/hostility-heldout-100.json"
    if not frame_path.exists() or not frozen_path.exists():
        pytest.skip("local policy frame and generated held-out definition are required")
    development = json.loads(
        (ROOT / "config/research_samples/hostility-development-100.json").read_text(
            encoding="utf-8"
        )
    )
    excluded = {str(turn["turn_key"]) for turn in development["turns"]}
    rows = select_rows(pq.read_table(frame_path), excluded)  # type: ignore[no-untyped-call]
    sample = load_frozen_sample(frozen_path)
    assert [str(row["turn_key"]) for row in rows] == [turn.turn_key for turn in sample.turns]
    assert [str(row["primary_domain"]) for row in rows] == [
        turn.primary_domain for turn in sample.turns
    ]
    with (ROOT / "data/processed/hostility_heldout_100.csv").open(
        encoding="utf-8", newline=""
    ) as source:
        csv_rows = list(csv.DictReader(source))
    parquet_rows = pq.read_table(  # type: ignore[no-untyped-call]
        ROOT / "data/processed/hostility_heldout_100.parquet"
    ).to_pylist()
    expected = [(turn.sample_order, turn.turn_key, turn.primary_domain) for turn in sample.turns]
    assert [
        (int(row["sample_order"]), row["turn_key"], row["primary_domain"])
        for row in csv_rows
    ] == expected
    assert [
        (int(row["sample_order"]), row["turn_key"], row["primary_domain"])
        for row in parquet_rows
    ] == expected
