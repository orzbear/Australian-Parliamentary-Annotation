"""Allow immutable non-refusal terminal outcomes for prediction runs.

Revision ID: 20261027_13
Revises: 20261027_12
Create Date: 2026-10-27
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261027_13"
down_revision = "20261027_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_prediction_abstentions
          RENAME COLUMN refusal_provenance TO outcome_provenance;
        ALTER TABLE model_prediction_abstentions
          DROP CONSTRAINT model_prediction_abstentions_resolution_type_check,
          DROP CONSTRAINT model_prediction_abstentions_resolution_reason_check;
        ALTER TABLE model_prediction_abstentions
          ADD CONSTRAINT model_prediction_abstentions_resolution_type_check
            CHECK (resolution_type IN ('PROVIDER_REFUSAL','INVALID_OUTPUT')),
          ADD CONSTRAINT model_prediction_abstentions_resolution_reason_check
            CHECK (resolution_reason IN ('PROHIBITED_CONTENT','EVIDENCE_RESOLUTION_FAILED')),
          ADD CONSTRAINT model_prediction_abstentions_resolution_pair_check CHECK (
            (resolution_type='PROVIDER_REFUSAL' AND resolution_reason='PROHIBITED_CONTENT') OR
            (resolution_type='INVALID_OUTPUT' AND resolution_reason='EVIDENCE_RESOLUTION_FAILED')
          );
        ALTER TABLE ai_annotation_campaigns
          ADD COLUMN terminal_invalid_count integer NOT NULL DEFAULT 0
            CHECK (terminal_invalid_count >= 0);
        CREATE TABLE model_prediction_outcome_reviews (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          abstention_id bigint NOT NULL REFERENCES model_prediction_abstentions(id) ON DELETE RESTRICT,
          speaker_turn_id bigint NOT NULL REFERENCES speaker_turns(id) ON DELETE RESTRICT,
          reviewer_user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          assignment_id bigint REFERENCES assignments(id) ON DELETE RESTRICT,
          annotation_version_id bigint REFERENCES annotation_versions(id) ON DELETE RESTRICT,
          review_status text NOT NULL CHECK (review_status IN
            ('human_annotated_after_ai_failure','skipped')),
          note text,
          reviewed_at timestamptz NOT NULL DEFAULT now(),
          CHECK (review_status <> 'human_annotated_after_ai_failure' OR annotation_version_id IS NOT NULL)
        );
        CREATE INDEX ix_model_prediction_outcome_reviews_queue
          ON model_prediction_outcome_reviews (abstention_id,reviewed_at DESC,id DESC);
        CREATE TRIGGER trg_model_prediction_outcome_reviews_append_only
          BEFORE UPDATE OR DELETE ON model_prediction_outcome_reviews
          FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    count = int(bind.execute(text("SELECT count(*) FROM model_prediction_outcome_reviews")).scalar_one())
    invalid = int(bind.execute(text(
        "SELECT count(*) FROM model_prediction_abstentions WHERE resolution_type='INVALID_OUTPUT'"
    )).scalar_one())
    campaigns = int(bind.execute(text(
        "SELECT count(*) FROM ai_annotation_campaigns WHERE terminal_invalid_count>0"
    )).scalar_one())
    if count or invalid or campaigns:
        raise RuntimeError("terminal invalid outcomes or their campaign accounting exist")
    op.execute(
        """
        DROP TABLE model_prediction_outcome_reviews;
        ALTER TABLE ai_annotation_campaigns DROP COLUMN terminal_invalid_count;
        ALTER TABLE model_prediction_abstentions
          DROP CONSTRAINT model_prediction_abstentions_resolution_pair_check,
          DROP CONSTRAINT model_prediction_abstentions_resolution_type_check,
          DROP CONSTRAINT model_prediction_abstentions_resolution_reason_check;
        ALTER TABLE model_prediction_abstentions
          ADD CONSTRAINT model_prediction_abstentions_resolution_type_check
            CHECK (resolution_type='PROVIDER_REFUSAL'),
          ADD CONSTRAINT model_prediction_abstentions_resolution_reason_check
            CHECK (resolution_reason='PROHIBITED_CONTENT');
        """
    )
    op.alter_column(
        "model_prediction_abstentions",
        "outcome_provenance",
        new_column_name="refusal_provenance",
    )
