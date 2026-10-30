"""Permit deletion of campaigns with local-only planning state.

Revision ID: 20261030_16
Revises: 20261029_15
Create Date: 2026-10-30
"""

from __future__ import annotations

from alembic import op

revision = "20261030_16"
down_revision = "20261029_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION ai_annotation_campaign_is_deletable(target_campaign_id bigint)
        RETURNS boolean LANGUAGE sql STABLE AS $$
          SELECT EXISTS (
            SELECT 1 FROM ai_annotation_campaigns campaign
            WHERE campaign.id=target_campaign_id
              AND campaign.status IN ('DRAFT','PLANNED','READY','CANCELLED')
              AND campaign.provider_job_id IS NULL
              AND campaign.provider_state IS NULL
              AND campaign.cache_status IN ('REQUIRED','NOT_REQUIRED')
              AND campaign.cache_identifier IS NULL
              AND campaign.cache_content_sha256 IS NULL
              AND campaign.cache_model IS NULL
              AND campaign.cache_display_name IS NULL
              AND campaign.cache_created_at IS NULL
              AND campaign.cache_expires_at IS NULL
              AND campaign.cache_verified_at IS NULL
              AND campaign.cache_metadata='{}'::jsonb
              AND campaign.artifact_reference IS NULL
              AND campaign.artifact_sha256 IS NULL
              AND campaign.submitted_at IS NULL
              AND campaign.provider_completed_at IS NULL
              AND campaign.collected_at IS NULL
              AND campaign.validated_at IS NULL
              AND campaign.imported_at IS NULL
              AND campaign.provider_success_count=0
              AND campaign.provider_failure_count=0
              AND campaign.valid_count=0
              AND campaign.invalid_count=0
              AND campaign.terminal_invalid_count=0
              AND campaign.model_prediction_run_id IS NULL
              AND campaign.authorized_by_user_id IS NULL
              AND campaign.authorized_at IS NULL
              AND campaign.authorized_spend_ceiling_usd IS NULL
              AND campaign.authorized_record_count IS NULL
              AND campaign.operational_metadata - 'phase4e'='{}'::jsonb
              AND (
                NOT (campaign.operational_metadata ? 'phase4e') OR
                (
                  jsonb_typeof(campaign.operational_metadata->'phase4e')='object'
                  AND campaign.operational_metadata->'phase4e' - ARRAY[
                    'chunk_count','estimated_full_input_tokens',
                    'safe_active_token_ceiling','estimate_method',
                    'pricing_effective_date'
                  ]::text[]='{}'::jsonb
                )
              )
              AND campaign.failure_metadata='{}'::jsonb
              AND campaign.orchestration_status IN ('INACTIVE','PLANNED')
              AND campaign.authorized_configuration IS NULL
              AND campaign.paused_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM ai_annotation_campaign_batches batch
                WHERE batch.campaign_id=campaign.id
                  AND (
                    batch.status<>'PENDING'
                    OR batch.provider_job_id IS NOT NULL
                    OR batch.provider_state IS NOT NULL
                    OR batch.provider_input_file_id IS NOT NULL
                    OR batch.provider_output_file_id IS NOT NULL
                    OR batch.provider_output_file_sha256 IS NOT NULL
                    OR batch.provider_error_file_id IS NOT NULL
                    OR batch.provider_error_file_sha256 IS NOT NULL
                    OR batch.cache_identifier IS NOT NULL
                    OR batch.cache_content_sha256 IS NOT NULL
                    OR batch.cache_model IS NOT NULL
                    OR batch.cache_display_name IS NOT NULL
                    OR batch.cache_created_at IS NOT NULL
                    OR batch.cache_expires_at IS NOT NULL
                    OR batch.cache_verified_at IS NOT NULL
                    OR batch.cache_metadata<>'{}'::jsonb
                    OR batch.provider_success_count<>0
                    OR batch.provider_failure_count<>0
                    OR batch.valid_count<>0
                    OR batch.repaired_count<>0
                    OR batch.invalid_count<>0
                    OR batch.missing_count<>0
                    OR batch.duplicate_count<>0
                    OR batch.evidence_failure_count<>0
                    OR batch.input_tokens<>0
                    OR batch.cached_input_tokens<>0
                    OR batch.output_tokens<>0
                    OR batch.reasoning_tokens<>0
                    OR batch.processing_cost_usd<>0
                    OR batch.cache_cost_usd<>0
                    OR batch.artifact_reference IS NOT NULL
                    OR batch.artifact_sha256 IS NOT NULL
                    OR batch.failure_metadata<>'{}'::jsonb
                    OR batch.retry_count<>0
                    OR batch.submission_started_at IS NOT NULL
                    OR batch.submitted_at IS NOT NULL
                    OR batch.provider_completed_at IS NOT NULL
                    OR batch.collected_at IS NOT NULL
                  )
              )
              AND NOT EXISTS (
                SELECT 1 FROM ai_annotation_campaign_recoveries recovery
                WHERE recovery.campaign_id=campaign.id
              )
              AND NOT EXISTS (
                SELECT 1 FROM audit_events event
                WHERE event.entity_public_id=campaign.public_id
              )
          )
        $$;

        DROP TRIGGER trg_ai_annotation_campaign_records_append_only
          ON ai_annotation_campaign_records;
        CREATE FUNCTION protect_ai_campaign_record_mutation() RETURNS trigger AS $$
        BEGIN
          IF TG_OP='DELETE'
             AND current_setting('hansard.allow_unused_campaign_delete',true)
                 = OLD.campaign_id::text
             AND ai_annotation_campaign_is_deletable(OLD.campaign_id) THEN
            RETURN OLD;
          END IF;
          RAISE EXCEPTION 'campaign record membership is append-only';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_ai_annotation_campaign_records_append_only
          BEFORE UPDATE OR DELETE ON ai_annotation_campaign_records
          FOR EACH ROW EXECUTE FUNCTION protect_ai_campaign_record_mutation();

        CREATE FUNCTION protect_ai_campaign_delete() RETURNS trigger AS $$
        BEGIN
          IF current_setting('hansard.allow_unused_campaign_delete',true)
               IS DISTINCT FROM OLD.id::text
             OR NOT ai_annotation_campaign_is_deletable(OLD.id)
             OR EXISTS (
               SELECT 1 FROM ai_annotation_campaign_records record
               WHERE record.campaign_id=OLD.id
             )
             OR EXISTS (
               SELECT 1 FROM ai_annotation_campaign_batches batch
               WHERE batch.campaign_id=OLD.id
             ) THEN
            RAISE EXCEPTION 'campaign has provider or research activity and cannot be deleted';
          END IF;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_ai_annotation_campaign_delete
          BEFORE DELETE ON ai_annotation_campaigns
          FOR EACH ROW EXECUTE FUNCTION protect_ai_campaign_delete();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER trg_ai_annotation_campaign_delete ON ai_annotation_campaigns;
        DROP FUNCTION protect_ai_campaign_delete();
        DROP TRIGGER trg_ai_annotation_campaign_records_append_only
          ON ai_annotation_campaign_records;
        DROP FUNCTION protect_ai_campaign_record_mutation();
        CREATE TRIGGER trg_ai_annotation_campaign_records_append_only
          BEFORE UPDATE OR DELETE ON ai_annotation_campaign_records
          FOR EACH ROW EXECUTE FUNCTION reject_phase3_append_only_mutation();
        DROP FUNCTION ai_annotation_campaign_is_deletable(bigint);
        """
    )
