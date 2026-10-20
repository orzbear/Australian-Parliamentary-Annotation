"""Persist terminal provider abstentions separately from model predictions.

Revision ID: 20261020_10
Revises: 20261018_09
Create Date: 2026-10-20
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261020_10"
down_revision = "20261018_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE model_prediction_abstentions (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          run_id bigint NOT NULL REFERENCES model_prediction_runs(id) ON DELETE RESTRICT,
          speaker_turn_id bigint NOT NULL REFERENCES speaker_turns(id) ON DELETE RESTRICT,
          record_id text NOT NULL CHECK (record_id LIKE 'turn:%'),
          resolution_type text NOT NULL DEFAULT 'PROVIDER_REFUSAL'
            CHECK (resolution_type='PROVIDER_REFUSAL'),
          resolution_reason text NOT NULL
            CHECK (resolution_reason='PROHIBITED_CONTENT'),
          refusal_provenance jsonb NOT NULL
            CHECK (jsonb_typeof(refusal_provenance)='object'),
          artifact_reference text NOT NULL CHECK (btrim(artifact_reference)<>''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (run_id,speaker_turn_id),
          UNIQUE (run_id,record_id)
        );
        CREATE INDEX ix_model_prediction_abstentions_turn
          ON model_prediction_abstentions (speaker_turn_id,run_id);
        CREATE TRIGGER trg_model_prediction_abstentions_append_only
          BEFORE UPDATE OR DELETE ON model_prediction_abstentions
          FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = int(
        bind.execute(text("SELECT count(*) FROM model_prediction_abstentions")).scalar_one()
    )
    if populated:
        raise RuntimeError(
            "provider-abstention downgrade refused: terminal provenance exists"
        )
    op.execute("DROP TABLE model_prediction_abstentions")
