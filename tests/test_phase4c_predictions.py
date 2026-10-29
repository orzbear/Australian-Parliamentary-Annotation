from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape

from hansard_annotator.llm_eval.hostility import partisan_hostility
from hansard_annotator.llm_eval.import_predictions import (
    iter_terminal_invalid_artifact,
    load_prediction_artifact,
)
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_GENERATION,
    APPROVED_PRODUCTION_MODEL,
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_MODEL,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
)
from hansard_annotator.llm_eval.prediction_tasks import human_suggestion
from hansard_annotator.web.annotations.ai_review import (
    _ai_review_queue_query,
    _domain_display,
    ai_review_run,
    ai_review_runs,
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


def test_prediction_artifact_accepts_new_production_identity_without_rewriting_history(
    tmp_path: Path,
) -> None:
    historical = tmp_path / "historical"
    current = tmp_path / "current"
    _write_artifact(historical, record_id="turn:historical")
    _write_artifact(current, record_id="turn:current")

    current_summary_path = current / "operational_summary.json"
    current_summary = json.loads(current_summary_path.read_text(encoding="utf-8"))
    provenance = current_summary["provenance"]
    provenance["model"] = APPROVED_PRODUCTION_MODEL
    provenance.pop("thinking_budget")
    provenance.update(APPROVED_PRODUCTION_GENERATION)
    current_summary_path.write_text(json.dumps(current_summary), encoding="utf-8")

    historical_summary, _, _ = load_prediction_artifact(historical)
    current_loaded, _, _ = load_prediction_artifact(current)
    assert historical_summary["provenance"]["model"] == FROZEN_MODEL
    assert historical_summary["provenance"]["thinking_budget"] == 0
    assert current_loaded["provenance"]["model"] == APPROVED_PRODUCTION_MODEL
    assert current_loaded["provenance"]["thinking_level"] == "minimal"


def test_hostility_terminal_evidence_failure_is_not_a_prediction(tmp_path: Path) -> None:
    artifact = tmp_path / "hostility-terminal"
    _write_artifact(artifact)
    summary_path = artifact / "operational_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["coverage"].update(
        planned_requests=2,
        valid_structured_predictions=1,
        invalid_structured_predictions=1,
        provider_refused=0,
        failed_provider_responses=0,
        missing_responses=0,
        duplicate_mappings=0,
        accounting_invariant_ok=False,
    )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    invalid = {
        "record_id": "turn:invalid-key",
        "status": "invalid",
        "prediction": None,
        "validation_error": "hostility evidence must resolve uniquely to canonical text",
        "provider_response": {"responseId": "response-1"},
        "request_index": 1,
        "provider": "gemini",
        "configured_model": FROZEN_MODEL,
        "returned_model": FROZEN_MODEL,
        "prompt_version": FROZEN_PROMPT_VERSION,
        "prompt_sha256": FROZEN_PROMPT_SHA256,
        "schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
        "batch_id": "batches/production-shape",
        "response_index": 1,
        "usage": {"input_tokens": 10},
    }
    with (artifact / "predictions.jsonl").open("a", encoding="utf-8") as target:
        target.write(json.dumps(invalid) + "\n")
    expected = {
        "provider": "gemini",
        "prompt_version": FROZEN_PROMPT_VERSION,
        "assembled_prompt_sha256": FROZEN_PROMPT_SHA256,
        "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
        "execution_mode": "batch",
    }
    _summary, predictions, _digest = load_prediction_artifact(
        artifact,
        prediction_task="hostility",
        expected_configuration=expected,
    )
    terminal = list(iter_terminal_invalid_artifact(artifact, prediction_task="hostility"))
    assert [row["record_id"] for row in predictions] == ["turn:stable-key"]
    assert [row["record_id"] for row in terminal] == ["turn:invalid-key"]


def test_historical_hostility_invalid_row_uses_four_recovery_attempts(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "hostility-historical-terminal"
    _write_artifact(artifact)
    summary_path = artifact / "operational_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["coverage"].update(
        planned_requests=100,
        valid_structured_predictions=99,
        invalid_structured_predictions=1,
        provider_refused=0,
        failed_provider_responses=0,
        missing_responses=0,
        duplicate_mappings=0,
        accounting_invariant_ok=False,
    )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    valid_rows = [
        json.loads(line)
        for line in (artifact / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    valid_rows.extend(
        {**valid_rows[0], "record_id": f"turn:synthetic-{index}"}
        for index in range(2, 100)
    )
    invalid = {
        "record_id": "turn:historical-invalid",
        "status": "invalid",
        "prediction": None,
        "validation_error": "hostility evidence must resolve uniquely to canonical text",
        "provider_response": {"responseId": "historical-response"},
        "provider": "gemini",
        "configured_model": FROZEN_MODEL,
        "returned_model": FROZEN_MODEL,
        "prompt_version": FROZEN_PROMPT_VERSION,
        "prompt_sha256": FROZEN_PROMPT_SHA256,
        "schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
        "batch_id": "batches/historical",
        "request_index": 99,
        "response_index": 99,
        "usage": {"input_tokens": 10},
    }
    (artifact / "predictions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in [*valid_rows, invalid]),
        encoding="utf-8",
    )
    recovery_history = {
        "turn:historical-invalid": [
            {
                "attempt_number": attempt,
                "recovery_id": f"recovery-{attempt}",
                "provider_job_id": f"batches/recovery-{attempt}",
                "original_validation_error": "hostility evidence must resolve uniquely to canonical text",
                "replacement_validation_result": "invalid",
                "replacement_validation_error": "hostility evidence must resolve uniquely to canonical text",
            }
            for attempt in range(1, 5)
        ]
    }
    terminal = list(
        iter_terminal_invalid_artifact(
            artifact,
            prediction_task="hostility",
            terminal_recovery_history=recovery_history,
            require_recovery_lineage=True,
        )
    )
    assert len(terminal) == 1
    assert terminal[0]["prediction"] is None
    assert terminal[0]["attempt_count"] == 4
    assert terminal[0]["recovery_attempt_count"] == 4
    assert terminal[0]["recovery_attempt_number"] == 4
    assert terminal[0]["recovery_provider_job_id"] == "batches/recovery-4"
    assert terminal[0]["original_validation_error"] == invalid["validation_error"]
    assert terminal[0]["final_validation_error"] == invalid["validation_error"]
    assert "recovery_attempt_number" not in invalid


def test_terminal_invalid_campaign_requires_recoverable_lineage(tmp_path: Path) -> None:
    artifact = tmp_path / "hostility-lineage-required"
    _write_artifact(artifact)
    invalid = {
        "record_id": "turn:invalid-key",
        "status": "invalid",
        "prediction": None,
        "validation_error": "hostility evidence must resolve uniquely to canonical text",
        "provider_response": {"responseId": "response-1"},
    }
    with (artifact / "predictions.jsonl").open("a", encoding="utf-8") as target:
        target.write(json.dumps(invalid) + "\n")
    with pytest.raises(ValueError, match="no authoritative recovery provenance"):
        list(
            iter_terminal_invalid_artifact(
                artifact,
                prediction_task="hostility",
                require_recovery_lineage=True,
            )
        )


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


def test_human_workspace_is_separate_from_ai_review_controls() -> None:
    workspace = (
        ROOT / "src/hansard_annotator/web/templates/annotations/workspace.html"
    ).read_text(encoding="utf-8")
    assert "annotation-form" in workspace
    assert "Save draft" in workspace
    assert "Submit &amp; next" in workspace
    assert "Revision history" in workspace
    assert "expected_revision_number" in workspace
    assert "Flag codebook issue" not in workspace
    assert "Possible taxonomy/codebook ambiguity" not in workspace
    assert "Accept AI &amp; next" not in workspace
    assert "Machine provenance" not in workspace
    assert "raw_provider_response" not in workspace
    base = (ROOT / "src/hansard_annotator/web/templates/base.html").read_text(
        encoding="utf-8"
    )
    assert ">Codebook &amp; Taxonomy</a>" in base


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
        prediction_task="hostility",
        primary_domain="AU06",
        is_non_policy=False,
    )
    assert "mpr.public_id=%s::uuid" in query
    assert "mp.prediction_task=%s" in query
    assert "COALESCE(mp.primary_australian_domain,latest_policy.primary_australian_domain)=%s" in query
    assert "mp.is_non_policy=%s" in query
    assert "AU06" not in query
    assert parameters == (42, "run-id", "hostility", "AU06", False)


def test_hostility_queue_filters_are_bound_and_limit_outcome_queries() -> None:
    sample_keys = ["frozen-turn-1", "frozen-turn-2"]
    query, parameters = _ai_review_queue_query(
        42,
        review_filter="unreviewed",
        run_id="run-id",
        prediction_task="hostility",
        primary_domain="AU07",
        hostility_present="YES",
        hostility_target="OPPOSING_PARTISAN_TARGET",
        speech_year=2025,
        hostility_failure="evidence",
        sample_turn_keys=sample_keys,
        random_seed="c0ffee12",
    )
    assert "mp.prediction_values->>'hostility_present'=%s" in query
    assert "mp.prediction_values->>'target'=%s" in query
    assert "EXTRACT(YEAR FROM st.speech_date)=%s" in query
    assert "st.turn_key=ANY(%s)" in query
    assert "latest_policy.primary_australian_domain" in query
    assert "false" in query
    assert "ORDER BY md5(%s || mpr.public_id::text || mp.record_id)" in query
    assert parameters == (
        42,
        "run-id",
        "hostility",
        "AU07",
        "YES",
        "OPPOSING_PARTISAN_TARGET",
        2025,
        sample_keys,
        "c0ffee12",
    )


def test_one_hundred_zero_review_predictions_are_not_limited_below_one_hundred() -> None:
    query, _ = _ai_review_queue_query(42, review_filter="unreviewed")
    simulated_predictions = [{"prediction_id": number} for number in range(100)]
    assert len(simulated_predictions) == 100
    assert "LIMIT 101" in query


def test_review_run_summaries_use_one_lightweight_query_and_scope_selected_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    row = {
        "id": 7,
        "public_id": "run-public-id",
        "prediction_task": "hostility",
        "provider": "gemini",
        "configured_model": "model",
        "prompt_version": "hostility-prompt-v2",
        "imported_at": "2026-10-27",
        "campaign_name": "Hostility held-out 100",
        "campaign_status": "IMPORTED",
        "valid_count": 98,
        "terminal_invalid_count": 2,
        "prediction_count": 100,
        "reviewed_count": 10,
    }

    class _Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [row]

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, parameters: object = None) -> _Result:
            calls.append((query, parameters))
            return _Result()

    monkeypatch.setattr(
        "hansard_annotator.web.annotations.ai_review.psycopg.connect",
        lambda *_args, **_kwargs: _Connection(),
    )
    settings = SimpleNamespace(database=SimpleNamespace(psycopg_url="postgresql://local/test"))
    summaries = ai_review_runs(settings)  # type: ignore[arg-type]
    assert len(calls) == 1
    query, _parameters = calls[-1]
    assert "raw_provider_response" not in query
    assert "prediction_values" not in query
    assert "LEFT JOIN LATERAL" not in query
    assert summaries[0]["prediction_count"] == 100
    assert summaries[0]["reviewed_count"] == 10
    assert summaries[0]["remaining_count"] == 90
    assert summaries[0]["valid_count"] == 98
    assert summaries[0]["terminal_invalid_count"] == 2

    calls.clear()
    selected = ai_review_run(settings, "run-public-id")  # type: ignore[arg-type]
    assert selected is not None
    assert len(calls) == 1
    selected_query, parameters = calls[0]
    assert "WHERE run.public_id=%s::uuid" in selected_query
    assert parameters == ("run-public-id",)


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
    assert queue.count("/ai-review/{{ selected_run.public_id }}/{{ row.prediction_id }}") >= 2
    assert ">Review</a>" in queue
    assert "row.hostility_present" in queue
    assert "row.reasoning" not in queue


def test_ai_review_and_queue_render_without_inline_styles() -> None:
    templates = Environment(
        loader=FileSystemLoader(ROOT / "src/hansard_annotator/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    principal = {
        "csrf_token": "test-csrf",
        "display_name": "Reviewer",
        "is_admin": True,
    }
    request = type("Request", (), {"query_params": {}})()
    base_prediction = {
            "speaker_name_raw": "Test Member",
            "public_id": "prediction-id",
            "chamber": "House",
            "speech_date": "2026-10-27",
            "major_heading_original": "Questions",
            "minor_heading_original": "",
            "business_type": "Debate",
            "text_clean": "A representative speech.",
            "prediction_task": "hostility",
            "values": {
                "hostility_present": "NO", "target": "N/A", "evidence_spans": []
            },
            "partisan_hostility": False,
            "reasoning": "No hostility detected.",
            "configured_model": "model",
            "prompt_version": "hostility-prompt-v2",
            "run_public_id": "run-id",
            "execution_mode": "batch",
            "created_at": "2026-10-27",
            "failure_message": "AI evidence was unresolved.",
        }
    context = {
        "principal": principal,
        "request": request,
        "errors": {},
        "human_annotation": None,
        "codebook_flag": None,
        "fields": [SimpleNamespace(
            field_key="hostility_present",
            label="Hostility present",
            required=True,
            help_text="Classify the speech.",
            field_type="uncertainty",
            visibility_rules={},
            ui_hints={},
        )],
        "labels": {},
        "values": {},
    }
    valid_hostility = templates.get_template("annotations/ai_workspace.html").render(
        **context,
        prediction=base_prediction,
        suggested_values={"hostility_present": "NO", "target": None, "evidence_span": []},
        ai_unavailable=False,
        effective_review=None,
    )
    terminal_invalid = templates.get_template("annotations/ai_workspace.html").render(
        **context,
        prediction=base_prediction,
        ai_unavailable=True,
        effective_review=None,
    )
    policy_prediction = {
        **base_prediction,
        "prediction_task": "policy",
        "is_non_policy": False,
        "primary_domain_display": "AU04 — Education",
        "secondary_domain_displays": [],
    }
    policy = templates.get_template("annotations/ai_workspace.html").render(
        **context,
        prediction=policy_prediction,
        ai_unavailable=False,
        effective_review=None,
    )
    selected_run = {
            "public_id": "run-id",
            "display_name": "Hostility review",
            "prediction_task": "hostility",
            "configured_model": "model",
            "prompt_version": "hostility-prompt-v2",
            "review_complete": False,
            "reviewed_count": 1,
            "prediction_count": 2,
            "remaining_count": 1,
            "valid_count": 1,
            "terminal_invalid_count": 1,
            "imported_at": "2026-10-27",
        }
    queue = templates.get_template("annotations/ai_queue.html").render(
        principal=principal,
        selected_run=selected_run,
        rows=[],
        active_filter="unreviewed",
    )
    selector = templates.get_template("annotations/ai_review_projects.html").render(
        principal=principal,
        runs=[selected_run],
    )
    for rendered in (valid_hostility, terminal_invalid, policy, queue, selector):
        assert "style=" not in rendered
        assert "<style" not in rendered
    assert "Hostility: NO" in valid_hostility
    assert "Hostility target" in valid_hostility
    assert "Partisan hostility" in valid_hostility
    assert "Evidence</dt><dd>None" in valid_hostility
    assert "Optional: note a hostility or target boundary issue" in valid_hostility
    assert "AU07/AU08 boundary unclear" in policy
    assert "AI SUGGESTION" in valid_hostility
    assert valid_hostility.index("Hostility: NO") < valid_hostility.index("Model reasoning")
    assert valid_hostility.index("Model reasoning") < valid_hostility.index("Model provenance")
    assert "AI prediction unavailable" in terminal_invalid
    assert "<dt>Evidence</dt>" not in terminal_invalid
    assert "Annotate from scratch" in terminal_invalid
    assert 'action="/ai-reviews/prediction-id/edit"' in terminal_invalid
    assert 'name="hostility_present"' in terminal_invalid
    assert "Save edited annotation &amp; next" in terminal_invalid
    assert "AU04 — Education" in policy
    assert '<progress class="progress"' in queue
    assert "Continue review" in selector
    assert "/ai-review/run-id" in selector


def test_hostility_ai_suggestion_uses_stored_structured_fields_and_evidence() -> None:
    templates = Environment(
        loader=FileSystemLoader(ROOT / "src/hansard_annotator/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = templates.get_template("annotations/ai_workspace.html")
    speech = "Members should condemn this disgraceful conduct and restore trust."
    # Use exact canonical offsets/text so this fixture mirrors validated stored evidence.
    spans = [
        {"start_offset": 15, "end_offset": 22, "text": "condemn"},
        {"start_offset": 28, "end_offset": 47, "text": "disgraceful conduct"},
    ]
    principal = {"csrf_token": "csrf", "display_name": "Reviewer", "is_admin": True}
    fields = [
        SimpleNamespace(
            field_key="evidence_span",
            label="Evidence",
            required=False,
            help_text="Stored evidence.",
            field_type="evidence_span",
            visibility_rules={
                "allowed_when": {
                    "field_not_equals": {"field": "hostility_present", "value": "NO"}
                }
            },
            ui_hints={},
        )
    ]
    base = {
        "speaker_name_raw": "Member",
        "public_id": "prediction-id",
        "chamber": "House",
        "speech_date": "2026-10-27",
        "major_heading_original": "Debate",
        "minor_heading_original": "",
        "business_type": "Debate",
        "text_clean": speech,
        "prediction_task": "hostility",
        "reasoning": "Reasoning appears only after the result.",
        "configured_model": "model",
        "prompt_version": "hostility-prompt-v2",
        "run_public_id": "run-id",
        "execution_mode": "batch",
        "created_at": "2026-10-27",
        "failure_message": "AI evidence was unresolved.",
    }
    cases = [
        ("YES", "OPPOSING_PARTISAN_TARGET", "YES", spans),
        ("YES", "OTHER_POLITICAL_TARGET", "NO", spans),
        ("NO", "N/A", "NO", []),
        ("UNSURE", "UNSURE", "NO", spans[:1]),
    ]
    rendered_cases: list[str] = []
    for presence, target, partisan, evidence in cases:
        ai_values = {
            "hostility_present": presence,
            "target": target,
            "evidence_spans": evidence,
        }
        form_values = human_suggestion("hostility", ai_values)
        prediction = {
            **base,
            "values": ai_values,
            "partisan_hostility": partisan_hostility(ai_values),
        }
        rendered = template.render(
            principal=principal,
            request=type("Request", (), {"query_params": {}})(),
            errors={},
            human_annotation=None,
            codebook_flag=None,
            fields=fields,
            labels={},
            prediction=prediction,
            values=form_values,
            suggested_values=form_values,
            ai_unavailable=False,
            effective_review=None,
        )
        rendered_cases.append(rendered)
        assert f"Hostility: {presence}" in rendered
        assert f"<dd>{partisan}</dd>" in rendered
        assert "Target relationship" not in rendered
        if target == "N/A":
            assert "<dd>—</dd>" in rendered
        else:
            assert f"<dd>{target}</dd>" in rendered
        for span in evidence:
            assert f"<q>{span['text']}</q>" in rendered
        if evidence:
            # The sidebar reads immutable prediction values; the hidden input that
            # initializes speech highlighting receives the same evidence objects.
            input_match = re.search(
                r"name=\"evidence_span\" value='([^']*)' data-evidence-input", rendered
            )
            assert input_match is not None
            assert json.loads(unescape(input_match.group(1))) == evidence
        else:
            assert "Evidence</dt><dd>None" in rendered

    assert "Hostility: YES" in rendered_cases[0]
    assert "<dd>YES</dd>" in rendered_cases[0]
    assert "<dd>NO</dd>" in rendered_cases[1]
    assert "Hostility: NO" in rendered_cases[2]
    assert "Hostility: UNSURE" in rendered_cases[3]

    middleware = (ROOT / "src/hansard_annotator/web/middleware.py").read_text(
        encoding="utf-8"
    )
    assert "style-src 'self'" in middleware
    assert "script-src 'self'" in middleware
    assert "unsafe-inline" not in middleware
    for rendered in rendered_cases:
        assert "style=" not in rendered
        assert "<style" not in rendered


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
        "AI SUGGESTION",
        "prediction.text_clean",
        "prediction.reasoning",
        "Accept AI &amp; next",
        "Save edited annotation &amp; next",
        "Flag codebook issue",
        "Skip &amp; next",
        "Back to project queue",
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
    asset_versions = re.findall(r"(?:app\.css|app\.js)\?v=([^\"']+)", base)
    assert asset_versions == [
        "20261027-ai-review-csp-1",
        "20261028-hostility-no-clear-1",
    ]


def test_ai_review_project_cards_are_compact_and_keep_technical_identity() -> None:
    selector = (ROOT / "src/hansard_annotator/web/templates/annotations/ai_review_projects.html").read_text(
        encoding="utf-8"
    )
    for text in (
        "run.display_name",
        "run.prediction_task",
        "run.prediction_count",
        "run.reviewed_count",
        "run.remaining_count",
        "run.valid_count",
        "run.terminal_invalid_count",
        "run.configured_model",
        "run.prompt_version",
        "Continue review",
        "Open review",
    ):
        assert text in selector
    service = (
        ROOT / "src/hansard_annotator/web/annotations/ai_review.py"
    ).read_text(encoding="utf-8")
    assert "campaign.name AS campaign_name" in service
    assert 'row["campaign_name"] or "Imported prediction run"' in service
    assert "ORDER BY run.imported_at DESC NULLS LAST,run.id DESC" in service
    assert "reviewed_prediction_ids AS MATERIALIZED" in service


def test_dashboard_and_navigation_make_ai_review_first_class() -> None:
    dashboard = (ROOT / "src/hansard_annotator/web/templates/dashboard/index.html").read_text(
        encoding="utf-8"
    )
    base = (ROOT / "src/hansard_annotator/web/templates/base.html").read_text(encoding="utf-8")
    assert "Continue AI review" in dashboard
    for key in ("predictions", "unreviewed", "accepted", "edited", "flagged_codebook", "skipped"):
        assert key in dashboard
    assert ">AI Review</a>" in base


def test_routes_include_individual_actions_and_next_redirect() -> None:
    routes = (ROOT / "src/hansard_annotator/web/tasks/routes.py").read_text(encoding="utf-8")
    for suffix in ('"/ai-reviews/{prediction_id}"', "/accept", "/edit", "/flag", "/skip"):
        assert suffix in routes
    assert '"/ai-review"' in routes
    assert '"/ai-review/{run_id}"' in routes
    assert '"/ai-review/{run_id}/{prediction_id}"' in routes
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
