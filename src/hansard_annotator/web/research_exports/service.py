"""Read-only research exports reusing canonical Phase 3/4C services."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.llm_eval.hostility import (
    MODEL_TARGET_CODES,
    PRESENCE_CODES,
    partisan_hostility,
)
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


@dataclass(frozen=True)
class ResearchFileDownload:
    filename: str
    media_type: str
    path: Path


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
                        WHERE abstention.run_id=run.id
                          AND abstention.resolution_type='PROVIDER_REFUSAL') AS provider_refusal_count,
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
        corpora = [
            dict(row)
            for row in connection.execute(
                """
                SELECT corpus.public_id,corpus.slug,corpus.name,
                       count(turn.id)::bigint AS turn_count
                FROM corpora corpus
                JOIN reconstruction_runs reconstruction
                  ON reconstruction.preprocessing_run_id=corpus.current_preprocessing_run_id
                JOIN speaker_turns turn ON turn.reconstruction_run_id=reconstruction.id
                GROUP BY corpus.id ORDER BY corpus.name,corpus.id
                """
            ).fetchall()
        ]
    return {"runs": runs, "projects": projects, "corpora": corpora}


def prediction_export(
    settings: WebSettings, principal: Principal, run_public_id: str
) -> ResearchFileDownload:
    _require_researcher(principal)
    settings.export_work_root.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        run = connection.execute(
            """
            SELECT id,public_id,prediction_task,provider,configured_model,prompt_version,
                   prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
                   provider_schema_sha256,generation_config,execution_mode,record_set_sha256,
                   artifact_sha256,provider_job_id,imported_at,valid_count
            FROM model_prediction_runs WHERE public_id::text=%s
            """,
            (run_public_id,),
        ).fetchone()
        if run is None:
            raise LookupError("model prediction run not found")
        task = run["prediction_task"]
        if task not in {"policy", "hostility"}:
            raise ValueError(f"unsupported prediction task for CSV export: {task!r}")

        source_identity = {
            "export_version": 1,
            "run_id": int(run["id"]),
            "run_public_id": str(run["public_id"]),
            "prediction_task": task,
            "artifact_sha256": str(run["artifact_sha256"]),
            "record_set_sha256": str(run["record_set_sha256"]),
            "valid_count": int(run["valid_count"]),
        }
        source_sha256 = hashlib.sha256(
            json.dumps(source_identity, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        cache_stem = f"ai-predictions-{int(run['id'])}-{source_sha256}"
        output_path = settings.export_work_root / f"{cache_stem}.csv"
        manifest_path = settings.export_work_root / f"{cache_stem}.json"
        if _verified_prediction_export_cache(
            output_path, manifest_path, source_identity, source_sha256
        ):
            return ResearchFileDownload(
                f"ai-predictions-{run['public_id']}.csv",
                "text/csv; charset=utf-8",
                output_path,
            )

        free_bytes = shutil.disk_usage(settings.export_work_root).free
        if free_bytes < settings.export_min_free_bytes:
            raise OSError(
                f"insufficient free disk space in {settings.export_work_root} "
                f"({free_bytes} bytes available; {settings.export_min_free_bytes} bytes required)"
            )

        fields = _prediction_export_fields(task)
        cursor = connection.cursor(name=f"prediction_export_{int(run['id'])}")
        cursor.itersize = 500
        cursor.execute(
            """SELECT prediction.public_id AS prediction_id,prediction.record_id,
                      prediction.speaker_turn_id,prediction.prediction_task,
                      prediction.prediction_values,turn.turn_key,turn.speech_date,turn.chamber,
                      turn.speaker_name_raw,prediction.is_non_policy,
                      prediction.primary_australian_domain,
                      prediction.secondary_australian_domains,prediction.reasoning,
                      prediction.returned_model,prediction.created_at
               FROM model_predictions prediction
               JOIN speaker_turns turn ON turn.id=prediction.speaker_turn_id
               WHERE prediction.run_id=%s ORDER BY prediction.record_id""",
            (run["id"],),
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{cache_stem}-", suffix=".tmp", dir=settings.export_work_root
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=fields,
                    lineterminator="\n",
                    extrasaction="ignore",
                )
                writer.writeheader()
                row_count = 0
                for raw in cursor:
                    row = dict(raw)
                    _validate_and_prepare_prediction_export_row(row, task, run)
                    for key in fields:
                        value = row.get(key)
                        if isinstance(value, (list, dict)):
                            row[key] = json.dumps(
                                value,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        elif value is not None and hasattr(value, "isoformat"):
                            row[key] = value.isoformat()
                    writer.writerow(row)
                    row_count += 1
                if row_count != int(run["valid_count"]):
                    raise ValueError(
                        "prediction row count does not match the run's valid count"
                    )
                output.flush()
                os.fsync(output.fileno())
            csv_sha256 = _sha256_file(temporary_path)
            csv_bytes = temporary_path.stat().st_size
            os.replace(temporary_path, output_path)
            _write_prediction_export_manifest(
                manifest_path,
                {
                    "source_identity": source_identity,
                    "source_sha256": source_sha256,
                    "csv_sha256": csv_sha256,
                    "csv_bytes": csv_bytes,
                    "row_count": row_count,
                },
            )
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        finally:
            cursor.close()
    return ResearchFileDownload(
        f"ai-predictions-{run['public_id']}.csv", "text/csv; charset=utf-8", output_path
    )


def _prediction_export_fields(task: str) -> list[str]:
    metadata_fields = [
        "run_id", "provider", "configured_model", "prompt_version", "prompt_sha256",
        "canonical_schema_sha256", "prediction_contract_sha256", "provider_schema_sha256",
        "generation_config", "execution_mode", "record_set_sha256", "artifact_sha256",
        "provider_job_id", "run_imported_at", "prediction_id", "record_id",
        "speaker_turn_id", "turn_key", "speech_date", "chamber", "speaker_name_raw",
    ]
    task_fields = (
        ["is_non_policy", "primary_australian_domain", "secondary_australian_domains"]
        if task == "policy"
        else ["prediction_task", "hostility_present", "target", "evidence_spans", "partisan_hostility"]
    )
    return [*metadata_fields, *task_fields, "reasoning", "returned_model", "created_at"]


def _validate_and_prepare_prediction_export_row(
    row: dict[str, Any], task: str, run: dict[str, Any]
) -> None:
    if row["prediction_task"] != task:
        raise ValueError(f"prediction {row['prediction_id']} task does not match its run task")
    values = row["prediction_values"]
    if not isinstance(values, dict):
        raise ValueError(f"prediction {row['prediction_id']} values are not a JSON object")
    if task == "hostility":
        if any(row[field] is not None for field in (
            "is_non_policy", "primary_australian_domain", "secondary_australian_domains"
        )):
            raise ValueError(f"prediction {row['prediction_id']} has policy fields for a hostility task")
        if set(values) != {"hostility_present", "target", "evidence_spans"}:
            raise ValueError(f"prediction {row['prediction_id']} has invalid hostility values")
        presence, target, spans = values["hostility_present"], values["target"], values["evidence_spans"]
        if presence not in PRESENCE_CODES or target not in MODEL_TARGET_CODES:
            raise ValueError(f"prediction {row['prediction_id']} has invalid hostility label or target")
        if not isinstance(spans, list) or any(
            not isinstance(span, dict)
            or set(span) != {"start_offset", "end_offset", "text"}
            or not isinstance(span.get("start_offset"), int)
            or isinstance(span.get("start_offset"), bool)
            or not isinstance(span.get("end_offset"), int)
            or isinstance(span.get("end_offset"), bool)
            or span["start_offset"] < 0
            or span["end_offset"] <= span["start_offset"]
            or not isinstance(span.get("text"), str)
            or not span["text"]
            for span in spans
        ):
            raise ValueError(f"prediction {row['prediction_id']} has invalid hostility evidence spans")
        if (presence == "NO" and (target != "N/A" or spans)) or (
            presence != "NO" and (target == "N/A" or not spans)
        ):
            raise ValueError(f"prediction {row['prediction_id']} has inconsistent hostility values")
        row.update(
            prediction_task=task,
            hostility_present=presence,
            target=target,
            evidence_spans=spans,
            partisan_hostility=partisan_hostility(values),
        )
    else:
        if (
            not isinstance(row["is_non_policy"], bool)
            or not isinstance(row["secondary_australian_domains"], list)
            or (row["primary_australian_domain"] is not None
                and not isinstance(row["primary_australian_domain"], str))
        ):
            raise ValueError(f"prediction {row['prediction_id']} has invalid policy fields")
        if values and (
            set(values) != {"is_non_policy", "primary_australian_domain", "secondary_australian_domains"}
            or values["is_non_policy"] != row["is_non_policy"]
            or values["primary_australian_domain"] != row["primary_australian_domain"]
            or values["secondary_australian_domains"] != row["secondary_australian_domains"]
        ):
            raise ValueError(f"prediction {row['prediction_id']} has inconsistent policy values")
    row.update(
        run_id=str(run["public_id"]), provider=run["provider"],
        configured_model=run["configured_model"], prompt_version=run["prompt_version"],
        prompt_sha256=run["prompt_sha256"], canonical_schema_sha256=run["canonical_schema_sha256"],
        prediction_contract_sha256=run["prediction_contract_sha256"],
        provider_schema_sha256=run["provider_schema_sha256"],
        generation_config=run["generation_config"], execution_mode=run["execution_mode"],
        record_set_sha256=run["record_set_sha256"], artifact_sha256=run["artifact_sha256"],
        provider_job_id=run["provider_job_id"], run_imported_at=run["imported_at"],
    )


def _verified_prediction_export_cache(
    output_path: Path, manifest_path: Path,
    source_identity: dict[str, Any], source_sha256: str,
) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return (
            output_path.is_file()
            and manifest.get("source_identity") == source_identity
            and manifest.get("source_sha256") == source_sha256
            and manifest.get("row_count") == source_identity["valid_count"]
            and manifest.get("csv_bytes") == output_path.stat().st_size
            and manifest.get("csv_sha256") == _sha256_file(output_path)
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def _write_prediction_export_manifest(path: Path, manifest: dict[str, Any]) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            json.dump(manifest, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
                   abstention.resolution_reason,
                   abstention.outcome_provenance AS refusal_provenance,
                   abstention.artifact_reference,abstention.created_at
            FROM model_prediction_abstentions abstention
            JOIN speaker_turns turn ON turn.id=abstention.speaker_turn_id
            WHERE abstention.run_id=%s AND abstention.resolution_type='PROVIDER_REFUSAL'
            ORDER BY abstention.record_id,abstention.id
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
