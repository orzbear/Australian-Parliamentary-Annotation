from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from hansard_annotator.llm_eval.import_predictions import load_prediction_artifact
from hansard_annotator.llm_eval.operations import (
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_MODEL,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
)
from hansard_annotator.web.annotations.ai_review import (
    _ai_review_queue_query,
    _domain_display,
)
from hansard_annotator.web.tasks.routes import (
    _accepted_ai_payload,
    _prediction_decision_status,
    _review_workspace_or_http,
)

ROOT = Path(__file__).parents[1]


def _write_artifact(directory: Path, *, record_id: str = "turn:stable-key") -> None:
    directory.mkdir()
    summary = {
        "provenance": {
            "provider": "gemini",
            "model": FROZEN_MODEL,
            "prompt_version": FROZEN_PROMPT_VERSION,
            "assembled_prompt_sha256": FROZEN_PROMPT_SHA256,
            "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
            "thinking_budget": 0,
            "execution_mode": "batch",
        },
        "coverage": {
            "accounting_invariant_ok": True,
            "invalid_structured_predictions": 0,
            "valid_structured_predictions": 1,
        },
    }
    prediction = {
        "record_id": record_id,
        "status": "success",
        "prediction": {
            "is_non_policy": False,
            "primary_australian_domain": "AU06",
            "secondary_australian_domains": ["AU02"],
            "reasoning": "Transport policy.",
        },
    }
    (directory / "operational_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (directory / "predictions.jsonl").write_text(json.dumps(prediction) + "\n", encoding="utf-8")


def test_prediction_artifact_validation_and_stable_hash(tmp_path: Path) -> None:
    artifact = tmp_path / "valid"
    _write_artifact(artifact)
    _, rows, first_hash = load_prediction_artifact(artifact)
    assert rows[0]["record_id"] == "turn:stable-key"
    assert load_prediction_artifact(artifact)[2] == first_hash


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("provenance", "assembled_prompt_sha256", "wrong"), "provenance mismatch"),
        (("coverage", "invalid_structured_predictions", 1), "not importable"),
    ],
)
def test_prediction_artifact_rejects_inconsistent_metadata(
    tmp_path: Path, mutation: tuple[str, str, Any], message: str
) -> None:
    artifact = tmp_path / "artifact"
    _write_artifact(artifact)
    summary_path = artifact / "operational_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    section, key, value = mutation
    summary[section][key] = value
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_prediction_artifact(artifact)


def test_invalid_500_pilot_is_explicitly_blocked(tmp_path: Path) -> None:
    artifact = tmp_path / "batch_pilot_500_results"
    artifact.mkdir()
    with pytest.raises(ValueError, match="explicitly prohibited"):
        load_prediction_artifact(artifact)


def test_accept_ai_copies_only_prediction_fields_into_human_payload() -> None:
    metadata: dict[str, object] = {
        "fields": [
            {"field_key": "is_non_policy", "field_type": "boolean"},
            {"field_key": "primary_australian_domain", "field_type": "single_taxonomy"},
            {"field_key": "secondary_australian_domains", "field_type": "multiple_taxonomy"},
            {"field_key": "annotation_notes", "field_type": "long_text"},
        ],
        "ai_prediction": {
            "is_non_policy": False,
            "primary_australian_domain": "AU06",
            "secondary_australian_domains": ["AU02"],
            "reasoning": "A model explanation.",
            "raw_provider_response": {"secret": "must not propagate"},
        },
    }
    payload = _accepted_ai_payload(metadata)
    assert payload == {
        "is_non_policy": False,
        "primary_australian_domain": "AU06",
        "secondary_australian_domains": ["AU02"],
        "annotation_notes": None,
    }


def test_workspace_distinguishes_ai_and_human_and_hides_raw_payload() -> None:
    workspace = (ROOT / "src/hansard_annotator/web/templates/annotations/workspace.html").read_text(
        encoding="utf-8"
    )
    assert "AI suggestion · not yet human-approved" in workspace
    assert "Accept AI &amp; next" in workspace
    assert "Save edited annotation &amp; next" in workspace
    assert "raw_provider_response" not in workspace


def test_unreviewed_queue_is_prediction_based_and_requires_zero_review_events() -> None:
    query, parameters = _ai_review_queue_query(42, review_filter="unreviewed")
    assert "FROM model_predictions mp" in query
    assert "latest_review.review_status IS NULL" in query
    assert "FROM assignments a" not in query
    assert "LEFT JOIN LATERAL" in query
    assert parameters == (42,)


@pytest.mark.parametrize("status", ["accepted", "edited", "skipped"])
def test_effective_status_queues_exclude_unreviewed_and_select_requested_state(
    status: str,
) -> None:
    query, parameters = _ai_review_queue_query(42, review_filter=status)
    assert "latest_review.review_status=%s" in query
    assert "latest_review.review_status IS NULL" not in query
    assert parameters == (42, status)


def test_codebook_flag_queue_is_orthogonal_to_latest_decision_state() -> None:
    query, parameters = _ai_review_queue_query(42, review_filter="flagged_codebook")
    assert "EXISTS (SELECT 1 FROM model_prediction_reviews flag" in query
    assert "flag.review_status='flagged_codebook'" in query
    assert "latest_review.review_status=%s" not in query
    assert parameters == (42,)


def test_reviewed_queue_contains_any_effective_review_state() -> None:
    query, parameters = _ai_review_queue_query(42, review_filter="reviewed")
    assert "latest_review.review_status IS NOT NULL" in query
    assert parameters == (42,)


def test_latest_review_lateral_join_returns_at_most_one_row_per_prediction() -> None:
    query, _ = _ai_review_queue_query(42, review_filter="reviewed")
    assert "ORDER BY reviewed_at DESC,id DESC LIMIT 1" in query
    assert query.count("FROM model_predictions mp") == 1


def test_queue_filters_remain_bound_and_composable() -> None:
    query, parameters = _ai_review_queue_query(
        42,
        review_filter="unreviewed",
        run_id="run-id",
        primary_domain="AU06",
        is_non_policy=False,
    )
    assert "mpr.public_id::text=%s" in query
    assert "mp.primary_australian_domain=%s" in query
    assert "mp.is_non_policy=%s" in query
    assert parameters == (42, "run-id", "AU06", False)


def test_one_hundred_zero_review_predictions_are_not_limited_below_one_hundred() -> None:
    query, _ = _ai_review_queue_query(42, review_filter="unreviewed")
    simulated_predictions = [{"prediction_id": number} for number in range(100)]
    assert len(simulated_predictions) == 100
    assert "LIMIT 500" in query


def test_prediction_decision_detects_accept_and_edit_without_mutating_ai() -> None:
    suggested = {
        "is_non_policy": False,
        "primary_australian_domain": "AU07",
        "secondary_australian_domains": ["AU08"],
    }
    assert _prediction_decision_status(dict(suggested), suggested) == "accepted"
    edited = {**suggested, "primary_australian_domain": "AU08"}
    assert _prediction_decision_status(edited, suggested) == "edited"
    assert suggested["primary_australian_domain"] == "AU07"


def test_ai_queue_has_obvious_prediction_links_and_review_action() -> None:
    queue = (ROOT / "src/hansard_annotator/web/templates/annotations/ai_queue.html").read_text(
        encoding="utf-8"
    )
    assert queue.count("/ai-reviews/{{ row.prediction_id }}") >= 2
    assert ">Review</a>" in queue
    assert "row.secondary_domain_displays" in queue
    assert "row.reasoning" not in queue


def test_domain_codes_are_displayed_with_human_readable_category_names() -> None:
    labels = {"AU04": "Education, science, technology and communications"}
    assert _domain_display("AU04", labels) == (
        "AU04 — Education, science, technology and communications"
    )
    assert _domain_display(None, labels) == "None"
    queue = (ROOT / "src/hansard_annotator/web/templates/annotations/ai_queue.html").read_text(
        encoding="utf-8"
    )
    workspace = (
        ROOT / "src/hansard_annotator/web/templates/annotations/ai_workspace.html"
    ).read_text(encoding="utf-8")
    assert "row.primary_domain_display" in queue
    assert "prediction.primary_domain_display" in workspace
    assert "prediction.secondary_domain_displays" in workspace


def test_individual_workspace_renders_prediction_actions_and_comparison() -> None:
    workspace = (
        ROOT / "src/hansard_annotator/web/templates/annotations/ai_workspace.html"
    ).read_text(encoding="utf-8")
    for text in (
        "AI suggestion — not yet human approved",
        "prediction.text_clean",
        "prediction.reasoning",
        "Accept AI &amp; next",
        "Save edited annotation &amp; next",
        "Flag codebook issue",
        "Skip &amp; next",
        "Back to queue",
        "AI versus human",
        "Codebook issue recorded",
    ):
        assert text in workspace
    assert "raw_provider_response" not in workspace


def test_correction_controls_are_opt_in_and_secondary_actions_precede_accept() -> None:
    workspace = (
        ROOT / "src/hansard_annotator/web/templates/annotations/ai_workspace.html"
    ).read_text(encoding="utf-8")
    javascript = (
        ROOT / "src/hansard_annotator/web/static/src/app.js"
    ).read_text(encoding="utf-8")
    assert "I want to correct the annotation" in workspace
    assert "data-correction-toggle" in workspace
    assert "data-correction-form" in workspace
    assert "data-correction-form" in workspace
    assert workspace.index("Codebook issue note") < workspace.index("Accept AI &amp; next")
    assert workspace.index("Skip &amp; next") < workspace.index("Accept AI &amp; next")
    assert workspace.index("Accept AI &amp; next") < workspace.index(
        "Save edited annotation &amp; next"
    )
    assert "correctionForm.hidden = !correctionToggle.checked" in javascript
    css = (ROOT / "src/hansard_annotator/web/static/src/app.css").read_text(
        encoding="utf-8"
    )
    assert ".correction-toggle-input:not(:checked) ~ [data-correction-form]" in css
    base = (ROOT / "src/hansard_annotator/web/templates/base.html").read_text(
        encoding="utf-8"
    )
    assert "app.css?v=20261011-ai-correction-2" in base
    assert "app.js?v=20261011-ai-correction-2" in base


def test_dashboard_and_navigation_make_ai_review_first_class() -> None:
    dashboard = (ROOT / "src/hansard_annotator/web/templates/dashboard/index.html").read_text(
        encoding="utf-8"
    )
    base = (ROOT / "src/hansard_annotator/web/templates/base.html").read_text(encoding="utf-8")
    assert "Continue AI review" in dashboard
    for key in ("predictions", "unreviewed", "accepted", "edited", "flagged_codebook", "skipped"):
        assert key in dashboard
    assert ">AI Reviews</a>" in base


def test_routes_include_individual_actions_and_next_redirect() -> None:
    routes = (ROOT / "src/hansard_annotator/web/tasks/routes.py").read_text(encoding="utf-8")
    for suffix in ('"/ai-reviews/{prediction_id}"', "/accept", "/edit", "/flag", "/skip"):
        assert suffix in routes
    assert "_next_review_destination" in routes
    service = (ROOT / "src/hansard_annotator/web/annotations/ai_review.py").read_text(
        encoding="utf-8"
    )
    assert "UPDATE model_predictions" not in service


def test_individual_review_converts_unauthorized_project_to_http_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise PermissionError("no active compatible policy-annotation project is available")

    monkeypatch.setattr("hansard_annotator.web.tasks.routes.prediction_review_workspace", denied)
    with pytest.raises(HTTPException) as raised:
        _review_workspace_or_http(None, None, "prediction-id")  # type: ignore[arg-type]
    assert raised.value.status_code == 403
