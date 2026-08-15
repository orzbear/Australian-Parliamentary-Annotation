"""Deterministic batching and PostgreSQL-backed assignment workflows."""

from __future__ import annotations

import hashlib
import json

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.web.audit.service import record_event
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.projects.permissions import (
    ANNOTATOR_ROLES,
    MANAGER_ROLES,
    PermissionDenied,
    require_project_role,
)
from hansard_annotator.web.tasks.schemas import SelectionCriteria


def _selection_query(
    project_id: int, criteria: SelectionCriteria, *, count_only: bool
) -> tuple[sql.SQL | sql.Composed, list[object]]:
    conditions: list[sql.Composable] = [
        sql.SQL("p.id=%s"),
        sql.SQL("rr.preprocessing_run_id=p.preprocessing_run_id"),
        sql.SQL("st.calculated_word_count >= %s"),
        sql.SQL("st.interruption_count >= %s"),
    ]
    parameters: list[object] = [
        project_id,
        criteria.minimum_words,
        criteria.minimum_interruption_count,
    ]
    if criteria.maximum_words is not None:
        conditions.append(sql.SQL("st.calculated_word_count <= %s"))
        parameters.append(criteria.maximum_words)
    if criteria.date_from is not None:
        conditions.append(sql.SQL("st.speech_date >= %s"))
        parameters.append(criteria.date_from)
    if criteria.date_to is not None:
        conditions.append(sql.SQL("st.speech_date <= %s"))
        parameters.append(criteria.date_to)
    if criteria.years:
        conditions.append(sql.SQL("EXTRACT(YEAR FROM st.speech_date)::int = ANY(%s)"))
        parameters.append(criteria.years)
    if criteria.exclude_orphan_continuations:
        conditions.append(sql.SQL("st.is_orphan_continuation IS NOT TRUE"))
    for column, value in (
        ("is_question_time", criteria.question_time_hint),
        ("is_procedural", criteria.procedural_hint),
        ("is_ceremonial", criteria.ceremonial_hint),
    ):
        if value is not None:
            conditions.append(
                sql.SQL("st.{}=%s").format(sql.Identifier(column))
            )
            parameters.append(value)
    select = sql.SQL("count(*) AS eligible_count") if count_only else sql.SQL(
        "st.id,st.turn_key,NULL::bigint AS source_annotation_version_id,"
        "NULL::text AS source_values_sha256"
    )
    query = sql.SQL(
        """
        SELECT {} FROM projects p
        JOIN reconstruction_runs rr ON rr.preprocessing_run_id=p.preprocessing_run_id
        JOIN speaker_turns st ON st.reconstruction_run_id=rr.id
        WHERE {}
        """
    ).format(select, sql.SQL(" AND ").join(conditions))
    if not count_only:
        query += sql.SQL(" ORDER BY md5(st.turn_key || %s),st.turn_key")
        parameters.append(str(criteria.seed))
        if criteria.limit is not None:
            query += sql.SQL(" LIMIT %s")
            parameters.append(criteria.limit)
    return query, parameters


def _derived_selection_query(
    project_id: int, criteria: SelectionCriteria, *, count_only: bool
) -> tuple[sql.SQL | sql.Composed, list[object]]:
    if criteria.source_domain_code is None:
        raise ValueError("derived batches require source_domain_code")
    eligible = sql.SQL(
        """
        WITH eligible AS (
          SELECT DISTINCT ON (st.id)
                 st.id,st.turn_key,av.id AS source_annotation_version_id,
                 av.values_sha256 AS source_values_sha256
          FROM projects target
          JOIN projects source ON source.id=target.source_project_id
          JOIN tasks source_task ON source_task.project_id=source.id
          JOIN speaker_turns st ON st.id=source_task.speaker_turn_id
          JOIN assignments source_assignment
            ON source_assignment.task_id=source_task.id
          JOIN annotations source_annotation
            ON source_annotation.assignment_id=source_assignment.id
           AND source_annotation.status IN ('submitted','revised')
          JOIN annotation_versions av
            ON av.id=source_annotation.current_version_id
          WHERE target.id=%s
            AND COALESCE((av.values->>'is_non_policy')::boolean,false)=false
            AND (av.values->>'primary_australian_domain'=%s
                 OR av.values->'secondary_australian_domains' ? %s)
          ORDER BY st.id,av.id
        )
        """
    )
    parameters: list[object] = [
        project_id,
        criteria.source_domain_code,
        criteria.source_domain_code,
    ]
    if count_only:
        return eligible + sql.SQL("SELECT count(*) AS eligible_count FROM eligible"), parameters
    query = eligible + sql.SQL(
        """
        SELECT id,turn_key,source_annotation_version_id,source_values_sha256
        FROM eligible ORDER BY md5(turn_key || %s),turn_key
        """
    )
    parameters.append(str(criteria.seed))
    if criteria.limit is not None:
        query += sql.SQL(" LIMIT %s")
        parameters.append(criteria.limit)
    return query, parameters


def _batch_query(
    connection: psycopg.Connection[dict[str, object]],
    project_id: int,
    criteria: SelectionCriteria,
    *,
    count_only: bool,
) -> tuple[sql.SQL | sql.Composed, list[object]]:
    project = connection.execute(
        "SELECT source_project_id FROM projects WHERE id=%s", (project_id,)
    ).fetchone()
    if project is None:
        raise ValueError("project does not exist")
    if project["source_project_id"] is None:
        if criteria.source_domain_code is not None:
            raise ValueError("corpus batches cannot use source_domain_code")
        return _selection_query(project_id, criteria, count_only=count_only)
    if criteria.source_domain_code is None:
        raise ValueError("derived batches require source_domain_code")
    known_code = connection.execute(
        """
        SELECT 1
        FROM projects source_project
        JOIN annotation_field_definitions field
          ON field.annotation_schema_version_id=
             source_project.annotation_schema_version_id
         AND field.field_key='primary_australian_domain'
        JOIN taxonomy_labels label
          ON label.taxonomy_version_id=field.taxonomy_version_id
         AND label.code=%s AND label.is_active
        WHERE source_project.id=%s
        """,
        (criteria.source_domain_code, project["source_project_id"]),
    ).fetchone()
    if known_code is None:
        raise ValueError("source_domain_code is not in the source project's taxonomy")
    return _derived_selection_query(project_id, criteria, count_only=count_only)


def preview_batch(
    settings: WebSettings,
    principal: Principal,
    project_id: int,
    criteria: SelectionCriteria,
) -> dict[str, object]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        require_project_role(connection, principal, project_id, MANAGER_ROLES)
        query, parameters = _batch_query(
            connection, project_id, criteria, count_only=True
        )
        row = connection.execute(query, tuple(parameters)).fetchone()
        assert row is not None
        eligible = int(row["eligible_count"])
        selected = min(eligible, criteria.limit) if criteria.limit else eligible
        record_event(
            connection,
            "batch_previewed",
            "batch",
            actor_user_id=principal.user_id,
            project_id=project_id,
            metadata={"eligible_count": eligible, "selected_count": selected},
        )
        return {"eligible_count": eligible, "selected_count": selected}


def generate_batch(
    settings: WebSettings,
    principal: Principal,
    project_id: int,
    *,
    name: str,
    criteria: SelectionCriteria,
) -> dict[str, object]:
    criteria_json = criteria.model_dump(mode="json")
    canonical = json.dumps(criteria_json, sort_keys=True, separators=(",", ":"))
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        require_project_role(connection, principal, project_id, MANAGER_ROLES)
        project = connection.execute(
            "SELECT status FROM projects WHERE id=%s FOR UPDATE", (project_id,)
        ).fetchone()
        if project is None or project["status"] == "archived":
            raise ValueError("archived or missing project cannot generate tasks")
        existing = connection.execute(
            """
            SELECT id,public_id,name,status,selected_count,selection_sha256,
                   selection_criteria
            FROM batches WHERE project_id=%s AND name=%s
            """,
            (project_id, name.strip()),
        ).fetchone()
        if existing is not None:
            stored = json.dumps(
                existing["selection_criteria"], sort_keys=True, separators=(",", ":")
            )
            if stored != canonical:
                raise ValueError("batch name already exists with different criteria")
            return {**dict(existing), "reused": True}
        query, parameters = _batch_query(
            connection, project_id, criteria, count_only=False
        )
        turns = connection.execute(query, tuple(parameters)).fetchall()
        fingerprint_lines = [
            "|".join(
                (
                    str(row["turn_key"]),
                    str(row["source_annotation_version_id"] or ""),
                    str(row["source_values_sha256"] or ""),
                )
            )
            for row in turns
        ]
        fingerprint = hashlib.sha256("\n".join(fingerprint_lines).encode()).hexdigest()
        batch = connection.execute(
            """
            INSERT INTO batches
              (project_id,name,status,selection_criteria,random_seed,requested_count,
               selected_count,selection_sha256,created_by_user_id,finalised_at)
            VALUES (%s,%s,'generated',%s,%s,%s,%s,%s,%s,now())
            RETURNING id,public_id,name,status,selected_count,selection_sha256
            """,
            (
                project_id,
                name.strip(),
                Jsonb(criteria_json),
                criteria.seed,
                criteria.limit,
                len(turns),
                fingerprint,
                principal.user_id,
            ),
        ).fetchone()
        assert batch is not None
        if turns:
            task_rows = connection.execute(
                """
                INSERT INTO tasks
                  (project_id,batch_id,speaker_turn_id,source_annotation_version_id)
                SELECT %s,%s,candidate.turn_id,candidate.source_version_id
                FROM unnest(%s::bigint[],%s::bigint[]) WITH ORDINALITY
                  candidate(turn_id,source_version_id,ordinal)
                ORDER BY candidate.ordinal
                ON CONFLICT (project_id,speaker_turn_id) DO NOTHING
                RETURNING id
                """,
                (
                    project_id,
                    batch["id"],
                    [row["id"] for row in turns],
                    [row["source_annotation_version_id"] for row in turns],
                ),
            ).fetchall()
            connection.cursor().executemany(
                "INSERT INTO assignments (task_id,status) VALUES (%s,'assigned')",
                [(row["id"],) for row in task_rows],
            )
        record_event(
            connection,
            "batch_generated",
            "batch",
            actor_user_id=principal.user_id,
            entity_public_id=batch["public_id"],
            project_id=project_id,
            metadata={
                "selected_count": len(turns),
                "selection_sha256": fingerprint,
            },
        )
        return {**dict(batch), "reused": False}


def claim_next(
    settings: WebSettings, principal: Principal, project_id: int
) -> dict[str, object] | None:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        require_project_role(connection, principal, project_id, ANNOTATOR_ROLES)
        project = connection.execute(
            "SELECT status FROM projects WHERE id=%s", (project_id,)
        ).fetchone()
        if project is None or project["status"] != "active":
            raise PermissionDenied("Only active projects permit task claiming")
        assignment = connection.execute(
            """
            SELECT a.id,a.public_id,a.task_id
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            WHERE t.project_id=%s AND t.status='available'
              AND a.status='assigned' AND a.user_id IS NULL
            ORDER BY t.priority DESC,t.id,a.id
            FOR UPDATE OF a,t SKIP LOCKED
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if assignment is None:
            return None
        connection.execute(
            """
            UPDATE assignments
            SET user_id=%s,status='claimed',claimed_at=now(),updated_at=now()
            WHERE id=%s
            """,
            (principal.user_id, assignment["id"]),
        )
        connection.execute(
            "UPDATE tasks SET status='in_progress' WHERE id=%s",
            (assignment["task_id"],),
        )
        record_event(
            connection,
            "task_claimed",
            "assignment",
            actor_user_id=principal.user_id,
            entity_public_id=assignment["public_id"],
            project_id=project_id,
            metadata={"task_id": assignment["task_id"]},
        )
        return dict(assignment)


def assign_to_user(
    settings: WebSettings,
    principal: Principal,
    project_id: int,
    assignment_id: int,
    user_id: int,
) -> None:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        require_project_role(connection, principal, project_id, MANAGER_ROLES)
        member = connection.execute(
            """
            SELECT 1 FROM project_memberships
            WHERE project_id=%s AND user_id=%s AND status='active'
              AND project_role='annotator'
            """,
            (project_id, user_id),
        ).fetchone()
        if member is None:
            raise ValueError("assignee must be an active project annotator")
        assignment = connection.execute(
            """
            SELECT a.public_id FROM assignments a JOIN tasks t ON t.id=a.task_id
            WHERE a.id=%s AND t.project_id=%s AND a.user_id IS NULL
              AND a.status='assigned'
            FOR UPDATE OF a
            """,
            (assignment_id, project_id),
        ).fetchone()
        if assignment is None:
            raise ValueError("assignment is no longer available")
        connection.execute(
            """
            UPDATE assignments SET user_id=%s,assigned_by_user_id=%s,
                   updated_at=now() WHERE id=%s
            """,
            (user_id, principal.user_id, assignment_id),
        )
        record_event(
            connection,
            "task_assigned",
            "assignment",
            actor_user_id=principal.user_id,
            entity_public_id=assignment["public_id"],
            project_id=project_id,
            metadata={"user_id": user_id},
        )


def get_assignment(
    settings: WebSettings, principal: Principal, assignment_id: int
) -> dict[str, object]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT a.id,a.public_id,a.status AS assignment_status,a.flag_reason,
                   t.id AS task_id,t.project_id,t.status AS task_status,
                   st.turn_key,st.speech_date,st.chamber,st.speaker_name_raw,
                   st.major_heading_original,st.minor_heading_original,
                   st.business_type,st.calculated_word_count,st.interruption_count,
                   st.is_orphan_continuation,st.is_procedural,st.is_ceremonial,
                   st.is_question_time,st.text_clean,sf.relative_path AS source_file,
                   first_fragment.source_url,p.name AS project_name,p.status AS project_status,
                   p.preprocessing_run_id,pr.run_name,pr.pipeline_version,
                   c.name AS corpus_name,p.annotation_schema_version_id
                   ,t.source_annotation_version_id,
                   source_version.values_sha256 AS source_values_sha256,
                   source_project.name AS source_project_name
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            JOIN projects p ON p.id=t.project_id
            JOIN speaker_turns st ON st.id=t.speaker_turn_id
            JOIN source_file_versions sfv ON sfv.id=st.source_file_version_id
            JOIN source_files sf ON sf.id=sfv.source_file_id
            JOIN preprocessing_runs pr ON pr.id=p.preprocessing_run_id
            JOIN corpora c ON c.id=p.corpus_id
            LEFT JOIN speaker_turn_fragments lineage
              ON lineage.turn_id=st.id AND lineage.ordinal=1
            LEFT JOIN speech_fragments first_fragment
              ON first_fragment.id=lineage.fragment_id
            LEFT JOIN annotation_versions source_version
              ON source_version.id=t.source_annotation_version_id
            LEFT JOIN annotations source_annotation
              ON source_annotation.id=source_version.annotation_id
            LEFT JOIN assignments source_assignment
              ON source_assignment.id=source_annotation.assignment_id
            LEFT JOIN tasks source_task ON source_task.id=source_assignment.task_id
            LEFT JOIN projects source_project ON source_project.id=source_task.project_id
            WHERE a.id=%s AND a.user_id=%s
            """,
            (assignment_id, principal.user_id),
        ).fetchone()
        if row is None:
            raise LookupError("assignment not found")
        connection.execute(
            """
            UPDATE assignments SET last_opened_at=now(),
              status=CASE WHEN status='claimed' THEN 'in_progress' ELSE status END,
              updated_at=now() WHERE id=%s
            """,
            (assignment_id,),
        )
        return dict(row)


def set_assignment_outcome(
    settings: WebSettings,
    principal: Principal,
    assignment_id: int,
    outcome: str,
    *,
    reason: str | None = None,
) -> None:
    if outcome not in {"skipped", "flagged"}:
        raise ValueError("invalid assignment outcome")
    if outcome == "flagged" and not (reason or "").strip():
        raise ValueError("flagging requires a reason")
    timestamp = "skipped_at" if outcome == "skipped" else "flagged_at"
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT a.public_id,t.id AS task_id,t.project_id
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            WHERE a.id=%s AND a.user_id=%s
              AND a.status IN ('claimed','in_progress','skipped')
            FOR UPDATE
            """,
            (assignment_id, principal.user_id),
        ).fetchone()
        if row is None:
            raise LookupError("assignment not available")
        connection.execute(
            f"""
            UPDATE assignments SET status=%s,{timestamp}=now(),flag_reason=%s,
              updated_at=now() WHERE id=%s
            """,
            (outcome, reason.strip() if reason else None, assignment_id),
        )
        connection.execute(
            "UPDATE tasks SET status=%s WHERE id=%s",
            ("flagged" if outcome == "flagged" else "available", row["task_id"]),
        )
        record_event(
            connection,
            f"assignment_{outcome}",
            "assignment",
            actor_user_id=principal.user_id,
            entity_public_id=row["public_id"],
            project_id=row["project_id"],
            metadata={"reason_provided": bool(reason)},
        )


def personal_dashboard(
    settings: WebSettings, principal: Principal
) -> dict[str, object]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        counts = connection.execute(
            """
            SELECT count(*) FILTER (WHERE a.status IN ('assigned','claimed','in_progress'))
                     AS assigned,
                   count(*) FILTER (WHERE an.status='draft') AS drafts,
                   count(*) FILTER (WHERE a.status='submitted') AS submitted,
                   count(*) FILTER (WHERE a.status='skipped') AS skipped,
                   count(*) FILTER (WHERE a.status='flagged') AS flagged
            FROM assignments a
            JOIN tasks t ON t.id=a.task_id
            JOIN projects p ON p.id=t.project_id
            LEFT JOIN annotations an ON an.assignment_id=a.id
            WHERE a.user_id=%s AND p.status='active'
            """,
            (principal.user_id,),
        ).fetchone()
        queue = connection.execute(
            """
            SELECT a.id,a.public_id,a.status,p.id AS project_id,p.name AS project_name,
                   asv.version AS schema_version,st.speaker_name_raw,st.speech_date,
                   st.calculated_word_count
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            JOIN projects p ON p.id=t.project_id
            JOIN annotation_schema_versions asv
              ON asv.id=p.annotation_schema_version_id
            JOIN speaker_turns st ON st.id=t.speaker_turn_id
            WHERE a.user_id=%s AND p.status='active'
              AND a.status IN ('assigned','claimed','in_progress')
            ORDER BY CASE a.status WHEN 'in_progress' THEN 0 WHEN 'claimed' THEN 1
                     WHEN 'skipped' THEN 2 ELSE 3 END,a.updated_at DESC
            LIMIT 20
            """,
            (principal.user_id,),
        ).fetchall()
        return {
            "counts": dict(counts or {}),
            "queue": [dict(row) for row in queue],
        }
