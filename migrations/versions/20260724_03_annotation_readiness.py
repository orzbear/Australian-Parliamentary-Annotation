"""Make annotation-ready views explicitly preprocessing-version aware.

Revision ID: 20260724_03
Revises: 20260724_02
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op

revision = "20260724_03"
down_revision = "20260724_02"
branch_labels = None
depends_on = None

PHASE_25_VIEW = """
CREATE VIEW annotation_ready_turns AS
SELECT t.turn_key, t.speech_date, t.chamber, t.speaker_id_raw,
       t.speaker_name_raw, t.major_heading_original, t.minor_heading_original,
       t.business_type, t.text_clean, t.calculated_word_count,
       t.is_presiding_officer, t.is_collective_speaker, t.is_procedural,
       t.is_ceremonial, t.is_division_related, t.is_question_time,
       t.is_question, t.is_answer, t.is_orphan_continuation,
       t.eligible_50_words, t.eligible_100_words, t.eligible_main_analysis,
       t.interruption_count, sf.relative_path AS source_file,
       first_fragment.source_url, pr.run_name, rr.reconstruction_version,
       c.public_id AS corpus_public_id, c.slug AS corpus_slug,
       c.name AS corpus_name
FROM speaker_turns t
JOIN source_file_versions sfv ON sfv.id=t.source_file_version_id
JOIN source_files sf ON sf.id=sfv.source_file_id
JOIN reconstruction_runs rr ON rr.id=t.reconstruction_run_id
JOIN preprocessing_runs pr ON pr.id=rr.preprocessing_run_id
JOIN corpora c ON c.id=pr.corpus_id
LEFT JOIN speaker_turn_fragments first_lineage
  ON first_lineage.turn_id=t.id AND first_lineage.ordinal=1
LEFT JOIN speech_fragments first_fragment
  ON first_fragment.id=first_lineage.fragment_id
"""

VERSION_AWARE_VIEW = """
CREATE VIEW annotation_ready_turns AS
SELECT t.turn_key, t.speech_date, t.chamber, t.speaker_id_raw,
       t.speaker_name_raw, t.major_heading_original, t.minor_heading_original,
       t.business_type, t.text_clean, t.calculated_word_count,
       t.is_presiding_officer, t.is_collective_speaker, t.is_procedural,
       t.is_ceremonial, t.is_division_related, t.is_question_time,
       t.is_question, t.is_answer, t.is_orphan_continuation,
       t.eligible_50_words, t.eligible_100_words, t.eligible_main_analysis,
       t.interruption_count, sf.relative_path AS source_file,
       first_fragment.source_url, pr.run_name, rr.reconstruction_version,
       c.public_id AS corpus_public_id, c.slug AS corpus_slug,
       c.name AS corpus_name, pr.id AS preprocessing_run_id,
       pr.pipeline_version,
       (c.current_preprocessing_run_id = pr.id) AS is_current_corpus_version
FROM speaker_turns t
JOIN source_file_versions sfv ON sfv.id=t.source_file_version_id
JOIN source_files sf ON sf.id=sfv.source_file_id
JOIN reconstruction_runs rr ON rr.id=t.reconstruction_run_id
JOIN preprocessing_runs pr ON pr.id=rr.preprocessing_run_id
JOIN corpora c ON c.id=pr.corpus_id
LEFT JOIN speaker_turn_fragments first_lineage
  ON first_lineage.turn_id=t.id AND first_lineage.ordinal=1
LEFT JOIN speech_fragments first_fragment
  ON first_fragment.id=first_lineage.fragment_id
"""


def upgrade() -> None:
    op.execute("DROP VIEW annotation_ready_turns")
    op.execute(VERSION_AWARE_VIEW)
    op.execute(
        """
        CREATE VIEW current_annotation_ready_turns AS
        SELECT *
        FROM annotation_ready_turns
        WHERE is_current_corpus_version
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW current_annotation_ready_turns")
    op.execute("DROP VIEW annotation_ready_turns")
    op.execute(PHASE_25_VIEW)
