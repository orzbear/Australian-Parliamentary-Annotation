"""Allow the frozen Luna production campaign and preserve OpenAI Batch file provenance.

Revision ID: 20261029_15
Revises: 20261029_14
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261029_15"
down_revision = "20261029_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_campaign_configuration_id_check,
          DROP CONSTRAINT ai_campaign_configuration_shape_check;
        ALTER TABLE ai_annotation_campaigns
          ADD CONSTRAINT ai_campaign_configuration_id_check CHECK
            (configuration_id IN
              ('policy-v2','hostility-v2','hostility-luna-confirmation-100',
               'hostility-luna-production-v1')),
          ADD CONSTRAINT ai_campaign_configuration_shape_check CHECK (
            (configuration_id='policy-v2' AND prediction_task='policy'
              AND annotation_schema_slug='australian_policy_annotation'
              AND annotation_schema_version='0.2.0'
              AND record_set_id IS NULL AND approved_record_set_sha256 IS NULL)
            OR
            (configuration_id='hostility-v2' AND prediction_task='hostility'
              AND annotation_schema_slug='australian_partisan_hostility'
              AND annotation_schema_version='0.2.0'
              AND record_set_id='hostility-heldout-100'
              AND approved_record_set_sha256=
                'f3d9451b3352025b769d263fc896f57683a741ea6fd9af0b6477651707cf3b36')
            OR
            (configuration_id='hostility-luna-confirmation-100'
              AND prediction_task='hostility'
              AND annotation_schema_slug='australian_partisan_hostility'
              AND annotation_schema_version='0.2.0'
              AND record_set_id='hostility-luna-confirmation-100'
              AND approved_record_set_sha256=
                '482d2951253840c5a294be028f6a61891693b6826ac3bdbebb087e71aa5c3c48')
            OR
            (configuration_id='hostility-luna-production-v1'
              AND prediction_task='hostility'
              AND annotation_schema_slug='australian_partisan_hostility'
              AND annotation_schema_version='0.2.0'
              AND record_set_id IS NULL AND approved_record_set_sha256 IS NULL)
          );

        ALTER TABLE ai_annotation_campaign_batches
          ADD COLUMN provider_input_file_id text,
          ADD COLUMN provider_input_file_sha256 char(64)
            CHECK (provider_input_file_sha256 IS NULL OR
                   provider_input_file_sha256 ~ '^[0-9a-f]{64}$'),
          ADD COLUMN provider_output_file_id text,
          ADD COLUMN provider_output_file_sha256 char(64)
            CHECK (provider_output_file_sha256 IS NULL OR
                   provider_output_file_sha256 ~ '^[0-9a-f]{64}$'),
          ADD COLUMN provider_error_file_id text,
          ADD COLUMN provider_error_file_sha256 char(64)
            CHECK (provider_error_file_sha256 IS NULL OR
                   provider_error_file_sha256 ~ '^[0-9a-f]{64}$'),
          ADD COLUMN retry_count integer NOT NULL DEFAULT 0
            CHECK (retry_count BETWEEN 0 AND 3),
          ADD COLUMN evidence_failure_count integer NOT NULL DEFAULT 0
            CHECK (evidence_failure_count>=0);

        ALTER TABLE ai_annotation_campaign_recoveries
          ADD COLUMN provider_input_file_id text,
          ADD COLUMN provider_input_file_sha256 char(64)
            CHECK (provider_input_file_sha256 IS NULL OR
                   provider_input_file_sha256 ~ '^[0-9a-f]{64}$'),
          ADD COLUMN provider_output_file_id text,
          ADD COLUMN provider_output_file_sha256 char(64)
            CHECK (provider_output_file_sha256 IS NULL OR
                   provider_output_file_sha256 ~ '^[0-9a-f]{64}$'),
          ADD COLUMN provider_error_file_id text;
        ALTER TABLE ai_annotation_campaign_recoveries
          ADD COLUMN provider_error_file_sha256 char(64)
            CHECK (provider_error_file_sha256 IS NULL OR
                   provider_error_file_sha256 ~ '^[0-9a-f]{64}$');

        CREATE FUNCTION protect_ai_campaign_recovery_openai_provenance() RETURNS trigger AS $$
        BEGIN
          IF OLD.provider_input_file_id IS NOT NULL AND
             (NEW.provider_input_file_id IS DISTINCT FROM OLD.provider_input_file_id OR
              NEW.provider_input_file_sha256 IS DISTINCT FROM OLD.provider_input_file_sha256) THEN
            RAISE EXCEPTION 'campaign recovery input file provenance is immutable';
          END IF;
          IF OLD.provider_output_file_id IS NOT NULL AND
             NEW.provider_output_file_id IS DISTINCT FROM OLD.provider_output_file_id THEN
            RAISE EXCEPTION 'campaign recovery output file identity is immutable';
          END IF;
          IF OLD.provider_output_file_sha256 IS NOT NULL AND
             NEW.provider_output_file_sha256 IS DISTINCT FROM OLD.provider_output_file_sha256 THEN
            RAISE EXCEPTION 'campaign recovery output file hash is immutable';
          END IF;
          IF OLD.provider_error_file_id IS NOT NULL AND
             NEW.provider_error_file_id IS DISTINCT FROM OLD.provider_error_file_id THEN
            RAISE EXCEPTION 'campaign recovery error file identity is immutable';
          END IF;
          IF OLD.provider_error_file_sha256 IS NOT NULL AND
             NEW.provider_error_file_sha256 IS DISTINCT FROM OLD.provider_error_file_sha256 THEN
            RAISE EXCEPTION 'campaign recovery error file hash is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_ai_campaign_recovery_openai_provenance
          BEFORE UPDATE ON ai_annotation_campaign_recoveries
          FOR EACH ROW EXECUTE FUNCTION protect_ai_campaign_recovery_openai_provenance();

        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_annotation_campaigns_cache_status_check,
          ADD CONSTRAINT ai_annotation_campaigns_cache_status_check
            CHECK (cache_status IN
              ('REQUIRED','CREATING','REUSABLE','UNAVAILABLE','NOT_REQUIRED'));

        CREATE OR REPLACE FUNCTION protect_ai_campaign_batch_provenance() RETURNS trigger AS $$
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
          IF OLD.provider_input_file_id IS NOT NULL AND
             (NEW.provider_input_file_id IS DISTINCT FROM OLD.provider_input_file_id OR
              NEW.provider_input_file_sha256 IS DISTINCT FROM OLD.provider_input_file_sha256) THEN
            RAISE EXCEPTION 'campaign child input file provenance is immutable';
          END IF;
          IF OLD.provider_output_file_id IS NOT NULL AND
             NEW.provider_output_file_id IS DISTINCT FROM OLD.provider_output_file_id THEN
            RAISE EXCEPTION 'campaign child output file identity is immutable';
          END IF;
          IF OLD.provider_output_file_sha256 IS NOT NULL AND
             NEW.provider_output_file_sha256 IS DISTINCT FROM OLD.provider_output_file_sha256 THEN
            RAISE EXCEPTION 'campaign child output file hash is immutable';
          END IF;
          IF OLD.provider_error_file_id IS NOT NULL AND
             NEW.provider_error_file_id IS DISTINCT FROM OLD.provider_error_file_id THEN
            RAISE EXCEPTION 'campaign child error file identity is immutable';
          END IF;
          IF OLD.provider_error_file_sha256 IS NOT NULL AND
             NEW.provider_error_file_sha256 IS DISTINCT FROM OLD.provider_error_file_sha256 THEN
            RAISE EXCEPTION 'campaign child error file hash is immutable';
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
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    used = int(
        bind.execute(
            text("SELECT count(*) FROM ai_annotation_campaigns "
                 "WHERE configuration_id='hostility-luna-production-v1'")
        ).scalar_one()
    )
    batches = int(
        bind.execute(
            text("SELECT count(*) FROM ai_annotation_campaign_batches "
                 "WHERE provider_input_file_id IS NOT NULL OR "
                 "provider_input_file_sha256 IS NOT NULL OR "
                 "provider_output_file_id IS NOT NULL OR provider_error_file_id IS NOT NULL OR "
                 "provider_output_file_sha256 IS NOT NULL OR provider_error_file_sha256 IS NOT NULL")
        ).scalar_one()
    )
    recoveries = int(
        bind.execute(
            text("SELECT count(*) FROM ai_annotation_campaign_recoveries "
                 "WHERE provider_input_file_id IS NOT NULL OR "
                 "provider_input_file_sha256 IS NOT NULL OR "
                 "provider_output_file_id IS NOT NULL OR provider_error_file_id IS NOT NULL OR "
                 "provider_output_file_sha256 IS NOT NULL OR provider_error_file_sha256 IS NOT NULL")
        ).scalar_one()
    )
    if used or batches or recoveries:
        raise RuntimeError("Luna production campaigns or OpenAI Batch file provenance exist")
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_campaign_configuration_shape_check,
          DROP CONSTRAINT ai_campaign_configuration_id_check;
        ALTER TABLE ai_annotation_campaigns
          ADD CONSTRAINT ai_campaign_configuration_id_check CHECK
            (configuration_id IN
              ('policy-v2','hostility-v2','hostility-luna-confirmation-100')),
          ADD CONSTRAINT ai_campaign_configuration_shape_check CHECK (
            (configuration_id='policy-v2' AND prediction_task='policy'
              AND annotation_schema_slug='australian_policy_annotation'
              AND annotation_schema_version='0.2.0'
              AND record_set_id IS NULL AND approved_record_set_sha256 IS NULL)
            OR
            (configuration_id='hostility-v2' AND prediction_task='hostility'
              AND annotation_schema_slug='australian_partisan_hostility'
              AND annotation_schema_version='0.2.0'
              AND record_set_id='hostility-heldout-100'
              AND approved_record_set_sha256=
                'f3d9451b3352025b769d263fc896f57683a741ea6fd9af0b6477651707cf3b36')
            OR
            (configuration_id='hostility-luna-confirmation-100'
              AND prediction_task='hostility'
              AND annotation_schema_slug='australian_partisan_hostility'
              AND annotation_schema_version='0.2.0'
              AND record_set_id='hostility-luna-confirmation-100'
              AND approved_record_set_sha256=
                '482d2951253840c5a294be028f6a61891693b6826ac3bdbebb087e71aa5c3c48')
          );
        CREATE OR REPLACE FUNCTION protect_ai_campaign_batch_provenance() RETURNS trigger AS $$
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
        ALTER TABLE ai_annotation_campaign_batches
          DROP COLUMN provider_error_file_id,
          DROP COLUMN provider_error_file_sha256,
          DROP COLUMN provider_output_file_id,
          DROP COLUMN provider_output_file_sha256,
          DROP COLUMN provider_input_file_sha256,
          DROP COLUMN provider_input_file_id,
          DROP COLUMN retry_count,
          DROP COLUMN evidence_failure_count;
        DROP TRIGGER trg_ai_campaign_recovery_openai_provenance
          ON ai_annotation_campaign_recoveries;
        DROP FUNCTION protect_ai_campaign_recovery_openai_provenance();
        ALTER TABLE ai_annotation_campaign_recoveries
          DROP COLUMN provider_error_file_id,
          DROP COLUMN provider_error_file_sha256,
          DROP COLUMN provider_output_file_id,
          DROP COLUMN provider_output_file_sha256,
          DROP COLUMN provider_input_file_sha256,
          DROP COLUMN provider_input_file_id;
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_annotation_campaigns_cache_status_check,
          ADD CONSTRAINT ai_annotation_campaigns_cache_status_check
            CHECK (cache_status IN ('REQUIRED','CREATING','REUSABLE','UNAVAILABLE'));
        """
    )
