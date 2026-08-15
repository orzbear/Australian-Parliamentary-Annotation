"""Append-only draft, submission, and revision persistence."""

from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.web.annotations.renderer import load_schema
from hansard_annotator.web.annotations.validation import (
    ValidationOutcome,
    validate_annotation,
)
from hansard_annotator.web.audit.service import record_event
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings


class AnnotationValidationError(Exception):
    def __init__(self, outcome: ValidationOutcome) -> None:
        super().__init__("annotation validation failed")
        self.outcome = outcome


def schema_form(
    settings: WebSettings,
    principal: Principal,
    assignment_id: int,
) -> dict[str, object]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        assignment = connection.execute(
            """
            SELECT a.id,p.annotation_schema_version_id,
                   annotation_schema.name AS schema_name,
                   schema_version.version AS schema_version
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            JOIN projects p ON p.id=t.project_id
            JOIN annotation_schema_versions schema_version
              ON schema_version.id=p.annotation_schema_version_id
            JOIN annotation_schemas annotation_schema
              ON annotation_schema.id=schema_version.annotation_schema_id
            WHERE a.id=%s AND a.user_id=%s
            """,
            (assignment_id, principal.user_id),
        ).fetchone()
        if assignment is None:
            raise LookupError("assignment not found")
        fields, labels = load_schema(
            connection, assignment["annotation_schema_version_id"]
        )
        current = connection.execute(
            """
            SELECT av.values,av.revision_number,an.status
            FROM annotations an LEFT JOIN annotation_versions av
              ON av.id=an.current_version_id
            WHERE an.assignment_id=%s
            """,
            (assignment_id,),
        ).fetchone()
        return {
            "fields": fields,
            "labels": labels,
            "values": dict(current["values"]) if current and current["values"] else {},
            "revision_number": current["revision_number"] if current else None,
            "annotation_status": current["status"] if current else None,
            "schema_name": assignment["schema_name"],
            "schema_version": assignment["schema_version"],
        }


def save_annotation(
    settings: WebSettings,
    principal: Principal,
    assignment_id: int,
    payload: dict[str, object],
    *,
    event_type: str,
) -> dict[str, object]:
    if event_type not in {"draft_saved", "submitted", "revised"}:
        raise ValueError("invalid annotation event type")
    submitting = event_type in {"submitted", "revised"}
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        assignment = connection.execute(
            """
            SELECT a.id,a.public_id,a.status,t.id AS task_id,t.project_id,
                   p.annotation_schema_version_id,p.status AS project_status
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            JOIN projects p ON p.id=t.project_id
            WHERE a.id=%s AND a.user_id=%s
            FOR UPDATE OF a,t
            """,
            (assignment_id, principal.user_id),
        ).fetchone()
        if assignment is None:
            raise LookupError("assignment not found")
        if assignment["status"] in {"withdrawn", "flagged"}:
            raise ValueError("assignment is not editable")
        if assignment["status"] == "submitted" and event_type != "revised":
            raise ValueError("submitted annotation requires an explicit revision")
        fields, labels = load_schema(
            connection, assignment["annotation_schema_version_id"]
        )
        taxonomy_codes = {
            taxonomy_id: {str(label["code"]) for label in taxonomy_labels}
            for taxonomy_id, taxonomy_labels in labels.items()
        }
        outcome = validate_annotation(
            payload, fields, taxonomy_codes, submitting=submitting
        )
        if not outcome.valid:
            raise AnnotationValidationError(outcome)
        annotation = connection.execute(
            """
            SELECT an.id,an.public_id,an.current_version_id,an.status,
                   av.values_sha256,av.revision_number
            FROM annotations an LEFT JOIN annotation_versions av
              ON av.id=an.current_version_id
            WHERE an.assignment_id=%s
            FOR UPDATE OF an
            """,
            (assignment_id,),
        ).fetchone()
        if (
            annotation
            and annotation["values_sha256"] == outcome.sha256
            and event_type == "draft_saved"
            and annotation["status"] == "draft"
        ):
            return {
                "annotation_id": annotation["id"],
                "revision_number": annotation["revision_number"],
                "sha256": outcome.sha256,
                "reused": True,
                "status": annotation["status"],
            }
        if annotation is None:
            annotation = connection.execute(
                """
                INSERT INTO annotations
                  (assignment_id,annotation_schema_version_id,status)
                VALUES (%s,%s,'draft')
                RETURNING id,public_id,NULL::bigint AS current_version_id,
                          status,NULL::text AS values_sha256,
                          0::integer AS revision_number
                """,
                (assignment_id, assignment["annotation_schema_version_id"]),
            ).fetchone()
            assert annotation is not None
        revision_number = int(annotation["revision_number"] or 0) + 1
        validation_result = {
            "valid": True,
            "submitting": submitting,
            "schema_version_id": assignment["annotation_schema_version_id"],
        }
        version = connection.execute(
            """
            INSERT INTO annotation_versions
              (annotation_id,revision_number,event_type,values,values_sha256,
               validation_result,created_by_user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                annotation["id"],
                revision_number,
                event_type,
                Jsonb(json.loads(outcome.canonical_json)),
                outcome.sha256,
                Jsonb(validation_result),
                principal.user_id,
            ),
        ).fetchone()
        assert version is not None
        logical_status = (
            "revised"
            if event_type == "revised"
            else "submitted"
            if event_type == "submitted"
            else "draft"
        )
        connection.execute(
            """
            UPDATE annotations
            SET current_version_id=%s,status=%s,updated_at=now(),
                submitted_at=CASE WHEN %s THEN COALESCE(submitted_at,now())
                                  ELSE submitted_at END
            WHERE id=%s
            """,
            (version["id"], logical_status, submitting, annotation["id"]),
        )
        if submitting:
            connection.execute(
                """
                UPDATE assignments
                SET status='submitted',submitted_at=now(),updated_at=now()
                WHERE id=%s
                """,
                (assignment_id,),
            )
            connection.execute(
                "UPDATE tasks SET status='submitted',completed_at=now() WHERE id=%s",
                (assignment["task_id"],),
            )
        else:
            connection.execute(
                """
                UPDATE assignments SET status='in_progress',updated_at=now()
                WHERE id=%s AND status IN ('assigned','claimed','in_progress')
                """,
                (assignment_id,),
            )
        record_event(
            connection,
            event_type,
            "annotation",
            actor_user_id=principal.user_id,
            entity_public_id=annotation["public_id"],
            project_id=assignment["project_id"],
            metadata={
                "assignment_id": assignment_id,
                "revision_number": revision_number,
                "values_sha256": outcome.sha256,
            },
        )
        return {
            "annotation_id": annotation["id"],
            "revision_number": revision_number,
            "sha256": outcome.sha256,
            "reused": False,
            "status": logical_status,
        }


def revision_history(
    settings: WebSettings, principal: Principal, assignment_id: int
) -> list[dict[str, object]]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            """
            SELECT av.revision_number,av.event_type,av.values_sha256,av.created_at
            FROM assignments a JOIN annotations an ON an.assignment_id=a.id
            JOIN annotation_versions av ON av.annotation_id=an.id
            WHERE a.id=%s AND a.user_id=%s
            ORDER BY av.revision_number DESC
            """,
            (assignment_id, principal.user_id),
        ).fetchall()
        return [dict(row) for row in rows]
