"""Allow the frozen Gemini hostility production campaign configuration.

Revision ID: 20261030_17
Revises: 20261030_16
Create Date: 2026-10-30
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261030_17"
down_revision = "20261030_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_campaign_configuration_shape_check,
          DROP CONSTRAINT ai_campaign_configuration_id_check;
        ALTER TABLE ai_annotation_campaigns
          ADD CONSTRAINT ai_campaign_configuration_id_check CHECK
            (configuration_id IN
              ('policy-v2','hostility-v2','hostility-luna-confirmation-100',
               'hostility-luna-production-v1',
               'hostility-gemini-25-flash-production')),
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
            OR
            (configuration_id='hostility-gemini-25-flash-production'
              AND prediction_task='hostility'
              AND annotation_schema_slug='australian_partisan_hostility'
              AND annotation_schema_version='0.2.0'
              AND record_set_id IS NULL AND approved_record_set_sha256 IS NULL)
          );
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    gemini_campaigns = int(
        bind.execute(
            text(
                "SELECT count(*) FROM ai_annotation_campaigns "
                "WHERE configuration_id='hostility-gemini-25-flash-production'"
            )
        ).scalar_one()
    )
    if gemini_campaigns:
        raise RuntimeError(
            "Gemini hostility production campaigns exist; refusing to remove their configuration"
        )
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_campaign_configuration_shape_check,
          DROP CONSTRAINT ai_campaign_configuration_id_check;
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
        """
    )
