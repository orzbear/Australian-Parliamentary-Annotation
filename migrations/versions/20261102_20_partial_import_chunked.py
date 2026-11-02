"""Allow chunked campaigns to use partial-import state without a parent job ID.

Revision ID: 20261102_20
Revises: 20261102_19
Create Date: 2026-11-02
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261102_20"
down_revision = "20261102_19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_annotation_campaigns_partial_import_check,
          ADD CONSTRAINT ai_annotation_campaigns_partial_import_check CHECK (
            status<>'PARTIALLY_IMPORTED' OR
            (record_set_sha256 IS NOT NULL AND planned_count>0
             AND (orchestration_mode='CHUNKED' OR provider_job_id IS NOT NULL)
             AND imported_at IS NOT NULL AND model_prediction_run_id IS NOT NULL
             AND terminal_invalid_count>0)
          );
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    chunked_partials_without_parent_job = int(
        bind.execute(
            text(
                "SELECT count(*) FROM ai_annotation_campaigns "
                "WHERE status='PARTIALLY_IMPORTED' "
                "AND orchestration_mode='CHUNKED' AND provider_job_id IS NULL"
            )
        ).scalar_one()
    )
    if chunked_partials_without_parent_job:
        raise RuntimeError(
            "chunked partial imports without a parent provider job exist; downgrade refused"
        )
    op.execute(
        """
        ALTER TABLE ai_annotation_campaigns
          DROP CONSTRAINT ai_annotation_campaigns_partial_import_check,
          ADD CONSTRAINT ai_annotation_campaigns_partial_import_check CHECK (
            status<>'PARTIALLY_IMPORTED' OR
            (record_set_sha256 IS NOT NULL AND planned_count>0
             AND provider_job_id IS NOT NULL AND imported_at IS NOT NULL
             AND model_prediction_run_id IS NOT NULL AND terminal_invalid_count>0)
          );
        """
    )
