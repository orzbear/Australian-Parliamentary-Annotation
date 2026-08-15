"""Project creation, provenance, membership, and lifecycle services."""

from __future__ import annotations

import re

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.web.audit.service import record_event
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.projects.permissions import (
    MANAGER_ROLES,
    VIEW_ROLES,
    require_project_role,
)


def list_projects(
    settings: WebSettings, principal: Principal
) -> list[dict[str, object]]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            """
            SELECT p.id,p.public_id,p.slug,p.name,p.description,p.status,p.mode,
                   c.name AS corpus_name,pr.run_name,asv.version AS schema_version,
                   asv.status AS schema_status,pm.project_role,
                   source.name AS source_project_name,
                   (SELECT count(*) FROM tasks t WHERE t.project_id=p.id) AS task_count,
                   (SELECT count(*) FROM tasks t
                    WHERE t.project_id=p.id AND t.status='submitted') AS submitted_count
            FROM projects p
            LEFT JOIN projects source ON source.id=p.source_project_id
            JOIN corpora c ON c.id=p.corpus_id
            JOIN preprocessing_runs pr ON pr.id=p.preprocessing_run_id
            JOIN annotation_schema_versions asv
              ON asv.id=p.annotation_schema_version_id
            LEFT JOIN project_memberships pm
              ON pm.project_id=p.id AND pm.user_id=%s AND pm.status='active'
            WHERE %s OR pm.id IS NOT NULL
            ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1
                          WHEN 'draft' THEN 2 ELSE 3 END,
                     (p.source_project_id IS NOT NULL),p.updated_at DESC,p.id DESC
            """,
            (principal.user_id, principal.is_admin),
        ).fetchall()
        return [dict(row) for row in rows]


def project_options(settings: WebSettings) -> dict[str, list[dict[str, object]]]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        corpora = connection.execute(
            """
            SELECT id,name,slug,date_from,date_to FROM corpora
            WHERE is_active ORDER BY name
            """
        ).fetchall()
        runs = connection.execute(
            """
            SELECT id,corpus_id,run_name,pipeline_version,manifest_sha256,
                   inventory_sha256,status
            FROM preprocessing_runs WHERE status='accepted'
            ORDER BY processing_completed_at DESC,id DESC
            """
        ).fetchall()
        schemas = connection.execute(
            """
            SELECT asv.id,ans.slug,ans.name,asv.version,asv.status,asv.content_sha256
            FROM annotation_schema_versions asv
            JOIN annotation_schemas ans ON ans.id=asv.annotation_schema_id
            ORDER BY ans.name,asv.version
            """
        ).fetchall()
        source_projects = connection.execute(
            """
            SELECT id,name,slug,corpus_id,preprocessing_run_id,status
            FROM projects WHERE status IN ('active','paused')
            ORDER BY name,id
            """
        ).fetchall()
    return {
        "corpora": [dict(row) for row in corpora],
        "runs": [dict(row) for row in runs],
        "schemas": [dict(row) for row in schemas],
        "source_projects": [dict(row) for row in source_projects],
    }


def create_project(
    settings: WebSettings,
    principal: Principal,
    *,
    slug: str,
    name: str,
    description: str,
    mode: str,
    corpus_id: int,
    preprocessing_run_id: int,
    annotation_schema_version_id: int,
    source_project_id: int | None = None,
) -> dict[str, object]:
    slug = slug.strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("slug must use lowercase words separated by hyphens")
    if not name.strip() or mode not in {"development", "pilot", "production"}:
        raise ValueError("valid project name and mode are required")
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        if not principal.is_admin:
            raise PermissionError("only administrators can create projects")
        schema = connection.execute(
            """
            SELECT content_sha256,status FROM annotation_schema_versions WHERE id=%s
            """,
            (annotation_schema_version_id,),
        ).fetchone()
        if schema is None:
            raise ValueError("annotation schema version does not exist")
        taxonomy_pins = connection.execute(
            """
            SELECT DISTINCT tv.id,tv.content_sha256,tv.status
            FROM annotation_field_definitions afd
            JOIN taxonomy_versions tv ON tv.id=afd.taxonomy_version_id
            WHERE afd.annotation_schema_version_id=%s
            """,
            (annotation_schema_version_id,),
        ).fetchall()
        if mode == "production" and (
            schema["status"] != "published"
            or any(pin["status"] != "published" for pin in taxonomy_pins)
        ):
            raise ValueError("production projects require published schema and taxonomies")
        if source_project_id is not None:
            source = connection.execute(
                """
                SELECT id,corpus_id,preprocessing_run_id,status
                FROM projects WHERE id=%s
                """,
                (source_project_id,),
            ).fetchone()
            if source is None or source["status"] not in {"active", "paused"}:
                raise ValueError("source project must be active or paused")
            if (source["corpus_id"], source["preprocessing_run_id"]) != (
                corpus_id,
                preprocessing_run_id,
            ):
                raise ValueError(
                    "source project must use the same corpus and preprocessing run"
                )
            source_fields = connection.execute(
                """
                SELECT afd.field_key
                FROM projects source_project
                JOIN annotation_field_definitions afd
                  ON afd.annotation_schema_version_id=
                     source_project.annotation_schema_version_id
                WHERE source_project.id=%s AND afd.is_active
                """,
                (source_project_id,),
            ).fetchall()
            required_source_fields = {
                "is_non_policy",
                "primary_australian_domain",
                "secondary_australian_domains",
            }
            if not required_source_fields.issubset(
                {str(field["field_key"]) for field in source_fields}
            ):
                raise ValueError(
                    "source project must use the streamlined general-domain schema"
                )
        project = connection.execute(
            """
            INSERT INTO projects
              (slug,name,description,mode,corpus_id,preprocessing_run_id,
               annotation_schema_version_id,schema_content_sha256,created_by_user_id,
               source_project_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id,public_id,slug,name,status,mode
            """,
            (
                slug,
                name.strip(),
                description.strip(),
                mode,
                corpus_id,
                preprocessing_run_id,
                annotation_schema_version_id,
                schema["content_sha256"],
                principal.user_id,
                source_project_id,
            ),
        ).fetchone()
        assert project is not None
        connection.cursor().executemany(
            """
            INSERT INTO project_taxonomy_pins
              (project_id,taxonomy_version_id,content_sha256)
            VALUES (%s,%s,%s)
            """,
            [
                (project["id"], pin["id"], pin["content_sha256"])
                for pin in taxonomy_pins
            ],
        )
        connection.execute(
            """
            INSERT INTO project_memberships
              (project_id,user_id,project_role,added_by_user_id)
            VALUES (%s,%s,'project_manager',%s)
            """,
            (project["id"], principal.user_id, principal.user_id),
        )
        record_event(
            connection,
            "project_created",
            "project",
            actor_user_id=principal.user_id,
            entity_public_id=project["public_id"],
            project_id=project["id"],
            metadata={
                "mode": mode,
                "corpus_id": corpus_id,
                "preprocessing_run_id": preprocessing_run_id,
                "schema_version_id": annotation_schema_version_id,
                "source_project_id": source_project_id,
            },
        )
        return dict(project)


def get_project(
    settings: WebSettings, principal: Principal, project_id: int
) -> dict[str, object]:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        project_role = require_project_role(connection, principal, project_id, VIEW_ROLES)
        project = connection.execute(
            """
            SELECT p.*,c.name AS corpus_name,c.date_from,c.date_to,
                   pr.run_name,pr.pipeline_version,pr.manifest_sha256,
                   pr.inventory_sha256,ans.name AS schema_name,
                   asv.version AS schema_version,asv.status AS schema_status,
                   asv.content_sha256 AS schema_hash,
                   source.name AS source_project_name,
                   source.slug AS source_project_slug
            FROM projects p JOIN corpora c ON c.id=p.corpus_id
            LEFT JOIN projects source ON source.id=p.source_project_id
            JOIN preprocessing_runs pr ON pr.id=p.preprocessing_run_id
            JOIN annotation_schema_versions asv
              ON asv.id=p.annotation_schema_version_id
            JOIN annotation_schemas ans ON ans.id=asv.annotation_schema_id
            WHERE p.id=%s
            """,
            (project_id,),
        ).fetchone()
        if project is None:
            raise LookupError("project not found")
        taxonomies = connection.execute(
            """
            SELECT tx.name,tx.slug,tv.version,tv.status,ptp.content_sha256
            FROM project_taxonomy_pins ptp
            JOIN taxonomy_versions tv ON tv.id=ptp.taxonomy_version_id
            JOIN taxonomies tx ON tx.id=tv.taxonomy_id
            WHERE ptp.project_id=%s ORDER BY tx.name
            """,
            (project_id,),
        ).fetchall()
        members = connection.execute(
            """
            SELECT u.id,u.public_id,u.username,u.display_name,pm.project_role,pm.status
            FROM project_memberships pm JOIN users u ON u.id=pm.user_id
            WHERE pm.project_id=%s ORDER BY u.display_name
            """,
            (project_id,),
        ).fetchall()
        task_counts = connection.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status='available') AS available,
                   count(*) FILTER (WHERE status='in_progress') AS in_progress,
                   count(*) FILTER (WHERE status='submitted') AS submitted,
                   count(*) FILTER (WHERE status='flagged') AS flagged,
                   count(*) FILTER (WHERE status='excluded') AS excluded
            FROM tasks WHERE project_id=%s
            """,
            (project_id,),
        ).fetchone()
        progress_by_annotator = connection.execute(
            """
            SELECT u.display_name,
                   count(*) AS total,
                   count(*) FILTER (WHERE a.status='submitted') AS submitted,
                   count(*) FILTER (WHERE a.status='skipped') AS skipped,
                   count(*) FILTER (WHERE a.status='flagged') AS flagged
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            JOIN users u ON u.id=a.user_id
            WHERE t.project_id=%s
            GROUP BY u.id,u.display_name ORDER BY u.display_name
            """,
            (project_id,),
        ).fetchall()
        progress_by_batch = connection.execute(
            """
            SELECT b.name,b.selected_count,
                   count(t.id) FILTER (WHERE t.status='submitted') AS submitted
            FROM batches b LEFT JOIN tasks t ON t.batch_id=b.id
            WHERE b.project_id=%s
            GROUP BY b.id,b.name,b.selected_count ORDER BY b.created_at
            """,
            (project_id,),
        ).fetchall()
        distributions = connection.execute(
            """
            SELECT
              CASE
                WHEN av.values ? 'discusses_aukus' THEN
                  CASE WHEN (av.values->>'discusses_aukus')::boolean
                       THEN 'AUKUS discussion' ELSE 'Not AUKUS' END
                WHEN COALESCE((av.values->>'is_non_policy')::boolean,false)
                  THEN 'Entirely non-policy'
                ELSE COALESCE(av.values->>'primary_australian_domain','Policy')
              END AS distribution_label,
              CASE WHEN av.values ? 'secondary_australian_domains'
                   THEN 'General-domain pass' ELSE NULL END AS distribution_detail,
                   count(*) AS annotations
            FROM annotations an JOIN annotation_versions av
              ON av.id=an.current_version_id
            JOIN assignments a ON a.id=an.assignment_id
            JOIN tasks t ON t.id=a.task_id
            WHERE t.project_id=%s AND an.status IN ('submitted','revised')
            GROUP BY distribution_label,distribution_detail
            ORDER BY annotations DESC
            """,
            (project_id,),
        ).fetchall()
        users = connection.execute(
            """
            SELECT id,username,display_name FROM users
            WHERE status='active' ORDER BY display_name LIMIT 200
            """
        ).fetchall()
        unassigned = connection.execute(
            """
            SELECT a.id AS assignment_id,st.speaker_name_raw,st.speech_date,
                   st.calculated_word_count,b.name AS batch_name
            FROM assignments a JOIN tasks t ON t.id=a.task_id
            JOIN speaker_turns st ON st.id=t.speaker_turn_id
            JOIN batches b ON b.id=t.batch_id
            WHERE t.project_id=%s AND a.user_id IS NULL AND a.status='assigned'
            ORDER BY t.priority DESC,t.id LIMIT 50
            """,
            (project_id,),
        ).fetchall()
        result = dict(project)
        result["can_export"] = principal.is_admin or project_role in MANAGER_ROLES
        result["taxonomies"] = [dict(row) for row in taxonomies]
        result["members"] = [dict(row) for row in members]
        result["task_counts"] = dict(task_counts or {})
        result["progress_by_annotator"] = [
            dict(row) for row in progress_by_annotator
        ]
        result["progress_by_batch"] = [dict(row) for row in progress_by_batch]
        result["distributions"] = [dict(row) for row in distributions]
        result["available_users"] = [dict(row) for row in users]
        result["unassigned"] = [dict(row) for row in unassigned]
        return result


def set_membership(
    settings: WebSettings,
    principal: Principal,
    project_id: int,
    user_id: int,
    role: str,
) -> None:
    if role not in {"project_manager", "annotator", "adjudicator", "viewer"}:
        raise ValueError("invalid project role")
    with psycopg.connect(settings.database.psycopg_url) as connection:
        require_project_role(connection, principal, project_id, MANAGER_ROLES)
        connection.execute(
            """
            INSERT INTO project_memberships
              (project_id,user_id,project_role,status,added_by_user_id)
            VALUES (%s,%s,%s,'active',%s)
            ON CONFLICT (project_id,user_id) DO UPDATE
            SET project_role=EXCLUDED.project_role,status='active',
                removed_at=NULL,added_by_user_id=EXCLUDED.added_by_user_id
            """,
            (project_id, user_id, role, principal.user_id),
        )
        record_event(
            connection,
            "membership_changed",
            "project_membership",
            actor_user_id=principal.user_id,
            project_id=project_id,
            metadata={"user_id": user_id, "role": role},
        )


def transition_project(
    settings: WebSettings,
    principal: Principal,
    project_id: int,
    target: str,
) -> None:
    allowed = {
        "draft": {"active"},
        "active": {"paused", "archived"},
        "paused": {"active", "archived"},
        "archived": set(),
    }
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        require_project_role(connection, principal, project_id, MANAGER_ROLES)
        row = connection.execute(
            "SELECT status FROM projects WHERE id=%s FOR UPDATE", (project_id,)
        ).fetchone()
        if row is None or target not in allowed[row["status"]]:
            raise ValueError("invalid project lifecycle transition")
        timestamp_column = {
            "active": "activated_at",
            "paused": "paused_at",
            "archived": "archived_at",
        }[target]
        connection.execute(
            f"UPDATE projects SET status=%s,{timestamp_column}=now(),updated_at=now() "
            "WHERE id=%s",
            (target, project_id),
        )
        record_event(
            connection,
            f"project_{target}",
            "project",
            actor_user_id=principal.user_id,
            project_id=project_id,
            metadata={"from": row["status"], "to": target},
        )
