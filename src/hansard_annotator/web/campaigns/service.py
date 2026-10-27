"""Persistent, recoverable orchestration around Phase 4B and Phase 4C services."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.llm_eval.costs import load_pricing
from hansard_annotator.llm_eval.hostility import prediction_json_schema as hostility_schema
from hansard_annotator.llm_eval.import_predictions import (
    import_predictions,
    inspect_prediction_artifact,
    iter_terminal_invalid_artifact,
    load_prediction_artifact,
)
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_GENERATION,
    APPROVED_PRODUCTION_MODEL,
    FROZEN_PROMPT_VERSION,
    batch_resource,
    build_batch_create_request,
    build_cache_create_request,
    canonical_hash,
    collect_inline_batch,
    gemini_batch_schema,
    parse_batch_status,
    production_record_from_turn,
    simulate_costs,
    submission_batch_name,
    validate_production_plan,
)
from hansard_annotator.llm_eval.ops_cli import (
    _collection_summary,
    _get_batch,
    _get_cache,
    _get_gemini_resource,
)
from hansard_annotator.llm_eval.prompt import (
    build_system_prompt,
    prediction_json_schema,
    prompt_hash,
)
from hansard_annotator.llm_eval.providers import _http_json
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.campaigns.configurations import (
    APPROVED_AI_CONFIGURATIONS,
    POLICY_CONFIGURATION_ID,
    ApprovedAIConfiguration,
    approved_configuration,
    configuration_for_campaign,
)
from hansard_annotator.web.campaigns.recovery import (
    RecoveryArtifactError,
    artifact_sha256,
    build_effective_artifact,
    build_effective_artifact_streaming,
    build_provider_refusal_artifact,
    build_structurally_repaired_artifact,
    iter_jsonl,
    provider_refusal_count,
    provider_refusal_directory,
    read_jsonl,
    structural_repair_count,
    structural_repair_directory,
)
from hansard_annotator.web.codebook.service import ROOT, production_codebook
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.tasks.frozen_samples import load_frozen_sample

PREDICTION_CONTRACT_SHA256 = "99cafa6c473293dc076793ee6baeca44b6961d7cb11de8f9174517defc4a0879"
GEMINI_BATCH_SCHEMA_SHA256 = "980b0ed6d6002a8eadd326f84942ba60dcdf8f2abd9e70ea6aaecf7d78de3c22"
PAID_CONFIRMATION = "START AI CAMPAIGN"
RECOVERY_CONFIRMATION = "RETRY INVALID RECORDS"
PRODUCTION_GENERATION_CONFIG = APPROVED_PRODUCTION_GENERATION
PRODUCTION_EXECUTION_MODE = "batch"
DEFAULT_EXCLUDE_HUMAN_ANNOTATED = True
AI_ELIGIBILITY_RULE = "prompt_text_present_v1"
OTHER_EXCLUSION_LABELS = {
    "missing_prompt_text": "No cleaned or raw speech text",
    "unmapped_business_heading": (
        "Legacy main-analysis rule: business heading has no reviewed mapping"
    ),
    "committees_procedural_status_unknown": (
        "Legacy main-analysis rule: Committees procedural status is indeterminate"
    ),
    "motions_procedural_status_unknown": (
        "Legacy main-analysis rule: Motions procedural status is indeterminate"
    ),
    "parliamentary_representation_ceremonial_status_unknown": (
        "Legacy main-analysis rule: Parliamentary Representation ceremonial status "
        "is indeterminate"
    ),
    "legacy_business_mapping_indeterminate": (
        "Legacy main-analysis rule: business mapping eligibility is indeterminate"
    ),
    "under_50_words": "Legacy main-analysis rule: fewer than 50 words",
    "unknown_speaker": "Legacy main-analysis rule: speaker identity is unknown",
    "collective_speaker": "Legacy main-analysis rule: collective speaker",
    "procedural_business": "Legacy main-analysis rule: procedural business",
    "ceremonial_business": "Legacy main-analysis rule: ceremonial business",
    "orphan_continuation": "Legacy main-analysis rule: orphan continuation",
}

# Calibrated from the successful 100-record Phase 4B schema-fix validation run.
# Calibrated from the frozen 120-record Gemini 3.1 Flash-Lite benchmark.  The
# cached prefix token count is the benchmark's locally recorded planning value;
# a live campaign retains the provider-reported cache metadata separately.
INPUT_TOKENS_PER_RECORD = 581870 / 120
OUTPUT_TOKENS_PER_RECORD = 13687 / 120
EXPLICIT_CACHE_TOKENS = 4131
CACHED_INPUT_SHARE = EXPLICIT_CACHE_TOKENS / INPUT_TOKENS_PER_RECORD
EXPLICIT_CACHE_TTL_HOURS = 24.0
EXPLICIT_CACHE_TTL_SECONDS = int(EXPLICIT_CACHE_TTL_HOURS * 3600)
CACHE_REUSE_MINIMUM = timedelta(hours=1)
PLANNING_CHUNK_SIZE = 1_000

STATES = {
    "DRAFT",
    "PLANNED",
    "READY",
    "SUBMITTED",
    "RUNNING",
    "PROVIDER_COMPLETED",
    "COLLECTED",
    "VALIDATED",
    "IMPORTED",
    "FAILED",
    "CANCELLED",
}
TRANSITIONS = {
    "DRAFT": {"PLANNED", "CANCELLED"},
    "PLANNED": {"READY", "CANCELLED"},
    "READY": {"SUBMITTED", "CANCELLED"},
    "SUBMITTED": {"RUNNING", "PROVIDER_COMPLETED", "FAILED"},
    "RUNNING": {"RUNNING", "PROVIDER_COMPLETED", "FAILED"},
    "PROVIDER_COMPLETED": {"COLLECTED", "FAILED"},
    "COLLECTED": {"VALIDATED", "FAILED"},
    "VALIDATED": {"IMPORTED", "FAILED"},
    "FAILED": {"RUNNING", "PROVIDER_COMPLETED", "COLLECTED", "VALIDATED"},
    "CANCELLED": set(),
    "IMPORTED": set(),
}


class CampaignError(ValueError):
    """Campaign state, identity, or lifecycle invariant failed."""


def validate_transition(current: str, target: str) -> None:
    if current not in STATES or target not in STATES:
        raise CampaignError("unknown campaign state")
    if target not in TRANSITIONS[current]:
        raise CampaignError(f"invalid campaign transition: {current} -> {target}")


def production_configuration() -> dict[str, Any]:
    configuration = approved_configuration(POLICY_CONFIGURATION_ID)
    return {
        key: value
        for key, value in configuration.persistence_values().items()
        if key in {
            "provider", "configured_model", "prompt_version", "prompt_sha256",
            "canonical_schema_sha256", "prediction_contract_sha256",
            "provider_schema_sha256", "generation_config", "execution_mode",
        }
    }


def approved_campaign_configurations() -> list[dict[str, Any]]:
    return [value.persistence_values() for value in APPROVED_AI_CONFIGURATIONS.values()]


def configuration_class(configuration: dict[str, Any]) -> str:
    try:
        configuration_for_campaign(configuration)
        return "APPROVED_PRODUCTION"
    except ValueError:
        return (
            "APPROVED_PRODUCTION"
            if same_prediction_configuration(configuration, production_configuration())
            else "RESEARCH"
        )


def same_prediction_configuration(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Match the identity used by SQL eligibility; run/campaign IDs are irrelevant."""
    keys = (
        "provider",
        "configured_model",
        "prompt_version",
        "prompt_sha256",
        "canonical_schema_sha256",
        "prediction_contract_sha256",
        "provider_schema_sha256",
        "generation_config",
        "execution_mode",
    )
    return all(left.get(key) == right.get(key) for key in keys)


def _cache_display_name(content_sha256: str) -> str:
    return f"phase4d-{FROZEN_PROMPT_VERSION}-{content_sha256[:16]}"


def _configuration_context(configuration: ApprovedAIConfiguration) -> dict[str, Any]:
    if configuration.prediction_task == "policy":
        return cast(dict[str, Any], production_codebook()["context"])
    path = ROOT / "config/llm_eval/hostility-v2-codebook-context.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CampaignError("frozen hostility codebook context is not an object")
    assembled = build_system_prompt(value, configuration.prompt_version)
    if prompt_hash(assembled) != configuration.prompt_sha256:
        raise CampaignError("frozen hostility codebook context hash differs")
    return value


def _campaign_configuration(campaign: dict[str, Any]) -> ApprovedAIConfiguration:
    try:
        return configuration_for_campaign(campaign)
    except ValueError as error:
        raise CampaignError(str(error)) from error


def _canonical_cache_plan(campaign: dict[str, Any]) -> dict[str, Any]:
    configuration = _campaign_configuration(campaign)
    context = _configuration_context(configuration)
    system_prompt = build_system_prompt(context, str(campaign["prompt_version"]))
    content_sha256 = prompt_hash(system_prompt)
    if content_sha256 != configuration.prompt_sha256 or content_sha256 != campaign["prompt_sha256"]:
        raise CampaignError("canonical cache content does not match frozen prompt provenance")
    display_name = _cache_display_name(content_sha256)
    return {
        "content_sha256": content_sha256,
        "display_name": display_name,
        "model": f"models/{campaign['configured_model']}",
        "system_prompt": system_prompt,
        "request": build_cache_create_request(
            model=str(campaign["configured_model"]),
            system_prompt=system_prompt,
            ttl_seconds=EXPLICIT_CACHE_TTL_SECONDS,
            display_name=display_name,
        ),
    }


def _provider_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CampaignError(f"Gemini cache response has no {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CampaignError(f"Gemini cache response has invalid {field}") from None
    if parsed.tzinfo is None:
        raise CampaignError(f"Gemini cache response has timezone-free {field}")
    return parsed.astimezone(UTC)


def _cache_resource_fields(
    resource: dict[str, Any], plan: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    name = resource.get("name")
    if not isinstance(name, str) or not name.startswith("cachedContents/"):
        raise CampaignError("Gemini cache response has an invalid resource name")
    if resource.get("model") != plan["model"]:
        raise CampaignError("Gemini cache model does not match the campaign model")
    if resource.get("displayName") != plan["display_name"]:
        raise CampaignError("Gemini cache display identity does not match canonical content")
    created_at = _provider_time(resource.get("createTime"), "createTime")
    expires_at = _provider_time(resource.get("expireTime"), "expireTime")
    if expires_at <= now + CACHE_REUSE_MINIMUM:
        raise CampaignError("Gemini cache is expired or has insufficient remaining lifetime")
    instruction = resource.get("systemInstruction")
    if instruction is not None:
        parts = instruction.get("parts") if isinstance(instruction, dict) else None
        text = parts[0].get("text") if isinstance(parts, list) and len(parts) == 1 else None
        if text != plan["system_prompt"]:
            raise CampaignError("Gemini cache returned incompatible system instruction content")
    safe_metadata = {
        key: resource[key]
        for key in ("updateTime", "usageMetadata")
        if key in resource
    }
    return {
        "cache_status": "REUSABLE",
        "cache_identifier": name,
        "cache_content_sha256": plan["content_sha256"],
        "cache_model": plan["model"],
        "cache_display_name": plan["display_name"],
        "cache_created_at": created_at,
        "cache_expires_at": expires_at,
        "cache_verified_at": now,
        "cache_metadata": safe_metadata,
    }


def cache_plan_status(campaign: dict[str, Any], *, now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if not campaign.get("cache_identifier"):
        return "REQUIRED"
    expires = campaign.get("cache_expires_at")
    try:
        configuration = _campaign_configuration(campaign)
    except CampaignError:
        return "UNAVAILABLE"
    compatible = (
        campaign.get("cache_content_sha256") == configuration.prompt_sha256
        and campaign.get("cache_model") == f"models/{campaign.get('configured_model')}"
        and campaign.get("cache_display_name") == _cache_display_name(configuration.prompt_sha256)
        and campaign.get("cache_verified_at") is not None
        and isinstance(expires, datetime)
        and expires.astimezone(UTC) > current + CACHE_REUSE_MINIMUM
    )
    return "REUSABLE" if compatible else "UNAVAILABLE"


def deterministic_selection(
    rows: list[dict[str, Any]], *, limit: int, seed: str
) -> tuple[list[dict[str, Any]], str]:
    if limit < 1:
        raise CampaignError("planned count must be positive")
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        turn_key = str(row["turn_key"])
        if turn_key in unique:
            raise CampaignError(f"duplicate eligible turn key: {turn_key}")
        unique[turn_key] = row
    ordered = sorted(
        unique.values(),
        key=lambda row: (
            hashlib.sha256(f"{row['turn_key']}:{seed}".encode()).hexdigest(),
            str(row["turn_key"]),
        ),
    )[:limit]
    record_ids = [f"turn:{row['turn_key']}" for row in ordered]
    return ordered, canonical_hash(record_ids)


def other_exclusion_reason(row: dict[str, Any]) -> str | None:
    """Return a content-neutral reason that prevents a usable model prompt."""
    text = row.get("text_clean") or row.get("text_raw")
    if not isinstance(text, str) or not text.strip():
        return "missing_prompt_text"
    return None


def selection_breakdown(
    rows: list[dict[str, Any]], *, exclude_human_annotated: bool
) -> dict[str, Any]:
    """Return disjoint plan buckets plus factual overlap diagnostics."""
    same_configuration = 0
    human_annotated = 0
    human_annotated_excluded = 0
    other_excluded = 0
    eligible = 0
    human_prediction_overlap = 0
    other_reasons: Counter[str] = Counter()
    for row in rows:
        predicted = bool(row["already_predicted"])
        human = bool(row["human_annotated"])
        human_annotated += int(human)
        human_prediction_overlap += int(human and predicted)
        if predicted:
            same_configuration += 1
        elif exclude_human_annotated and human:
            human_annotated_excluded += 1
        elif reason := other_exclusion_reason(row):
            other_excluded += 1
            other_reasons[reason] += 1
        else:
            eligible += 1
    return {
        "corpus_total_count": len(rows),
        "human_annotated_count": human_annotated,
        "excluded_existing_prediction_count": same_configuration,
        "excluded_human_annotated_count": human_annotated_excluded,
        "excluded_other_count": other_excluded,
        "eligible_count": eligible,
        "human_prediction_overlap_count": human_prediction_overlap,
        "other_exclusion_reasons": dict(sorted(other_reasons.items())),
    }


def estimate_plan(
    records: int, *, model: str = APPROVED_PRODUCTION_MODEL
) -> dict[str, float | int]:
    pricing = load_pricing(ROOT / "config/llm_eval/pricing.yaml")
    entry = pricing["models"][f"gemini:{model}"]
    rates = entry["modes"]["batch"]
    estimate = simulate_costs(
        records=records,
        input_tokens_per_record=INPUT_TOKENS_PER_RECORD,
        output_tokens_per_record=OUTPUT_TOKENS_PER_RECORD,
        cached_share=CACHED_INPUT_SHARE,
        input_rate=float(rates["input_per_million_usd"]),
        cached_rate=float(rates["cached_input_per_million_usd"]),
        output_rate=float(rates["output_per_million_usd"]),
        explicit_cache_tokens=EXPLICIT_CACHE_TOKENS,
        ttl_hours=EXPLICIT_CACHE_TTL_HOURS,
        storage_rate=float(entry["cache_storage_per_million_token_hours_usd"]),
    )
    estimate["input_tokens"] = round(INPUT_TOKENS_PER_RECORD * records)
    return estimate


class _RecordSetHasher:
    """Incrementally reproduce canonical_hash(list[str]) without retaining the list."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self._count = 0

    def add(self, record_id: str) -> None:
        if self._count:
            self._digest.update(b",")
        rendered = json.dumps(record_id, ensure_ascii=False, separators=(",", ":"))
        self._digest.update(rendered.encode("utf-8"))
        self._count += 1

    def hexdigest(self) -> str:
        digest = self._digest.copy()
        digest.update(b"]")
        return digest.hexdigest()


def _configuration_query_parameters(campaign: dict[str, Any]) -> tuple[object, ...]:
    return (
        campaign["provider"],
        campaign["configured_model"],
        campaign["prompt_version"],
        campaign["prompt_sha256"],
        campaign["canonical_schema_sha256"],
        campaign["prediction_contract_sha256"],
        campaign["provider_schema_sha256"],
        Jsonb(campaign["generation_config"]),
        campaign["execution_mode"],
    )


def _planning_breakdown(
    connection: psycopg.Connection[dict[str, Any]],
    campaign: dict[str, Any],
    *,
    exclude_human_annotated: bool,
    retry_provider_refusals: bool = False,
) -> dict[str, Any]:
    row = connection.execute(
        """
        WITH base AS MATERIALIZED (
          SELECT
            (EXISTS (
              SELECT 1 FROM model_predictions prediction
              JOIN model_prediction_runs run ON run.id=prediction.run_id
              WHERE prediction.speaker_turn_id=turn.id
                AND run.provider=%s AND run.configured_model=%s
                AND run.prompt_version=%s AND run.prompt_sha256=%s
                AND run.canonical_schema_sha256=%s
                AND run.prediction_contract_sha256=%s
                AND run.provider_schema_sha256=%s
                AND run.generation_config @> %s
                AND run.execution_mode=%s
            ) OR (NOT %s AND EXISTS (
              SELECT 1 FROM model_prediction_abstentions abstention
              JOIN model_prediction_runs run ON run.id=abstention.run_id
              WHERE abstention.speaker_turn_id=turn.id
                AND run.provider=%s AND run.configured_model=%s
                AND run.prompt_version=%s AND run.prompt_sha256=%s
                AND run.canonical_schema_sha256=%s
                AND run.prediction_contract_sha256=%s
                AND run.provider_schema_sha256=%s
                AND run.generation_config @> %s
                AND run.execution_mode=%s
            ))) AS already_predicted,
            EXISTS (
              SELECT 1 FROM tasks task
              JOIN assignments assignment ON assignment.task_id=task.id
              JOIN annotations annotation ON annotation.assignment_id=assignment.id
              WHERE task.speaker_turn_id=turn.id
                AND annotation.status IN ('submitted','revised')
            ) AS human_annotated,
            CASE
              WHEN turn.text_clean IS NOT NULL AND turn.text_clean<>''
                THEN btrim(turn.text_clean)<>''
              WHEN turn.text_raw IS NOT NULL AND turn.text_raw<>''
                THEN btrim(turn.text_raw)<>''
              ELSE false
            END AS prompt_text_present
          FROM speaker_turns turn
          JOIN reconstruction_runs reconstruction
            ON reconstruction.id=turn.reconstruction_run_id
          JOIN corpora corpus ON corpus.current_preprocessing_run_id=
            reconstruction.preprocessing_run_id
          WHERE corpus.id=%s
        )
        SELECT
          count(*)::integer AS corpus_total_count,
          count(*) FILTER (WHERE human_annotated)::integer AS human_annotated_count,
          count(*) FILTER (
            WHERE human_annotated AND already_predicted
          )::integer AS human_prediction_overlap_count,
          count(*) FILTER (
            WHERE already_predicted
          )::integer AS excluded_existing_prediction_count,
          count(*) FILTER (
            WHERE NOT already_predicted AND %s AND human_annotated
          )::integer AS excluded_human_annotated_count,
          count(*) FILTER (
            WHERE NOT already_predicted
              AND (NOT %s OR NOT human_annotated)
              AND NOT prompt_text_present
          )::integer AS excluded_other_count,
          count(*) FILTER (
            WHERE NOT already_predicted
              AND (NOT %s OR NOT human_annotated)
              AND prompt_text_present
          )::integer AS eligible_count
        FROM base
        """,
        (
            *_configuration_query_parameters(campaign),
            retry_provider_refusals,
            *_configuration_query_parameters(campaign),
            campaign["corpus_id"],
            exclude_human_annotated,
            exclude_human_annotated,
            exclude_human_annotated,
        ),
    ).fetchone()
    assert row is not None
    result = dict(row)
    result["other_exclusion_reasons"] = (
        {"missing_prompt_text": result["excluded_other_count"]}
        if result["excluded_other_count"]
        else {}
    )
    return result


def _selection_query() -> str:
    return """
        SELECT turn.id,turn.turn_key,turn.speech_date AS date,turn.chamber,
               turn.speaker_name_raw,turn.major_heading_original,
               turn.minor_heading_original,turn.business_type,
               turn.text_clean,turn.text_raw
        FROM speaker_turns turn
        JOIN reconstruction_runs reconstruction
          ON reconstruction.id=turn.reconstruction_run_id
        JOIN corpora corpus ON corpus.current_preprocessing_run_id=
          reconstruction.preprocessing_run_id
        WHERE corpus.id=%s
          AND NOT EXISTS (
            SELECT 1 FROM model_predictions prediction
            JOIN model_prediction_runs run ON run.id=prediction.run_id
            WHERE prediction.speaker_turn_id=turn.id
              AND run.provider=%s AND run.configured_model=%s
              AND run.prompt_version=%s AND run.prompt_sha256=%s
              AND run.canonical_schema_sha256=%s
              AND run.prediction_contract_sha256=%s
              AND run.provider_schema_sha256=%s
              AND run.generation_config @> %s
              AND run.execution_mode=%s
          )
          AND (%s OR NOT EXISTS (
            SELECT 1 FROM model_prediction_abstentions abstention
            JOIN model_prediction_runs run ON run.id=abstention.run_id
            WHERE abstention.speaker_turn_id=turn.id
              AND run.provider=%s AND run.configured_model=%s
              AND run.prompt_version=%s AND run.prompt_sha256=%s
              AND run.canonical_schema_sha256=%s
              AND run.prediction_contract_sha256=%s
              AND run.provider_schema_sha256=%s
              AND run.generation_config @> %s
              AND run.execution_mode=%s
          ))
          AND (NOT %s OR NOT EXISTS (
            SELECT 1 FROM tasks task
            JOIN assignments assignment ON assignment.task_id=task.id
            JOIN annotations annotation ON annotation.assignment_id=assignment.id
            WHERE task.speaker_turn_id=turn.id
              AND annotation.status IN ('submitted','revised')
          ))
          AND CASE
            WHEN turn.text_clean IS NOT NULL AND turn.text_clean<>''
              THEN btrim(turn.text_clean)<>''
            WHEN turn.text_raw IS NOT NULL AND turn.text_raw<>''
              THEN btrim(turn.text_raw)<>''
            ELSE false
          END
        ORDER BY
          sha256(convert_to(turn.turn_key || ':' || %s,'UTF8')),
          turn.turn_key COLLATE "C"
        LIMIT %s
    """


def _selection_query_parameters(
    campaign: dict[str, Any],
    *,
    exclude_human_annotated: bool,
    retry_provider_refusals: bool = False,
    seed: str,
    limit: int,
) -> tuple[object, ...]:
    return (
        campaign["corpus_id"],
        *_configuration_query_parameters(campaign),
        retry_provider_refusals,
        *_configuration_query_parameters(campaign),
        exclude_human_annotated,
        seed,
        limit,
    )


class GeminiGateway(Protocol):
    def create_cache(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def get_cache(self, cache_identifier: str) -> dict[str, Any]: ...

    def submit(self, plan: dict[str, Any], model: str) -> str: ...

    def status(self, provider_job_id: str) -> dict[str, Any]: ...

    def find_batch(self, display_name: str, model: str) -> str | None: ...


class LiveGeminiGateway:
    """Thin web adapter over the already validated Phase 4B provider operations."""

    @staticmethod
    def _key() -> str:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise CampaignError("Gemini credentials are not configured")
        return key

    def create_cache(self, request: dict[str, Any]) -> dict[str, Any]:
        return _http_json(
            "https://generativelanguage.googleapis.com/v1beta/cachedContents",
            {"x-goog-api-key": self._key(), "Content-Type": "application/json"},
            request,
            120.0,
        )

    def get_cache(self, cache_identifier: str) -> dict[str, Any]:
        self._key()
        return _get_cache(cache_identifier)

    def submit(self, plan: dict[str, Any], model: str) -> str:
        validate_production_plan(plan)
        response = _http_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchGenerateContent",
            {"x-goog-api-key": self._key(), "Content-Type": "application/json"},
            plan["request"],
            120.0,
        )
        return submission_batch_name({"response": response})

    def status(self, provider_job_id: str) -> dict[str, Any]:
        return _get_batch(provider_job_id)

    def find_batch(self, display_name: str, model: str) -> str | None:
        """Recover a submitted operation by its deterministic display identity."""
        self._key()
        matches: list[str] = []
        page_token: str | None = None
        while True:
            query = "batches?pageSize=100"
            if page_token:
                query += f"&pageToken={quote(page_token, safe='')}"
            value = _get_gemini_resource(query, resource_label="batch list")
            operations = value.get("operations")
            if isinstance(operations, list):
                for operation in operations:
                    if not isinstance(operation, dict):
                        continue
                    batch = batch_resource(operation)
                    if (
                        batch.get("displayName") == display_name
                        and batch.get("model") == f"models/{model}"
                        and isinstance(operation.get("name"), str)
                    ):
                        matches.append(str(operation["name"]))
            token = value.get("nextPageToken")
            if not isinstance(token, str) or not token:
                break
            page_token = token
        if len(set(matches)) > 1:
            raise CampaignError("multiple Gemini batches share the recovery identity")
        return matches[0] if matches else None


@dataclass(frozen=True)
class PlanResult:
    campaign: dict[str, Any]
    preview: list[dict[str, Any]]


def _require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise PermissionError("administrator or research access required")


def list_corpora(settings: WebSettings) -> list[dict[str, Any]]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT id,public_id,slug,name FROM corpora WHERE is_active ORDER BY name"
            ).fetchall()
        ]


def create_campaign(
    settings: WebSettings,
    principal: Principal,
    *,
    name: str,
    corpus_id: int,
    configuration_id: str = POLICY_CONFIGURATION_ID,
) -> dict[str, Any]:
    _require_admin(principal)
    if not name.strip():
        raise CampaignError("campaign name is required")
    try:
        approved = approved_configuration(configuration_id)
    except ValueError as error:
        raise CampaignError(str(error)) from error
    configuration = approved.persistence_values()
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        reusable = connection.execute(
            """
            SELECT cache_identifier,cache_content_sha256,cache_model,cache_display_name,
                   cache_created_at,cache_expires_at,cache_verified_at,cache_metadata
            FROM ai_annotation_campaigns
            WHERE provider=%s AND configured_model=%s AND prompt_version=%s
              AND prompt_sha256=%s AND canonical_schema_sha256=%s
              AND prediction_contract_sha256=%s AND provider_schema_sha256=%s
              AND generation_config=%s AND execution_mode=%s
              AND cache_status='REUSABLE' AND cache_identifier IS NOT NULL
              AND cache_content_sha256=%s AND cache_model=%s
              AND cache_display_name=%s AND cache_verified_at IS NOT NULL
              AND cache_expires_at>now()+(%s * interval '1 second')
            ORDER BY cache_expires_at DESC,id DESC LIMIT 1
            """,
            (
                configuration["provider"],
                configuration["configured_model"],
                configuration["prompt_version"],
                configuration["prompt_sha256"],
                configuration["canonical_schema_sha256"],
                configuration["prediction_contract_sha256"],
                configuration["provider_schema_sha256"],
                Jsonb(configuration["generation_config"]),
                configuration["execution_mode"],
                approved.prompt_sha256,
                f"models/{approved.configured_model}",
                _cache_display_name(approved.prompt_sha256),
                int(CACHE_REUSE_MINIMUM.total_seconds()),
            ),
        ).fetchone()
        cache = dict(reusable) if reusable is not None else {}
        row = connection.execute(
            """
            INSERT INTO ai_annotation_campaigns
              (name,corpus_id,created_by_user_id,configuration_id,prediction_task,
               annotation_schema_slug,annotation_schema_version,record_set_id,
               approved_record_set_sha256,provider,configured_model,prompt_version,
               prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
               provider_schema_sha256,generation_config,execution_mode,configuration_class,
               cache_status,cache_identifier,cache_content_sha256,cache_model,
               cache_display_name,cache_created_at,cache_expires_at,cache_verified_at,
               cache_metadata)
            SELECT %s,c.id,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            FROM corpora c WHERE c.id=%s AND c.is_active
            RETURNING *
            """,
            (
                name.strip(),
                principal.user_id,
                approved.configuration_id,
                approved.prediction_task,
                approved.annotation_schema_slug,
                approved.annotation_schema_version,
                approved.record_set_id,
                approved.record_set_sha256,
                configuration["provider"],
                configuration["configured_model"],
                configuration["prompt_version"],
                configuration["prompt_sha256"],
                configuration["canonical_schema_sha256"],
                configuration["prediction_contract_sha256"],
                configuration["provider_schema_sha256"],
                Jsonb(configuration["generation_config"]),
                configuration["execution_mode"],
                "APPROVED_PRODUCTION",
                "REUSABLE" if cache else "REQUIRED",
                cache.get("cache_identifier"),
                cache.get("cache_content_sha256"),
                cache.get("cache_model"),
                cache.get("cache_display_name"),
                cache.get("cache_created_at"),
                cache.get("cache_expires_at"),
                cache.get("cache_verified_at"),
                Jsonb(cache.get("cache_metadata", {})),
                corpus_id,
            ),
        ).fetchone()
        if row is None:
            raise LookupError("active corpus not found")
        return dict(row)


def list_campaigns(settings: WebSettings, principal: Principal) -> list[dict[str, Any]]:
    _require_admin(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT campaign.*,corpus.name AS corpus_name,run.public_id AS run_public_id
                FROM ai_annotation_campaigns campaign
                JOIN corpora corpus ON corpus.id=campaign.corpus_id
                LEFT JOIN model_prediction_runs run
                  ON run.id=campaign.model_prediction_run_id
                ORDER BY campaign.created_at DESC,campaign.id DESC
                """
            ).fetchall()
        ]


def _legacy_other_exclusion_breakdown(
    connection: psycopg.Connection[dict[str, Any]],
    campaign: dict[str, Any],
) -> dict[str, int]:
    """Explain a stored pre-fix plan without mutating its frozen record selection."""
    exclude_human = bool(
        (campaign.get("selection_definition") or {}).get("exclude_human_annotated")
    )
    rows = connection.execute(
        """
        WITH base AS (
          SELECT turn.*,
            (EXISTS (
              SELECT 1 FROM model_predictions prediction
              JOIN model_prediction_runs run ON run.id=prediction.run_id
              WHERE prediction.speaker_turn_id=turn.id
                AND run.provider=%s AND run.configured_model=%s
                AND run.prompt_version=%s AND run.prompt_sha256=%s
                AND run.canonical_schema_sha256=%s
                AND run.prediction_contract_sha256=%s
                AND run.provider_schema_sha256=%s
                AND run.generation_config @> %s AND run.execution_mode=%s
            ) OR EXISTS (
              SELECT 1 FROM model_prediction_abstentions abstention
              JOIN model_prediction_runs run ON run.id=abstention.run_id
              WHERE abstention.speaker_turn_id=turn.id
                AND run.provider=%s AND run.configured_model=%s
                AND run.prompt_version=%s AND run.prompt_sha256=%s
                AND run.canonical_schema_sha256=%s
                AND run.prediction_contract_sha256=%s
                AND run.provider_schema_sha256=%s
                AND run.generation_config @> %s AND run.execution_mode=%s
            )) AS already_predicted,
            EXISTS (
              SELECT 1 FROM tasks task
              JOIN assignments assignment ON assignment.task_id=task.id
              JOIN annotations annotation ON annotation.assignment_id=assignment.id
              WHERE task.speaker_turn_id=turn.id
                AND annotation.status IN ('submitted','revised')
            ) AS human_annotated
          FROM speaker_turns turn
          JOIN reconstruction_runs reconstruction
            ON reconstruction.id=turn.reconstruction_run_id
          JOIN corpora corpus ON corpus.current_preprocessing_run_id=
            reconstruction.preprocessing_run_id
          WHERE corpus.id=%s
        ), reasons AS (
          SELECT CASE
            WHEN business_type IS NULL THEN 'unmapped_business_heading'
            WHEN business_type='committees' AND is_procedural IS NULL
              THEN 'committees_procedural_status_unknown'
            WHEN business_type='motions' AND is_procedural IS NULL
              THEN 'motions_procedural_status_unknown'
            WHEN business_type='parliamentary_representation'
              AND is_ceremonial IS NULL
              THEN 'parliamentary_representation_ceremonial_status_unknown'
            WHEN is_procedural IS NULL OR is_ceremonial IS NULL
              THEN 'legacy_business_mapping_indeterminate'
            WHEN NOT eligible_50_words THEN 'under_50_words'
            WHEN NOT has_known_speaker THEN 'unknown_speaker'
            WHEN is_collective_speaker IS TRUE THEN 'collective_speaker'
            WHEN is_procedural IS TRUE THEN 'procedural_business'
            WHEN is_ceremonial IS TRUE THEN 'ceremonial_business'
            WHEN is_orphan_continuation THEN 'orphan_continuation'
            ELSE NULL
          END AS reason
          FROM base
          WHERE NOT already_predicted
            AND (NOT %s OR NOT human_annotated)
            AND eligible_main_analysis IS NOT TRUE
        )
        SELECT reason,count(*) AS count FROM reasons
        GROUP BY reason ORDER BY reason
        """,
        (
            campaign["provider"],
            campaign["configured_model"],
            campaign["prompt_version"],
            campaign["prompt_sha256"],
            campaign["canonical_schema_sha256"],
            campaign["prediction_contract_sha256"],
            campaign["provider_schema_sha256"],
            Jsonb(campaign["generation_config"]),
            campaign["execution_mode"],
            *_configuration_query_parameters(campaign),
            campaign["corpus_id"],
            exclude_human,
        ),
    ).fetchall()
    result: dict[str, int] = {}
    for row in rows:
        reason = row["reason"]
        if not isinstance(reason, str) or reason not in OTHER_EXCLUSION_LABELS:
            raise CampaignError("stored campaign has an unexplained legacy exclusion")
        result[reason] = int(row["count"])
    if sum(result.values()) != int(campaign["excluded_other_count"]):
        raise CampaignError("stored campaign exclusion breakdown no longer reconciles")
    return result


def get_campaign(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    chunk_page: int = 1,
) -> dict[str, Any]:
    _require_admin(principal)
    if chunk_page < 1:
        raise CampaignError("child Batch page must be positive")
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT campaign.*,corpus.name AS corpus_name,run.public_id AS run_public_id
            FROM ai_annotation_campaigns campaign
            JOIN corpora corpus ON corpus.id=campaign.corpus_id
            LEFT JOIN model_prediction_runs run ON run.id=campaign.model_prediction_run_id
            WHERE campaign.public_id::text=%s
            """,
            (public_id,),
        ).fetchone()
        if row is None:
            raise LookupError("AI annotation campaign not found")
        result = dict(row)
        approved: ApprovedAIConfiguration | None = None
        try:
            approved = _campaign_configuration(result)
            result["configuration_display_label"] = approved.display_label
            result["record_set_mode"] = approved.record_set_mode
            result["approved_record_set_size"] = approved.record_set_size
        except CampaignError:
            result["configuration_display_label"] = "Historical / research configuration"
            result["record_set_mode"] = (
                "frozen_sample"
                if result.get("record_set_id")
                else "deterministic_selection"
            )
            result["approved_record_set_size"] = result.get("planned_count")
        result["cache_plan_status"] = cache_plan_status(result)
        selection = cast(dict[str, Any], result.get("selection_definition") or {})
        other_reasons = selection.get("other_exclusion_reasons")
        if not isinstance(other_reasons, dict) and selection.get(
            "eligible_main_analysis"
        ) is True:
            other_reasons = _legacy_other_exclusion_breakdown(connection, result)
        if not isinstance(other_reasons, dict):
            other_reasons = {}
        result["other_exclusion_details"] = [
            {
                "code": code,
                "label": label,
                "count": int(other_reasons[code]),
            }
            for code, label in OTHER_EXCLUSION_LABELS.items()
            if code in other_reasons
        ]
        if approved is not None and approved.record_set_mode == "frozen_sample":
            result["selection_current"] = (
                selection.get("mode") == "frozen_sample"
                and selection.get("sample_id") == approved.record_set_id
                and selection.get("sample_size") == approved.record_set_size
                and selection.get("sample_sha256") == approved.record_set_sha256
            )
        else:
            result["selection_current"] = (
                selection.get("eligibility_rule") == AI_ELIGIBILITY_RULE
            )
        result["preview"] = [
            dict(value)
            for value in connection.execute(
                """
                SELECT record.record_id,record.selection_order,turn.speaker_name_raw,
                       turn.speech_date,turn.major_heading_original,turn.text_clean
                FROM ai_annotation_campaign_records record
                JOIN speaker_turns turn ON turn.id=record.speaker_turn_id
                WHERE record.campaign_id=%s ORDER BY record.selection_order LIMIT 10
                """,
                (row["id"],),
            ).fetchall()
        ]
        recoveries = [
            dict(value)
            for value in connection.execute(
                """
                SELECT * FROM ai_annotation_campaign_recoveries
                WHERE campaign_id=%s ORDER BY attempt_number
                """,
                (row["id"],),
            ).fetchall()
        ]
        result["recoveries"] = recoveries
        result["latest_recovery"] = recoveries[-1] if recoveries else None
        result["active_recovery"] = next(
            (
                recovery
                for recovery in reversed(recoveries)
                if recovery["status"]
                in {"PREPARING", "SUBMITTED", "RUNNING", "PROVIDER_COMPLETED"}
            ),
            None,
        )
        batch_progress = connection.execute(
            """
            SELECT count(*) AS chunks_total,
              count(*) FILTER (WHERE status='PENDING') AS chunks_pending,
              count(*) FILTER (WHERE status IN ('SUBMITTING','SUBMITTED')) AS chunks_submitted,
              count(*) FILTER (WHERE status='RUNNING') AS chunks_running,
              count(*) FILTER (WHERE status='PROVIDER_COMPLETED') AS chunks_completed,
              count(*) FILTER (WHERE status='COLLECTED') AS chunks_collected,
              COALESCE(sum(record_count),0) AS chunk_records,
              COALESCE(sum(estimated_input_tokens),0) AS chunk_estimated_input_tokens,
              COALESCE(sum(valid_count),0) AS chunk_valid,
              COALESCE(sum(repaired_count),0) AS chunk_repaired,
              COALESCE(sum(invalid_count),0) AS chunk_invalid,
              COALESCE(sum(provider_failure_count),0) AS chunk_provider_failures,
              COALESCE(sum(missing_count),0) AS chunk_missing,
              COALESCE(sum(duplicate_count),0) AS chunk_duplicates,
              COALESCE(sum(input_tokens),0) AS chunk_input_tokens,
              COALESCE(sum(cached_input_tokens),0) AS chunk_cached_input_tokens,
              COALESCE(sum(output_tokens),0) AS chunk_output_tokens,
              COALESCE(sum(reasoning_tokens),0) AS chunk_reasoning_tokens,
              COALESCE(sum(processing_cost_usd),0) AS chunk_processing_cost,
              COALESCE(sum(cache_cost_usd),0) AS chunk_cache_cost
            FROM ai_annotation_campaign_batches WHERE campaign_id=%s
            """,
            (row["id"],),
        ).fetchone()
        result["batch_progress"] = dict(batch_progress) if batch_progress else {}
        result["chunk_preview"] = [
            dict(value)
            for value in connection.execute(
                """
                SELECT chunk_number,first_selection_order,last_selection_order,record_count,
                       estimated_input_tokens,status,provider_state,provider_job_id,
                       cache_identifier,valid_count,repaired_count,invalid_count,
                       missing_count,duplicate_count,processing_cost_usd,cache_cost_usd
                FROM ai_annotation_campaign_batches WHERE campaign_id=%s
                ORDER BY chunk_number LIMIT 20 OFFSET %s
                """,
                (row["id"], (chunk_page - 1) * 20),
            ).fetchall()
        ]
        chunk_total = int(result["batch_progress"].get("chunks_total") or 0)
        result["chunk_page"] = chunk_page
        result["chunk_pages"] = max(1, math.ceil(chunk_total / 20))
        result["structural_repair_available_count"] = 0
        result["structural_repair_applied_count"] = 0
        result["provider_refusal_available_count"] = 0
        result["provider_refusal_applied_count"] = 0
        if result.get("artifact_reference"):
            source_directory = _base_effective_artifact_directory(result, recoveries)
            repaired_directory = structural_repair_directory(source_directory)
            if repaired_directory.exists():
                repair_summary = json.loads(
                    (repaired_directory / "operational_summary.json").read_text(
                        encoding="utf-8"
                    )
                )
                repair_metadata = repair_summary.get("structural_repair", {})
                if isinstance(repair_metadata, dict):
                    result["structural_repair_applied_count"] = int(
                        repair_metadata.get("repaired_count") or 0
                    )
            elif (
                result["status"] == "COLLECTED"
                and int(result["invalid_count"]) > 0
                and result.get("prediction_task", "policy") == "policy"
            ):
                result["structural_repair_available_count"] = structural_repair_count(
                    iter_jsonl(source_directory / "predictions.jsonl"),
                    _production_domain_codes(),
                )
            current_directory = (
                repaired_directory if repaired_directory.exists() else source_directory
            )
            refusal_directory = provider_refusal_directory(current_directory)
            if refusal_directory.exists():
                refusal_summary = json.loads(
                    (refusal_directory / "operational_summary.json").read_text(
                        encoding="utf-8"
                    )
                )
                refusal_metadata = refusal_summary.get("provider_refusals", {})
                if isinstance(refusal_metadata, dict):
                    result["provider_refusal_applied_count"] = int(
                        refusal_metadata.get("count") or 0
                    )
            elif result["status"] == "COLLECTED" and int(result["invalid_count"]) > 0:
                result["provider_refusal_available_count"] = provider_refusal_count(
                    iter_jsonl(current_directory / "predictions.jsonl")
                )
        original_metadata = cast(
            dict[str, Any], result.get("operational_metadata") or {}
        )
        original_cost = cast(dict[str, Any], original_metadata.get("cost") or {})
        recovery_cost = sum(
            float(metadata.get("cost", {}).get("total_pilot_cost_usd") or 0)
            for recovery in recoveries
            if recovery["status"] == "COLLECTED"
            and isinstance((metadata := recovery.get("operational_metadata")), dict)
            and isinstance(metadata.get("cost"), dict)
        )
        result["recovery_cost_usd"] = recovery_cost
        result["campaign_total_cost_usd"] = (
            float(original_cost.get("total_pilot_cost_usd") or 0) + recovery_cost
        )
        return result


def plan_campaign(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    limit: int,
    seed: str,
    exclude_human_annotated: bool,
    retry_provider_refusals: bool = False,
    _connection: psycopg.Connection[dict[str, Any]] | None = None,
) -> PlanResult:
    _require_admin(principal)
    if limit < 1:
        raise CampaignError("planned count must be positive")
    if limit > 200_000:
        raise CampaignError("planned count exceeds the 200,000-record safety limit")
    connection_context = (
        psycopg.connect(settings.database.psycopg_url, row_factory=dict_row)
        if _connection is None
        else nullcontext(_connection)
    )
    with connection_context as connection:
        campaign = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign is None:
            raise LookupError("AI annotation campaign not found")
        if campaign["status"] != "DRAFT":
            raise CampaignError("only a DRAFT campaign can be planned")
        configuration = _campaign_configuration(dict(campaign))
        if configuration.record_set_mode == "frozen_sample":
            if limit != configuration.record_set_size:
                raise CampaignError("frozen hostility sample requires planned_count=100")
            return _plan_frozen_sample_campaign(connection, dict(campaign), configuration)
        breakdown = _planning_breakdown(
            connection,
            dict(campaign),
            exclude_human_annotated=exclude_human_annotated,
            retry_provider_refusals=retry_provider_refusals,
        )
        record_set_hasher = _RecordSetHasher()
        selected_count = 0
        preview: list[dict[str, Any]] = []
        insert_values: list[tuple[object, ...]] = []
        insert_cursor = connection.cursor()
        with connection.cursor(name=f"campaign_plan_{campaign['id']}") as selected_rows:
            selected_rows.itersize = PLANNING_CHUNK_SIZE
            selected_rows.execute(
                _selection_query(),
                _selection_query_parameters(
                    dict(campaign),
                    exclude_human_annotated=exclude_human_annotated,
                    retry_provider_refusals=retry_provider_refusals,
                    seed=seed,
                    limit=limit,
                ),
            )
            for selected_row in selected_rows:
                row = dict(selected_row)
                record = production_record_from_turn(row)
                record_id = str(record["record_id"])
                record_set_hasher.add(record_id)
                insert_values.append(
                    (
                        campaign["id"],
                        row["id"],
                        record_id,
                        selected_count,
                        canonical_hash(record),
                    )
                )
                if len(preview) < 10:
                    preview.append(row)
                selected_count += 1
                if len(insert_values) == PLANNING_CHUNK_SIZE:
                    insert_cursor.executemany(
                        """
                        INSERT INTO ai_annotation_campaign_records
                          (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
                        VALUES (%s,%s,%s,%s,%s)
                        """,
                        insert_values,
                    )
                    insert_values.clear()
        if not selected_count:
            raise CampaignError("selection contains no eligible records")
        if insert_values:
            insert_cursor.executemany(
                """
            INSERT INTO ai_annotation_campaign_records
              (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
            VALUES (%s,%s,%s,%s,%s)
                """,
                insert_values,
            )
        record_set_sha256 = record_set_hasher.hexdigest()
        estimate = estimate_plan(selected_count)
        selection = {
            "eligibility_rule": AI_ELIGIBILITY_RULE,
            "exclude_human_annotated": exclude_human_annotated,
            "retry_provider_refusals": retry_provider_refusals,
            "human_annotated_count": breakdown["human_annotated_count"],
            "excluded_human_annotated_count": breakdown[
                "excluded_human_annotated_count"
            ],
            "human_prediction_overlap_count": breakdown[
                "human_prediction_overlap_count"
            ],
            "other_exclusion_reasons": breakdown["other_exclusion_reasons"],
            "seed": seed,
            "limit": limit,
            "ordering": "sha256(turn_key + ':' + seed), turn_key",
        }
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET
              status='PLANNED',selection_definition=%s,record_set_sha256=%s,
              corpus_total_count=%s,eligible_count=%s,
              excluded_existing_prediction_count=%s,excluded_other_count=%s,
              planned_count=%s,estimated_input_tokens=%s,
              estimated_cached_input_tokens=%s,estimated_uncached_input_tokens=%s,
              estimated_output_tokens=%s,estimated_processing_cost_usd=%s,
              estimated_cache_storage_cost_usd=%s,estimated_total_cost_usd=%s,
              planned_at=now()
            WHERE id=%s RETURNING *
            """,
            (
                Jsonb(selection),
                record_set_sha256,
                breakdown["corpus_total_count"],
                breakdown["eligible_count"],
                breakdown["excluded_existing_prediction_count"],
                breakdown["excluded_other_count"],
                selected_count,
                estimate["input_tokens"],
                estimate["cached_input_tokens"],
                estimate["uncached_input_tokens"],
                estimate["output_tokens"],
                estimate["token_processing_cost_usd"],
                estimate["explicit_cache_storage_cost_usd"],
                estimate["estimated_total_cost_usd"],
                campaign["id"],
            ),
        ).fetchone()
        assert updated is not None
        return PlanResult(dict(updated), preview)


def _plan_frozen_sample_campaign(
    connection: psycopg.Connection[dict[str, Any]],
    campaign: dict[str, Any],
    configuration: ApprovedAIConfiguration,
) -> PlanResult:
    if configuration.record_set_id != "hostility-heldout-100":
        raise CampaignError("unsupported frozen hostility record set")
    path = ROOT / "config/research_samples/hostility-heldout-100.json"
    sample = load_frozen_sample(path, expected_sample_id=configuration.record_set_id)
    if (
        sample.sample_size != configuration.record_set_size
        or sample.sample_sha256 != configuration.record_set_sha256
        or len(sample.turns) != 100
        or len({turn.turn_key for turn in sample.turns}) != 100
    ):
        raise CampaignError("frozen hostility sample identity, count, or membership differs")
    development = load_frozen_sample(
        ROOT / "config/research_samples/hostility-development-100.json",
        expected_sample_id="hostility-development-100",
    )
    if {turn.turn_key for turn in sample.turns} & {
        turn.turn_key for turn in development.turns
    }:
        raise CampaignError("frozen hostility validation sample overlaps development data")
    turn_keys = [turn.turn_key for turn in sample.turns]
    rows = connection.execute(
        """
        SELECT turn.id,turn.turn_key,turn.speech_date AS date,turn.chamber,
               turn.speaker_name_raw,turn.major_heading_original,
               turn.minor_heading_original,turn.business_type,turn.text_clean,turn.text_raw
        FROM speaker_turns turn
        JOIN reconstruction_runs reconstruction ON reconstruction.id=turn.reconstruction_run_id
        JOIN corpora corpus ON corpus.current_preprocessing_run_id=reconstruction.preprocessing_run_id
        WHERE corpus.id=%s AND turn.turn_key=ANY(%s)
        """,
        (campaign["corpus_id"], turn_keys),
    ).fetchall()
    by_key = {str(row["turn_key"]): dict(row) for row in rows}
    if set(by_key) != set(turn_keys):
        missing = len(set(turn_keys) - set(by_key))
        raise CampaignError(f"frozen hostility sample has {missing} unresolved canonical turns")
    record_set_hasher = _RecordSetHasher()
    values: list[tuple[object, ...]] = []
    preview: list[dict[str, Any]] = []
    for index, frozen_turn in enumerate(sample.turns):
        row = by_key[frozen_turn.turn_key]
        record = production_record_from_turn(row)
        record_id = str(record["record_id"])
        record_set_hasher.add(record_id)
        values.append(
            (campaign["id"], row["id"], record_id, index, canonical_hash(record))
        )
        if len(preview) < 10:
            preview.append(row)
    connection.cursor().executemany(
        """
        INSERT INTO ai_annotation_campaign_records
          (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
        VALUES (%s,%s,%s,%s,%s)
        """,
        values,
    )
    estimate = estimate_plan(len(values), model=configuration.configured_model)
    selection = {
        "mode": "frozen_sample",
        "sample_id": sample.sample_id,
        "sample_size": sample.sample_size,
        "sample_sha256": sample.sample_sha256,
        "sample_version": sample.effective_version,
        "ordering": "frozen sample_order",
    }
    corpus_total_row = connection.execute(
        """
        SELECT count(*) AS count FROM speaker_turns turn
        JOIN reconstruction_runs reconstruction ON reconstruction.id=turn.reconstruction_run_id
        JOIN corpora corpus ON corpus.current_preprocessing_run_id=
          reconstruction.preprocessing_run_id WHERE corpus.id=%s
        """,
        (campaign["corpus_id"],),
    ).fetchone()
    assert corpus_total_row is not None
    updated = connection.execute(
        """
        UPDATE ai_annotation_campaigns SET status='PLANNED',selection_definition=%s,
          record_set_sha256=%s,corpus_total_count=%s,eligible_count=100,
          excluded_existing_prediction_count=0,excluded_other_count=0,planned_count=100,
          estimated_input_tokens=%s,estimated_cached_input_tokens=%s,
          estimated_uncached_input_tokens=%s,estimated_output_tokens=%s,
          estimated_processing_cost_usd=%s,estimated_cache_storage_cost_usd=%s,
          estimated_total_cost_usd=%s,planned_at=now()
        WHERE id=%s RETURNING *
        """,
        (
            Jsonb(selection), record_set_hasher.hexdigest(), corpus_total_row["count"],
            estimate["input_tokens"], estimate["cached_input_tokens"],
            estimate["uncached_input_tokens"], estimate["output_tokens"],
            estimate["token_processing_cost_usd"],
            estimate["explicit_cache_storage_cost_usd"],
            estimate["estimated_total_cost_usd"], campaign["id"],
        ),
    ).fetchone()
    assert updated is not None
    return PlanResult(dict(updated), preview)


def transition_campaign(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    target: str,
) -> dict[str, Any]:
    _require_admin(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if row is None:
            raise LookupError("AI annotation campaign not found")
        validate_transition(str(row["status"]), target)
        updated = connection.execute(
            "UPDATE ai_annotation_campaigns SET status=%s WHERE id=%s RETURNING *",
            (target, row["id"]),
        ).fetchone()
        assert updated is not None
        return dict(updated)


def _records_for_campaign(
    connection: psycopg.Connection[dict[str, Any]], campaign_id: int
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        """
        SELECT turn.turn_key,turn.speech_date AS date,turn.chamber,turn.speaker_name_raw,
               turn.major_heading_original,turn.minor_heading_original,turn.business_type,
               turn.text_clean,turn.text_raw
        FROM ai_annotation_campaign_records record
        JOIN speaker_turns turn ON turn.id=record.speaker_turn_id
        WHERE record.campaign_id=%s ORDER BY record.selection_order
        """,
        (campaign_id,),
    ).fetchall()
    return tuple(production_record_from_turn(dict(row)) for row in rows)


def _provider_plan(
    campaign: dict[str, Any],
    records: tuple[dict[str, Any], ...],
    *,
    expected_record_set_sha256: str | None = None,
    cache_identifier: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    configuration = _campaign_configuration(campaign)
    if (
        configuration.record_set_mode == "frozen_sample"
        and expected_record_set_sha256 is None
        and len(records) != configuration.record_set_size
    ):
        raise CampaignError("frozen hostility campaign requires exactly 100 records")
    context = _configuration_context(configuration)
    system_prompt = build_system_prompt(context, str(campaign["prompt_version"]))
    if prompt_hash(system_prompt) != campaign["prompt_sha256"]:
        raise CampaignError("assembled prompt no longer matches frozen campaign provenance")
    if configuration.prediction_task == "policy":
        codebook = production_codebook()
        domain_codes = tuple(label.code for label in codebook["taxonomy"].labels)
        schema = prediction_json_schema(domain_codes)
    else:
        schema = hostility_schema(configuration.prompt_version)
    pricing = load_pricing(ROOT / "config/llm_eval/pricing.yaml")
    pricing_entry = pricing["models"][f"gemini:{campaign['configured_model']}"]
    request = build_batch_create_request(
        model=str(campaign["configured_model"]),
        system_prompt=system_prompt,
        records=records,
        schema=schema,
        display_name=display_name or f"phase4d-{campaign['public_id']}",
        cached_content=cache_identifier or campaign["cache_identifier"],
        generation=campaign["generation_config"],
    )
    provenance = {
        "provider": campaign["provider"],
        "model": campaign["configured_model"],
        "prompt_version": campaign["prompt_version"],
        "assembled_prompt_sha256": campaign["prompt_sha256"],
        "canonical_schema_sha256": campaign["canonical_schema_sha256"],
        "prediction_contract_sha256": campaign["prediction_contract_sha256"],
        "provider_schema_sha256": campaign["provider_schema_sha256"],
        **campaign["generation_config"],
        "execution_mode": campaign["execution_mode"],
        "cached_content": cache_identifier or campaign["cache_identifier"],
        "record_count": len(records),
        "record_ids_sha256": canonical_hash([record["record_id"] for record in records]),
        "pricing_format_version": 1,
        "pricing_effective_date": pricing_entry.get(
            "effective_date", pricing.get("effective_date")
        ),
    }
    plan = {"provenance": provenance, "request": request}
    validate_production_plan(plan)
    expected_hash = expected_record_set_sha256 or campaign["record_set_sha256"]
    if provenance["record_ids_sha256"] != expected_hash:
        raise CampaignError("stored record set no longer matches campaign plan")
    if canonical_hash(schema) != campaign["prediction_contract_sha256"]:
        raise CampaignError("prediction contract no longer matches campaign")
    if canonical_hash(gemini_batch_schema(schema)) != campaign["provider_schema_sha256"]:
        raise CampaignError("provider schema no longer matches campaign")
    return plan


def _persist_cache_fields(
    connection: psycopg.Connection[dict[str, Any]],
    campaign_id: int,
    fields: dict[str, Any],
) -> dict[str, Any]:
    updated = connection.execute(
        """
        UPDATE ai_annotation_campaigns SET
          cache_status=%s,cache_identifier=%s,cache_content_sha256=%s,
          cache_model=%s,cache_display_name=%s,cache_created_at=%s,
          cache_expires_at=%s,cache_verified_at=%s,cache_metadata=%s
        WHERE id=%s AND status='READY' AND provider_job_id IS NULL RETURNING *
        """,
        (
            fields["cache_status"],
            fields["cache_identifier"],
            fields["cache_content_sha256"],
            fields["cache_model"],
            fields["cache_display_name"],
            fields["cache_created_at"],
            fields["cache_expires_at"],
            fields["cache_verified_at"],
            Jsonb(fields["cache_metadata"]),
            campaign_id,
        ),
    ).fetchone()
    if updated is None:
        raise CampaignError("campaign changed while cache provenance was being persisted")
    return dict(updated)


def _mark_cache_unavailable(
    settings: WebSettings, campaign_id: int, reason: str
) -> None:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET cache_status='UNAVAILABLE',
              failure_metadata=failure_metadata || %s
            WHERE id=%s AND status='READY' AND provider_job_id IS NULL
            """,
            (Jsonb({"cache_error": reason[:1000]}), campaign_id),
        )


def require_current_ai_selection(campaign: dict[str, Any]) -> None:
    selection = campaign.get("selection_definition")
    configuration = _campaign_configuration(campaign)
    if configuration.record_set_mode == "frozen_sample":
        if not isinstance(selection, dict) or (
            selection.get("mode") != "frozen_sample"
            or selection.get("sample_id") != configuration.record_set_id
            or selection.get("sample_size") != configuration.record_set_size
            or selection.get("sample_sha256") != configuration.record_set_sha256
        ):
            raise CampaignError("campaign frozen record-set identity differs")
        return
    rule = selection.get("eligibility_rule") if isinstance(selection, dict) else None
    if rule != AI_ELIGIBILITY_RULE:
        raise CampaignError(
            "campaign uses the legacy main-analysis eligibility rule; create and plan "
            "a new campaign before paid submission"
        )


def ensure_campaign_cache(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    gateway: GeminiGateway | None = None,
) -> dict[str, Any]:
    """Verify a locally-bound cache or create it only inside the paid submission path."""
    _require_admin(principal)
    provider = gateway or LiveGeminiGateway()
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if row is None:
            raise LookupError("AI annotation campaign not found")
        campaign = dict(row)
        if campaign["status"] != "READY" or campaign["provider_job_id"] is not None:
            raise CampaignError("campaign is not ready for cache preparation")
        if configuration_class(campaign) != "APPROVED_PRODUCTION":
            raise CampaignError("cache preparation requires the approved production configuration")
        require_current_ai_selection(campaign)
        cache_plan = _canonical_cache_plan(campaign)
        if campaign.get("cache_status") == "CREATING":
            raise CampaignError("cache preparation is already in progress")
        if cache_plan_status(campaign) == "REUSABLE":
            identifier = str(campaign["cache_identifier"])
            try:
                resource = provider.get_cache(identifier)
            except ValueError as error:
                if "HTTP 404" not in str(error):
                    raise CampaignError(f"Gemini cache verification failed: {error}") from error
            else:
                try:
                    fields = _cache_resource_fields(
                        resource, cache_plan, now=datetime.now(UTC)
                    )
                    if fields["cache_identifier"] != identifier:
                        raise CampaignError("Gemini returned a different cache identifier")
                except CampaignError:
                    pass
                else:
                    return _persist_cache_fields(connection, int(campaign["id"]), fields)
        reserved = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET cache_status='CREATING'
            WHERE id=%s AND status='READY' AND provider_job_id IS NULL
              AND cache_status<>'CREATING' RETURNING *
            """,
            (campaign["id"],),
        ).fetchone()
        if reserved is None:
            raise CampaignError("cache preparation could not be reserved")

    try:
        resource = provider.create_cache(cast(dict[str, Any], cache_plan["request"]))
        fields = _cache_resource_fields(resource, cache_plan, now=datetime.now(UTC))
    except Exception as error:
        _mark_cache_unavailable(settings, int(campaign["id"]), str(error))
        if isinstance(error, CampaignError):
            raise
        raise CampaignError(f"Gemini cache creation failed: {error}") from error

    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE id=%s FOR UPDATE",
            (campaign["id"],),
        ).fetchone()
        if row is None or row["cache_status"] != "CREATING":
            raise CampaignError("campaign cache reservation was lost")
        return _persist_cache_fields(connection, int(campaign["id"]), fields)


def submit_campaign(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    confirmation: str,
    *,
    gateway: GeminiGateway | None = None,
) -> dict[str, Any]:
    _require_admin(principal)
    if confirmation != PAID_CONFIRMATION:
        raise CampaignError(f"paid submission requires typing {PAID_CONFIRMATION}")
    provider = gateway or LiveGeminiGateway()
    ensure_campaign_cache(settings, principal, public_id, gateway=provider)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign_row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign_row is None:
            raise LookupError("AI annotation campaign not found")
        campaign = dict(campaign_row)
        if campaign["status"] != "READY" or campaign["provider_job_id"] is not None:
            raise CampaignError("campaign is not ready for a first submission")
        if configuration_class(campaign) != "APPROVED_PRODUCTION":
            raise CampaignError("paid submission is restricted to the approved production configuration")
        if cache_plan_status(campaign) != "REUSABLE" or campaign.get("cache_status") != "REUSABLE":
            raise CampaignError("approved production submission requires a verified live cache")
        records = _records_for_campaign(connection, int(campaign["id"]))
        if len(records) != int(campaign["planned_count"]):
            raise CampaignError("stored campaign record count differs from the approved plan")
        plan = _provider_plan(campaign, records)
        provider_job_id = provider.submit(plan, str(campaign["configured_model"]))
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='SUBMITTED',provider_job_id=%s,
                   provider_state='BATCH_STATE_PENDING',submitted_at=now()
            WHERE id=%s AND provider_job_id IS NULL AND status='READY' RETURNING *
            """,
            (provider_job_id, campaign["id"]),
        ).fetchone()
        if updated is None:
            raise CampaignError("campaign submission identity could not be persisted")
        return dict(updated)


def refresh_campaign(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    gateway: GeminiGateway | None = None,
) -> dict[str, Any]:
    _require_admin(principal)
    provider = gateway or LiveGeminiGateway()
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign is None:
            raise LookupError("AI annotation campaign not found")
        job = campaign["provider_job_id"]
        if not isinstance(job, str):
            raise CampaignError("campaign has no provider job to refresh")
        status = parse_batch_status(provider.status(job))
        provider_state = str(status["state"])
        if status["successful_terminal"]:
            target = "PROVIDER_COMPLETED"
        elif status["terminal"]:
            target = "FAILED"
        else:
            target = "RUNNING"
        current = str(campaign["status"])
        if current != target:
            validate_transition(current, target)
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status=%s,provider_state=%s,
              provider_success_count=%s,provider_failure_count=%s,
              provider_completed_at=CASE WHEN %s THEN COALESCE(provider_completed_at,now())
                                         ELSE provider_completed_at END,
              failure_metadata=CASE WHEN %s THEN %s ELSE failure_metadata END
            WHERE id=%s RETURNING *
            """,
            (
                target,
                provider_state,
                status["successful_count"],
                status["failed_count"],
                bool(status["terminal"]),
                target == "FAILED",
                Jsonb({"provider_error": status["error"]}),
                campaign["id"],
            ),
        ).fetchone()
        assert updated is not None
        return dict(updated)


def _artifact_directory(public_id: str) -> Path:
    root = Path(os.getenv("HANSARD_CAMPAIGN_ARTIFACT_ROOT", ROOT / "data/review/phase4d"))
    root = root.resolve()
    target = (root / f"campaign-{public_id}").resolve()
    if target.parent != root:
        raise CampaignError("unsafe campaign artifact path")
    return target


def _production_domain_codes() -> tuple[str, ...]:
    return tuple(label.code for label in production_codebook()["taxonomy"].labels)


def _base_effective_artifact_directory(
    campaign: dict[str, Any], recoveries: list[dict[str, Any]]
) -> Path:
    for recovery in reversed(recoveries):
        reference = recovery.get("effective_artifact_reference")
        if recovery.get("status") == "COLLECTED" and isinstance(reference, str):
            return Path(reference)
    return Path(str(campaign["artifact_reference"]))


def _current_effective_artifact_directory(
    campaign: dict[str, Any], recoveries: list[dict[str, Any]]
) -> Path:
    source = _base_effective_artifact_directory(campaign, recoveries)
    repaired = structural_repair_directory(source)
    source = repaired if repaired.exists() else source
    refused = provider_refusal_directory(source)
    return refused if refused.exists() else source


def collect_campaign(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    gateway: GeminiGateway | None = None,
) -> dict[str, Any]:
    _require_admin(principal)
    provider = gateway or LiveGeminiGateway()
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign_row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign_row is None:
            raise LookupError("AI annotation campaign not found")
        campaign = dict(campaign_row)
        if campaign["status"] == "COLLECTED" and campaign["artifact_reference"]:
            return campaign
        if campaign["status"] != "PROVIDER_COMPLETED":
            raise CampaignError("campaign provider job is not ready for collection")
        records = _records_for_campaign(connection, int(campaign["id"]))
        plan = _provider_plan(campaign, records)
        batch_value = provider.status(str(campaign["provider_job_id"]))
        configuration = _campaign_configuration(campaign)
        domain_codes = (
            tuple(label.code for label in production_codebook()["taxonomy"].labels)
            if configuration.prediction_task == "policy"
            else ()
        )
        results, coverage = collect_inline_batch(
            batch_value=batch_value,
            plan=plan,
            domain_codes=domain_codes,
            prediction_task=configuration.prediction_task,
            records_by_id={str(record["record_id"]): record for record in records},
        )
        for result in results:
            result.update(
                provider=campaign["provider"],
                configured_model=campaign["configured_model"],
                prompt_version=campaign["prompt_version"],
                prompt_sha256=campaign["prompt_sha256"],
                schema_sha256=campaign["canonical_schema_sha256"],
                execution_mode=campaign["execution_mode"],
                cache_identifier=campaign["cache_identifier"],
                batch_id=campaign["provider_job_id"],
                pricing_effective_date=plan["provenance"]["pricing_effective_date"],
            )
            result.update(campaign["generation_config"])
        pricing = load_pricing(ROOT / "config/llm_eval/pricing.yaml")
        summary = _collection_summary(batch_value, plan, results, coverage, pricing, None)
        directory = _artifact_directory(public_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (directory / "predictions.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in results),
            encoding="utf-8",
        )
        failures = [row for row in results if row.get("status") != "success"]
        if failures:
            (directory / "failures.jsonl").write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in failures
                ),
                encoding="utf-8",
            )
        (directory / "operational_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        artifact_sha256 = hashlib.sha256(
            (directory / "operational_summary.json").read_bytes()
            + (directory / "predictions.jsonl").read_bytes()
        ).hexdigest()
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='COLLECTED',artifact_reference=%s,
              artifact_sha256=%s,collected_at=now(),provider_success_count=%s,
              provider_failure_count=%s,valid_count=%s,invalid_count=%s,
              operational_metadata=%s
            WHERE id=%s RETURNING *
            """,
            (
                str(directory),
                artifact_sha256,
                summary["coverage"]["successful_provider_responses"],
                summary["coverage"]["failed_provider_responses"],
                summary["coverage"]["valid_structured_predictions"],
                summary["coverage"]["invalid_structured_predictions"],
                Jsonb(
                    {
                        "provider_status": summary["batch_status"],
                        "coverage": summary["coverage"],
                        "tokens": summary["tokens"],
                        "cost": summary["cost"],
                    }
                ),
                campaign["id"],
            ),
        ).fetchone()
        assert updated is not None
        return dict(updated)


def _collected_recoveries(
    connection: psycopg.Connection[dict[str, Any]], campaign_id: int
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM ai_annotation_campaign_recoveries
            WHERE campaign_id=%s AND status='COLLECTED' ORDER BY attempt_number
            """,
            (campaign_id,),
        ).fetchall()
    ]


def _original_coverage_complete(campaign: dict[str, Any]) -> None:
    metadata = campaign.get("operational_metadata")
    coverage = metadata.get("coverage") if isinstance(metadata, dict) else None
    if not isinstance(coverage, dict):
        raise CampaignError("campaign has no collected coverage metadata")
    planned = int(campaign["planned_count"])
    if (
        int(coverage.get("planned_requests") or -1) != planned
        or int(coverage.get("collected_responses") or -1) != planned
        or int(coverage.get("missing_responses") or 0) != 0
        or int(coverage.get("duplicate_mappings") or 0) != 0
    ):
        raise CampaignError("targeted retry requires complete, duplicate-free provider coverage")


def _recovery_directory(public_id: str, attempt_number: int) -> Path:
    campaign_directory = _artifact_directory(public_id)
    recovery_root = (campaign_directory / "recovery").resolve()
    target = (recovery_root / f"attempt-{attempt_number:03d}").resolve()
    if target.parent != recovery_root:
        raise CampaignError("unsafe campaign recovery artifact path")
    return target


def _records_for_recovery(
    connection: psycopg.Connection[dict[str, Any]], recovery_id: int
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        """
        SELECT turn.turn_key,turn.speech_date AS date,turn.chamber,
               turn.speaker_name_raw,turn.major_heading_original,
               turn.minor_heading_original,turn.business_type,
               turn.text_clean,turn.text_raw
        FROM ai_annotation_campaign_recovery_records recovery_record
        JOIN ai_annotation_campaign_records campaign_record
          ON campaign_record.id=recovery_record.campaign_record_id
        JOIN speaker_turns turn ON turn.id=campaign_record.speaker_turn_id
        WHERE recovery_record.recovery_id=%s
        ORDER BY recovery_record.original_request_index
        """,
        (recovery_id,),
    ).fetchall()
    return tuple(production_record_from_turn(dict(row)) for row in rows)


def _recovery_cache(
    campaign: dict[str, Any], provider: GeminiGateway
) -> tuple[dict[str, Any], bool]:
    cache_plan = _canonical_cache_plan(campaign)
    if cache_plan_status(campaign) == "REUSABLE":
        identifier = str(campaign["cache_identifier"])
        try:
            resource = provider.get_cache(identifier)
            fields = _cache_resource_fields(resource, cache_plan, now=datetime.now(UTC))
            if fields["cache_identifier"] == identifier:
                return fields, False
        except (CampaignError, ValueError):
            pass
    resource = provider.create_cache(cast(dict[str, Any], cache_plan["request"]))
    return _cache_resource_fields(resource, cache_plan, now=datetime.now(UTC)), True


def _mark_recovery_failed(
    settings: WebSettings, recovery_id: int, error: Exception
) -> None:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        connection.execute(
            """
            UPDATE ai_annotation_campaign_recoveries SET status='FAILED',
              failure_metadata=%s WHERE id=%s AND status='PREPARING'
            """,
            (Jsonb({"submission_error": str(error)[:1000]}), recovery_id),
        )


def submit_recovery(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    confirmation: str,
    *,
    gateway: GeminiGateway | None = None,
) -> dict[str, Any]:
    """Submit only unresolved invalid/provider-failed records under frozen provenance."""
    _require_admin(principal)
    if confirmation != RECOVERY_CONFIRMATION:
        raise CampaignError(f"paid recovery requires typing {RECOVERY_CONFIRMATION}")
    provider = gateway or LiveGeminiGateway()
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if row is None:
            raise LookupError("AI annotation campaign not found")
        campaign = dict(row)
        if campaign["status"] != "COLLECTED" or campaign.get("model_prediction_run_id"):
            raise CampaignError("campaign is not eligible for targeted recovery")
        if configuration_class(campaign) != "APPROVED_PRODUCTION":
            raise CampaignError("recovery requires the approved production configuration")
        _original_coverage_complete(campaign)
        active = connection.execute(
            """
            SELECT * FROM ai_annotation_campaign_recoveries
            WHERE campaign_id=%s AND status IN
              ('PREPARING','SUBMITTED','RUNNING','PROVIDER_COMPLETED')
            ORDER BY attempt_number DESC LIMIT 1
            """,
            (campaign["id"],),
        ).fetchone()
        if active is not None:
            raise CampaignError("an unresolved-record recovery job is already active")
        collected = _collected_recoveries(connection, int(campaign["id"]))
        source_directory = _current_effective_artifact_directory(campaign, collected)
        source_path = source_directory / "predictions.jsonl"
        unresolved_digest = hashlib.sha256(b"[")
        unresolved_count = 0
        for result in iter_jsonl(source_path):
            if result.get("status") not in {"invalid", "provider_failure", "missing"}:
                continue
            record_id = result.get("record_id")
            if not isinstance(record_id, str):
                raise CampaignError("unresolved result has no stable record ID")
            if unresolved_count:
                unresolved_digest.update(b",")
            unresolved_digest.update(
                json.dumps(record_id, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            unresolved_count += 1
        unresolved_digest.update(b"]")
        if not unresolved_count:
            raise CampaignError("campaign has no unresolved retry-eligible records")
        unresolved_hash = unresolved_digest.hexdigest()
        attempt_row = connection.execute(
            """
            SELECT COALESCE(max(attempt_number),0)+1 AS attempt_number
            FROM ai_annotation_campaign_recoveries WHERE campaign_id=%s
            """,
            (campaign["id"],),
        ).fetchone()
        assert attempt_row is not None
        attempt_number = int(attempt_row["attempt_number"])
        display_name = f"phase4d-recovery-{public_id}-{attempt_number:03d}"
        recovery = connection.execute(
            """
            INSERT INTO ai_annotation_campaign_recoveries
              (campaign_id,attempt_number,unresolved_set_sha256,request_count,status,
               submission_display_name)
            VALUES (%s,%s,%s,%s,'PREPARING',%s) RETURNING *
            """,
            (
                campaign["id"],
                attempt_number,
                unresolved_hash,
                unresolved_count,
                display_name,
            ),
        ).fetchone()
        assert recovery is not None
        for result in iter_jsonl(source_path):
            if result.get("status") not in {"invalid", "provider_failure", "missing"}:
                continue
            record_id = str(result["record_id"])
            error = result.get("validation_error") or result.get("provider_error")
            rendered_error = (
                str(error)
                if isinstance(error, str)
                else json.dumps(error, sort_keys=True, ensure_ascii=False)
            )
            campaign_record = connection.execute(
                """
                SELECT id FROM ai_annotation_campaign_records
                WHERE campaign_id=%s AND record_id=%s
                """,
                (campaign["id"], record_id),
            ).fetchone()
            if campaign_record is None:
                raise CampaignError("retry record is not present in the campaign")
            connection.execute(
                """
                INSERT INTO ai_annotation_campaign_recovery_records
                  (recovery_id,campaign_record_id,record_id,original_request_index,
                   original_validation_error)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    recovery["id"],
                    campaign_record["id"],
                    record_id,
                    int(result["request_index"]),
                    rendered_error,
                ),
            )

    submission_may_exist = False
    try:
        cache, cache_created = _recovery_cache(campaign, provider)
        with psycopg.connect(
            settings.database.psycopg_url, row_factory=dict_row
        ) as connection:
            records = _records_for_recovery(connection, int(recovery["id"]))
            plan = _provider_plan(
                {**campaign, "cache_identifier": cache["cache_identifier"]},
                records,
                expected_record_set_sha256=unresolved_hash,
                cache_identifier=str(cache["cache_identifier"]),
                display_name=display_name,
            )
            directory = _recovery_directory(public_id, attempt_number)
            directory.mkdir(parents=True, exist_ok=False)
            (directory / "plan.json").write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            lineage_rows = connection.execute(
                """
                SELECT record_id,original_request_index,original_validation_error
                FROM ai_annotation_campaign_recovery_records
                WHERE recovery_id=%s ORDER BY original_request_index
                """,
                (recovery["id"],),
            ).fetchall()
            requests = plan["request"]["batch"]["input_config"]["requests"]["requests"]
            (directory / "requests.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "campaign_id": public_id,
                            "recovery_attempt_number": attempt_number,
                            **dict(lineage),
                            "provider_request": request,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                    for lineage, request in zip(lineage_rows, requests, strict=True)
                ),
                encoding="utf-8",
            )
            cache_metadata = {
                **cast(dict[str, Any], cache["cache_metadata"]),
                "created_for_recovery": cache_created,
            }
            prepared = connection.execute(
                """
                UPDATE ai_annotation_campaign_recoveries SET
                  cache_identifier=%s,cache_content_sha256=%s,cache_model=%s,
                  cache_display_name=%s,cache_created_at=%s,cache_expires_at=%s,
                  cache_verified_at=%s,cache_metadata=%s
                WHERE id=%s AND status='PREPARING' RETURNING id
                """,
                (
                    cache["cache_identifier"],
                    cache["cache_content_sha256"],
                    cache["cache_model"],
                    cache["cache_display_name"],
                    cache["cache_created_at"],
                    cache["cache_expires_at"],
                    cache["cache_verified_at"],
                    Jsonb(cache_metadata),
                    recovery["id"],
                ),
            ).fetchone()
            if prepared is None:
                raise CampaignError("recovery cache provenance could not be persisted")
            connection.commit()
            submission_may_exist = True
            finder = getattr(provider, "find_batch", None)
            provider_job_id = (
                finder(display_name, str(campaign["configured_model"]))
                if callable(finder)
                else None
            )
            if provider_job_id is None:
                provider_job_id = provider.submit(
                    plan, str(campaign["configured_model"])
                )
            updated = connection.execute(
                """
                UPDATE ai_annotation_campaign_recoveries SET status='SUBMITTED',
                  provider_job_id=%s,provider_state='BATCH_STATE_PENDING',
                  submitted_at=now()
                WHERE id=%s AND status='PREPARING' AND provider_job_id IS NULL
                RETURNING *
                """,
                (
                    provider_job_id,
                    recovery["id"],
                ),
            ).fetchone()
            if updated is None:
                raise CampaignError("recovery submission identity could not be persisted")
            return dict(updated)
    except Exception as error:
        if not submission_may_exist:
            _mark_recovery_failed(settings, int(recovery["id"]), error)
        if isinstance(error, (CampaignError, RecoveryArtifactError)):
            raise
        raise CampaignError(f"Gemini recovery submission failed: {error}") from error


def refresh_recovery(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    gateway: GeminiGateway | None = None,
) -> dict[str, Any]:
    _require_admin(principal)
    provider = gateway or LiveGeminiGateway()
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        recovery = connection.execute(
            """
            SELECT recovery.* FROM ai_annotation_campaign_recoveries recovery
            JOIN ai_annotation_campaigns campaign ON campaign.id=recovery.campaign_id
            WHERE campaign.public_id::text=%s AND recovery.status IN
              ('SUBMITTED','RUNNING','PROVIDER_COMPLETED')
            ORDER BY recovery.attempt_number DESC LIMIT 1 FOR UPDATE OF recovery
            """,
            (public_id,),
        ).fetchone()
        if recovery is None or not isinstance(recovery["provider_job_id"], str):
            raise CampaignError("campaign has no active recovery job")
        status = parse_batch_status(provider.status(str(recovery["provider_job_id"])))
        target = (
            "PROVIDER_COMPLETED"
            if status["successful_terminal"]
            else "FAILED"
            if status["terminal"]
            else "RUNNING"
        )
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaign_recoveries SET status=%s,provider_state=%s,
              provider_completed_at=CASE WHEN %s THEN COALESCE(provider_completed_at,now())
                                         ELSE provider_completed_at END,
              failure_metadata=CASE WHEN %s THEN %s ELSE failure_metadata END
            WHERE id=%s RETURNING *
            """,
            (
                target,
                status["state"],
                bool(status["terminal"]),
                target == "FAILED",
                Jsonb({"provider_error": status["error"]}),
                recovery["id"],
            ),
        ).fetchone()
        assert updated is not None
        return dict(updated)


def repair_campaign_results(
    settings: WebSettings, principal: Principal, public_id: str
) -> dict[str, Any]:
    """Create an append-only local repair artifact without contacting a provider."""
    _require_admin(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if row is None:
            raise LookupError("AI annotation campaign not found")
        campaign = dict(row)
        if _campaign_configuration(campaign).prediction_task != "policy":
            raise CampaignError("structural repair is only supported for policy campaigns")
        if campaign["status"] != "COLLECTED" or campaign.get("model_prediction_run_id"):
            raise CampaignError("campaign is not eligible for structural repair")
        active = connection.execute(
            """
            SELECT id FROM ai_annotation_campaign_recoveries
            WHERE campaign_id=%s AND status IN
              ('PREPARING','SUBMITTED','RUNNING','PROVIDER_COMPLETED')
            LIMIT 1
            """,
            (campaign["id"],),
        ).fetchone()
        if active is not None:
            raise CampaignError("structural repair is blocked by an active recovery job")
        recoveries = _collected_recoveries(connection, int(campaign["id"]))
        source_directory = _base_effective_artifact_directory(campaign, recoveries)
        output_directory = structural_repair_directory(source_directory)
        if output_directory.exists():
            summary = json.loads(
                (output_directory / "operational_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            rows = read_jsonl(output_directory / "predictions.jsonl")
        else:
            summary, rows, _artifact_hash = build_structurally_repaired_artifact(
                source_directory, output_directory, _production_domain_codes()
            )
        coverage = summary.get("coverage")
        if not isinstance(coverage, dict):
            raise CampaignError("structural repair artifact has no coverage metadata")
        invalid = int(coverage.get("invalid_structured_predictions") or 0)
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET valid_count=%s,invalid_count=%s
            WHERE id=%s RETURNING *
            """,
            (len(rows) - invalid, invalid, campaign["id"]),
        ).fetchone()
        assert updated is not None
        return dict(updated)


def resolve_campaign_provider_refusals(
    settings: WebSettings, principal: Principal, public_id: str
) -> dict[str, Any]:
    """Create an append-only terminal abstention artifact without a provider call."""
    _require_admin(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if row is None:
            raise LookupError("AI annotation campaign not found")
        campaign = dict(row)
        if campaign["status"] != "COLLECTED" or campaign.get("model_prediction_run_id"):
            raise CampaignError("campaign is not eligible for provider-refusal resolution")
        active = connection.execute(
            """
            SELECT id FROM ai_annotation_campaign_recoveries
            WHERE campaign_id=%s AND status IN
              ('PREPARING','SUBMITTED','RUNNING','PROVIDER_COMPLETED')
            LIMIT 1
            """,
            (campaign["id"],),
        ).fetchone()
        if active is not None:
            raise CampaignError("provider-refusal resolution is blocked by an active recovery job")
        recoveries = _collected_recoveries(connection, int(campaign["id"]))
        source_directory = _current_effective_artifact_directory(campaign, recoveries)
        source_summary = json.loads(
            (source_directory / "operational_summary.json").read_text(encoding="utf-8")
        )
        if isinstance(source_summary, dict) and source_summary.get("provider_refusals"):
            return campaign
        output_directory = provider_refusal_directory(source_directory)
        original_directory = Path(str(campaign["artifact_reference"]))
        attempt_sources = [
            {
                "source_kind": "original_campaign",
                "artifact_reference": str(original_directory),
                "artifact_sha256": artifact_sha256(original_directory),
                "provider_job_id": campaign.get("provider_job_id")
                or f"phase4e:{public_id}",
            },
            *[
                {
                    "source_kind": "targeted_recovery",
                    "recovery_attempt_number": int(recovery["attempt_number"]),
                    "provider_job_id": recovery.get("provider_job_id"),
                    "artifact_reference": recovery.get("artifact_reference"),
                    "artifact_sha256": recovery.get("artifact_sha256"),
                }
                for recovery in recoveries
            ],
        ]
        summary, row_count, _artifact_hash = build_provider_refusal_artifact(
            source_directory, output_directory, attempt_sources
        )
        coverage = summary.get("coverage")
        refusal_metadata = summary.get("provider_refusals")
        if not isinstance(coverage, dict) or not isinstance(refusal_metadata, dict):
            raise CampaignError("provider-refusal artifact has incomplete metadata")
        unresolved = sum(
            int(coverage.get(key) or 0)
            for key in (
                "invalid_structured_predictions",
                "failed_provider_responses",
                "missing_responses",
                "duplicate_mappings",
            )
        )
        orchestration_status = (
            "COMPLETE" if coverage.get("accounting_invariant_ok") else "RECOVERY_REQUIRED"
        )
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET valid_count=%s,invalid_count=%s,
              orchestration_status=%s,operational_metadata=operational_metadata || %s
            WHERE id=%s RETURNING *
            """,
            (
                int(coverage.get("valid_structured_predictions") or 0),
                unresolved,
                orchestration_status,
                Jsonb(
                    {
                        "coverage": coverage,
                        "provider_refusals": refusal_metadata,
                        "effective_artifact_reference": str(output_directory),
                    }
                ),
                campaign["id"],
            ),
        ).fetchone()
        assert row_count == int(campaign["planned_count"])
        assert updated is not None
        return dict(updated)


def recover_recovery_submission(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    gateway: GeminiGateway | None = None,
) -> dict[str, Any]:
    """Recover a provider job when submission succeeded but its receipt was lost."""
    _require_admin(principal)
    provider = gateway or LiveGeminiGateway()
    finder = getattr(provider, "find_batch", None)
    if not callable(finder):
        raise CampaignError("the Gemini gateway cannot recover submitted Batch jobs")
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        recovery = connection.execute(
            """
            SELECT recovery.*,campaign.configured_model
            FROM ai_annotation_campaign_recoveries recovery
            JOIN ai_annotation_campaigns campaign ON campaign.id=recovery.campaign_id
            WHERE campaign.public_id::text=%s AND recovery.status='PREPARING'
            ORDER BY recovery.attempt_number DESC LIMIT 1 FOR UPDATE OF recovery
            """,
            (public_id,),
        ).fetchone()
        if recovery is None:
            raise CampaignError("campaign has no pending recovery submission to recover")
        provider_job_id = finder(
            str(recovery["submission_display_name"]),
            str(recovery["configured_model"]),
        )
        if provider_job_id is None:
            raise CampaignError("no matching Gemini recovery Batch is visible yet")
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaign_recoveries SET status='SUBMITTED',
              provider_job_id=%s,provider_state='BATCH_STATE_PENDING',submitted_at=now()
            WHERE id=%s AND status='PREPARING' AND provider_job_id IS NULL RETURNING *
            """,
            (provider_job_id, recovery["id"]),
        ).fetchone()
        if updated is None:
            raise CampaignError("recovery submission was already recovered")
        return dict(updated)


def collect_recovery(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    gateway: GeminiGateway | None = None,
) -> dict[str, Any]:
    _require_admin(principal)
    provider = gateway or LiveGeminiGateway()
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT campaign.*,recovery.id AS recovery_id,
                   recovery.attempt_number,recovery.status AS recovery_status,
                   recovery.provider_job_id AS recovery_provider_job_id,
                   recovery.cache_identifier AS recovery_cache_identifier,
                   recovery.cache_created_at AS recovery_cache_created_at,
                   recovery.cache_expires_at AS recovery_cache_expires_at,
                   recovery.cache_metadata AS recovery_cache_metadata,
                   recovery.unresolved_set_sha256
            FROM ai_annotation_campaigns campaign
            JOIN ai_annotation_campaign_recoveries recovery
              ON recovery.campaign_id=campaign.id
            WHERE campaign.public_id::text=%s
            ORDER BY recovery.attempt_number DESC LIMIT 1 FOR UPDATE OF recovery
            """,
            (public_id,),
        ).fetchone()
        if row is None:
            raise CampaignError("campaign has no recovery attempt")
        combined = dict(row)
        if combined["recovery_status"] == "COLLECTED":
            return combined
        if combined["recovery_status"] != "PROVIDER_COMPLETED":
            raise CampaignError("recovery provider job is not ready for collection")
        records = _records_for_recovery(connection, int(combined["recovery_id"]))
        plan = _provider_plan(
            {**combined, "cache_identifier": combined["recovery_cache_identifier"]},
            records,
            expected_record_set_sha256=str(combined["unresolved_set_sha256"]),
            cache_identifier=str(combined["recovery_cache_identifier"]),
            display_name=f"phase4d-recovery-{public_id}-{int(combined['attempt_number']):03d}",
        )
        batch_value = provider.status(str(combined["recovery_provider_job_id"]))
        configuration = _campaign_configuration(combined)
        domain_codes = (
            tuple(label.code for label in production_codebook()["taxonomy"].labels)
            if configuration.prediction_task == "policy"
            else ()
        )
        results, coverage = collect_inline_batch(
            batch_value=batch_value, plan=plan, domain_codes=domain_codes,
            prediction_task=configuration.prediction_task,
            records_by_id={str(record["record_id"]): record for record in records},
        )
        lineage = {
            str(value["record_id"]): dict(value)
            for value in connection.execute(
                """
                SELECT record_id,original_request_index,original_validation_error
                FROM ai_annotation_campaign_recovery_records WHERE recovery_id=%s
                """,
                (combined["recovery_id"],),
            ).fetchall()
        }
        for result in results:
            details = lineage[str(result["record_id"])]
            result.update(
                campaign_id=public_id,
                recovery_attempt_number=int(combined["attempt_number"]),
                recovery_provider_job_id=combined["recovery_provider_job_id"],
                original_request_index=details["original_request_index"],
                original_validation_error=details["original_validation_error"],
                replacement_validation_result=result.get("status"),
                provider=combined["provider"],
                configured_model=combined["configured_model"],
                prompt_version=combined["prompt_version"],
                prompt_sha256=combined["prompt_sha256"],
                schema_sha256=combined["canonical_schema_sha256"],
                execution_mode=combined["execution_mode"],
                cache_identifier=combined["recovery_cache_identifier"],
                batch_id=combined["recovery_provider_job_id"],
                pricing_effective_date=plan["provenance"]["pricing_effective_date"],
            )
            result.update(combined["generation_config"])
        cache_metadata = combined.get("recovery_cache_metadata")
        cache_receipt = None
        if isinstance(cache_metadata, dict) and cache_metadata.get("created_for_recovery"):
            cache_receipt = {
                "response": {
                    "createTime": str(combined["recovery_cache_created_at"]),
                    "expireTime": str(combined["recovery_cache_expires_at"]),
                    "usageMetadata": cache_metadata.get("usageMetadata", {}),
                }
            }
        pricing = load_pricing(ROOT / "config/llm_eval/pricing.yaml")
        summary = _collection_summary(
            batch_value, plan, results, coverage, pricing, cache_receipt
        )
        directory = _recovery_directory(public_id, int(combined["attempt_number"]))
        if not directory.exists():
            raise CampaignError("recovery request artifact directory is missing")
        (directory / "provider_response.json").write_text(
            json.dumps(batch_value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (directory / "predictions.jsonl").write_text(
            "".join(
                json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
                for result in results
            ),
            encoding="utf-8",
        )
        failures = [result for result in results if result.get("status") != "success"]
        (directory / "failures.jsonl").write_text(
            "".join(
                json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
                for result in failures
            ),
            encoding="utf-8",
        )
        (directory / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        recovery_hash = hashlib.sha256(
            (directory / "summary.json").read_bytes()
            + (directory / "predictions.jsonl").read_bytes()
        ).hexdigest()
        current_recovery = {
            "attempt_number": combined["attempt_number"],
            "provider_job_id": combined["recovery_provider_job_id"],
            "artifact_reference": str(directory),
            "artifact_sha256": recovery_hash,
            "valid_count": summary["coverage"]["valid_structured_predictions"],
            "invalid_count": summary["coverage"]["invalid_structured_predictions"],
            "operational_metadata": {
                "coverage": summary["coverage"],
                "tokens": summary["tokens"],
                "cost": summary["cost"],
            },
        }
        collected = _collected_recoveries(connection, int(combined["id"]))
        effective_directory = directory / "effective"
        if combined.get("orchestration_mode") == "CHUNKED":
            effective_summary, effective_row_count, effective_hash = (
                build_effective_artifact_streaming(
                    Path(str(combined["artifact_reference"])),
                    [*collected, current_recovery],
                    effective_directory,
                )
            )
        else:
            effective_summary, effective_rows, effective_hash = build_effective_artifact(
                Path(str(combined["artifact_reference"])),
                [*collected, current_recovery],
                effective_directory,
            )
            effective_row_count = len(effective_rows)
        effective_invalid = int(
            effective_summary["coverage"]["invalid_structured_predictions"]
        )
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaign_recoveries SET status='COLLECTED',
              artifact_reference=%s,artifact_sha256=%s,
              effective_artifact_reference=%s,effective_artifact_sha256=%s,
              valid_count=%s,invalid_count=%s,operational_metadata=%s,collected_at=now()
            WHERE id=%s AND status='PROVIDER_COMPLETED' RETURNING *
            """,
            (
                str(directory),
                recovery_hash,
                str(effective_directory),
                effective_hash,
                current_recovery["valid_count"],
                current_recovery["invalid_count"],
                Jsonb(current_recovery["operational_metadata"]),
                combined["recovery_id"],
            ),
        ).fetchone()
        if updated is None:
            raise CampaignError("recovery collection could not be persisted")
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET valid_count=%s,invalid_count=%s
            WHERE id=%s
            """,
            (
                effective_row_count - effective_invalid,
                effective_invalid,
                combined["id"],
            ),
        )
        return dict(updated)


def validate_campaign(
    settings: WebSettings, principal: Principal, public_id: str
) -> dict[str, Any]:
    _require_admin(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign is None:
            raise LookupError("AI annotation campaign not found")
        if campaign["status"] == "VALIDATED":
            return dict(campaign)
        if campaign["status"] != "COLLECTED":
            raise CampaignError("campaign results have not been collected")
        directory = _current_effective_artifact_directory(
            dict(campaign), _collected_recoveries(connection, int(campaign["id"]))
        )
        configuration = _campaign_configuration(dict(campaign))
        expected = {
            "provider": configuration.provider,
            "model": configuration.configured_model,
            "prompt_version": configuration.prompt_version,
            "assembled_prompt_sha256": configuration.prompt_sha256,
            "canonical_schema_sha256": configuration.canonical_schema_sha256,
            "prediction_contract_sha256": configuration.prediction_contract_sha256,
            "provider_schema_sha256": configuration.provider_schema_sha256,
            "execution_mode": configuration.execution_mode,
        }
        if campaign.get("orchestration_mode") == "CHUNKED":
            summary, row_count, _artifact_hash = inspect_prediction_artifact(
                directory, prediction_task=configuration.prediction_task,
                expected_configuration=expected,
            )
        else:
            summary, rows, _artifact_hash = load_prediction_artifact(
                directory, prediction_task=configuration.prediction_task,
                expected_configuration=expected,
            )
            row_count = len(rows)
        coverage = summary["coverage"]
        valid_count = int(coverage.get("valid_structured_predictions") or 0)
        refusal_count = int(coverage.get("provider_refused") or 0)
        terminal_invalid_count = sum(
            1
            for _row in iter_terminal_invalid_artifact(
                directory, prediction_task=configuration.prediction_task
            )
        )
        invalid_reported = int(coverage.get("invalid_structured_predictions") or 0)
        if invalid_reported != terminal_invalid_count:
            raise CampaignError("terminal invalid outcomes do not match collected results")
        if terminal_invalid_count and configuration.prediction_task != "hostility":
            raise CampaignError("terminal invalid outcomes are not supported for this task")
        if row_count != valid_count or valid_count + refusal_count + terminal_invalid_count != int(
            campaign["planned_count"]
        ):
            raise CampaignError("validated result coverage does not equal planned records")
        if any(
            int(coverage.get(key) or 0)
            for key in (
                "failed_provider_responses",
                "missing_responses",
                "duplicate_mappings",
            )
        ):
            raise CampaignError("unresolved provider results or mappings block import")
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='VALIDATED',validated_at=now(),
              valid_count=%s,invalid_count=%s,terminal_invalid_count=%s
            WHERE id=%s RETURNING *
            """,
            (valid_count, 0, terminal_invalid_count, campaign["id"]),
        ).fetchone()
        assert updated is not None
        return dict(updated)


def import_campaign(
    settings: WebSettings, principal: Principal, public_id: str
) -> dict[str, Any]:
    _require_admin(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign is None:
            raise LookupError("AI annotation campaign not found")
        if campaign["status"] == "IMPORTED":
            return dict(campaign)
        if campaign["status"] != "VALIDATED" or campaign["model_prediction_run_id"] is not None:
            raise CampaignError("campaign is not ready for a first prediction import")
        artifact = _current_effective_artifact_directory(
            dict(campaign), _collected_recoveries(connection, int(campaign["id"]))
        )
        configuration = _campaign_configuration(dict(campaign))
        expected = {
            "provider": configuration.provider,
            "model": configuration.configured_model,
            "prompt_version": configuration.prompt_version,
            "assembled_prompt_sha256": configuration.prompt_sha256,
            "canonical_schema_sha256": configuration.canonical_schema_sha256,
            "prediction_contract_sha256": configuration.prediction_contract_sha256,
            "provider_schema_sha256": configuration.provider_schema_sha256,
            "execution_mode": configuration.execution_mode,
        }
        history_rows = connection.execute(
            """SELECT campaign_record.record_id,recovery.attempt_number,
                      recovery.public_id::text AS recovery_id,recovery.provider_job_id,
                      recovery.status,recovery.artifact_reference,
                      recovery_record.original_validation_error
               FROM ai_annotation_campaign_recoveries recovery
               JOIN ai_annotation_campaign_recovery_records recovery_record
                 ON recovery_record.recovery_id=recovery.id
               JOIN ai_annotation_campaign_records campaign_record
                 ON campaign_record.id=recovery_record.campaign_record_id
               WHERE recovery.campaign_id=%s
               ORDER BY campaign_record.record_id,recovery.attempt_number""",
            (campaign["id"],),
        ).fetchall()
        terminal_recovery_history: dict[str, list[dict[str, Any]]] = {}
        for history_row in history_rows:
            terminal_recovery_history.setdefault(str(history_row["record_id"]), []).append(
                dict(history_row)
            )
        result = import_predictions(
            artifact, settings.database, dry_run=False,
            prediction_task=configuration.prediction_task,
            annotation_schema_slug=configuration.annotation_schema_slug,
            annotation_schema_version=configuration.annotation_schema_version,
            expected_configuration=expected,
            terminal_recovery_history=terminal_recovery_history,
            campaign_provenance={
                "campaign_id": str(campaign["public_id"]),
                "campaign_name": str(campaign["name"]),
                "configuration_id": str(campaign["configuration_id"]),
            },
        )
        run = connection.execute(
            "SELECT id FROM model_prediction_runs WHERE artifact_sha256=%s",
            (result["artifact_sha256"],),
        ).fetchone()
        if run is None:
            raise CampaignError("prediction importer did not produce a linked run")
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='IMPORTED',imported_at=now(),
                   model_prediction_run_id=%s
            WHERE id=%s AND model_prediction_run_id IS NULL RETURNING *
            """,
            (run["id"], campaign["id"]),
        ).fetchone()
        if updated is None:
            raise CampaignError("campaign was already linked during import")
        return dict(updated)


def credential_status() -> dict[str, dict[str, object]]:
    return {
        "gemini": {
            "name": "Gemini",
            "configured": bool(
                os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            ),
            "environment_variables": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "purpose": "Required for approved production campaigns, cache creation, and paid Gemini Batch execution.",
        },
        "openai": {
            "name": "OpenAI",
            "configured": bool(os.environ.get("OPENAI_API_KEY")),
            "environment_variables": ("OPENAI_API_KEY",),
            "purpose": "Required for research configurations only; not used by this campaign configuration.",
        },
    }
