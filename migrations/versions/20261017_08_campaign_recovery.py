"""Add append-only lineage for targeted campaign recovery attempts.

Revision ID: 20261017_08
Revises: 20261013_07
Create Date: 2026-10-17
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261017_08"
down_revision = "20261013_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_annotation_campaign_recoveries (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          campaign_id bigint NOT NULL
            REFERENCES ai_annotation_campaigns(id) ON DELETE RESTRICT,
          attempt_number integer NOT NULL CHECK (attempt_number>0),
          unresolved_set_sha256 char(64) NOT NULL
            CHECK (unresolved_set_sha256 ~ '^[0-9a-f]{64}$'),
          request_count integer NOT NULL CHECK (request_count>0),
          status text NOT NULL CHECK (status IN
            ('PREPARING','SUBMITTED','RUNNING','PROVIDER_COMPLETED','COLLECTED','FAILED')),
          submission_display_name text NOT NULL CHECK (btrim(submission_display_name)<>''),
          provider_job_id text UNIQUE,
          provider_state text,
          cache_identifier text,
          cache_content_sha256 char(64),
          cache_model text,
          cache_display_name text,
          cache_created_at timestamptz,
          cache_expires_at timestamptz,
          cache_verified_at timestamptz,
          cache_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(cache_metadata)='object'),
          artifact_reference text,
          artifact_sha256 char(64),
          effective_artifact_reference text,
          effective_artifact_sha256 char(64),
          valid_count integer NOT NULL DEFAULT 0 CHECK (valid_count>=0),
          invalid_count integer NOT NULL DEFAULT 0 CHECK (invalid_count>=0),
          operational_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(operational_metadata)='object'),
          failure_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(failure_metadata)='object'),
          created_at timestamptz NOT NULL DEFAULT now(),
          submitted_at timestamptz,
          provider_completed_at timestamptz,
          collected_at timestamptz,
          UNIQUE (campaign_id,attempt_number),
          CHECK (status NOT IN
                   ('SUBMITTED','RUNNING','PROVIDER_COMPLETED','COLLECTED')
                 OR provider_job_id IS NOT NULL),
          CHECK (status<>'COLLECTED' OR
                 (artifact_reference IS NOT NULL AND artifact_sha256 IS NOT NULL))
        );
        CREATE UNIQUE INDEX uq_ai_campaign_recovery_active_set
          ON ai_annotation_campaign_recoveries (campaign_id,unresolved_set_sha256)
          WHERE status IN ('PREPARING','SUBMITTED','RUNNING','PROVIDER_COMPLETED');
        CREATE INDEX ix_ai_campaign_recovery_campaign
          ON ai_annotation_campaign_recoveries (campaign_id,attempt_number DESC);

        CREATE TABLE ai_annotation_campaign_recovery_records (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          recovery_id bigint NOT NULL
            REFERENCES ai_annotation_campaign_recoveries(id) ON DELETE RESTRICT,
          campaign_record_id bigint NOT NULL
            REFERENCES ai_annotation_campaign_records(id) ON DELETE RESTRICT,
          record_id text NOT NULL CHECK (btrim(record_id)<>''),
          original_request_index integer NOT NULL CHECK (original_request_index>=0),
          original_validation_error text NOT NULL CHECK (btrim(original_validation_error)<>''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (recovery_id,campaign_record_id),
          UNIQUE (recovery_id,record_id),
          UNIQUE (recovery_id,original_request_index)
        );
        CREATE TRIGGER trg_ai_campaign_recovery_records_append_only
          BEFORE UPDATE OR DELETE ON ai_annotation_campaign_recovery_records
          FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();

        CREATE FUNCTION protect_ai_campaign_recovery_provenance() RETURNS trigger AS $$
        BEGIN
          IF NEW.campaign_id IS DISTINCT FROM OLD.campaign_id OR
             NEW.attempt_number IS DISTINCT FROM OLD.attempt_number OR
             NEW.unresolved_set_sha256 IS DISTINCT FROM OLD.unresolved_set_sha256 OR
             NEW.request_count IS DISTINCT FROM OLD.request_count OR
             NEW.submission_display_name IS DISTINCT FROM OLD.submission_display_name THEN
            RAISE EXCEPTION 'campaign recovery provenance is immutable';
          END IF;
          IF OLD.provider_job_id IS NOT NULL AND
             NEW.provider_job_id IS DISTINCT FROM OLD.provider_job_id THEN
            RAISE EXCEPTION 'campaign recovery provider job identity is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_ai_campaign_recovery_provenance
          BEFORE UPDATE ON ai_annotation_campaign_recoveries
          FOR EACH ROW EXECUTE FUNCTION protect_ai_campaign_recovery_provenance();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = int(
        bind.execute(
            text("SELECT count(*) FROM ai_annotation_campaign_recoveries")
        ).scalar_one()
    )
    if populated:
        raise RuntimeError("Phase 4D recovery downgrade refused: recovery provenance exists")
    op.execute(
        """
        DROP TABLE ai_annotation_campaign_recovery_records;
        DROP TRIGGER trg_ai_campaign_recovery_provenance
          ON ai_annotation_campaign_recoveries;
        DROP FUNCTION protect_ai_campaign_recovery_provenance();
        DROP TABLE ai_annotation_campaign_recoveries;
        """
    )
