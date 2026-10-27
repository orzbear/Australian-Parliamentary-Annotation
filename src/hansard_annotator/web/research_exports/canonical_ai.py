"""Canonical, configuration-pinned full-corpus AI research export."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.campaigns.service import production_configuration
from hansard_annotator.web.config import MINIMUM_EXPORT_MIN_FREE_BYTES, WebSettings

EXPORT_VERSION = "canonical-full-corpus-ai-v1"
CANONICAL_EXPORT_DIR_PREFIX = "canonical-full-corpus-ai-"
OUTCOME_PREDICTION = "PREDICTION"
OUTCOME_PROVIDER_REFUSAL = "PROVIDER_REFUSAL"
OUTCOME_NO_USABLE_TEXT = "NO_USABLE_TEXT"
ROW_BATCH_SIZE = 1_000


class CanonicalAIExportError(ValueError):
    """The database snapshot cannot form one canonical full-corpus layer."""


@dataclass(frozen=True)
class CanonicalAIExport:
    directory: Path
    csv_path: Path
    parquet_path: Path
    manifest_path: Path
    zip_path: Path
    manifest: dict[str, Any]


FIELDS = [
    "corpus_order",
    "corpus_id",
    "corpus_slug",
    "corpus_name",
    "turn_id",
    "turn_key",
    "record_id",
    "speech_date",
    "chamber",
    "source_file",
    "source_url",
    "turn_sequence",
    "speaker_id",
    "speaker_name",
    "major_heading",
    "minor_heading",
    "business_type",
    "calculated_word_count",
    "warning_codes",
    "text_clean",
    "text_raw",
    "outcome_status",
    "is_non_policy",
    "primary_australian_domain",
    "secondary_australian_domains",
    "reasoning",
    "prediction_id",
    "abstention_id",
    "provider_block_reason",
    "refusal_provenance_json",
    "provider",
    "configured_model",
    "returned_model",
    "prompt_version",
    "prompt_sha256",
    "canonical_schema_sha256",
    "prediction_contract_sha256",
    "provider_schema_sha256",
    "thinking_level",
    "generation_config_json",
    "execution_mode",
    "prediction_run_id",
    "artifact_reference",
    "run_artifact_sha256",
    "run_record_set_sha256",
    "provider_job_id",
    "run_imported_at",
    "prediction_created_at",
    "abstention_created_at",
    "campaign_id",
    "campaign_name",
    "campaign_status",
    "campaign_record_set_sha256",
    "campaign_artifact_sha256",
    "campaign_imported_at",
    "latest_review_status",
    "review_edited",
    "latest_reviewed_at",
    "latest_review_id",
    "latest_review_annotation_version_id",
    "latest_review_note",
    "has_codebook_flag",
    "latest_codebook_flag_note",
    "latest_codebook_flagged_at",
]


def _field(name: str, value_type: pa.DataType, nullable: bool = True) -> pa.Field:
    return pa.field(name, value_type, nullable=nullable)


PARQUET_SCHEMA = pa.schema(
    [
        _field("corpus_order", pa.int64(), False),
        _field("corpus_id", pa.string(), False),
        _field("corpus_slug", pa.string(), False),
        _field("corpus_name", pa.string(), False),
        _field("turn_id", pa.int64(), False),
        _field("turn_key", pa.string(), False),
        _field("record_id", pa.string(), False),
        _field("speech_date", pa.date32(), False),
        _field("chamber", pa.string(), False),
        _field("source_file", pa.string(), False),
        _field("source_url", pa.string()),
        _field("turn_sequence", pa.int64(), False),
        _field("speaker_id", pa.string()),
        _field("speaker_name", pa.string()),
        _field("major_heading", pa.string(), False),
        _field("minor_heading", pa.string(), False),
        _field("business_type", pa.string()),
        _field("calculated_word_count", pa.int64(), False),
        _field("warning_codes", pa.list_(pa.string()), False),
        _field("text_clean", pa.string(), False),
        _field("text_raw", pa.string(), False),
        _field("outcome_status", pa.string(), False),
        _field("is_non_policy", pa.bool_()),
        _field("primary_australian_domain", pa.string()),
        _field("secondary_australian_domains", pa.list_(pa.string())),
        _field("reasoning", pa.string()),
        _field("prediction_id", pa.string()),
        _field("abstention_id", pa.string()),
        _field("provider_block_reason", pa.string()),
        _field("refusal_provenance_json", pa.string()),
        _field("provider", pa.string()),
        _field("configured_model", pa.string()),
        _field("returned_model", pa.string()),
        _field("prompt_version", pa.string()),
        _field("prompt_sha256", pa.string()),
        _field("canonical_schema_sha256", pa.string()),
        _field("prediction_contract_sha256", pa.string()),
        _field("provider_schema_sha256", pa.string()),
        _field("thinking_level", pa.string()),
        _field("generation_config_json", pa.string()),
        _field("execution_mode", pa.string()),
        _field("prediction_run_id", pa.string()),
        _field("artifact_reference", pa.string()),
        _field("run_artifact_sha256", pa.string()),
        _field("run_record_set_sha256", pa.string()),
        _field("provider_job_id", pa.string()),
        _field("run_imported_at", pa.timestamp("us", tz="UTC")),
        _field("prediction_created_at", pa.timestamp("us", tz="UTC")),
        _field("abstention_created_at", pa.timestamp("us", tz="UTC")),
        _field("campaign_id", pa.string()),
        _field("campaign_name", pa.string()),
        _field("campaign_status", pa.string()),
        _field("campaign_record_set_sha256", pa.string()),
        _field("campaign_artifact_sha256", pa.string()),
        _field("campaign_imported_at", pa.timestamp("us", tz="UTC")),
        _field("latest_review_status", pa.string()),
        _field("review_edited", pa.bool_(), False),
        _field("latest_reviewed_at", pa.timestamp("us", tz="UTC")),
        _field("latest_review_id", pa.string()),
        _field("latest_review_annotation_version_id", pa.int64()),
        _field("latest_review_note", pa.string()),
        _field("has_codebook_flag", pa.bool_(), False),
        _field("latest_codebook_flag_note", pa.string()),
        _field("latest_codebook_flagged_at", pa.timestamp("us", tz="UTC")),
    ]
)


def _configuration_parameters() -> tuple[object, ...]:
    configuration = production_configuration()
    return (
        configuration["provider"],
        configuration["configured_model"],
        configuration["prompt_version"],
        configuration["prompt_sha256"],
        configuration["canonical_schema_sha256"],
        configuration["prediction_contract_sha256"],
        configuration["provider_schema_sha256"],
        Jsonb(configuration["generation_config"]),
        configuration["execution_mode"],
    )


def _canonical_query() -> str:
    return """
        WITH matching_runs AS MATERIALIZED (
          SELECT * FROM model_prediction_runs
          WHERE provider=%s AND configured_model=%s AND prompt_version=%s
            AND prompt_sha256=%s AND canonical_schema_sha256=%s
            AND prediction_contract_sha256=%s AND provider_schema_sha256=%s
            AND generation_config=%s AND execution_mode=%s AND status='imported'
        ),
        matching_predictions AS (
          SELECT prediction.*,
                 run.public_id AS run_public_id,run.provider,run.configured_model,
                 run.returned_model AS run_returned_model,run.prompt_version,
                 run.prompt_sha256,run.canonical_schema_sha256,
                 run.prediction_contract_sha256,run.provider_schema_sha256,
                 run.generation_config,run.execution_mode,run.artifact_sha256,
                 run.record_set_sha256 AS run_record_set_sha256,run.provider_job_id,
                 run.imported_at AS run_imported_at,
                 campaign.public_id AS campaign_public_id,campaign.name AS campaign_name,
                 campaign.status AS campaign_status,
                 campaign.record_set_sha256 AS campaign_record_set_sha256,
                 campaign.artifact_sha256 AS campaign_artifact_sha256,
                 campaign.imported_at AS campaign_imported_at,
                 count(*) OVER (PARTITION BY prediction.speaker_turn_id) AS match_count,
                 row_number() OVER (
                   PARTITION BY prediction.speaker_turn_id ORDER BY run.id,prediction.id
                 ) AS match_number
          FROM model_predictions prediction
          JOIN matching_runs run ON run.id=prediction.run_id
          LEFT JOIN ai_annotation_campaigns campaign
            ON campaign.model_prediction_run_id=run.id
        ),
        matching_abstentions AS (
          SELECT abstention.*,
                 run.public_id AS run_public_id,run.provider,run.configured_model,
                 run.returned_model AS run_returned_model,run.prompt_version,
                 run.prompt_sha256,run.canonical_schema_sha256,
                 run.prediction_contract_sha256,run.provider_schema_sha256,
                 run.generation_config,run.execution_mode,run.artifact_sha256,
                 run.record_set_sha256 AS run_record_set_sha256,run.provider_job_id,
                 run.imported_at AS run_imported_at,
                 campaign.public_id AS campaign_public_id,campaign.name AS campaign_name,
                 campaign.status AS campaign_status,
                 campaign.record_set_sha256 AS campaign_record_set_sha256,
                 campaign.artifact_sha256 AS campaign_artifact_sha256,
                 campaign.imported_at AS campaign_imported_at,
                 count(*) OVER (PARTITION BY abstention.speaker_turn_id) AS match_count,
                 row_number() OVER (
                   PARTITION BY abstention.speaker_turn_id ORDER BY run.id,abstention.id
                 ) AS match_number
          FROM model_prediction_abstentions abstention
          JOIN matching_runs run ON run.id=abstention.run_id
          LEFT JOIN ai_annotation_campaigns campaign
            ON campaign.model_prediction_run_id=run.id
        )
        SELECT corpus.public_id AS corpus_public_id,corpus.slug AS corpus_slug,
               corpus.name AS corpus_name,turn.id AS turn_id,turn.turn_key,
               turn.speech_date,turn.chamber,source.relative_path AS source_file,
               first_fragment.source_url,turn.turn_sequence,turn.speaker_id_raw,
               turn.speaker_name_raw,turn.major_heading_original,
               turn.minor_heading_original,turn.business_type,
               turn.calculated_word_count,turn.warning_codes,turn.text_clean,turn.text_raw,
               CASE
                 WHEN turn.text_clean IS NOT NULL AND turn.text_clean<>''
                   THEN btrim(turn.text_clean)<>''
                 WHEN turn.text_raw IS NOT NULL AND turn.text_raw<>''
                   THEN btrim(turn.text_raw)<>''
                 ELSE false
               END AS usable_text,
               prediction.public_id AS prediction_public_id,prediction.prediction_task,
               prediction.prediction_values,prediction.is_non_policy,
               prediction.primary_australian_domain,
               prediction.secondary_australian_domains,prediction.reasoning,
               prediction.returned_model AS prediction_returned_model,
               prediction.artifact_reference AS prediction_artifact_reference,
               prediction.created_at AS prediction_created_at,
               prediction.match_count AS prediction_match_count,
               abstention.public_id AS abstention_public_id,
               abstention.resolution_type,abstention.resolution_reason,
               abstention.outcome_provenance AS refusal_provenance,
               abstention.artifact_reference AS abstention_artifact_reference,
               abstention.created_at AS abstention_created_at,
               abstention.match_count AS abstention_match_count,
               COALESCE(prediction.run_public_id,abstention.run_public_id) AS run_public_id,
               COALESCE(prediction.provider,abstention.provider) AS provider,
               COALESCE(prediction.configured_model,abstention.configured_model)
                 AS configured_model,
               COALESCE(prediction.run_returned_model,abstention.run_returned_model)
                 AS run_returned_model,
               COALESCE(prediction.prompt_version,abstention.prompt_version)
                 AS prompt_version,
               COALESCE(prediction.prompt_sha256,abstention.prompt_sha256) AS prompt_sha256,
               COALESCE(prediction.canonical_schema_sha256,
                        abstention.canonical_schema_sha256) AS canonical_schema_sha256,
               COALESCE(prediction.prediction_contract_sha256,
                        abstention.prediction_contract_sha256) AS prediction_contract_sha256,
               COALESCE(prediction.provider_schema_sha256,
                        abstention.provider_schema_sha256) AS provider_schema_sha256,
               COALESCE(prediction.generation_config,abstention.generation_config)
                 AS generation_config,
               COALESCE(prediction.execution_mode,abstention.execution_mode)
                 AS execution_mode,
               COALESCE(prediction.artifact_sha256,abstention.artifact_sha256)
                 AS run_artifact_sha256,
               COALESCE(prediction.run_record_set_sha256,
                        abstention.run_record_set_sha256) AS run_record_set_sha256,
               COALESCE(prediction.provider_job_id,abstention.provider_job_id)
                 AS provider_job_id,
               COALESCE(prediction.run_imported_at,abstention.run_imported_at)
                 AS run_imported_at,
               COALESCE(prediction.campaign_public_id,abstention.campaign_public_id)
                 AS campaign_public_id,
               COALESCE(prediction.campaign_name,abstention.campaign_name) AS campaign_name,
               COALESCE(prediction.campaign_status,abstention.campaign_status)
                 AS campaign_status,
               COALESCE(prediction.campaign_record_set_sha256,
                        abstention.campaign_record_set_sha256) AS campaign_record_set_sha256,
               COALESCE(prediction.campaign_artifact_sha256,
                        abstention.campaign_artifact_sha256) AS campaign_artifact_sha256,
               COALESCE(prediction.campaign_imported_at,abstention.campaign_imported_at)
                 AS campaign_imported_at,
               terminal.public_id AS latest_review_public_id,
               terminal.review_status AS latest_review_status,
               terminal.reviewed_at AS latest_reviewed_at,
               terminal.annotation_version_id AS latest_review_annotation_version_id,
               terminal.note AS latest_review_note,
               flag.note AS latest_flag_note,flag.reviewed_at AS latest_flagged_at
        FROM speaker_turns turn
        JOIN reconstruction_runs reconstruction ON reconstruction.id=turn.reconstruction_run_id
        JOIN corpora corpus
          ON corpus.current_preprocessing_run_id=reconstruction.preprocessing_run_id
        JOIN source_file_versions source_version ON source_version.id=turn.source_file_version_id
        JOIN source_files source ON source.id=source_version.source_file_id
        LEFT JOIN speaker_turn_fragments first_lineage
          ON first_lineage.turn_id=turn.id AND first_lineage.ordinal=1
        LEFT JOIN speech_fragments first_fragment ON first_fragment.id=first_lineage.fragment_id
        LEFT JOIN matching_predictions prediction
          ON prediction.speaker_turn_id=turn.id AND prediction.match_number=1
        LEFT JOIN matching_abstentions abstention
          ON abstention.speaker_turn_id=turn.id AND abstention.match_number=1
        LEFT JOIN LATERAL (
          SELECT review.public_id,review.review_status,review.reviewed_at,
                 review.annotation_version_id,review.note
          FROM model_prediction_reviews review
          WHERE review.model_prediction_id=prediction.id
            AND review.review_status IN ('accepted','edited','skipped')
          ORDER BY review.reviewed_at DESC,review.id DESC LIMIT 1
        ) terminal ON true
        LEFT JOIN LATERAL (
          SELECT review.note,review.reviewed_at
          FROM model_prediction_reviews review
          WHERE review.model_prediction_id=prediction.id
            AND review.review_status='flagged_codebook'
          ORDER BY review.reviewed_at DESC,review.id DESC LIMIT 1
        ) flag ON true
        WHERE corpus.public_id::text=%s
        ORDER BY turn.speech_date,source.relative_path COLLATE "C",turn.turn_sequence,
                 turn.turn_key COLLATE "C"
    """


def _canonical_json(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _verify_row_provenance(raw: dict[str, Any], turn_key: str) -> str | None:
    configuration = production_configuration()
    for field in (
        "provider",
        "configured_model",
        "prompt_version",
        "prompt_sha256",
        "canonical_schema_sha256",
        "prediction_contract_sha256",
        "provider_schema_sha256",
        "execution_mode",
    ):
        value = raw.get(field)
        if value is not None and value != configuration[field]:
            raise CanonicalAIExportError(
                f"non-canonical {field} ({value!r}) for {turn_key}"
            )
    gen_config = raw.get("generation_config")
    if gen_config is not None:
        if not isinstance(gen_config, dict) or (
            gen_config != configuration["generation_config"]
        ):
            raise CanonicalAIExportError(
                f"non-canonical generation_config ({gen_config!r}) for {turn_key}"
            )
        thinking = gen_config.get("thinking_level")
        return str(thinking) if thinking is not None else None
    return None


def _output_row(raw: dict[str, Any], order: int) -> dict[str, Any]:
    turn_key = str(raw["turn_key"])
    prediction = raw.get("prediction_public_id") is not None
    refusal = raw.get("abstention_public_id") is not None
    usable_text = raw.get("usable_text") is True
    if int(raw.get("prediction_match_count") or 0) > 1:
        raise CanonicalAIExportError(f"multiple production predictions for {turn_key}")
    if int(raw.get("abstention_match_count") or 0) > 1:
        raise CanonicalAIExportError(f"multiple production refusals for {turn_key}")
    if prediction and refusal:
        raise CanonicalAIExportError(f"prediction/refusal conflict for {turn_key}")
    if not usable_text:
        if prediction or refusal:
            raise CanonicalAIExportError(f"no-text turn has a model outcome: {turn_key}")
        outcome = OUTCOME_NO_USABLE_TEXT
        thinking_level = None
    elif prediction:
        thinking_level = _verify_row_provenance(raw, turn_key)
        outcome = OUTCOME_PREDICTION
    elif refusal:
        if (
            raw.get("resolution_type") != OUTCOME_PROVIDER_REFUSAL
            or raw.get("resolution_reason") != "PROHIBITED_CONTENT"
        ):
            raise CanonicalAIExportError(f"unsupported abstention for {turn_key}")
        thinking_level = _verify_row_provenance(raw, turn_key)
        outcome = OUTCOME_PROVIDER_REFUSAL
    else:
        raise CanonicalAIExportError(f"usable turn has no production outcome: {turn_key}")

    classification = outcome == OUTCOME_PREDICTION
    has_run = outcome in (OUTCOME_PREDICTION, OUTCOME_PROVIDER_REFUSAL)
    returned_model = (
        (raw.get("prediction_returned_model") or raw.get("run_returned_model"))
        if has_run
        else None
    )
    artifact_ref = (
        raw.get("prediction_artifact_reference")
        if prediction
        else (raw.get("abstention_artifact_reference") if refusal else None)
    )
    latest_review_status = raw.get("latest_review_status") if prediction else None
    latest_version_id = (
        int(raw["latest_review_annotation_version_id"])
        if prediction and raw.get("latest_review_annotation_version_id") is not None
        else None
    )
    return {
        "corpus_order": order,
        "corpus_id": str(raw["corpus_public_id"]),
        "corpus_slug": raw["corpus_slug"],
        "corpus_name": raw["corpus_name"],
        "turn_id": int(raw["turn_id"]),
        "turn_key": turn_key,
        "record_id": f"turn:{turn_key}",
        "speech_date": raw["speech_date"],
        "chamber": raw["chamber"],
        "source_file": raw["source_file"],
        "source_url": raw.get("source_url"),
        "turn_sequence": int(raw["turn_sequence"]),
        "speaker_id": raw.get("speaker_id_raw"),
        "speaker_name": raw.get("speaker_name_raw"),
        "major_heading": raw["major_heading_original"],
        "minor_heading": raw["minor_heading_original"],
        "business_type": raw.get("business_type"),
        "calculated_word_count": int(raw["calculated_word_count"]),
        "warning_codes": list(raw.get("warning_codes") or []),
        "text_clean": raw["text_clean"],
        "text_raw": raw["text_raw"],
        "outcome_status": outcome,
        "is_non_policy": raw.get("is_non_policy") if classification else None,
        "primary_australian_domain": (
            raw.get("primary_australian_domain") if classification else None
        ),
        "secondary_australian_domains": (
            list(raw.get("secondary_australian_domains") or []) if classification else None
        ),
        "reasoning": raw.get("reasoning") if classification else None,
        "prediction_id": str(raw["prediction_public_id"]) if prediction else None,
        "abstention_id": str(raw["abstention_public_id"]) if refusal else None,
        "provider_block_reason": raw.get("resolution_reason") if refusal else None,
        "refusal_provenance_json": (
            _canonical_json(raw.get("refusal_provenance")) if refusal else None
        ),
        "provider": raw.get("provider") if has_run else None,
        "configured_model": raw.get("configured_model") if has_run else None,
        "returned_model": returned_model,
        "prompt_version": raw.get("prompt_version") if has_run else None,
        "prompt_sha256": raw.get("prompt_sha256") if has_run else None,
        "canonical_schema_sha256": raw.get("canonical_schema_sha256") if has_run else None,
        "prediction_contract_sha256": (
            raw.get("prediction_contract_sha256") if has_run else None
        ),
        "provider_schema_sha256": raw.get("provider_schema_sha256") if has_run else None,
        "thinking_level": thinking_level if has_run else None,
        "generation_config_json": (
            _canonical_json(raw.get("generation_config")) if has_run else None
        ),
        "execution_mode": raw.get("execution_mode") if has_run else None,
        "prediction_run_id": (
            str(raw["run_public_id"]) if has_run and raw.get("run_public_id") else None
        ),
        "artifact_reference": artifact_ref,
        "run_artifact_sha256": raw.get("run_artifact_sha256") if has_run else None,
        "run_record_set_sha256": raw.get("run_record_set_sha256") if has_run else None,
        "provider_job_id": raw.get("provider_job_id") if has_run else None,
        "run_imported_at": raw.get("run_imported_at") if has_run else None,
        "prediction_created_at": raw.get("prediction_created_at") if prediction else None,
        "abstention_created_at": raw.get("abstention_created_at") if refusal else None,
        "campaign_id": (
            str(raw["campaign_public_id"])
            if has_run and raw.get("campaign_public_id")
            else None
        ),
        "campaign_name": raw.get("campaign_name") if has_run else None,
        "campaign_status": raw.get("campaign_status") if has_run else None,
        "campaign_record_set_sha256": (
            raw.get("campaign_record_set_sha256") if has_run else None
        ),
        "campaign_artifact_sha256": (
            raw.get("campaign_artifact_sha256") if has_run else None
        ),
        "campaign_imported_at": raw.get("campaign_imported_at") if has_run else None,
        "latest_review_status": latest_review_status,
        "review_edited": latest_review_status == "edited",
        "latest_reviewed_at": raw.get("latest_reviewed_at") if prediction else None,
        "latest_review_id": (
            str(raw["latest_review_public_id"])
            if prediction and raw.get("latest_review_public_id")
            else None
        ),
        "latest_review_annotation_version_id": latest_version_id,
        "latest_review_note": raw.get("latest_review_note") if prediction else None,
        "has_codebook_flag": bool(raw.get("latest_flagged_at")) if prediction else False,
        "latest_codebook_flag_note": raw.get("latest_flag_note") if prediction else None,
        "latest_codebook_flagged_at": raw.get("latest_flagged_at") if prediction else None,
    }


def _csv_row(row: dict[str, Any]) -> dict[str, object]:
    rendered: dict[str, object] = {}
    for field in FIELDS:
        value = row[field]
        if isinstance(value, (list, dict)):
            rendered[field] = _canonical_json(value) or ""
        elif isinstance(value, bool):
            rendered[field] = "true" if value else "false"
        elif hasattr(value, "isoformat"):
            rendered[field] = value.isoformat()
        elif value is None:
            rendered[field] = ""
        else:
            rendered[field] = value
    return rendered


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_deterministic_zip(path: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(files, key=lambda value: value.name):
            info = zipfile.ZipInfo(source.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            with source.open("rb") as contents, archive.open(info, "w") as target:
                for block in iter(lambda: contents.read(1024 * 1024), b""):
                    target.write(block)


def _rows(
    connection: psycopg.Connection[dict[str, Any]], corpus_public_id: str
) -> Iterator[dict[str, Any]]:
    with connection.cursor(name="canonical_full_corpus_ai_export") as cursor:
        cursor.itersize = ROW_BATCH_SIZE
        cursor.execute(
            _canonical_query(),
            (*_configuration_parameters(), corpus_public_id),
        )
        for raw in cursor:
            yield dict(raw)


def check_export_disk_space(path: Path, minimum_free_bytes: int) -> int:
    """Ensure the export workspace filesystem has enough free disk space."""
    if minimum_free_bytes < MINIMUM_EXPORT_MIN_FREE_BYTES:
        raise CanonicalAIExportError(
            "canonical export minimum free space threshold must be at least "
            f"{MINIMUM_EXPORT_MIN_FREE_BYTES} bytes"
        )
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    if usage.free < minimum_free_bytes:
        raise CanonicalAIExportError(
            f"insufficient free disk space in {probe} "
            f"({usage.free} bytes available; {minimum_free_bytes} bytes required)"
        )
    return int(usage.free)


def cleanup_canonical_export_work_dir(directory: Path, work_root: Path) -> None:
    """Remove only one request's canonical export directory inside work_root."""
    resolved_root = work_root.resolve()
    resolved_dir = directory.resolve()
    if (
        resolved_dir == resolved_root
        or resolved_dir.parent != resolved_root
        or not resolved_dir.name.startswith(CANONICAL_EXPORT_DIR_PREFIX)
    ):
        raise ValueError("refusing to remove path outside canonical export work root")
    shutil.rmtree(resolved_dir, ignore_errors=True)


def create_canonical_full_corpus_export(
    settings: WebSettings,
    principal: Principal,
    corpus_public_id: str,
    output_directory: Path,
    *,
    generated_at: datetime | None = None,
) -> CanonicalAIExport:
    """Stream one immutable production outcome for every current corpus turn."""
    if not principal.is_admin:
        raise PermissionError("administrator or research export access required")
    if output_directory.exists() and any(output_directory.iterdir()):
        raise CanonicalAIExportError("canonical export directory is not empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    check_export_disk_space(output_directory, settings.export_min_free_bytes)
    csv_path = output_directory / "canonical_full_corpus_ai.csv"
    parquet_path = output_directory / "canonical_full_corpus_ai.parquet"
    manifest_path = output_directory / "manifest.json"
    zip_path = output_directory / "canonical_full_corpus_ai.zip"
    created = (generated_at or datetime.now(UTC)).astimezone(UTC)
    counts = {
        OUTCOME_PREDICTION: 0,
        OUTCOME_PROVIDER_REFUSAL: 0,
        OUTCOME_NO_USABLE_TEXT: 0,
    }
    run_ids: set[str] = set()
    campaign_ids: set[str] = set()
    row_count = 0
    previous_turn_id: int | None = None
    previous_turn_key: str | None = None
    corpus: dict[str, Any] | None = None
    parquet_batch: list[dict[str, Any]] = []
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        metadata = connection.execute(
            """
            SELECT corpus.public_id,corpus.slug,corpus.name,count(turn.id)::bigint AS turn_count
            FROM corpora corpus
            LEFT JOIN reconstruction_runs reconstruction
              ON reconstruction.preprocessing_run_id=corpus.current_preprocessing_run_id
            LEFT JOIN speaker_turns turn ON turn.reconstruction_run_id=reconstruction.id
            WHERE corpus.public_id::text=%s
            GROUP BY corpus.id
            """,
            (corpus_public_id,),
        ).fetchone()
        if metadata is None:
            raise LookupError("corpus not found")
        corpus = dict(metadata)
        with csv_path.open("w", encoding="utf-8", newline="") as csv_target:
            writer = csv.DictWriter(csv_target, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            with pq.ParquetWriter(  # type: ignore[no-untyped-call]
                parquet_path,
                PARQUET_SCHEMA,
                compression="zstd",
                version="2.6",
                write_statistics=True,
            ) as parquet_writer:
                for raw in _rows(connection, corpus_public_id):
                    row_count += 1
                    row = _output_row(raw, row_count)
                    turn_id = int(row["turn_id"])
                    turn_key = str(row["turn_key"])
                    if turn_id == previous_turn_id or turn_key == previous_turn_key:
                        raise CanonicalAIExportError("duplicate corpus turn in canonical ordering")
                    previous_turn_id = turn_id
                    previous_turn_key = turn_key
                    outcome = str(row["outcome_status"])
                    counts[outcome] += 1
                    if row["prediction_run_id"]:
                        run_ids.add(str(row["prediction_run_id"]))
                    if row["campaign_id"]:
                        campaign_ids.add(str(row["campaign_id"]))
                    writer.writerow(_csv_row(row))
                    parquet_batch.append(row)
                    if len(parquet_batch) == ROW_BATCH_SIZE:
                        parquet_writer.write_table(
                            pa.Table.from_pylist(parquet_batch, schema=PARQUET_SCHEMA)
                        )
                        parquet_batch.clear()
                if parquet_batch:
                    parquet_writer.write_table(
                        pa.Table.from_pylist(parquet_batch, schema=PARQUET_SCHEMA)
                    )
    assert corpus is not None
    expected_count = int(corpus["turn_count"])
    if row_count != expected_count or sum(counts.values()) != expected_count:
        raise CanonicalAIExportError("canonical outcome counts do not reconcile to corpus total")
    configuration = production_configuration()
    files = {
        csv_path.name: {"sha256": _sha256(csv_path), "bytes": csv_path.stat().st_size},
        parquet_path.name: {
            "sha256": _sha256(parquet_path),
            "bytes": parquet_path.stat().st_size,
        },
    }
    manifest = {
        "export_version": EXPORT_VERSION,
        "generated_at": created.isoformat(),
        "configuration": configuration,
        "corpus": {
            "public_id": str(corpus["public_id"]),
            "slug": corpus["slug"],
            "name": corpus["name"],
        },
        "ordering": (
            "speech_date, source_file bytewise, turn_sequence, turn_key bytewise"
        ),
        "row_count": row_count,
        "outcome_counts": counts,
        "prediction_run_ids": sorted(run_ids),
        "campaign_ids": sorted(campaign_ids),
        "fields": FIELDS,
        "parquet_schema": str(PARQUET_SCHEMA),
        "files": files,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_deterministic_zip(zip_path, [csv_path, parquet_path, manifest_path])
    return CanonicalAIExport(
        directory=output_directory,
        csv_path=csv_path,
        parquet_path=parquet_path,
        manifest_path=manifest_path,
        zip_path=zip_path,
        manifest=manifest,
    )
