"""Read-only research exports reusing canonical Phase 3/4C services."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.llm_eval.review_audit import create_review_audit
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.codebook.service import DownloadKind, canonical_download
from hansard_annotator.web.config import WebSettings

ReviewArtifact = Literal[
    "full",
    "edited",
    "flags",
    "summary",
    "report",
    "package",
]

COMPLETED_REVIEW_ARTIFACTS = frozenset({"summary", "report", "package"})


@dataclass(frozen=True)
class ResearchDownload:
    filename: str
    media_type: str
    content: bytes


def _require_researcher(principal: Principal) -> None:
    if not principal.is_admin:
        raise PermissionError("administrator or research export access required")


def export_catalog(settings: WebSettings, principal: Principal) -> dict[str, Any]:
    _require_researcher(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        runs = [
            dict(row)
            for row in connection.execute(
                """
                SELECT run.public_id,run.provider,run.configured_model,run.prompt_version,
                       run.valid_count,run.imported_at,campaign.name AS campaign_name,
                       count(prediction.id) AS prediction_count,
                       (SELECT count(*) FROM model_prediction_abstentions abstention
                        WHERE abstention.run_id=run.id) AS provider_refusal_count,
                       count(prediction.id) FILTER (WHERE terminal.review_status IS NOT NULL)
                         AS reviewed_count,
                       count(prediction.id) FILTER (WHERE terminal.review_status IS NULL)
                         AS unreviewed_count
                FROM model_prediction_runs run
                LEFT JOIN ai_annotation_campaigns campaign
                  ON campaign.model_prediction_run_id=run.id
                LEFT JOIN model_predictions prediction ON prediction.run_id=run.id
                LEFT JOIN LATERAL (
                  SELECT review_status FROM model_prediction_reviews review
                  WHERE review.model_prediction_id=prediction.id
                    AND review.review_status IN ('accepted','edited','skipped')
                  ORDER BY reviewed_at DESC,id DESC LIMIT 1
                ) terminal ON true
                GROUP BY run.id,campaign.id ORDER BY run.imported_at DESC NULLS LAST,run.id DESC
                """
            ).fetchall()
        ]
        projects = [
            dict(row)
            for row in connection.execute(
                """
                SELECT project.id,project.name,
                       count(annotation.id) FILTER
                         (WHERE annotation.status IN ('submitted','revised')) AS submitted_count
                FROM projects project
                LEFT JOIN tasks task ON task.project_id=project.id
                LEFT JOIN assignments assignment ON assignment.task_id=task.id
                LEFT JOIN annotations annotation ON annotation.assignment_id=assignment.id
                GROUP BY project.id ORDER BY project.name
                """
            ).fetchall()
        ]
    return {"runs": runs, "projects": projects}


def prediction_export(
    settings: WebSettings, principal: Principal, run_public_id: str
) -> ResearchDownload:
    _require_researcher(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        run = connection.execute(
            """
            SELECT id,public_id,provider,configured_model,returned_model,prompt_version,
                   prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
                   provider_schema_sha256,generation_config,execution_mode,record_set_sha256,
                   artifact_sha256,provider_job_id,imported_at
            FROM model_prediction_runs WHERE public_id::text=%s
            """,
            (run_public_id,),
        ).fetchone()
        if run is None:
            raise LookupError("model prediction run not found")
        rows = connection.execute(
            """
            SELECT prediction.public_id AS prediction_id,prediction.record_id,
                   prediction.speaker_turn_id,turn.turn_key,turn.speech_date,turn.chamber,
                   turn.speaker_name_raw,prediction.is_non_policy,
                   prediction.primary_australian_domain,
                   prediction.secondary_australian_domains,prediction.reasoning,
                   prediction.returned_model,prediction.created_at
            FROM model_predictions prediction
            JOIN speaker_turns turn ON turn.id=prediction.speaker_turn_id
            WHERE prediction.run_id=%s ORDER BY prediction.record_id,prediction.id
            """,
            (run["id"],),
        ).fetchall()
    output = io.StringIO(newline="")
    fields = [
        "run_id",
        "provider",
        "configured_model",
        "prompt_version",
        "prompt_sha256",
        "canonical_schema_sha256",
        "prediction_contract_sha256",
        "provider_schema_sha256",
        "generation_config",
        "execution_mode",
        "record_set_sha256",
        "artifact_sha256",
        "provider_job_id",
        "run_imported_at",
        "prediction_id",
        "record_id",
        "speaker_turn_id",
        "turn_key",
        "speech_date",
        "chamber",
        "speaker_name_raw",
        "is_non_policy",
        "primary_australian_domain",
        "secondary_australian_domains",
        "reasoning",
        "returned_model",
        "created_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for raw in rows:
        row = dict(raw)
        row.update(
            run_id=str(run["public_id"]),
            provider=run["provider"],
            configured_model=run["configured_model"],
            prompt_version=run["prompt_version"],
            prompt_sha256=run["prompt_sha256"],
            canonical_schema_sha256=run["canonical_schema_sha256"],
            prediction_contract_sha256=run["prediction_contract_sha256"],
            provider_schema_sha256=run["provider_schema_sha256"],
            generation_config=run["generation_config"],
            execution_mode=run["execution_mode"],
            record_set_sha256=run["record_set_sha256"],
            artifact_sha256=run["artifact_sha256"],
            provider_job_id=run["provider_job_id"],
            run_imported_at=run["imported_at"],
        )
        for key, value in tuple(row.items()):
            if isinstance(value, (list, dict)):
                row[key] = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            elif hasattr(value, "isoformat"):
                row[key] = value.isoformat()
        writer.writerow({field: row.get(field) for field in fields})
    return ResearchDownload(
        f"ai-predictions-{run_public_id}.csv",
        "text/csv; charset=utf-8",
        output.getvalue().encode("utf-8"),
    )


def provider_refusal_export(
    settings: WebSettings, principal: Principal, run_public_id: str
) -> ResearchDownload:
    """Export terminal abstentions separately from genuine model predictions."""
    _require_researcher(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        run = connection.execute(
            "SELECT id,public_id FROM model_prediction_runs WHERE public_id::text=%s",
            (run_public_id,),
        ).fetchone()
        if run is None:
            raise LookupError("model prediction run not found")
        rows = connection.execute(
            """
            SELECT abstention.public_id AS abstention_id,abstention.record_id,
                   abstention.speaker_turn_id,turn.turn_key,turn.speech_date,turn.chamber,
                   turn.speaker_name_raw,abstention.resolution_type,
                   abstention.resolution_reason,abstention.refusal_provenance,
                   abstention.artifact_reference,abstention.created_at
            FROM model_prediction_abstentions abstention
            JOIN speaker_turns turn ON turn.id=abstention.speaker_turn_id
            WHERE abstention.run_id=%s ORDER BY abstention.record_id,abstention.id
            """,
            (run["id"],),
        ).fetchall()
    fields = [
        "run_id",
        "abstention_id",
        "record_id",
        "speaker_turn_id",
        "turn_key",
        "speech_date",
        "chamber",
        "speaker_name_raw",
        "resolution_type",
        "resolution_reason",
        "refusal_provenance",
        "artifact_reference",
        "created_at",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for raw in rows:
        row = dict(raw)
        row["run_id"] = str(run["public_id"])
        for key, value in tuple(row.items()):
            if isinstance(value, (list, dict)):
                row[key] = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            elif hasattr(value, "isoformat"):
                row[key] = value.isoformat()
        writer.writerow({field: row.get(field) for field in fields})
    return ResearchDownload(
        f"ai-provider-refusals-{run_public_id}.csv",
        "text/csv; charset=utf-8",
        output.getvalue().encode("utf-8"),
    )


def _deterministic_zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, files[name])
    return output.getvalue()


def require_complete_review(summary: dict[str, Any], artifact: ReviewArtifact) -> None:
    """Keep finalized audit products distinct from explicitly partial CSVs."""
    counts = summary.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("review audit did not produce count metadata")
    unreviewed = counts.get("unreviewed_predictions")
    if not isinstance(unreviewed, int) or unreviewed < 0:
        raise ValueError("review audit produced an invalid unreviewed count")
    if artifact in COMPLETED_REVIEW_ARTIFACTS and unreviewed:
        raise ValueError(
            "completed audit exports require zero unreviewed predictions; "
            "the review CSV files remain available as clearly partial exports"
        )


def review_export(
    settings: WebSettings,
    principal: Principal,
    run_public_id: str,
    artifact: ReviewArtifact,
) -> ResearchDownload:
    _require_researcher(principal)
    mapping = {
        "full": ("full_review_results.csv", "text/csv; charset=utf-8"),
        "edited": ("edited_cases.csv", "text/csv; charset=utf-8"),
        "flags": ("codebook_flagged_cases.csv", "text/csv; charset=utf-8"),
        "summary": ("review_summary.json", "application/json; charset=utf-8"),
        "report": ("review_audit.md", "text/markdown; charset=utf-8"),
    }
    if artifact not in {*mapping, "package"}:
        raise ValueError("unknown review export artifact")
    with tempfile.TemporaryDirectory(prefix="phase4d-review-audit-") as raw_directory:
        directory = Path(raw_directory)
        summary = create_review_audit(
            settings.database,
            directory,
            run_id=run_public_id,
            latest=False,
        )
        require_complete_review(summary, artifact)
        if artifact == "package":
            names = [
                "full_review_results.csv",
                "edited_cases.csv",
                "codebook_flagged_cases.csv",
                "review_summary.json",
                "review_audit.md",
            ]
            return ResearchDownload(
                f"ai-review-audit-{run_public_id}.zip",
                "application/zip",
                _deterministic_zip({name: (directory / name).read_bytes() for name in names}),
            )
        filename, media_type = mapping[artifact]
        return ResearchDownload(
            f"{run_public_id}-{filename}", media_type, (directory / filename).read_bytes()
        )


def codebook_export(principal: Principal, kind: DownloadKind) -> ResearchDownload:
    _require_researcher(principal)
    artifact = canonical_download(kind)
    return ResearchDownload(artifact.filename, artifact.media_type, artifact.content)
