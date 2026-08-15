"""Create the secure Phase 3 annotation web application schema.

Revision ID: 20260724_04
Revises: 20260724_03
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20260724_04"
down_revision = "20260724_03"
branch_labels = None
depends_on = None

TABLES_WITH_USER_DATA = (
    "users",
    "user_global_roles",
    "web_sessions",
    "projects",
    "project_taxonomy_pins",
    "project_memberships",
    "batches",
    "tasks",
    "assignments",
    "annotations",
    "annotation_versions",
    "audit_events",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          username text NOT NULL,
          email text,
          display_name text NOT NULL,
          status text NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','disabled')),
          must_change_password boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          disabled_at timestamptz,
          last_login_at timestamptz
        );
        CREATE UNIQUE INDEX uq_users_username_ci ON users (lower(username));
        CREATE UNIQUE INDEX uq_users_email_ci ON users (lower(email))
          WHERE email IS NOT NULL;

        CREATE TABLE user_credentials (
          user_id bigint PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          password_hash text NOT NULL,
          password_changed_at timestamptz NOT NULL DEFAULT now(),
          failed_login_count integer NOT NULL DEFAULT 0
            CHECK (failed_login_count >= 0),
          locked_until timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE global_roles (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          role_key text NOT NULL UNIQUE CHECK (role_key IN ('admin')),
          description text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO global_roles (role_key,description)
        VALUES ('admin','System-wide account and project administration')
        ON CONFLICT (role_key) DO NOTHING;

        CREATE TABLE user_global_roles (
          user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          global_role_id bigint NOT NULL REFERENCES global_roles(id) ON DELETE RESTRICT,
          granted_by_user_id bigint REFERENCES users(id) ON DELETE SET NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (user_id,global_role_id)
        );

        CREATE TABLE web_sessions (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          token_hash char(64) NOT NULL UNIQUE
            CHECK (token_hash ~ '^[0-9a-f]{64}$'),
          csrf_secret_hash char(64) NOT NULL
            CHECK (csrf_secret_hash ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          idle_expires_at timestamptz NOT NULL,
          absolute_expires_at timestamptz NOT NULL,
          revoked_at timestamptz,
          user_agent_summary text,
          created_ip_hash char(64),
          CHECK (idle_expires_at <= absolute_expires_at)
        );
        CREATE INDEX ix_web_sessions_active
          ON web_sessions (token_hash,idle_expires_at)
          WHERE revoked_at IS NULL;
        CREATE INDEX ix_web_sessions_user_active
          ON web_sessions (user_id) WHERE revoked_at IS NULL;

        CREATE TABLE projects (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          slug text NOT NULL UNIQUE
            CHECK (slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'),
          name text NOT NULL,
          description text NOT NULL DEFAULT '',
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','active','paused','archived')),
          mode text NOT NULL
            CHECK (mode IN ('development','pilot','production')),
          corpus_id bigint NOT NULL REFERENCES corpora(id) ON DELETE RESTRICT,
          preprocessing_run_id bigint NOT NULL
            REFERENCES preprocessing_runs(id) ON DELETE RESTRICT,
          annotation_schema_version_id bigint NOT NULL
            REFERENCES annotation_schema_versions(id) ON DELETE RESTRICT,
          schema_content_sha256 char(64) NOT NULL
            CHECK (schema_content_sha256 ~ '^[0-9a-f]{64}$'),
          created_by_user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          activated_at timestamptz,
          paused_at timestamptz,
          archived_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_projects_status ON projects (status);
        CREATE INDEX ix_projects_corpus_run
          ON projects (corpus_id,preprocessing_run_id);

        CREATE TABLE project_taxonomy_pins (
          project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          taxonomy_version_id bigint NOT NULL
            REFERENCES taxonomy_versions(id) ON DELETE RESTRICT,
          content_sha256 char(64) NOT NULL
            CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
          PRIMARY KEY (project_id,taxonomy_version_id)
        );

        CREATE TABLE project_memberships (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          project_role text NOT NULL CHECK (
            project_role IN ('project_manager','annotator','adjudicator','viewer')
          ),
          status text NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','removed')),
          added_by_user_id bigint REFERENCES users(id) ON DELETE SET NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          removed_at timestamptz,
          UNIQUE (project_id,user_id)
        );
        CREATE INDEX ix_project_memberships_user
          ON project_memberships (user_id,status,project_id);
        CREATE INDEX ix_project_memberships_project
          ON project_memberships (project_id,status,project_role);

        CREATE TABLE batches (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          project_id bigint NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
          name text NOT NULL,
          description text,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','generated','closed')),
          selection_criteria jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(selection_criteria)='object'),
          random_seed bigint,
          requested_count integer CHECK (requested_count IS NULL OR requested_count > 0),
          selected_count integer NOT NULL DEFAULT 0 CHECK (selected_count >= 0),
          selection_sha256 char(64)
            CHECK (selection_sha256 IS NULL OR selection_sha256 ~ '^[0-9a-f]{64}$'),
          created_by_user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at timestamptz NOT NULL DEFAULT now(),
          finalised_at timestamptz,
          UNIQUE (project_id,name)
        );
        CREATE INDEX ix_batches_project_status ON batches (project_id,status);

        CREATE TABLE tasks (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          project_id bigint NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
          batch_id bigint NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
          speaker_turn_id bigint NOT NULL REFERENCES speaker_turns(id) ON DELETE RESTRICT,
          status text NOT NULL DEFAULT 'available' CHECK (
            status IN ('available','in_progress','submitted','flagged','excluded')
          ),
          priority integer NOT NULL DEFAULT 0,
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          excluded_at timestamptz,
          exclusion_reason text,
          UNIQUE (project_id,speaker_turn_id)
        );
        CREATE INDEX ix_tasks_project_status_priority
          ON tasks (project_id,status,priority DESC,id);
        CREATE INDEX ix_tasks_batch ON tasks (batch_id,status);

        CREATE TABLE assignments (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          task_id bigint NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
          user_id bigint REFERENCES users(id) ON DELETE RESTRICT,
          status text NOT NULL DEFAULT 'assigned' CHECK (
            status IN (
              'assigned','claimed','in_progress','submitted',
              'skipped','flagged','withdrawn'
            )
          ),
          assigned_by_user_id bigint REFERENCES users(id) ON DELETE SET NULL,
          assigned_at timestamptz NOT NULL DEFAULT now(),
          claimed_at timestamptz,
          last_opened_at timestamptz,
          submitted_at timestamptz,
          skipped_at timestamptz,
          flagged_at timestamptz,
          flag_reason text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK (status <> 'flagged' OR length(trim(flag_reason)) > 0)
        );
        CREATE UNIQUE INDEX uq_assignments_task_user_active
          ON assignments (task_id,user_id)
          WHERE user_id IS NOT NULL
            AND status IN ('assigned','claimed','in_progress');
        CREATE INDEX ix_assignments_user_status
          ON assignments (user_id,status,assigned_at,id);
        CREATE INDEX ix_assignments_task ON assignments (task_id,status);

        CREATE TABLE annotations (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          assignment_id bigint NOT NULL UNIQUE
            REFERENCES assignments(id) ON DELETE RESTRICT,
          annotation_schema_version_id bigint NOT NULL
            REFERENCES annotation_schema_versions(id) ON DELETE RESTRICT,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','submitted','revised')),
          current_version_id bigint,
          created_at timestamptz NOT NULL DEFAULT now(),
          submitted_at timestamptz,
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_annotations_assignment ON annotations (assignment_id);

        CREATE TABLE annotation_versions (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          annotation_id bigint NOT NULL REFERENCES annotations(id) ON DELETE RESTRICT,
          revision_number integer NOT NULL CHECK (revision_number > 0),
          event_type text NOT NULL
            CHECK (event_type IN ('draft_saved','submitted','revised')),
          values jsonb NOT NULL CHECK (jsonb_typeof(values)='object'),
          values_sha256 char(64) NOT NULL
            CHECK (values_sha256 ~ '^[0-9a-f]{64}$'),
          validation_result jsonb NOT NULL
            CHECK (jsonb_typeof(validation_result)='object'),
          created_by_user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (annotation_id,revision_number)
        );
        CREATE INDEX ix_annotation_versions_order
          ON annotation_versions (annotation_id,revision_number DESC);
        ALTER TABLE annotations ADD CONSTRAINT fk_annotations_current_version
          FOREIGN KEY (current_version_id) REFERENCES annotation_versions(id)
          DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE audit_events (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          actor_user_id bigint REFERENCES users(id) ON DELETE SET NULL,
          event_type text NOT NULL,
          entity_type text NOT NULL,
          entity_public_id uuid,
          project_id bigint REFERENCES projects(id) ON DELETE SET NULL,
          metadata jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(metadata)='object'),
          occurred_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_audit_events_time ON audit_events (occurred_at DESC,id DESC);
        CREATE INDEX ix_audit_events_project
          ON audit_events (project_id,occurred_at DESC);
        CREATE INDEX ix_audit_events_actor
          ON audit_events (actor_user_id,occurred_at DESC);

        COMMENT ON TABLE web_sessions IS
          'Server-side sessions; token_hash is a one-way application-peppered digest.';
        COMMENT ON TABLE annotation_versions IS
          'Append-only human annotation revision evidence; UPDATE and DELETE are rejected.';
        COMMENT ON TABLE audit_events IS
          'Append-only sanitised security and research workflow audit evidence.';
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_phase3_project()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE run_corpus bigint; schema_status text; schema_hash text;
        BEGIN
          SELECT corpus_id INTO run_corpus FROM preprocessing_runs
            WHERE id=NEW.preprocessing_run_id;
          IF run_corpus IS DISTINCT FROM NEW.corpus_id THEN
            RAISE EXCEPTION 'project preprocessing run does not belong to corpus'
              USING ERRCODE='23514';
          END IF;
          SELECT status,content_sha256 INTO schema_status,schema_hash
            FROM annotation_schema_versions
            WHERE id=NEW.annotation_schema_version_id;
          IF schema_hash IS DISTINCT FROM NEW.schema_content_sha256 THEN
            RAISE EXCEPTION 'project schema hash does not match pinned version'
              USING ERRCODE='23514';
          END IF;
          IF NEW.mode='production' AND schema_status<>'published' THEN
            RAISE EXCEPTION 'production project requires published schema'
              USING ERRCODE='23514';
          END IF;
          IF TG_OP='UPDATE'
             AND (OLD.corpus_id,OLD.preprocessing_run_id,OLD.annotation_schema_version_id)
               IS DISTINCT FROM
                 (NEW.corpus_id,NEW.preprocessing_run_id,NEW.annotation_schema_version_id)
             AND (OLD.status<>'draft'
                  OR EXISTS (SELECT 1 FROM tasks WHERE project_id=OLD.id)) THEN
            RAISE EXCEPTION 'project pins are immutable after activation or task creation'
              USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER trg_validate_phase3_project
          BEFORE INSERT OR UPDATE ON projects
          FOR EACH ROW EXECUTE FUNCTION validate_phase3_project();

        CREATE FUNCTION validate_phase3_task()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE project_run bigint; turn_run bigint; batch_project bigint;
        BEGIN
          SELECT preprocessing_run_id INTO project_run
            FROM projects WHERE id=NEW.project_id;
          SELECT rr.preprocessing_run_id INTO turn_run
            FROM speaker_turns st JOIN reconstruction_runs rr
              ON rr.id=st.reconstruction_run_id
            WHERE st.id=NEW.speaker_turn_id;
          SELECT project_id INTO batch_project FROM batches WHERE id=NEW.batch_id;
          IF project_run IS DISTINCT FROM turn_run
             OR batch_project IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'task turn/batch is incompatible with project pin'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER trg_validate_phase3_task
          BEFORE INSERT OR UPDATE OF project_id,batch_id,speaker_turn_id ON tasks
          FOR EACH ROW EXECUTE FUNCTION validate_phase3_task();

        CREATE FUNCTION validate_phase3_annotation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE pinned_schema bigint;
        BEGIN
          SELECT p.annotation_schema_version_id INTO pinned_schema
          FROM assignments a
          JOIN tasks t ON t.id=a.task_id
          JOIN projects p ON p.id=t.project_id
          WHERE a.id=NEW.assignment_id;
          IF pinned_schema IS DISTINCT FROM NEW.annotation_schema_version_id THEN
            RAISE EXCEPTION 'annotation schema does not match project pin'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER trg_validate_phase3_annotation
          BEFORE INSERT OR UPDATE OF assignment_id,annotation_schema_version_id
          ON annotations FOR EACH ROW
          EXECUTE FUNCTION validate_phase3_annotation();

        CREATE FUNCTION reject_phase3_append_only_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only',TG_TABLE_NAME USING ERRCODE='55000';
        END
        $$;
        CREATE TRIGGER trg_annotation_versions_append_only
          BEFORE UPDATE OR DELETE ON annotation_versions
          FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();
        CREATE TRIGGER trg_audit_events_append_only
          BEFORE UPDATE OR DELETE ON audit_events
          FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = sum(
        int(
            bind.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one()
        )
        for table in TABLES_WITH_USER_DATA
    )
    if populated:
        raise RuntimeError(
            "Phase 3 downgrade refused: user, project, annotation, session, or audit "
            "data would be destroyed"
        )
    op.execute(
        """
        DROP FUNCTION reject_phase3_append_only_mutation() CASCADE;
        DROP FUNCTION validate_phase3_annotation() CASCADE;
        DROP FUNCTION validate_phase3_task() CASCADE;
        DROP FUNCTION validate_phase3_project() CASCADE;
        DROP TABLE audit_events;
        ALTER TABLE annotations DROP CONSTRAINT fk_annotations_current_version;
        DROP TABLE annotation_versions;
        DROP TABLE annotations;
        DROP TABLE assignments;
        DROP TABLE tasks;
        DROP TABLE batches;
        DROP TABLE project_memberships;
        DROP TABLE project_taxonomy_pins;
        DROP TABLE projects;
        DROP TABLE web_sessions;
        DROP TABLE user_global_roles;
        DROP TABLE global_roles;
        DROP TABLE user_credentials;
        DROP TABLE users;
        """
    )
