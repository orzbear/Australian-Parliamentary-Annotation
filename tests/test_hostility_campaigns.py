from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.llm_eval.hostility import prediction_json_schema
from hansard_annotator.llm_eval.hostility_validation import (
    CONTRACT_SHA256,
    HELDOUT_SAMPLE_ID,
    HELDOUT_SAMPLE_SHA256,
    PROMPT_SHA256,
)
from hansard_annotator.llm_eval.hostility_validation import (
    MODEL as HOSTILITY_MODEL,
)
from hansard_annotator.llm_eval.hostility_validation import (
    PROMPT_VERSION as HOSTILITY_PROMPT_VERSION,
)
from hansard_annotator.llm_eval.import_predictions import _prediction_insert_values
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_GENERATION,
    APPROVED_PRODUCTION_MODEL,
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
    HOSTILITY_PREDICTION_FIELDS,
    canonical_hash,
    gemini_batch_schema,
    validate_production_plan,
)
from hansard_annotator.llm_eval.prompt import build_system_prompt, prompt_hash
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.campaigns.configurations import (
    APPROVED_AI_CONFIGURATIONS,
    HOSTILITY_CONFIGURATION_ID,
    HOSTILITY_SCHEMA_SHA256,
    POLICY_CONFIGURATION_ID,
    approved_configuration,
    configuration_for_campaign,
)
from hansard_annotator.web.campaigns.phase4e import (
    authorize_full_production,
    plan_campaign_chunks,
)
from hansard_annotator.web.campaigns.routes import campaign_create
from hansard_annotator.web.campaigns.service import (
    PAID_CONFIRMATION,
    CampaignError,
    _provider_plan,
    approved_campaign_configurations,
    collect_campaign,
    create_campaign,
    credential_status,
    get_campaign,
    import_campaign,
    plan_campaign,
    production_configuration,
    repair_campaign_results,
    submit_campaign,
    validate_campaign,
)
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.tasks.frozen_samples import load_frozen_sample

ROOT = Path(__file__).resolve().parents[1]


def _principal(*, admin: bool = True) -> Principal:
    return Principal(
        user_id=1,
        public_id=UUID(int=1),
        username="researcher",
        display_name="Researcher",
        must_change_password=False,
        is_admin=admin,
        session_id=1,
        csrf_token="csrf-token",
    )


def _settings() -> WebSettings:
    return WebSettings(
        database=DatabaseSettings(
            url="postgresql://test:test@localhost/test",
            processed_data_root=Path("data/processed"),
        ),
        environment="test",
        session_secret="hostility-campaign-test-secret-at-least-thirty-two-chars",
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


def _hostility_campaign_row(**overrides: Any) -> dict[str, Any]:
    config = approved_configuration(HOSTILITY_CONFIGURATION_ID)
    base: dict[str, Any] = {
        **config.persistence_values(),
        "id": 42,
        "public_id": UUID(int=42),
        "name": "Hostility V2 Held-Out Validation",
        "corpus_id": 1,
        "corpus_name": "Australian Commonwealth Hansard 1998-2025",
        "status": "DRAFT",
        "configuration_class": "APPROVED_PRODUCTION",
        "record_set_id": config.record_set_id,
        "approved_record_set_sha256": config.record_set_sha256,
        "selection_definition": {},
        "record_set_sha256": None,
        "corpus_total_count": 0,
        "eligible_count": 0,
        "excluded_existing_prediction_count": 0,
        "excluded_other_count": 0,
        "planned_count": 0,
        "estimated_input_tokens": 0,
        "estimated_cached_input_tokens": 0,
        "estimated_uncached_input_tokens": 0,
        "estimated_output_tokens": 0,
        "estimated_processing_cost_usd": Decimal("0"),
        "estimated_cache_storage_cost_usd": Decimal("0"),
        "estimated_total_cost_usd": Decimal("0"),
        "cache_status": "REQUIRED",
        "cache_identifier": None,
        "cache_content_sha256": None,
        "cache_model": None,
        "cache_display_name": None,
        "cache_created_at": None,
        "cache_expires_at": None,
        "cache_verified_at": None,
        "cache_metadata": {},
        "provider_job_id": None,
        "provider_state": None,
        "provider_success_count": 0,
        "provider_failure_count": 0,
        "valid_count": 0,
        "invalid_count": 0,
        "artifact_reference": None,
        "artifact_sha256": None,
        "model_prediction_run_id": None,
        "run_public_id": None,
        "operational_metadata": {},
        "failure_metadata": {},
        "orchestration_mode": "LEGACY",
        "orchestration_status": "INACTIVE",
        "authorized_at": None,
        "collected_at": None,
        "validated_at": None,
        "imported_at": None,
    }
    base.update(overrides)
    return base


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(ROOT / "src/hansard_annotator/web/templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )


def test_approved_campaign_registry_is_closed_and_frozen() -> None:
    assert set(APPROVED_AI_CONFIGURATIONS) == {
        POLICY_CONFIGURATION_ID,
        HOSTILITY_CONFIGURATION_ID,
    }
    policy = approved_configuration(POLICY_CONFIGURATION_ID)
    hostility = approved_configuration(HOSTILITY_CONFIGURATION_ID)

    assert policy.display_label == "Policy classification — Frozen V2"
    assert policy.prediction_task == "policy"
    assert policy.annotation_schema_slug == "australian_policy_annotation"
    assert policy.annotation_schema_version == "0.2.0"
    assert policy.provider == "gemini"
    assert policy.configured_model == APPROVED_PRODUCTION_MODEL
    assert policy.prompt_version == FROZEN_PROMPT_VERSION
    assert policy.prompt_sha256 == FROZEN_PROMPT_SHA256
    assert policy.canonical_schema_sha256 == FROZEN_CANONICAL_SCHEMA_SHA256
    assert policy.generation_config == APPROVED_PRODUCTION_GENERATION
    assert policy.record_set_mode == "deterministic_selection"
    assert policy.record_set_id is None
    assert policy.record_set_sha256 is None

    assert hostility.display_label == "Targeted hostility — Frozen V2"
    assert hostility.prediction_task == "hostility"
    assert hostility.annotation_schema_slug == "australian_partisan_hostility"
    assert hostility.annotation_schema_version == "0.2.0"
    assert hostility.provider == "gemini"
    assert hostility.configured_model == "gemini-2.5-flash"
    assert hostility.prompt_version == "hostility-prompt-v2"
    assert hostility.prompt_sha256 == PROMPT_SHA256
    assert hostility.canonical_schema_sha256 == HOSTILITY_SCHEMA_SHA256
    assert hostility.prediction_contract_sha256 == CONTRACT_SHA256
    assert hostility.generation_config == {"thinking_budget": 0}
    assert hostility.record_set_mode == "frozen_sample"
    assert hostility.record_set_id == HELDOUT_SAMPLE_ID
    assert hostility.record_set_size == 100
    assert hostility.record_set_sha256 == HELDOUT_SAMPLE_SHA256

    with pytest.raises(ValueError, match="unknown approved"):
        approved_configuration("injected-prompt")


def test_hostility_context_contract_and_provider_schema_are_frozen() -> None:
    configuration = approved_configuration(HOSTILITY_CONFIGURATION_ID)
    context_path = ROOT / "config/llm_eval/hostility-v2-codebook-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert prompt_hash(build_system_prompt(context, configuration.prompt_version)) == PROMPT_SHA256
    schema = prediction_json_schema(configuration.prompt_version)
    assert canonical_hash(schema) == CONTRACT_SHA256
    assert canonical_hash(gemini_batch_schema(schema)) == configuration.provider_schema_sha256


def test_hostility_campaign_identity_rejects_server_side_drift() -> None:
    configuration = approved_configuration(HOSTILITY_CONFIGURATION_ID)
    campaign = _hostility_campaign_row()
    assert configuration_for_campaign(campaign) == configuration
    for key, bad in (
        ("prompt_sha256", "0" * 64),
        ("prediction_contract_sha256", "1" * 64),
        ("configured_model", "arbitrary-model"),
        ("prediction_task", "policy"),
        ("annotation_schema_slug", "arbitrary-schema"),
        ("record_set_id", "hostility-development-100"),
        ("approved_record_set_sha256", "2" * 64),
        ("planned_count", 99),
        ("selection_definition", {"mode": "frozen_sample", "sample_id": "wrong", "sample_size": 100, "sample_sha256": HELDOUT_SAMPLE_SHA256}),
    ):
        changed = {**campaign, key: bad}
        with pytest.raises(ValueError, match=key):
            configuration_for_campaign(changed)


def test_campaign_creation_persists_frozen_configuration_and_rejects_unknown_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_params: list[tuple[object, ...]] = []

    class _CreateResult:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self._row = row

        def fetchone(self) -> dict[str, Any] | None:
            return self._row

    class _CreateConnection:
        def __enter__(self) -> _CreateConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...]) -> _CreateResult:
            if "SELECT cache_identifier" in query:
                return _CreateResult(None)
            if "INSERT INTO ai_annotation_campaigns" in query:
                captured_params.append(params)
                return _CreateResult(_hostility_campaign_row(name=str(params[0]), configuration_id=str(params[2])))
            raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: _CreateConnection(),
    )

    created_policy = create_campaign(
        _settings(), _principal(), name="Policy Run", corpus_id=1
    )
    assert created_policy["configuration_id"] == POLICY_CONFIGURATION_ID
    assert captured_params[0][2] == POLICY_CONFIGURATION_ID
    assert captured_params[0][3] == "policy"
    assert captured_params[0][6] is None
    assert captured_params[0][7] is None
    assert captured_params[0][9] == APPROVED_PRODUCTION_MODEL

    created_hostility = create_campaign(
        _settings(),
        _principal(),
        name="Hostility Validation",
        corpus_id=1,
        configuration_id=HOSTILITY_CONFIGURATION_ID,
    )
    assert created_hostility["configuration_id"] == HOSTILITY_CONFIGURATION_ID
    assert captured_params[1][2] == HOSTILITY_CONFIGURATION_ID
    assert captured_params[1][3] == "hostility"
    assert captured_params[1][4] == "australian_partisan_hostility"
    assert captured_params[1][5] == "0.2.0"
    assert captured_params[1][6] == HELDOUT_SAMPLE_ID
    assert captured_params[1][7] == HELDOUT_SAMPLE_SHA256
    assert captured_params[1][9] == HOSTILITY_MODEL
    assert captured_params[1][10] == HOSTILITY_PROMPT_VERSION
    assert captured_params[1][11] == PROMPT_SHA256
    assert captured_params[1][13] == CONTRACT_SHA256

    with pytest.raises(CampaignError, match="unknown approved"):
        create_campaign(
            _settings(),
            _principal(),
            name="Injected",
            corpus_id=1,
            configuration_id="arbitrary-config",
        )

    # Verify the FastAPI route signature does not accept prompt, hash, model, or sample parameters.
    sig = inspect.signature(campaign_create)
    assert set(sig.parameters) == {
        "request",
        "settings",
        "principal",
        "name",
        "corpus_id",
        "configuration_id",
    }


def test_hostility_campaign_uses_only_exact_heldout_sample() -> None:
    sample = load_frozen_sample(
        ROOT / "config/research_samples/hostility-heldout-100.json",
        expected_sample_id="hostility-heldout-100",
    )
    development = load_frozen_sample(
        ROOT / "config/research_samples/hostility-development-100.json",
        expected_sample_id="hostility-development-100",
    )
    assert sample.sample_sha256 == HELDOUT_SAMPLE_SHA256
    assert sample.sample_size == len(sample.turns) == 100
    assert len({turn.turn_key for turn in sample.turns}) == 100
    assert not ({turn.turn_key for turn in sample.turns} & {turn.turn_key for turn in development.turns})


def test_hostility_planning_preserves_frozen_order_and_rejects_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = load_frozen_sample(
        ROOT / "config/research_samples/hostility-heldout-100.json",
        expected_sample_id="hostility-heldout-100",
    )
    # Return DB rows in reversed order to verify planning preserves frozen sample order.
    db_rows = [
        {
            "id": index + 1000,
            "turn_key": turn.turn_key,
            "date": "2026-01-01",
            "chamber": "House",
            "speaker_name_raw": f"Speaker {index}",
            "major_heading_original": "MATTERS OF PUBLIC IMPORTANCE",
            "minor_heading_original": None,
            "business_type": "matters_of_public_importance",
            "text_clean": f"Speech text for {turn.turn_key}.",
            "text_raw": None,
        }
        for index, turn in reversed(list(enumerate(sample.turns)))
    ]

    class _InsertCursor:
        def __init__(self) -> None:
            self.inserted: list[tuple[object, ...]] = []

        def executemany(self, _query: str, values: list[tuple[object, ...]]) -> None:
            self.inserted.extend(values)

    class _PlanResultRow:
        def __init__(self, row: dict[str, Any] | list[dict[str, Any]]) -> None:
            self.value = row

        def fetchone(self) -> dict[str, Any]:
            assert isinstance(self.value, dict)
            return self.value

        def fetchall(self) -> list[dict[str, Any]]:
            assert isinstance(self.value, list)
            return self.value

    class _HostilityPlanConnection:
        def __init__(self) -> None:
            self.campaign = _hostility_campaign_row()
            self.insert_cursor = _InsertCursor()
            self.queries: list[str] = []

        def __enter__(self) -> _HostilityPlanConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self, name: str | None = None) -> _InsertCursor:
            assert name is None, "policy server-side cursor must not be used for hostility"
            return self.insert_cursor

        def execute(self, query: str, params: object = None) -> _PlanResultRow:
            self.queries.append(query)
            if "FOR UPDATE" in query:
                return _PlanResultRow(self.campaign)
            if "turn.turn_key=ANY(%s)" in query:
                return _PlanResultRow(db_rows)
            if "SELECT count(*) AS count FROM speaker_turns" in query:
                return _PlanResultRow({"count": 160_577})
            if "UPDATE ai_annotation_campaigns SET status='PLANNED'" in query:
                assert isinstance(params, tuple)
                selection = params[0].obj
                self.campaign = {
                    **self.campaign,
                    "status": "PLANNED",
                    "selection_definition": selection,
                    "record_set_sha256": params[1],
                    "corpus_total_count": params[2],
                    "eligible_count": 100,
                    "planned_count": 100,
                }
                return _PlanResultRow(self.campaign)
            raise AssertionError(f"unexpected query during hostility planning: {query}")

    conn = _HostilityPlanConnection()
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service._planning_breakdown",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("policy breakdown called")),
    )

    # Non-100 planned_count is rejected server-side even if submitted via POST.
    with pytest.raises(CampaignError, match="planned_count=100"):
        plan_campaign(
            _settings(),
            _principal(),
            str(conn.campaign["public_id"]),
            limit=10,
            seed="ignored",
            exclude_human_annotated=True,
            _connection=conn,  # type: ignore[arg-type]
        )

    planned = plan_campaign(
        _settings(),
        _principal(),
        str(conn.campaign["public_id"]),
        limit=100,
        seed="frozen",
        exclude_human_annotated=False,
        _connection=conn,  # type: ignore[arg-type]
    )
    expected_record_ids = [f"turn:{turn.turn_key}" for turn in sample.turns]
    assert planned.campaign["status"] == "PLANNED"
    assert planned.campaign["planned_count"] == 100
    assert planned.campaign["record_set_sha256"] == canonical_hash(expected_record_ids)
    assert planned.campaign["selection_definition"] == {
        "mode": "frozen_sample",
        "sample_id": HELDOUT_SAMPLE_ID,
        "sample_size": 100,
        "sample_sha256": HELDOUT_SAMPLE_SHA256,
        "sample_version": sample.effective_version,
        "ordering": "frozen sample_order",
    }
    assert [row[2] for row in conn.insert_cursor.inserted] == expected_record_ids
    assert [row[3] for row in conn.insert_cursor.inserted] == list(range(100))

    # Reject if held-out sample SHA or count differs.
    conn_bad_sha = _HostilityPlanConnection()
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.load_frozen_sample",
        lambda path, expected_sample_id=None: sample.model_copy(
            update={"sample_sha256": "f" * 64}
        )
        if expected_sample_id == HELDOUT_SAMPLE_ID
        else sample,
    )
    with pytest.raises(CampaignError, match="frozen hostility sample identity"):
        plan_campaign(
            _settings(),
            _principal(),
            str(conn_bad_sha.campaign["public_id"]),
            limit=100,
            seed="frozen",
            exclude_human_annotated=False,
            _connection=conn_bad_sha,  # type: ignore[arg-type]
        )

    # Reject if development sample overlaps held-out sample.
    conn_overlap = _HostilityPlanConnection()
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.load_frozen_sample",
        lambda path, expected_sample_id=None: sample
        if expected_sample_id == HELDOUT_SAMPLE_ID
        else sample.model_copy(
            update={
                "sample_id": "hostility-development-100",
                "purpose": "HOSTILITY_DEVELOPMENT",
            }
        ),
    )
    with pytest.raises(CampaignError, match="overlaps development data"):
        plan_campaign(
            _settings(),
            _principal(),
            str(conn_overlap.campaign["public_id"]),
            limit=100,
            seed="frozen",
            exclude_human_annotated=False,
            _connection=conn_overlap,  # type: ignore[arg-type]
        )


def test_hostility_provider_plan_and_submission_use_hostility_contract_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = tuple(
        {
            "record_id": f"turn:key-{index:03d}",
            "speech": {
                "date": "2026-01-01",
                "chamber": "House",
                "speaker_name": "Speaker",
                "major_heading": "Debate",
                "minor_heading": None,
                "business_type": None,
                "text": f"Those opposite have betrayed Australians {index}.",
            },
        }
        for index in range(100)
    )
    record_ids_sha = canonical_hash([record["record_id"] for record in records])
    campaign = _hostility_campaign_row(
        status="READY",
        planned_count=100,
        record_set_sha256=record_ids_sha,
        cache_status="REUSABLE",
        cache_identifier="cachedContents/hostility-v2",
        cache_content_sha256=PROMPT_SHA256,
        cache_model=f"models/{HOSTILITY_MODEL}",
        cache_display_name=f"phase4d-{FROZEN_PROMPT_VERSION}-{PROMPT_SHA256[:16]}",
        cache_verified_at=datetime.now(UTC),
        cache_expires_at=datetime.now(UTC) + timedelta(hours=24),
        selection_definition={
            "mode": "frozen_sample",
            "sample_id": HELDOUT_SAMPLE_ID,
            "sample_size": 100,
            "sample_sha256": HELDOUT_SAMPLE_SHA256,
            "ordering": "frozen sample_order",
        },
    )

    plan = _provider_plan(campaign, records)
    assert plan["provenance"]["model"] == "gemini-2.5-flash"
    assert plan["provenance"]["prompt_version"] == "hostility-prompt-v2"
    assert plan["provenance"]["assembled_prompt_sha256"] == PROMPT_SHA256
    assert plan["provenance"]["prediction_contract_sha256"] == CONTRACT_SHA256
    assert plan["provenance"]["thinking_budget"] == 0
    assert "thinking_level" not in plan["provenance"]

    first_req = plan["request"]["batch"]["input_config"]["requests"]["requests"][0]["request"]
    assert first_req["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    props = set(first_req["generationConfig"]["responseSchema"]["properties"])
    assert props == HOSTILITY_PREDICTION_FIELDS
    assert "primary_australian_domain" not in props

    # validate_production_plan accepts valid hostility V2 plan and rejects tampered fields.
    validate_production_plan(plan)
    with pytest.raises(ValueError, match="unexpected model"):
        validate_production_plan(
            {**plan, "provenance": {**plan["provenance"], "model": "gemini-3.1-flash-lite"}}
        )
    with pytest.raises(ValueError, match="unexpected assembled_prompt_sha256"):
        validate_production_plan(
            {**plan, "provenance": {**plan["provenance"], "assembled_prompt_sha256": "0" * 64}}
        )

    # _provider_plan rejects hostility campaign if record count != 100.
    with pytest.raises(CampaignError, match="exactly 100 records"):
        _provider_plan(campaign, records[:99])

    # End-to-end submit_campaign with mocked gateway.
    class _SubmitConn:
        def __init__(self, row: dict[str, Any]) -> None:
            self.row = row

        def __enter__(self) -> _SubmitConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: object = None) -> Any:
            class _R:
                def __init__(self, val: dict[str, Any]) -> None:
                    self.val = val

                def fetchone(self) -> dict[str, Any]:
                    return self.val

            if "cache_status=%s,cache_identifier=%s" in query:
                return _R(self.row)
            if "SET status='SUBMITTED'" in query:
                assert isinstance(params, tuple)
                self.row = {**self.row, "status": "SUBMITTED", "provider_job_id": params[0]}
                return _R(self.row)
            return _R(self.row)

    class _HostilityGateway:
        def __init__(self) -> None:
            self.submitted_models: list[str] = []

        def create_cache(self, _request: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("cache was already reusable")

        def get_cache(self, cache_identifier: str) -> dict[str, Any]:
            now = datetime.now(UTC)
            return {
                "name": cache_identifier,
                "model": f"models/{HOSTILITY_MODEL}",
                "displayName": f"phase4d-{FROZEN_PROMPT_VERSION}-{PROMPT_SHA256[:16]}",
                "createTime": (now - timedelta(minutes=5)).isoformat(),
                "expireTime": (now + timedelta(hours=20)).isoformat(),
            }

        def submit(self, submitted_plan: dict[str, Any], model: str) -> str:
            validate_production_plan(submitted_plan)
            self.submitted_models.append(model)
            return "batches/hostility-100"

        def status(self, _job_id: str) -> dict[str, Any]:
            raise AssertionError("unexpected status call")

        def find_batch(self, _display_name: str, _model: str) -> str | None:
            return None

    conn = _SubmitConn(campaign)
    gateway = _HostilityGateway()
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: conn,
    )
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service._records_for_campaign",
        lambda *_args: records,
    )
    submitted = submit_campaign(
        _settings(), _principal(), str(campaign["public_id"]), PAID_CONFIRMATION, gateway=gateway
    )
    assert submitted["status"] == "SUBMITTED"
    assert submitted["provider_job_id"] == "batches/hostility-100"
    assert gateway.submitted_models == ["gemini-2.5-flash"]


def test_hostility_collection_validation_and_import_use_hostility_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HANSARD_CAMPAIGN_ARTIFACT_ROOT", str(tmp_path))
    records = tuple(
        {
            "record_id": f"turn:key-{index:03d}",
            "speech": {
                "date": "2026-01-01",
                "chamber": "House",
                "speaker_name": "Speaker",
                "major_heading": "Debate",
                "minor_heading": None,
                "business_type": None,
                "text": "The opposition'sreckless policy is a disgrace. Wait: those opposite failed Australians.",
            },
        }
        for index in range(100)
    )
    record_ids_sha = canonical_hash([record["record_id"] for record in records])
    campaign = _hostility_campaign_row(
        status="PROVIDER_COMPLETED",
        planned_count=100,
        record_set_sha256=record_ids_sha,
        provider_job_id="batches/hostility-100",
        cache_status="REUSABLE",
        cache_identifier="cachedContents/hostility-v2",
        selection_definition={
            "mode": "frozen_sample",
            "sample_id": HELDOUT_SAMPLE_ID,
            "sample_size": 100,
            "sample_sha256": HELDOUT_SAMPLE_SHA256,
            "ordering": "frozen sample_order",
        },
    )

    inlined_responses = [
        {
            "metadata": {"key": record["record_id"]},
            "response": {
                "modelVersion": "gemini-2.5-flash",
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "hostility_present": "YES",
                                            "target": "OPPOSING_PARTISAN_TARGET",
                                            "evidence_spans": [
                                                {"text": "those opposite failed Australians."}
                                            ],
                                            "reasoning": "Direct attack on the opposition.",
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4200,
                    "cachedContentTokenCount": 4000,
                    "candidatesTokenCount": 45,
                    "thoughtsTokenCount": 0,
                    "totalTokenCount": 4245,
                },
            },
        }
        for record in records
    ]

    class _CollectGateway:
        def create_cache(self, _req: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError

        def get_cache(self, _id: str) -> dict[str, Any]:
            raise AssertionError

        def submit(self, _plan: dict[str, Any], _model: str) -> str:
            raise AssertionError

        def status(self, job_id: str) -> dict[str, Any]:
            return {
                "name": job_id,
                "metadata": {
                    "state": "BATCH_STATE_SUCCEEDED",
                    "batchStats": {
                        "requestCount": "100",
                        "successfulRequestCount": "100",
                        "failedRequestCount": "0",
                    },
                },
                "response": {
                    "output": {
                        "inlinedResponses": {"inlinedResponses": inlined_responses}
                    }
                },
            }

        def find_batch(self, _display_name: str, _model: str) -> str | None:
            return None

    class _LifecycleConn:
        def __init__(self, row: dict[str, Any]) -> None:
            self.row = row

        def __enter__(self) -> _LifecycleConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: object = None) -> Any:
            class _Res:
                def __init__(self, val: Any) -> None:
                    self.val = val

                def fetchone(self) -> Any:
                    return self.val

                def fetchall(self) -> list[Any]:
                    return []

            if "SET status='COLLECTED'" in query:
                assert isinstance(params, tuple)
                self.row = {
                    **self.row,
                    "status": "COLLECTED",
                    "artifact_reference": params[0],
                    "artifact_sha256": params[1],
                    "provider_success_count": params[2],
                    "provider_failure_count": params[3],
                    "valid_count": params[4],
                    "invalid_count": params[5],
                    "operational_metadata": params[6].obj,
                }
                return _Res(self.row)
            if "SET status='VALIDATED'" in query:
                assert isinstance(params, tuple)
                self.row = {
                    **self.row,
                    "status": "VALIDATED",
                    "valid_count": params[0],
                    "invalid_count": params[1],
                }
                return _Res(self.row)
            if "FROM model_prediction_runs" in query:
                return _Res({"id": 777})
            if "SET status='IMPORTED'" in query:
                assert isinstance(params, tuple)
                self.row = {
                    **self.row,
                    "status": "IMPORTED",
                    "model_prediction_run_id": params[0],
                }
                return _Res(self.row)
            return _Res(self.row)

    conn = _LifecycleConn(campaign)
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: conn,
    )
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service._records_for_campaign",
        lambda *_args: records,
    )

    collected = collect_campaign(
        _settings(), _principal(), str(campaign["public_id"]), gateway=_CollectGateway()
    )
    assert collected["status"] == "COLLECTED"
    assert collected["valid_count"] == 100
    assert collected["invalid_count"] == 0

    artifact_dir = Path(str(collected["artifact_reference"]))
    first_prediction_row = json.loads(
        (artifact_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    pred = first_prediction_row["prediction"]
    assert pred["hostility_present"] == "YES"
    assert pred["target"] == "OPPOSING_PARTISAN_TARGET"
    assert pred["evidence_spans"] == [{"text": "those opposite failed Australians."}]
    assert "is_non_policy" not in pred
    assert "primary_australian_domain" not in pred

    insert_values = _prediction_insert_values(
        run_id=777,
        directory=artifact_dir,
        rows=[first_prediction_row],
        turns={
            "key-000": {
                "id": 1000,
                "turn_key": "key-000",
                "text_clean": cast(dict[str, Any], records[0]["speech"])["text"],
                "text_raw": None,
            }
        },
        prediction_task="hostility",
    )
    assert len(insert_values) == 1
    row_tuple = insert_values[0]
    assert row_tuple[3] == "hostility"
    assert row_tuple[4:7] == (None, None, None)
    canonical_values = cast(Any, row_tuple[7]).obj
    assert canonical_values["hostility_present"] == "YES"
    assert canonical_values["target"] == "OPPOSING_PARTISAN_TARGET"
    assert canonical_values["evidence_spans"][0]["text"] == "those opposite failed Australians."
    assert isinstance(canonical_values["evidence_spans"][0]["start_offset"], int)
    assert isinstance(canonical_values["evidence_spans"][0]["end_offset"], int)

    validated = validate_campaign(_settings(), _principal(), str(campaign["public_id"]))
    assert validated["status"] == "VALIDATED"
    assert validated["valid_count"] == 100

    imported_calls: list[dict[str, Any]] = []

    def _fake_import(
        directory: Path,
        _db: DatabaseSettings,
        *,
        dry_run: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert not dry_run
        imported_calls.append({"directory": directory, **kwargs})
        return {"artifact_sha256": collected["artifact_sha256"]}

    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.import_predictions", _fake_import
    )
    imported = import_campaign(_settings(), _principal(), str(campaign["public_id"]))
    assert imported["status"] == "IMPORTED"
    assert imported["model_prediction_run_id"] == 777
    assert len(imported_calls) == 1
    assert imported_calls[0]["prediction_task"] == "hostility"
    assert imported_calls[0]["annotation_schema_slug"] == "australian_partisan_hostility"
    assert imported_calls[0]["annotation_schema_version"] == "0.2.0"


def test_policy_only_repair_and_phase4e_reject_hostility_campaigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _hostility_campaign_row(status="COLLECTED", planned_count=100, invalid_count=1)

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: object = None) -> Any:
            class _R:
                def fetchone(self) -> dict[str, Any]:
                    return campaign

            return _R()

    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: _Conn(),
    )
    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.phase4e.psycopg.connect",
        lambda *_args, **_kwargs: _Conn(),
    )

    with pytest.raises(CampaignError, match="only supported for policy campaigns"):
        repair_campaign_results(_settings(), _principal(), str(campaign["public_id"]))

    campaign = _hostility_campaign_row(
        status="PLANNED",
        planned_count=100,
        orchestration_mode="LEGACY",
        orchestration_status="PLANNED",
        selection_definition={
            "mode": "frozen_sample",
            "sample_id": HELDOUT_SAMPLE_ID,
            "sample_size": 100,
            "sample_sha256": HELDOUT_SAMPLE_SHA256,
            "exclude_human_annotated": False,
        },
    )
    with pytest.raises(CampaignError, match="only supported for policy campaigns"):
        plan_campaign_chunks(
            _settings(),
            _principal(),
            str(campaign["public_id"]),
            enqueued_token_ceiling=10_000_000,
            safety_factor=0.9,
            max_chunk_records=100,
        )
    with pytest.raises(CampaignError, match="only supported for policy campaigns"):
        authorize_full_production(
            _settings(),
            _principal(),
            str(campaign["public_id"]),
            confirmation="START FULL PRODUCTION",
            spend_ceiling_usd=Decimal("5.00"),
        )


def test_webui_templates_render_task_aware_new_and_detail_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _jinja_env()
    new_html = env.get_template("campaigns/new.html").render(
        principal=_principal(),
        corpora=[{"id": 1, "name": "Australian Commonwealth Hansard 1998-2025"}],
        configuration=production_configuration(),
        configurations=approved_campaign_configurations(),
        credentials=credential_status(),
    )
    assert "Policy classification — Frozen V2" in new_html
    assert "Targeted hostility — Frozen V2" in new_html
    assert "australian_partisan_hostility" in new_html
    assert "hostility-prompt-v2" in new_html
    assert "gemini-2.5-flash" in new_html
    assert "Hostility held-out 100" in new_html
    assert 'name="configuration_id"' in new_html
    assert 'name="prompt_version"' not in new_html
    assert 'name="configured_model"' not in new_html
    assert 'name="sample_sha256"' not in new_html

    # Test get_campaign + detail.html for a READY hostility campaign.
    ready_row = _hostility_campaign_row(
        status="READY",
        planned_count=100,
        eligible_count=100,
        corpus_total_count=160_577,
        record_set_sha256="a" * 64,
        selection_definition={
            "mode": "frozen_sample",
            "sample_id": HELDOUT_SAMPLE_ID,
            "sample_size": 100,
            "sample_sha256": HELDOUT_SAMPLE_SHA256,
            "ordering": "frozen sample_order",
        },
    )

    class _DetailConn:
        def __enter__(self) -> _DetailConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, _params: object = None) -> Any:
            class _R:
                def __init__(self, val: Any) -> None:
                    self.val = val

                def fetchone(self) -> Any:
                    return self.val

                def fetchall(self) -> list[Any]:
                    return self.val if isinstance(self.val, list) else []

            if "FROM ai_annotation_campaigns campaign" in query:
                return _R(ready_row)
            if "FROM ai_annotation_campaign_batches" in query and "count(*)" in query:
                return _R({"chunks_total": 0})
            return _R([])

    monkeypatch.setattr(
        "hansard_annotator.web.campaigns.service.psycopg.connect",
        lambda *_args, **_kwargs: _DetailConn(),
    )
    detail_campaign = get_campaign(_settings(), _principal(), str(ready_row["public_id"]))
    assert detail_campaign["selection_current"] is True
    assert detail_campaign["configuration_display_label"] == "Targeted hostility — Frozen V2"
    assert detail_campaign["record_set_mode"] == "frozen_sample"
    assert detail_campaign["approved_record_set_size"] == 100

    detail_html = env.get_template("campaigns/detail.html").render(
        principal=_principal(),
        campaign=detail_campaign,
        credentials=credential_status(),
        notice_message=None,
        phase4e_defaults={
            "enqueued_token_ceiling": 10_000_000,
            "safety_factor": 0.9,
            "max_chunk_records": 1000,
        },
    )
    assert "Targeted hostility — Frozen V2" in detail_html
    assert "hostility" in detail_html
    assert "australian_partisan_hostility 0.2.0" in detail_html
    assert "hostility-prompt-v2" in detail_html
    assert "gemini-2.5-flash" in detail_html
    assert "hostility-heldout-100" in detail_html
    assert HELDOUT_SAMPLE_SHA256 in detail_html
    assert "Frozen validation sample" in detail_html
    assert "Submit paid Batch" in detail_html
    assert "Already human annotated (exclusive)" not in detail_html
    assert "Phase 4E child Batch orchestration" not in detail_html
    assert "Deterministic structural repairs" not in detail_html
