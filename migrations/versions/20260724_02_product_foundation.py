"""Create the Phase 2.5 product metadata foundation.

Revision ID: 20260724_02
Revises: 20260723_01
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import BigInteger, Column, Table, text

from hansard_annotator.db.models import Base

revision = "20260724_02"
down_revision = "20260723_01"
branch_labels = None
depends_on = None

CORPUS_UUID = "385a0089-7e50-59c7-bfc6-02ada0e4a139"
CORPUS_SLUG = "australian-house-representatives-hansard"
ACCEPTED_RUN_NAME = "phase1-full-20260723-v4"
ACCEPTED_MANIFEST_SHA256 = (
    "1d7698829d9d1cdceb0373c5d2fc55a719f2a43e6d5242e38bd99067215680df"
)
ACCEPTED_INVENTORY_SHA256 = (
    "7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2"
)
PRODUCT_TABLE_NAMES = (
    "corpora",
    "taxonomies",
    "taxonomy_versions",
    "taxonomy_labels",
    "taxonomy_label_relations",
    "annotation_schemas",
    "annotation_schema_versions",
    "annotation_field_definitions",
)
_PRODUCT_TABLES: dict[str, Table] = {}


def _detach_loaded_product_tables() -> None:
    """Keep the accepted revision's live Base metadata limited to Phase 2."""
    for name in PRODUCT_TABLE_NAMES:
        table = Base.metadata.tables.get(name)
        if table is not None:
            _PRODUCT_TABLES[name] = table
            Base.metadata.remove(table)


def _product_tables() -> dict[str, Table]:
    if len(_PRODUCT_TABLES) != len(PRODUCT_TABLE_NAMES):
        # Import only while revision 02 is executing. Revision 01 therefore sees the
        # same metadata it saw when Phase 2 was accepted.
        from hansard_annotator.product.db_models import PRODUCT_TABLES

        _PRODUCT_TABLES.update(PRODUCT_TABLES)
        _detach_loaded_product_tables()
    if len(_PRODUCT_TABLES) != len(PRODUCT_TABLE_NAMES):
        raise RuntimeError("Phase 2.5 product table metadata is incomplete")
    return _PRODUCT_TABLES


_detach_loaded_product_tables()


def _create_product_tables() -> None:
    bind = op.get_bind()
    tables = _product_tables()
    for name in PRODUCT_TABLE_NAMES:
        tables[name].create(bind=bind, checkfirst=False)


def upgrade() -> None:
    _create_product_tables()
    bind = op.get_bind()
    op.add_column("preprocessing_runs", Column("corpus_id", BigInteger(), nullable=True))
    op.add_column("source_files", Column("corpus_id", BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_preprocessing_runs_corpus",
        "preprocessing_runs",
        "corpora",
        ["corpus_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_source_files_corpus",
        "source_files",
        "corpora",
        ["corpus_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    bind.execute(
        text(
            """
            INSERT INTO corpora
              (public_id,slug,name,description,jurisdiction,legislature,chamber,
               language_code,source_format_key,source_format_version,
               source_description,licence_status,licence_note,
               redistribution_status,date_from,date_to,is_public,is_active)
            SELECT
              CAST(:public_id AS uuid), :slug,
              'Australian House of Representatives Hansard',
              'Australian House of Representatives parliamentary proceedings, '
                'registered as the first supported corpus.',
              'Australia','Parliament of Australia','House of Representatives','en',
              'openaustralia_publicwhip_xml','1',
              'OpenAustralia/PublicWhip-style normalised XML derived from Australian '
                'Parliament ParlInfo',
              'pending_review',
              'Source and processed-corpus licensing require formal review.',
              'pending_review',
              COALESCE((SELECT min(speech_date) FROM source_files), DATE '2010-01-01'),
              COALESCE((SELECT max(speech_date) FROM source_files), DATE '2010-01-01'),
              false,true
            WHERE NOT EXISTS (SELECT 1 FROM corpora WHERE slug=:slug)
            """
        ),
        {"public_id": CORPUS_UUID, "slug": CORPUS_SLUG},
    )
    corpus_id = bind.execute(
        text("SELECT id FROM corpora WHERE slug=:slug"), {"slug": CORPUS_SLUG}
    ).scalar_one()

    # Accepted runs are row-immutable. Alembic temporarily removes only that row trigger
    # for the controlled corpus-link backfill, then restores it before completing.
    op.execute("DROP TRIGGER trg_preprocessing_runs_immutable ON preprocessing_runs")
    bind.execute(
        text("UPDATE preprocessing_runs SET corpus_id=:corpus_id WHERE corpus_id IS NULL"),
        {"corpus_id": corpus_id},
    )
    op.execute(
        """
        CREATE TRIGGER trg_preprocessing_runs_immutable
          BEFORE UPDATE OR DELETE ON preprocessing_runs
          FOR EACH ROW EXECUTE FUNCTION reject_accepted_run_mutation()
        """
    )
    op.execute("DROP TRIGGER trg_source_files_immutable ON source_files")
    bind.execute(
        text("UPDATE source_files SET corpus_id=:corpus_id WHERE corpus_id IS NULL"),
        {"corpus_id": corpus_id},
    )
    op.execute(
        """
        CREATE TRIGGER trg_source_files_immutable
        BEFORE UPDATE OR DELETE ON source_files
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_corpus_mutation()
        """
    )
    unresolved_runs = bind.execute(
        text("SELECT count(*) FROM preprocessing_runs WHERE corpus_id IS NULL")
    ).scalar_one()
    unresolved_sources = bind.execute(
        text("SELECT count(*) FROM source_files WHERE corpus_id IS NULL")
    ).scalar_one()
    if unresolved_runs or unresolved_sources:
        raise RuntimeError("corpus backfill left unresolved Phase 2 records")

    op.alter_column("preprocessing_runs", "corpus_id", nullable=False)
    op.alter_column("source_files", "corpus_id", nullable=False)
    op.create_index(
        "ix_preprocessing_runs_corpus", "preprocessing_runs", ["corpus_id"]
    )
    op.create_index("ix_source_files_corpus", "source_files", ["corpus_id"])
    op.drop_constraint(
        "source_files_relative_path_key", "source_files", type_="unique"
    )
    op.create_unique_constraint(
        "uq_source_files_corpus_path",
        "source_files",
        ["corpus_id", "relative_path"],
    )

    accepted_run_count = bind.execute(
        text(
            """
            SELECT count(*) FROM preprocessing_runs
            WHERE status='accepted'
            """
        )
    ).scalar_one()
    accepted_run_id = bind.execute(
        text(
            """
            SELECT id FROM preprocessing_runs
            WHERE corpus_id=:corpus_id AND status='accepted'
              AND run_name=:run_name
              AND manifest_sha256=:manifest_sha256
              AND inventory_sha256=:inventory_sha256
            """
        ),
        {
            "corpus_id": corpus_id,
            "run_name": ACCEPTED_RUN_NAME,
            "manifest_sha256": ACCEPTED_MANIFEST_SHA256,
            "inventory_sha256": ACCEPTED_INVENTORY_SHA256,
        },
    ).scalar_one_or_none()
    if accepted_run_count and accepted_run_id is None:
        raise RuntimeError(
            "accepted Phase 2 preprocessing run does not match its stable identity"
        )
    bind.execute(
        text(
            """
            UPDATE corpora SET current_preprocessing_run_id=:accepted_run_id
            WHERE id=:corpus_id
            """
        ),
        {"corpus_id": corpus_id, "accepted_run_id": accepted_run_id},
    )
    op.execute(
        """
        CREATE FUNCTION validate_corpus_current_run()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE run_corpus bigint;
        BEGIN
          IF NEW.current_preprocessing_run_id IS NULL THEN RETURN NEW; END IF;
          SELECT corpus_id INTO run_corpus FROM preprocessing_runs
            WHERE id=NEW.current_preprocessing_run_id;
          IF run_corpus IS DISTINCT FROM NEW.id THEN
            RAISE EXCEPTION 'current preprocessing run belongs to another corpus'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE CONSTRAINT TRIGGER trg_corpus_current_run_compatible
          AFTER INSERT OR UPDATE OF current_preprocessing_run_id ON corpora
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_corpus_current_run();
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_taxonomy_label_parent()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_version bigint; cycle_found boolean;
        BEGIN
          IF NEW.parent_label_id IS NULL THEN RETURN NEW; END IF;
          SELECT taxonomy_version_id INTO parent_version FROM taxonomy_labels
            WHERE id=NEW.parent_label_id;
          IF parent_version IS DISTINCT FROM NEW.taxonomy_version_id THEN
            RAISE EXCEPTION 'taxonomy parent belongs to another version'
              USING ERRCODE='23514';
          END IF;
          WITH RECURSIVE ancestors(id,parent_label_id) AS (
            SELECT id,parent_label_id FROM taxonomy_labels WHERE id=NEW.parent_label_id
            UNION ALL
            SELECT label.id,label.parent_label_id FROM taxonomy_labels label
              JOIN ancestors ON label.id=ancestors.parent_label_id
          )
          SELECT EXISTS(SELECT 1 FROM ancestors WHERE id=NEW.id) INTO cycle_found;
          IF cycle_found THEN
            RAISE EXCEPTION 'cyclic taxonomy hierarchy' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE CONSTRAINT TRIGGER trg_taxonomy_parent_compatible
          AFTER INSERT OR UPDATE OF parent_label_id,taxonomy_version_id
          ON taxonomy_labels DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_taxonomy_label_parent();
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_published_taxonomy_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE version_status text;
        BEGIN
          IF TG_TABLE_NAME='taxonomy_versions' THEN version_status=OLD.status;
          ELSIF TG_TABLE_NAME='taxonomy_labels' THEN
            SELECT status INTO version_status FROM taxonomy_versions
              WHERE id=OLD.taxonomy_version_id;
          ELSE
            SELECT version.status INTO version_status
              FROM taxonomy_label_relations relation
              JOIN taxonomy_labels label ON label.id=relation.source_label_id
              JOIN taxonomy_versions version ON version.id=label.taxonomy_version_id
              WHERE relation.id=OLD.id;
          END IF;
          IF version_status='published' THEN
            RAISE EXCEPTION 'published taxonomy content is immutable'
              USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER trg_taxonomy_versions_published
          BEFORE UPDATE OR DELETE ON taxonomy_versions
          FOR EACH ROW EXECUTE FUNCTION reject_published_taxonomy_mutation();
        CREATE TRIGGER trg_taxonomy_labels_published
          BEFORE UPDATE OR DELETE ON taxonomy_labels
          FOR EACH ROW EXECUTE FUNCTION reject_published_taxonomy_mutation();
        CREATE TRIGGER trg_taxonomy_relations_published
          BEFORE UPDATE OR DELETE ON taxonomy_label_relations
          FOR EACH ROW EXECUTE FUNCTION reject_published_taxonomy_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_published_schema_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE version_status text;
        BEGIN
          IF TG_TABLE_NAME='annotation_schema_versions' THEN version_status=OLD.status;
          ELSE
            SELECT status INTO version_status FROM annotation_schema_versions
              WHERE id=OLD.annotation_schema_version_id;
          END IF;
          IF version_status='published' THEN
            RAISE EXCEPTION 'published annotation schema is immutable'
              USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER trg_annotation_schema_versions_published
          BEFORE UPDATE OR DELETE ON annotation_schema_versions
          FOR EACH ROW EXECUTE FUNCTION reject_published_schema_mutation();
        CREATE TRIGGER trg_annotation_fields_published
          BEFORE UPDATE OR DELETE ON annotation_field_definitions
          FOR EACH ROW EXECUTE FUNCTION reject_published_schema_mutation();
        """
    )
    op.execute("DROP VIEW annotation_ready_turns")
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
    )


def downgrade() -> None:
    bind = op.get_bind()
    loaded = bind.execute(
        text(
            "SELECT (SELECT count(*) FROM taxonomy_versions) + "
            "(SELECT count(*) FROM annotation_schema_versions)"
        )
    ).scalar_one()
    extra_corpora = bind.execute(
        text("SELECT count(*) FROM corpora WHERE slug<>:slug"), {"slug": CORPUS_SLUG}
    ).scalar_one()
    if loaded or extra_corpora:
        raise RuntimeError(
            "Phase 2.5 downgrade refused: taxonomy/schema or additional corpus data "
            "would be destroyed; remove it explicitly after review"
        )
    op.execute("DROP VIEW annotation_ready_turns")
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
          ON first_fragment.id=first_lineage.fragment_id
        """
    )
    op.execute(
        """
        DROP FUNCTION reject_published_schema_mutation() CASCADE;
        DROP FUNCTION reject_published_taxonomy_mutation() CASCADE;
        DROP FUNCTION validate_taxonomy_label_parent() CASCADE;
        DROP FUNCTION validate_corpus_current_run() CASCADE;
        """
    )
    op.drop_constraint("uq_source_files_corpus_path", "source_files", type_="unique")
    op.create_unique_constraint(
        "source_files_relative_path_key", "source_files", ["relative_path"]
    )
    op.drop_index("ix_source_files_corpus", table_name="source_files")
    op.drop_index("ix_preprocessing_runs_corpus", table_name="preprocessing_runs")
    op.drop_constraint("fk_source_files_corpus", "source_files", type_="foreignkey")
    op.drop_constraint(
        "fk_preprocessing_runs_corpus", "preprocessing_runs", type_="foreignkey"
    )
    op.drop_column("source_files", "corpus_id")
    op.drop_column("preprocessing_runs", "corpus_id")
    tables = _product_tables()
    for name in reversed(PRODUCT_TABLE_NAMES):
        tables[name].drop(bind=bind, checkfirst=False)
