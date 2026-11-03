"""Read-only, checksummed preflight for the frozen Gemini hostility campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
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
    deterministic_chunks,
    estimate_record_input_tokens,
)


class PreflightError(ValueError):
    """The requested production plan cannot be safely reconciled."""


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
        eligible_rows = connection.execute(
            """SELECT turn.id::text AS id,turn.turn_key,
                      turn.text_clean,turn.text_raw,
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
               ORDER BY selection_hash,turn.turn_key COLLATE "C""",
            (seed, corpus["id"]),
        ).fetchall()
        eligible_ids = [str(row["id"]) for row in eligible_rows]
        exact_run = """provider=%s AND configured_model=%s AND prompt_version=%s
            AND prompt_sha256=%s AND canonical_schema_sha256=%s
            AND prediction_contract_sha256=%s AND provider_schema_sha256=%s
            AND generation_config=%s::jsonb AND execution_mode=%s
            AND prediction_task='hostility' AND status='imported'"""
        exact_values = (*config_params[:7], config_params[7], config_params[8])
        matched = connection.execute(
            f"""SELECT turn.id::text AS id,
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
                GROUP BY turn.id""",
            (*exact_values, *exact_values, corpus["id"]),
        ).fetchall()
        duplicate_rows = [
            row
            for row in matched
            if int(row["predictions"]) > 1 or int(row["terminal_outcomes"]) > 1
            or (int(row["predictions"]) and int(row["terminal_outcomes"]))
        ]
        if duplicate_rows:
            raise PreflightError(
                "conflicting or duplicate exact-configuration outcomes: "
                + ", ".join(str(row["id"]) for row in duplicate_rows[:20])
            )
        completed = [str(row["id"]) for row in matched if int(row["predictions"]) == 1]
        terminal = [str(row["id"]) for row in matched if int(row["terminal_outcomes"]) == 1]
        terminal_invalid_count = sum(int(row["terminal_invalid"]) for row in matched)
        provider_refusal_count = sum(int(row["provider_refusals"]) for row in matched)
        manual = sum(int(row["manual_reviews"]) > 0 for row in matched)
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
        active_campaign_rows = connection.execute(
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
               ORDER BY campaign.created_at,campaign.id,record.selection_order""",
            (corpus["id"],),
        ).fetchall()
        accepted_outcome_ids = set(completed) | set(terminal)
        exact_active_members = [
            str(row["turn_id"])
            for row in active_campaign_rows
            if row["turn_id"] is not None
            and row["provider"] == "gemini"
            and row["configured_model"] == "gemini-2.5-flash"
            and row["prompt_sha256"].strip() == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prompt_sha256
            and row["canonical_schema_sha256"].strip() == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).canonical_schema_sha256
            and row["prediction_contract_sha256"].strip() == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prediction_contract_sha256
            and row["provider_schema_sha256"].strip() == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).provider_schema_sha256
            and row["generation_config"] == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).generation_config
            and row["execution_mode"] == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).execution_mode
            and str(row["turn_id"]) not in accepted_outcome_ids
        ]
        eligible_set = set(eligible_ids)
        stale_active_ids = sorted(set(exact_active_members) - eligible_set)
        current_active_members = [item for item in exact_active_members if item in eligible_set]
        reconciliation = reconcile_sets(
            eligible_ids, completed, terminal, active_ids=current_active_members
        )
        active_campaign_summaries: dict[str, dict[str, Any]] = {}
        for row in active_campaign_rows:
            key = str(row["public_id"])
            active_campaign_summaries.setdefault(
                key,
                {field: row[field] for field in (
                    "public_id", "name", "status", "provider", "configured_model",
                    "planned_count", "record_set_sha256",
                )},
            )
        active_exact_duplicates = reconciliation["duplicate_active_ids"]
        other_configuration_active_members = [
            str(row["turn_id"])
            for row in active_campaign_rows
            if row["turn_id"] is not None
            and not (
                row["provider"] == "gemini"
                and row["configured_model"] == "gemini-2.5-flash"
                and row["prompt_sha256"].strip() == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prompt_sha256
                and row["canonical_schema_sha256"].strip() == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).canonical_schema_sha256
                and row["prediction_contract_sha256"].strip() == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prediction_contract_sha256
                and row["provider_schema_sha256"].strip() == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).provider_schema_sha256
                and row["generation_config"] == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).generation_config
                and row["execution_mode"] == approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).execution_mode
            )
        ]
        other_configuration_active_overlap = sorted(
            set(other_configuration_active_members) & set(reconciliation["remaining_ids"])
        )
        records = []
        lookup = {str(row["id"]): row for row in eligible_rows}
        for order, record_id in enumerate(reconciliation["remaining_ids"]):
            row = lookup[record_id]
            text_value = row["text_clean"] or row["text_raw"] or ""
            records.append(
                {
                    "selection_order": order,
                    "turn_id": record_id,
                    "turn_key": str(row["turn_key"]),
                    "record_id": f"turn:{row['turn_key']}",
                    "estimated_input_tokens": estimate_record_input_tokens(text_value),
                }
            )
        chunks = list(
            deterministic_chunks(
                records,
                max_records=max_records_per_chunk,
                max_input_tokens=safe_ceiling,
            )
        )
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
    input_tokens = sum(int(item["estimated_input_tokens"]) for item in records)
    # Output estimation deliberately uses the 1,000-record pilot's measured per-record output.
    output_per_record = (
        pilot_output_tokens / pilot_requested_count if pilot_requested_count else None
    )
    output_tokens = int(len(records) * output_per_record) if output_per_record is not None else None
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
            projected_cache_storage_cost = pilot_cache_storage_cost / pilot_requested_count * len(records)
            projected_recovery_cost = actual_pilot_recovery_cost / pilot_requested_count * len(records)
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
        "counts": {"canonical_turns": int(total_turns["total"]), "hostility_eligible_turns": len(eligible_rows),
                   "not_hostility_eligible": int(total_turns["total"]) - len(eligible_rows),
                   "excluded_accepted_or_active": len(set(completed) | set(terminal) | set(current_active_members)),
                   **{k: v for k, v in reconciliation.items() if k.endswith("_count")},
                   "manual_resolutions": manual, "remaining": len(records)},
        "existing_terminal_invalid_outcomes": terminal_invalid_count,
        "existing_provider_refusals": provider_refusal_count,
        "remaining_turn_ids": [item["turn_id"] for item in records],
        "remaining_turn_keys": [item["turn_key"] for item in records],
        "active_campaigns": list(active_campaign_summaries.values()),
        "pending_exact_campaign_turn_ids": sorted(set(exact_active_members)),
        "overlapping_active_turn_ids": active_exact_duplicates,
        "stale_active_turn_ids": stale_active_ids,
        "other_configuration_active_overlap_turn_ids": other_configuration_active_overlap,
        "selection": {"seed": seed, "ordering": "sha256(turn_key + ':' + seed), turn_key COLLATE C"},
        "remaining_record_ids": [item["record_id"] for item in records],
        "remaining_record_set_sha256": canonical_hash([x["record_id"] for x in records]),
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
                      "ready": bool(records) and output_tokens is not None and not exact_active_members and not active_exact_duplicates and not stale_active_ids and quota_fits,
                      "blocking_issues": (["records remain assigned to another non-imported exact-configuration campaign"] if exact_active_members else []) + (["exact-configuration campaigns overlap on unresolved turn IDs"] if active_exact_duplicates else []) + (["exact-configuration campaigns include unresolved turns outside the current corpus"] if stale_active_ids else []) + (["measured pilot output-token usage unavailable"] if output_tokens is None else []) + (["active Gemini jobs have unknown token usage"] if unknown_legacy_jobs else []) + (["active jobs plus the next planned chunk exceed the operator-confirmed Batch token limit"] if quota_exceeded else []),
                      "checks": ["exact frozen configuration", "current canonical corpus", "set-based coverage", "deterministic chunk plan", "quota value supplied by operator"]},
    }
    canonical_json = json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    report["sha256"] = hashlib.sha256(canonical_json).hexdigest()
    if output_path is not None:
        output_path.write_text(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
