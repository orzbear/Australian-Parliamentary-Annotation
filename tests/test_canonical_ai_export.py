from __future__ import annotations

import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from hansard_annotator.web.campaigns.service import production_configuration
from hansard_annotator.web.research_exports.canonical_ai import (
    FIELDS,
    OUTCOME_NO_USABLE_TEXT,
    OUTCOME_PREDICTION,
    OUTCOME_PROVIDER_REFUSAL,
    PARQUET_SCHEMA,
    CanonicalAIExportError,
    _canonical_query,
    _configuration_parameters,
    _csv_row,
    _output_row,
    _write_deterministic_zip,
)


def _raw(**overrides: object) -> dict[str, object]:
    configuration = production_configuration()
    value: dict[str, object] = {
        "corpus_public_id": "00000000-0000-0000-0000-000000000001",
        "corpus_slug": "house-hansard",
        "corpus_name": "House Hansard",
        "turn_id": 1,
        "turn_key": "stable-turn",
        "speech_date": date(2026, 1, 1),
        "chamber": "House of Representatives",
        "source_file": "2026/2026-01-01.xml",
        "source_url": "https://example.test/turn",
        "turn_sequence": 1,
        "speaker_id_raw": "speaker-1",
        "speaker_name_raw": "Example Speaker",
        "major_heading_original": "Debate",
        "minor_heading_original": "Matter",
        "business_type": "Debate",
        "calculated_word_count": 2,
        "warning_codes": [],
        "text_clean": "Useful speech",
        "text_raw": "Useful speech",
        "usable_text": True,
        "prediction_public_id": None,
        "prediction_match_count": None,
        "abstention_public_id": None,
        "abstention_match_count": None,
        "provider": configuration["provider"],
        "configured_model": configuration["configured_model"],
        "run_returned_model": configuration["configured_model"],
        "prompt_version": configuration["prompt_version"],
        "prompt_sha256": configuration["prompt_sha256"],
        "canonical_schema_sha256": configuration["canonical_schema_sha256"],
        "prediction_contract_sha256": configuration["prediction_contract_sha256"],
        "provider_schema_sha256": configuration["provider_schema_sha256"],
        "generation_config": dict(configuration["generation_config"]),
        "execution_mode": configuration["execution_mode"],
        "latest_flagged_at": None,
    }
    value.update(overrides)
    return value


def test_canonical_query_is_corpus_first_and_exactly_configuration_pinned() -> None:
    query = _canonical_query()
    parameters = _configuration_parameters()
    configuration = production_configuration()
    assert list(PARQUET_SCHEMA.names) == FIELDS
    assert "FROM speaker_turns turn" in query
    assert "LEFT JOIN matching_predictions" in query
    assert "LEFT JOIN matching_abstentions" in query
    assert "generation_config=%s" in query
    assert "execution_mode=%s AND status='imported'" in query
    assert 'ORDER BY turn.speech_date,source.relative_path COLLATE "C"' in query
    assert parameters == (
        "gemini",
        "gemini-3.1-flash-lite",
        "phase4a-policy-v2",
        "a41027b001f10b61074c196996606c86b5dac4c0788ae185a05ff68fcf3b7cc6",
        "9c94bab9cd2976559c79b1c31991aef4138a2071a5d3b3d6186a26448f117126",
        "99cafa6c473293dc076793ee6baeca44b6961d7cb11de8f9174517defc4a0879",
        configuration["provider_schema_sha256"],
        parameters[7],
        "batch",
    )
    assert isinstance(parameters[7], Jsonb)
    assert parameters[7].obj == {"thinking_level": "minimal"}


def test_outcomes_never_synthesize_abstention_or_no_text_classification() -> None:
    reviewed_at = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    prediction = _output_row(
        _raw(
            prediction_public_id="00000000-0000-0000-0000-000000000010",
            prediction_match_count=1,
            is_non_policy=False,
            primary_australian_domain="AU07",
            secondary_australian_domains=["AU08"],
            reasoning="An immutable model explanation.",
            prediction_artifact_reference="data/predictions/batch-1/predictions.jsonl#L1",
            latest_review_public_id="00000000-0000-0000-0000-000000000099",
            latest_review_status="edited",
            latest_reviewed_at=reviewed_at,
            latest_review_annotation_version_id=42,
            latest_review_note="Human reviewer changed domain to AU12.",
            latest_flag_note="Check boundary with defence.",
            latest_flagged_at=reviewed_at,
        ),
        1,
    )
    assert prediction["outcome_status"] == OUTCOME_PREDICTION
    assert prediction["is_non_policy"] is False
    assert prediction["primary_australian_domain"] == "AU07"
    assert prediction["secondary_australian_domains"] == ["AU08"]
    assert prediction["reasoning"] == "An immutable model explanation."
    assert prediction["thinking_level"] == "minimal"
    assert prediction["provider_block_reason"] is None
    assert prediction["refusal_provenance_json"] is None
    assert prediction["latest_review_status"] == "edited"
    assert prediction["review_edited"] is True
    assert prediction["latest_review_annotation_version_id"] == 42
    assert prediction["latest_review_note"] == "Human reviewer changed domain to AU12."
    assert prediction["has_codebook_flag"] is True
    assert prediction["latest_codebook_flag_note"] == "Check boundary with defence."

    refusal_payload = {
        "resolution": "PROVIDER_REFUSAL",
        "reason": "PROHIBITED_CONTENT",
        "attempts": [{"provider_response": {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}}],
    }
    refusal = _output_row(
        _raw(
            abstention_public_id="00000000-0000-0000-0000-000000000020",
            abstention_match_count=1,
            resolution_type="PROVIDER_REFUSAL",
            resolution_reason="PROHIBITED_CONTENT",
            refusal_provenance=refusal_payload,
            abstention_artifact_reference="data/campaigns/c1/provider_refusals.jsonl",
            is_non_policy=False,
            primary_australian_domain="AU01",
            secondary_australian_domains=["AU02"],
            reasoning="Must not leak.",
        ),
        2,
    )
    assert refusal["outcome_status"] == OUTCOME_PROVIDER_REFUSAL
    assert refusal["is_non_policy"] is None
    assert refusal["primary_australian_domain"] is None
    assert refusal["secondary_australian_domains"] is None
    assert refusal["reasoning"] is None
    assert refusal["provider_block_reason"] == "PROHIBITED_CONTENT"
    assert json.loads(refusal["refusal_provenance_json"]) == refusal_payload
    assert refusal["artifact_reference"] == "data/campaigns/c1/provider_refusals.jsonl"
    assert refusal["thinking_level"] == "minimal"
    assert refusal["review_edited"] is False

    no_text = _output_row(
        _raw(
            text_clean="",
            text_raw="   ",
            usable_text=False,
            provider=None,
            configured_model=None,
            run_returned_model=None,
            prompt_version=None,
            prompt_sha256=None,
            canonical_schema_sha256=None,
            prediction_contract_sha256=None,
            provider_schema_sha256=None,
            generation_config=None,
            execution_mode=None,
        ),
        3,
    )
    assert no_text["outcome_status"] == OUTCOME_NO_USABLE_TEXT
    assert no_text["is_non_policy"] is None
    assert no_text["primary_australian_domain"] is None
    assert no_text["secondary_australian_domains"] is None
    assert no_text["reasoning"] is None
    assert no_text["prediction_run_id"] is None
    assert no_text["provider"] is None
    assert no_text["thinking_level"] is None
    assert no_text["provider_block_reason"] is None
    assert no_text["refusal_provenance_json"] is None


@pytest.mark.parametrize(
    "row,message",
    [
        (_raw(), "no production outcome"),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=2,
            ),
            "multiple production predictions",
        ),
        (
            _raw(
                abstention_public_id="refusal",
                abstention_match_count=2,
                resolution_type="PROVIDER_REFUSAL",
                resolution_reason="PROHIBITED_CONTENT",
            ),
            "multiple production refusals",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                abstention_public_id="refusal",
                abstention_match_count=1,
            ),
            "prediction/refusal conflict",
        ),
        (
            _raw(
                text_clean="",
                text_raw="",
                usable_text=False,
                prediction_public_id="prediction",
                prediction_match_count=1,
            ),
            "no-text turn has a model outcome",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                configured_model="gemini-2.5-flash",
            ),
            "non-canonical configured_model",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                prompt_version="phase4a-policy-v1",
            ),
            "non-canonical prompt_version",
        ),
        (
            _raw(
                prediction_public_id="prediction",
                prediction_match_count=1,
                generation_config={"thinking_budget": 0},
            ),
            "non-canonical generation_config",
        ),
    ],
)
def test_incomplete_duplicate_or_conflicting_layers_fail_closed(
    row: dict[str, object], message: str
) -> None:
    with pytest.raises(CanonicalAIExportError, match=message):
        _output_row(row, 1)


def test_completed_corpus_accounting_shape_reconciles_160577_turns() -> None:
    total_turns = 160_577
    expected_predictions = 160_567
    expected_refusals = 8
    expected_no_usable_text = 2

    counts = {
        OUTCOME_PREDICTION: 0,
        OUTCOME_PROVIDER_REFUSAL: 0,
        OUTCOME_NO_USABLE_TEXT: 0,
    }
    pred_template = _raw(
        prediction_public_id="00000000-0000-0000-0000-000000000010",
        prediction_match_count=1,
        is_non_policy=False,
        primary_australian_domain="AU01",
        secondary_australian_domains=[],
        reasoning="Policy turn.",
    )
    refusal_template = _raw(
        abstention_public_id="00000000-0000-0000-0000-000000000020",
        abstention_match_count=1,
        resolution_type="PROVIDER_REFUSAL",
        resolution_reason="PROHIBITED_CONTENT",
        refusal_provenance={"resolution": "PROVIDER_REFUSAL", "reason": "PROHIBITED_CONTENT"},
    )
    no_text_template = _raw(
        text_clean="",
        text_raw="",
        usable_text=False,
        provider=None,
        configured_model=None,
        run_returned_model=None,
        prompt_version=None,
        prompt_sha256=None,
        canonical_schema_sha256=None,
        prediction_contract_sha256=None,
        provider_schema_sha256=None,
        generation_config=None,
        execution_mode=None,
    )

    for order in range(1, total_turns + 1):
        if order <= expected_predictions:
            raw = {**pred_template, "turn_id": order, "turn_key": f"turn-{order:06d}"}
        elif order <= expected_predictions + expected_refusals:
            raw = {**refusal_template, "turn_id": order, "turn_key": f"turn-{order:06d}"}
        else:
            raw = {**no_text_template, "turn_id": order, "turn_key": f"turn-{order:06d}"}
        out = _output_row(raw, order)
        counts[str(out["outcome_status"])] += 1
        if order in (1, expected_predictions + 1, total_turns):
            rendered = _csv_row(out)
            assert set(rendered.keys()) == set(FIELDS)

    assert counts == {
        OUTCOME_PREDICTION: expected_predictions,
        OUTCOME_PROVIDER_REFUSAL: expected_refusals,
        OUTCOME_NO_USABLE_TEXT: expected_no_usable_text,
    }
    assert sum(counts.values()) == total_turns


def test_zip_bytes_are_deterministic_for_identical_files(tmp_path: Path) -> None:
    first = tmp_path / "a.csv"
    second = tmp_path / "manifest.json"
    first.write_bytes(b"header\nvalue\n")
    second.write_bytes(b'{"generated_at":"fixed"}\n')
    one = tmp_path / "one.zip"
    two = tmp_path / "two.zip"
    _write_deterministic_zip(one, [second, first])
    _write_deterministic_zip(two, [first, second])
    assert one.read_bytes() == two.read_bytes()
    with zipfile.ZipFile(one) as archive:
        assert archive.namelist() == ["a.csv", "manifest.json"]


def test_research_export_ui_has_separate_canonical_action() -> None:
    template = Path(
        "src/hansard_annotator/web/templates/exports/index.html"
    ).read_text(encoding="utf-8")
    assert "Canonical Full-Corpus AI Export" in template
    assert "/exports/canonical-ai/{{ corpus.public_id }}.zip" in template
    assert "never replaces the immutable AI result" in template

