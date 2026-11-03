"""Read-only, checksummed preflight for the frozen Gemini hostility campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.llm_eval.costs import load_pricing
from hansard_annotator.llm_eval.operations import canonical_hash
from hansard_annotator.web.campaigns.configurations import (
    GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID,
    approved_configuration,
)
from hansard_annotator.web.campaigns.phase4e import (
    CHARACTERS_PER_TOKEN,
    INVARIANT_INPUT_TOKENS,
    deterministic_chunks,
)


class PreflightError(ValueError):
    """The requested production plan cannot be safely reconciled."""


FETCH_BATCH_SIZE = 2_000


def _server_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    name: str,
    query: str,
    params: tuple[Any, ...] = (),
) -> Iterator[dict[str, Any]]:
    """Stream a potentially large query using a PostgreSQL server-side cursor."""
    cursor = connection.cursor(name=name)
    try:
        cursor.itersize = FETCH_BATCH_SIZE
        cursor.execute(query, params)
        while rows := cursor.fetchmany(FETCH_BATCH_SIZE):
            yield from rows
    finally:
        cursor.close()


def _input_tokens_from_utf8_bytes(byte_length: int) -> int:
    """Match the Phase 4E estimate without materialising canonical speech text."""
    return INVARIANT_INPUT_TOKENS + max(
        1, math.ceil(byte_length / CHARACTERS_PER_TOKEN)
    )


def _report_sha256(report: dict[str, Any]) -> str:
    encoder = json.JSONEncoder(
        sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    digest = hashlib.sha256()
    for segment in encoder.iterencode(report):
        digest.update(segment.encode("utf-8"))
    return digest.hexdigest()


def _write_report_atomically(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(report, temporary, sort_keys=True, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def fits_enqueued_token_budget(
    active_tokens: int, next_chunk_tokens: int, project_limit: int, unknown_jobs: int = 0
) -> bool:
    """Do not claim quota readiness when active job usage is unknown."""
    return (
        unknown_jobs == 0
        and active_tokens >= 0
        and next_chunk_tokens >= 0
        and active_tokens + next_chunk_tokens <= project_limit
    )


def reconcile_sets(
    eligible_ids: list[str],
    completed_ids: list[str],
    terminal_outcome_ids: list[str],
    *,
    active_ids: list[str] | None = None,
) -> dict[str, Any]:
    eligible = set(eligible_ids)
    completed = set(completed_ids)
    invalid = set(terminal_outcome_ids)
    active = set(active_ids or [])
    if len(eligible) != len(eligible_ids):
        raise PreflightError("canonical corpus contains duplicate eligible turn IDs")
    if len(completed) != len(completed_ids) or len(invalid) != len(terminal_outcome_ids):
        raise PreflightError("duplicate imported prediction or terminal outcome turn IDs")
    if completed & invalid:
        raise PreflightError("turns have both imported predictions and terminal-invalid outcomes")
    remaining = eligible - completed - invalid - active
    return {
        "eligible_count": len(eligible),
        "completed_count": len(completed),
        "terminal_outcome_count": len(invalid),
        "active_count": len(active),
        "active_membership_rows": len(active_ids or []),
        "existing_outside_eligible_ids": sorted((completed | invalid) - eligible),
        "duplicate_active_ids": sorted(
            record_id for record_id, count in Counter(active_ids or []).items() if count > 1
        ),
        "remaining_ids": [record_id for record_id in eligible_ids if record_id in remaining],
        "overlap_count": len(active & (completed | invalid)),
    }


def _configuration_values() -> tuple[Any, ...]:
    config = approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID)
    return (
        config.provider,
        config.configured_model,
        config.prompt_version,
        config.prompt_sha256,
        config.canonical_schema_sha256,
        config.prediction_contract_sha256,
        config.provider_schema_sha256,
        json.dumps(config.generation_config, sort_keys=True),
        config.execution_mode,
    )


def build_report(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    corpus_public_id: str,
    seed: str,
    google_enqueued_token_limit: int,
    max_records_per_chunk: int = 1000,
    max_concurrent_enqueued_tokens: int | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    if not seed.strip():
        raise PreflightError("an explicit non-empty production selection seed is required")
    if google_enqueued_token_limit < 1:
        raise PreflightError("Google's documented enqueued Batch token limit is required")
    if max_concurrent_enqueued_tokens is not None and max_concurrent_enqueued_tokens < 1:
        raise PreflightError("maximum concurrent enqueued-token budget must be positive")
    configured_ceiling = int(os.getenv("HANSARD_GEMINI_ENQUEUED_TOKEN_CEILING", "10000000"))
    safety = float(os.getenv("HANSARD_GEMINI_ENQUEUED_TOKEN_SAFETY_FACTOR", "0.90"))
    max_records_per_chunk = min(
        max_records_per_chunk,
        int(os.getenv("HANSARD_GEMINI_MAX_CHUNK_RECORDS", "1000")),
    )
    if not 0 < safety <= 1 or max_records_per_chunk < 1:
        raise PreflightError("campaign chunk ceiling and safety factor are invalid")
    safe_ceiling = min(
        configured_ceiling,
        int(google_enqueued_token_limit * safety),
        max_concurrent_enqueued_tokens or configured_ceiling,
    )
    if safe_ceiling < 1:
        raise PreflightError("effective enqueued token ceiling is not positive")
    config_params = _configuration_values()
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        corpus = connection.execute(
            """SELECT id,public_id::text,slug,name,current_preprocessing_run_id
               FROM corpora WHERE public_id::text=%s""",
            (corpus_public_id,),
        ).fetchone()
        if corpus is None:
            raise PreflightError("current corpus was not found")
        total_turns = connection.execute(
            """SELECT count(*)::bigint AS total FROM speaker_turns turn
               JOIN reconstruction_runs reconstruction ON reconstruction.id=turn.reconstruction_run_id
               WHERE reconstruction.preprocessing_run_id=%s""",
            (corpus["current_preprocessing_run_id"],),
        ).fetchone()
        if total_turns is None:
            raise PreflightError("could not count current corpus turns")
        eligible_query = (
            '''SELECT turn.id::text AS id,turn.turn_key,
                      CASE WHEN turn.text_clean IS NOT NULL AND turn.text_clean<>''
                        THEN octet_length(convert_to(turn.text_clean,'UTF8'))
                        ELSE octet_length(convert_to(COALESCE(turn.text_raw,''),'UTF8'))
                      END AS canonical_text_utf8_bytes,
                      sha256(convert_to(turn.turn_key || ':' || %s,'UTF8')) AS selection_hash
               FROM speaker_turns turn
               JOIN reconstruction_runs reconstruction ON reconstruction.id=turn.reconstruction_run_id
               JOIN corpora corpus ON corpus.current_preprocessing_run_id=reconstruction.preprocessing_run_id
               LEFT JOIN LATERAL (
                 SELECT p.is_non_policy FROM model_predictions p
                 JOIN model_prediction_runs r ON r.id=p.run_id
                 WHERE p.speaker_turn_id=turn.id AND p.prediction_task='policy'
                 ORDER BY r.imported_at DESC,p.run_id DESC LIMIT 1
               ) policy ON true
               WHERE corpus.id=%s
                 AND policy.is_non_policy IS FALSE
                 AND CASE WHEN turn.text_clean IS NOT NULL AND turn.text_clean<>''
                       THEN btrim(turn.text_clean)<>''
                       WHEN turn.text_raw IS NOT NULL AND turn.text_raw<>''
                       THEN btrim(turn.text_raw)<>'' ELSE false END
               ORDER BY selection_hash,turn.turn_key COLLATE "C"'''
        )
        eligible_rows: list[tuple[str, str, int]] = []
        for row in _server_rows(
            connection,
            name="hostility_preflight_eligible",
            query=eligible_query,
            params=(seed, corpus["id"]),
        ):
            eligible_rows.append((
                str(row["id"]), str(row["turn_key"]),
                int(row["canonical_text_utf8_bytes"]),
            ))
        eligible_ids = [row[0] for row in eligible_rows]
        exact_run = """provider=%s AND configured_model=%s AND prompt_version=%s
            AND prompt_sha256=%s AND canonical_schema_sha256=%s
            AND prediction_contract_sha256=%s AND provider_schema_sha256=%s
            AND generation_config=%s::jsonb AND execution_mode=%s
            AND prediction_task='hostility' AND status='imported'"""
        exact_values = (*config_params[:7], config_params[7], config_params[8])
        matched_query = f"""WITH matched AS (
                SELECT turn.id::text AS id,
                    count(DISTINCT prediction.id) AS predictions,
                    count(DISTINCT abstention.id) AS terminal_outcomes,
                        count(DISTINCT abstention.id) FILTER
                          (WHERE abstention.resolution_type='INVALID_OUTPUT') AS terminal_invalid,
                        count(DISTINCT abstention.id) FILTER
                          (WHERE abstention.resolution_type='PROVIDER_REFUSAL') AS provider_refusals,
                    count(DISTINCT review.id) AS manual_reviews
                FROM speaker_turns turn
                JOIN reconstruction_runs reconstruction ON reconstruction.id=turn.reconstruction_run_id
                JOIN corpora corpus ON corpus.current_preprocessing_run_id=reconstruction.preprocessing_run_id
                LEFT JOIN model_predictions prediction ON prediction.speaker_turn_id=turn.id
                  AND prediction.prediction_task='hostility'
                  AND EXISTS (SELECT 1 FROM model_prediction_runs run
                              WHERE run.id=prediction.run_id AND {exact_run})
                LEFT JOIN model_prediction_abstentions abstention ON abstention.speaker_turn_id=turn.id
                  AND EXISTS (SELECT 1 FROM model_prediction_runs run
                              WHERE run.id=abstention.run_id AND {exact_run})
                LEFT JOIN model_prediction_outcome_reviews review ON review.abstention_id=abstention.id
                  AND review.resolution_method IS NOT NULL
                WHERE corpus.id=%s AND (prediction.id IS NOT NULL OR abstention.id IS NOT NULL)
                GROUP BY turn.id
            )
            SELECT COALESCE(array_agg(id ORDER BY id) FILTER (WHERE predictions=1),ARRAY[]::text[]) AS completed_ids,
                   COALESCE(array_agg(id ORDER BY id) FILTER (WHERE terminal_outcomes=1),ARRAY[]::text[]) AS terminal_ids,
                   count(*) FILTER (WHERE predictions>1 OR terminal_outcomes>1
                     OR (predictions>0 AND terminal_outcomes>0)) AS conflict_count,
                   (COALESCE(array_agg(id ORDER BY id) FILTER (WHERE predictions>1 OR terminal_outcomes>1
                     OR (predictions>0 AND terminal_outcomes>0)),ARRAY[]::text[]))[1:20] AS conflict_ids,
                   COALESCE(sum(terminal_invalid),0)::bigint AS terminal_invalid,
                   COALESCE(sum(provider_refusals),0)::bigint AS provider_refusals,
                   COALESCE(sum((manual_reviews>0)::integer),0)::bigint AS manual_reviews
            FROM matched"""
        outcomes = connection.execute(
            matched_query,
            (*exact_values, *exact_values, corpus["id"]),
        ).fetchone()
        if outcomes is None:
            raise PreflightError("could not reconcile imported hostility outcomes")
        if int(outcomes["conflict_count"]):
            raise PreflightError(
                "conflicting or duplicate exact-configuration outcomes: "
                + ", ".join(str(item) for item in outcomes["conflict_ids"])
            )
        completed = [str(item) for item in outcomes["completed_ids"]]
        terminal = [str(item) for item in outcomes["terminal_ids"]]
        terminal_invalid_count = int(outcomes["terminal_invalid"])
        provider_refusal_count = int(outcomes["provider_refusals"])
        manual = int(outcomes["manual_reviews"])
        active_tokens = connection.execute(
            """WITH active_batches AS (
                 SELECT COALESCE(sum(batch.estimated_input_tokens),0)::bigint AS tokens
                 FROM ai_annotation_campaign_batches batch
                 JOIN ai_annotation_campaigns campaign ON campaign.id=batch.campaign_id
                 WHERE campaign.provider='gemini' AND batch.provider='gemini'
                   AND batch.status IN ('SUBMITTING','SUBMITTED','RUNNING','PROVIDER_COMPLETED')
               ), active_legacy AS (
                 SELECT COALESCE(sum(campaign.estimated_input_tokens),0)::bigint AS tokens,
                        count(*) FILTER (WHERE campaign.estimated_input_tokens IS NULL
                          OR campaign.estimated_input_tokens<=0)::bigint AS unknown_jobs
                 FROM ai_annotation_campaigns campaign
                 WHERE campaign.provider='gemini' AND campaign.orchestration_mode='LEGACY'
                   AND campaign.provider_job_id IS NOT NULL
                   AND campaign.status IN ('SUBMITTED','RUNNING','PROVIDER_COMPLETED')
               )
               SELECT active_batches.tokens+active_legacy.tokens AS tokens,
                      active_legacy.unknown_jobs AS unknown_legacy_jobs
               FROM active_batches CROSS JOIN active_legacy"""
        ).fetchone()
        if active_tokens is None:
            raise PreflightError("could not account for active Gemini Batch jobs")
        active_campaign_query = (
            """SELECT campaign.public_id::text AS public_id,campaign.name,campaign.status,
                      campaign.provider,campaign.configured_model,campaign.prompt_sha256,
                      campaign.canonical_schema_sha256,campaign.prediction_contract_sha256,
                      campaign.provider_schema_sha256,campaign.generation_config,
                      campaign.execution_mode,campaign.planned_count,
                      campaign.record_set_sha256,record.speaker_turn_id::text AS turn_id
               FROM ai_annotation_campaigns campaign
               LEFT JOIN ai_annotation_campaign_records record ON record.campaign_id=campaign.id
                 WHERE campaign.prediction_task='hostility'
                 AND campaign.status NOT IN ('DRAFT','IMPORTED')
                 AND campaign.corpus_id=%s
               ORDER BY campaign.created_at,campaign.id,record.selection_order"""
        )
        accepted_outcome_ids = set(completed) | set(terminal)
        frozen_config = approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID)
        exact_active_members: list[str] = []
        other_configuration_active_members: list[str] = []
        active_campaign_summaries: dict[str, dict[str, Any]] = {}
        for row in _server_rows(
            connection,
            name="hostility_preflight_active_campaigns",
            query=active_campaign_query,
            params=(corpus["id"],),
        ):
            key = str(row["public_id"])
            active_campaign_summaries.setdefault(
                key,
                {field: row[field] for field in (
                    "public_id", "name", "status", "provider", "configured_model",
                    "planned_count", "record_set_sha256",
                )},
            )
            if row["turn_id"] is None:
                continue
            turn_id = str(row["turn_id"])
            exact_configuration = (
                row["provider"] == frozen_config.provider
                and row["configured_model"] == frozen_config.configured_model
                and row["prompt_sha256"].strip() == frozen_config.prompt_sha256
                and row["canonical_schema_sha256"].strip() == frozen_config.canonical_schema_sha256
                and row["prediction_contract_sha256"].strip() == frozen_config.prediction_contract_sha256
                and row["provider_schema_sha256"].strip() == frozen_config.provider_schema_sha256
                and row["generation_config"] == frozen_config.generation_config
                and row["execution_mode"] == frozen_config.execution_mode
            )
            if exact_configuration:
                if turn_id not in accepted_outcome_ids:
                    exact_active_members.append(turn_id)
            else:
                other_configuration_active_members.append(turn_id)
        eligible_set = set(eligible_ids)
        stale_active_ids = sorted(set(exact_active_members) - eligible_set)
        current_active_members = [item for item in exact_active_members if item in eligible_set]
        reconciliation = reconcile_sets(
            eligible_ids, completed, terminal, active_ids=current_active_members
        )
        active_exact_duplicates = reconciliation["duplicate_active_ids"]
        other_configuration_active_overlap = sorted(
            set(other_configuration_active_members) & set(reconciliation["remaining_ids"])
        )
        remaining_ids_set = set(reconciliation["remaining_ids"])
        remaining_turn_ids: list[str] = []
        remaining_turn_keys: list[str] = []
        remaining_record_ids: list[str] = []

        def planned_records(
            source_rows: list[tuple[str, str, int]], remaining_ids: set[str]
        ) -> Iterator[dict[str, Any]]:
            selection_order = 0
            for turn_id, turn_key, text_bytes in source_rows:
                if turn_id not in remaining_ids:
                    continue
                record_id = f"turn:{turn_key}"
                remaining_turn_ids.append(turn_id)
                remaining_turn_keys.append(turn_key)
                remaining_record_ids.append(record_id)
                yield {
                    "selection_order": selection_order,
                    "record_id": record_id,
                    "estimated_input_tokens": _input_tokens_from_utf8_bytes(text_bytes),
                }
                selection_order += 1

        eligible_count = len(eligible_rows)
        chunks = list(deterministic_chunks(
            planned_records(eligible_rows, remaining_ids_set),
            max_records=max_records_per_chunk,
            max_input_tokens=safe_ceiling,
        ))
        del eligible_rows
        del remaining_ids_set
        pilot = connection.execute(
            """SELECT COALESCE(sum(run.output_tokens),0)::bigint AS output_tokens,
                      COALESCE(sum(run.requested_count),0)::bigint AS requested_count,
                      COALESCE(sum(run.input_tokens),0)::bigint AS input_tokens,
                      COALESCE(sum(run.cached_input_tokens),0)::bigint AS cached_input_tokens,
                      COALESCE(sum(run.cache_storage_cost_usd),0)::numeric AS cache_storage_cost_usd,
                      COALESCE(sum(run.total_cost_usd),0)::numeric AS actual_pilot_cost_usd,
                      COALESCE(sum(campaign.terminal_invalid_count),0)::bigint AS pilot_terminal_invalid_count,
                      COALESCE(sum((SELECT COALESCE(sum(NULLIF(recovery.operational_metadata->'cost'->>'total_pilot_cost_usd','')::numeric),0)
                                    FROM ai_annotation_campaign_recoveries recovery
                                    WHERE recovery.campaign_id=campaign.id)),0)::numeric AS actual_pilot_recovery_cost_usd
               FROM model_prediction_runs run
               JOIN ai_annotation_campaigns campaign ON campaign.model_prediction_run_id=run.id
               WHERE run.provider=%s AND run.configured_model=%s AND run.prompt_version=%s
                 AND run.prompt_sha256=%s AND run.canonical_schema_sha256=%s
                 AND run.prediction_contract_sha256=%s AND run.provider_schema_sha256=%s
                 AND run.generation_config=%s::jsonb AND run.execution_mode=%s
                 AND run.prediction_task='hostility' AND run.status='imported'
                 AND campaign.selection_definition->>'selection_kind'='production_pilot_1000'""",
            exact_values,
        ).fetchone()
        if pilot is None:
            raise PreflightError("could not read pilot token usage")
        pilot_output_tokens = int(pilot["output_tokens"])
        pilot_requested_count = int(pilot["requested_count"])
        pilot_input_tokens = int(pilot["input_tokens"])
        pilot_cached_input_tokens = int(pilot["cached_input_tokens"])
        pilot_cache_storage_cost = float(pilot["cache_storage_cost_usd"])
        actual_pilot_cost = float(pilot["actual_pilot_cost_usd"])
        pilot_terminal_invalid_count = int(pilot["pilot_terminal_invalid_count"])
        actual_pilot_recovery_cost = float(pilot["actual_pilot_recovery_cost_usd"])
        active_job_tokens = int(active_tokens["tokens"])
        unknown_legacy_jobs = int(active_tokens["unknown_legacy_jobs"])
    pricing = load_pricing(Path("config/llm_eval/pricing.yaml"))
    model_pricing = pricing["models"]["gemini:gemini-2.5-flash"]["modes"]["batch"]
    input_tokens = sum(chunk.estimated_input_tokens for chunk in chunks)
    # Output estimation deliberately uses the 1,000-record pilot's measured per-record output.
    output_per_record = (
        pilot_output_tokens / pilot_requested_count if pilot_requested_count else None
    )
    output_tokens = int(len(remaining_record_ids) * output_per_record) if output_per_record is not None else None
    estimated_cost = None
    if output_tokens is not None:
        estimated_cost = (
            input_tokens * float(model_pricing["input_per_million_usd"])
            + output_tokens * float(model_pricing["output_per_million_usd"])
        ) / 1_000_000
    pilot_cache_ratio = (
        min(1.0, pilot_cached_input_tokens / pilot_input_tokens) if pilot_input_tokens else 0.0
    )
    pilot_mix_cached_tokens = int(input_tokens * pilot_cache_ratio)
    pilot_mix_cost = None
    projected_cache_storage_cost = None
    projected_recovery_cost = None
    if output_tokens is not None:
        pilot_mix_cost = (
            (input_tokens - pilot_mix_cached_tokens) * float(model_pricing["input_per_million_usd"])
            + pilot_mix_cached_tokens * float(model_pricing["cached_input_per_million_usd"])
            + output_tokens * float(model_pricing["output_per_million_usd"])
        ) / 1_000_000
        if pilot_requested_count:
            projected_cache_storage_cost = pilot_cache_storage_cost / pilot_requested_count * len(remaining_record_ids)
            projected_recovery_cost = actual_pilot_recovery_cost / pilot_requested_count * len(remaining_record_ids)
    max_chunk_tokens = max((chunk.estimated_input_tokens for chunk in chunks), default=0)
    quota_fits = fits_enqueued_token_budget(
        active_job_tokens, max_chunk_tokens, google_enqueued_token_limit, unknown_legacy_jobs
    )
    quota_exceeded = not quota_fits and not unknown_legacy_jobs
    report: dict[str, Any] = {
        "format": "hostility-production-preflight-v1",
        "configuration": {
            "configuration_id": GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID,
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "prediction_task": "hostility",
            "annotation_schema_slug": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).annotation_schema_slug,
            "annotation_schema_version": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).annotation_schema_version,
            "prompt_version": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prompt_version,
            "prompt_sha256": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prompt_sha256,
            "canonical_schema_sha256": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).canonical_schema_sha256,
            "prediction_contract_sha256": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prediction_contract_sha256,
            "provider_schema_sha256": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).provider_schema_sha256,
            "generation_config": {"thinking_budget": 0},
            "execution_mode": "batch",
        },
        "corpus": {"public_id": str(corpus["public_id"]), "slug": corpus["slug"],
                   "preprocessing_run_id": corpus["current_preprocessing_run_id"]},
        "counts": {"canonical_turns": int(total_turns["total"]), "hostility_eligible_turns": eligible_count,
                   "not_hostility_eligible": int(total_turns["total"]) - eligible_count,
                   "excluded_accepted_or_active": len(set(completed) | set(terminal) | set(current_active_members)),
                   **{k: v for k, v in reconciliation.items() if k.endswith("_count")},
                   "manual_resolutions": manual, "remaining": len(remaining_record_ids)},
        "existing_terminal_invalid_outcomes": terminal_invalid_count,
        "existing_provider_refusals": provider_refusal_count,
        "remaining_turn_ids": remaining_turn_ids,
        "remaining_turn_keys": remaining_turn_keys,
        "active_campaigns": list(active_campaign_summaries.values()),
        "pending_exact_campaign_turn_ids": sorted(set(exact_active_members)),
        "overlapping_active_turn_ids": active_exact_duplicates,
        "stale_active_turn_ids": stale_active_ids,
        "other_configuration_active_overlap_turn_ids": other_configuration_active_overlap,
        "selection": {"seed": seed, "ordering": "sha256(turn_key + ':' + seed), turn_key COLLATE C"},
        "remaining_record_ids": remaining_record_ids,
        "remaining_record_set_sha256": canonical_hash(remaining_record_ids),
        "expected_provider_job_count": len(chunks),
        "max_estimated_enqueued_tokens": max_chunk_tokens,
        "chunks": [{"number": item.chunk_number, "records": item.record_count,
                    "input_tokens": item.estimated_input_tokens,
                    "record_set_sha256": item.record_set_sha256} for item in chunks],
        "token_estimates": {"input": input_tokens, "output": output_tokens,
                            "output_method": "measured exact-configuration 1,000-record pilot output tokens per requested record" if output_tokens is not None else "unavailable: exact-configuration pilot usage missing",
                            "pilot_measured_input_tokens": pilot_input_tokens,
                            "pilot_measured_cached_input_tokens": pilot_cached_input_tokens,
                            "pilot_measured_output_tokens": pilot_output_tokens,
                            "pilot_measured_requested_records": pilot_requested_count,
                            "pilot_terminal_invalid_records": pilot_terminal_invalid_count,
                            "pilot_cached_input_ratio": pilot_cache_ratio},
        "pricing": {"effective_date": pricing["models"]["gemini:gemini-2.5-flash"]["effective_date"],
                    "source": pricing["models"]["gemini:gemini-2.5-flash"]["source"],
                    "input_per_million_usd": model_pricing["input_per_million_usd"],
                    "cached_input_per_million_usd": model_pricing["cached_input_per_million_usd"],
                    "output_per_million_usd": model_pricing["output_per_million_usd"],
                    "estimated_cost_usd": estimated_cost,
                    "cost_assumption": "conservative uncached input; does not rely on cache survival across provider expiration",
                    "actual_pilot_cost_usd": actual_pilot_cost,
                    "actual_pilot_recovery_cost_usd": actual_pilot_recovery_cost,
                    "pilot_cache_mix_cost_usd": pilot_mix_cost,
                    "projected_cache_storage_cost_usd": projected_cache_storage_cost,
                    "projected_recovery_cost_usd": projected_recovery_cost,
                    "recovery_projection_method": "measured pilot recovery spend per requested record extrapolated; not a hard upper bound"},
        "quota": {"operator_confirmed_project_limit": google_enqueued_token_limit,
                  "configured_campaign_ceiling": configured_ceiling, "safety_factor": safety,
                  "effective_chunk_ceiling": safe_ceiling,
                  "known_active_enqueued_tokens": active_job_tokens,
                  "active_jobs_with_unknown_token_usage": unknown_legacy_jobs,
                  "operator_confirmation_required": True},
        "readiness": {"provider_calls_made": False, "database_writes": False,
                      "ready": bool(remaining_record_ids) and output_tokens is not None and not exact_active_members and not active_exact_duplicates and not stale_active_ids and quota_fits,
                      "blocking_issues": (["records remain assigned to another non-imported exact-configuration campaign"] if exact_active_members else []) + (["exact-configuration campaigns overlap on unresolved turn IDs"] if active_exact_duplicates else []) + (["exact-configuration campaigns include unresolved turns outside the current corpus"] if stale_active_ids else []) + (["measured pilot output-token usage unavailable"] if output_tokens is None else []) + (["active Gemini jobs have unknown token usage"] if unknown_legacy_jobs else []) + (["active jobs plus the next planned chunk exceed the operator-confirmed Batch token limit"] if quota_exceeded else []),
                      "checks": ["exact frozen configuration", "current canonical corpus", "set-based coverage", "deterministic chunk plan", "quota value supplied by operator"]},
    }
    report["sha256"] = _report_sha256(report)
    if output_path is not None:
        _write_report_atomically(output_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-public-id", required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--google-enqueued-token-limit", required=True, type=int)
    parser.add_argument("--max-concurrent-enqueued-tokens", type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    settings = DatabaseSettings.from_environment()
    try:
        with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as conn:
            report = build_report(
                conn,
                corpus_public_id=args.corpus_public_id,
                seed=args.seed,
                google_enqueued_token_limit=args.google_enqueued_token_limit,
                max_concurrent_enqueued_tokens=args.max_concurrent_enqueued_tokens,
                output_path=args.output,
            )
    except Exception as exc:
        parser.error(str(exc))
    print(json.dumps({"report": str(args.output), "sha256": report["sha256"], "ready": report["readiness"]["ready"]}))


if __name__ == "__main__":
    main()
