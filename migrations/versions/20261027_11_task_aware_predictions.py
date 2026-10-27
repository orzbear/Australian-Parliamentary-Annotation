"""Make immutable model predictions task and schema aware.

Revision ID: 20261027_11
Revises: 20261020_10
Create Date: 2026-10-27
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "20261027_11"
down_revision = "20261020_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_prediction_runs DISABLE TRIGGER trg_model_prediction_runs_append_only;
        ALTER TABLE model_predictions DISABLE TRIGGER trg_model_predictions_append_only;

        ALTER TABLE model_prediction_runs
          ADD COLUMN prediction_task text,
          ADD COLUMN annotation_schema_slug text,
          ADD COLUMN annotation_schema_version text;
        ALTER TABLE model_predictions
          ADD COLUMN prediction_task text,
          ADD COLUMN prediction_values jsonb;

        UPDATE model_prediction_runs SET
          prediction_task='policy',
          annotation_schema_slug='australian_policy_annotation',
          annotation_schema_version='0.2.0';
        UPDATE model_predictions SET
          prediction_task='policy',
          prediction_values=jsonb_build_object(
            'is_non_policy',is_non_policy,
            'primary_australian_domain',primary_australian_domain,
            'secondary_australian_domains',to_jsonb(secondary_australian_domains)
          );

        ALTER TABLE model_prediction_runs
          ALTER COLUMN prediction_task SET NOT NULL,
          ALTER COLUMN prediction_task SET DEFAULT 'policy',
          ALTER COLUMN annotation_schema_slug SET NOT NULL,
          ALTER COLUMN annotation_schema_slug SET DEFAULT 'australian_policy_annotation',
          ALTER COLUMN annotation_schema_version SET NOT NULL,
          ALTER COLUMN annotation_schema_version SET DEFAULT '0.2.0',
          ADD CONSTRAINT model_prediction_runs_task_check
            CHECK (prediction_task IN ('policy','hostility'));
        ALTER TABLE model_predictions
          ALTER COLUMN prediction_task SET NOT NULL,
          ALTER COLUMN prediction_task SET DEFAULT 'policy',
          ALTER COLUMN prediction_values SET NOT NULL,
          ALTER COLUMN prediction_values SET DEFAULT '{}'::jsonb,
          ALTER COLUMN is_non_policy DROP NOT NULL,
          ALTER COLUMN secondary_australian_domains DROP NOT NULL,
          ALTER COLUMN secondary_australian_domains DROP DEFAULT;

        ALTER TABLE model_predictions DROP CONSTRAINT model_predictions_check;
        ALTER TABLE model_predictions
          DROP CONSTRAINT model_predictions_secondary_australian_domains_check;
        ALTER TABLE model_predictions ADD CONSTRAINT model_predictions_task_check
          CHECK (prediction_task IN ('policy','hostility'));
        ALTER TABLE model_predictions ADD CONSTRAINT model_predictions_values_object_check
          CHECK (jsonb_typeof(prediction_values)='object');
        ALTER TABLE model_predictions ADD CONSTRAINT model_predictions_policy_shape_check
          CHECK (
            (prediction_task='policy' AND is_non_policy IS NOT NULL
              AND secondary_australian_domains IS NOT NULL
              AND cardinality(secondary_australian_domains)<=2
              AND ((is_non_policy AND primary_australian_domain IS NULL
                    AND cardinality(secondary_australian_domains)=0)
                OR (NOT is_non_policy AND primary_australian_domain IS NOT NULL)))
            OR
            (prediction_task='hostility' AND is_non_policy IS NULL
              AND primary_australian_domain IS NULL
              AND secondary_australian_domains IS NULL)
          );
        ALTER TABLE model_prediction_runs ADD CONSTRAINT model_prediction_runs_id_task_unique
          UNIQUE (id,prediction_task);
        ALTER TABLE model_predictions ADD CONSTRAINT model_predictions_run_task_fk
          FOREIGN KEY (run_id,prediction_task)
          REFERENCES model_prediction_runs(id,prediction_task) ON DELETE RESTRICT;

        ALTER TABLE model_prediction_runs ENABLE TRIGGER trg_model_prediction_runs_append_only;
        ALTER TABLE model_predictions ENABLE TRIGGER trg_model_predictions_append_only;
        CREATE INDEX ix_model_predictions_task_run
          ON model_predictions (prediction_task,run_id,id);
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    hostility = int(
        bind.execute(
            text("SELECT count(*) FROM model_predictions WHERE prediction_task='hostility'")
        ).scalar_one()
    )
    if hostility:
        raise RuntimeError("downgrade refused: hostility predictions exist")
    op.execute(
        """
        ALTER TABLE model_prediction_runs DISABLE TRIGGER trg_model_prediction_runs_append_only;
        ALTER TABLE model_predictions DISABLE TRIGGER trg_model_predictions_append_only;
        DROP INDEX ix_model_predictions_task_run;
        ALTER TABLE model_predictions
          DROP CONSTRAINT model_predictions_run_task_fk,
          DROP CONSTRAINT model_predictions_policy_shape_check,
          DROP CONSTRAINT model_predictions_values_object_check,
          DROP CONSTRAINT model_predictions_task_check,
          ALTER COLUMN is_non_policy SET NOT NULL,
          ALTER COLUMN secondary_australian_domains SET DEFAULT '{}',
          ALTER COLUMN secondary_australian_domains SET NOT NULL,
          DROP COLUMN prediction_values,
          DROP COLUMN prediction_task,
          ADD CHECK ((is_non_policy AND primary_australian_domain IS NULL
                      AND cardinality(secondary_australian_domains)=0)
                  OR (NOT is_non_policy AND primary_australian_domain IS NOT NULL)),
          ADD CHECK (cardinality(secondary_australian_domains)<=2);
        ALTER TABLE model_prediction_runs
          DROP CONSTRAINT model_prediction_runs_id_task_unique,
          DROP CONSTRAINT model_prediction_runs_task_check,
          DROP COLUMN annotation_schema_version,
          DROP COLUMN annotation_schema_slug,
          DROP COLUMN prediction_task;
        ALTER TABLE model_prediction_runs ENABLE TRIGGER trg_model_prediction_runs_append_only;
        ALTER TABLE model_predictions ENABLE TRIGGER trg_model_predictions_append_only;
        """
    )
