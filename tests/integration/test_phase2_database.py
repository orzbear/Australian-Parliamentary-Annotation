from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest
import yaml
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
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
from hansard_annotator.db.review_export import (
    EXPORT_COLUMNS,
    ReviewExportOptions,
    export_review_sample,
)
from hansard_annotator.product.cli import main as product_cli
from hansard_annotator.product.loaders import (
    load_annotation_schema,
    load_taxonomy,
)
from hansard_annotator.product.seeds import SeedConflictError
from hansard_annotator.product.verification import verify_product_foundation
from hansard_annotator.web.annotations.service import save_annotation
from hansard_annotator.web.app import create_app
from hansard_annotator.web.auth.service import (
    AuthenticationError,
    authenticate,
    create_user,
    resolve_session,
    revoke_session,
    set_user_enabled,
)
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.exports.service import (
    build_ai_codebook_export,
    build_annotation_export,
)
from hansard_annotator.web.projects.permissions import PermissionDenied
from hansard_annotator.web.projects.service import (
    create_project,
    set_membership,
    transition_project,
)
from hansard_annotator.web.security import SESSION_COOKIE
from hansard_annotator.web.tasks.schemas import SelectionCriteria
from hansard_annotator.web.tasks.service import (
    claim_next,
    generate_batch,
    get_assignment,
    preview_batch,
)

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
PHASE3_TABLES = {
    "users",
    "user_credentials",
    "global_roles",
    "user_global_roles",
    "web_sessions",
    "projects",
    "project_taxonomy_pins",
    "project_memberships",
    "batches",
    "tasks",
    "assignments",
    "annotations",
    "annotation_versions",
    "audit_events",
}


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
        connection.execute("TRUNCATE users,projects CASCADE")
        connection.execute("TRUNCATE annotation_schemas,taxonomies CASCADE")
    command.downgrade(config, "base")


def _fixture_run(
    tmp_path: Path,
    source_file: str = "2010/2010-02-02.xml",
    run_id: str = "phase2-fixture",
) -> ValidatedRun:
    run_dir = tmp_path / run_id
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
        "run_id": run_id,
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


def _web_settings(database_url: str) -> WebSettings:
    return WebSettings(
        database=DatabaseSettings(url=database_url, processed_data_root=Path("data/processed")),
        environment="test",
        session_secret="phase3-test-secret-at-least-thirty-two-characters",
        cookie_secure=False,
        allowed_hosts=("testserver",),
        base_url="http://testserver",
        idle_minutes=60,
        absolute_hours=12,
        minimum_password_length=12,
        lockout_failures=2,
        lockout_minutes=15,
        log_level="INFO",
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
        "current_annotation_ready_turns",
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
        connection.execute("ALTER TABLE preprocessing_runs ALTER COLUMN id RESTART WITH 1000")
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


def test_review_export_unicode_csv_and_database_read_only(
    database_url: str, tmp_path: Path
) -> None:
    settings = DatabaseSettings(url=database_url, processed_data_root=Path("data/processed"))
    import_validated_run(
        settings,
        _fixture_run(
            tmp_path,
            source_file="2016/2016-09-12.xml",
            run_id="unicode-export-fixture",
        ),
    )
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    flynn_key = "9e975417ade94faa2770959f9e108b5ab7a4ba71b28674f4bdee183e545ac64a"
    with psycopg.connect(url) as connection:
        before = connection.execute(
            """
            SELECT (SELECT count(*) FROM preprocessing_runs),
                   (SELECT count(*) FROM speaker_turns),
                   (SELECT count(*) FROM speech_fragments)
            """
        ).fetchone()

    target = tmp_path / "review" / "unicode.csv"
    result = export_review_sample(
        settings,
        ReviewExportOptions(
            output=target,
            limit=100_000,
            min_words=0,
            seed=20260724,
            excel_compatible=True,
        ),
    )
    assert result["database_writes"] == 0
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    with target.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        assert tuple(reader.fieldnames or ()) == EXPORT_COLUMNS
    assert rows
    assert all(set(row) == set(EXPORT_COLUMNS) for row in rows)
    assert all(len(row) == len(EXPORT_COLUMNS) for row in rows)
    assert all(row["source_file"] != row["text_clean"] for row in rows)
    assert all(row["is_orphan_continuation"] != "true" for row in rows)
    assert all(
        row["interrupted"] == ("true" if int(row["interruption_count"]) > 0 else "false")
        for row in rows
    )
    flynn = next(row for row in rows if row["turn_key"] == flynn_key)
    assert "CQ—projects" in flynn["text_clean"]
    dash = flynn["text_clean"][flynn["text_clean"].index("CQ") + 2]
    assert ord(dash) == 0x2014
    assert any(row["procedural_hint"] == "unknown" for row in rows)
    assert any(row["ceremonial_hint"] == "unknown" for row in rows)
    decoded = target.read_text(encoding="utf-8-sig")
    assert "Î“Ã‡" not in decoded
    assert "ΓÇ" not in decoded
    with psycopg.connect(url) as connection:
        after = connection.execute(
            """
            SELECT (SELECT count(*) FROM preprocessing_runs),
                   (SELECT count(*) FROM speaker_turns),
                   (SELECT count(*) FROM speech_fragments)
            """
        ).fetchone()
    assert after == before


def test_review_export_sampling_orphans_and_path_safety(database_url: str, tmp_path: Path) -> None:
    settings = DatabaseSettings(url=database_url, processed_data_root=Path("data/processed"))
    import_validated_run(settings, _fixture_run(tmp_path, run_id="sampling-fixture"))

    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    different = tmp_path / "different.csv"
    included = tmp_path / "included.csv"
    export_review_sample(
        settings,
        ReviewExportOptions(output=first, limit=5, min_words=0, seed=100),
    )
    export_review_sample(
        settings,
        ReviewExportOptions(output=second, limit=5, min_words=0, seed=100),
    )
    export_review_sample(
        settings,
        ReviewExportOptions(output=different, limit=5, min_words=0, seed=101),
    )
    assert first.read_bytes() == second.read_bytes()
    with first.open(encoding="utf-8", newline="") as source:
        first_keys = [row["turn_key"] for row in csv.DictReader(source)]
    with different.open(encoding="utf-8", newline="") as source:
        different_keys = [row["turn_key"] for row in csv.DictReader(source)]
    assert first_keys != different_keys

    export_review_sample(
        settings,
        ReviewExportOptions(
            output=included,
            limit=100_000,
            min_words=0,
            seed=100,
            include_orphans=True,
        ),
    )
    with included.open(encoding="utf-8", newline="") as source:
        included_rows = list(csv.DictReader(source))
    assert any(row["is_orphan_continuation"] == "true" for row in included_rows)

    repository_root = tmp_path / "repository"
    for relative in (
        Path("hansard_xml_files/review.csv"),
        Path("data/processed/accepted/review.csv"),
        Path("backups/review.csv"),
    ):
        with pytest.raises(ValueError, match="forbidden directory"):
            export_review_sample(
                settings,
                ReviewExportOptions(output=repository_root / relative),
                repository_root=repository_root,
            )


def test_annotation_ready_views_are_explicitly_current_run_aware(
    database_url: str, tmp_path: Path
) -> None:
    settings = DatabaseSettings(url=database_url, processed_data_root=Path("data/processed"))
    first = _fixture_run(tmp_path, run_id="historical-run")
    second = _fixture_run(
        tmp_path,
        source_file="2016/2016-09-12.xml",
        run_id="current-run",
    )
    import_validated_run(settings, first)
    import_validated_run(settings, second)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        historical = connection.execute(
            """
            SELECT run_name,preprocessing_run_id,pipeline_version,
                   is_current_corpus_version,count(*) AS rows
            FROM annotation_ready_turns
            GROUP BY run_name,preprocessing_run_id,pipeline_version,
                     is_current_corpus_version
            ORDER BY run_name
            """
        ).fetchall()
        current = connection.execute(
            """
            SELECT DISTINCT run_name,is_current_corpus_version
            FROM current_annotation_ready_turns
            """
        ).fetchall()
    assert {row["run_name"] for row in historical} == {"current-run", "historical-run"}
    assert {row["is_current_corpus_version"] for row in historical} == {False, True}
    assert current == [{"run_name": "current-run", "is_current_corpus_version": True}]


def test_phase3_relations_roles_and_empty_round_trip(database_url: str) -> None:
    settings = DatabaseSettings(url=database_url, processed_data_root=Path("data/processed"))
    inspector = inspect(create_database_engine(settings))
    assert set(inspector.get_table_names()) >= PHASE3_TABLES
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM global_roles WHERE role_key='admin'"
            ).fetchone()[0]
            == 1
        )
        corpus_before = connection.execute("SELECT count(*) FROM speaker_turns").fetchone()[0]
    config = Config("alembic.ini")
    command.downgrade(config, "20260724_03")
    command.upgrade(config, "head")
    with psycopg.connect(url) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM global_roles WHERE role_key='admin'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM speaker_turns").fetchone()[0] == corpus_before
        )


def test_phase3_authentication_lockout_sessions_and_disable(
    database_url: str,
) -> None:
    settings = _web_settings(database_url)
    password = "correct horse phase3"
    user = create_user(
        settings,
        "researcher",
        "Researcher",
        password,
        must_change_password=False,
    )
    principal, token = authenticate(settings, "researcher", password)
    assert principal.user_id == user["id"]
    assert resolve_session(settings, token) is not None
    revoke_session(settings, principal.session_id, principal.user_id)
    assert resolve_session(settings, token) is None
    for _ in range(2):
        with pytest.raises(AuthenticationError, match="Sign-in failed"):
            authenticate(settings, "researcher", "incorrect password")
    with pytest.raises(AuthenticationError, match="Sign-in failed"):
        authenticate(settings, "researcher", password)

    second = create_user(
        settings,
        "disable-me",
        "Disabled Researcher",
        password,
        must_change_password=False,
    )
    set_user_enabled(settings, int(second["id"]), False, int(second["id"]))
    with pytest.raises(AuthenticationError, match="Sign-in failed"):
        authenticate(settings, "disable-me", password)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        hashes = connection.execute("SELECT password_hash FROM user_credentials").fetchall()
        assert all(password not in row[0] and row[0].startswith("$argon2id$") for row in hashes)
        metadata = connection.execute("SELECT metadata::text FROM audit_events").fetchall()
        assert all(password not in row[0] and token not in row[0] for row in metadata)


def test_phase3_project_batch_claim_annotation_and_permissions(
    database_url: str, tmp_path: Path
) -> None:
    settings = _web_settings(database_url)
    db_settings = settings.database
    import_validated_run(
        db_settings,
        _fixture_run(tmp_path, run_id="phase3-corpus-fixture"),
    )
    for seed in (
        Path("config/taxonomies/australian_policy_domains/0.1.0.yaml"),
        Path("config/taxonomies/content_status/1.0.0.yaml"),
    ):
        load_taxonomy(db_settings, seed, repository_root=Path("."))
    load_annotation_schema(
        db_settings,
        Path("config/annotation_schemas/australian_policy_annotation/0.1.0.yaml"),
        repository_root=Path("."),
    )
    password = "phase three safe password"
    create_user(
        settings,
        "phase3-admin",
        "Phase 3 Admin",
        password,
        admin=True,
        must_change_password=False,
    )
    create_user(
        settings,
        "annotator-a",
        "Annotator A",
        password,
        must_change_password=False,
    )
    create_user(
        settings,
        "annotator-b",
        "Annotator B",
        password,
        must_change_password=False,
    )
    admin, _ = authenticate(settings, "phase3-admin", password)
    annotator_a, _ = authenticate(settings, "annotator-a", password)
    annotator_b, _ = authenticate(settings, "annotator-b", password)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        options = connection.execute(
            """
            SELECT c.id AS corpus_id,pr.id AS run_id,asv.id AS schema_id
            FROM corpora c JOIN preprocessing_runs pr ON pr.corpus_id=c.id
            CROSS JOIN annotation_schema_versions asv
            LIMIT 1
            """
        ).fetchone()
    assert options is not None
    project = create_project(
        settings,
        admin,
        slug="phase3-integration",
        name="Phase 3 integration",
        description="Synthetic integration workflow",
        mode="development",
        corpus_id=options["corpus_id"],
        preprocessing_run_id=options["run_id"],
        annotation_schema_version_id=options["schema_id"],
    )
    project_id = int(project["id"])
    set_membership(settings, admin, project_id, annotator_a.user_id, "annotator")
    set_membership(settings, admin, project_id, annotator_b.user_id, "annotator")
    criteria = SelectionCriteria(minimum_words=0, limit=4, seed=314159)
    preview = preview_batch(settings, admin, project_id, criteria)
    assert preview["eligible_count"] >= 4
    batch = generate_batch(
        settings,
        admin,
        project_id,
        name="Deterministic pilot",
        criteria=criteria,
    )
    reused = generate_batch(
        settings,
        admin,
        project_id,
        name="Deterministic pilot",
        criteria=criteria,
    )
    assert batch["selected_count"] == 4
    assert reused["reused"] is True
    with pytest.raises(ValueError, match="different criteria"):
        generate_batch(
            settings,
            admin,
            project_id,
            name="Deterministic pilot",
            criteria=SelectionCriteria(minimum_words=0, limit=3, seed=2),
        )
    transition_project(settings, admin, project_id, "active")

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda actor: claim_next(settings, actor, project_id),
                (annotator_a, annotator_b),
            )
        )
    claimed_ids = [claim["id"] for claim in claims if claim is not None]
    assert len(claimed_ids) == 2
    assert len(set(claimed_ids)) == 2

    assignment_id = int(claims[0]["id"])
    assignment = get_assignment(settings, annotator_a, assignment_id)
    assert assignment["project_id"] == project_id
    with pytest.raises(LookupError):
        get_assignment(settings, annotator_b, assignment_id)
    draft = {
        "content_status": "substantive_policy",
        "primary_australian_domain": None,
        "secondary_australian_domains": [],
        "specific_australian_issue": None,
        "topic_uncertain": False,
        "fallback_explanation": None,
        "unclassifiable_reason": None,
        "annotation_notes": "Draft note",
    }
    first = save_annotation(settings, annotator_a, assignment_id, draft, event_type="draft_saved")
    reused_draft = save_annotation(
        settings, annotator_a, assignment_id, draft, event_type="draft_saved"
    )
    assert first["revision_number"] == 1
    assert reused_draft["reused"] is True
    submitted = {
        **draft,
        "primary_australian_domain": "AU03",
        "annotation_notes": "Submitted note",
    }
    final = save_annotation(settings, annotator_a, assignment_id, submitted, event_type="submitted")
    assert final["revision_number"] == 2
    with psycopg.connect(url) as connection:
        assert (
            connection.execute(
                """
            SELECT count(*) FROM annotation_versions av
            JOIN annotations an ON an.id=av.annotation_id
            WHERE an.assignment_id=%s
            """,
                (assignment_id,),
            ).fetchone()[0]
            == 2
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE annotation_versions SET revision_number=9 WHERE annotation_id=%s",
                (first["annotation_id"],),
            )
        connection.rollback()
    transition_project(settings, admin, project_id, "paused")
    with pytest.raises(PermissionDenied, match="active"):
        claim_next(settings, annotator_a, project_id)


def test_phase3_browser_auth_csrf_headers_and_generic_errors(
    database_url: str,
) -> None:
    settings = _web_settings(database_url)
    password = "browser testing password"
    create_user(
        settings,
        "browser-user",
        "Browser User",
        password,
        must_change_password=False,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").json() == {"status": "ok"}
        login_page = client.get("/login")
        assert login_page.status_code == 200
        assert "Content-Security-Policy" in login_page.headers
        assert login_page.headers["X-Frame-Options"] == "DENY"
        login_csrf = client.cookies.get("hansard_login_csrf")
        assert login_csrf
        rejected = client.post(
            "/login",
            data={
                "username": "browser-user",
                "password": "wrong password",
                "csrf_token": login_csrf,
            },
        )
        assert rejected.status_code == 400
        assert "Username or password was not accepted" in rejected.text
        assert "$argon2" not in rejected.text

        login_csrf = client.cookies.get("hansard_login_csrf")
        accepted = client.post(
            "/login",
            data={
                "username": "browser-user",
                "password": password,
                "csrf_token": login_csrf,
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        session_token = client.cookies.get(SESSION_COOKIE)
        assert session_token
        assert client.get("/").status_code == 200
        assert client.post("/logout", data={"csrf_token": "invalid"}).status_code == 403
        principal = resolve_session(settings, session_token)
        assert principal is not None
        logout = client.post(
            "/logout",
            data={"csrf_token": principal.csrf_token},
            follow_redirects=False,
        )
        assert logout.status_code == 303
        assert resolve_session(settings, session_token) is None


def test_two_pass_aukus_derivation_pins_source_annotation_version(
    database_url: str, tmp_path: Path
) -> None:
    settings = _web_settings(database_url)
    import_validated_run(
        settings.database,
        _fixture_run(tmp_path, run_id="two-pass-corpus-fixture"),
    )
    load_taxonomy(
        settings.database,
        Path("config/taxonomies/australian_policy_domains/0.1.0.yaml"),
        repository_root=Path("."),
    )
    for seed in (
        Path("config/annotation_schemas/australian_policy_annotation/0.2.0.yaml"),
        Path("config/annotation_schemas/australian_aukus_screen/0.1.0.yaml"),
    ):
        load_annotation_schema(settings.database, seed, repository_root=Path("."))

    password = "two pass prototype password"
    create_user(
        settings,
        "prototype-admin",
        "Prototype Admin",
        password,
        admin=True,
        must_change_password=False,
    )
    create_user(
        settings,
        "prototype-coder",
        "Prototype Coder",
        password,
        must_change_password=False,
    )
    admin, admin_token = authenticate(settings, "prototype-admin", password)
    coder, _ = authenticate(settings, "prototype-coder", password)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        pins = connection.execute(
            """
            SELECT c.id AS corpus_id,pr.id AS run_id,
                   general.id AS general_schema_id,auk.id AS aukus_schema_id
            FROM corpora c JOIN preprocessing_runs pr ON pr.corpus_id=c.id
            JOIN annotation_schemas general_schema
              ON general_schema.slug='australian_policy_annotation'
            JOIN annotation_schema_versions general
              ON general.annotation_schema_id=general_schema.id
             AND general.version='0.2.0'
            JOIN annotation_schemas auk_schema
              ON auk_schema.slug='australian_aukus_screen'
            JOIN annotation_schema_versions auk
              ON auk.annotation_schema_id=auk_schema.id AND auk.version='0.1.0'
            LIMIT 1
            """
        ).fetchone()
    assert pins is not None
    general = create_project(
        settings,
        admin,
        slug="conference-general-pass",
        name="Conference general pass",
        description="Synthetic two-pass integration project",
        mode="development",
        corpus_id=pins["corpus_id"],
        preprocessing_run_id=pins["run_id"],
        annotation_schema_version_id=pins["general_schema_id"],
    )
    general_id = int(general["id"])
    set_membership(settings, admin, general_id, coder.user_id, "annotator")
    generate_batch(
        settings,
        admin,
        general_id,
        name="General source",
        criteria=SelectionCriteria(minimum_words=0, limit=2, seed=20260810),
    )
    transition_project(settings, admin, general_id, "active")
    source_assignment = claim_next(settings, coder, general_id)
    assert source_assignment is not None
    source_assignment_id = int(source_assignment["id"])
    source_submission = save_annotation(
        settings,
        coder,
        source_assignment_id,
        {
            "is_non_policy": False,
            "primary_australian_domain": "AU12",
            "secondary_australian_domains": [],
            "fallback_explanation": None,
            "annotation_notes": None,
        },
        event_type="submitted",
    )

    first_export = build_ai_codebook_export(settings, admin, general_id)
    second_export = build_ai_codebook_export(settings, admin, general_id)
    assert first_export.content == second_export.content
    assert first_export.record_count == 1
    assert first_export.snapshot_sha256[:12] in first_export.filename
    with zipfile.ZipFile(io.BytesIO(first_export.content)) as archive:
        assert archive.namelist() == [
            "AI_INSTRUCTIONS.md",
            "CODEBOOK_CONTEXT.json",
            "annotations.csv",
            "annotations.jsonl",
            "manifest.json",
        ]
        manifest = json.loads(archive.read("manifest.json"))
        record = json.loads(archive.read("annotations.jsonl"))
        context = json.loads(archive.read("CODEBOOK_CONTEXT.json"))
        assert manifest["record_count"] == 1
        assert manifest["privacy"]["annotator_identity_included"] is False
        assert manifest["privacy"]["raw_xml_included"] is False
        assert record["speech"]["text"]
        assert record["human_annotation"]["values"]["primary_australian_domain"] == "AU12"
        assert record["human_annotation"]["readable_fields"][1]["value"]["label"]
        assert context["schema"]["version"] == "0.2.0"
        combined = b"".join(archive.read(name) for name in archive.namelist())
        assert b"prototype-admin" not in combined
        assert b"prototype-coder" not in combined
    with pytest.raises(PermissionDenied):
        build_ai_codebook_export(settings, coder, general_id)
    first_csv = build_annotation_export(settings, admin, general_id, "simple_csv")
    second_csv = build_annotation_export(settings, admin, general_id, "simple_csv")
    assert first_csv.content == second_csv.content
    assert first_csv.media_type == "text/csv"
    assert first_csv.filename.endswith(".csv")
    csv_rows = list(csv.DictReader(io.StringIO(first_csv.content.decode("utf-8-sig"))))
    assert len(csv_rows) == 1
    assert csv_rows[0]["primary_australian_domain"] == "AU12"
    assert csv_rows[0]["primary_australian_domain_label"]
    assert csv_rows[0]["speech_text"]
    assert "annotation_notes" in csv_rows[0]
    keyword_any = tmp_path / "target-keyword-any.csv"
    keyword_all = tmp_path / "target-keyword-all.csv"
    domain_sample = tmp_path / "target-domain.csv"
    non_policy_sample = tmp_path / "target-non-policy.csv"
    database_settings = DatabaseSettings(
        url=database_url, processed_data_root=Path("data/processed")
    )
    export_review_sample(
        database_settings,
        ReviewExportOptions(
            output=keyword_any,
            limit=100,
            min_words=0,
            keywords=("SYNTHETIC", "term-that-does-not-exist"),
            keyword_mode="any",
        ),
    )
    export_review_sample(
        database_settings,
        ReviewExportOptions(
            output=keyword_all,
            limit=100,
            min_words=0,
            keywords=("synthetic", "term-that-does-not-exist"),
            keyword_mode="all",
        ),
    )
    export_review_sample(
        database_settings,
        ReviewExportOptions(
            output=domain_sample,
            limit=100,
            min_words=0,
            annotation_project_slug="conference-general-pass",
            primary_domain="AU12",
        ),
    )
    export_review_sample(
        database_settings,
        ReviewExportOptions(
            output=non_policy_sample,
            limit=100,
            min_words=0,
            annotation_project_slug="conference-general-pass",
            annotation_status="non-policy",
        ),
    )
    with keyword_any.open(encoding="utf-8", newline="") as source:
        assert list(csv.DictReader(source))
    with keyword_all.open(encoding="utf-8", newline="") as source:
        assert list(csv.DictReader(source)) == []
    with domain_sample.open(encoding="utf-8", newline="") as source:
        domain_rows = list(csv.DictReader(source))
    assert len(domain_rows) == 1
    with non_policy_sample.open(encoding="utf-8", newline="") as source:
        assert list(csv.DictReader(source)) == []
    with psycopg.connect(url) as connection:
        preserved = connection.execute(
            "SELECT values FROM annotation_versions WHERE annotation_id=%s AND revision_number=%s",
            (
                source_submission["annotation_id"],
                source_submission["revision_number"],
            ),
        ).fetchone()
    assert preserved is not None
    assert preserved[0]["primary_australian_domain"] == "AU12"
    with TestClient(create_app(settings)) as client:
        client.cookies.set(SESSION_COOKIE, admin_token)
        new_project_page = client.get("/projects/new")
        assert new_project_page.status_code == 200
        assert "Create a pinned research project" in new_project_page.text
        assert "Australian Policy Annotation 0.2.0" in new_project_page.text
        download = client.post(
            f"/projects/{general_id}/exports/annotations",
            data={"csrf_token": admin.csrf_token, "export_type": "ai_codebook"},
        )
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert "ai-codebook-input" in download.headers["content-disposition"]
        assert download.headers["cache-control"] == "no-store"
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert json.loads(archive.read("manifest.json"))["record_count"] == 1
        csv_download = client.post(
            f"/projects/{general_id}/exports/annotations",
            data={"csrf_token": admin.csrf_token, "export_type": "simple_csv"},
        )
        assert csv_download.status_code == 200
        assert csv_download.headers["content-type"].startswith("text/csv")
        assert "annotated-data" in csv_download.headers["content-disposition"]
        assert next(csv.DictReader(io.StringIO(csv_download.content.decode("utf-8-sig"))))[
            "primary_australian_domain_label"
        ]

    aukus = create_project(
        settings,
        admin,
        slug="conference-aukus-pass",
        name="Conference AUKUS pass",
        description="Synthetic AU12-derived AUKUS screen",
        mode="development",
        corpus_id=pins["corpus_id"],
        preprocessing_run_id=pins["run_id"],
        annotation_schema_version_id=pins["aukus_schema_id"],
        source_project_id=general_id,
    )
    aukus_id = int(aukus["id"])
    set_membership(settings, admin, aukus_id, coder.user_id, "annotator")
    criteria = SelectionCriteria(source_domain_code="AU12", limit=10, seed=20260810)
    assert preview_batch(settings, admin, aukus_id, criteria) == {
        "eligible_count": 1,
        "selected_count": 1,
    }
    derived_batch = generate_batch(
        settings,
        admin,
        aukus_id,
        name="AUKUS source screen",
        criteria=criteria,
    )
    assert derived_batch["selected_count"] == 1
    with psycopg.connect(url, row_factory=dict_row) as connection:
        provenance = connection.execute(
            """
            SELECT t.source_annotation_version_id,av.values_sha256,
                   an.current_version_id
            FROM tasks t JOIN annotation_versions av
              ON av.id=t.source_annotation_version_id
            JOIN annotations an ON an.id=av.annotation_id
            WHERE t.project_id=%s
            """,
            (aukus_id,),
        ).fetchone()
    assert provenance is not None
    pinned_source_version = int(provenance["source_annotation_version_id"])
    assert pinned_source_version == int(provenance["current_version_id"])
    assert source_submission["sha256"] == provenance["values_sha256"]

    transition_project(settings, admin, aukus_id, "active")
    derived_assignment = claim_next(settings, coder, aukus_id)
    assert derived_assignment is not None
    derived_detail = get_assignment(settings, coder, int(derived_assignment["id"]))
    assert derived_detail["source_annotation_version_id"] == pinned_source_version
    save_annotation(
        settings,
        coder,
        int(derived_assignment["id"]),
        {"discusses_aukus": True, "annotation_notes": None},
        event_type="submitted",
    )

    save_annotation(
        settings,
        coder,
        source_assignment_id,
        {
            "is_non_policy": False,
            "primary_australian_domain": "AU12",
            "secondary_australian_domains": [],
            "fallback_explanation": None,
            "annotation_notes": "Later source revision",
        },
        event_type="revised",
    )
    with psycopg.connect(url, row_factory=dict_row) as connection:
        after_revision = connection.execute(
            """
            SELECT t.source_annotation_version_id,an.current_version_id
            FROM tasks t JOIN annotation_versions av
              ON av.id=t.source_annotation_version_id
            JOIN annotations an ON an.id=av.annotation_id
            WHERE t.project_id=%s
            """,
            (aukus_id,),
        ).fetchone()
    assert after_revision is not None
    assert int(after_revision["source_annotation_version_id"]) == pinned_source_version
    assert int(after_revision["current_version_id"]) != pinned_source_version
