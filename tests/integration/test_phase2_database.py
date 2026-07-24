from __future__ import annotations

import csv
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest
import yaml
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.db.engine import create_database_engine
from hansard_annotator.db.exceptions import ImportConflictError
from hansard_annotator.db.import_models import EXPECTED_FIELDS, ValidatedRun
from hansard_annotator.db.importer import import_validated_run
from hansard_annotator.db.queries import (
    corpus_summary,
    inspect_turn,
    unlinked_interjections,
)
from hansard_annotator.product.cli import main as product_cli
from hansard_annotator.product.loaders import (
    load_annotation_schema,
    load_taxonomy,
)
from hansard_annotator.product.seeds import SeedConflictError
from hansard_annotator.product.verification import verify_product_foundation

TEST_URL = os.environ.get("HANSARD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="HANSARD_TEST_DATABASE_URL real PostgreSQL is required"
)
ACCEPTED = Path("data/processed/phase1-full-20260723-v4")
ACCEPTED_INVENTORY = "7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2"
ACCEPTED_MANIFEST = "1d7698829d9d1cdceb0373c5d2fc55a719f2a43e6d5242e38bd99067215680df"
PHASE2_TABLES = (
    "preprocessing_runs",
    "database_import_runs",
    "source_files",
    "source_file_versions",
    "debate_sections",
    "debate_events",
    "speech_fragments",
    "reconstruction_runs",
    "speaker_turns",
    "speaker_turn_fragments",
    "interjections",
    "continuation_anomalies",
    "image_anomalies",
    "import_artifacts",
)


@pytest.fixture
def database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    assert TEST_URL is not None
    monkeypatch.setenv("HANSARD_DATABASE_URL", TEST_URL)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield TEST_URL
    cleanup_url = TEST_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(cleanup_url, autocommit=True) as connection:
        connection.execute("TRUNCATE annotation_schemas,taxonomies CASCADE")
    command.downgrade(config, "base")


def _fixture_run(tmp_path: Path, source_file: str = "2010/2010-02-02.xml") -> ValidatedRun:
    run_dir = tmp_path / "phase2-fixture"
    run_dir.mkdir()
    counts: dict[str, int] = {}
    for name, fields in EXPECTED_FIELDS.items():
        dataset = ds.dataset(  # type: ignore[no-untyped-call]
            ACCEPTED / name, format="parquet", partitioning="hive"
        )
        table = dataset.to_table(
            columns=[column for column, _ in fields],
            filter=ds.field("source_file") == source_file,
        )
        target = run_dir / name
        target.mkdir()
        pq.write_table(table, target / "part-00000.parquet")  # type: ignore[no-untyped-call]
        counts[name] = table.num_rows

    with (ACCEPTED / "reports" / "file_processing_report.csv").open(
        encoding="utf-8", newline=""
    ) as source:
        processing_rows = [
            row for row in csv.DictReader(source) if row["source_file"] == source_file
        ]
    reports = run_dir / "reports"
    reports.mkdir()
    with (reports / "file_processing_report.csv").open(
        "w", encoding="utf-8", newline=""
    ) as destination:
        writer = csv.DictWriter(destination, fieldnames=list(processing_rows[0]))
        writer.writeheader()
        writer.writerows(processing_rows)
    with (reports / "image_elements.csv").open("w", encoding="utf-8", newline="") as destination:
        writer = csv.writer(destination)
        writer.writerow(
            [
                "image_key",
                "fragment_key",
                "source_file",
                "sequence_number",
                "image_index",
                "attributes_json",
                "alt_text",
                "clean_text_replacement",
                "major_heading_original",
                "minor_heading_original",
            ]
        )

    source_table = ds.dataset(  # type: ignore[no-untyped-call]
        run_dir / "source_files", format="parquet"
    ).to_table()
    source_row = source_table.to_pylist()[0]
    counts["image_elements"] = 0
    manifest = {
        "run_id": "phase2-fixture",
        "pipeline_version": "1.0.0",
        "git_commit": None,
        "input_inventory_sha256": "b" * 64,
        "configuration_hashes": {
            "reconstruction": "c" * 64,
            "business_types": "d" * 64,
        },
        "configuration_versions": {"reconstruction": "1.0.0"},
        "source_files": [
            {
                "source_file": source_file,
                "source_file_sha256": source_row["source_file_sha256"],
                "byte_size": source_row["byte_size"],
            }
        ],
        "record_counts": counts,
        "output_files": [],
        "started_at": "2026-07-23T00:00:00+00:00",
        "completed_at": "2026-07-23T00:01:00+00:00",
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    (run_dir / "run_manifest.json").write_bytes(manifest_bytes)
    return ValidatedRun(
        run_dir=run_dir,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        artefact_count=0,
        artefact_bytes=0,
        dataset_rows={name: counts[name] for name in EXPECTED_FIELDS},
    )


def _phase2_schema_fingerprint(url: str) -> tuple[tuple[object, ...], ...]:
    queries = (
        """
        SELECT 'column',table_name,column_name,ordinal_position,data_type,udt_name,
               is_nullable,COALESCE(column_default,''),is_identity
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=ANY(%s)
        ORDER BY table_name,ordinal_position
        """,
        """
        SELECT 'constraint',rel.relname,con.conname,con.contype,
               pg_get_constraintdef(con.oid,true)
        FROM pg_constraint con JOIN pg_class rel ON rel.oid=con.conrelid
        JOIN pg_namespace ns ON ns.oid=rel.relnamespace
        WHERE ns.nspname='public' AND rel.relname=ANY(%s)
        ORDER BY rel.relname,con.conname
        """,
        """
        SELECT 'index',tablename,indexname,indexdef
        FROM pg_indexes
        WHERE schemaname='public' AND tablename=ANY(%s)
        ORDER BY tablename,indexname
        """,
        """
        SELECT 'view',viewname,definition
        FROM pg_views
        WHERE schemaname='public'
          AND viewname=ANY(ARRAY[
            'annotation_ready_turns','turns_with_fragment_counts','corpus_year_summary'
          ])
        ORDER BY viewname
        """,
        """
        SELECT 'trigger',rel.relname,trg.tgname,pg_get_triggerdef(trg.oid,true)
        FROM pg_trigger trg JOIN pg_class rel ON rel.oid=trg.tgrelid
        JOIN pg_namespace ns ON ns.oid=rel.relnamespace
        WHERE ns.nspname='public' AND NOT trg.tgisinternal AND rel.relname=ANY(%s)
        ORDER BY rel.relname,trg.tgname
        """,
    )
    rows: list[tuple[object, ...]] = []
    with psycopg.connect(url) as connection:
        for query in queries:
            parameters = () if "'view'" in query else (list(PHASE2_TABLES),)
            rows.extend(tuple(row) for row in connection.execute(query, parameters))
    return tuple(rows)


def test_migration_schema_downgrade_and_upgrade(database_url: str) -> None:
    settings = DatabaseSettings(url=database_url, processed_data_root=Path("data/processed"))
    inspector = inspect(create_database_engine(settings))
    expected = set(PHASE2_TABLES)
    assert expected <= set(inspector.get_table_names())
    assert {
        "annotation_ready_turns",
        "turns_with_fragment_counts",
        "corpus_year_summary",
    } <= set(inspector.get_view_names())
    config = Config("alembic.ini")
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    command.downgrade(config, "base")
    command.upgrade(config, "20260723_01")
    first = _phase2_schema_fingerprint(url)
    command.downgrade(config, "base")
    command.upgrade(config, "20260723_01")
    second = _phase2_schema_fingerprint(url)
    assert first == second
    command.upgrade(config, "head")


def test_constraints_nullable_unknowns_and_immutability(database_url: str) -> None:
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO preprocessing_runs
                  (corpus_id,run_name,pipeline_version,inventory_sha256,manifest_sha256,
                   configuration_hashes,source_file_count,source_byte_count,manifest,
                   processing_started_at,processing_completed_at,status)
                VALUES ((SELECT id FROM corpora LIMIT 1),'bad','1','not-a-sha',%s,
                  '{}',0,0,'{}',now(),now(),'importing')
                """,
                ("a" * 64,),
            )
        connection.rollback()
        run_id = connection.execute(
            """
            INSERT INTO preprocessing_runs
              (corpus_id,run_name,pipeline_version,inventory_sha256,manifest_sha256,
               configuration_hashes,source_file_count,source_byte_count,manifest,
               processing_started_at,processing_completed_at,status,imported_at)
            VALUES ((SELECT id FROM corpora LIMIT 1),'accepted','1',%s,%s,
              '{}',0,0,'{}',now(),now(),'accepted',now())
            RETURNING id
            """,
            ("a" * 64, "b" * 64),
        ).fetchone()[0]
        connection.commit()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE preprocessing_runs SET run_name='mutated' WHERE id=%s", (run_id,)
            )
        connection.rollback()


def test_fixture_import_queries_and_identical_reuse(database_url: str, tmp_path: Path) -> None:
    validated = _fixture_run(tmp_path)
    settings = DatabaseSettings(url=database_url, processed_data_root=tmp_path, batch_size=17)
    first = import_validated_run(settings, validated)
    assert first["status"] == "completed"
    assert first["counts"] == validated.manifest["record_counts"]
    second = import_validated_run(settings, validated)
    assert second["status"] == "completed_reused"
    assert second["corpus_writes"] == 0

    engine = create_database_engine(settings)
    years = corpus_summary(engine)
    assert [row["year"] for row in years] == [2010]
    assert years[0]["fragments"] == validated.manifest["record_counts"]["speech_fragments"]
    with engine.connect() as connection:
        turn_key = connection.exec_driver_sql(
            "SELECT turn_key FROM speaker_turns ORDER BY turn_key LIMIT 1"
        ).scalar_one()
        unknown = connection.exec_driver_sql(
            "SELECT is_presiding_officer FROM speaker_turns "
            "WHERE is_presiding_officer IS NULL LIMIT 1"
        ).scalar_one()
        assert unknown is None
        with pytest.raises(DBAPIError):
            connection.exec_driver_sql(
                "UPDATE speaker_turns SET text_clean='mutated' WHERE turn_key=%s",
                (turn_key,),
            )
    turn = inspect_turn(engine, turn_key)
    assert turn is not None
    assert [row["ordinal"] for row in turn["lineage"]] == sorted(
        row["ordinal"] for row in turn["lineage"]
    )
    assert all(item["link_reason"] for item in unlinked_interjections(engine, limit=10))


def test_failed_import_rolls_back_all_corpus_rows(database_url: str, tmp_path: Path) -> None:
    validated = _fixture_run(tmp_path)
    lineage_path = validated.run_dir / "speaker_turn_fragments" / "part-00000.parquet"
    table = pq.read_table(lineage_path)  # type: ignore[no-untyped-call]
    bad_relations = table["relation_type"].to_pylist()
    bad_relations[0] = "invalid_relation"
    table = table.set_column(
        table.schema.get_field_index("relation_type"),
        "relation_type",
        pa.array(bad_relations),
    )
    pq.write_table(table, lineage_path)  # type: ignore[no-untyped-call]
    settings = DatabaseSettings(url=database_url, processed_data_root=tmp_path, batch_size=17)
    with pytest.raises(psycopg.errors.CheckViolation):
        import_validated_run(settings, validated)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*) FROM preprocessing_runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM speech_fragments").fetchone()[0] == 0
        attempt = connection.execute(
            "SELECT status,error_code FROM database_import_runs"
        ).fetchone()
        assert attempt == ("rolled_back", "CheckViolation")


def test_same_key_different_record_checksum_fails_without_overwrite(
    database_url: str, tmp_path: Path
) -> None:
    validated = _fixture_run(tmp_path)
    settings = DatabaseSettings(url=database_url, processed_data_root=tmp_path, batch_size=17)
    first = import_validated_run(settings, validated)
    source_path = validated.run_dir / "source_files" / "part-00000.parquet"
    table = pq.read_table(source_path)  # type: ignore[no-untyped-call]
    changed_mtime = [value + 1 for value in table["modification_time_ns"].to_pylist()]
    table = table.set_column(
        table.schema.get_field_index("modification_time_ns"),
        "modification_time_ns",
        pa.array(changed_mtime),
    )
    pq.write_table(table, source_path)  # type: ignore[no-untyped-call]
    conflicting_manifest = dict(validated.manifest)
    conflicting_manifest["run_id"] = "phase2-fixture-conflict"
    conflict = ValidatedRun(
        run_dir=validated.run_dir,
        manifest=conflicting_manifest,
        manifest_sha256="e" * 64,
        artefact_count=validated.artefact_count,
        artefact_bytes=validated.artefact_bytes,
        dataset_rows=validated.dataset_rows,
    )
    with pytest.raises(ImportConflictError, match="source_file_versions"):
        import_validated_run(settings, conflict)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*) FROM preprocessing_runs").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM speech_fragments").fetchone()[0]
            == first["counts"]["speech_fragments"]
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM database_import_runs WHERE status='rolled_back'"
            ).fetchone()[0]
            == 1
        )


def test_phase25_registry_seeds_schema_and_product_verification(
    database_url: str, tmp_path: Path
) -> None:
    validated = _fixture_run(tmp_path)
    settings = DatabaseSettings(url=database_url, processed_data_root=tmp_path, batch_size=17)
    imported = import_validated_run(settings, validated)
    counts_before = dict(imported["counts"])

    policy_path = Path("config/taxonomies/australian_policy_domains/0.1.0.yaml")
    status_path = Path("config/taxonomies/content_status/1.0.0.yaml")
    schema_path = Path("config/annotation_schemas/australian_policy_annotation/0.1.0.yaml")
    policy = load_taxonomy(settings, policy_path, repository_root=Path.cwd())
    statuses = load_taxonomy(settings, status_path, repository_root=Path.cwd())
    schema = load_annotation_schema(settings, schema_path, repository_root=Path.cwd())
    assert policy["label_count"] == 15
    assert statuses["label_count"] == 4
    assert schema["field_count"] == 8
    assert load_taxonomy(settings, policy_path, repository_root=Path.cwd())["status"] == "reused"
    assert (
        load_annotation_schema(settings, schema_path, repository_root=Path.cwd())["status"]
        == "reused"
    )

    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        corpus = connection.execute(
            "SELECT * FROM corpora WHERE slug=%s",
            ("australian-house-representatives-hansard",),
        ).fetchone()
        assert corpus is not None
        assert corpus["licence_status"] == "pending_review"
        assert corpus["redistribution_status"] == "pending_review"
        assert corpus["current_preprocessing_run_id"] == imported["preprocessing_run_id"]
        assert (
            connection.execute(
                "SELECT count(*) FROM preprocessing_runs WHERE corpus_id=%s",
                (corpus["id"],),
            ).fetchone()["count"]
            == 1
        )
        assert (
            connection.execute("SELECT corpus_slug FROM annotation_ready_turns LIMIT 1").fetchone()[
                "corpus_slug"
            ]
            == corpus["slug"]
        )
        analytical = connection.execute(
            """
            SELECT count(*) FROM taxonomy_labels label
            JOIN taxonomy_versions version ON version.id=label.taxonomy_version_id
            JOIN taxonomies taxonomy ON taxonomy.id=version.taxonomy_id
            WHERE taxonomy.slug='australian_policy_domains' AND label.is_analytical
            """
        ).fetchone()["count"]
        assert analytical == 14
        assert connection.execute(
            "SELECT array_agg(status ORDER BY status) FROM taxonomy_versions"
        ).fetchone()["array_agg"] == ["draft", "draft"]
        assert connection.execute(
            "SELECT array_agg(status ORDER BY status) FROM annotation_schema_versions"
        ).fetchone()["array_agg"] == ["draft"]
        result = verify_product_foundation(connection)
        assert result["ok"]
        assert not result["cap_required"]
        assert result["phase2"]["counts"] == counts_before


def test_phase25_constraints_conflicts_and_published_immutability(
    database_url: str, tmp_path: Path
) -> None:
    settings = DatabaseSettings(url=database_url, processed_data_root=tmp_path, batch_size=17)
    policy_path = Path("config/taxonomies/australian_policy_domains/0.1.0.yaml")
    load_taxonomy(settings, policy_path, repository_root=Path.cwd())
    copied = tmp_path / "0.1.0.yaml"
    copied.write_text(policy_path.read_text(encoding="utf-8"), encoding="utf-8")
    changed = copied.read_text(encoding="utf-8").replace(
        "Federal budget balance", "Changed example"
    )
    copied.write_text(changed, encoding="utf-8")
    with pytest.raises(SeedConflictError, match="different content"):
        load_taxonomy(settings, copied, repository_root=tmp_path)

    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """
                INSERT INTO corpora
                  (public_id,slug,name,description,jurisdiction,legislature,chamber,
                   language_code,source_format_key,source_format_version,
                   source_description,licence_status,redistribution_status,date_from,
                   date_to,is_public,is_active)
                SELECT gen_random_uuid(),slug,'Duplicate',description,jurisdiction,
                  legislature,chamber,language_code,source_format_key,
                  source_format_version,source_description,licence_status,
                  redistribution_status,date_from,date_to,false,true
                FROM corpora LIMIT 1
                """
            )
        connection.rollback()

        second_corpus_id = connection.execute(
            """
            INSERT INTO corpora
              (slug,name,description,jurisdiction,legislature,chamber,language_code,
               source_format_key,source_format_version,source_description,
               licence_status,redistribution_status,date_from,date_to,is_public,is_active)
            VALUES ('second-corpus','Second','Synthetic second corpus','Test','Test',
              'Test','en','test','1','test','unknown','restricted',
              DATE '2020-01-01',DATE '2020-01-01',false,true) RETURNING id
            """
        ).fetchone()[0]
        second_run_id = connection.execute(
            """
            INSERT INTO preprocessing_runs
              (corpus_id,run_name,pipeline_version,inventory_sha256,manifest_sha256,
               configuration_hashes,source_file_count,source_byte_count,manifest,
               processing_started_at,processing_completed_at,status,imported_at)
            VALUES (%s,'other-corpus-run','1',%s,%s,'{}',0,0,'{}',
              now(),now(),'accepted',now()) RETURNING id
            """,
            (second_corpus_id, "c" * 64, "d" * 64),
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                UPDATE corpora SET current_preprocessing_run_id=%s
                WHERE slug='australian-house-representatives-hansard'
                """,
                (second_run_id,),
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        connection.rollback()

        old_label_id, taxonomy_id = connection.execute(
            """
            SELECT label.id,version.taxonomy_id
            FROM taxonomy_labels label
            JOIN taxonomy_versions version ON version.id=label.taxonomy_version_id
            LIMIT 1
            """
        ).fetchone()
        new_version_id = connection.execute(
            """
            INSERT INTO taxonomy_versions
              (taxonomy_id,version,status,description,source_seed_path,content_sha256)
            VALUES (%s,'0.1.1','draft','test','tests/synthetic',%s) RETURNING id
            """,
            (taxonomy_id, "e" * 64),
        ).fetchone()[0]
        new_label_id = connection.execute(
            """
            INSERT INTO taxonomy_labels
              (taxonomy_version_id,code,label,short_definition,
               inclusion_rules,exclusion_rules,positive_examples,negative_examples,
               borderline_examples,display_order,is_active,is_analytical,is_fallback,
               requires_review,metadata)
            VALUES (%s,'TEST','Test','Test','[]','[]','[]','[]','[]',1,
              true,true,false,false,'{}') RETURNING id
            """,
            (new_version_id,),
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE taxonomy_labels SET parent_label_id=%s WHERE id=%s",
                (old_label_id, new_label_id),
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        connection.rollback()
        version_id = connection.execute(
            """
            UPDATE taxonomy_versions SET status='published',published_at=now()
            WHERE version='0.1.0' RETURNING id
            """
        ).fetchone()[0]
        connection.commit()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                """
                UPDATE taxonomy_labels SET label='Mutated'
                WHERE taxonomy_version_id=%s
                """,
                (version_id,),
            )
        connection.rollback()


def test_phase25_upgrade_backfills_existing_phase2_rows(database_url: str) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "20260723_01")
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        connection.execute(
            "ALTER TABLE preprocessing_runs ALTER COLUMN id RESTART WITH 1000"
        )
        dummy_id = connection.execute(
            """
            INSERT INTO preprocessing_runs
              (run_name,pipeline_version,inventory_sha256,manifest_sha256,
               configuration_hashes,source_file_count,source_byte_count,manifest,
               processing_started_at,processing_completed_at,status,imported_at)
            VALUES ('earlier-dummy','1',%s,%s,'{}',0,0,
              '{"record_counts":{}}',now(),now(),'failed',now()) RETURNING id
            """,
            ("a" * 64, "b" * 64),
        ).fetchone()[0]
        run_id = connection.execute(
            """
            INSERT INTO preprocessing_runs
              (run_name,pipeline_version,inventory_sha256,manifest_sha256,
               configuration_hashes,source_file_count,source_byte_count,manifest,
               processing_started_at,processing_completed_at,status,imported_at)
            VALUES ('phase1-full-20260723-v4','1',%s,%s,'{}',1,1,
              '{"record_counts":{}}',now(),now(),'accepted',now()) RETURNING id
            """,
            (ACCEPTED_INVENTORY, ACCEPTED_MANIFEST),
        ).fetchone()[0]
        connection.execute("ALTER TABLE source_files ALTER COLUMN id RESTART WITH 2000")
        source_id = connection.execute(
            """
            INSERT INTO source_files
              (relative_path,speech_date,chamber,folder_year)
            VALUES ('2010/2010-02-02.xml',DATE '2010-02-02',
              'House of Representatives',2010) RETURNING id
            """
        ).fetchone()[0]
        connection.commit()
    assert dummy_id == 1000
    assert run_id == 1001
    assert source_id == 2000
    assert run_id != 3
    command.upgrade(config, "head")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        corpus = connection.execute(
            "SELECT * FROM corpora WHERE slug=%s",
            ("australian-house-representatives-hansard",),
        ).fetchone()
        assert corpus is not None
        assert corpus["date_from"].isoformat() == "2010-02-02"
        assert corpus["date_to"].isoformat() == "2010-02-02"
        assert corpus["current_preprocessing_run_id"] == run_id
        assert (
            connection.execute(
                "SELECT corpus_id FROM preprocessing_runs WHERE id=%s", (run_id,)
            ).fetchone()["corpus_id"]
            == corpus["id"]
        )
        assert (
            connection.execute(
                "SELECT corpus_id FROM source_files WHERE id=%s", (source_id,)
            ).fetchone()["corpus_id"]
            == corpus["id"]
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO corpora
                  (slug,name,description,jurisdiction,legislature,chamber,language_code,
                   source_format_key,source_format_version,source_description,
                   licence_status,redistribution_status,date_from,date_to,is_public,is_active)
                VALUES ('invalid-range','Invalid','Invalid','Australia','Parliament',
                  'House','en','test','1','test','invented','pending_review',
                  DATE '2025-01-02',DATE '2025-01-01',false,true)
                """
            )
        connection.rollback()


def test_phase25_schema_unknown_taxonomy_and_cli(
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = DatabaseSettings(url=database_url, processed_data_root=tmp_path, batch_size=17)
    import_validated_run(settings, _fixture_run(tmp_path))
    schema_value = json.loads(
        json.dumps(
            yaml.safe_load(
                Path("config/annotation_schemas/australian_policy_annotation/0.1.0.yaml").read_text(
                    encoding="utf-8"
                )
            )
        )
    )
    schema_value["fields"][0]["taxonomy"]["slug"] = "missing_taxonomy"
    unknown = tmp_path / "unknown-taxonomy.yaml"
    unknown.write_text(yaml.safe_dump(schema_value, sort_keys=False), encoding="utf-8")
    with pytest.raises(SeedConflictError, match="unknown taxonomy reference"):
        load_annotation_schema(settings, unknown, repository_root=tmp_path)

    monkeypatch.setenv("HANSARD_DATABASE_URL", database_url)
    commands = [
        ["adapters", "list"],
        ["corpora", "list"],
        ["corpora", "show", "australian-house-representatives-hansard"],
        ["taxonomy", "validate", "config/taxonomies/australian_policy_domains/0.1.0.yaml"],
        ["taxonomy", "load", "config/taxonomies/australian_policy_domains/0.1.0.yaml"],
        ["taxonomy", "load", "config/taxonomies/content_status/1.0.0.yaml"],
        ["taxonomy", "list"],
        ["taxonomy", "show", "australian_policy_domains", "--version", "0.1.0"],
        [
            "schema",
            "validate",
            "config/annotation_schemas/australian_policy_annotation/0.1.0.yaml",
        ],
        [
            "schema",
            "load",
            "config/annotation_schemas/australian_policy_annotation/0.1.0.yaml",
        ],
        ["schema", "list"],
        ["schema", "show", "australian_policy_annotation", "--version", "0.1.0"],
        ["verify"],
    ]
    for command_args in commands:
        assert product_cli(command_args) == 0
    output = capsys.readouterr()
    assert "openaustralia_publicwhip_xml" in output.out
    assert "postgresql://" not in output.out
    assert product_cli(["adapters", "show", "unsupported"]) == 1
    assert "unsupported adapter key" in capsys.readouterr().err
