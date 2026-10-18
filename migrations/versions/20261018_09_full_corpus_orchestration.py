"""Add Phase 4E full-corpus child Batch orchestration.

Revision ID: 20261018_09
Revises: 20261017_08
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261018_09"
down_revision = "20261017_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          ADD COLUMN orchestration_mode text NOT NULL DEFAULT 'LEGACY'
            CHECK (orchestration_mode IN ('LEGACY','CHUNKED')),
          ADD COLUMN orchestration_status text NOT NULL DEFAULT 'INACTIVE'
            CHECK (orchestration_status IN
              ('INACTIVE','PLANNED','RUNNING','PAUSED','MAIN_COMPLETE',
               'RECOVERY_REQUIRED','COMPLETE','FAILED')),
          ADD COLUMN chunk_planner_version text,
          ADD COLUMN max_chunk_records integer CHECK (max_chunk_records>0),
          ADD COLUMN enqueued_token_ceiling bigint CHECK (enqueued_token_ceiling>0),
          ADD COLUMN enqueued_token_safety_factor numeric(6,5)
            CHECK (enqueued_token_safety_factor>0 AND enqueued_token_safety_factor<=1),
          ADD COLUMN authorized_spend_ceiling_usd numeric(18,8)
            CHECK (authorized_spend_ceiling_usd>=0),
          ADD COLUMN authorized_record_count integer CHECK (authorized_record_count>0),
          ADD COLUMN authorized_configuration jsonb
            CHECK (authorized_configuration IS NULL OR
                   jsonb_typeof(authorized_configuration)='object'),
          ADD COLUMN authorized_by_user_id bigint REFERENCES users(id) ON DELETE RESTRICT,
          ADD COLUMN authorized_at timestamptz,
          ADD COLUMN paused_at timestamptz;

        DO $$
        DECLARE constraint_name text;
        BEGIN
          SELECT conname INTO constraint_name
          FROM pg_constraint
          WHERE conrelid='ai_annotation_campaigns'::regclass AND contype='c'
            AND pg_get_constraintdef(oid) LIKE '%provider_job_id IS NOT NULL%';
          IF constraint_name IS NOT NULL THEN
            EXECUTE format(
              'ALTER TABLE ai_annotation_campaigns DROP CONSTRAINT %I',
              constraint_name
            );
          END IF;
        END $$;
        ALTER TABLE ai_annotation_campaigns ADD CONSTRAINT
          ck_ai_campaign_provider_job_or_chunked CHECK (
            orchestration_mode='CHUNKED' OR
            status NOT IN ('SUBMITTED','RUNNING','PROVIDER_COMPLETED','COLLECTED',
                           'VALIDATED','IMPORTED') OR provider_job_id IS NOT NULL
          );

        CREATE TABLE ai_annotation_campaign_batches (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          campaign_id bigint NOT NULL
            REFERENCES ai_annotation_campaigns(id) ON DELETE RESTRICT,
          chunk_number integer NOT NULL CHECK (chunk_number>0),
          first_selection_order integer NOT NULL CHECK (first_selection_order>=0),
          last_selection_order integer NOT NULL
            CHECK (last_selection_order>=first_selection_order),
          record_count integer NOT NULL CHECK
            (record_count=last_selection_order-first_selection_order+1),
          record_set_sha256 char(64) NOT NULL
            CHECK (record_set_sha256 ~ '^[0-9a-f]{64}$'),
          estimated_input_tokens bigint NOT NULL CHECK (estimated_input_tokens>0),
          planner_version text NOT NULL CHECK (btrim(planner_version)<>''),
          status text NOT NULL DEFAULT 'PENDING' CHECK (status IN
            ('PENDING','SUBMITTING','SUBMITTED','RUNNING','PROVIDER_COMPLETED',
             'COLLECTED','FAILED')),

          provider text NOT NULL,
          configured_model text NOT NULL,
          prompt_version text NOT NULL,
          prompt_sha256 char(64) NOT NULL,
          canonical_schema_sha256 char(64) NOT NULL,
          prediction_contract_sha256 char(64) NOT NULL,
          provider_schema_sha256 char(64) NOT NULL,
          generation_config jsonb NOT NULL CHECK
            (jsonb_typeof(generation_config)='object'),
          execution_mode text NOT NULL,

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
          cache_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

          provider_success_count integer NOT NULL DEFAULT 0 CHECK (provider_success_count>=0),
          provider_failure_count integer NOT NULL DEFAULT 0 CHECK (provider_failure_count>=0),
          valid_count integer NOT NULL DEFAULT 0 CHECK (valid_count>=0),
          repaired_count integer NOT NULL DEFAULT 0 CHECK (repaired_count>=0),
          invalid_count integer NOT NULL DEFAULT 0 CHECK (invalid_count>=0),
          missing_count integer NOT NULL DEFAULT 0 CHECK (missing_count>=0),
          duplicate_count integer NOT NULL DEFAULT 0 CHECK (duplicate_count>=0),
          input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens>=0),
          cached_input_tokens bigint NOT NULL DEFAULT 0 CHECK (cached_input_tokens>=0),
          output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens>=0),
          reasoning_tokens bigint NOT NULL DEFAULT 0 CHECK (reasoning_tokens>=0),
          processing_cost_usd numeric(18,8) NOT NULL DEFAULT 0,
          cache_cost_usd numeric(18,8) NOT NULL DEFAULT 0,
          artifact_reference text,
          artifact_sha256 char(64),
          failure_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          submission_started_at timestamptz,
          submitted_at timestamptz,
          provider_completed_at timestamptz,
          collected_at timestamptz,
          UNIQUE (campaign_id,chunk_number),
          UNIQUE (campaign_id,first_selection_order),
          UNIQUE (campaign_id,last_selection_order),
          UNIQUE (campaign_id,submission_display_name),
          CHECK (status NOT IN
            ('SUBMITTED','RUNNING','PROVIDER_COMPLETED','COLLECTED')
            OR provider_job_id IS NOT NULL),
          CHECK (status<>'COLLECTED' OR
            (artifact_reference IS NOT NULL AND artifact_sha256 IS NOT NULL))
        );
        CREATE INDEX ix_ai_campaign_batches_work
          ON ai_annotation_campaign_batches (campaign_id,status,chunk_number);

        CREATE FUNCTION protect_ai_campaign_batch_provenance() RETURNS trigger AS $$
        BEGIN
          IF NEW.campaign_id IS DISTINCT FROM OLD.campaign_id OR
             NEW.chunk_number IS DISTINCT FROM OLD.chunk_number OR
             NEW.first_selection_order IS DISTINCT FROM OLD.first_selection_order OR
             NEW.last_selection_order IS DISTINCT FROM OLD.last_selection_order OR
             NEW.record_count IS DISTINCT FROM OLD.record_count OR
             NEW.record_set_sha256 IS DISTINCT FROM OLD.record_set_sha256 OR
             NEW.estimated_input_tokens IS DISTINCT FROM OLD.estimated_input_tokens OR
             NEW.planner_version IS DISTINCT FROM OLD.planner_version OR
             NEW.provider IS DISTINCT FROM OLD.provider OR
             NEW.configured_model IS DISTINCT FROM OLD.configured_model OR
             NEW.prompt_version IS DISTINCT FROM OLD.prompt_version OR
             NEW.prompt_sha256 IS DISTINCT FROM OLD.prompt_sha256 OR
             NEW.canonical_schema_sha256 IS DISTINCT FROM OLD.canonical_schema_sha256 OR
             NEW.prediction_contract_sha256 IS DISTINCT FROM OLD.prediction_contract_sha256 OR
             NEW.provider_schema_sha256 IS DISTINCT FROM OLD.provider_schema_sha256 OR
             NEW.generation_config IS DISTINCT FROM OLD.generation_config OR
             NEW.execution_mode IS DISTINCT FROM OLD.execution_mode OR
             NEW.submission_display_name IS DISTINCT FROM OLD.submission_display_name THEN
            RAISE EXCEPTION 'campaign child Batch provenance is immutable';
          END IF;
          IF OLD.provider_job_id IS NOT NULL AND
             NEW.provider_job_id IS DISTINCT FROM OLD.provider_job_id THEN
            RAISE EXCEPTION 'campaign child provider job identity is immutable';
          END IF;
          IF OLD.cache_identifier IS NOT NULL AND
             (NEW.cache_identifier IS DISTINCT FROM OLD.cache_identifier OR
              NEW.cache_content_sha256 IS DISTINCT FROM OLD.cache_content_sha256 OR
              NEW.cache_model IS DISTINCT FROM OLD.cache_model) THEN
            RAISE EXCEPTION 'campaign child cache provenance is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_ai_campaign_batch_provenance
          BEFORE UPDATE ON ai_annotation_campaign_batches
          FOR EACH ROW EXECUTE FUNCTION protect_ai_campaign_batch_provenance();

        CREATE FUNCTION protect_ai_campaign_authorization() RETURNS trigger AS $$
        BEGIN
          IF OLD.authorized_at IS NOT NULL AND
             (NEW.authorized_at IS DISTINCT FROM OLD.authorized_at OR
              NEW.authorized_by_user_id IS DISTINCT FROM OLD.authorized_by_user_id OR
              NEW.authorized_spend_ceiling_usd IS DISTINCT FROM
                OLD.authorized_spend_ceiling_usd OR
              NEW.authorized_record_count IS DISTINCT FROM OLD.authorized_record_count OR
              NEW.enqueued_token_ceiling IS DISTINCT FROM OLD.enqueued_token_ceiling OR
              NEW.enqueued_token_safety_factor IS DISTINCT FROM
                OLD.enqueued_token_safety_factor OR
              NEW.authorized_configuration IS DISTINCT FROM OLD.authorized_configuration) THEN
            RAISE EXCEPTION 'full-production authorization is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_ai_campaign_authorization
          BEFORE UPDATE ON ai_annotation_campaigns
          FOR EACH ROW EXECUTE FUNCTION protect_ai_campaign_authorization();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = int(
        bind.execute(text("SELECT count(*) FROM ai_annotation_campaign_batches")).scalar_one()
    )
    authorized = int(
        bind.execute(
            text("SELECT count(*) FROM ai_annotation_campaigns WHERE authorized_at IS NOT NULL")
        ).scalar_one()
    )
    if populated or authorized:
        raise RuntimeError("Phase 4E downgrade refused: orchestration provenance exists")
    op.execute(
        """
        DROP TRIGGER trg_ai_campaign_authorization ON ai_annotation_campaigns;
        DROP FUNCTION protect_ai_campaign_authorization();
        DROP TABLE ai_annotation_campaign_batches;
        DROP FUNCTION protect_ai_campaign_batch_provenance();
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ck_ai_campaign_provider_job_or_chunked;
        ALTER TABLE ai_annotation_campaigns
          DROP COLUMN paused_at,
          DROP COLUMN authorized_at,
          DROP COLUMN authorized_by_user_id,
          DROP COLUMN authorized_configuration,
          DROP COLUMN authorized_record_count,
          DROP COLUMN authorized_spend_ceiling_usd,
          DROP COLUMN enqueued_token_safety_factor,
          DROP COLUMN enqueued_token_ceiling,
          DROP COLUMN max_chunk_records,
          DROP COLUMN chunk_planner_version,
          DROP COLUMN orchestration_status,
          DROP COLUMN orchestration_mode;
        ALTER TABLE ai_annotation_campaigns ADD CONSTRAINT
          ck_ai_campaign_provider_job_legacy CHECK (
            status NOT IN ('SUBMITTED','RUNNING','PROVIDER_COMPLETED','COLLECTED',
                           'VALIDATED','IMPORTED') OR provider_job_id IS NOT NULL
          );
        """
    )
