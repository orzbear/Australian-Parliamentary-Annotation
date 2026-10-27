"""Persist allowlisted AI campaign and frozen record-set identity.

Revision ID: 20261027_12
Revises: 20261027_11
Create Date: 2026-10-27
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261027_12"
down_revision = "20261027_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          ADD COLUMN configuration_id text,
          ADD COLUMN prediction_task text,
          ADD COLUMN annotation_schema_slug text,
          ADD COLUMN annotation_schema_version text,
          ADD COLUMN record_set_id text,
          ADD COLUMN approved_record_set_sha256 char(64);
        UPDATE ai_annotation_campaigns SET
          configuration_id='policy-v2',prediction_task='policy',
          annotation_schema_slug='australian_policy_annotation',
          annotation_schema_version='0.2.0';
        ALTER TABLE ai_annotation_campaigns
          ALTER COLUMN configuration_id SET NOT NULL,
          ALTER COLUMN configuration_id SET DEFAULT 'policy-v2',
          ALTER COLUMN prediction_task SET NOT NULL,
          ALTER COLUMN prediction_task SET DEFAULT 'policy',
          ALTER COLUMN annotation_schema_slug SET NOT NULL,
          ALTER COLUMN annotation_schema_slug SET DEFAULT 'australian_policy_annotation',
          ALTER COLUMN annotation_schema_version SET NOT NULL,
          ALTER COLUMN annotation_schema_version SET DEFAULT '0.2.0',
          ADD CONSTRAINT ai_campaign_configuration_id_check
            CHECK (configuration_id IN ('policy-v2','hostility-v2')),
          ADD CONSTRAINT ai_campaign_prediction_task_check
            CHECK (prediction_task IN ('policy','hostility')),
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
          );

        CREATE OR REPLACE FUNCTION protect_ai_campaign_provenance() RETURNS trigger AS $$
        BEGIN
          IF OLD.status NOT IN ('DRAFT','PLANNED','READY') AND (
            NEW.configuration_id IS DISTINCT FROM OLD.configuration_id OR
            NEW.prediction_task IS DISTINCT FROM OLD.prediction_task OR
            NEW.annotation_schema_slug IS DISTINCT FROM OLD.annotation_schema_slug OR
            NEW.annotation_schema_version IS DISTINCT FROM OLD.annotation_schema_version OR
            NEW.record_set_id IS DISTINCT FROM OLD.record_set_id OR
            NEW.approved_record_set_sha256 IS DISTINCT FROM OLD.approved_record_set_sha256 OR
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
          ) THEN RAISE EXCEPTION 'submitted campaign provenance is immutable'; END IF;
          IF OLD.provider_job_id IS NOT NULL
             AND NEW.provider_job_id IS DISTINCT FROM OLD.provider_job_id THEN
            RAISE EXCEPTION 'campaign provider job identity is immutable';
          END IF;
          IF OLD.model_prediction_run_id IS NOT NULL
             AND NEW.model_prediction_run_id IS DISTINCT FROM OLD.model_prediction_run_id THEN
            RAISE EXCEPTION 'campaign prediction-run link is immutable';
          END IF;
          NEW.updated_at := now(); RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    hostility = int(
        bind.execute(
            text("SELECT count(*) FROM ai_annotation_campaigns WHERE configuration_id='hostility-v2'")
        ).scalar_one()
    )
    if hostility:
        raise RuntimeError("downgrade refused: hostility campaigns exist")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_ai_campaign_provenance() RETURNS trigger AS $$
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
          ) THEN RAISE EXCEPTION 'submitted campaign provenance is immutable'; END IF;
          IF OLD.provider_job_id IS NOT NULL
             AND NEW.provider_job_id IS DISTINCT FROM OLD.provider_job_id THEN
            RAISE EXCEPTION 'campaign provider job identity is immutable';
          END IF;
          IF OLD.model_prediction_run_id IS NOT NULL
             AND NEW.model_prediction_run_id IS DISTINCT FROM OLD.model_prediction_run_id THEN
            RAISE EXCEPTION 'campaign prediction-run link is immutable';
          END IF;
          NEW.updated_at := now(); RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_campaign_configuration_shape_check,
          DROP CONSTRAINT ai_campaign_prediction_task_check,
          DROP CONSTRAINT ai_campaign_configuration_id_check,
          DROP COLUMN approved_record_set_sha256,
          DROP COLUMN record_set_id,
          DROP COLUMN annotation_schema_version,
          DROP COLUMN annotation_schema_slug,
          DROP COLUMN prediction_task,
          DROP COLUMN configuration_id;
        """
    )
