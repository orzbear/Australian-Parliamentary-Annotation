from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_GENERATION,
    APPROVED_PRODUCTION_MODEL,
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
    canonical_hash,
)
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.campaigns.routes import campaign_plan
from hansard_annotator.web.campaigns.routes import router as campaign_router
from hansard_annotator.web.campaigns.service import (
    AI_ELIGIBILITY_RULE,
    DEFAULT_EXCLUDE_HUMAN_ANNOTATED,
    GEMINI_BATCH_SCHEMA_SHA256,
    PAID_CONFIRMATION,
    PREDICTION_CONTRACT_SHA256,
    CampaignError,
    _planning_breakdown,
    _provider_plan,
    _RecordSetHasher,
    _selection_query,
    cache_plan_status,
    collect_campaign,
    configuration_class,
    credential_status,
    delete_unused_campaign,
    deterministic_selection,
    ensure_campaign_cache,
    estimate_plan,
    import_campaign,
    other_exclusion_reason,
    plan_campaign,
    production_configuration,
    refresh_campaign,
    same_prediction_configuration,
    selection_breakdown,
    submit_campaign,
    validate_campaign,
    validate_transition,
)
from hansard_annotator.web.codebook.routes import router as codebook_router
from hansard_annotator.web.codebook.service import canonical_download, production_codebook
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.research_exports.routes import router as export_router
from hansard_annotator.web.research_exports.service import (
    _deterministic_zip,
    prediction_export,
    require_complete_review,
)

ROOT = Path(__file__).parents[1]


def _principal(*, admin: bool = True) -> Principal:
    return Principal(
        user_id=1,
        public_id=UUID(int=1),
        username="researcher",
        display_name="Researcher",
        must_change_password=False,
        is_admin=admin,
        session_id=1,
        csrf_token="csrf",
    )


def _settings() -> WebSettings:
    return WebSettings(
        database=DatabaseSettings(
            url="postgresql://test:test@localhost/test",
            processed_data_root=Path("data/processed"),
        ),
        environment="test",
        session_secret="phase4d-test-secret-at-least-thirty-two-characters",
        cookie_secure=False,
        allowed_hosts=("testserver",),
        base_url="http://testserver",
        idle_minutes=60,
        absolute_hours=12,
        minimum_password_length=12,
        lockout_failures=2,
        lockout_minutes=15,
        log_level="INFO",
    )


def test_production_configuration_is_exact_and_research_alternatives_are_distinct() -> None:
    configuration = production_configuration()
    assert configuration == {
        "provider": "gemini",
        "configured_model": APPROVED_PRODUCTION_MODEL,
        "prompt_version": FROZEN_PROMPT_VERSION,
        "prompt_sha256": FROZEN_PROMPT_SHA256,
        "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
        "prediction_contract_sha256": PREDICTION_CONTRACT_SHA256,
        "provider_schema_sha256": GEMINI_BATCH_SCHEMA_SHA256,
        "generation_config": APPROVED_PRODUCTION_GENERATION,
        "execution_mode": "batch",
    }
    assert configuration_class(configuration) == "APPROVED_PRODUCTION"
    assert configuration_class({**configuration, "configured_model": "future-model"}) == "RESEARCH"
    assert same_prediction_configuration(configuration, dict(configuration))
    assert not same_prediction_configuration(
        configuration, {**configuration, "configured_model": "future-model"}
    )


def test_new_campaign_provider_plan_uses_flash_lite_minimal_payload() -> None:
    records = (
        {
            "record_id": "record-1",
            "speech": {
                "date": "2026-01-01",
                "chamber": "House",
                "speaker_name": "Speaker",
                "major_heading": "Debate",
                "minor_heading": None,
                "business_type": None,
                "text": "A policy speech.",
            },
        },
    )
    campaign = {
        **production_configuration(),
        "public_id": UUID(int=3),
        "cache_identifier": "cachedContents/gemini31",
        "record_set_sha256": canonical_hash(["record-1"]),
    }
    plan = _provider_plan(campaign, records)
    assert plan["provenance"]["model"] == APPROVED_PRODUCTION_MODEL
    assert plan["provenance"]["thinking_level"] == "minimal"
    assert "thinking_budget" not in plan["provenance"]
    request = plan["request"]["batch"]["input_config"]["requests"]["requests"][0]
    thinking = request["request"]["generationConfig"]["thinkingConfig"]
    assert thinking == {"thinkingLevel": "minimal"}
    assert "thinkingBudget" not in thinking


def test_eligibility_query_is_configuration_aware_and_not_ever_predicted() -> None:
    source = (
        ROOT / "src/hansard_annotator/web/campaigns/service.py"
    ).read_text(encoding="utf-8")
    for predicate in (
        "run.provider=%s",
        "run.configured_model=%s",
        "run.prompt_version=%s",
        "run.prompt_sha256=%s",
        "run.canonical_schema_sha256=%s",
        "run.prediction_contract_sha256=%s",
        "run.provider_schema_sha256=%s",
        "run.generation_config @> %s",
        "run.execution_mode=%s",
    ):
        assert predicate in source
    assert "exclude_human_annotated" in source
    assert 'and row["eligible_main_analysis"]' not in source
    assert '"eligibility_rule": AI_ELIGIBILITY_RULE' not in source
    assert '"eligibility_rule": (' in source
    assert "HOSTILITY_POLICY_ELIGIBILITY_RULE" in source


def test_state_machine_accepts_only_explicit_transitions() -> None:
    for current, target in (
        ("DRAFT", "PLANNED"),
        ("PLANNED", "READY"),
        ("READY", "SUBMITTED"),
        ("SUBMITTED", "RUNNING"),
        ("RUNNING", "PROVIDER_COMPLETED"),
        ("PROVIDER_COMPLETED", "COLLECTED"),
        ("COLLECTED", "VALIDATED"),
        ("VALIDATED", "IMPORTED"),
    ):
        validate_transition(current, target)
    with pytest.raises(CampaignError, match="invalid campaign transition"):
        validate_transition("DRAFT", "SUBMITTED")
    with pytest.raises(CampaignError, match="invalid campaign transition"):
        validate_transition("IMPORTED", "READY")


def test_deterministic_selection_hash_and_duplicate_protection() -> None:
    rows = [{"turn_key": f"turn-{number}"} for number in range(20)]
    first, first_hash = deterministic_selection(rows, limit=10, seed="fixed")
    second, second_hash = deterministic_selection(list(reversed(rows)), limit=10, seed="fixed")
    assert [row["turn_key"] for row in first] == [row["turn_key"] for row in second]
    assert first_hash == second_hash
    assert len(first_hash) == 64
    different, _ = deterministic_selection(rows, limit=10, seed="different")
    assert [row["turn_key"] for row in different] != [row["turn_key"] for row in first]
    with pytest.raises(CampaignError, match="duplicate eligible turn key"):
        deterministic_selection([rows[0], rows[0]], limit=1, seed="fixed")


def test_streaming_record_set_hash_matches_historical_canonical_hash() -> None:
    record_ids = ["turn:one", "turn:quote-\"", "turn:unicode-\N{SNOWMAN}"]
    hasher = _RecordSetHasher()
    for record_id in record_ids:
        hasher.add(record_id)
    assert hasher.hexdigest() == canonical_hash(record_ids)


def test_planning_query_is_database_bounded_and_preserves_seeded_ordering() -> None:
    source = inspect.getsource(plan_campaign)
    count_source = inspect.getsource(_planning_breakdown)
    query = _selection_query()
    assert ".fetchall()" not in source
    assert "all_rows" not in source
    assert "connection.cursor(name=" in source
    assert "LIMIT %s" in query
    assert "sha256(convert_to(turn.turn_key || ':' || %s,'UTF8'))" in query
    assert 'turn.turn_key COLLATE "C"' in query
    assert "eligible_main_analysis" not in query
    assert "annotation.status IN ('submitted','revised')" in query
    assert "annotation.status IN ('submitted','revised')" in count_source
    assert "prediction.prediction_task='policy'" in count_source
    assert "ORDER BY policy_run.imported_at DESC, prediction.run_id DESC" in count_source
    assert "latest_policy_is_non_policy IS FALSE" in count_source
    assert "latest_policy.is_non_policy IS FALSE" in query
    for noncanonical_status in ("draft", "skipped", "flagged", "in_progress"):
        assert f"'{noncanonical_status}'" not in query
        assert f"'{noncanonical_status}'" not in count_source


class _PlanningResult:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any]:
        return self.row


class _PlanningSelectionCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.selected: list[dict[str, Any]] = []
        self.itersize = 0

    def __enter__(self) -> _PlanningSelectionCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        assert "LIMIT %s" in query
        limit = int(parameters[-1])
        self.selected = self.rows[:limit]

    def __iter__(self) -> object:
        return iter(self.selected)


class _PlanningInsertCursor:
    def __init__(self) -> None:
        self.values: list[tuple[object, ...]] = []

    def executemany(self, _query: str, values: list[tuple[object, ...]]) -> None:
        self.values.extend(values)


class _PlanningConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.campaign = {
            **production_configuration(),
            "id": 1,
            "public_id": UUID(int=4),
            "corpus_id": 1,
            "status": "DRAFT",
        }
        self.breakdown = {
            "corpus_total_count": 160_577,
            "latest_policy_count": 0,
            "policy_eligible_count": 0,
            "excluded_non_policy_count": 0,
            "missing_latest_policy_count": 0,
            "human_annotated_count": 43,
            "human_prediction_overlap_count": 11,
            "excluded_existing_prediction_count": 100,
            "excluded_human_annotated_count": 32,
            "excluded_other_count": 10,
            "missing_prompt_text_count": 10,
            "hostility_policy_ineligible_count": 0,
            "eligible_count": 160_435,
        }
        self.rows = rows
        self.selection_cursor = _PlanningSelectionCursor(rows)
        self.insert_cursor = _PlanningInsertCursor()
        self.queries: list[str] = []

    def __enter__(self) -> _PlanningConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: object = None) -> _PlanningResult:
        self.queries.append(query)
        if "FOR UPDATE" in query:
            return _PlanningResult(self.campaign)
        if "WITH base AS MATERIALIZED" in query:
            return _PlanningResult(self.breakdown)
        if "UPDATE ai_annotation_campaigns" in query:
            assert isinstance(parameters, tuple)
            selection = parameters[0].obj
            self.campaign = {
                **self.campaign,
                "status": "PLANNED",
                "selection_definition": selection,
                "record_set_sha256": parameters[1],
                "corpus_total_count": parameters[2],
                "eligible_count": parameters[3],
                "planned_count": parameters[6],
            }
            return _PlanningResult(self.campaign)
        raise AssertionError(f"unexpected planning query: {query}")

    def cursor(self, name: str | None = None) -> object:
        return self.selection_cursor if name else self.insert_cursor


def test_ten_record_plan_from_large_corpus_materialises_only_selected_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = "bounded-seed"
    candidates = [
        {
            "id": number,
            "turn_key": f"turn-{number:06d}",
            "date": "2026-01-01",
            "chamber": "House",
            "speaker_name_raw": "Speaker",
            "major_heading_original": "Debate",
            "minor_heading_original": None,
            "business_type": None,
            "text_clean": f"Policy speech {number}",
            "text_raw": None,
        }
        for number in range(30)
    ]
    ordered = sorted(
        candidates,
        key=lambda row: (
            hashlib.sha256(f"{row['turn_key']}:{seed}".encode()).hexdigest(),
            row["turn_key"],
        ),
    )
    connection = _PlanningConnection(ordered)
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    result = plan_campaign(
        _settings(),
        _principal(),
        str(connection.campaign["public_id"]),
        limit=10,
        seed=seed,
        exclude_human_annotated=True,
    )

    expected_ids = [f"turn:{row['turn_key']}" for row in ordered[:10]]
    assert result.campaign["corpus_total_count"] == 160_577
    assert result.campaign["eligible_count"] == 160_435
    assert result.campaign["planned_count"] == 10
    assert result.campaign["record_set_sha256"] == canonical_hash(expected_ids)
    assert [value[2] for value in connection.insert_cursor.values] == expected_ids
    assert len(result.preview) == 10
    assert connection.selection_cursor.itersize == 1_000
    assert all("fetchall" not in query.lower() for query in connection.queries)


def test_human_annotation_exclusion_default_and_disjoint_plan_buckets() -> None:
    assert DEFAULT_EXCLUDE_HUMAN_ANNOTATED is True
    assert (
        inspect.signature(campaign_plan).parameters["exclude_human_annotated"].default
        is True
    )
    rows = [
        {
            "already_predicted": True,
            "human_annotated": True,
            "eligible_main_analysis": True,
            "text_clean": "Already predicted text",
        },
        {
            "already_predicted": False,
            "human_annotated": True,
            "eligible_main_analysis": True,
            "text_clean": "Human annotated text",
        },
        {
            "already_predicted": False,
            "human_annotated": False,
            "eligible_main_analysis": False,
            "text_clean": "A procedural or short record still needs model classification",
        },
        {
            "already_predicted": False,
            "human_annotated": False,
            "eligible_main_analysis": True,
            "text_clean": "Main analysis record",
        },
        {
            "already_predicted": False,
            "human_annotated": False,
            "eligible_main_analysis": False,
            "text_clean": "",
            "text_raw": "",
        },
    ]
    breakdown = selection_breakdown(rows, exclude_human_annotated=True)
    assert breakdown == {
        "corpus_total_count": 5,
        "human_annotated_count": 2,
        "excluded_existing_prediction_count": 1,
        "excluded_human_annotated_count": 1,
        "excluded_other_count": 1,
        "eligible_count": 2,
        "human_prediction_overlap_count": 1,
        "other_exclusion_reasons": {"missing_prompt_text": 1},
    }
    assert (
        breakdown["excluded_existing_prediction_count"]
        + breakdown["excluded_human_annotated_count"]
        + breakdown["excluded_other_count"]
        + breakdown["eligible_count"]
        == breakdown["corpus_total_count"]
    )


def test_human_annotation_exclusion_can_be_disabled() -> None:
    rows = [
        {
            "already_predicted": False,
            "human_annotated": True,
            "eligible_main_analysis": True,
            "text_clean": "Existing human annotation",
        }
    ]
    breakdown = selection_breakdown(rows, exclude_human_annotated=False)
    assert breakdown["excluded_human_annotated_count"] == 0
    assert breakdown["eligible_count"] == 1


def test_ai_eligibility_never_prefilters_policy_or_non_policy_content() -> None:
    for row in (
        {
            "eligible_main_analysis": False,
            "is_procedural": True,
            "text_clean": "The House will now proceed to the next item.",
        },
        {
            "eligible_main_analysis": False,
            "is_ceremonial": True,
            "text_clean": "I acknowledge today's distinguished visitors.",
        },
        {
            "eligible_main_analysis": None,
            "business_type": None,
            "text_clean": "The government should change this program's eligibility.",
        },
        {
            "eligible_main_analysis": False,
            "eligible_50_words": False,
            "text_clean": "A short contribution.",
        },
    ):
        assert other_exclusion_reason(row) is None
    assert other_exclusion_reason({"text_clean": "", "text_raw": ""}) == (
        "missing_prompt_text"
    )


def test_cost_plan_reuses_phase4b_simulation_and_batch_pricing() -> None:
    estimate = estimate_plan(100)
    assert estimate["records"] == 100
    assert estimate["input_tokens"] == 484892
    assert estimate["cached_input_tokens"] == 413100
    assert estimate["uncached_input_tokens"] == 71792
    assert estimate["output_tokens"] == 11406
    assert float(estimate["estimated_total_cost_usd"]) > float(
        estimate["token_processing_cost_usd"]
    )
    assert estimate["token_processing_cost_usd"] == pytest.approx(0.0226920833333)
    assert estimate["explicit_cache_storage_cost_usd"] == pytest.approx(0.049572)
    assert estimate["estimated_total_cost_usd"] == pytest.approx(0.0722640833333)


def test_full_corpus_plan_matches_gemini_31_benchmark_projection() -> None:
    estimate = estimate_plan(160443)
    assert estimate["records"] == 160443
    assert estimate["estimated_total_cost_usd"] == pytest.approx(36.457431, abs=0.000001)


def test_canonical_codebook_loads_frozen_prompt_schema_and_all_domains() -> None:
    codebook = production_codebook()
    assert codebook["schema"].content_sha256 == FROZEN_CANONICAL_SCHEMA_SHA256
    assert codebook["production"]["prompt_sha256"] == FROZEN_PROMPT_SHA256
    assert codebook["production"]["editable"] is False
    assert [label.code for label in codebook["taxonomy"].labels] == [
        *(f"AU{number:02d}" for number in range(1, 15)),
        "AU_OTHER_REVIEW",
    ]
    assert any(section["title"].startswith("STEP 1") for section in codebook["prompt_sections"])
    assert any(section["title"].startswith("STEP 3") for section in codebook["prompt_sections"])


def test_codebook_download_is_allowlisted_and_does_not_leak_paths() -> None:
    artifact = canonical_download("taxonomy-yaml")
    assert artifact.filename == "australian-policy-domains-0.1.0.yaml"
    assert b"AU_OTHER_REVIEW" in artifact.content
    assert str(ROOT).encode() not in artifact.content
    with pytest.raises(ValueError, match="unsupported"):
        canonical_download("../../.env")  # type: ignore[arg-type]


def test_navigation_and_templates_express_phase4d_information_architecture() -> None:
    base = (ROOT / "src/hansard_annotator/web/templates/base.html").read_text(encoding="utf-8")
    for label in (
        "Dashboard",
        "Human Annotation",
        "AI Annotation",
        "AI Review",
        "Codebook &amp; Taxonomy",
        "Exports",
        "Administration",
    ):
        assert f">{label}</a>" in base
    assert ">Projects</a>" not in base
    codebook = (ROOT / "src/hansard_annotator/web/templates/codebook/index.html").read_text(
        encoding="utf-8"
    )
    assert "FROZEN PRODUCTION" not in codebook  # supplied by the strict service, not duplicated
    assert "Edit Codebook" not in codebook
    assert "Edit Prompt" not in codebook
    campaigns = (ROOT / "src/hansard_annotator/web/templates/campaigns/detail.html").read_text(
        encoding="utf-8"
    )
    provider_status = (
        ROOT / "src/hansard_annotator/web/templates/campaigns/_provider_status.html"
    ).read_text(encoding="utf-8")
    assert PAID_CONFIRMATION in provider_status
    assert "Plan campaign — no provider call" in campaigns
    assert 'class="compact-check' in campaigns
    assert 'name="exclude_human_annotated" value="true" checked' in campaigns
    assert 'name="exclude_human_annotated" value="false"' in campaigns
    assert "Already human annotated (exclusive)" in campaigns
    assert "Other exclusions breakdown" in campaigns
    assert "Legacy selection rule" in campaigns
    assert (
        "same-configuration predictions or terminal provider refusals take precedence"
        in campaigns
    )
    new_campaign = (
        ROOT / "src/hansard_annotator/web/templates/campaigns/new.html"
    ).read_text(encoding="utf-8")
    for text in (
        "AI annotation configuration",
        "item.display_label",
        "item.description",
        "item.configured_model",
        "item.prompt_version",
        "Hostility held-out 100",
    ):
        assert text in new_campaign
    assert "campaign.configured_model" in campaigns
    assert "Gemini 2.5 Flash" in campaigns
    assert "generation_config.get('thinking_budget'" in campaigns
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src/hansard_annotator/web/templates").rglob("*.html")
    )
    assert 'name="api_key"' not in combined.lower()
    assert "HANSARD_DATABASE_URL" not in combined


def test_provider_status_uses_safe_supported_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "never-render-this-credential-value"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    unconfigured = credential_status()
    assert unconfigured["gemini"]["configured"] is False
    assert unconfigured["openai"]["configured"] is False

    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    configured = credential_status()
    assert configured["gemini"]["configured"] is True
    assert configured["openai"]["configured"] is True
    assert configured["gemini"]["environment_variables"] == (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    )
    assert configured["openai"]["environment_variables"] == ("OPENAI_API_KEY",)
    assert secret not in repr(configured)


def test_form_controls_exclude_checks_radios_and_hidden_inputs_from_text_styles() -> None:
    css = (ROOT / "src/hansard_annotator/web/static/src/app.css").read_text(
        encoding="utf-8"
    )
    assert ':not([type="checkbox"]):not([type="radio"]):not([type="hidden"])' in css
    assert '.compact-check input[type="checkbox"]' in css
    assert "focus-visible" in css


def test_planning_path_has_no_provider_operation() -> None:
    source = inspect.getsource(
        __import__(
            "hansard_annotator.web.campaigns.service", fromlist=["plan_campaign"]
        ).plan_campaign
    )
    assert ".submit(" not in source
    assert ".create_cache(" not in source
    assert ".get_cache(" not in source


def test_research_exports_reject_non_admin_before_database_access() -> None:
    with pytest.raises(PermissionError, match="administrator"):
        prediction_export(_settings(), _principal(admin=False), "run-id")


def test_phase4d_routers_expose_campaign_codebook_and_export_endpoints() -> None:
    paths = {
        route.path
        for router in (campaign_router, codebook_router, export_router)
        for route in router.routes
    }
    assert "/ai-annotation" in paths
    assert "/ai-annotation/{campaign_id}/submit" in paths
    assert "/ai-annotation/{campaign_id}/provider-status" in paths
    assert "/ai-annotation/{campaign_id}/repair-structural" in paths
    assert "/codebook" in paths
    assert "/exports" in paths


class _Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self.row

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class _Connection:
    def __init__(self, campaign: dict[str, Any]) -> None:
        self.campaign = campaign
        self.queries: list[str] = []

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _parameters: object = None) -> _Result:
        self.queries.append(query)
        if query.lstrip().startswith("SELECT"):
            return _Result(self.campaign)
        return _Result({**self.campaign, "status": "SUBMITTED", "provider_job_id": "batches/1"})


class _Gateway:
    def __init__(self) -> None:
        self.submissions = 0
        self.submitted_models: list[str] = []
        self.submitted_plans: list[dict[str, Any]] = []
        self.cache_creations = 0
        self.cache_lookups = 0

    @staticmethod
    def _cache_resource(name: str = "cachedContents/test") -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "name": name,
            "model": f"models/{APPROVED_PRODUCTION_MODEL}",
            "displayName": f"phase4d-{FROZEN_PROMPT_VERSION}-{FROZEN_PROMPT_SHA256[:16]}",
            "createTime": (now - timedelta(minutes=1)).isoformat(),
            "expireTime": (now + timedelta(hours=24)).isoformat(),
            "usageMetadata": {"totalTokenCount": 4175},
        }

    def create_cache(self, _request: dict[str, Any]) -> dict[str, Any]:
        self.cache_creations += 1
        return self._cache_resource()

    def get_cache(self, cache_identifier: str) -> dict[str, Any]:
        self.cache_lookups += 1
        return self._cache_resource(cache_identifier)

    def submit(self, _plan: dict[str, Any], _model: str) -> str:
        self.submissions += 1
        self.submitted_plans.append(_plan)
        self.submitted_models.append(_model)
        return "batches/1"

    def status(self, _provider_job_id: str) -> dict[str, Any]:
        raise AssertionError("status was not expected")


class _StatusGateway:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def submit(self, _plan: dict[str, Any], _model: str) -> str:
        raise AssertionError("submit was not expected")

    def create_cache(self, _request: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("cache creation was not expected")

    def get_cache(self, _cache_identifier: str) -> dict[str, Any]:
        raise AssertionError("cache lookup was not expected")

    def status(self, provider_job_id: str) -> dict[str, Any]:
        self.requests.append(provider_job_id)
        return {
            "name": provider_job_id,
            "metadata": {
                "state": "BATCH_STATE_RUNNING",
                "batchStats": {
                    "requestCount": "10",
                    "successfulRequestCount": "2",
                    "failedRequestCount": "0",
                    "pendingRequestCount": "8",
                },
            },
        }


def test_paid_submit_confirmation_and_duplicate_submission_are_blocked_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _Gateway()
    with pytest.raises(CampaignError, match=PAID_CONFIRMATION):
        submit_campaign(_settings(), _principal(), "campaign", "wrong", gateway=gateway)
    assert gateway.submissions == 0

    connection = _Connection({"id": 1, "status": "SUBMITTED", "provider_job_id": "batches/old"})
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    with pytest.raises(CampaignError, match="not ready"):
        submit_campaign(
            _settings(), _principal(), "campaign", PAID_CONFIRMATION, gateway=gateway
        )
    assert gateway.submissions == 0


def test_successful_submit_persists_provider_job_once(monkeypatch: pytest.MonkeyPatch) -> None:
    campaign = {
        **production_configuration(),
        "id": 1,
        "public_id": UUID(int=2),
        "status": "READY",
        "provider_job_id": None,
        "cache_identifier": "cachedContents/test",
        "cache_status": "REUSABLE",
        "cache_content_sha256": FROZEN_PROMPT_SHA256,
        "cache_model": f"models/{APPROVED_PRODUCTION_MODEL}",
        "cache_display_name": f"phase4d-{FROZEN_PROMPT_VERSION}-{FROZEN_PROMPT_SHA256[:16]}",
        "cache_verified_at": datetime.now(UTC),
        "cache_expires_at": datetime.now(UTC) + timedelta(hours=24),
        "planned_count": 1,
        "selection_definition": {"eligibility_rule": AI_ELIGIBILITY_RULE},
    }
    connection = _Connection(campaign)
    gateway = _Gateway()
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service._records_for_campaign",
        lambda *_args: ({"record_id": "turn:one"},),
    )
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service._provider_plan",
        lambda *_args: {"validated": True},
    )
    result = submit_campaign(
        _settings(), _principal(), "campaign", PAID_CONFIRMATION, gateway=gateway
    )
    assert result["provider_job_id"] == "batches/1"
    assert gateway.submissions == 1
    assert gateway.submitted_models == [APPROVED_PRODUCTION_MODEL]
    assert gateway.cache_lookups == 1
    assert gateway.cache_creations == 0
    assert any("provider_job_id=%s" in query for query in connection.queries)


class _CacheConnection(_Connection):
    def execute(self, query: str, parameters: object = None) -> _Result:
        self.queries.append(query)
        values = parameters if isinstance(parameters, tuple) else ()
        if query.lstrip().startswith("SELECT"):
            return _Result(self.campaign)
        if "cache_status='CREATING'" in query:
            self.campaign = {**self.campaign, "cache_status": "CREATING"}
            return _Result(self.campaign)
        if "cache_status=%s,cache_identifier=%s" in query:
            self.campaign = {
                **self.campaign,
                "cache_status": values[0],
                "cache_identifier": values[1],
                "cache_content_sha256": values[2],
                "cache_model": values[3],
                "cache_display_name": values[4],
                "cache_created_at": values[5],
                "cache_expires_at": values[6],
                "cache_verified_at": values[7],
                "cache_metadata": values[8].obj,
            }
            return _Result(self.campaign)
        if "cache_status='UNAVAILABLE'" in query:
            self.campaign = {**self.campaign, "cache_status": "UNAVAILABLE"}
        return _Result(self.campaign)


def _ready_campaign(**overrides: Any) -> dict[str, Any]:
    return {
        **production_configuration(),
        "id": 1,
        "public_id": UUID(int=2),
        "status": "READY",
        "provider_job_id": None,
        "cache_status": "REQUIRED",
        "cache_identifier": None,
        "cache_content_sha256": None,
        "cache_model": None,
        "cache_display_name": None,
        "cache_created_at": None,
        "cache_expires_at": None,
        "cache_verified_at": None,
        "cache_metadata": {},
        "selection_definition": {"eligibility_rule": AI_ELIGIBILITY_RULE},
        **overrides,
    }


def test_legacy_selection_is_blocked_before_cache_or_paid_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _CacheConnection(
        _ready_campaign(
            selection_definition={"eligible_main_analysis": True},
        )
    )
    gateway = _Gateway()
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    with pytest.raises(CampaignError, match="legacy main-analysis eligibility"):
        ensure_campaign_cache(_settings(), _principal(), "campaign", gateway=gateway)
    assert gateway.cache_lookups == 0
    assert gateway.cache_creations == 0
    assert gateway.submissions == 0


def test_confirmed_submission_path_creates_and_persists_canonical_cache_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _CacheConnection(_ready_campaign())
    gateway = _Gateway()
    requests: list[dict[str, Any]] = []

    def create_cache(request: dict[str, Any]) -> dict[str, Any]:
        requests.append(request)
        return gateway._cache_resource()

    gateway.create_cache = create_cache  # type: ignore[method-assign]
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    result = ensure_campaign_cache(
        _settings(), _principal(), "campaign", gateway=gateway
    )
    assert result["cache_status"] == "REUSABLE"
    assert result["cache_identifier"] == "cachedContents/test"
    assert result["cache_content_sha256"] == FROZEN_PROMPT_SHA256
    assert result["cache_model"] == f"models/{APPROVED_PRODUCTION_MODEL}"
    assert result["cache_verified_at"] is not None
    assert result["cache_expires_at"] > datetime.now(UTC)
    assert requests[0]["model"] == f"models/{APPROVED_PRODUCTION_MODEL}"
    assert requests[0]["displayName"].endswith(FROZEN_PROMPT_SHA256[:16])
    assert requests[0]["systemInstruction"]["parts"][0]["text"]
    assert requests[0]["ttl"] == "86400s"


def test_live_compatible_cache_is_verified_and_reused_without_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _Gateway()
    connection = _CacheConnection(
        _ready_campaign(
            cache_status="REUSABLE",
            cache_identifier="cachedContents/existing",
            cache_content_sha256=FROZEN_PROMPT_SHA256,
            cache_model=f"models/{APPROVED_PRODUCTION_MODEL}",
            cache_display_name=(
                f"phase4d-{FROZEN_PROMPT_VERSION}-{FROZEN_PROMPT_SHA256[:16]}"
            ),
            cache_created_at=datetime.now(UTC) - timedelta(minutes=5),
            cache_expires_at=datetime.now(UTC) + timedelta(hours=12),
            cache_verified_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    result = ensure_campaign_cache(
        _settings(), _principal(), "campaign", gateway=gateway
    )
    assert result["cache_identifier"] == "cachedContents/existing"
    assert gateway.cache_lookups == 1
    assert gateway.cache_creations == 0


def test_expired_cache_is_not_reusable() -> None:
    campaign = _ready_campaign(
        cache_identifier="cachedContents/expired",
        cache_content_sha256=FROZEN_PROMPT_SHA256,
        cache_model=f"models/{APPROVED_PRODUCTION_MODEL}",
        cache_display_name=f"phase4d-{FROZEN_PROMPT_VERSION}-{FROZEN_PROMPT_SHA256[:16]}",
        cache_verified_at=datetime.now(UTC),
        cache_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert cache_plan_status(campaign) == "UNAVAILABLE"


def test_cache_reuse_is_model_specific_for_new_production_configuration() -> None:
    expiry = datetime.now(UTC) + timedelta(hours=12)
    old_model_cache = _ready_campaign(
        cache_identifier="cachedContents/old-model",
        cache_content_sha256=FROZEN_PROMPT_SHA256,
        cache_model="models/gemini-2.5-flash",
        cache_display_name=f"phase4d-{FROZEN_PROMPT_VERSION}-{FROZEN_PROMPT_SHA256[:16]}",
        cache_verified_at=datetime.now(UTC),
        cache_expires_at=expiry,
    )
    assert cache_plan_status(old_model_cache) == "UNAVAILABLE"

    current_model_cache = {
        **old_model_cache,
        "cache_identifier": "cachedContents/current-model",
        "cache_model": f"models/{APPROVED_PRODUCTION_MODEL}",
    }
    assert cache_plan_status(current_model_cache) == "REUSABLE"


def test_missing_provider_cache_is_replaced_before_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingCacheGateway(_Gateway):
        def get_cache(self, cache_identifier: str) -> dict[str, Any]:
            self.cache_lookups += 1
            raise ValueError(f"Gemini cache lookup HTTP 404: {cache_identifier} was not found")

    gateway = MissingCacheGateway()
    connection = _CacheConnection(
        _ready_campaign(
            cache_status="REUSABLE",
            cache_identifier="cachedContents/missing",
            cache_content_sha256=FROZEN_PROMPT_SHA256,
            cache_model=f"models/{APPROVED_PRODUCTION_MODEL}",
            cache_display_name=(
                f"phase4d-{FROZEN_PROMPT_VERSION}-{FROZEN_PROMPT_SHA256[:16]}"
            ),
            cache_created_at=datetime.now(UTC) - timedelta(minutes=5),
            cache_expires_at=datetime.now(UTC) + timedelta(hours=12),
            cache_verified_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    result = ensure_campaign_cache(
        _settings(), _principal(), "campaign", gateway=gateway
    )
    assert gateway.cache_lookups == 1
    assert gateway.cache_creations == 1
    assert result["cache_identifier"] == "cachedContents/test"


def test_provider_refresh_uses_stored_job_and_advances_to_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = {
        "id": 1,
        "status": "SUBMITTED",
        "provider_job_id": "batches/1",
    }

    class RefreshConnection(_Connection):
        def execute(self, query: str, parameters: object = None) -> _Result:
            self.queries.append(query)
            if query.lstrip().startswith("SELECT"):
                return _Result(self.campaign)
            values = parameters if isinstance(parameters, tuple) else ()
            return _Result({**self.campaign, "status": values[0], "provider_state": values[1]})

    connection = RefreshConnection(campaign)
    gateway = _StatusGateway()
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    result = refresh_campaign(_settings(), _principal(), "campaign", gateway=gateway)
    assert result["status"] == "RUNNING"
    assert result["provider_state"] == "BATCH_STATE_RUNNING"
    assert gateway.requests == ["batches/1"]


def test_collected_campaign_resumes_without_calling_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = {
        "id": 1,
        "status": "COLLECTED",
        "artifact_reference": "controlled/campaign-artifact",
    }
    connection = _Connection(campaign)
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    result = collect_campaign(_settings(), _principal(), "campaign", gateway=_Gateway())
    assert result == campaign


def test_invalid_collected_artifact_blocks_validation_and_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "invalid-artifact"
    artifact.mkdir()
    (artifact / "operational_summary.json").write_text(
        '{"provenance": {}}\n', encoding="utf-8"
    )
    (artifact / "predictions.jsonl").write_text("", encoding="utf-8")
    connection = _Connection(
        {
            "id": 1,
            "status": "COLLECTED",
            "artifact_reference": str(artifact),
            "planned_count": 1,
                **production_configuration(),
        }
    )
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    with pytest.raises(ValueError, match="artifact provenance mismatch"):
        validate_campaign(_settings(), _principal(), "campaign")
    assert not any("status='VALIDATED'" in query for query in connection.queries)


def test_import_links_existing_phase4c_run_and_repeated_import_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    campaign = {
        "id": 1,
        "public_id": "campaign-public-id",
        "name": "Fixture campaign",
        "configuration_id": "policy-v2",
        "status": "VALIDATED",
        "model_prediction_run_id": None,
        "artifact_reference": str(tmp_path / "artifact"),
        **production_configuration(),
    }

    class ImportConnection(_Connection):
        def execute(self, query: str, _parameters: object = None) -> _Result:
            self.queries.append(query)
            if "FROM model_prediction_runs" in query:
                return _Result({"id": 99})
            if query.lstrip().startswith("UPDATE"):
                return _Result({**self.campaign, "status": "IMPORTED", "model_prediction_run_id": 99})
            return _Result(self.campaign)

    connection = ImportConnection(campaign)
    imports: list[Path] = []
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    def fake_import(
        path: Path, _settings: DatabaseSettings, *, dry_run: bool, **_kwargs: object
    ) -> dict[str, Any]:
        assert not dry_run
        imports.append(path)
        return {"artifact_sha256": "a" * 64}

    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.import_predictions", fake_import
    )
    result = import_campaign(_settings(), _principal(), "campaign")
    assert result["status"] == "IMPORTED"
    assert result["model_prediction_run_id"] == 99
    assert imports == [tmp_path / "artifact"]

    connection.campaign = result
    second = import_campaign(_settings(), _principal(), "campaign")
    assert second["status"] == "IMPORTED"
    assert imports == [tmp_path / "artifact"]


def test_research_zip_is_byte_deterministic_and_contains_expected_files() -> None:
    files = {"b.csv": b"b\n", "a.json": b"{}\n"}
    assert _deterministic_zip(files) == _deterministic_zip(dict(reversed(list(files.items()))))


def test_final_exports_require_complete_state_but_csvs_can_be_partial() -> None:
    summary = {"counts": {"unreviewed_predictions": 1}}
    for artifact in ("full", "edited", "flags"):
        require_complete_review(summary, artifact)
    with pytest.raises(ValueError, match="zero unreviewed"):
        require_complete_review(summary, "package")
    with pytest.raises(ValueError, match="zero unreviewed"):
        require_complete_review(summary, "summary")
    with pytest.raises(ValueError, match="zero unreviewed"):
        require_complete_review(summary, "report")
    require_complete_review({"counts": {"unreviewed_predictions": 0}}, "package")


def test_campaign_long_actions_disable_and_running_status_auto_polls() -> None:
    detail = (ROOT / "src/hansard_annotator/web/templates/campaigns/detail.html").read_text(
        encoding="utf-8"
    )
    status = (
        ROOT / "src/hansard_annotator/web/templates/campaigns/_provider_status.html"
    ).read_text(encoding="utf-8")
    javascript = (ROOT / "src/hansard_annotator/web/static/src/app.js").read_text(
        encoding="utf-8"
    )
    assert "data-progress-form" in detail
    for progress_text in (
        "Planning...",
        "Approving...",
        "Submitting Batch...",
        "Refreshing...",
        "Collecting...",
        "Validating...",
        "Importing...",
        "Cancelling...",
    ):
        assert progress_text in detail + status
    assert 'button.disabled = true' in javascript
    assert 'progressForm.dataset.submitting === "true"' in javascript
    assert "hx-post" in status
    assert 'campaign.status in [\'SUBMITTED\',\'RUNNING\']' in status
    assert "every 7s" in status
    assert "Checking Gemini status automatically" in status
    assert "PROVIDER_COMPLETED" not in status.split("hx-post", 1)[0]


def test_campaign_feedback_and_export_completion_copy_are_explicit() -> None:
    routes = (ROOT / "src/hansard_annotator/web/campaigns/routes.py").read_text(
        encoding="utf-8"
    )
    for message in (
        "Plan created.",
        "Campaign approved.",
        "Batch submitted.",
        "Results collected.",
        "Validation passed:",
        "Predictions imported:",
    ):
        assert message in routes
    exports = (ROOT / "src/hansard_annotator/web/templates/exports/index.html").read_text(
        encoding="utf-8"
    )
    assert "Partial — review incomplete" in exports
    assert "Final audit artifacts are enabled when every prediction" in exports
    assert "run.unreviewed_count == 0" in exports
    assert "Summary JSON" in exports and "disabled" in exports


def test_migration_has_restrictive_provenance_and_unsafe_downgrade_guard() -> None:
    migration = (
        ROOT / "migrations/versions/20261013_07_ai_annotation_campaigns.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "20261013_07"' in migration
    assert 'down_revision = "20261009_06"' in migration
    assert "CREATE TABLE ai_annotation_campaigns" in migration
    assert "CREATE TABLE ai_annotation_campaign_records" in migration
    assert "UNIQUE (campaign_id,speaker_turn_id)" in migration
    assert "provider_job_id text UNIQUE" in migration
    assert "operational_metadata jsonb" in migration
    assert "cache_status text NOT NULL DEFAULT 'REQUIRED'" in migration
    assert "cache_expires_at timestamptz" in migration
    assert "NEW.cache_identifier IS DISTINCT FROM OLD.cache_identifier" in migration
    assert "ON DELETE RESTRICT" in migration
    assert "submitted campaign provenance is immutable" in migration
    assert "downgrade refused" in migration


def test_phase4d_services_reuse_phase4b_and_phase4c_instead_of_parallel_algorithms() -> None:
    campaign_service = (
        ROOT / "src/hansard_annotator/web/campaigns/service.py"
    ).read_text(encoding="utf-8")
    for reused in (
        "build_batch_create_request",
        "simulate_costs",
        "validate_production_plan",
        "parse_batch_status",
        "collect_inline_batch",
        "load_prediction_artifact",
        "import_predictions",
    ):
        assert reused in campaign_service
    export_service = (
        ROOT / "src/hansard_annotator/web/research_exports/service.py"
    ).read_text(encoding="utf-8")
    assert "create_review_audit" in export_service
    assert "UPDATE model_predictions" not in export_service
    assert "INSERT INTO model_prediction_reviews" not in export_service


class _CampaignDeleteResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _CampaignDeleteTransaction:
    def __enter__(self) -> _CampaignDeleteTransaction:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _CampaignDeleteConnection:
    def __init__(
        self,
        campaign: dict[str, Any],
        *,
        eligible: bool,
        batch_count: int = 0,
        record_count: int = 0,
    ) -> None:
        self.campaign = campaign
        self.eligible = eligible
        self.batch_count = batch_count
        self.record_count = record_count
        self.statements: list[tuple[str, object]] = []

    def __enter__(self) -> _CampaignDeleteConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> _CampaignDeleteTransaction:
        return _CampaignDeleteTransaction()

    def execute(self, query: str, parameters: object = None) -> _CampaignDeleteResult:
        sql = " ".join(query.split())
        self.statements.append((sql, parameters))
        if "SELECT * FROM ai_annotation_campaigns" in sql:
            return _CampaignDeleteResult([self.campaign])
        if "SELECT id FROM ai_annotation_campaign_batches" in sql:
            return _CampaignDeleteResult([{"id": value} for value in range(self.batch_count)])
        if "SELECT id FROM ai_annotation_campaign_records" in sql:
            return _CampaignDeleteResult([{"id": value} for value in range(self.record_count)])
        if "ai_annotation_campaign_is_deletable" in sql:
            return _CampaignDeleteResult([{"eligible": self.eligible}])
        return _CampaignDeleteResult()


def _unused_campaign() -> dict[str, Any]:
    return {"id": 19, "public_id": UUID(int=19), "name": "Unused draft"}


def test_admin_can_delete_unused_draft_and_local_planning_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hansard_annotator.web.campaigns import service as campaign_service

    connection = _CampaignDeleteConnection(
        {**_unused_campaign(), "status": "PLANNED"},
        eligible=True,
        batch_count=2,
        record_count=5,
    )
    monkeypatch.setattr(campaign_service.psycopg, "connect", lambda *_args, **_kwargs: connection)

    delete_unused_campaign(
        _settings(), _principal(), str(connection.campaign["public_id"]),
        confirmation="Delete Unused draft",
    )

    statements = [statement for statement, _ in connection.statements]
    assert "FOR UPDATE" in statements[0]
    assert statements.index("DELETE FROM ai_annotation_campaign_batches WHERE campaign_id=%s") < statements.index(
        "DELETE FROM ai_annotation_campaign_records WHERE campaign_id=%s"
    ) < statements.index("DELETE FROM ai_annotation_campaigns WHERE id=%s")
    assert statements[-1].startswith("INSERT INTO audit_events")
    assert all(parameters in ((19,),) for statement, parameters in connection.statements if "DELETE FROM" in statement)


def test_campaign_delete_rejects_non_admin_bad_confirmation_and_forged_ineligible_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hansard_annotator.web.campaigns import service as campaign_service

    with pytest.raises(PermissionError):
        delete_unused_campaign(
            _settings(), _principal(admin=False), "campaign-id",
            confirmation="Delete Unused draft",
        )

    connection = _CampaignDeleteConnection(_unused_campaign(), eligible=False)
    monkeypatch.setattr(campaign_service.psycopg, "connect", lambda *_args, **_kwargs: connection)
    with pytest.raises(CampaignError, match="provider or research activity"):
        delete_unused_campaign(
            _settings(), _principal(), str(connection.campaign["public_id"]),
            confirmation="Delete Unused draft",
        )
    assert not any("DELETE FROM" in statement for statement, _ in connection.statements)

    with pytest.raises(CampaignError, match="confirmation phrase"):
        delete_unused_campaign(
            _settings(), _principal(), str(connection.campaign["public_id"]),
            confirmation="Delete campaign",
        )


def test_campaign_deletion_migration_blocks_provider_and_research_provenance() -> None:
    migration = (
        ROOT / "migrations/versions/20261030_16_unused_campaign_deletion.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "20261030_16"' in migration
    assert 'down_revision = "20261029_15"' in migration
    for guarded_state in (
        "campaign.provider_job_id IS NULL",
        "batch.provider_job_id IS NOT NULL",
        "batch.provider_input_file_id IS NOT NULL",
        "campaign.model_prediction_run_id IS NULL",
        "ai_annotation_campaign_recoveries recovery",
        "audit_events event",
        "campaign.authorized_at IS NULL",
        "batch.status<>'PENDING'",
        "event.entity_public_id=campaign.public_id",
    ):
        assert guarded_state in migration
    assert "ON DELETE CASCADE" not in migration
    assert "hansard.allow_unused_campaign_delete" in migration


def test_gemini_hostility_configuration_migration_preserves_allowlist_and_guards_downgrade() -> None:
    migration = (
        ROOT / "migrations/versions/20261030_17_gemini_hostility_campaign.py"
    ).read_text(encoding="utf-8")
    upgrade, downgrade = migration.split("def downgrade() -> None:", maxsplit=1)
    for configuration_id in (
        "policy-v2",
        "hostility-v2",
        "hostility-luna-confirmation-100",
        "hostility-luna-production-v1",
        "hostility-gemini-25-flash-production",
    ):
        assert configuration_id in upgrade
    assert "prediction_task='hostility'" in upgrade
    assert "annotation_schema_slug='australian_partisan_hostility'" in upgrade
    assert "configuration_id='hostility-gemini-25-flash-production'" in upgrade
    assert "WHERE configuration_id='hostility-gemini-25-flash-production'" in downgrade
    assert "refusing to remove their configuration" in downgrade
    assert "hostility-luna-confirmation-100" in downgrade
    assert "hostility-luna-production-v1" in downgrade


def test_campaign_delete_route_requires_post_and_redirects_with_success_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from hansard_annotator.web.campaigns import routes as campaign_routes

    async def csrf_ok(*_args: object) -> None:
        return None

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(campaign_routes, "enforce_csrf", csrf_ok)
    monkeypatch.setattr(
        campaign_routes,
        "delete_unused_campaign",
        lambda _settings, _principal, campaign_id, *, confirmation: calls.append(
            (campaign_id, confirmation)
        ),
    )

    response = asyncio.run(
        campaign_routes.campaign_delete(
            None, "unused-public-id", _settings(), _principal(), "Delete Test campaign"
        )
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ai-annotation?notice=campaign-deleted"
    assert calls == [("unused-public-id", "Delete Test campaign")]
