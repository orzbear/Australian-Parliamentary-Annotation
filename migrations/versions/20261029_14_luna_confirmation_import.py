"""Allow the exact frozen Luna confirmation review campaign and terminal statuses.

Revision ID: 20261029_14
Revises: 20261027_13
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261029_14"
down_revision = "20261027_13"
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

        ALTER TABLE model_prediction_abstentions
          DROP CONSTRAINT model_prediction_abstentions_resolution_reason_check,
          DROP CONSTRAINT model_prediction_abstentions_resolution_pair_check;
        ALTER TABLE model_prediction_abstentions
          ADD CONSTRAINT model_prediction_abstentions_resolution_reason_check
            CHECK (resolution_reason IN
              ('PROHIBITED_CONTENT','EVIDENCE_RESOLUTION_FAILED',
               'INVALID_STRUCTURED_OUTPUT','API_FAILURE')),
          ADD CONSTRAINT model_prediction_abstentions_resolution_pair_check CHECK (
            (resolution_type='PROVIDER_REFUSAL' AND resolution_reason='PROHIBITED_CONTENT') OR
            (resolution_type='INVALID_OUTPUT' AND resolution_reason IN
              ('EVIDENCE_RESOLUTION_FAILED','INVALID_STRUCTURED_OUTPUT','API_FAILURE'))
          );
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    campaigns = int(
        bind.execute(
            text("SELECT count(*) FROM ai_annotation_campaigns "
                 "WHERE configuration_id='hostility-luna-confirmation-100'")
        ).scalar_one()
    )
    outcomes = int(
        bind.execute(
            text("SELECT count(*) FROM model_prediction_abstentions "
                 "WHERE resolution_reason IN ('INVALID_STRUCTURED_OUTPUT','API_FAILURE')")
        ).scalar_one()
    )
    if campaigns or outcomes:
        raise RuntimeError("Luna confirmation campaigns or outcomes exist")
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_campaign_configuration_shape_check,
          DROP CONSTRAINT ai_campaign_configuration_id_check;
        ALTER TABLE ai_annotation_campaigns
          ADD CONSTRAINT ai_campaign_configuration_id_check
            CHECK (configuration_id IN ('policy-v2','hostility-v2')),
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
        ALTER TABLE model_prediction_abstentions
          DROP CONSTRAINT model_prediction_abstentions_resolution_pair_check,
          DROP CONSTRAINT model_prediction_abstentions_resolution_reason_check;
        ALTER TABLE model_prediction_abstentions
          ADD CONSTRAINT model_prediction_abstentions_resolution_reason_check
            CHECK (resolution_reason IN
              ('PROHIBITED_CONTENT','EVIDENCE_RESOLUTION_FAILED')),
          ADD CONSTRAINT model_prediction_abstentions_resolution_pair_check CHECK (
            (resolution_type='PROVIDER_REFUSAL' AND resolution_reason='PROHIBITED_CONTENT') OR
            (resolution_type='INVALID_OUTPUT' AND resolution_reason='EVIDENCE_RESOLUTION_FAILED')
          );
        """
    )
