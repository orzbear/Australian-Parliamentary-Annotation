from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hansard_annotator.web.research_exports.canonical_hostility import (
    CanonicalHostilityExportError,
    _configuration_parameters,
    _query,
    export_hostility_rows,
    make_row,
)


def raw_row(**updates: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "corpus_public_id": "corpus-1", "corpus_slug": "house", "corpus_name": "House",
        "turn_id": 1, "turn_key": "turn:1", "speech_date": date(2026, 1, 2), "chamber": "House",
        "source_file": "source.xml", "source_url": None, "turn_sequence": 1,
        "speaker_id": "s1", "speaker_name": "Speaker", "major_heading": "Debate",
        "minor_heading": "", "business_type": "Debate", "calculated_word_count": 2,
        "warning_codes": [], "text_clean": "Speech text", "text_raw": None,
        "usable_text": True, "latest_policy_is_non_policy": False,
        "prediction_match_count": 1, "prediction_public_id": "prediction-1",
        "prediction_values": {"hostility_present": "YES", "target": "OPPOSING_PARTISAN_TARGET",
                              "evidence_spans": [{"start": 0, "end": 6, "text": "Speech"}]},
        "reasoning": "stored reasoning", "terminal_match_count": 0, "terminal_public_id": None,
        "terminal_failure_category": None, "terminal_validation_message": None,
        "terminal_outcome_type": None,
        "terminal_frozen_classification": None, "terminal_model_evidence": None,
        "terminal_recovery_attempt_history": None,
        "run_public_id": "run-1", "provider": "gemini", "configured_model": "gemini-2.5-flash",
        "returned_model": "gemini-2.5-flash", "prediction_returned_model": "gemini-2.5-flash",
        "prompt_version": "hostility-prompt-v2",
        "annotation_schema_slug": "australian_partisan_hostility", "annotation_schema_version": "0.2.0",
        "prompt_sha256": "a" * 64, "canonical_schema_sha256": "b" * 64,
        "prediction_contract_sha256": "c" * 64, "provider_schema_sha256": "d" * 64,
        "generation_config": {"thinking_budget": 0}, "execution_mode": "batch",
        "run_artifact_sha256": "e" * 64, "run_record_set_sha256": "f" * 64,
        "provider_job_id": "jobs/1", "run_imported_at": None,
        "campaign_public_id": "campaign-1", "campaign_name": "Pilot", "campaign_status": "IMPORTED",
        "campaign_record_set_sha256": "1" * 64, "campaign_artifact_sha256": "2" * 64,
        "recovery_lineage": [], "chunk_lineage": [], "human_review_status": None,
        "human_resolution_method": None, "human_reviewer_id": None, "human_reviewed_at": None,
        "human_annotation_version_id": None, "human_values": None, "human_review_note": None,
    }
    row.update(updates)
    return row


def test_yes_no_unsure_and_partisan_derivation() -> None:
    yes = make_row(raw_row(), 1)
    no = make_row(raw_row(prediction_values={"hostility_present": "NO", "target": None,
                                             "evidence_spans": []}), 2)
    unsure = make_row(raw_row(prediction_values={"hostility_present": "UNSURE", "target": "UNSURE",
                                                 "evidence_spans": []}), 3)
    assert yes["partisan_hostility"] is True
    assert yes["evidence_spans"] == '[{"end":6,"start":0,"text":"Speech"}]'
    assert no["partisan_hostility"] is False
    assert no["hostility_present"] == "NO"
    assert unsure["partisan_hostility"] is False


def test_missing_invalid_and_ineligible_are_distinct_not_no() -> None:
    missing = make_row(raw_row(prediction_match_count=0, prediction_values=None,
                               prediction_public_id=None, reasoning=None,
                               run_public_id=None, provider=None, configured_model=None,
                               returned_model=None, prompt_version=None, prompt_sha256=None,
                               canonical_schema_sha256=None, prediction_contract_sha256=None,
                               provider_schema_sha256=None, generation_config=None,
                               execution_mode=None, run_artifact_sha256=None,
                               run_record_set_sha256=None, provider_job_id=None,
                               run_imported_at=None, terminal_match_count=0,
                               campaign_public_id=None, campaign_name=None, campaign_status=None,
                               campaign_record_set_sha256=None, campaign_artifact_sha256=None), 1)
    invalid = make_row(raw_row(prediction_match_count=0, prediction_values=None,
                               prediction_public_id=None, reasoning=None, terminal_match_count=1,
                               terminal_public_id="terminal-1", terminal_failure_category="INVALID_STRUCTURED_OUTPUT",
                               terminal_validation_message="JSON truncated", campaign_public_id=None), 2)
    assert missing["ai_result_status"] == "NO_AI_PREDICTION"
    assert missing["hostility_present"] is None
    assert invalid["ai_result_status"] == "AI_TERMINAL_INVALID"
    assert invalid["hostility_present"] is None
    assert invalid["terminal_validation_message"] == "JSON truncated"


def test_duplicate_or_overlapping_campaign_outcomes_fail() -> None:
    with pytest.raises(CanonicalHostilityExportError, match="overlapping"):
        make_row(raw_row(prediction_match_count=2), 1)
    with pytest.raises(CanonicalHostilityExportError, match="predictions=1, terminal=1"):
        make_row(raw_row(terminal_match_count=1), 1)


def test_export_query_pins_all_frozen_fields_and_reports_overlap_counts() -> None:
    query = _query()
    assert "prediction_task='hostility' AND status='imported'" in query
    for field in (
        "provider", "configured_model", "prompt_version", "prompt_sha256",
        "canonical_schema_sha256", "prediction_contract_sha256",
        "provider_schema_sha256", "generation_config", "execution_mode",
    ):
        assert field in query
    parameters = _configuration_parameters()
    assert parameters[0] == "gemini"
    assert parameters[1] == "gemini-2.5-flash"
    assert parameters[-1] == "batch"
    assert "count(*) OVER() AS prediction_count" in query
    assert "count(*) OVER() AS outcome_count" in query


def _projection_names(select_list: str) -> set[str]:
    expressions: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(select_list):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            expressions.append(select_list[start:index])
            start = index + 1
    expressions.append(select_list[start:])

    names: set[str] = set()
    for expression in expressions:
        alias = re.search(r"\bAS\s+([a-z_][a-z0-9_]*)\s*$", expression, re.IGNORECASE)
        if alias:
            names.add(alias.group(1).lower())
        else:
            direct_column = re.search(r"\.([a-z_][a-z0-9_]*)\s*$", expression.strip())
            if direct_column:
                names.add(direct_column.group(1).lower())
    return names


def test_export_cte_projections_match_outer_references_and_migration_schema() -> None:
    query = _query()
    outer_select = query[
        query.index("SELECT corpus.public_id") : query.index("FROM corpora corpus")
    ]
    projections = {
        "prediction": (
            query[
                query.index("SELECT count(*) OVER() AS prediction_count") : query.index(
                    "FROM model_predictions mp",
                    query.index("SELECT count(*) OVER() AS prediction_count"),
                )
            ],
            "prediction_count",
        ),
        "terminal": (
            query[
                query.index("SELECT count(*) OVER() AS outcome_count") : query.index(
                    "FROM model_prediction_abstentions outcome"
                )
            ],
            "outcome_count",
        ),
    }
    for alias, (projection, _) in projections.items():
        select_index = projection.upper().index("SELECT")
        select_list = projection[select_index + len("SELECT") :]
        projected = _projection_names(select_list)
        referenced = {
            name.lower()
            for name in re.findall(rf"\b{alias}\.([a-z_][a-z0-9_]*)", outer_select, re.I)
        }
        assert referenced <= projected, f"{alias} references missing projection(s): {referenced - projected}"

    abstention_migration = Path(
        "migrations/versions/20261020_10_provider_abstentions.py"
    ).read_text(encoding="utf-8")
    outcome_migration = Path(
        "migrations/versions/20261027_13_terminal_prediction_outcomes.py"
    ).read_text(encoding="utf-8")
    table_definition = re.search(
        r"CREATE TABLE model_prediction_abstentions \((.*?)\n\s*\);",
        abstention_migration,
        re.DOTALL,
    )
    assert table_definition is not None
    schema_columns = set(
        re.findall(
            r"^\s+([a-z_][a-z0-9_]*)\s+(?:bigint|uuid|text|jsonb|timestamptz)\b",
            table_definition.group(1),
            re.MULTILINE | re.IGNORECASE,
        )
    )
    assert "resolution_type" in schema_columns
    assert "RENAME COLUMN refusal_provenance TO outcome_provenance" in outcome_migration
    schema_columns.discard("refusal_provenance")
    schema_columns.add("outcome_provenance")
    selected_outcome_columns = set(re.findall(r"\boutcome\.([a-z_][a-z0-9_]*)", query, re.I))
    assert selected_outcome_columns <= schema_columns
    terminal_projection, _ = projections["terminal"]
    assert "outcome.resolution_type" in terminal_projection


def test_terminal_provider_refusal_stays_distinct_and_not_a_hostility_no() -> None:
    row = make_row(raw_row(prediction_match_count=0, prediction_values=None,
                            prediction_public_id=None, reasoning=None, terminal_match_count=1,
                            terminal_outcome_type="PROVIDER_REFUSAL",
                            terminal_public_id="refusal-1", campaign_public_id=None), 1)
    assert row["ai_result_status"] == "AI_PROVIDER_REFUSAL"
    assert row["hostility_present"] is None


def test_human_resolution_remains_separate_from_ai_values() -> None:
    row = make_row(raw_row(terminal_match_count=1, terminal_public_id="terminal-1",
                            prediction_match_count=0, prediction_values=None,
                            prediction_public_id=None, reasoning=None,
                            human_review_status="human_annotated_after_ai_failure",
                            human_resolution_method="INDEPENDENT_HUMAN_CLASSIFICATION",
                            human_reviewer_id=4, human_annotation_version_id=8,
                            human_values={"hostility_present": "YES", "target": "OTHER_POLITICAL_TARGET"}), 1)
    assert row["hostility_present"] is None
    assert row["human_values_json"] == '{"hostility_present":"YES","target":"OTHER_POLITICAL_TARGET"}'


def test_csv_parquet_and_manifest_are_deterministic(tmp_path: Path) -> None:
    corpus = {"public_id": "corpus-1", "slug": "house", "name": "House",
              "turn_count": 1, "preprocessing_run_id": "prep-1"}
    first = export_hostility_rows(iter([raw_row()]), tmp_path / "one", corpus=corpus)
    second = export_hostility_rows(iter([raw_row()]), tmp_path / "two", corpus=corpus)
    assert first["files"] == second["files"]
    with (tmp_path / "one/canonical_hostility.csv").open(encoding="utf-8", newline="") as stream:
        data = next(csv.DictReader(stream))
    assert data["hostility_present"] == "YES"
    assert json.loads(data["evidence_spans"])[0]["text"] == "Speech"
    import pyarrow.parquet as pq

    table = pq.read_table(tmp_path / "one/canonical_hostility.parquet")
    assert table.column("hostility_present").to_pylist() == ["YES"]
    assert table.column("partisan_hostility").to_pylist() == [True]


def test_interrupted_stream_does_not_publish_partial_export(tmp_path: Path) -> None:
    def interrupted():
        yield raw_row()
        raise RuntimeError("interrupted")

    target = tmp_path / "partial"
    with pytest.raises(RuntimeError, match="interrupted"):
        export_hostility_rows(interrupted(), target,
                              corpus={"public_id": "c", "slug": "s", "name": "n",
                                      "turn_count": 2, "preprocessing_run_id": "p"})
    assert not target.exists()
