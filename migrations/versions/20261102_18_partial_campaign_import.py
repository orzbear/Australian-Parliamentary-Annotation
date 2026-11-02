"""Track campaigns with unresolved records after importing valid results.

Revision ID: 20261102_18
Revises: 20261030_17
Create Date: 2026-11-02
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261102_18"
down_revision = "20261030_17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_annotation_campaigns_status_check,
          ADD CONSTRAINT ai_annotation_campaigns_status_check CHECK (status IN
            ('DRAFT','PLANNED','READY','SUBMITTED','RUNNING','PROVIDER_COMPLETED',
             'COLLECTED','VALIDATED','PARTIALLY_IMPORTED','IMPORTED','FAILED','CANCELLED'));
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ck_ai_campaign_provider_job_or_chunked;
        ALTER TABLE ai_annotation_campaigns ADD CONSTRAINT
          ck_ai_campaign_provider_job_or_chunked CHECK (
            orchestration_mode='CHUNKED' OR
            status NOT IN ('SUBMITTED','RUNNING','PROVIDER_COMPLETED','COLLECTED',
                           'VALIDATED','PARTIALLY_IMPORTED','IMPORTED')
            OR provider_job_id IS NOT NULL
          );
        ALTER TABLE ai_annotation_campaigns
          ADD CONSTRAINT ai_annotation_campaigns_partial_import_check CHECK (
            status<>'PARTIALLY_IMPORTED' OR
            (record_set_sha256 IS NOT NULL AND planned_count>0 AND provider_job_id IS NOT NULL
             AND imported_at IS NOT NULL AND model_prediction_run_id IS NOT NULL
             AND terminal_invalid_count>0)
          );
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    partial_campaigns = int(
        bind.execute(
            text("SELECT count(*) FROM ai_annotation_campaigns WHERE status='PARTIALLY_IMPORTED'")
        ).scalar_one()
    )
    if partial_campaigns:
        raise RuntimeError("partial campaign imports exist; downgrade refused")
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_annotation_campaigns_partial_import_check,
          DROP CONSTRAINT ai_annotation_campaigns_status_check,
          DROP CONSTRAINT ck_ai_campaign_provider_job_or_chunked;
        ALTER TABLE ai_annotation_campaigns
          ADD CONSTRAINT ai_annotation_campaigns_status_check CHECK (status IN
            ('DRAFT','PLANNED','READY','SUBMITTED','RUNNING','PROVIDER_COMPLETED',
             'COLLECTED','VALIDATED','IMPORTED','FAILED','CANCELLED'));
        ALTER TABLE ai_annotation_campaigns ADD CONSTRAINT
          ck_ai_campaign_provider_job_or_chunked CHECK (
            orchestration_mode='CHUNKED' OR
            status NOT IN ('SUBMITTED','RUNNING','PROVIDER_COMPLETED','COLLECTED',
                           'VALIDATED','IMPORTED') OR provider_job_id IS NOT NULL
          );
        """
    )
