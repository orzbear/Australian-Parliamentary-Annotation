"""Human review events for immutable model predictions."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.llm_eval.prediction_tasks import changed_fields, human_suggestion
from hansard_annotator.web.annotations.renderer import load_schema
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings

REVIEW_FILTERS = {
    "unreviewed",
    "reviewed",
    "accepted",
    "edited",
    "flagged_codebook",
    "skipped",
}
RUN_FILTERS = {"needs_review", "completed", "all"}


def ai_review_runs(
    settings: WebSettings, *, run_filter: str = "all"
) -> list[dict[str, object]]:
    """Summarise immutable runs without collapsing their provenance."""
    if run_filter not in RUN_FILTERS:
        raise ValueError("invalid AI review run filter")
    having = {
        "needs_review": "(count(prediction.id) FILTER (WHERE terminal.review_status IS NULL) "
        "+ max(failures.unreviewed))>0",
        "completed": "(count(prediction.id)+max(failures.total))>0 AND "
        "(count(prediction.id) FILTER (WHERE terminal.review_status IS NULL) "
        "+ max(failures.unreviewed))=0",
        "all": "true",
    }[run_filter]
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            f"""
            SELECT run.public_id,run.provider,run.configured_model,run.prompt_version,
                   run.imported_at,campaign.name AS campaign_name,campaign.status AS campaign_status,
                   count(prediction.id)+max(failures.total) AS prediction_count,
                   count(prediction.id) FILTER (WHERE terminal.review_status IS NOT NULL)
                     + max(failures.reviewed) AS reviewed_count,
                   count(prediction.id) FILTER (WHERE terminal.review_status IS NULL)
                     + max(failures.unreviewed) AS unreviewed_count
            FROM model_prediction_runs run
            LEFT JOIN ai_annotation_campaigns campaign
              ON campaign.model_prediction_run_id=run.id
            LEFT JOIN model_predictions prediction ON prediction.run_id=run.id
            LEFT JOIN LATERAL (
              SELECT count(*) AS total,
                     count(*) FILTER (WHERE latest.review_status IS NOT NULL) AS reviewed,
                     count(*) FILTER (WHERE latest.review_status IS NULL) AS unreviewed
              FROM model_prediction_abstentions outcome
              LEFT JOIN LATERAL (
                SELECT review_status FROM model_prediction_outcome_reviews review
                WHERE review.abstention_id=outcome.id
                ORDER BY reviewed_at DESC,id DESC LIMIT 1
              ) latest ON true
              WHERE outcome.run_id=run.id AND outcome.resolution_type='INVALID_OUTPUT'
            ) failures ON true
            LEFT JOIN LATERAL (
              SELECT review_status FROM model_prediction_reviews review
              WHERE review.model_prediction_id=prediction.id
                AND review.review_status IN ('accepted','edited','skipped')
              ORDER BY reviewed_at DESC,id DESC LIMIT 1
            ) terminal ON true
            GROUP BY run.id,campaign.id HAVING {having}
            ORDER BY run.imported_at DESC NULLS LAST,run.id DESC
            """
        ).fetchall()
    output: list[dict[str, object]] = []
    for row in rows:
        value = dict(row)
        total = int(row["prediction_count"])
        reviewed = int(row["reviewed_count"])
        value["display_name"] = row["campaign_name"] or "Imported prediction run"
        value["remaining_count"] = int(row["unreviewed_count"])
        value["progress_percent"] = round(reviewed / total * 100) if total else 0
        value["review_complete"] = total > 0 and reviewed == total
        output.append(value)
    return output


def _domain_label_map(
    labels: dict[int, list[dict[str, Any]]],
) -> dict[str, str]:
    return {
        str(label["code"]): str(label["label"])
        for taxonomy_labels in labels.values()
        for label in taxonomy_labels
    }


def _domain_display(code: object, domain_labels: dict[str, str]) -> str:
    if code is None:
        return "None"
    value = str(code)
    label = domain_labels.get(value)
    return f"{value} — {label}" if label else value


def record_ai_review(
    settings: WebSettings,
    principal: Principal,
    assignment_id: int,
    status: str,
    *,
    annotation_version_id: int | None = None,
    note: str | None = None,
    prediction_public_id: str | None = None,
) -> dict[str, object] | None:
    if status not in {"accepted", "edited", "flagged_codebook", "skipped"}:
        raise ValueError("invalid AI review status")
    if status in {"accepted", "edited"} and annotation_version_id is None:
        raise ValueError("accepted or edited review requires a human annotation version")
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        prediction = connection.execute(
            """
            SELECT mp.id,mp.speaker_turn_id FROM assignments a
            JOIN tasks t ON t.id=a.task_id
            JOIN model_predictions mp ON mp.speaker_turn_id=t.speaker_turn_id
            JOIN model_prediction_runs run ON run.id=mp.run_id
            WHERE a.id=%s AND a.user_id=%s
              AND (%s IS NULL OR mp.public_id::text=%s)
            ORDER BY run.imported_at DESC,mp.id DESC LIMIT 1
            """,
            (assignment_id, principal.user_id, prediction_public_id, prediction_public_id),
        ).fetchone()
        if prediction is None:
            return None
        previous = connection.execute(
            """
            SELECT id,review_status,note,annotation_version_id
            FROM model_prediction_reviews
            WHERE model_prediction_id=%s AND reviewer_user_id=%s
            ORDER BY reviewed_at DESC,id DESC LIMIT 1
            """,
            (prediction["id"], principal.user_id),
        ).fetchone()
        if (
            previous
            and previous["review_status"] == status
            and previous["note"] == note
            and previous["annotation_version_id"] == annotation_version_id
        ):
            return {**dict(previous), "reused": True}
        row = connection.execute(
            """
            INSERT INTO model_prediction_reviews
              (model_prediction_id,speaker_turn_id,reviewer_user_id,assignment_id,
               annotation_version_id,review_status,note)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id,public_id,review_status,reviewed_at
            """,
            (
                prediction["id"],
                prediction["speaker_turn_id"],
                principal.user_id,
                assignment_id,
                annotation_version_id,
                status,
                note.strip() if note else None,
            ),
        ).fetchone()
        assert row is not None
        return {**dict(row), "reused": False}


def record_prediction_review(
    settings: WebSettings,
    principal: Principal,
    prediction_public_id: str,
    status: str,
    *,
    assignment_id: int | None = None,
    annotation_version_id: int | None = None,
    note: str | None = None,
) -> dict[str, object]:
    """Append a review event for an explicitly identified prediction."""
    if status not in {"accepted", "edited", "flagged_codebook", "skipped"}:
        raise ValueError("invalid AI review status")
    if status in {"accepted", "edited"} and annotation_version_id is None:
        raise ValueError("accepted or edited review requires a human annotation version")
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        prediction = connection.execute(
            "SELECT id,speaker_turn_id FROM model_predictions WHERE public_id::text=%s",
            (prediction_public_id,),
        ).fetchone()
        if prediction is None:
            raise LookupError("model prediction not found")
        previous = connection.execute(
            """
            SELECT id,review_status,note,annotation_version_id,assignment_id
            FROM model_prediction_reviews
            WHERE model_prediction_id=%s
            ORDER BY reviewed_at DESC,id DESC LIMIT 1
            """,
            (prediction["id"],),
        ).fetchone()
        cleaned_note = note.strip() if note else None
        if (
            previous
            and previous["review_status"] == status
            and previous["note"] == cleaned_note
            and previous["annotation_version_id"] == annotation_version_id
            and previous["assignment_id"] == assignment_id
        ):
            return {**dict(previous), "reused": True}
        row = connection.execute(
            """
            INSERT INTO model_prediction_reviews
              (model_prediction_id,speaker_turn_id,reviewer_user_id,assignment_id,
               annotation_version_id,review_status,note)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id,public_id,review_status,reviewed_at
            """,
            (
                prediction["id"],
                prediction["speaker_turn_id"],
                principal.user_id,
                assignment_id,
                annotation_version_id,
                status,
                cleaned_note,
            ),
        ).fetchone()
        assert row is not None
        return {**dict(row), "reused": False}


def record_outcome_review(
    settings: WebSettings,
    principal: Principal,
    outcome_public_id: str,
    status: str,
    *,
    assignment_id: int | None = None,
    annotation_version_id: int | None = None,
    note: str | None = None,
) -> dict[str, object]:
    if status not in {"human_annotated_after_ai_failure", "skipped"}:
        raise ValueError("invalid terminal outcome review status")
    if status == "human_annotated_after_ai_failure" and annotation_version_id is None:
        raise ValueError("human annotation after AI failure requires an annotation version")
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        outcome = connection.execute(
            "SELECT id,speaker_turn_id FROM model_prediction_abstentions "
            "WHERE public_id::text=%s AND resolution_type='INVALID_OUTPUT'",
            (outcome_public_id,),
        ).fetchone()
        if outcome is None:
            raise LookupError("terminal invalid outcome not found")
        row = connection.execute(
            """INSERT INTO model_prediction_outcome_reviews
               (abstention_id,speaker_turn_id,reviewer_user_id,assignment_id,
                annotation_version_id,review_status,note)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               RETURNING id,public_id,review_status,reviewed_at""",
            (outcome["id"], outcome["speaker_turn_id"], principal.user_id,
             assignment_id, annotation_version_id, status, note.strip() if note else None),
        ).fetchone()
        assert row is not None
        return {**dict(row), "reused": False}


def _compatible_review_project(
    connection: psycopg.Connection[dict[str, Any]],
    principal: Principal,
    prediction_id: int,
) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT p.id,p.annotation_schema_version_id,mpr.public_id AS run_public_id,
               mpr.valid_count,mpr.requested_count,mpr.record_set_sha256
        FROM model_predictions mp
        JOIN model_prediction_runs mpr ON mpr.id=mp.run_id
        JOIN speaker_turns st ON st.id=mp.speaker_turn_id
        JOIN reconstruction_runs rr ON rr.id=st.reconstruction_run_id
        JOIN projects p ON p.preprocessing_run_id=rr.preprocessing_run_id
        JOIN annotation_schema_versions asv ON asv.id=p.annotation_schema_version_id
        JOIN annotation_schemas ans ON ans.id=asv.annotation_schema_id
        WHERE mp.id=%s AND p.status='active'
          AND ans.slug=mpr.annotation_schema_slug AND asv.version=mpr.annotation_schema_version
          AND (
            %s OR EXISTS (
              SELECT 1 FROM project_memberships membership
              WHERE membership.project_id=p.id AND membership.user_id=%s
                AND membership.status='active'
                AND membership.project_role IN ('project_manager','annotator','adjudicator')
            )
          )
        ORDER BY p.id
        """,
        (prediction_id, principal.is_admin, principal.user_id),
    ).fetchall()
    if not rows:
        raise PermissionError("no active compatible annotation project is available")
    if len(rows) > 1:
        raise ValueError(
            "multiple active compatible review projects; pause all but the intended one"
        )
    return dict(rows[0])


def _terminal_failure_workspace(
    connection: psycopg.Connection[dict[str, Any]],
    principal: Principal,
    outcome_public_id: str,
) -> dict[str, object]:
    outcome = connection.execute(
        """SELECT outcome.id,outcome.public_id,outcome.speaker_turn_id,
                  outcome.resolution_reason,outcome.outcome_provenance,
                  run.public_id AS run_public_id,run.configured_model,run.prompt_version,
                  run.execution_mode,run.annotation_schema_slug,run.annotation_schema_version,
                  run.valid_count,run.record_set_sha256,st.turn_key,st.speech_date,st.chamber,
                  st.speaker_name_raw,st.major_heading_original,st.minor_heading_original,
                  st.business_type,st.calculated_word_count,st.text_clean
           FROM model_prediction_abstentions outcome
           JOIN model_prediction_runs run ON run.id=outcome.run_id
           JOIN speaker_turns st ON st.id=outcome.speaker_turn_id
           WHERE outcome.public_id::text=%s AND outcome.resolution_type='INVALID_OUTPUT'""",
        (outcome_public_id,),
    ).fetchone()
    if outcome is None:
        raise LookupError("model prediction or terminal outcome not found")
    projects = connection.execute(
        """SELECT p.id,p.annotation_schema_version_id
           FROM model_prediction_abstentions outcome
           JOIN model_prediction_runs run ON run.id=outcome.run_id
           JOIN speaker_turns st ON st.id=outcome.speaker_turn_id
           JOIN reconstruction_runs rr ON rr.id=st.reconstruction_run_id
           JOIN projects p ON p.preprocessing_run_id=rr.preprocessing_run_id
           JOIN annotation_schema_versions asv ON asv.id=p.annotation_schema_version_id
           JOIN annotation_schemas ans ON ans.id=asv.annotation_schema_id
           WHERE outcome.id=%s AND p.status='active' AND ans.slug=run.annotation_schema_slug
             AND asv.version=run.annotation_schema_version
             AND (%s OR EXISTS (SELECT 1 FROM project_memberships membership
               WHERE membership.project_id=p.id AND membership.user_id=%s
                 AND membership.status='active'
                 AND membership.project_role IN ('project_manager','annotator','adjudicator')))
           ORDER BY p.id""",
        (outcome["id"], principal.is_admin, principal.user_id),
    ).fetchall()
    if not projects:
        raise PermissionError("no active compatible annotation project is available")
    if len(projects) != 1:
        raise ValueError("multiple active compatible review projects; pause all but the intended one")
    fields, labels = load_schema(connection, int(projects[0]["annotation_schema_version_id"]))
    effective = connection.execute(
        """SELECT review_status,note,reviewed_at,reviewer_user_id,annotation_version_id
           FROM model_prediction_outcome_reviews WHERE abstention_id=%s
           ORDER BY reviewed_at DESC,id DESC LIMIT 1""",
        (outcome["id"],),
    ).fetchone()
    human = connection.execute(
        """SELECT av.values,av.revision_number,av.created_at,u.display_name
           FROM model_prediction_outcome_reviews review
           JOIN annotation_versions av ON av.id=review.annotation_version_id
           JOIN users u ON u.id=av.created_by_user_id
           WHERE review.abstention_id=%s
           ORDER BY review.reviewed_at DESC,review.id DESC LIMIT 1""",
        (outcome["id"],),
    ).fetchone()
    human_data = dict(human) if human else None
    if human_data:
        human_data["changed_fields"] = []
    return {
        "prediction": {
            **dict(outcome),
            "prediction_task": "hostility",
            "values": {},
            "reasoning": None,
            "ai_unavailable": True,
            "failure_message": "The frozen model response did not pass canonical evidence validation.",
        },
        "effective_review": dict(effective) if effective else None,
        "human_annotation": human_data,
        "codebook_flag": None,
        "fields": fields,
        "labels": labels,
        "values": dict(human["values"]) if human else {
            "hostility_present": None, "target": None, "evidence_span": []
        },
        "suggested_values": {"hostility_present": None, "target": None, "evidence_span": []},
        "ai_unavailable": True,
    }


def prediction_review_workspace(
    settings: WebSettings,
    principal: Principal,
    prediction_public_id: str,
) -> dict[str, object]:
    """Load speech, prediction, effective review, human result, and form metadata."""
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        prediction = connection.execute(
            """
            SELECT mp.id,mp.public_id,mp.speaker_turn_id,mp.prediction_task,
                   mp.prediction_values,mp.is_non_policy,
                   mp.primary_australian_domain,mp.secondary_australian_domains,
                   mp.reasoning,mp.created_at,
                   mpr.public_id AS run_public_id,mpr.configured_model,
                   mpr.prompt_version,mpr.execution_mode,
                   st.turn_key,st.speech_date,st.chamber,st.speaker_name_raw,
                   st.major_heading_original,st.minor_heading_original,
                   st.business_type,st.calculated_word_count,st.text_clean
            FROM model_predictions mp
            JOIN model_prediction_runs mpr ON mpr.id=mp.run_id
            JOIN speaker_turns st ON st.id=mp.speaker_turn_id
            WHERE mp.public_id::text=%s
            """,
            (prediction_public_id,),
        ).fetchone()
        if prediction is None:
            return _terminal_failure_workspace(
                connection, principal, prediction_public_id
            )
        project = _compatible_review_project(connection, principal, int(prediction["id"]))
        fields, labels = load_schema(connection, int(project["annotation_schema_version_id"]))
        domain_labels = _domain_label_map(labels)
        effective_review = connection.execute(
            """
            SELECT review.review_status,review.note,review.reviewed_at,
                   review.reviewer_user_id,review.annotation_version_id
            FROM model_prediction_reviews review
            WHERE review.model_prediction_id=%s
            ORDER BY review.reviewed_at DESC,review.id DESC LIMIT 1
            """,
            (prediction["id"],),
        ).fetchone()
        human = connection.execute(
            """
            SELECT av.values,av.revision_number,av.created_at,u.display_name
            FROM model_prediction_reviews review
            JOIN annotation_versions av ON av.id=review.annotation_version_id
            JOIN users u ON u.id=av.created_by_user_id
            WHERE review.model_prediction_id=%s
            ORDER BY review.reviewed_at DESC,review.id DESC LIMIT 1
            """,
            (prediction["id"],),
        ).fetchone()
        codebook_flag = connection.execute(
            """
            SELECT note,reviewed_at,reviewer_user_id
            FROM model_prediction_reviews
            WHERE model_prediction_id=%s AND review_status='flagged_codebook'
            ORDER BY reviewed_at DESC,id DESC LIMIT 1
            """,
            (prediction["id"],),
        ).fetchone()
        task = str(prediction["prediction_task"])
        if task not in {"policy", "hostility"}:
            raise ValueError("unsupported prediction task")
        prediction_values = dict(prediction["prediction_values"])
        # Old policy rows can only have an empty default when inserted by legacy test/setup code.
        if task == "policy" and not prediction_values:
            prediction_values = {
                "is_non_policy": prediction["is_non_policy"],
                "primary_australian_domain": prediction["primary_australian_domain"],
                "secondary_australian_domains": list(
                    prediction["secondary_australian_domains"] or []
                ),
            }
        suggested_values = human_suggestion(task, prediction_values)  # type: ignore[arg-type]
        values = dict(human["values"]) if human else suggested_values
        prediction_data = dict(prediction)
        prediction_data["values"] = prediction_values
        prediction_data["primary_domain_display"] = _domain_display(
            prediction["primary_australian_domain"], domain_labels
        )
        prediction_data["secondary_domain_displays"] = [
            _domain_display(code, domain_labels)
            for code in prediction["secondary_australian_domains"] or []
        ]
        human_data = dict(human) if human else None
        if human_data:
            human_values = dict(human_data["values"])
            human_data["primary_domain_display"] = _domain_display(
                human_values.get("primary_australian_domain"), domain_labels
            )
            human_data["secondary_domain_displays"] = [
                _domain_display(code, domain_labels)
                for code in human_values.get("secondary_australian_domains", [])
            ]
            human_data["changed_fields"] = changed_fields(
                task, suggested_values, human_values  # type: ignore[arg-type]
            )
        return {
            "prediction": prediction_data,
            "effective_review": dict(effective_review) if effective_review else None,
            "human_annotation": human_data,
            "codebook_flag": dict(codebook_flag) if codebook_flag else None,
            "fields": fields,
            "labels": labels,
            "values": values,
            "suggested_values": suggested_values,
        }


def ensure_prediction_assignment(
    settings: WebSettings,
    principal: Principal,
    prediction_public_id: str,
) -> int:
    """Create/reuse standard Phase 3 batch, task, and assignment rows on submission."""
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        prediction = connection.execute(
            "SELECT id,speaker_turn_id FROM model_predictions WHERE public_id::text=%s FOR SHARE",
            (prediction_public_id,),
        ).fetchone()
        if prediction is None:
            outcome = connection.execute(
                """SELECT outcome.id,outcome.speaker_turn_id,run.public_id AS run_public_id,
                          run.valid_count,run.requested_count,run.record_set_sha256,
                          run.annotation_schema_slug,run.annotation_schema_version
                   FROM model_prediction_abstentions outcome
                   JOIN model_prediction_runs run ON run.id=outcome.run_id
                   WHERE outcome.public_id::text=%s AND outcome.resolution_type='INVALID_OUTPUT'""",
                (prediction_public_id,),
            ).fetchone()
            if outcome is None:
                raise LookupError("model prediction or terminal outcome not found")
            projects = connection.execute(
                """SELECT p.id FROM model_prediction_abstentions outcome
                   JOIN model_prediction_runs run ON run.id=outcome.run_id
                   JOIN speaker_turns st ON st.id=outcome.speaker_turn_id
                   JOIN reconstruction_runs rr ON rr.id=st.reconstruction_run_id
                   JOIN projects p ON p.preprocessing_run_id=rr.preprocessing_run_id
                   JOIN annotation_schema_versions asv ON asv.id=p.annotation_schema_version_id
                   JOIN annotation_schemas ans ON ans.id=asv.annotation_schema_id
                   WHERE outcome.id=%s AND p.status='active'
                     AND ans.slug=run.annotation_schema_slug AND asv.version=run.annotation_schema_version
                     AND (%s OR EXISTS (SELECT 1 FROM project_memberships membership
                       WHERE membership.project_id=p.id AND membership.user_id=%s
                         AND membership.status='active'
                         AND membership.project_role IN ('project_manager','annotator','adjudicator')))
                   ORDER BY p.id""",
                (outcome["id"], principal.is_admin, principal.user_id),
            ).fetchall()
            if not projects:
                raise PermissionError("no active compatible annotation project is available")
            if len(projects) != 1:
                raise ValueError("multiple active compatible review projects; pause all but the intended one")
            project = {
                **dict(projects[0]),
                "run_public_id": outcome["run_public_id"],
                "valid_count": outcome["valid_count"],
                "requested_count": outcome["requested_count"],
                "record_set_sha256": outcome["record_set_sha256"],
            }
            speaker_turn_id = outcome["speaker_turn_id"]
        else:
            project = _compatible_review_project(connection, principal, int(prediction["id"]))
            speaker_turn_id = prediction["speaker_turn_id"]
        batch_name = f"AI review {project['run_public_id']}"
        criteria = {"source": "model_prediction_run", "run_id": str(project["run_public_id"])}
        batch = connection.execute(
            """
            INSERT INTO batches
              (project_id,name,description,status,selection_criteria,requested_count,
               selected_count,selection_sha256,created_by_user_id,finalised_at)
            VALUES (%s,%s,%s,'generated',%s,%s,0,%s,%s,now())
            ON CONFLICT (project_id,name) DO UPDATE SET name=EXCLUDED.name
            RETURNING id
            """,
            (
                project["id"],
                batch_name,
                "Human review assignments for an immutable model prediction run.",
                Jsonb(criteria),
                project["requested_count"],
                project["record_set_sha256"],
                principal.user_id,
            ),
        ).fetchone()
        assert batch is not None
        task = connection.execute(
            """
            INSERT INTO tasks (project_id,batch_id,speaker_turn_id,status)
            VALUES (%s,%s,%s,'in_progress')
            ON CONFLICT (project_id,speaker_turn_id) DO UPDATE SET priority=tasks.priority
            RETURNING id
            """,
            (project["id"], batch["id"], speaker_turn_id),
        ).fetchone()
        assert task is not None
        assignment = connection.execute(
            """
            SELECT id FROM assignments
            WHERE task_id=%s AND user_id=%s
            ORDER BY updated_at DESC,id DESC LIMIT 1
            """,
            (task["id"], principal.user_id),
        ).fetchone()
        if assignment is None:
            assignment = connection.execute(
                """
                INSERT INTO assignments
                  (task_id,user_id,assigned_by_user_id,status,claimed_at)
                VALUES (%s,%s,%s,'in_progress',now()) RETURNING id
                """,
                (task["id"], principal.user_id, principal.user_id),
            ).fetchone()
            assert assignment is not None
        connection.execute(
            """
            UPDATE batches SET selected_count=(
              SELECT count(*) FROM tasks WHERE batch_id=%s
            ) WHERE id=%s
            """,
            (batch["id"], batch["id"]),
        )
        return int(assignment["id"])


def next_unreviewed_prediction(
    settings: WebSettings, current_prediction_public_id: str | None = None
) -> str | None:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        current_run = (
            connection.execute(
                """SELECT run_id FROM model_predictions WHERE public_id::text=%s
                   UNION ALL SELECT run_id FROM model_prediction_abstentions
                   WHERE public_id::text=%s AND resolution_type='INVALID_OUTPUT' LIMIT 1""",
                (current_prediction_public_id, current_prediction_public_id),
            ).fetchone()
            if current_prediction_public_id
            else None
        )
        run_id = current_run["run_id"] if current_run else None
        row = connection.execute(
            """SELECT candidate.public_id FROM (
                 SELECT mp.public_id,mp.record_id,mp.id,mp.run_id
                 FROM model_predictions mp
                 WHERE NOT EXISTS (SELECT 1 FROM model_prediction_reviews review
                                   WHERE review.model_prediction_id=mp.id)
                 UNION ALL
                 SELECT outcome.public_id,outcome.record_id,outcome.id,outcome.run_id
                 FROM model_prediction_abstentions outcome
                 WHERE outcome.resolution_type='INVALID_OUTPUT'
                   AND NOT EXISTS (SELECT 1 FROM model_prediction_outcome_reviews review
                                   WHERE review.abstention_id=outcome.id)
               ) candidate
               WHERE (%s::bigint IS NULL OR candidate.run_id=%s)
                 AND (%s::text IS NULL OR candidate.public_id::text<>%s)
               ORDER BY candidate.record_id,candidate.id LIMIT 1""",
            (run_id, run_id, current_prediction_public_id, current_prediction_public_id),
        ).fetchone()
        return str(row["public_id"]) if row else None


def ai_review_summary(settings: WebSettings) -> dict[str, object]:
    """Return global prediction counts using the same latest-event semantics as queues."""
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT count(*) AS predictions,
                   count(*) FILTER (WHERE latest.review_status IS NULL) AS unreviewed,
                   count(*) FILTER (WHERE latest.review_status='accepted') AS accepted,
                   count(*) FILTER (WHERE latest.review_status='edited') AS edited,
                   count(*) FILTER (WHERE EXISTS (
                     SELECT 1 FROM model_prediction_reviews flag
                     WHERE flag.model_prediction_id=mp.id
                       AND flag.review_status='flagged_codebook'
                   )) AS flagged_codebook,
                   count(*) FILTER (WHERE latest.review_status='skipped') AS skipped
            FROM model_predictions mp
            LEFT JOIN LATERAL (
              SELECT review_status FROM model_prediction_reviews review
              WHERE review.model_prediction_id=mp.id
              ORDER BY reviewed_at DESC,id DESC LIMIT 1
            ) latest ON true
            """
        ).fetchone()
        counts = dict(row or {})
        failure_counts = connection.execute(
            """SELECT count(*) AS outcomes,
                      count(*) FILTER (WHERE latest.review_status IS NULL) AS unreviewed,
                      count(*) FILTER (WHERE latest.review_status='skipped') AS skipped,
                      count(*) FILTER (WHERE latest.review_status='human_annotated_after_ai_failure')
                        AS human_annotated_after_ai_failure
               FROM model_prediction_abstentions outcome
               LEFT JOIN LATERAL (
                 SELECT review_status FROM model_prediction_outcome_reviews review
                 WHERE review.abstention_id=outcome.id
                 ORDER BY reviewed_at DESC,id DESC LIMIT 1
               ) latest ON true
               WHERE outcome.resolution_type='INVALID_OUTPUT'"""
        ).fetchone()
        if failure_counts:
            counts["predictions"] = int(counts.get("predictions") or 0) + int(failure_counts["outcomes"])
            counts["unreviewed"] = int(counts.get("unreviewed") or 0) + int(failure_counts["unreviewed"])
            counts["skipped"] = int(counts.get("skipped") or 0) + int(failure_counts["skipped"])
            counts["human_annotated_after_ai_failure"] = int(
                failure_counts["human_annotated_after_ai_failure"]
            )
    counts["next_prediction_id"] = next_unreviewed_prediction(settings)
    return counts


def ai_review_queue(
    settings: WebSettings,
    principal: Principal,
    *,
    review_filter: str = "unreviewed",
    run_id: str | None = None,
    primary_domain: str | None = None,
    is_non_policy: bool | None = None,
) -> list[dict[str, object]]:
    """Return predictions with their effective latest append-only review event."""
    query, parameters = _ai_review_queue_query(
        principal.user_id,
        review_filter=review_filter,
        run_id=run_id,
        primary_domain=primary_domain,
        is_non_policy=is_non_policy,
    )
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        rows = connection.execute(query, parameters).fetchall()
        label_rows = connection.execute(
            """
            SELECT DISTINCT ON (code) code,label
            FROM taxonomy_labels WHERE is_active
            ORDER BY code,taxonomy_version_id DESC,id DESC
            """
        ).fetchall()
        domain_labels = {str(row["code"]): str(row["label"]) for row in label_rows}
        output: list[dict[str, object]] = []
        for row in rows:
            value = dict(row)
            value["primary_domain_display"] = _domain_display(
                row["primary_australian_domain"], domain_labels
            )
            value["secondary_domain_displays"] = [
                _domain_display(code, domain_labels)
                for code in row["secondary_australian_domains"] or []
            ]
            output.append(value)
        if (
            review_filter in {"unreviewed", "reviewed", "skipped"}
            and primary_domain is None
            and is_non_policy is None
        ):
            status_sql = {
                "unreviewed": "latest.review_status IS NULL",
                "reviewed": "latest.review_status IS NOT NULL",
                "skipped": "latest.review_status='skipped'",
            }[review_filter]
            filter_run = "AND run.public_id::text=%s" if run_id else ""
            failure_parameters: tuple[object, ...] = (
                (principal.user_id, run_id) if run_id else (principal.user_id,)
            )
            failure_rows = connection.execute(
                f"""SELECT assignment.id AS assignment_id,assignment.status AS assignment_status,
                           st.speaker_name_raw,st.speech_date,st.major_heading_original,
                           outcome.public_id::text AS prediction_id,'hostility'::text AS prediction_task,
                           NULL::jsonb AS prediction_values,NULL::boolean AS is_non_policy,
                           NULL::text AS primary_australian_domain,NULL::text[] AS secondary_australian_domains,
                           run.public_id::text AS run_id,run.configured_model,run.prompt_version,
                           latest.review_status,latest.reviewed_at,true AS ai_unavailable
                    FROM model_prediction_abstentions outcome
                    JOIN model_prediction_runs run ON run.id=outcome.run_id
                    JOIN speaker_turns st ON st.id=outcome.speaker_turn_id
                    LEFT JOIN LATERAL (
                      SELECT review_status,reviewed_at FROM model_prediction_outcome_reviews
                      WHERE abstention_id=outcome.id ORDER BY reviewed_at DESC,id DESC LIMIT 1
                    ) latest ON true
                    LEFT JOIN LATERAL (
                      SELECT a.id,a.status FROM tasks t JOIN assignments a ON a.task_id=t.id
                      WHERE t.speaker_turn_id=outcome.speaker_turn_id AND a.user_id=%s
                      ORDER BY a.updated_at DESC,a.id DESC LIMIT 1
                    ) assignment ON true
                    WHERE outcome.resolution_type='INVALID_OUTPUT' AND {status_sql} {filter_run}
                    ORDER BY st.speech_date,st.id,outcome.id LIMIT 500""",
                failure_parameters,
            ).fetchall()
            output.extend(dict(row) for row in failure_rows)
        return output


def _ai_review_queue_query(
    user_id: int,
    *,
    review_filter: str,
    run_id: str | None = None,
    primary_domain: str | None = None,
    is_non_policy: bool | None = None,
) -> tuple[str, tuple[object, ...]]:
    """Build the fixed allowlisted queue query and its bound parameters."""
    if review_filter not in REVIEW_FILTERS:
        raise ValueError("invalid AI review filter")
    conditions: list[str] = []
    parameters: list[object] = [user_id]
    if review_filter == "unreviewed":
        conditions.append("latest_review.review_status IS NULL")
    elif review_filter == "reviewed":
        conditions.append("latest_review.review_status IS NOT NULL")
    elif review_filter == "flagged_codebook":
        conditions.append(
            "EXISTS (SELECT 1 FROM model_prediction_reviews flag "
            "WHERE flag.model_prediction_id=mp.id "
            "AND flag.review_status='flagged_codebook')"
        )
    else:
        conditions.append("latest_review.review_status=%s")
        parameters.append(review_filter)
    if run_id:
        conditions.append("mpr.public_id::text=%s")
        parameters.append(run_id)
    if primary_domain:
        conditions.append("mp.primary_australian_domain=%s")
        parameters.append(primary_domain)
    if is_non_policy is not None:
        conditions.append("mp.is_non_policy=%s")
        parameters.append(is_non_policy)
    where = " AND ".join(conditions) if conditions else "true"
    return (
        f"""
            SELECT current_assignment.id AS assignment_id,
                   current_assignment.status AS assignment_status,
                   st.speaker_name_raw,st.speech_date,st.major_heading_original,
                   mp.public_id AS prediction_id,mp.prediction_task,mp.prediction_values,
                   mp.is_non_policy,
                   mp.primary_australian_domain,mp.secondary_australian_domains,
                   mpr.public_id AS run_id,
                   mpr.configured_model,mpr.prompt_version,
                   latest_review.review_status,latest_review.reviewed_at
            FROM model_predictions mp
            JOIN speaker_turns st ON st.id=mp.speaker_turn_id
            JOIN model_prediction_runs mpr ON mpr.id=mp.run_id
            LEFT JOIN LATERAL (
              SELECT review_status,reviewed_at,id
              FROM model_prediction_reviews review
              WHERE review.model_prediction_id=mp.id
              ORDER BY reviewed_at DESC,id DESC LIMIT 1
            ) latest_review ON true
            LEFT JOIN LATERAL (
              SELECT a.id,a.status
              FROM tasks t JOIN assignments a ON a.task_id=t.id
              WHERE t.speaker_turn_id=mp.speaker_turn_id AND a.user_id=%s
              ORDER BY CASE a.status
                WHEN 'in_progress' THEN 0 WHEN 'claimed' THEN 1
                WHEN 'assigned' THEN 2 WHEN 'skipped' THEN 3 ELSE 4 END,
                a.updated_at DESC,a.id DESC
              LIMIT 1
            ) current_assignment ON true
            WHERE {where}
            ORDER BY st.speech_date,st.id,mp.id
            LIMIT 500
            """,
        tuple(parameters),
    )
