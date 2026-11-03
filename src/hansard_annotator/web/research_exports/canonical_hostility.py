"""Streaming, configuration-pinned current-corpus hostility export."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.llm_eval.hostility import partisan_hostility
from hansard_annotator.web.campaigns.configurations import (
    GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID,
    approved_configuration,
)
from hansard_annotator.web.config import (
    DEFAULT_EXPORT_MIN_FREE_BYTES,
    MINIMUM_EXPORT_MIN_FREE_BYTES,
)
from hansard_annotator.web.research_exports.canonical_ai import check_export_disk_space

EXPORT_VERSION = "canonical-full-corpus-hostility-v1"
ROW_BATCH_SIZE = 1000


class CanonicalHostilityExportError(ValueError):
    """The canonical hostility dataset cannot be represented without ambiguity."""


FIELDS = [
    "corpus_order", "corpus_public_id", "corpus_slug", "corpus_name",
    "turn_id", "turn_key", "speech_date", "chamber", "source_file", "source_url",
    "turn_sequence", "speaker_id",
    "speaker_name", "major_heading", "minor_heading", "business_type",
    "calculated_word_count", "warning_codes_json", "text_clean", "text_raw",
    "hostility_eligible", "ai_result_status",
    "hostility_present", "target", "partisan_hostility", "evidence_spans",
    "reasoning", "prediction_public_id", "prediction_returned_model", "terminal_outcome_public_id",
    "terminal_failure_category", "terminal_validation_message",
    "terminal_outcome_type",
    "terminal_frozen_classification_json", "terminal_model_evidence_json",
    "terminal_recovery_attempt_history_json",
    "run_public_id", "provider", "configured_model", "returned_model",
    "prediction_task", "annotation_schema_slug", "annotation_schema_version",
    "prompt_version", "prompt_sha256", "canonical_schema_sha256",
    "prediction_contract_sha256", "provider_schema_sha256",
    "generation_config_json", "execution_mode", "run_artifact_sha256",
    "run_record_set_sha256", "provider_job_id", "run_imported_at",
    "campaign_public_id", "campaign_name", "campaign_status",
    "campaign_record_set_sha256", "campaign_artifact_sha256",
    "recovery_lineage_json", "chunk_lineage_json", "human_review_status",
    "human_resolution_method", "human_reviewer_id", "human_reviewed_at",
    "human_annotation_version_id", "human_values_json", "human_review_note",
]

PARQUET_SCHEMA = pa.schema(
    [
        pa.field("corpus_order", pa.int64(), nullable=False),
        *[pa.field(name, pa.string()) for name in FIELDS if name not in {"corpus_order", "hostility_eligible", "partisan_hostility", "speech_date", "run_imported_at", "human_reviewed_at", "human_annotation_version_id", "human_reviewer_id", "turn_sequence", "calculated_word_count"}],
        pa.field("hostility_eligible", pa.bool_(), nullable=False),
        pa.field("partisan_hostility", pa.bool_()),
        pa.field("speech_date", pa.date32()),
        pa.field("turn_sequence", pa.int64()),
        pa.field("calculated_word_count", pa.int64()),
        pa.field("run_imported_at", pa.timestamp("us", tz="UTC")),
        pa.field("human_reviewed_at", pa.timestamp("us", tz="UTC")),
        pa.field("human_annotation_version_id", pa.int64()),
        pa.field("human_reviewer_id", pa.int64()),
    ]
)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def make_row(raw: dict[str, Any], order: int) -> dict[str, Any]:
    prediction_count = int(raw["prediction_match_count"] or 0)
    terminal_count = int(raw["terminal_match_count"] or 0)
    if prediction_count > 1 or terminal_count > 1 or (prediction_count and terminal_count):
        raise CanonicalHostilityExportError(
            f"overlapping exact-configuration hostility outcomes for turn {raw['turn_key']} "
            f"(predictions={prediction_count}, terminal={terminal_count}); resolve campaign precedence explicitly"
        )
    if prediction_count:
        values = raw["prediction_values"]
        if not isinstance(values, dict):
            raise CanonicalHostilityExportError(f"stored hostility values are not an object for {raw['turn_key']}")
        required = {"hostility_present", "target", "evidence_spans"}
        if not required.issubset(values):
            raise CanonicalHostilityExportError(f"stored hostility values are incomplete for {raw['turn_key']}")
        label = values["hostility_present"]
        target = values["target"]
        evidence = values["evidence_spans"]
        if not isinstance(evidence, list):
            raise CanonicalHostilityExportError(f"stored evidence is not a list for {raw['turn_key']}")
        partisan = partisan_hostility(values)
        status = "AI_VALID"
    elif terminal_count:
        values = None
        label = target = evidence = partisan = None
        status = "AI_PROVIDER_REFUSAL" if raw["terminal_outcome_type"] == "PROVIDER_REFUSAL" else "AI_TERMINAL_INVALID"
    else:
        values = None
        label = target = evidence = partisan = None
        status = "NO_USABLE_TEXT" if not raw["usable_text"] else "NO_AI_PREDICTION"
    if prediction_count and not raw["usable_text"]:
        status = "AI_VALID_NO_USABLE_TEXT"
    return {
        "corpus_order": order,
        "corpus_public_id": str(raw["corpus_public_id"]),
        "corpus_slug": raw["corpus_slug"],
        "corpus_name": raw["corpus_name"],
        "turn_id": str(raw["turn_id"]),
        "turn_key": raw["turn_key"],
        "speech_date": raw["speech_date"],
        "chamber": raw["chamber"],
        "source_file": raw["source_file"],
        "source_url": raw["source_url"],
        "turn_sequence": raw["turn_sequence"],
        "speaker_id": raw["speaker_id"],
        "speaker_name": raw["speaker_name"],
        "major_heading": raw["major_heading"],
        "minor_heading": raw["minor_heading"],
        "business_type": raw["business_type"],
        "calculated_word_count": raw["calculated_word_count"],
        "warning_codes_json": _json(raw["warning_codes"]),
        "text_clean": raw["text_clean"],
        "text_raw": raw["text_raw"],
        "hostility_eligible": bool(raw["usable_text"] and raw["latest_policy_is_non_policy"] is False),
        "ai_result_status": status,
        "hostility_present": label,
        "target": target,
        "partisan_hostility": partisan,
        "evidence_spans": _json(evidence),
        "reasoning": raw["reasoning"] if prediction_count else None,
        "prediction_public_id": str(raw["prediction_public_id"]) if prediction_count else None,
        "terminal_outcome_public_id": str(raw["terminal_public_id"]) if terminal_count else None,
        "terminal_failure_category": raw["terminal_failure_category"] if terminal_count else None,
        "terminal_outcome_type": raw["terminal_outcome_type"] if terminal_count else None,
        "terminal_validation_message": raw["terminal_validation_message"] if terminal_count else None,
        "terminal_frozen_classification_json": _json(raw["terminal_frozen_classification"]) if terminal_count else None,
        "terminal_model_evidence_json": _json(raw["terminal_model_evidence"]) if terminal_count else None,
        "terminal_recovery_attempt_history_json": _json(raw["terminal_recovery_attempt_history"]) if terminal_count else None,
        "run_public_id": str(raw["run_public_id"]) if prediction_count or terminal_count else None,
        "provider": raw["provider"] if prediction_count or terminal_count else None,
        "configured_model": raw["configured_model"] if prediction_count or terminal_count else None,
        "returned_model": raw["returned_model"] if prediction_count or terminal_count else None,
        "prediction_returned_model": raw["prediction_returned_model"] if prediction_count else None,
        "prediction_task": "hostility" if prediction_count or terminal_count else None,
        "annotation_schema_slug": raw["annotation_schema_slug"] if prediction_count or terminal_count else None,
        "annotation_schema_version": raw["annotation_schema_version"] if prediction_count or terminal_count else None,
        "prompt_version": raw["prompt_version"] if prediction_count or terminal_count else None,
        "prompt_sha256": raw["prompt_sha256"] if prediction_count or terminal_count else None,
        "canonical_schema_sha256": raw["canonical_schema_sha256"] if prediction_count or terminal_count else None,
        "prediction_contract_sha256": raw["prediction_contract_sha256"] if prediction_count or terminal_count else None,
        "provider_schema_sha256": raw["provider_schema_sha256"] if prediction_count or terminal_count else None,
        "generation_config_json": _json(raw["generation_config"]) if prediction_count or terminal_count else None,
        "execution_mode": raw["execution_mode"] if prediction_count or terminal_count else None,
        "run_artifact_sha256": raw["run_artifact_sha256"] if prediction_count or terminal_count else None,
        "run_record_set_sha256": raw["run_record_set_sha256"] if prediction_count or terminal_count else None,
        "provider_job_id": raw["provider_job_id"] if prediction_count or terminal_count else None,
        "run_imported_at": raw["run_imported_at"] if prediction_count or terminal_count else None,
        "campaign_public_id": str(raw["campaign_public_id"]) if raw["campaign_public_id"] else None,
        "campaign_name": raw["campaign_name"],
        "campaign_status": raw["campaign_status"],
        "campaign_record_set_sha256": raw["campaign_record_set_sha256"],
        "campaign_artifact_sha256": raw["campaign_artifact_sha256"],
        "recovery_lineage_json": _json(raw["recovery_lineage"]),
        "chunk_lineage_json": _json(raw["chunk_lineage"]),
        "human_review_status": raw["human_review_status"],
        "human_resolution_method": raw["human_resolution_method"],
        "human_reviewer_id": raw["human_reviewer_id"],
        "human_reviewed_at": raw["human_reviewed_at"],
        "human_annotation_version_id": raw["human_annotation_version_id"],
        "human_values_json": _json(raw["human_values"]),
        "human_review_note": raw["human_review_note"],
    }


def _query() -> str:
    return """
      WITH frozen_runs AS MATERIALIZED (
        SELECT * FROM model_prediction_runs
        WHERE provider=%s AND configured_model=%s AND prompt_version=%s
          AND prompt_sha256=%s AND canonical_schema_sha256=%s
          AND prediction_contract_sha256=%s AND provider_schema_sha256=%s
          AND generation_config=%s AND execution_mode=%s
          AND prediction_task='hostility' AND status='imported'
      ), campaign_lineage AS (
        SELECT campaign.id,campaign.public_id,campaign.name,campaign.status,
               campaign.record_set_sha256,campaign.artifact_sha256
        FROM ai_annotation_campaigns campaign
      )
      SELECT corpus.public_id AS corpus_public_id,corpus.slug AS corpus_slug,
             corpus.name AS corpus_name,turn.id AS turn_id,turn.turn_key,turn.speech_date,
             turn.chamber,source.relative_path AS source_file,first_fragment.source_url,
             turn.turn_sequence,turn.speaker_id_raw AS speaker_id,turn.speaker_name_raw AS speaker_name,
             turn.major_heading_original AS major_heading,turn.minor_heading_original AS minor_heading,
             turn.business_type,turn.calculated_word_count,turn.warning_codes,
             turn.text_clean,turn.text_raw,
             CASE
               WHEN turn.text_clean IS NOT NULL AND turn.text_clean<>'' THEN btrim(turn.text_clean)<>''
               WHEN turn.text_raw IS NOT NULL AND turn.text_raw<>'' THEN btrim(turn.text_raw)<>''
               ELSE false
             END AS usable_text,
             policy.is_non_policy AS latest_policy_is_non_policy,
             prediction.prediction_count AS prediction_match_count,
             prediction.public_id AS prediction_public_id,prediction.prediction_values,
             prediction.reasoning,prediction.returned_model,
             terminal.outcome_count AS terminal_match_count,
             terminal.public_id AS terminal_public_id,
             terminal.resolution_reason AS terminal_failure_category,
             terminal.resolution_type AS terminal_outcome_type,
             COALESCE(terminal.outcome_provenance->>'validation_error',
                      terminal.outcome_provenance->>'provider_error') AS terminal_validation_message,
             terminal.outcome_provenance->'frozen_classification' AS terminal_frozen_classification,
             terminal.outcome_provenance->'model_supplied_evidence' AS terminal_model_evidence,
             terminal.outcome_provenance->'recovery_attempt_history' AS terminal_recovery_attempt_history,
             COALESCE(prediction.run_public_id,terminal.run_public_id) AS run_public_id,
             COALESCE(prediction.provider,terminal.provider) AS provider,
             COALESCE(prediction.configured_model,terminal.configured_model) AS configured_model,
             COALESCE(prediction.run_returned_model,terminal.returned_model) AS returned_model,
             prediction.returned_model AS prediction_returned_model,
             COALESCE(prediction.annotation_schema_slug,terminal.annotation_schema_slug) AS annotation_schema_slug,
             COALESCE(prediction.annotation_schema_version,terminal.annotation_schema_version) AS annotation_schema_version,
             COALESCE(prediction.prompt_version,terminal.prompt_version) AS prompt_version,
             COALESCE(prediction.prompt_sha256,terminal.prompt_sha256) AS prompt_sha256,
             COALESCE(prediction.canonical_schema_sha256,terminal.canonical_schema_sha256) AS canonical_schema_sha256,
             COALESCE(prediction.prediction_contract_sha256,terminal.prediction_contract_sha256) AS prediction_contract_sha256,
             COALESCE(prediction.provider_schema_sha256,terminal.provider_schema_sha256) AS provider_schema_sha256,
             COALESCE(prediction.generation_config,terminal.generation_config) AS generation_config,
             COALESCE(prediction.execution_mode,terminal.execution_mode) AS execution_mode,
             COALESCE(prediction.artifact_sha256,terminal.artifact_sha256) AS run_artifact_sha256,
             COALESCE(prediction.record_set_sha256,terminal.record_set_sha256) AS run_record_set_sha256,
             COALESCE(prediction.provider_job_id,terminal.provider_job_id) AS provider_job_id,
             COALESCE(prediction.imported_at,terminal.imported_at) AS run_imported_at,
             COALESCE(prediction.campaign_public_id,terminal.campaign_public_id) AS campaign_public_id,
             COALESCE(prediction.campaign_name,terminal.campaign_name) AS campaign_name,
             COALESCE(prediction.campaign_status,terminal.campaign_status) AS campaign_status,
             COALESCE(prediction.campaign_record_set_sha256,terminal.campaign_record_set_sha256) AS campaign_record_set_sha256,
             COALESCE(prediction.campaign_artifact_sha256,terminal.campaign_artifact_sha256) AS campaign_artifact_sha256,
             COALESCE(prediction.recovery_lineage,terminal.recovery_lineage) AS recovery_lineage,
             COALESCE(prediction.chunk_lineage,terminal.chunk_lineage) AS chunk_lineage,
             COALESCE(prediction.human_review_status,terminal.human_review_status) AS human_review_status,
             COALESCE(prediction.human_resolution_method,terminal.human_resolution_method) AS human_resolution_method,
             COALESCE(prediction.human_reviewer_id,terminal.human_reviewer_id) AS human_reviewer_id,
             COALESCE(prediction.human_reviewed_at,terminal.human_reviewed_at) AS human_reviewed_at,
             COALESCE(prediction.human_annotation_version_id,terminal.human_annotation_version_id) AS human_annotation_version_id,
             COALESCE(prediction.human_values,terminal.human_values) AS human_values,
             COALESCE(prediction.human_review_note,terminal.human_review_note) AS human_review_note
      FROM corpora corpus
      JOIN reconstruction_runs reconstruction
        ON reconstruction.preprocessing_run_id=corpus.current_preprocessing_run_id
      JOIN speaker_turns turn ON turn.reconstruction_run_id=reconstruction.id
      JOIN source_file_versions source_version ON source_version.id=turn.source_file_version_id
      JOIN source_files source ON source.id=source_version.source_file_id
      LEFT JOIN speaker_turn_fragments first_lineage
        ON first_lineage.turn_id=turn.id AND first_lineage.ordinal=1
      LEFT JOIN speech_fragments first_fragment ON first_fragment.id=first_lineage.fragment_id
      LEFT JOIN LATERAL (
        SELECT mp.is_non_policy FROM model_predictions mp
        JOIN model_prediction_runs run ON run.id=mp.run_id
        WHERE mp.speaker_turn_id=turn.id AND mp.prediction_task='policy'
        ORDER BY run.imported_at DESC,run.id DESC LIMIT 1
      ) policy ON true
      LEFT JOIN LATERAL (
        SELECT count(*) OVER() AS prediction_count,mp.public_id,mp.prediction_values,mp.reasoning,
               mp.returned_model,run.public_id AS run_public_id,run.provider,run.configured_model,
               run.returned_model AS run_returned_model,run.annotation_schema_slug,
               run.annotation_schema_version,run.prompt_version,run.prompt_sha256,
               run.canonical_schema_sha256,run.prediction_contract_sha256,run.provider_schema_sha256,
               run.generation_config,run.execution_mode,run.artifact_sha256,
               run.record_set_sha256,run.provider_job_id,run.imported_at,
               campaign.public_id AS campaign_public_id,campaign.name AS campaign_name,
               campaign.status AS campaign_status,campaign.record_set_sha256 AS campaign_record_set_sha256,
               campaign.artifact_sha256 AS campaign_artifact_sha256,
               recovery_lineage.items AS recovery_lineage,chunk_lineage.item AS chunk_lineage,
               review.review_status AS human_review_status,NULL::text AS human_resolution_method,
               review.reviewer_user_id AS human_reviewer_id,review.reviewed_at AS human_reviewed_at,
               review.annotation_version_id AS human_annotation_version_id,
               av.values AS human_values,review.note AS human_review_note
        FROM model_predictions mp JOIN frozen_runs run ON run.id=mp.run_id
        LEFT JOIN ai_annotation_campaigns campaign ON campaign.model_prediction_run_id=run.id
        LEFT JOIN campaign_lineage lineage ON lineage.id=campaign.id
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object(
            'attempt_number',recovery.attempt_number,'status',recovery.status,
            'provider_job_id',recovery.provider_job_id,'artifact_sha256',recovery.artifact_sha256,
            'effective_artifact_sha256',recovery.effective_artifact_sha256,
            'recovery_mode',recovery.operational_metadata->>'recovery_mode',
            'original_validation_error',recovery_record.original_validation_error
          ) ORDER BY recovery.attempt_number) AS items
          FROM ai_annotation_campaign_recovery_records recovery_record
          JOIN ai_annotation_campaign_records campaign_record
            ON campaign_record.id=recovery_record.campaign_record_id
          JOIN ai_annotation_campaign_recoveries recovery
            ON recovery.id=recovery_record.recovery_id
          WHERE campaign_record.campaign_id=campaign.id
            AND campaign_record.speaker_turn_id=turn.id
        ) recovery_lineage ON true
        LEFT JOIN LATERAL (
          SELECT jsonb_build_object(
            'chunk_number',batch.chunk_number,'status',batch.status,
            'provider_job_id',batch.provider_job_id,'artifact_sha256',batch.artifact_sha256,
            'record_set_sha256',batch.record_set_sha256,
            'estimated_input_tokens',batch.estimated_input_tokens,
            'input_tokens',batch.input_tokens,'cached_input_tokens',batch.cached_input_tokens,
            'output_tokens',batch.output_tokens,'processing_cost_usd',batch.processing_cost_usd,
            'cache_cost_usd',batch.cache_cost_usd
          ) AS item
          FROM ai_annotation_campaign_records campaign_record
          JOIN ai_annotation_campaign_batches batch
            ON batch.campaign_id=campaign_record.campaign_id
           AND campaign_record.selection_order BETWEEN batch.first_selection_order AND batch.last_selection_order
          WHERE campaign_record.campaign_id=campaign.id
            AND campaign_record.speaker_turn_id=turn.id
          LIMIT 1
        ) chunk_lineage ON true
        LEFT JOIN LATERAL (
          SELECT * FROM model_prediction_reviews r WHERE r.model_prediction_id=mp.id
            AND r.review_status IN ('accepted','edited','skipped')
          ORDER BY r.reviewed_at DESC,r.id DESC LIMIT 1
        ) review ON true
        LEFT JOIN annotation_versions av ON av.id=review.annotation_version_id
        WHERE mp.speaker_turn_id=turn.id AND mp.prediction_task='hostility'
        ORDER BY run.id,mp.id LIMIT 1
      ) prediction ON true
      LEFT JOIN LATERAL (
        SELECT count(*) OVER() AS outcome_count,outcome.public_id,outcome.resolution_reason,
               outcome.outcome_provenance,run.public_id AS run_public_id,run.provider,
               run.configured_model,run.returned_model,run.annotation_schema_slug,
               run.annotation_schema_version,run.prompt_version,run.prompt_sha256,
               run.canonical_schema_sha256,run.prediction_contract_sha256,run.provider_schema_sha256,
               run.generation_config,run.execution_mode,run.artifact_sha256,run.record_set_sha256,
               run.provider_job_id,run.imported_at,campaign.public_id AS campaign_public_id,
               campaign.name AS campaign_name,campaign.status AS campaign_status,
               campaign.record_set_sha256 AS campaign_record_set_sha256,
               campaign.artifact_sha256 AS campaign_artifact_sha256,
               recovery_lineage.items AS recovery_lineage,chunk_lineage.item AS chunk_lineage,
               review.review_status,review.resolution_method AS human_resolution_method,
               review.reviewer_user_id AS human_reviewer_id,review.reviewed_at AS human_reviewed_at,
               review.annotation_version_id AS human_annotation_version_id,
               av.values AS human_values,review.note AS human_review_note
        FROM model_prediction_abstentions outcome JOIN frozen_runs run ON run.id=outcome.run_id
        LEFT JOIN ai_annotation_campaigns campaign ON campaign.model_prediction_run_id=run.id
        LEFT JOIN campaign_lineage lineage ON lineage.id=campaign.id
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object(
            'attempt_number',recovery.attempt_number,'status',recovery.status,
            'provider_job_id',recovery.provider_job_id,'artifact_sha256',recovery.artifact_sha256,
            'effective_artifact_sha256',recovery.effective_artifact_sha256,
            'recovery_mode',recovery.operational_metadata->>'recovery_mode',
            'original_validation_error',recovery_record.original_validation_error
          ) ORDER BY recovery.attempt_number) AS items
          FROM ai_annotation_campaign_recovery_records recovery_record
          JOIN ai_annotation_campaign_records campaign_record
            ON campaign_record.id=recovery_record.campaign_record_id
          JOIN ai_annotation_campaign_recoveries recovery
            ON recovery.id=recovery_record.recovery_id
          WHERE campaign_record.campaign_id=campaign.id
            AND campaign_record.speaker_turn_id=turn.id
        ) recovery_lineage ON true
        LEFT JOIN LATERAL (
          SELECT jsonb_build_object(
            'chunk_number',batch.chunk_number,'status',batch.status,
            'provider_job_id',batch.provider_job_id,'artifact_sha256',batch.artifact_sha256,
            'record_set_sha256',batch.record_set_sha256,
            'estimated_input_tokens',batch.estimated_input_tokens,
            'input_tokens',batch.input_tokens,'cached_input_tokens',batch.cached_input_tokens,
            'output_tokens',batch.output_tokens,'processing_cost_usd',batch.processing_cost_usd,
            'cache_cost_usd',batch.cache_cost_usd
          ) AS item
          FROM ai_annotation_campaign_records campaign_record
          JOIN ai_annotation_campaign_batches batch
            ON batch.campaign_id=campaign_record.campaign_id
           AND campaign_record.selection_order BETWEEN batch.first_selection_order AND batch.last_selection_order
          WHERE campaign_record.campaign_id=campaign.id
            AND campaign_record.speaker_turn_id=turn.id
          LIMIT 1
        ) chunk_lineage ON true
        LEFT JOIN LATERAL (
          SELECT * FROM model_prediction_outcome_reviews r WHERE r.abstention_id=outcome.id
          ORDER BY r.reviewed_at DESC,r.id DESC LIMIT 1
        ) review ON true
        LEFT JOIN annotation_versions av ON av.id=review.annotation_version_id
        WHERE outcome.speaker_turn_id=turn.id
        ORDER BY run.id,outcome.id LIMIT 1
      ) terminal ON true
      WHERE corpus.public_id::text=%s
      ORDER BY turn.id
    """


def _configuration_parameters() -> tuple[Any, ...]:
    configuration = approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID)
    return (
        configuration.provider,
        configuration.configured_model,
        configuration.prompt_version,
        configuration.prompt_sha256,
        configuration.canonical_schema_sha256,
        configuration.prediction_contract_sha256,
        configuration.provider_schema_sha256,
        Jsonb(configuration.generation_config),
        configuration.execution_mode,
    )


def _iter_rows(connection: psycopg.Connection[dict[str, Any]], corpus_id: str) -> Iterator[dict[str, Any]]:
    with connection.cursor(name="canonical_hostility_export") as cursor:
        cursor.itersize = ROW_BATCH_SIZE
        cursor.execute(_query(), (*_configuration_parameters(), corpus_id))
        for raw in cursor:
            yield dict(raw)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_hostility_rows(
    rows: Sequence[dict[str, Any]] | Iterator[dict[str, Any]],
    output_directory: Path,
    *,
    corpus: dict[str, Any],
) -> dict[str, Any]:
    if output_directory.exists() and any(output_directory.iterdir()):
        raise CanonicalHostilityExportError("export destination must be absent or empty")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_directory.name}-", dir=output_directory.parent))
    csv_path = staging / "canonical_hostility.csv"
    parquet_path = staging / "canonical_hostility.parquet"
    manifest_path = staging / "manifest.json"
    count = 0
    last_turn_id: str | None = None
    parquet_batch: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    counts: dict[str, int] = {}
    turn_set_hash = hashlib.sha256()
    try:
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            with pq.ParquetWriter(  # type: ignore[no-untyped-call]
                parquet_path, PARQUET_SCHEMA, compression="zstd", version="2.6"
            ) as parquet:
                for raw in rows:
                    count += 1
                    row = make_row(raw, count)
                    if row["turn_id"] == last_turn_id:
                        raise CanonicalHostilityExportError("duplicate current corpus turn in canonical row stream")
                    last_turn_id = str(row["turn_id"])
                    turn_set_hash.update(
                        json.dumps(
                            [row["turn_id"], row["turn_key"]],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
                    writer.writerow(row)
                    parquet_batch.append(row)
                    if row["run_public_id"]:
                        run_ids.add(str(row["run_public_id"]))
                    status = str(row["ai_result_status"])
                    counts[status] = counts.get(status, 0) + 1
                    if len(parquet_batch) == ROW_BATCH_SIZE:
                        parquet.write_table(pa.Table.from_pylist(parquet_batch, schema=PARQUET_SCHEMA))
                        parquet_batch.clear()
                if parquet_batch:
                    parquet.write_table(pa.Table.from_pylist(parquet_batch, schema=PARQUET_SCHEMA))
        if count != int(corpus["turn_count"]):
            raise CanonicalHostilityExportError(
                f"row count {count} differs from current corpus turn count {corpus['turn_count']}"
            )
        manifest: dict[str, Any] = {
            "format": EXPORT_VERSION,
            "corpus": {"public_id": str(corpus["public_id"]), "slug": corpus["slug"],
                       "name": corpus["name"], "turn_count": count,
                       "preprocessing_run_id": str(corpus["preprocessing_run_id"])},
            "configuration": {
                "configuration_id": GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID,
                "provider": "gemini", "configured_model": "gemini-2.5-flash",
            "prediction_task": "hostility",
            "annotation_schema_slug": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).annotation_schema_slug,
            "annotation_schema_version": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).annotation_schema_version,
                "prompt_version": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prompt_version,
                "prompt_sha256": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prompt_sha256,
                "canonical_schema_sha256": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).canonical_schema_sha256,
                "prediction_contract_sha256": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).prediction_contract_sha256,
                "provider_schema_sha256": approved_configuration(GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID).provider_schema_sha256,
                "generation_config": {"thinking_budget": 0}, "execution_mode": "batch",
            },
            "run_public_ids": sorted(run_ids), "row_count": count,
            "canonical_turn_set_sha256": turn_set_hash.hexdigest(),
            "ai_result_status_counts": dict(sorted(counts.items())),
            "hostility_eligibility_rule": "latest current policy prediction is_non_policy=false and non-blank current canonical text",
            "csv_null_semantics": "empty CSV cell represents SQL NULL; string values are not coerced",
            "files": {
                "canonical_hostility.csv": {"sha256": _file_hash(csv_path), "size": csv_path.stat().st_size},
                "canonical_hostility.parquet": {"sha256": _file_hash(parquet_path), "size": parquet_path.stat().st_size},
            },
        }
        content = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        manifest["manifest_sha256"] = hashlib.sha256(content).hexdigest()
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if output_directory.exists():
            output_directory.rmdir()
        os.replace(staging, output_directory)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def create_canonical_hostility_export(corpus_id: str, output_directory: Path) -> dict[str, Any]:
    settings = DatabaseSettings.from_environment()
    configured_minimum = int(os.getenv("HANSARD_EXPORT_MIN_FREE_BYTES", str(DEFAULT_EXPORT_MIN_FREE_BYTES)))
    if configured_minimum < MINIMUM_EXPORT_MIN_FREE_BYTES:
        raise CanonicalHostilityExportError("export minimum free-space threshold is below the application minimum")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    check_export_disk_space(output_directory.parent, configured_minimum)
    with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        corpus = connection.execute(
            """SELECT corpus.public_id::text AS public_id,corpus.slug,corpus.name,
                      corpus.current_preprocessing_run_id::text AS preprocessing_run_id,
                      count(turn.id)::bigint AS turn_count
               FROM corpora corpus
               LEFT JOIN reconstruction_runs reconstruction
                 ON reconstruction.preprocessing_run_id=corpus.current_preprocessing_run_id
               LEFT JOIN speaker_turns turn ON turn.reconstruction_run_id=reconstruction.id
               WHERE corpus.public_id::text=%s
               GROUP BY corpus.id""",
            (corpus_id,),
        ).fetchone()
        if corpus is None:
            raise LookupError("current corpus not found")
        # The repeatable-read snapshot remains open while the server-side cursor is consumed.
        return export_hostility_rows(_iter_rows(connection, corpus_id), output_directory, corpus=dict(corpus))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-public-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = create_canonical_hostility_export(args.corpus_public_id, args.output)
    except (CanonicalHostilityExportError, LookupError, OSError, psycopg.Error) as exc:
        parser.error(str(exc))
    print(json.dumps({"output": str(args.output), "manifest_sha256": result["manifest_sha256"],
                      "row_count": result["row_count"]}, sort_keys=True))


if __name__ == "__main__":
    main()
