"""Add Phase 4D AI annotation campaign orchestration.

Revision ID: 20261013_07
Revises: 20261009_06
Create Date: 2026-10-13
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261013_07"
down_revision = "20261009_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_annotation_campaigns (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          name text NOT NULL CHECK (btrim(name)<>''),
          corpus_id bigint NOT NULL REFERENCES corpora(id) ON DELETE RESTRICT,
          created_by_user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          status text NOT NULL DEFAULT 'DRAFT' CHECK (status IN
            ('DRAFT','PLANNED','READY','SUBMITTED','RUNNING','PROVIDER_COMPLETED',
             'COLLECTED','VALIDATED','IMPORTED','FAILED','CANCELLED')),

          provider text NOT NULL,
          configured_model text NOT NULL,
          prompt_version text NOT NULL,
          prompt_sha256 char(64) NOT NULL CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
          canonical_schema_sha256 char(64) NOT NULL
            CHECK (canonical_schema_sha256 ~ '^[0-9a-f]{64}$'),
          prediction_contract_sha256 char(64) NOT NULL
            CHECK (prediction_contract_sha256 ~ '^[0-9a-f]{64}$'),
          provider_schema_sha256 char(64) NOT NULL
            CHECK (provider_schema_sha256 ~ '^[0-9a-f]{64}$'),
          generation_config jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(generation_config)='object'),
          execution_mode text NOT NULL,
          configuration_class text NOT NULL CHECK
            (configuration_class IN ('APPROVED_PRODUCTION','RESEARCH')),

          selection_definition jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(selection_definition)='object'),
          record_set_sha256 char(64) CHECK
            (record_set_sha256 IS NULL OR record_set_sha256 ~ '^[0-9a-f]{64}$'),
          corpus_total_count integer NOT NULL DEFAULT 0 CHECK (corpus_total_count>=0),
          eligible_count integer NOT NULL DEFAULT 0 CHECK (eligible_count>=0),
          excluded_existing_prediction_count integer NOT NULL DEFAULT 0
            CHECK (excluded_existing_prediction_count>=0),
          excluded_other_count integer NOT NULL DEFAULT 0 CHECK (excluded_other_count>=0),
          planned_count integer NOT NULL DEFAULT 0 CHECK (planned_count>=0),

          estimated_input_tokens bigint NOT NULL DEFAULT 0 CHECK (estimated_input_tokens>=0),
          estimated_cached_input_tokens bigint NOT NULL DEFAULT 0
            CHECK (estimated_cached_input_tokens>=0),
          estimated_uncached_input_tokens bigint NOT NULL DEFAULT 0
            CHECK (estimated_uncached_input_tokens>=0),
          estimated_output_tokens bigint NOT NULL DEFAULT 0 CHECK (estimated_output_tokens>=0),
          estimated_processing_cost_usd numeric(18,8),
          estimated_cache_storage_cost_usd numeric(18,8),
          estimated_total_cost_usd numeric(18,8),

          provider_job_id text UNIQUE,
          provider_state text,
          cache_status text NOT NULL DEFAULT 'REQUIRED' CHECK
            (cache_status IN ('REQUIRED','CREATING','REUSABLE','UNAVAILABLE')),
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
          planned_at timestamptz,
          submitted_at timestamptz,
          provider_completed_at timestamptz,
          collected_at timestamptz,
          validated_at timestamptz,
          imported_at timestamptz,
          provider_success_count integer NOT NULL DEFAULT 0 CHECK (provider_success_count>=0),
          provider_failure_count integer NOT NULL DEFAULT 0 CHECK (provider_failure_count>=0),
          valid_count integer NOT NULL DEFAULT 0 CHECK (valid_count>=0),
          invalid_count integer NOT NULL DEFAULT 0 CHECK (invalid_count>=0),
          operational_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(operational_metadata)='object'),
          failure_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(failure_metadata)='object'),
          model_prediction_run_id bigint UNIQUE
            REFERENCES model_prediction_runs(id) ON DELETE RESTRICT,
          CHECK (estimated_cached_input_tokens<=estimated_input_tokens),
          CHECK (status NOT IN ('PLANNED','READY','SUBMITTED','RUNNING','PROVIDER_COMPLETED',
                                'COLLECTED','VALIDATED','IMPORTED')
                 OR (record_set_sha256 IS NOT NULL AND planned_count>0)),
          CHECK (status NOT IN ('SUBMITTED','RUNNING','PROVIDER_COMPLETED','COLLECTED',
                                'VALIDATED','IMPORTED') OR provider_job_id IS NOT NULL),
          CHECK (status<>'IMPORTED' OR model_prediction_run_id IS NOT NULL)
        );
        CREATE INDEX ix_ai_annotation_campaigns_status
          ON ai_annotation_campaigns (status,created_at DESC);
        CREATE INDEX ix_ai_annotation_campaigns_configuration
          ON ai_annotation_campaigns
          (provider,configured_model,prompt_sha256,canonical_schema_sha256,created_at DESC);

        CREATE TABLE ai_annotation_campaign_records (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          campaign_id bigint NOT NULL REFERENCES ai_annotation_campaigns(id) ON DELETE RESTRICT,
          speaker_turn_id bigint NOT NULL REFERENCES speaker_turns(id) ON DELETE RESTRICT,
          record_id text NOT NULL CHECK (btrim(record_id)<>''),
          selection_order integer NOT NULL CHECK (selection_order>=0),
          record_sha256 char(64) NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (campaign_id,speaker_turn_id),
          UNIQUE (campaign_id,record_id),
          UNIQUE (campaign_id,selection_order)
        );
        CREATE INDEX ix_ai_annotation_campaign_records_turn
          ON ai_annotation_campaign_records (speaker_turn_id,campaign_id);
        CREATE TRIGGER trg_ai_annotation_campaign_records_append_only
          BEFORE UPDATE OR DELETE ON ai_annotation_campaign_records
          FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();

        CREATE FUNCTION protect_ai_campaign_provenance() RETURNS trigger AS $$
        BEGIN
          IF OLD.status NOT IN ('DRAFT','PLANNED','READY') AND (
            NEW.provider IS DISTINCT FROM OLD.provider OR
            NEW.configured_model IS DISTINCT FROM OLD.configured_model OR
            NEW.prompt_version IS DISTINCT FROM OLD.prompt_version OR
            NEW.prompt_sha256 IS DISTINCT FROM OLD.prompt_sha256 OR
            NEW.canonical_schema_sha256 IS DISTINCT FROM OLD.canonical_schema_sha256 OR
            NEW.prediction_contract_sha256 IS DISTINCT FROM OLD.prediction_contract_sha256 OR
            NEW.provider_schema_sha256 IS DISTINCT FROM OLD.provider_schema_sha256 OR
            NEW.generation_config IS DISTINCT FROM OLD.generation_config OR
            NEW.execution_mode IS DISTINCT FROM OLD.execution_mode OR
            NEW.selection_definition IS DISTINCT FROM OLD.selection_definition OR
            NEW.record_set_sha256 IS DISTINCT FROM OLD.record_set_sha256 OR
            NEW.planned_count IS DISTINCT FROM OLD.planned_count OR
            NEW.cache_status IS DISTINCT FROM OLD.cache_status OR
            NEW.cache_identifier IS DISTINCT FROM OLD.cache_identifier OR
            NEW.cache_content_sha256 IS DISTINCT FROM OLD.cache_content_sha256 OR
            NEW.cache_model IS DISTINCT FROM OLD.cache_model OR
            NEW.cache_display_name IS DISTINCT FROM OLD.cache_display_name OR
            NEW.cache_created_at IS DISTINCT FROM OLD.cache_created_at OR
            NEW.cache_expires_at IS DISTINCT FROM OLD.cache_expires_at OR
            NEW.cache_verified_at IS DISTINCT FROM OLD.cache_verified_at OR
            NEW.cache_metadata IS DISTINCT FROM OLD.cache_metadata
          ) THEN
            RAISE EXCEPTION 'submitted campaign provenance is immutable';
          END IF;
          IF OLD.provider_job_id IS NOT NULL
             AND NEW.provider_job_id IS DISTINCT FROM OLD.provider_job_id THEN
            RAISE EXCEPTION 'campaign provider job identity is immutable';
          END IF;
          IF OLD.model_prediction_run_id IS NOT NULL
             AND NEW.model_prediction_run_id IS DISTINCT FROM OLD.model_prediction_run_id THEN
            RAISE EXCEPTION 'campaign prediction-run link is immutable';
          END IF;
          NEW.updated_at := now();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_ai_annotation_campaign_provenance
          BEFORE UPDATE ON ai_annotation_campaigns
          FOR EACH ROW EXECUTE FUNCTION protect_ai_campaign_provenance();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = int(
        bind.execute(text("SELECT count(*) FROM ai_annotation_campaigns")).scalar_one()
    )
    if populated:
        raise RuntimeError("Phase 4D downgrade refused: campaign provenance exists")
    op.execute(
        """
        DROP TABLE ai_annotation_campaign_records;
        DROP TRIGGER trg_ai_annotation_campaign_provenance ON ai_annotation_campaigns;
        DROP FUNCTION protect_ai_campaign_provenance();
        DROP TABLE ai_annotation_campaigns;
        """
    )
