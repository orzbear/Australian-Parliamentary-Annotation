"""Deterministic batching and PostgreSQL-backed assignment workflows."""

from __future__ import annotations

import hashlib
import json

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.web.annotations.renderer import load_schema
from hansard_annotator.web.audit.service import record_event
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.projects.permissions import (
    ANNOTATOR_ROLES,
    MANAGER_ROLES,
    VIEW_ROLES,
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


HUMAN_WORK_FILTERS = {"to_annotate", "in_progress", "completed", "skipped", "all"}


def _applicable_human_work_filters(
    field_keys: set[str], policy_status: str | None, primary_domain: str | None
) -> tuple[str | None, str | None, bool, bool]:
    supports_policy_status = bool(
        {"is_non_policy", "content_status"}.intersection(field_keys)
    )
    supports_primary_domain = "primary_australian_domain" in field_keys
    if supports_policy_status and policy_status not in {None, "policy", "non_policy"}:
        raise ValueError("invalid policy status filter")
    return (
        policy_status if supports_policy_status else None,
        primary_domain if supports_primary_domain else None,
        supports_policy_status,
        supports_primary_domain,
    )


def _human_work_cte() -> str:
    return """
        WITH human_work AS (
          SELECT a.id AS assignment_id,a.public_id AS assignment_public_id,
                 a.user_id,a.status AS assignment_status,a.updated_at,a.submitted_at,
                 a.skipped_at,t.project_id,t.priority,st.turn_key,st.speech_date,
                 st.speaker_name_raw,st.major_heading_original,
                 an.status AS annotation_status,av.revision_number,av.values,
                 av.created_at AS version_created_at,u.display_name AS annotator_name,
                 CASE
                   WHEN an.status IN ('submitted','revised') OR a.status='submitted'
                     THEN 'completed'
                   WHEN a.status='skipped' THEN 'skipped'
                   WHEN an.status='draft' OR a.status='in_progress' THEN 'in_progress'
                   ELSE 'to_annotate'
                 END AS workflow_status,
                 CASE
                   WHEN av.values ? 'is_non_policy' THEN
                     CASE WHEN (av.values->>'is_non_policy')::boolean
                          THEN 'non_policy' ELSE 'policy' END
                   WHEN av.values ? 'content_status' THEN
                     CASE WHEN av.values->>'content_status'='substantive_policy'
                          THEN 'policy' ELSE 'non_policy' END
                   ELSE NULL
                 END AS policy_status,
                 av.values->>'primary_australian_domain' AS primary_domain,
                 av.values->>'hostility_present' AS hostility_present
          FROM assignments a JOIN tasks t ON t.id=a.task_id
          JOIN speaker_turns st ON st.id=t.speaker_turn_id
          LEFT JOIN users u ON u.id=a.user_id
          LEFT JOIN annotations an ON an.assignment_id=a.id
          LEFT JOIN annotation_versions av ON av.id=an.current_version_id
          WHERE t.project_id=%s AND (%s=false OR a.user_id=%s)
            AND a.status NOT IN ('withdrawn','flagged')
        )
    """


def human_annotation_work(
    settings: WebSettings,
    principal: Principal,
    project_id: int,
    *,
    status: str = "to_annotate",
    policy_status: str | None = None,
    primary_domain: str | None = None,
    annotator: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, object]:
    """Return scoped counts plus one bounded page of Human Annotation work."""
    if status not in HUMAN_WORK_FILTERS:
        raise ValueError("invalid Human Annotation status filter")
    if page < 1 or page_size < 1 or page_size > 100:
        raise ValueError("invalid pagination")
    cleaned_domain = (primary_domain or "").strip() or None
    cleaned_annotator = (annotator or "").strip() or None
    cleaned_query = (query or "").strip() or None
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        project_role = require_project_role(connection, principal, project_id, VIEW_ROLES)
        field_rows = connection.execute(
            """
            SELECT field.field_key FROM projects project
            JOIN annotation_field_definitions field
              ON field.annotation_schema_version_id=project.annotation_schema_version_id
            WHERE project.id=%s AND field.is_active
            """,
            (project_id,),
        ).fetchall()
        field_keys = {str(row["field_key"]) for row in field_rows}
        (
            policy_status,
            cleaned_domain,
            supports_policy_status,
            supports_primary_domain,
        ) = _applicable_human_work_filters(field_keys, policy_status, cleaned_domain)
        own_only = project_role == "annotator"
        base_parameters: tuple[object, ...] = (
            project_id,
            own_only,
            principal.user_id,
        )
        counts = connection.execute(
            _human_work_cte()
            + """
              SELECT count(*) AS total,
                     count(*) FILTER (WHERE workflow_status='to_annotate') AS to_annotate,
                     count(*) FILTER (WHERE workflow_status='in_progress') AS in_progress,
                     count(*) FILTER (WHERE workflow_status='completed') AS completed,
                     count(*) FILTER (WHERE workflow_status='skipped') AS skipped
              FROM human_work
            """,
            base_parameters,
        ).fetchone()
        rows = connection.execute(
            _human_work_cte()
            + """
              SELECT *,count(*) OVER () AS filtered_count
              FROM human_work
              WHERE (%s='all' OR workflow_status=%s)
                AND (%s::text IS NULL OR policy_status=%s)
                AND (%s::text IS NULL OR primary_domain=%s)
                AND (%s::text IS NULL OR annotator_name ILIKE '%%' || %s || '%%')
                AND (%s::text IS NULL OR turn_key ILIKE '%%' || %s || '%%'
                     OR speaker_name_raw ILIKE '%%' || %s || '%%')
              ORDER BY
                CASE workflow_status WHEN 'in_progress' THEN 0 WHEN 'to_annotate' THEN 1
                  WHEN 'skipped' THEN 2 ELSE 3 END,
                priority DESC,COALESCE(updated_at,version_created_at) DESC,assignment_id
              LIMIT %s OFFSET %s
            """,
            (
                *base_parameters,
                status,
                status,
                policy_status,
                policy_status,
                cleaned_domain,
                cleaned_domain,
                cleaned_annotator,
                cleaned_annotator,
                cleaned_query,
                cleaned_query,
                cleaned_query,
                page_size,
                (page - 1) * page_size,
            ),
        ).fetchall()
        domain_labels = connection.execute(
            """
            SELECT label.code,label.label FROM projects p
            JOIN annotation_field_definitions field
              ON field.annotation_schema_version_id=p.annotation_schema_version_id
             AND field.field_key='primary_australian_domain'
            JOIN taxonomy_labels label
              ON label.taxonomy_version_id=field.taxonomy_version_id AND label.is_active
            WHERE p.id=%s ORDER BY label.display_order,label.code
            """,
            (project_id,),
        ).fetchall()
    filtered_count = int(rows[0]["filtered_count"]) if rows else 0
    label_map = {str(row["code"]): str(row["label"]) for row in domain_labels}
    items: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        code = item.get("primary_domain")
        item["primary_domain_display"] = (
            f"{code} — {label_map.get(str(code), str(code))}" if code else None
        )
        item["can_edit"] = item.get("user_id") == principal.user_id
        items.append(item)
    return {
        "counts": dict(counts or {}),
        "rows": items,
        "filtered_count": filtered_count,
        "page": page,
        "page_size": page_size,
        "page_count": max(1, (filtered_count + page_size - 1) // page_size),
        "active_status": status,
        "policy_status": policy_status,
        "primary_domain": cleaned_domain,
        "annotator": cleaned_annotator,
        "query": cleaned_query,
        "domain_labels": [dict(row) for row in domain_labels],
        "supports_policy_status": supports_policy_status,
        "supports_primary_domain": supports_primary_domain,
    }


def _display_taxonomy_value(
    value: object, field: dict[str, object], labels: dict[int, list[dict[str, object]]]
) -> object:
    taxonomy_id = field.get("taxonomy_version_id")
    if taxonomy_id is None:
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return value
    label_map = {
        str(label["code"]): f"{label['code']} — {label['label']}"
        for label in labels.get(int(str(taxonomy_id)), [])
    }
    if isinstance(value, list):
        return [label_map.get(str(item), str(item)) for item in value]
    return label_map.get(str(value), value) if value is not None else None


def _version_summary(values: dict[str, object]) -> dict[str, object]:
    if "is_non_policy" in values:
        policy_label = "Non-Policy" if values.get("is_non_policy") else "Policy"
    elif "content_status" in values:
        policy_label = (
            "Policy" if values.get("content_status") == "substantive_policy" else "Non-Policy"
        )
    else:
        policy_label = "Not applicable"
    hostility = values.get("hostility_present")
    return {
        "policy_label": policy_label,
        "has_policy_summary": policy_label != "Not applicable",
        "summary_label": (
            policy_label
            if policy_label != "Not applicable"
            else f"Hostility: {hostility}"
            if hostility
            else "Annotation"
        ),
        "primary_domain": values.get("primary_australian_domain"),
        "secondary_domains": values.get("secondary_australian_domains") or [],
    }


def human_annotation_detail(
    settings: WebSettings, principal: Principal, assignment_id: int
) -> dict[str, object]:
    """Read a completed annotation under existing project visibility rules."""
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        scope = connection.execute(
            "SELECT t.project_id FROM assignments a JOIN tasks t ON t.id=a.task_id WHERE a.id=%s",
            (assignment_id,),
        ).fetchone()
        if scope is None:
            raise LookupError("assignment not found")
        project_id = int(scope["project_id"])
        project_role = require_project_role(connection, principal, project_id, VIEW_ROLES)
        own_only = project_role == "annotator"
        row = connection.execute(
            """
            SELECT a.id,a.public_id,a.user_id,a.status AS assignment_status,a.submitted_at,
                   a.updated_at,t.project_id,st.turn_key,st.speech_date,st.chamber,
                   st.speaker_name_raw,st.major_heading_original,st.minor_heading_original,
                   st.business_type,st.text_clean,st.calculated_word_count,p.name AS project_name,
                   p.annotation_schema_version_id,an.status AS annotation_status,
                   an.submitted_at AS annotation_submitted_at,an.updated_at AS annotation_updated_at,
                   av.revision_number,av.event_type,av.values,av.created_at AS version_created_at,
                   u.display_name AS annotator_name
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            JOIN projects p ON p.id=t.project_id
            JOIN speaker_turns st ON st.id=t.speaker_turn_id
            JOIN users u ON u.id=a.user_id
            JOIN annotations an ON an.assignment_id=a.id
            JOIN annotation_versions av ON av.id=an.current_version_id
            WHERE a.id=%s AND an.status IN ('submitted','revised')
              AND (%s=false OR a.user_id=%s)
            """,
            (assignment_id, own_only, principal.user_id),
        ).fetchone()
        if row is None:
            raise LookupError("completed annotation not found")
        fields, labels = load_schema(connection, row["annotation_schema_version_id"])
        history = connection.execute(
            """
            SELECT av.revision_number,av.event_type,av.values,av.created_at,
                   u.display_name AS annotator_name
            FROM annotation_versions av JOIN annotations an ON an.id=av.annotation_id
            JOIN users u ON u.id=av.created_by_user_id
            WHERE an.assignment_id=%s ORDER BY av.revision_number DESC
            """,
            (assignment_id,),
        ).fetchall()
    result = dict(row)
    values = dict(row["values"])
    result.update(_version_summary(values))
    result["can_edit"] = row["user_id"] == principal.user_id
    result["field_values"] = [
        {
            "field_key": field["field_key"],
            "label": field["label"],
            "value": _display_taxonomy_value(
                values.get(str(field["field_key"])), field, labels
            ),
        }
        for field in fields
    ]
    result["history"] = [
        {**dict(version), **_version_summary(dict(version["values"]))}
        for version in history
    ]
    return result


def assignment_progress(
    settings: WebSettings, principal: Principal, project_id: int
) -> dict[str, int]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        require_project_role(connection, principal, project_id, ANNOTATOR_ROLES)
        row = connection.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status='submitted') AS completed
            FROM assignments WHERE user_id=%s
              AND task_id IN (SELECT id FROM tasks WHERE project_id=%s)
              AND status <> 'withdrawn'
            """,
            (principal.user_id, project_id),
        ).fetchone()
        assert row is not None
    return {"total": int(row["total"]), "completed": int(row["completed"])}


def next_human_assignment(
    settings: WebSettings,
    principal: Principal,
    project_id: int,
    *,
    exclude_assignment_id: int | None = None,
) -> dict[str, object] | None:
    """Prefer already-owned work, then atomically claim a new assignment."""
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        require_project_role(connection, principal, project_id, ANNOTATOR_ROLES)
        row = connection.execute(
            """
            SELECT a.id,a.public_id,a.task_id FROM assignments a
            JOIN tasks t ON t.id=a.task_id
            LEFT JOIN annotations an ON an.assignment_id=a.id
            WHERE t.project_id=%s AND a.user_id=%s
              AND a.status IN ('assigned','claimed','in_progress')
              AND (%s IS NULL OR a.id<>%s)
            ORDER BY CASE WHEN an.status='draft' THEN 0
                          WHEN a.status='in_progress' THEN 1 ELSE 2 END,
                     a.updated_at,a.id
            LIMIT 1
            """,
            (
                project_id,
                principal.user_id,
                exclude_assignment_id,
                exclude_assignment_id,
            ),
        ).fetchone()
        if row is not None:
            return dict(row)
    return claim_next(settings, principal, project_id)


def reopen_skipped_assignment(
    settings: WebSettings, principal: Principal, assignment_id: int
) -> None:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT a.public_id,t.id AS task_id,t.project_id FROM assignments a
            JOIN tasks t ON t.id=a.task_id
            WHERE a.id=%s AND a.user_id=%s AND a.status='skipped'
            FOR UPDATE OF a,t
            """,
            (assignment_id, principal.user_id),
        ).fetchone()
        if row is None:
            raise LookupError("skipped assignment not available")
        require_project_role(connection, principal, int(row["project_id"]), ANNOTATOR_ROLES)
        competing = connection.execute(
            """
            SELECT 1 FROM assignments WHERE task_id=%s AND id<>%s
              AND status IN ('claimed','in_progress','submitted') LIMIT 1
            """,
            (row["task_id"], assignment_id),
        ).fetchone()
        if competing is not None:
            raise ValueError("this skipped task is already being handled elsewhere")
        connection.execute(
            "UPDATE assignments SET status='in_progress',updated_at=now() WHERE id=%s",
            (assignment_id,),
        )
        connection.execute(
            "UPDATE tasks SET status='in_progress' WHERE id=%s",
            (row["task_id"],),
        )
        record_event(
            connection,
            "assignment_reopened",
            "assignment",
            actor_user_id=principal.user_id,
            entity_public_id=row["public_id"],
            project_id=row["project_id"],
            metadata={},
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
