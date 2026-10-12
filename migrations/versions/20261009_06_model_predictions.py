"""Add immutable model prediction runs, predictions, and human review events.

Revision ID: 20261009_06
Revises: 20260810_05
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261009_06"
down_revision = "20260810_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE model_prediction_runs (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          artifact_sha256 char(64) NOT NULL UNIQUE CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
          provider text NOT NULL, configured_model text NOT NULL, returned_model text,
          prompt_version text NOT NULL, prompt_sha256 char(64) NOT NULL,
          canonical_schema_sha256 char(64) NOT NULL,
          prediction_contract_sha256 char(64), provider_schema_sha256 char(64),
          taxonomy_provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
          generation_config jsonb NOT NULL DEFAULT '{}'::jsonb,
          execution_mode text NOT NULL, provider_job_id text, cache_identifier text,
          cache_content_sha256 char(64), pricing_format_version integer,
          pricing_effective_date date, record_set_sha256 char(64) NOT NULL,
          started_at timestamptz, submitted_at timestamptz, completed_at timestamptz,
          imported_at timestamptz NOT NULL DEFAULT now(),
          requested_count integer NOT NULL CHECK (requested_count >= 0),
          provider_success_count integer NOT NULL CHECK (provider_success_count >= 0),
          provider_failure_count integer NOT NULL CHECK (provider_failure_count >= 0),
          valid_count integer NOT NULL CHECK (valid_count >= 0),
          invalid_count integer NOT NULL CHECK (invalid_count >= 0),
          input_tokens bigint NOT NULL DEFAULT 0, cached_input_tokens bigint NOT NULL DEFAULT 0,
          output_tokens bigint NOT NULL DEFAULT 0, reasoning_tokens bigint NOT NULL DEFAULT 0,
          token_processing_cost_usd numeric(18,8), cache_storage_cost_usd numeric(18,8),
          total_cost_usd numeric(18,8), status text NOT NULL,
          notes text, metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          CHECK (jsonb_typeof(taxonomy_provenance)='object'),
          CHECK (jsonb_typeof(generation_config)='object'),
          CHECK (jsonb_typeof(metadata)='object')
        );
        CREATE INDEX ix_model_prediction_runs_configuration
          ON model_prediction_runs (provider,configured_model,prompt_version,imported_at DESC);

        CREATE TABLE model_predictions (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          run_id bigint NOT NULL REFERENCES model_prediction_runs(id) ON DELETE RESTRICT,
          speaker_turn_id bigint NOT NULL REFERENCES speaker_turns(id) ON DELETE RESTRICT,
          record_id text NOT NULL, is_non_policy boolean NOT NULL,
          primary_australian_domain text,
          secondary_australian_domains text[] NOT NULL DEFAULT '{}',
          reasoning text NOT NULL, validation_status text NOT NULL CHECK (validation_status='success'),
          raw_provider_response jsonb, artifact_reference text,
          provider_response_id text, input_tokens integer NOT NULL DEFAULT 0,
          cached_input_tokens integer NOT NULL DEFAULT 0, output_tokens integer NOT NULL DEFAULT 0,
          reasoning_tokens integer NOT NULL DEFAULT 0, request_index integer, response_index integer,
          returned_model text, created_at timestamptz NOT NULL DEFAULT now(),
          imported_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (run_id,speaker_turn_id), UNIQUE (run_id,record_id),
          CHECK ((is_non_policy AND primary_australian_domain IS NULL
                  AND cardinality(secondary_australian_domains)=0)
              OR (NOT is_non_policy AND primary_australian_domain IS NOT NULL)),
          CHECK (cardinality(secondary_australian_domains) <= 2)
        );
        CREATE INDEX ix_model_predictions_turn ON model_predictions (speaker_turn_id,run_id);
        CREATE INDEX ix_model_predictions_primary ON model_predictions (run_id,primary_australian_domain);

        CREATE TABLE model_prediction_reviews (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
          model_prediction_id bigint NOT NULL REFERENCES model_predictions(id) ON DELETE RESTRICT,
          speaker_turn_id bigint NOT NULL REFERENCES speaker_turns(id) ON DELETE RESTRICT,
          reviewer_user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          assignment_id bigint REFERENCES assignments(id) ON DELETE RESTRICT,
          annotation_version_id bigint REFERENCES annotation_versions(id) ON DELETE RESTRICT,
          review_status text NOT NULL CHECK (review_status IN
            ('accepted','edited','flagged_codebook','skipped')),
          note text, reviewed_at timestamptz NOT NULL DEFAULT now(),
          CHECK (review_status NOT IN ('accepted','edited') OR annotation_version_id IS NOT NULL)
        );
        CREATE INDEX ix_model_prediction_reviews_queue
          ON model_prediction_reviews (model_prediction_id,reviewed_at DESC,id DESC);
        CREATE INDEX ix_model_prediction_reviews_reviewer
          ON model_prediction_reviews (reviewer_user_id,review_status,reviewed_at DESC);

        CREATE TRIGGER trg_model_prediction_runs_append_only BEFORE UPDATE OR DELETE
          ON model_prediction_runs FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();
        CREATE TRIGGER trg_model_predictions_append_only BEFORE UPDATE OR DELETE
          ON model_predictions FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();
        CREATE TRIGGER trg_model_prediction_reviews_append_only BEFORE UPDATE OR DELETE
          ON model_prediction_reviews FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = sum(
        int(bind.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
        for table in ("model_prediction_runs", "model_predictions", "model_prediction_reviews")
    )
    if populated:
        raise RuntimeError("Phase 4C downgrade refused: model prediction or review data exists")
    op.execute(
        """
        DROP TABLE model_prediction_reviews;
        DROP TABLE model_predictions;
        DROP TABLE model_prediction_runs;
        """
    )
