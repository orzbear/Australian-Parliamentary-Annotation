"""Record the method used to resolve a terminal AI outcome manually.

Revision ID: 20261102_19
Revises: 20261102_18
Create Date: 2026-11-02
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261102_19"
down_revision = "20261102_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_prediction_outcome_reviews
          ADD COLUMN resolution_method text,
          ADD CONSTRAINT model_prediction_outcome_reviews_resolution_method_check
            CHECK (resolution_method IS NULL OR resolution_method IN
              ('CONFIRMED_FROZEN_CLASSIFICATION','INDEPENDENT_HUMAN_CLASSIFICATION'));
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    recorded_methods = int(
        bind.execute(
            text(
                "SELECT count(*) FROM model_prediction_outcome_reviews "
                "WHERE resolution_method IS NOT NULL"
            )
        ).scalar_one()
    )
    if recorded_methods:
        raise RuntimeError("manual resolution methods exist; downgrade refused")
    op.execute(
        """
        ALTER TABLE model_prediction_outcome_reviews
          DROP CONSTRAINT model_prediction_outcome_reviews_resolution_method_check,
          DROP COLUMN resolution_method;
        """
    )
