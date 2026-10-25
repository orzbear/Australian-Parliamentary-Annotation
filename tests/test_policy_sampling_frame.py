from __future__ import annotations

import csv
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from scripts.build_policy_sampling_frame import (
    DOMAINS,
    FRAME_SCHEMA,
    SOURCE_COLUMNS,
    build_frame,
    discover_source,
    join_counts,
    normalize_policy,
    population_rows,
    require_local_database,
)


def _write_source(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=SOURCE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _row(
    order: int,
    key: str,
    *,
    outcome: str = "PREDICTION",
    is_non_policy: str = "false",
    domain: str = "AU01",
) -> dict[str, str]:
    return {
        "corpus_order": str(order),
        "turn_key": key,
        "outcome_status": outcome,
        "is_non_policy": is_non_policy,
        "primary_australian_domain": domain,
        "campaign_id": "campaign-1",
        "provider": "provider",
        "configured_model": "model",
        "prompt_version": "v1",
        "prediction_run_id": "run-1",
    }


def test_builds_compact_deterministic_frame_and_counts_domains(tmp_path: Path) -> None:
    source = tmp_path / "canonical.csv"
    _write_source(
        source,
        [
            _row(1, "turn-1"),
            _row(2, "turn-2", is_non_policy="true", domain=""),
            _row(
                3,
                "turn-3",
                outcome="PROVIDER_REFUSAL",
                is_non_policy="",
                domain="",
            ),
        ],
    )
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"

    statistics, outcomes, domains = build_frame(source, first)
    build_frame(source, second)

    assert first.read_bytes() == second.read_bytes()
    assert statistics.source_rows == statistics.output_rows == 3
    assert statistics.usable_classified_turns == 2
    assert statistics.policy_turns == 1
    assert statistics.non_policy_turns == 1
    assert statistics.unavailable_turns == 1
    assert outcomes == {"PREDICTION": 2, "PROVIDER_REFUSAL": 1}
    assert domains == {"AU01": 1}
    table = pq.read_table(first)
    assert table.schema == FRAME_SCHEMA
    assert table.column_names == FRAME_SCHEMA.names
    assert table["policy_status"].to_pylist() == ["POLICY", "NON_POLICY", "UNAVAILABLE"]

    population = population_rows(domains)
    assert [row["primary_domain"] for row in population[:-1]] == list(DOMAINS)
    assert population[-1]["eligible_policy_speeches"] == 1


def test_streams_quoted_multiline_values(tmp_path: Path) -> None:
    source = tmp_path / "canonical.csv"
    row = _row(1, "turn-1")
    row["configured_model"] = "model\nvariant"
    _write_source(source, [row])

    output = tmp_path / "frame.parquet"
    statistics, _, _ = build_frame(source, output)

    assert statistics.output_rows == 1
    assert pq.read_table(output)["source_model"].to_pylist() == ["model\nvariant"]


def test_rejects_duplicate_turn_keys_without_leaving_output(tmp_path: Path) -> None:
    source = tmp_path / "canonical.csv"
    _write_source(source, [_row(1, "duplicate"), _row(2, "duplicate")])
    output = tmp_path / "frame.parquet"

    with pytest.raises(ValueError, match="duplicates=1"):
        build_frame(source, output)

    assert not output.exists()
    assert not output.with_suffix(".parquet.part").exists()


@pytest.mark.parametrize(
    ("outcome", "is_non_policy", "domain", "message"),
    [
        ("PREDICTION", "false", "", "lacks valid"),
        ("PREDICTION", "true", "AU01", "non-policy row"),
        ("PROVIDER_REFUSAL", "", "AU02", "unexpectedly carries"),
        ("SURPRISE", "", "", "unexpected prediction outcome"),
    ],
)
def test_normalization_fails_loudly_on_invalid_rows(
    outcome: str, is_non_policy: str, domain: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_policy(outcome, is_non_policy, domain)


def test_discovers_only_the_canonical_shape(tmp_path: Path) -> None:
    expected = tmp_path / "canonical.csv"
    _write_source(expected, [_row(1, "turn-1")])
    (tmp_path / "unrelated.csv").write_text("something_else\nvalue\n", encoding="utf-8")

    assert discover_source(tmp_path) == expected


def test_database_guard_and_join_accounting() -> None:
    require_local_database("hansard_dev")
    with pytest.raises(RuntimeError, match="refusing database access"):
        require_local_database("production")

    assert join_counts({"a", "b"}, {"b", "c"}) == {
        "compact_frame_rows": 2,
        "matched_speaker_turns": 1,
        "unmatched_frame_rows": 1,
        "speaker_turns_without_frame": 1,
        "duplicate_joins": 0,
    }
