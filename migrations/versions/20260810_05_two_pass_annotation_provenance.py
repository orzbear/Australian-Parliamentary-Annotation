"""Add reproducible provenance for derived second-pass annotation projects.

Revision ID: 20260810_05
Revises: 20260724_04
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20260810_05"
down_revision = "20260724_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects ADD COLUMN source_project_id bigint
          REFERENCES projects(id) ON DELETE RESTRICT;
        ALTER TABLE projects ADD CONSTRAINT ck_projects_not_own_source
          CHECK (source_project_id IS NULL OR source_project_id <> id);
        CREATE INDEX ix_projects_source_project
          ON projects (source_project_id) WHERE source_project_id IS NOT NULL;

        ALTER TABLE tasks ADD COLUMN source_annotation_version_id bigint
          REFERENCES annotation_versions(id) ON DELETE RESTRICT;
        CREATE INDEX ix_tasks_source_annotation_version
          ON tasks (source_annotation_version_id)
          WHERE source_annotation_version_id IS NOT NULL;

        COMMENT ON COLUMN projects.source_project_id IS
          'Optional preceding human-annotation project used to derive this project.';
        COMMENT ON COLUMN tasks.source_annotation_version_id IS
          'Exact immutable submitted annotation version that qualified a derived task.';
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_two_pass_project()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_corpus bigint; source_run bigint;
        BEGIN
          IF NEW.source_project_id IS NOT NULL THEN
            SELECT corpus_id,preprocessing_run_id INTO source_corpus,source_run
              FROM projects WHERE id=NEW.source_project_id;
            IF source_corpus IS NULL
               OR (source_corpus,source_run) IS DISTINCT FROM
                  (NEW.corpus_id,NEW.preprocessing_run_id) THEN
              RAISE EXCEPTION
                'derived project source must pin the same corpus and preprocessing run'
                USING ERRCODE='23514';
            END IF;
          END IF;
          IF TG_OP='UPDATE'
             AND OLD.source_project_id IS DISTINCT FROM NEW.source_project_id
             AND (OLD.status<>'draft'
                  OR EXISTS (SELECT 1 FROM tasks WHERE project_id=OLD.id)) THEN
            RAISE EXCEPTION
              'derived project source is immutable after activation or task creation'
              USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER trg_validate_two_pass_project
          BEFORE INSERT OR UPDATE OF source_project_id,corpus_id,preprocessing_run_id,status
          ON projects FOR EACH ROW EXECUTE FUNCTION validate_two_pass_project();

        CREATE FUNCTION validate_two_pass_task()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target_source_project bigint; source_project bigint;
                source_turn bigint; source_event text;
        BEGIN
          SELECT source_project_id INTO target_source_project
            FROM projects WHERE id=NEW.project_id;
          IF target_source_project IS NULL
             AND NEW.source_annotation_version_id IS NOT NULL THEN
            RAISE EXCEPTION 'ordinary task cannot cite a source annotation version'
              USING ERRCODE='23514';
          END IF;
          IF target_source_project IS NOT NULL THEN
            IF NEW.source_annotation_version_id IS NULL THEN
              RAISE EXCEPTION 'derived task requires a source annotation version'
                USING ERRCODE='23514';
            END IF;
            SELECT source_task.project_id,source_task.speaker_turn_id,av.event_type
              INTO source_project,source_turn,source_event
            FROM annotation_versions av
            JOIN annotations an ON an.id=av.annotation_id
            JOIN assignments source_assignment
              ON source_assignment.id=an.assignment_id
            JOIN tasks source_task ON source_task.id=source_assignment.task_id
            WHERE av.id=NEW.source_annotation_version_id;
            IF (source_project,source_turn) IS DISTINCT FROM
               (target_source_project,NEW.speaker_turn_id)
               OR source_event NOT IN ('submitted','revised') THEN
              RAISE EXCEPTION
                'derived task source must be a submitted version for the same turn and source project'
                USING ERRCODE='23514';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER trg_validate_two_pass_task
          BEFORE INSERT OR UPDATE OF project_id,speaker_turn_id,
            source_annotation_version_id
          ON tasks FOR EACH ROW EXECUTE FUNCTION validate_two_pass_task();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    derived_projects = int(
        bind.execute(
            text("SELECT count(*) FROM projects WHERE source_project_id IS NOT NULL")
        ).scalar_one()
    )
    derived_tasks = int(
        bind.execute(
            text(
                "SELECT count(*) FROM tasks "
                "WHERE source_annotation_version_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if derived_projects or derived_tasks:
        raise RuntimeError(
            "Two-pass downgrade refused: derived project/task provenance would be lost"
        )
    op.execute(
        """
        DROP FUNCTION validate_two_pass_task() CASCADE;
        DROP FUNCTION validate_two_pass_project() CASCADE;
        ALTER TABLE tasks DROP COLUMN source_annotation_version_id;
        ALTER TABLE projects DROP CONSTRAINT ck_projects_not_own_source;
        ALTER TABLE projects DROP COLUMN source_project_id;
        """
    )
