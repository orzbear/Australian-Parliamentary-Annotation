from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scripts.build_hostility_development_sample import ALLOCATION, SEED, select_rows

from hansard_annotator.web.tasks.frozen_samples import (
    FrozenSample,
    load_frozen_sample,
    map_frozen_turn_ids,
)


def _frame() -> pa.Table:
    rows: list[dict[str, object]] = []
    order = 0
    for domain in ALLOCATION:
        for number in range(12):
            order += 1
            rows.append(
                {
                    "corpus_order": order,
                    "turn_key": f"{domain}-turn-{number:02d}",
                    "policy_status": "POLICY",
                    "primary_domain": domain,
                    "prediction_outcome": "PREDICTION",
                    "source_campaign_id": "campaign",
                    "source_provider": "provider",
                    "source_model": "model",
                    "source_prompt_version": "prompt",
                    "source_prediction_run_id": "run",
                }
            )
    rows.extend(
        [
            {**rows[0], "turn_key": "excluded-non-policy", "policy_status": "NON_POLICY"},
            {**rows[0], "turn_key": "excluded-refusal", "prediction_outcome": "PROVIDER_REFUSAL"},
        ]
    )
    return pa.Table.from_pylist(rows)


def test_exact_deterministic_eligible_development_sample() -> None:
    first = select_rows(_frame())
    second = select_rows(_frame())

    assert [row["turn_key"] for row in first] == [row["turn_key"] for row in second]
    assert len(first) == len({row["turn_key"] for row in first}) == 100
    assert Counter(row["primary_domain"] for row in first) == Counter(ALLOCATION)
    assert all(row["policy_status"] == "POLICY" for row in first)
    assert all(row["prediction_outcome"] == "PREDICTION" for row in first)
    assert all(row["selection_seed"] == SEED for row in first)


def test_committed_frozen_sample_definition() -> None:
    root = Path(__file__).parents[1]
    sample = load_frozen_sample(
        root / "config/research_samples/hostility-development-100.json",
        "hostility-development-100",
    )

    assert sample.schema_version == "1"
    assert sample.sample_id == "hostility-development-100"
    assert sample.seed == 20261025
    assert sample.sample_size == len(sample.turns) == 100
    assert len({turn.turn_key for turn in sample.turns}) == 100
    assert Counter(turn.primary_domain for turn in sample.turns) == Counter(ALLOCATION)
    assert sample.sample_sha256 == (
        "c69219f8d28104366db0825d6b92ca7be4c4819683a088a7b12168d61a564d12"
    )


def test_local_policy_frame_reproduces_frozen_sample() -> None:
    root = Path(__file__).parents[1]
    frame_path = root / "data/processed/policy_sampling_frame.parquet"
    if not frame_path.exists():
        pytest.skip(
            "local generated policy sampling frame is required for full reproducibility check"
        )
    sample = load_frozen_sample(
        root / "config/research_samples/hostility-development-100.json",
        "hostility-development-100",
    )
    rows = select_rows(pq.read_table(frame_path))
    assert [turn.turn_key for turn in sample.turns] == [str(row["turn_key"]) for row in rows]
    assert [turn.primary_domain for turn in sample.turns] == [
        str(row["primary_domain"]) for row in rows
    ]


def test_mutated_frozen_sample_and_missing_database_key_fail(tmp_path: Path) -> None:
    source = Path("config/research_samples/hostility-development-100.json")
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["turns"][0]["turn_key"] = "mutated"
    mutated = tmp_path / "mutated.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256"):
        load_frozen_sample(mutated)
    with pytest.raises(ValueError, match="missing turn keys"):
        map_frozen_turn_ids(["one", "missing"], [{"id": 1, "turn_key": "one"}])


def test_frozen_schema_rejects_duplicate_ids() -> None:
    source = Path("config/research_samples/hostility-development-100.json")
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["turns"][1]["turn_key"] = payload["turns"][0]["turn_key"]

    with pytest.raises(ValueError, match="duplicate turn keys"):
        FrozenSample.model_validate(payload)
