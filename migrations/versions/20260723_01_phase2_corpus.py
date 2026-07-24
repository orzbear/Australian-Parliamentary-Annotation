"""Create the immutable Phase 2 corpus schema.

Revision ID: 20260723_01
Revises:
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op

from hansard_annotator.db.models import Base

revision = "20260723_01"
down_revision = None
branch_labels = None
depends_on = None


IMMUTABLE_TABLES = (
    "source_files",
    "source_file_versions",
    "debate_sections",
    "debate_events",
    "speech_fragments",
    "speaker_turns",
    "speaker_turn_fragments",
    "interjections",
    "continuation_anomalies",
    "image_anomalies",
    "import_artifacts",
)
def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)

    op.execute(
        """
        CREATE FUNCTION reject_immutable_corpus_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'accepted corpus table % is immutable', TG_TABLE_NAME
            USING ERRCODE = '55000';
        END
        $$;
        """
    )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_corpus_mutation()
            """
        )

    op.execute(
        """
        CREATE FUNCTION reject_accepted_run_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status = 'accepted' THEN
            RAISE EXCEPTION 'accepted run table % is immutable', TG_TABLE_NAME
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER trg_preprocessing_runs_immutable
          BEFORE UPDATE OR DELETE ON preprocessing_runs
          FOR EACH ROW EXECUTE FUNCTION reject_accepted_run_mutation();
        CREATE TRIGGER trg_reconstruction_runs_immutable
          BEFORE UPDATE OR DELETE ON reconstruction_runs
          FOR EACH ROW EXECUTE FUNCTION reject_accepted_run_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_section_parent()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_source bigint; parent_level text;
        BEGIN
          IF NEW.section_level = 'major' AND NEW.parent_section_id IS NOT NULL THEN
            RAISE EXCEPTION 'major section cannot have a parent' USING ERRCODE = '23514';
          END IF;
          IF NEW.section_level = 'minor' THEN
            IF NEW.parent_section_id IS NULL THEN
              RAISE EXCEPTION 'minor section requires a parent' USING ERRCODE = '23514';
            END IF;
            SELECT source_file_version_id, section_level INTO parent_source, parent_level
              FROM debate_sections WHERE id = NEW.parent_section_id;
            IF parent_source IS DISTINCT FROM NEW.source_file_version_id
               OR parent_level IS DISTINCT FROM 'major' THEN
              RAISE EXCEPTION 'minor section parent is incompatible' USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE CONSTRAINT TRIGGER trg_section_parent_compatible
          AFTER INSERT ON debate_sections
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_section_parent();
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_turn_lineage()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE turn_source bigint; turn_pre bigint; frag_source bigint; frag_pre bigint;
                frag_interjection boolean; frag_sequence integer;
        BEGIN
          SELECT t.source_file_version_id, r.preprocessing_run_id
            INTO turn_source, turn_pre
            FROM speaker_turns t JOIN reconstruction_runs r ON r.id=t.reconstruction_run_id
            WHERE t.id=NEW.turn_id;
          SELECT source_file_version_id, preprocessing_run_id, is_interjection, sequence_number
            INTO frag_source, frag_pre, frag_interjection, frag_sequence
            FROM speech_fragments WHERE id=NEW.fragment_id;
          IF turn_source IS DISTINCT FROM frag_source OR turn_pre IS DISTINCT FROM frag_pre
             OR frag_sequence IS DISTINCT FROM NEW.fragment_sequence THEN
            RAISE EXCEPTION 'turn/fragment lineage is incompatible' USING ERRCODE = '23514';
          END IF;
          IF frag_interjection THEN
            RAISE EXCEPTION 'interjection fragment cannot have turn lineage' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE CONSTRAINT TRIGGER trg_lineage_compatible
          AFTER INSERT ON speaker_turn_fragments
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_turn_lineage();
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_interjection()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE frag_source bigint; frag_pre bigint; frag_sequence integer;
                frag_flag boolean; recon_pre bigint; interrupted_recon bigint;
        BEGIN
          SELECT source_file_version_id, preprocessing_run_id, sequence_number, is_interjection
            INTO frag_source, frag_pre, frag_sequence, frag_flag
            FROM speech_fragments WHERE id=NEW.fragment_id;
          SELECT preprocessing_run_id INTO recon_pre
            FROM reconstruction_runs WHERE id=NEW.reconstruction_run_id;
          IF frag_source IS DISTINCT FROM NEW.source_file_version_id
             OR frag_pre IS DISTINCT FROM recon_pre
             OR frag_sequence IS DISTINCT FROM NEW.sequence_number
             OR frag_flag IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'interjection source/reconstruction is incompatible'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (SELECT 1 FROM speaker_turn_fragments WHERE fragment_id=NEW.fragment_id) THEN
            RAISE EXCEPTION 'interjection fragment already has turn lineage'
              USING ERRCODE = '23514';
          END IF;
          IF NEW.interrupted_turn_id IS NOT NULL THEN
            SELECT reconstruction_run_id INTO interrupted_recon
              FROM speaker_turns WHERE id=NEW.interrupted_turn_id;
            IF interrupted_recon IS DISTINCT FROM NEW.reconstruction_run_id THEN
              RAISE EXCEPTION 'interrupted turn belongs to another reconstruction'
                USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE CONSTRAINT TRIGGER trg_interjection_compatible
          AFTER INSERT ON interjections
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_interjection();
        """
    )
    op.execute(
        """
        CREATE VIEW annotation_ready_turns AS
        SELECT t.turn_key, t.speech_date, t.chamber, t.speaker_id_raw,
               t.speaker_name_raw, t.major_heading_original, t.minor_heading_original,
               t.business_type, t.text_clean, t.calculated_word_count,
               t.is_presiding_officer, t.is_collective_speaker, t.is_procedural,
               t.is_ceremonial, t.is_division_related, t.is_question_time,
               t.is_question, t.is_answer, t.is_orphan_continuation,
               t.eligible_50_words, t.eligible_100_words, t.eligible_main_analysis,
               t.interruption_count, sf.relative_path AS source_file,
               first_fragment.source_url, pr.run_name, rr.reconstruction_version
        FROM speaker_turns t
        JOIN source_file_versions sfv ON sfv.id=t.source_file_version_id
        JOIN source_files sf ON sf.id=sfv.source_file_id
        JOIN reconstruction_runs rr ON rr.id=t.reconstruction_run_id
        JOIN preprocessing_runs pr ON pr.id=rr.preprocessing_run_id
        LEFT JOIN speaker_turn_fragments first_lineage
          ON first_lineage.turn_id=t.id AND first_lineage.ordinal=1
        LEFT JOIN speech_fragments first_fragment
          ON first_fragment.id=first_lineage.fragment_id;

        CREATE VIEW turns_with_fragment_counts AS
        SELECT t.id, t.turn_key, t.fragment_count AS stored_fragment_count,
               count(stf.fragment_id)::integer AS lineage_fragment_count,
               t.fragment_count = count(stf.fragment_id) AS count_matches
        FROM speaker_turns t
        LEFT JOIN speaker_turn_fragments stf ON stf.turn_id=t.id
        GROUP BY t.id;

        CREATE VIEW corpus_year_summary AS
        WITH years AS (
          SELECT DISTINCT EXTRACT(YEAR FROM speech_date)::integer AS year
          FROM source_files
        )
        SELECT y.year,
          (SELECT count(*) FROM speech_fragments f
            WHERE EXTRACT(YEAR FROM f.speech_date)::integer=y.year) AS fragments,
          (SELECT count(*) FROM speaker_turns t
            WHERE EXTRACT(YEAR FROM t.speech_date)::integer=y.year) AS turns,
          (SELECT count(*) FROM interjections i
            WHERE EXTRACT(YEAR FROM i.speech_date)::integer=y.year) AS interjections,
          (SELECT count(*) FROM speaker_turns t
            WHERE EXTRACT(YEAR FROM t.speech_date)::integer=y.year
              AND t.is_orphan_continuation) AS orphan_continuations,
          (SELECT count(*) FROM speaker_turns t
            WHERE EXTRACT(YEAR FROM t.speech_date)::integer=y.year
              AND t.eligible_50_words) AS eligible_50_word_turns,
          (SELECT count(*) FROM speaker_turns t
            WHERE EXTRACT(YEAR FROM t.speech_date)::integer=y.year
              AND t.eligible_100_words) AS eligible_100_word_turns
        FROM years y;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW IF EXISTS corpus_year_summary;
        DROP VIEW IF EXISTS turns_with_fragment_counts;
        DROP VIEW IF EXISTS annotation_ready_turns;
        """
    )
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
    op.execute(
        """
        DROP FUNCTION IF EXISTS validate_interjection();
        DROP FUNCTION IF EXISTS validate_turn_lineage();
        DROP FUNCTION IF EXISTS validate_section_parent();
        DROP FUNCTION IF EXISTS reject_accepted_run_mutation();
        DROP FUNCTION IF EXISTS reject_immutable_corpus_mutation();
        """
    )
