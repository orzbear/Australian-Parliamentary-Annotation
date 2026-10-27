from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import os
import threading
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from psycopg.types.json import Jsonb
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
from hansard_annotator.llm_eval.import_predictions import import_predictions
from hansard_annotator.llm_eval.operations import FROZEN_MODEL, canonical_hash
from hansard_annotator.llm_eval.prediction_tasks import adapt_hostility_prediction
from hansard_annotator.product.cli import main as product_cli
from hansard_annotator.product.loaders import (
    load_annotation_schema,
    load_taxonomy,
)
from hansard_annotator.product.seeds import SeedConflictError
from hansard_annotator.product.verification import verify_product_foundation
from hansard_annotator.web.annotations.ai_review import (
    ensure_prediction_assignment,
    record_prediction_review,
)
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
from hansard_annotator.web.campaigns import phase4e as phase4e_module
from hansard_annotator.web.campaigns.phase4e import (
    authorize_full_production,
    orchestration_tick,
    plan_campaign_chunks,
    set_orchestration_paused,
)
from hansard_annotator.web.campaigns.recovery import (
    artifact_sha256,
)
from hansard_annotator.web.campaigns.service import (
    create_campaign,
    deterministic_selection,
    get_campaign,
    import_campaign,
    list_campaigns,
    other_exclusion_reason,
    plan_campaign,
    production_configuration,
    resolve_campaign_provider_refusals,
    transition_campaign,
    validate_campaign,
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
from hansard_annotator.web.research_exports.canonical_ai import (
    OUTCOME_NO_USABLE_TEXT,
    OUTCOME_PREDICTION,
    OUTCOME_PROVIDER_REFUSAL,
    CanonicalAIExportError,
    create_canonical_full_corpus_export,
)
from hansard_annotator.web.research_exports.service import provider_refusal_export
from hansard_annotator.web.security import SESSION_COOKIE
from hansard_annotator.web.tasks.schemas import SelectionCriteria
from hansard_annotator.web.tasks.service import (
    claim_next,
    generate_batch,
    get_assignment,
    human_annotation_work,
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
PHASE4C_TABLES = {
    "model_prediction_runs",
    "model_predictions",
    "model_prediction_reviews",
}
PHASE4D_TABLES = {
    "ai_annotation_campaigns",
    "ai_annotation_campaign_records",
}
PHASE4D_RECOVERY_TABLES = {
    "ai_annotation_campaign_recoveries",
    "ai_annotation_campaign_recovery_records",
}
PHASE4E_TABLES = {"ai_annotation_campaign_batches"}
PHASE4E_ABSTENTION_TABLES = {"model_prediction_abstentions"}


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
        connection.execute(
            "TRUNCATE model_prediction_outcome_reviews,model_prediction_abstentions,"
            "ai_annotation_campaign_recovery_records,"
            "ai_annotation_campaign_recoveries,ai_annotation_campaign_records,"
            "ai_annotation_campaign_batches,ai_annotation_campaigns,"
            "model_prediction_reviews,model_predictions,model_prediction_runs"
        )
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
    assert set(inspector.get_table_names()) >= (
        PHASE3_TABLES | PHASE4C_TABLES | PHASE4D_TABLES | PHASE4D_RECOVERY_TABLES
    )
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


def test_phase4d_migration_constraints_immutability_linkage_and_downgrade_guard(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "20261009_06")
    settings = DatabaseSettings(url=database_url, processed_data_root=Path("data/processed"))
    inspector = inspect(create_database_engine(settings))
    assert set(inspector.get_table_names()) >= PHASE4C_TABLES
    assert not (PHASE4D_TABLES & set(inspector.get_table_names()))

    command.upgrade(config, "20261013_07")
    inspector = inspect(create_database_engine(settings))
    assert set(inspector.get_table_names()) >= PHASE4C_TABLES | PHASE4D_TABLES
    campaign_fks = inspector.get_foreign_keys("ai_annotation_campaigns")
    assert {fk["referred_table"] for fk in campaign_fks} >= {
        "corpora",
        "users",
        "model_prediction_runs",
    }
    record_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("ai_annotation_campaign_records")
    }
    assert {
        ("campaign_id", "speaker_turn_id"),
        ("campaign_id", "record_id"),
        ("campaign_id", "selection_order"),
    } <= record_uniques
    command.upgrade(config, "20261017_08")
    inspector = inspect(create_database_engine(settings))
    assert set(inspector.get_table_names()) >= PHASE4D_RECOVERY_TABLES
    recovery_fks = inspector.get_foreign_keys("ai_annotation_campaign_recoveries")
    assert {fk["referred_table"] for fk in recovery_fks} == {
        "ai_annotation_campaigns"
    }
    command.upgrade(config, "20261018_09")
    inspector = inspect(create_database_engine(settings))
    assert set(inspector.get_table_names()) >= PHASE4E_TABLES
    assert not (PHASE4E_ABSTENTION_TABLES & set(inspector.get_table_names()))
    command.upgrade(config, "20261020_10")
    inspector = inspect(create_database_engine(settings))
    assert set(inspector.get_table_names()) >= PHASE4E_ABSTENTION_TABLES
    abstention_fks = inspector.get_foreign_keys("model_prediction_abstentions")
    assert {fk["referred_table"] for fk in abstention_fks} == {
        "model_prediction_runs",
        "speaker_turns",
    }
    command.upgrade(config, "head")
    inspector = inspect(create_database_engine(settings))
    assert "model_prediction_outcome_reviews" in inspector.get_table_names()
    assert "terminal_invalid_count" in {
        column["name"] for column in inspector.get_columns("ai_annotation_campaigns")
    }

    import_validated_run(settings, _fixture_run(tmp_path, run_id="phase4d-db-fixture"))
    password = "phase4d database test password"
    create_user(
        _web_settings(database_url),
        "phase4d-db-admin",
        "Phase 4D DB Admin",
        password,
        admin=True,
        must_change_password=False,
    )
    admin, _ = authenticate(_web_settings(database_url), "phase4d-db-admin", password)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    frozen = "a41027b001f10b61074c196996606c86b5dac4c0788ae185a05ff68fcf3b7cc6"
    schema = "9c94bab9cd2976559c79b1c31991aef4138a2071a5d3b3d6186a26448f117126"
    with psycopg.connect(url) as connection:
        corpus_id = connection.execute("SELECT id FROM corpora LIMIT 1").fetchone()[0]
        turn_id = connection.execute("SELECT id FROM speaker_turns LIMIT 1").fetchone()[0]
    campaign = create_campaign(
        _web_settings(database_url), admin, name="DB acceptance", corpus_id=corpus_id
    )
    campaign_id = campaign["id"]
    assert campaign["status"] == "DRAFT"
    assert campaign["cache_status"] == "REQUIRED"
    with psycopg.connect(url) as connection:
        connection.execute(
            """
            INSERT INTO ai_annotation_campaign_records
              (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
            VALUES (%s,%s,'turn:db-acceptance',0,%s)
            """,
            (campaign_id, turn_id, "1" * 64),
        )
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='PLANNED',planned_count=1,
                   record_set_sha256=%s WHERE id=%s
            """,
            ("2" * 64, campaign_id),
        )
        connection.commit()

        recovery_id = connection.execute(
            """
            INSERT INTO ai_annotation_campaign_recoveries
              (campaign_id,attempt_number,unresolved_set_sha256,request_count,status,
               submission_display_name)
            VALUES (%s,1,%s,1,'PREPARING','phase4d-recovery-db-001') RETURNING id
            """,
            (campaign_id, "6" * 64),
        ).fetchone()[0]
        campaign_record_id = connection.execute(
            "SELECT id FROM ai_annotation_campaign_records WHERE campaign_id=%s",
            (campaign_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO ai_annotation_campaign_recovery_records
              (recovery_id,campaign_record_id,record_id,original_request_index,
               original_validation_error)
            VALUES (%s,%s,'turn:db-acceptance',0,
                    'primary domain cannot also be secondary')
            """,
            (recovery_id, campaign_record_id),
        )
        connection.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """
                INSERT INTO ai_annotation_campaign_recoveries
                  (campaign_id,attempt_number,unresolved_set_sha256,request_count,status,
                   submission_display_name)
                VALUES (%s,2,%s,1,'PREPARING','phase4d-recovery-db-002')
                """,
                (campaign_id, "6" * 64),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="recovery provenance"):
            connection.execute(
                """
                UPDATE ai_annotation_campaign_recoveries SET request_count=2 WHERE id=%s
                """,
                (recovery_id,),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "DELETE FROM ai_annotation_campaign_recovery_records WHERE recovery_id=%s",
                (recovery_id,),
            )
        connection.rollback()

        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """
                INSERT INTO ai_annotation_campaign_records
                  (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
                VALUES (%s,%s,'turn:duplicate',1,%s)
                """,
                (campaign_id, turn_id, "3" * 64),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE ai_annotation_campaign_records SET selection_order=2 WHERE campaign_id=%s",
                (campaign_id,),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "DELETE FROM ai_annotation_campaign_records WHERE campaign_id=%s",
                (campaign_id,),
            )
        connection.rollback()

        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='SUBMITTED',provider_job_id='batches/db',
              cache_status='REUSABLE',cache_identifier='cachedContents/db',
              cache_content_sha256=%s,cache_model='models/gemini-2.5-flash',
              cache_display_name='phase4d-phase4a-policy-v2-a41027b001f10b61',
              cache_created_at=now(),cache_expires_at=now()+interval '24 hours',
              cache_verified_at=now()
            WHERE id=%s
            """,
            (frozen, campaign_id),
        )
        connection.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="provenance is immutable"):
            connection.execute(
                "UPDATE ai_annotation_campaigns SET prompt_sha256=%s WHERE id=%s",
                ("4" * 64, campaign_id),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="provenance is immutable"):
            connection.execute(
                "UPDATE ai_annotation_campaigns SET cache_identifier='cachedContents/other' WHERE id=%s",
                (campaign_id,),
            )
        connection.rollback()

        run_id = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,provider,configured_model,prompt_version,prompt_sha256,
               canonical_schema_sha256,execution_mode,record_set_sha256,requested_count,
               provider_success_count,provider_failure_count,valid_count,invalid_count,status)
            VALUES (%s,'gemini','gemini-2.5-flash','phase4a-policy-v2',%s,%s,'batch',%s,
                    1,1,0,1,0,'imported') RETURNING id
            """,
            ("5" * 64, frozen, schema, "2" * 64),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='IMPORTED',model_prediction_run_id=%s,
                   imported_at=now() WHERE id=%s
            """,
            (run_id, campaign_id),
        )
        connection.commit()
        linked = connection.execute(
            "SELECT status,model_prediction_run_id FROM ai_annotation_campaigns WHERE id=%s",
            (campaign_id,),
        ).fetchone()
        assert linked == ("IMPORTED", run_id)

    monkeypatch.setenv("HANSARD_DATABASE_URL", database_url)
    with pytest.raises(RuntimeError, match="recovery downgrade refused"):
        command.downgrade(config, "20261009_06")


def test_phase4e_chunk_persistence_authorization_and_downgrade_guard(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _web_settings(database_url)
    import_validated_run(
        settings.database,
        _fixture_run(tmp_path, run_id="phase4e-chunk-fixture"),
    )
    password = "phase4e database test password"
    create_user(
        settings,
        "phase4e-planner",
        "Phase 4E Planner",
        password,
        admin=True,
        must_change_password=False,
    )
    admin, _ = authenticate(settings, "phase4e-planner", password)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        corpus = connection.execute("SELECT id FROM corpora LIMIT 1").fetchone()
        assert corpus is not None
        corpus_id = int(corpus[0])
    campaign = create_campaign(settings, admin, name="Phase 4E DB", corpus_id=corpus_id)
    planned = plan_campaign(
        settings,
        admin,
        str(campaign["public_id"]),
        limit=1,
        seed="phase4e-db",
        exclude_human_annotated=False,
    ).campaign
    chunked = plan_campaign_chunks(
        settings,
        admin,
        str(campaign["public_id"]),
        enqueued_token_ceiling=10_000_000,
        safety_factor=0.9,
        max_chunk_records=1,
    )
    assert chunked["orchestration_status"] == "PLANNED"
    with psycopg.connect(url) as connection:
        batches = connection.execute(
            """
            SELECT chunk_number,first_selection_order,last_selection_order,record_count
            FROM ai_annotation_campaign_batches WHERE campaign_id=%s ORDER BY chunk_number
            """,
            (campaign["id"],),
        ).fetchall()
        assert batches == [(1, 0, 0, 1)]
        with pytest.raises(psycopg.errors.RaiseException, match="provenance is immutable"):
            connection.execute(
                """
                UPDATE ai_annotation_campaign_batches SET record_set_sha256=%s
                WHERE campaign_id=%s AND chunk_number=1
                """,
                ("f" * 64, campaign["id"]),
            )
        connection.rollback()
    authorized = authorize_full_production(
        settings,
        admin,
        str(campaign["public_id"]),
        confirmation="START FULL PRODUCTION",
        spend_ceiling_usd=Decimal(str(planned["estimated_total_cost_usd"])),
    )
    assert authorized["status"] == "READY"
    assert authorized["orchestration_status"] == "RUNNING"
    assert set_orchestration_paused(
        settings, admin, str(campaign["public_id"]), paused=True
    )["orchestration_status"] == "PAUSED"
    assert set_orchestration_paused(
        settings, admin, str(campaign["public_id"]), paused=False
    )["orchestration_status"] == "RUNNING"
    with psycopg.connect(url) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="authorization is immutable"):
            connection.execute(
                """
                UPDATE ai_annotation_campaigns SET authorized_spend_ceiling_usd=999
                WHERE id=%s
                """,
                (campaign["id"],),
            )
        connection.rollback()
        record_id = connection.execute(
            "SELECT record_id FROM ai_annotation_campaign_records WHERE campaign_id=%s",
            (campaign["id"],),
        ).fetchone()[0]

    rollback_artifact = tmp_path / "phase4e-import-rollback"
    rollback_artifact.mkdir()
    prediction = {
        "record_id": record_id,
        "request_index": 0,
        "status": "success",
        "prediction": {
            "is_non_policy": True,
            "primary_australian_domain": None,
            "secondary_australian_domains": [],
            "reasoning": "Non-policy.",
        },
        "usage": {},
    }
    summary = {
        "provenance": {
            "provider": planned["provider"],
            "model": planned["configured_model"],
            "prompt_version": planned["prompt_version"],
            "assembled_prompt_sha256": planned["prompt_sha256"],
            "canonical_schema_sha256": planned["canonical_schema_sha256"],
            "prediction_contract_sha256": planned["prediction_contract_sha256"],
            "provider_schema_sha256": planned["provider_schema_sha256"],
            **planned["generation_config"],
            "execution_mode": "batch",
            "batch_id": "phase4e:rollback-test",
            "record_ids_sha256": "f" * 64,
        },
        "coverage": {
            "accounting_invariant_ok": True,
            "planned_requests": 2,
            "successful_provider_responses": 2,
            "failed_provider_responses": 0,
            "valid_structured_predictions": 2,
            "invalid_structured_predictions": 0,
        },
        "tokens": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        },
        "cost": {
            "token_processing_cost_usd": 0,
            "explicit_cache_storage_cost_usd": 0,
            "total_pilot_cost_usd": 0,
        },
        "batch_status": {"model": planned["configured_model"]},
    }
    (rollback_artifact / "operational_summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    (rollback_artifact / "predictions.jsonl").write_text(
        json.dumps(prediction) + "\n" + json.dumps(prediction) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        import_predictions(rollback_artifact, settings.database, dry_run=False)
    with psycopg.connect(url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM model_prediction_runs WHERE provider_job_id=%s",
            ("phase4e:rollback-test",),
        ).fetchone()[0] == 0
    monkeypatch.setenv("HANSARD_DATABASE_URL", database_url)
    with pytest.raises(RuntimeError, match="Phase 4E downgrade refused"):
        command.downgrade(Config("alembic.ini"), "20261017_08")


def test_phase4e_advisory_lock_allows_progress_and_is_scoped_per_campaign(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _web_settings(database_url)
    import_validated_run(
        settings.database,
        _fixture_run(tmp_path, run_id="phase4e-advisory-lock-fixture"),
    )
    password = "phase4e advisory lock test password"
    create_user(
        settings,
        "phase4e-lock-tester",
        "Phase 4E Lock Tester",
        password,
        admin=True,
        must_change_password=False,
    )
    admin, _ = authenticate(settings, "phase4e-lock-tester", password)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        corpus_id = int(connection.execute("SELECT id FROM corpora LIMIT 1").fetchone()[0])

    def authorized_campaign(name: str, seed: str) -> dict[str, object]:
        campaign = create_campaign(settings, admin, name=name, corpus_id=corpus_id)
        planned = plan_campaign(
            settings,
            admin,
            str(campaign["public_id"]),
            limit=1,
            seed=seed,
            exclude_human_annotated=False,
        ).campaign
        plan_campaign_chunks(
            settings,
            admin,
            str(campaign["public_id"]),
            enqueued_token_ceiling=10_000_000,
            safety_factor=0.9,
            max_chunk_records=1,
        )
        return authorize_full_production(
            settings,
            admin,
            str(campaign["public_id"]),
            confirmation="START FULL PRODUCTION",
            spend_ceiling_usd=Decimal(str(planned["estimated_total_cost_usd"])),
        )

    first = authorized_campaign("Phase 4E lock first", "phase4e-lock-first")
    second = authorized_campaign("Phase 4E lock second", "phase4e-lock-second")
    first_id = str(first["public_id"])
    second_id = str(second["public_id"])

    progressed = orchestration_tick(settings, first_id)
    assert progressed.action == "reserved"
    with psycopg.connect(url) as connection:
        assert connection.execute(
            """
            SELECT batch.status FROM ai_annotation_campaign_batches batch
            JOIN ai_annotation_campaigns campaign ON campaign.id=batch.campaign_id
            WHERE campaign.public_id::text=%s
            """,
            (first_id,),
        ).fetchone()[0] == "SUBMITTING"

    entered = threading.Event()
    release = threading.Event()

    def hold_same_campaign(
        _settings: WebSettings, public_id: str, *, gateway: object = None
    ) -> phase4e_module.TickResult:
        del gateway
        entered.set()
        assert release.wait(timeout=10)
        return phase4e_module.TickResult("held", public_id)

    with monkeypatch.context() as patch:
        patch.setattr(phase4e_module, "_orchestration_tick_locked", hold_same_campaign)
        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(orchestration_tick, settings, first_id)
            assert entered.wait(timeout=10)
            contender = executor.submit(orchestration_tick, settings, first_id)
            contender_result = contender.result(timeout=10)
            release.set()
            holder_result = holder.result(timeout=10)
    assert {holder_result.action, contender_result.action} == {"held", "locked"}

    barrier = threading.Barrier(2)

    def hold_different_campaigns(
        _settings: WebSettings, public_id: str, *, gateway: object = None
    ) -> phase4e_module.TickResult:
        del gateway
        barrier.wait(timeout=10)
        return phase4e_module.TickResult("independent", public_id)

    with monkeypatch.context() as patch:
        patch.setattr(
            phase4e_module, "_orchestration_tick_locked", hold_different_campaigns
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                future.result(timeout=10)
                for future in (
                    executor.submit(orchestration_tick, settings, first_id),
                    executor.submit(orchestration_tick, settings, second_id),
                )
            ]
    assert [result.action for result in results] == ["independent", "independent"]

    class Gateway:
        def __init__(self) -> None:
            self.submissions = 0

        def create_cache(self, request: dict[str, object]) -> dict[str, object]:
            now = datetime.now(UTC)
            return {
                "name": "cachedContents/phase4e-lock-test",
                "model": request["model"],
                "displayName": request["displayName"],
                "createTime": now.isoformat(),
                "expireTime": (now + timedelta(hours=24)).isoformat(),
                "usageMetadata": {"totalTokenCount": 4_131},
            }

        def get_cache(self, _cache_identifier: str) -> dict[str, object]:
            raise AssertionError("new cache should be created only once")

        def find_batch(self, _display_name: str, _model: str) -> None:
            return None

        def submit(self, _plan: dict[str, object], _model: str) -> str:
            self.submissions += 1
            return "batches/phase4e-lock-test"

        def status(self, provider_job_id: str) -> dict[str, object]:
            return {
                "name": provider_job_id,
                "metadata": {
                    "state": "BATCH_STATE_RUNNING",
                    "batchStats": {
                        "requestCount": "1",
                        "successfulRequestCount": "0",
                        "failedRequestCount": "0",
                        "pendingRequestCount": "1",
                    },
                },
            }

    gateway = Gateway()
    submitted = orchestration_tick(settings, first_id, gateway=gateway)
    refreshed = orchestration_tick(settings, first_id, gateway=gateway)
    assert submitted.action == "submitted"
    assert refreshed.action == "refreshed"
    assert gateway.submissions == 1


def test_phase4d_planning_matches_historical_selection_and_rolls_back_atomically(
    database_url: str, tmp_path: Path
) -> None:
    settings = _web_settings(database_url)
    import_validated_run(
        settings.database,
        _fixture_run(tmp_path, run_id="phase4d-planning-fixture"),
    )
    password = "phase4d planning test password"
    create_user(
        settings,
        "phase4d-planner",
        "Phase 4D Planner",
        password,
        admin=True,
        must_change_password=False,
    )
    admin, _ = authenticate(settings, "phase4d-planner", password)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        corpus = connection.execute("SELECT id FROM corpora LIMIT 1").fetchone()
        assert corpus is not None
        source_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT turn.id,turn.turn_key,turn.speech_date AS date,turn.chamber,
                       turn.speaker_name_raw,turn.major_heading_original,
                       turn.minor_heading_original,turn.business_type,
                       turn.text_clean,turn.text_raw
                FROM speaker_turns turn
                JOIN reconstruction_runs reconstruction
                  ON reconstruction.id=turn.reconstruction_run_id
                JOIN corpora corpus ON corpus.current_preprocessing_run_id=
                  reconstruction.preprocessing_run_id
                WHERE corpus.id=%s
                """,
                (corpus["id"],),
            ).fetchall()
        ]
    eligible = [row for row in source_rows if other_exclusion_reason(row) is None]
    expected, expected_hash = deterministic_selection(
        eligible, limit=10, seed="database-bounded"
    )
    assert len(expected) == 10

    first = create_campaign(
        settings, admin, name="Bounded planning one", corpus_id=corpus["id"]
    )
    second = create_campaign(
        settings, admin, name="Bounded planning two", corpus_id=corpus["id"]
    )
    first_plan = plan_campaign(
        settings,
        admin,
        str(first["public_id"]),
        limit=10,
        seed="database-bounded",
        exclude_human_annotated=True,
    )
    second_plan = plan_campaign(
        settings,
        admin,
        str(second["public_id"]),
        limit=10,
        seed="database-bounded",
        exclude_human_annotated=True,
    )
    assert first_plan.campaign["planned_count"] == 10
    assert first_plan.campaign["record_set_sha256"] == expected_hash
    assert second_plan.campaign["record_set_sha256"] == expected_hash
    with psycopg.connect(url, row_factory=dict_row) as connection:
        first_records = connection.execute(
            """
            SELECT speaker_turn_id,record_id,selection_order
            FROM ai_annotation_campaign_records
            WHERE campaign_id=%s ORDER BY selection_order
            """,
            (first["id"],),
        ).fetchall()
        second_ids = [
            row["record_id"]
            for row in connection.execute(
                """
                SELECT record_id FROM ai_annotation_campaign_records
                WHERE campaign_id=%s ORDER BY selection_order
                """,
                (second["id"],),
            ).fetchall()
        ]
    expected_ids = [f"turn:{row['turn_key']}" for row in expected]
    assert [row["record_id"] for row in first_records] == expected_ids
    assert second_ids == expected_ids

    interrupted = create_campaign(
        settings, admin, name="Interrupted planning", corpus_id=corpus["id"]
    )
    with psycopg.connect(url) as connection:
        connection.execute(
            """
            INSERT INTO ai_annotation_campaign_records
              (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
            VALUES (%s,%s,'turn:preexisting',0,%s)
            """,
            (interrupted["id"], first_records[0]["speaker_turn_id"], "1" * 64),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        plan_campaign(
            settings,
            admin,
            str(interrupted["public_id"]),
            limit=10,
            seed="database-bounded",
            exclude_human_annotated=True,
        )
    with psycopg.connect(url) as connection:
        state = connection.execute(
            """
            SELECT campaign.status,count(record.id)
            FROM ai_annotation_campaigns campaign
            LEFT JOIN ai_annotation_campaign_records record
              ON record.campaign_id=campaign.id
            WHERE campaign.id=%s GROUP BY campaign.id
            """,
            (interrupted["id"],),
        ).fetchone()
    assert state == ("DRAFT", 1)


def test_provider_refusals_import_separately_and_exclude_same_configuration(
    database_url: str, tmp_path: Path
) -> None:
    settings = _web_settings(database_url)
    import_validated_run(
        settings.database,
        _fixture_run(tmp_path, run_id="phase4e-provider-refusal-fixture"),
    )
    password = "phase4e provider refusal test password"
    create_user(
        settings,
        "phase4e-refusal-tester",
        "Phase 4E Refusal Tester",
        password,
        admin=True,
        must_change_password=False,
    )
    admin, _ = authenticate(settings, "phase4e-refusal-tester", password)
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        turns = connection.execute(
            "SELECT id,turn_key FROM speaker_turns ORDER BY id LIMIT 2"
        ).fetchall()
        corpus = connection.execute("SELECT id FROM corpora LIMIT 1").fetchone()
        assert corpus is not None
        corpus_id = int(corpus["id"])
    assert len(turns) == 2
    record_ids = [f"turn:{turn['turn_key']}" for turn in turns]
    configuration = production_configuration()
    response = {
        "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
        "modelVersion": configuration["configured_model"],
    }
    rows = [
        {
            "record_id": record_ids[0],
            "request_index": 0,
            "status": "success",
            "prediction": {
                "is_non_policy": True,
                "primary_australian_domain": None,
                "secondary_australian_domains": [],
                "reasoning": "Non-policy.",
            },
            "usage": {},
        },
        {
            "record_id": record_ids[1],
            "request_index": 1,
            "status": "invalid",
            "prediction": None,
            "validation_error": "prediction must be an object",
            "provider_response": response,
            "usage": {},
        },
    ]
    source = tmp_path / "provider-refusal-source"
    source.mkdir()
    (source / "predictions.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    provenance = {
        "provider": configuration["provider"],
        "model": configuration["configured_model"],
        "prompt_version": configuration["prompt_version"],
        "assembled_prompt_sha256": configuration["prompt_sha256"],
        "canonical_schema_sha256": configuration["canonical_schema_sha256"],
        "prediction_contract_sha256": configuration["prediction_contract_sha256"],
        "provider_schema_sha256": configuration["provider_schema_sha256"],
        **configuration["generation_config"],
        "execution_mode": configuration["execution_mode"],
        "batch_id": "phase4e:provider-refusal-integration",
        "record_ids_sha256": canonical_hash(record_ids),
    }
    summary = {
        "provenance": provenance,
        "coverage": {
            "planned_requests": 2,
            "collected_responses": 2,
            "missing_responses": 0,
            "duplicate_mappings": 0,
            "successful_provider_responses": 2,
            "failed_provider_responses": 0,
            "valid_structured_predictions": 1,
            "invalid_structured_predictions": 1,
            "accounting_invariant_ok": True,
        },
        "tokens": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        },
        "cost": {
            "token_processing_cost_usd": 0,
            "explicit_cache_storage_cost_usd": 0,
            "total_pilot_cost_usd": 0,
        },
        "batch_status": {"model": configuration["configured_model"]},
    }
    (source / "operational_summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    source_hash = artifact_sha256(source)
    lifecycle = create_campaign(
        settings, admin, name="Provider refusal lifecycle", corpus_id=corpus_id
    )
    with psycopg.connect(url) as connection:
        for index, (turn, record_id) in enumerate(zip(turns, record_ids, strict=True)):
            connection.execute(
                """
                INSERT INTO ai_annotation_campaign_records
                  (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (lifecycle["id"], turn["id"], record_id, index, canonical_hash(rows[index])),
            )
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='COLLECTED',planned_count=2,
              record_set_sha256=%s,artifact_reference=%s,artifact_sha256=%s,
              valid_count=1,invalid_count=1,orchestration_mode='CHUNKED',
              orchestration_status='RECOVERY_REQUIRED',operational_metadata=%s
            WHERE id=%s
            """,
            (
                canonical_hash(record_ids),
                str(source),
                source_hash,
                Jsonb({"coverage": summary["coverage"]}),
                lifecycle["id"],
            ),
        )
    resolved = resolve_campaign_provider_refusals(
        settings, admin, str(lifecycle["public_id"])
    )
    assert resolved["valid_count"] == 1
    assert resolved["invalid_count"] == 0
    assert resolved["orchestration_status"] == "COMPLETE"
    assert validate_campaign(
        settings, admin, str(lifecycle["public_id"])
    )["status"] == "VALIDATED"
    imported = import_campaign(settings, admin, str(lifecycle["public_id"]))
    assert imported["status"] == "IMPORTED"

    with psycopg.connect(url) as connection:
        run = connection.execute(
            "SELECT id,public_id FROM model_prediction_runs WHERE provider_job_id=%s",
            ("phase4e:provider-refusal-integration",),
        ).fetchone()
        assert run is not None
        run_id = run[0]
        assert connection.execute(
            "SELECT count(*) FROM model_predictions WHERE run_id=%s", (run_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM model_prediction_abstentions WHERE run_id=%s",
            (run_id,),
        ).fetchone()[0] == 1
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE model_prediction_abstentions SET resolution_reason='OTHER' "
                "WHERE run_id=%s",
                (run_id,),
            )
        connection.rollback()

    refusal_csv = provider_refusal_export(settings, admin, str(run[1])).content.decode(
        "utf-8"
    )
    exported = list(csv.DictReader(io.StringIO(refusal_csv)))
    assert len(exported) == 1
    assert exported[0]["record_id"] == record_ids[1]
    refusal_provenance = json.loads(exported[0]["refusal_provenance"])
    assert refusal_provenance["resolution"] == "PROVIDER_REFUSAL"
    assert refusal_provenance["reason"] == "PROHIBITED_CONTENT"
    assert len(refusal_provenance["attempts"]) == 1
    assert refusal_provenance["attempts"][0]["provider_response"] == response

    campaign = create_campaign(
        settings, admin, name="Refusal exclusion", corpus_id=corpus_id
    )
    planned = plan_campaign(
        settings,
        admin,
        str(campaign["public_id"]),
        limit=200_000,
        seed="provider-refusal-exclusion",
        exclude_human_annotated=False,
    ).campaign
    assert int(planned["excluded_existing_prediction_count"]) == 2
    with psycopg.connect(url) as connection:
        selected = {
            str(row[0])
            for row in connection.execute(
                "SELECT record_id FROM ai_annotation_campaign_records WHERE campaign_id=%s",
                (campaign["id"],),
            ).fetchall()
        }
    assert not (set(record_ids) & selected)

    override = create_campaign(
        settings, admin, name="Explicit refusal retry", corpus_id=corpus_id
    )
    plan_campaign(
        settings,
        admin,
        str(override["public_id"]),
        limit=200_000,
        seed="provider-refusal-override",
        exclude_human_annotated=False,
        retry_provider_refusals=True,
    )
    with psycopg.connect(url) as connection:
        override_selected = {
            str(row[0])
            for row in connection.execute(
                "SELECT record_id FROM ai_annotation_campaign_records WHERE campaign_id=%s",
                (override["id"],),
            ).fetchall()
        }
    assert record_ids[0] not in override_selected
    assert record_ids[1] in override_selected


def test_hostility_terminal_invalid_import_is_immutable_and_idempotent(
    database_url: str, tmp_path: Path
) -> None:
    settings = DatabaseSettings.from_environment()
    import_validated_run(settings, _fixture_run(tmp_path, run_id="terminal-invalid-fixture"))
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        turn = connection.execute(
            "SELECT turn_key FROM speaker_turns ORDER BY id LIMIT 1"
        ).fetchone()
    assert turn is not None
    record_id = f"turn:{turn[0]}"
    artifact = tmp_path / "terminal-invalid-artifact"
    artifact.mkdir()
    provenance = {
        "provider": "gemini", "model": FROZEN_MODEL,
        "prompt_version": "hostility-prompt-v2",
        "assembled_prompt_sha256": "a" * 64, "canonical_schema_sha256": "b" * 64,
        "prediction_contract_sha256": "c" * 64, "provider_schema_sha256": "d" * 64,
        "thinking_budget": 0, "execution_mode": "batch",
        "batch_id": "batches/terminal-invalid-fixture", "record_ids_sha256": "e" * 64,
    }
    summary = {
        "provenance": provenance, "batch_status": {"model": FROZEN_MODEL},
        "coverage": {
            "planned_requests": 1, "successful_provider_responses": 1,
            "failed_provider_responses": 0, "valid_structured_predictions": 0,
            "invalid_structured_predictions": 1, "provider_refused": 0,
            "missing_responses": 0, "duplicate_mappings": 0,
            "accounting_invariant_ok": False,
        },
        "tokens": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
        "cost": {"token_processing_cost_usd": 0, "explicit_cache_storage_cost_usd": 0,
                 "total_pilot_cost_usd": 0},
    }
    invalid = {
        "record_id": record_id, "status": "invalid",
        "prediction": {"hostility_present": "YES", "evidence_span": "ambiguous"},
        "validation_error": "hostility evidence must resolve uniquely to canonical text",
        "provider_response": {"responseId": "terminal-invalid-response"},
        "request_index": 0, "response_index": 0, "returned_model": FROZEN_MODEL,
        "usage": {},
    }
    (artifact / "operational_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (artifact / "predictions.jsonl").write_text(json.dumps(invalid) + "\n", encoding="utf-8")
    expected = {
        key: provenance[key]
        for key in (
            "provider", "model", "prompt_version", "assembled_prompt_sha256",
            "canonical_schema_sha256", "prediction_contract_sha256",
            "provider_schema_sha256", "execution_mode",
        )
    }
    kwargs = {
        "prediction_task": "hostility",
        "annotation_schema_slug": "australian_partisan_hostility",
        "annotation_schema_version": "0.2.0",
        "expected_configuration": expected,
    }
    first = import_predictions(artifact, settings, dry_run=False, **kwargs)
    repeated = import_predictions(artifact, settings, dry_run=False, **kwargs)
    assert first["inserted_predictions"] == 0
    assert first["inserted_terminal_invalid_outcomes"] == 1
    assert repeated["inserted_runs"] == 0
    assert repeated["existing_terminal_invalid_outcomes"] == 1
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*) FROM model_predictions").fetchone()[0] == 0
        outcome = connection.execute(
            "SELECT resolution_type,resolution_reason,outcome_provenance "
            "FROM model_prediction_abstentions"
        ).fetchone()
        assert outcome is not None
        assert outcome[0:2] == ("INVALID_OUTPUT", "EVIDENCE_RESOLUTION_FAILED")
        assert outcome[2]["attempt_count"] == 1
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute(
                "UPDATE model_prediction_abstentions SET resolution_reason='PROHIBITED_CONTENT'"
            )


def test_canonical_full_corpus_ai_export_combines_exact_configuration_runs(
    database_url: str, tmp_path: Path
) -> None:
    export_work_root = tmp_path / "export-work"
    settings = dataclasses.replace(
        _web_settings(database_url),
        export_work_root=export_work_root,
    )
    import_validated_run(
        settings.database,
        _fixture_run(tmp_path, run_id="canonical-ai-export-fixture"),
    )
    password = "canonical AI export test password"
    create_user(
        settings,
        "canonical-ai-exporter",
        "Canonical AI Exporter",
        password,
        admin=True,
        must_change_password=False,
    )
    admin, admin_token = authenticate(settings, "canonical-ai-exporter", password)
    configuration = production_configuration()
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        corpus = connection.execute(
            "SELECT id,public_id FROM corpora LIMIT 1"
        ).fetchone()
        assert corpus is not None
        turns = connection.execute(
            """
            SELECT turn.id,turn.turn_key
            FROM speaker_turns turn
            JOIN reconstruction_runs reconstruction
              ON reconstruction.id=turn.reconstruction_run_id
            JOIN corpora corpus
              ON corpus.current_preprocessing_run_id=reconstruction.preprocessing_run_id
            WHERE corpus.id=%s ORDER BY turn.id
            """,
            (corpus["id"],),
        ).fetchall()
        assert len(turns) >= 4
        run_one_turns = turns[:-3]
        run_two_turn = turns[-3]
        refused_turn = turns[-2]
        no_text_turn = turns[-1]

        connection.execute(
            """
            INSERT INTO speaker_turns
              (turn_key,reconstruction_run_id,source_file_version_id,major_section_id,
               minor_section_id,turn_sequence,first_fragment_sequence,last_fragment_sequence,
               fragment_count,speech_date,chamber,major_heading_original,
               minor_heading_original,speaker_id_raw,speaker_name_raw,text_raw,text_clean,
               calculated_word_count,warning_codes,business_type,business_mapping_version,
               is_interjection,is_orphan_continuation,interrupted,interruption_count,
               has_known_speaker,eligible_50_words,eligible_100_words,record_sha256)
            SELECT %s,reconstruction_run_id,source_file_version_id,major_section_id,
                   minor_section_id,
                   (SELECT max(turn_sequence)+1 FROM speaker_turns WHERE reconstruction_run_id=t.reconstruction_run_id),
                   first_fragment_sequence,last_fragment_sequence,fragment_count,speech_date,
                   chamber,major_heading_original,minor_heading_original,speaker_id_raw,
                   speaker_name_raw,'   ','',0,warning_codes,business_type,business_mapping_version,
                   is_interjection,is_orphan_continuation,interrupted,interruption_count,
                   has_known_speaker,false,false,%s
            FROM speaker_turns t WHERE t.id=%s
            """,
            ("f" * 64, "e" * 64, no_text_turn["id"]),
        )
        turns = connection.execute(
            """
            SELECT turn.id,turn.turn_key
            FROM speaker_turns turn
            JOIN reconstruction_runs reconstruction
              ON reconstruction.id=turn.reconstruction_run_id
            JOIN corpora corpus
              ON corpus.current_preprocessing_run_id=reconstruction.preprocessing_run_id
            WHERE corpus.id=%s ORDER BY turn.id
            """,
            (corpus["id"],),
        ).fetchall()
        run_one_turns = turns[:-3]
        run_two_turn = turns[-3]
        refused_turn = turns[-2]
        no_text_turn = turns[-1]

        production_run_1 = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,provider,configured_model,returned_model,prompt_version,
               prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
               provider_schema_sha256,generation_config,execution_mode,provider_job_id,
               record_set_sha256,requested_count,provider_success_count,
               provider_failure_count,valid_count,invalid_count,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,0,'imported')
            RETURNING id,public_id
            """,
            (
                "a" * 64,
                configuration["provider"],
                configuration["configured_model"],
                configuration["configured_model"],
                configuration["prompt_version"],
                configuration["prompt_sha256"],
                configuration["canonical_schema_sha256"],
                configuration["prediction_contract_sha256"],
                configuration["provider_schema_sha256"],
                Jsonb(configuration["generation_config"]),
                configuration["execution_mode"],
                "batches/canonical-production-1",
                "b" * 64,
                len(run_one_turns),
                len(run_one_turns),
                len(run_one_turns),
            ),
        ).fetchone()
        assert production_run_1 is not None
        first_prediction_public_id: str | None = None
        for index, turn in enumerate(run_one_turns):
            inserted = connection.execute(
                """
                INSERT INTO model_predictions
                  (run_id,speaker_turn_id,record_id,is_non_policy,
                   primary_australian_domain,secondary_australian_domains,reasoning,
                   validation_status,returned_model,artifact_reference)
                VALUES (%s,%s,%s,false,'AU07','{"AU08"}','Immutable production prediction.',
                        'success',%s,'data/predictions/run1/predictions.jsonl')
                RETURNING public_id
                """,
                (
                    production_run_1["id"],
                    turn["id"],
                    f"turn:{turn['turn_key']}",
                    configuration["configured_model"],
                ),
            ).fetchone()
            assert inserted is not None
            if index == 0:
                first_prediction_public_id = str(inserted["public_id"])

        assert first_prediction_public_id is not None

        production_run_2 = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,provider,configured_model,returned_model,prompt_version,
               prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
               provider_schema_sha256,generation_config,execution_mode,provider_job_id,
               record_set_sha256,requested_count,provider_success_count,
               provider_failure_count,valid_count,invalid_count,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,2,2,0,1,0,'imported')
            RETURNING id,public_id
            """,
            (
                "1" * 64,
                configuration["provider"],
                configuration["configured_model"],
                configuration["configured_model"],
                configuration["prompt_version"],
                configuration["prompt_sha256"],
                configuration["canonical_schema_sha256"],
                configuration["prediction_contract_sha256"],
                configuration["provider_schema_sha256"],
                Jsonb(configuration["generation_config"]),
                configuration["execution_mode"],
                "batches/canonical-production-2",
                "2" * 64,
            ),
        ).fetchone()
        assert production_run_2 is not None
        connection.execute(
            """
            INSERT INTO model_predictions
              (run_id,speaker_turn_id,record_id,is_non_policy,
               secondary_australian_domains,reasoning,validation_status,
               returned_model,artifact_reference)
            VALUES (%s,%s,%s,true,'{}','Second canonical campaign prediction.',
                    'success',%s,'data/predictions/run2/predictions.jsonl')
            """,
            (
                production_run_2["id"],
                run_two_turn["id"],
                f"turn:{run_two_turn['turn_key']}",
                configuration["configured_model"],
            ),
        )
        connection.execute(
            """
            INSERT INTO model_prediction_abstentions
              (run_id,speaker_turn_id,record_id,resolution_reason,
               outcome_provenance,artifact_reference)
            VALUES (%s,%s,%s,'PROHIBITED_CONTENT',%s,'test/provider_refusals.jsonl')
            """,
            (
                production_run_2["id"],
                refused_turn["id"],
                f"turn:{refused_turn['turn_key']}",
                Jsonb(
                    {
                        "resolution": "PROVIDER_REFUSAL",
                        "reason": "PROHIBITED_CONTENT",
                        "configuration_provenance": configuration,
                    }
                ),
            ),
        )
        historical_run = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,provider,configured_model,returned_model,prompt_version,
               prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
               provider_schema_sha256,generation_config,execution_mode,provider_job_id,
               record_set_sha256,requested_count,provider_success_count,
               provider_failure_count,valid_count,invalid_count,status)
            VALUES (%s,'gemini','gemini-2.5-flash','gemini-2.5-flash',%s,%s,%s,%s,%s,
                    %s,'batch','batches/historical',%s,3,3,0,3,0,'imported')
            RETURNING id
            """,
            (
                "c" * 64,
                configuration["prompt_version"],
                configuration["prompt_sha256"],
                configuration["canonical_schema_sha256"],
                configuration["prediction_contract_sha256"],
                configuration["provider_schema_sha256"],
                Jsonb({"thinking_budget": 0}),
                "d" * 64,
            ),
        ).fetchone()
        assert historical_run is not None
        for turn in (run_one_turns[0], refused_turn, no_text_turn):
            connection.execute(
                """
                INSERT INTO model_predictions
                  (run_id,speaker_turn_id,record_id,is_non_policy,
                   secondary_australian_domains,reasoning,validation_status,
                   returned_model)
                VALUES (%s,%s,%s,true,'{}','HISTORICAL MODEL MUST NOT LEAK',
                        'success','gemini-2.5-flash')
                """,
                (
                    historical_run["id"],
                    turn["id"],
                    f"turn:{turn['turn_key']}",
                ),
            )

    campaign_1 = create_campaign(
        settings,
        admin,
        name="Canonical production provenance 1",
        corpus_id=int(corpus["id"]),
    )
    campaign_2 = create_campaign(
        settings,
        admin,
        name="Canonical production provenance 2",
        corpus_id=int(corpus["id"]),
    )
    with psycopg.connect(url) as connection:
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='IMPORTED',planned_count=%s,
              record_set_sha256=%s,provider_job_id='phase4e:canonical-export-test-1',
              artifact_reference='test/canonical-1',artifact_sha256=%s,
              valid_count=%s,invalid_count=0,model_prediction_run_id=%s,imported_at=now()
            WHERE id=%s
            """,
            (
                len(run_one_turns),
                "b" * 64,
                "a" * 64,
                len(run_one_turns),
                production_run_1["id"],
                campaign_1["id"],
            ),
        )
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='IMPORTED',planned_count=2,
              record_set_sha256=%s,provider_job_id='phase4e:canonical-export-test-2',
              artifact_reference='test/canonical-2',artifact_sha256=%s,
              valid_count=1,invalid_count=0,model_prediction_run_id=%s,imported_at=now()
            WHERE id=%s
            """,
            (
                "2" * 64,
                "1" * 64,
                production_run_2["id"],
                campaign_2["id"],
            ),
        )

    load_taxonomy(
        settings.database,
        Path("config/taxonomies/australian_policy_domains/0.1.0.yaml"),
        repository_root=Path("."),
    )
    load_annotation_schema(
        settings.database,
        Path("config/annotation_schemas/australian_policy_annotation/0.2.0.yaml"),
        repository_root=Path("."),
    )
    with psycopg.connect(url, row_factory=dict_row) as connection:
        pins = connection.execute(
            """
            SELECT pr.id AS run_id,asv.id AS schema_id
            FROM corpora c JOIN preprocessing_runs pr ON pr.id=c.current_preprocessing_run_id
            JOIN annotation_schemas s ON s.slug='australian_policy_annotation'
            JOIN annotation_schema_versions asv
              ON asv.annotation_schema_id=s.id AND asv.version='0.2.0'
            WHERE c.id=%s
            """,
            (corpus["id"],),
        ).fetchone()
    assert pins is not None
    review_project = create_project(
        settings,
        admin,
        slug="canonical-review-layer",
        name="Canonical Review Layer",
        description="Optional human review layer",
        mode="development",
        corpus_id=int(corpus["id"]),
        preprocessing_run_id=int(pins["run_id"]),
        annotation_schema_version_id=int(pins["schema_id"]),
    )
    transition_project(settings, admin, int(review_project["id"]), "active")
    assignment_id = ensure_prediction_assignment(settings, admin, first_prediction_public_id)
    human_edit = save_annotation(
        settings,
        admin,
        assignment_id,
        {
            "is_non_policy": False,
            "primary_australian_domain": "AU12",
            "secondary_australian_domains": [],
            "fallback_explanation": None,
            "annotation_notes": "Human reviewer edited domain to AU12.",
        },
        event_type="submitted",
    )
    record_prediction_review(
        settings,
        admin,
        first_prediction_public_id,
        "edited",
        assignment_id=assignment_id,
        annotation_version_id=int(human_edit["annotation_version_id"]),  # type: ignore[arg-type]
        note="Human reviewer edited domain to AU12.",
    )
    record_prediction_review(
        settings,
        admin,
        first_prediction_public_id,
        "flagged_codebook",
        assignment_id=assignment_id,
        note="Boundary ambiguity noted.",
    )

    fixed = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    first = create_canonical_full_corpus_export(
        settings, admin, str(corpus["public_id"]), tmp_path / "canonical-first", generated_at=fixed
    )
    second = create_canonical_full_corpus_export(
        settings,
        admin,
        str(corpus["public_id"]),
        tmp_path / "canonical-second",
        generated_at=fixed + timedelta(seconds=1),
    )
    assert first.csv_path.read_bytes() == second.csv_path.read_bytes()
    assert first.parquet_path.read_bytes() == second.parquet_path.read_bytes()
    first_without_time = {**first.manifest}
    second_without_time = {**second.manifest}
    first_without_time.pop("generated_at")
    second_without_time.pop("generated_at")
    assert first_without_time == second_without_time

    with first.csv_path.open(encoding="utf-8", newline="") as source:
        csv_rows = list(csv.DictReader(source))
    parquet_rows = pq.read_table(first.parquet_path).to_pylist()  # type: ignore[no-untyped-call]
    assert len(csv_rows) == len(parquet_rows) == len(turns)
    assert [row["turn_key"] for row in csv_rows] == [
        str(row["turn_key"]) for row in parquet_rows
    ]
    assert [row["outcome_status"] for row in csv_rows] == [
        str(row["outcome_status"]) for row in parquet_rows
    ]
    assert first.manifest["outcome_counts"] == {
        OUTCOME_PREDICTION: len(turns) - 2,
        OUTCOME_PROVIDER_REFUSAL: 1,
        OUTCOME_NO_USABLE_TEXT: 1,
    }
    assert first.manifest["configuration"] == configuration
    assert first.manifest["prediction_run_ids"] == sorted(
        [str(production_run_1["public_id"]), str(production_run_2["public_id"])]
    )
    assert first.manifest["campaign_ids"] == sorted(
        [str(campaign_1["public_id"]), str(campaign_2["public_id"])]
    )
    assert [int(row["corpus_order"]) for row in csv_rows] == list(
        range(1, len(turns) + 1)
    )
    assert "HISTORICAL MODEL MUST NOT LEAK" not in first.csv_path.read_text(
        encoding="utf-8"
    )

    reviewed_row = next(
        row for row in csv_rows if row["turn_key"] == str(run_one_turns[0]["turn_key"])
    )
    assert reviewed_row["outcome_status"] == OUTCOME_PREDICTION
    assert reviewed_row["is_non_policy"] == "false"
    assert reviewed_row["primary_australian_domain"] == "AU07"
    assert reviewed_row["secondary_australian_domains"] == '["AU08"]'
    assert reviewed_row["reasoning"] == "Immutable production prediction."
    assert reviewed_row["thinking_level"] == "minimal"
    assert reviewed_row["latest_review_status"] == "edited"
    assert reviewed_row["review_edited"] == "true"
    assert reviewed_row["latest_review_note"] == "Human reviewer edited domain to AU12."
    assert reviewed_row["has_codebook_flag"] == "true"
    assert reviewed_row["latest_codebook_flag_note"] == "Boundary ambiguity noted."

    refusal = next(
        row for row in csv_rows if row["outcome_status"] == OUTCOME_PROVIDER_REFUSAL
    )
    assert refusal["record_id"] == f"turn:{refused_turn['turn_key']}"
    assert refusal["is_non_policy"] == ""
    assert refusal["primary_australian_domain"] == ""
    assert refusal["secondary_australian_domains"] == ""
    assert refusal["reasoning"] == ""
    assert refusal["provider_block_reason"] == "PROHIBITED_CONTENT"
    assert json.loads(refusal["refusal_provenance_json"])["reason"] == "PROHIBITED_CONTENT"
    assert refusal["artifact_reference"] == "test/provider_refusals.jsonl"

    no_text = next(
        row for row in csv_rows if row["outcome_status"] == OUTCOME_NO_USABLE_TEXT
    )
    assert no_text["record_id"] == f"turn:{no_text_turn['turn_key']}"
    assert no_text["is_non_policy"] == ""
    assert no_text["primary_australian_domain"] == ""
    assert no_text["secondary_australian_domains"] == ""
    assert no_text["reasoning"] == ""
    assert no_text["provider"] == ""
    assert no_text["prediction_run_id"] == ""

    assert any(row["text_raw"] for row in csv_rows)
    with zipfile.ZipFile(first.zip_path) as archive:
        assert archive.namelist() == [
            "canonical_full_corpus_ai.csv",
            "canonical_full_corpus_ai.parquet",
            "manifest.json",
        ]
    for name, metadata in first.manifest["files"].items():
        assert metadata["sha256"] == hashlib.sha256(
            (first.directory / name).read_bytes()
        ).hexdigest()

    sibling_export = export_work_root / "canonical-full-corpus-ai-sibling"
    sibling_export.mkdir(parents=True)
    sibling_marker = sibling_export / "in-progress.part"
    sibling_marker.write_bytes(b"sibling")

    with TestClient(create_app(settings)) as client:
        client.cookies.set(SESSION_COOKIE, admin_token)
        exports_page = client.get("/exports")
        assert exports_page.status_code == 200
        assert "Canonical Full-Corpus AI Export" in exports_page.text
        download = client.post(
            f"/exports/canonical-ai/{corpus['public_id']}.zip",
            data={"csrf_token": admin.csrf_token},
        )
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert archive.read("canonical_full_corpus_ai.csv") == first.csv_path.read_bytes()

    assert list(export_work_root.iterdir()) == [sibling_export]
    assert sibling_marker.read_bytes() == b"sibling"

    with psycopg.connect(url) as connection:
        connection.execute(
            """
            INSERT INTO model_predictions
              (run_id,speaker_turn_id,record_id,is_non_policy,
               secondary_australian_domains,reasoning,validation_status,
               returned_model)
            VALUES (%s,%s,%s,true,'{}','Duplicate canonical prediction.',
                    'success',%s)
            """,
            (
                production_run_2["id"],
                run_one_turns[0]["id"],
                f"turn:{run_one_turns[0]['turn_key']}",
                configuration["configured_model"],
            ),
        )
    with pytest.raises(CanonicalAIExportError, match="multiple production predictions"):
        create_canonical_full_corpus_export(
            settings,
            admin,
            str(corpus["public_id"]),
            tmp_path / "canonical-duplicate",
            generated_at=fixed,
        )



def test_phase4c_prediction_runs_allow_same_turn_across_immutable_runs(
    database_url: str, tmp_path: Path
) -> None:
    settings = DatabaseSettings(url=database_url, processed_data_root=Path("data/processed"))
    import_validated_run(settings, _fixture_run(tmp_path, run_id="phase4c-fixture"))
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as connection:
        turn_id = connection.execute("SELECT id FROM speaker_turns ORDER BY id LIMIT 1").fetchone()[0]
        run_ids = []
        for marker in ("a", "b"):
            run_ids.append(
                connection.execute(
                    """
                    INSERT INTO model_prediction_runs
                      (artifact_sha256,provider,configured_model,prompt_version,prompt_sha256,
                       canonical_schema_sha256,execution_mode,record_set_sha256,
                       requested_count,provider_success_count,provider_failure_count,
                       valid_count,invalid_count,status)
                    VALUES (%s,'gemini','gemini-2.5-flash','phase4a-policy-v2',%s,%s,
                            'batch',%s,1,1,0,1,0,'imported') RETURNING id
                    """,
                    (marker * 64, "1" * 64, "2" * 64, "3" * 64),
                ).fetchone()[0]
            )
        for index, run_id in enumerate(run_ids):
            connection.execute(
                """
                INSERT INTO model_predictions
                  (run_id,speaker_turn_id,record_id,is_non_policy,
                   primary_australian_domain,secondary_australian_domains,
                   reasoning,validation_status)
                VALUES (%s,%s,%s,false,'AU06','{}','Transport policy.','success')
                """,
                (run_id, turn_id, f"turn:fixture-{index}"),
            )
        assert connection.execute(
            "SELECT count(*) FROM model_predictions WHERE speaker_turn_id=%s", (turn_id,)
        ).fetchone()[0] == 2
        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(
                "UPDATE model_predictions SET reasoning='changed' WHERE run_id=%s",
                (run_ids[0],),
            )


def test_task_aware_prediction_migration_round_trip_with_existing_policy_data(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HANSARD_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "20261020_10")
    settings = DatabaseSettings(url=database_url, processed_data_root=Path("data/processed"))
    import_validated_run(settings, _fixture_run(tmp_path, run_id="task-aware-migration"))
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        turn = connection.execute(
            "SELECT id,turn_key,text_clean FROM speaker_turns ORDER BY id LIMIT 1"
        ).fetchone()
        assert turn is not None
        policy_run = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,provider,configured_model,prompt_version,prompt_sha256,
               canonical_schema_sha256,execution_mode,record_set_sha256,
               requested_count,provider_success_count,provider_failure_count,
               valid_count,invalid_count,status)
            VALUES (%s,'gemini','gemini-2.5-flash','phase4a-policy-v2',%s,%s,
                    'batch',%s,1,1,0,1,0,'imported') RETURNING id
            """,
            ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
        ).fetchone()
        assert policy_run is not None
        policy = connection.execute(
            """
            INSERT INTO model_predictions
              (run_id,speaker_turn_id,record_id,is_non_policy,
               primary_australian_domain,secondary_australian_domains,
               reasoning,validation_status)
            VALUES (%s,%s,%s,false,'AU06',ARRAY['AU02'],'Policy reason.','success')
            RETURNING id
            """,
            (policy_run["id"], turn["id"], f"turn:{turn['turn_key']}"),
        ).fetchone()
        assert policy is not None

    command.upgrade(config, "head")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        migrated = connection.execute(
            """
            SELECT mp.*,run.prediction_task AS run_task,run.annotation_schema_slug,
                   run.annotation_schema_version
            FROM model_predictions mp JOIN model_prediction_runs run ON run.id=mp.run_id
            WHERE mp.id=%s
            """,
            (policy["id"],),
        ).fetchone()
        assert migrated is not None
        assert migrated["prediction_task"] == migrated["run_task"] == "policy"
        assert migrated["annotation_schema_slug"] == "australian_policy_annotation"
        assert migrated["annotation_schema_version"] == "0.2.0"
        assert migrated["prediction_values"] == {
            "is_non_policy": False,
            "primary_australian_domain": "AU06",
            "secondary_australian_domains": ["AU02"],
        }
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute(
                """
                INSERT INTO model_predictions
                  (run_id,speaker_turn_id,record_id,prediction_task,prediction_values,
                   is_non_policy,secondary_australian_domains,reasoning,validation_status)
                VALUES (%s,%s,'turn:malformed-policy','policy','{}',false,'{}',
                        'Missing primary.','success')
                """,
                (policy_run["id"], turn["id"]),
            )

        adapted = adapt_hostility_prediction(
            {
                "hostility_present": "YES",
                "target": "OPPOSING_PARTISAN_TARGET",
                "evidence_spans": [{"text": str(turn["text_clean"])[:10]}],
                "reasoning": "Representative hostility reason.",
            },
            str(turn["text_clean"]),
        )
        hostility_run = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,prediction_task,annotation_schema_slug,
               annotation_schema_version,provider,configured_model,prompt_version,
               prompt_sha256,canonical_schema_sha256,execution_mode,record_set_sha256,
               requested_count,provider_success_count,provider_failure_count,
               valid_count,invalid_count,status)
            VALUES (%s,'hostility',%s,%s,'gemini','gemini-2.5-flash',
                    'hostility-prompt-v2',%s,%s,'batch',%s,1,1,0,1,0,'imported')
            RETURNING id
            """,
            ("e" * 64, adapted.schema_slug, adapted.schema_version,
             "f" * 64, "1" * 64, "2" * 64),
        ).fetchone()
        assert hostility_run is not None
        hostility = connection.execute(
            """
            INSERT INTO model_predictions
              (run_id,speaker_turn_id,record_id,prediction_task,prediction_values,
               is_non_policy,primary_australian_domain,secondary_australian_domains,
               reasoning,validation_status)
            VALUES (%s,%s,'turn:hostility','hostility',%s,NULL,NULL,NULL,%s,'success')
            RETURNING id,prediction_task,prediction_values,reasoning,is_non_policy,
                      primary_australian_domain,secondary_australian_domains
            """,
            (hostility_run["id"], turn["id"], Jsonb(adapted.values), adapted.reasoning),
        ).fetchone()
        assert hostility is not None
        assert hostility["prediction_task"] == "hostility"
        assert hostility["prediction_values"] == adapted.values
        assert hostility["reasoning"] == "Representative hostility reason."
        assert hostility["is_non_policy"] is None
        assert hostility["primary_australian_domain"] is None
        assert hostility["secondary_australian_domains"] is None
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState
        ), connection.transaction():
            connection.execute(
                "UPDATE model_predictions SET reasoning='changed' WHERE id=%s",
                (hostility["id"],),
            )
        mismatch_run = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,provider,configured_model,prompt_version,prompt_sha256,
               canonical_schema_sha256,execution_mode,record_set_sha256,
               requested_count,provider_success_count,provider_failure_count,
               valid_count,invalid_count,status)
            VALUES (%s,'gemini','gemini-2.5-flash','phase4a-policy-v2',%s,%s,
                    'batch',%s,1,1,0,1,0,'imported') RETURNING id
            """,
            ("3" * 64, "4" * 64, "5" * 64, "6" * 64),
        ).fetchone()
        assert mismatch_run is not None
        with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
            connection.execute(
                """
                INSERT INTO model_predictions
                  (run_id,speaker_turn_id,record_id,prediction_task,prediction_values,
                   reasoning,validation_status)
                VALUES (%s,%s,'turn:wrong-task','hostility','{}','Wrong run task.','success')
                """,
                (mismatch_run["id"], turn["id"]),
            )

    with pytest.raises(RuntimeError, match="hostility predictions exist"):
        command.downgrade(config, "20261020_10")
    with psycopg.connect(url) as connection:
        connection.execute(
            "ALTER TABLE model_predictions DISABLE TRIGGER trg_model_predictions_append_only"
        )
        connection.execute("DELETE FROM model_predictions WHERE prediction_task='hostility'")
        connection.execute(
            "ALTER TABLE model_predictions ENABLE TRIGGER trg_model_predictions_append_only"
        )
        connection.execute(
            "ALTER TABLE model_prediction_runs DISABLE TRIGGER "
            "trg_model_prediction_runs_append_only"
        )
        connection.execute("DELETE FROM model_prediction_runs WHERE prediction_task='hostility'")
        connection.execute(
            "ALTER TABLE model_prediction_runs ENABLE TRIGGER "
            "trg_model_prediction_runs_append_only"
        )
    command.downgrade(config, "20261020_10")
    with psycopg.connect(url) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name='model_predictions'
                """
            )
        }
        assert "prediction_task" not in columns
        assert "prediction_values" not in columns
        assert connection.execute(
            "SELECT count(*) FROM model_predictions WHERE id=%s", (policy["id"],)
        ).fetchone()[0] == 1
    command.upgrade(config, "head")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        reupgraded = connection.execute(
            "SELECT prediction_task,prediction_values FROM model_predictions WHERE id=%s",
            (policy["id"],),
        ).fetchone()
        assert reupgraded is not None
        assert reupgraded["prediction_task"] == "policy"
        assert reupgraded["prediction_values"]["primary_australian_domain"] == "AU06"


def test_campaign_configuration_migration_round_trip_and_constraints(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HANSARD_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "20261027_11")
    web_settings = _web_settings(database_url)
    import_validated_run(
        web_settings.database,
        _fixture_run(tmp_path, run_id="campaign-config-migration"),
    )
    password = "campaign config migration password"
    create_user(
        web_settings,
        "campaign-migration-admin",
        "Campaign Migration Admin",
        password,
        admin=True,
        must_change_password=False,
    )
    admin, _ = authenticate(web_settings, "campaign-migration-admin", password)
    policy_config = production_configuration()
    url = database_url.replace("postgresql+psycopg://", "postgresql://")

    with psycopg.connect(url, row_factory=dict_row) as connection:
        pre12_columns = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name='ai_annotation_campaigns'
                """
            )
        }
        assert not (
            {
                "configuration_id",
                "prediction_task",
                "annotation_schema_slug",
                "annotation_schema_version",
                "record_set_id",
                "approved_record_set_sha256",
            }
            & pre12_columns
        )
        corpus = connection.execute("SELECT id FROM corpora LIMIT 1").fetchone()
        assert corpus is not None
        corpus_id = int(corpus["id"])
        turns = connection.execute(
            "SELECT id,turn_key FROM speaker_turns ORDER BY id LIMIT 3"
        ).fetchall()
        assert len(turns) == 3

        policy_run = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,prediction_task,annotation_schema_slug,
               annotation_schema_version,provider,configured_model,prompt_version,
               prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
               provider_schema_sha256,generation_config,execution_mode,
               record_set_sha256,requested_count,provider_success_count,
               provider_failure_count,valid_count,invalid_count,status,provider_job_id)
            VALUES (%s,'policy','australian_policy_annotation','0.2.0',%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,1,1,0,1,0,'imported','batches/legacy-policy-imported')
            RETURNING id
            """,
            (
                "1" * 64,
                policy_config["provider"],
                policy_config["configured_model"],
                policy_config["prompt_version"],
                policy_config["prompt_sha256"],
                policy_config["canonical_schema_sha256"],
                policy_config["prediction_contract_sha256"],
                policy_config["provider_schema_sha256"],
                Jsonb(policy_config["generation_config"]),
                policy_config["execution_mode"],
                "2" * 64,
            ),
        ).fetchone()
        assert policy_run is not None
        policy_run_id = int(policy_run["id"])

        draft_pre12 = connection.execute(
            """
            INSERT INTO ai_annotation_campaigns
              (name,corpus_id,created_by_user_id,status,provider,configured_model,
               prompt_version,prompt_sha256,canonical_schema_sha256,
               prediction_contract_sha256,provider_schema_sha256,generation_config,
               execution_mode,configuration_class)
            VALUES ('Legacy Policy Draft',%s,%s,'DRAFT',%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    'APPROVED_PRODUCTION')
            RETURNING id,public_id
            """,
            (
                corpus_id,
                admin.user_id,
                policy_config["provider"],
                policy_config["configured_model"],
                policy_config["prompt_version"],
                policy_config["prompt_sha256"],
                policy_config["canonical_schema_sha256"],
                policy_config["prediction_contract_sha256"],
                policy_config["provider_schema_sha256"],
                Jsonb(policy_config["generation_config"]),
                policy_config["execution_mode"],
            ),
        ).fetchone()
        ready_pre12 = connection.execute(
            """
            INSERT INTO ai_annotation_campaigns
              (name,corpus_id,created_by_user_id,status,provider,configured_model,
               prompt_version,prompt_sha256,canonical_schema_sha256,
               prediction_contract_sha256,provider_schema_sha256,generation_config,
               execution_mode,configuration_class,selection_definition,
               record_set_sha256,planned_count,planned_at)
            VALUES ('Legacy Policy Ready',%s,%s,'READY',%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    'APPROVED_PRODUCTION',%s,%s,2,now())
            RETURNING id,public_id
            """,
            (
                corpus_id,
                admin.user_id,
                policy_config["provider"],
                policy_config["configured_model"],
                policy_config["prompt_version"],
                policy_config["prompt_sha256"],
                policy_config["canonical_schema_sha256"],
                policy_config["prediction_contract_sha256"],
                policy_config["provider_schema_sha256"],
                Jsonb(policy_config["generation_config"]),
                policy_config["execution_mode"],
                Jsonb(
                    {
                        "eligibility_rule": "prompt_text_present_v1",
                        "exclude_human_annotated": True,
                        "seed": "legacy-ready-seed",
                        "limit": 2,
                    }
                ),
                "3" * 64,
            ),
        ).fetchone()
        imported_pre12 = connection.execute(
            """
            INSERT INTO ai_annotation_campaigns
              (name,corpus_id,created_by_user_id,status,provider,configured_model,
               prompt_version,prompt_sha256,canonical_schema_sha256,
               prediction_contract_sha256,provider_schema_sha256,generation_config,
               execution_mode,configuration_class,selection_definition,
               record_set_sha256,planned_count,provider_job_id,provider_state,
               model_prediction_run_id,planned_at,submitted_at,imported_at)
            VALUES ('Legacy Policy Imported',%s,%s,'IMPORTED',%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,'APPROVED_PRODUCTION',%s,%s,1,'batches/legacy-policy-imported',
                    'BATCH_STATE_SUCCEEDED',%s,now(),now(),now())
            RETURNING id,public_id
            """,
            (
                corpus_id,
                admin.user_id,
                policy_config["provider"],
                policy_config["configured_model"],
                policy_config["prompt_version"],
                policy_config["prompt_sha256"],
                policy_config["canonical_schema_sha256"],
                policy_config["prediction_contract_sha256"],
                policy_config["provider_schema_sha256"],
                Jsonb(policy_config["generation_config"]),
                policy_config["execution_mode"],
                Jsonb(
                    {
                        "eligibility_rule": "prompt_text_present_v1",
                        "exclude_human_annotated": False,
                        "seed": "legacy-imported-seed",
                        "limit": 1,
                    }
                ),
                "2" * 64,
                policy_run_id,
            ),
        ).fetchone()
        assert draft_pre12 is not None
        assert ready_pre12 is not None
        assert imported_pre12 is not None
        for index, turn in enumerate(turns[:2]):
            connection.execute(
                """
                INSERT INTO ai_annotation_campaign_records
                  (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    ready_pre12["id"],
                    turn["id"],
                    f"turn:{turn['turn_key']}",
                    index,
                    "4" * 64,
                ),
            )
        connection.execute(
            """
            INSERT INTO ai_annotation_campaign_records
              (campaign_id,speaker_turn_id,record_id,selection_order,record_sha256)
            VALUES (%s,%s,%s,0,%s)
            """,
            (
                imported_pre12["id"],
                turns[2]["id"],
                f"turn:{turns[2]['turn_key']}",
                "5" * 64,
            ),
        )

    command.upgrade(config, "20261027_12")

    with psycopg.connect(url, row_factory=dict_row) as connection:
        post12_columns = {
            row["column_name"]: (row["is_nullable"], row["column_default"])
            for row in connection.execute(
                """
                SELECT column_name,is_nullable,column_default
                FROM information_schema.columns
                WHERE table_name='ai_annotation_campaigns'
                  AND column_name IN (
                    'configuration_id','prediction_task','annotation_schema_slug',
                    'annotation_schema_version','record_set_id',
                    'approved_record_set_sha256'
                  )
                """
            )
        }
        assert set(post12_columns) == {
            "configuration_id",
            "prediction_task",
            "annotation_schema_slug",
            "annotation_schema_version",
            "record_set_id",
            "approved_record_set_sha256",
        }
        assert post12_columns["configuration_id"][0] == "NO"
        assert post12_columns["prediction_task"][0] == "NO"
        assert post12_columns["annotation_schema_slug"][0] == "NO"
        assert post12_columns["annotation_schema_version"][0] == "NO"
        assert post12_columns["record_set_id"][0] == "YES"
        assert post12_columns["approved_record_set_sha256"][0] == "YES"

        constraints = {
            row["conname"]
            for row in connection.execute(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid='ai_annotation_campaigns'::regclass
                  AND conname IN (
                    'ai_campaign_configuration_id_check',
                    'ai_campaign_prediction_task_check',
                    'ai_campaign_configuration_shape_check'
                  )
                """
            )
        }
        assert constraints == {
            "ai_campaign_configuration_id_check",
            "ai_campaign_prediction_task_check",
            "ai_campaign_configuration_shape_check",
        }

        for expected_id, expected_status in (
            (draft_pre12["id"], "DRAFT"),
            (ready_pre12["id"], "READY"),
            (imported_pre12["id"], "IMPORTED"),
        ):
            migrated_row = connection.execute(
                "SELECT * FROM ai_annotation_campaigns WHERE id=%s",
                (expected_id,),
            ).fetchone()
            assert migrated_row is not None
            assert migrated_row["status"] == expected_status
            assert migrated_row["configuration_id"] == "policy-v2"
            assert migrated_row["prediction_task"] == "policy"
            assert (
                migrated_row["annotation_schema_slug"]
                == "australian_policy_annotation"
            )
            assert migrated_row["annotation_schema_version"] == "0.2.0"
            assert migrated_row["record_set_id"] is None
            assert migrated_row["approved_record_set_sha256"] is None
            assert migrated_row["configured_model"] == policy_config["configured_model"]
            assert migrated_row["prompt_version"] == policy_config["prompt_version"]
            assert migrated_row["prompt_sha256"] == policy_config["prompt_sha256"]

    for item in (draft_pre12, ready_pre12, imported_pre12):
        detail = get_campaign(web_settings, admin, str(item["public_id"]))
        assert detail["configuration_id"] == "policy-v2"
        assert detail["configuration_display_label"] == "Policy classification — Frozen V2"
        assert detail["prediction_task"] == "policy"
        assert detail["annotation_schema_slug"] == "australian_policy_annotation"
        assert detail["annotation_schema_version"] == "0.2.0"
        assert detail["record_set_mode"] == "deterministic_selection"

    # Pre-submission lifecycle on migrated DRAFT policy campaign works normally
    planned_legacy = plan_campaign(
        web_settings,
        admin,
        str(draft_pre12["public_id"]),
        limit=2,
        seed="post-12-plan-seed",
        exclude_human_annotated=True,
    ).campaign
    assert planned_legacy["status"] == "PLANNED"
    assert planned_legacy["planned_count"] == 2
    ready_legacy = transition_campaign(
        web_settings, admin, str(draft_pre12["public_id"]), "READY"
    )
    assert ready_legacy["status"] == "READY"

    # Create representative Hostility V2 campaign through normal service path
    hostility_campaign = create_campaign(
        web_settings,
        admin,
        name="Hostility V2 Held-Out Validation",
        corpus_id=corpus_id,
        configuration_id="hostility-v2",
    )
    expected_sample_sha = (
        "f3d9451b3352025b769d263fc896f57683a741ea6fd9af0b6477651707cf3b36"
    )
    assert hostility_campaign["configuration_id"] == "hostility-v2"
    assert hostility_campaign["prediction_task"] == "hostility"
    assert (
        hostility_campaign["annotation_schema_slug"]
        == "australian_partisan_hostility"
    )
    assert hostility_campaign["annotation_schema_version"] == "0.2.0"
    assert hostility_campaign["record_set_id"] == "hostility-heldout-100"
    assert hostility_campaign["approved_record_set_sha256"] == expected_sample_sha
    assert hostility_campaign["provider"] == "gemini"
    assert hostility_campaign["configured_model"] == "gemini-2.5-flash"
    assert hostility_campaign["prompt_version"] == "hostility-prompt-v2"
    assert (
        hostility_campaign["prompt_sha256"]
        == "185f5162348ae3b3d735ea43247da18cf6b3c1bb12f8a72cad57e38ce31cb1a8"
    )
    assert (
        hostility_campaign["prediction_contract_sha256"]
        == "dc716b49e0759ba7063973038b234f6696e778c4d90ee4a405d9600a2435b11d"
    )
    assert hostility_campaign["generation_config"] == {"thinking_budget": 0}
    hostility_detail = get_campaign(
        web_settings, admin, str(hostility_campaign["public_id"])
    )
    assert (
        hostility_detail["configuration_display_label"]
        == "Targeted hostility — Frozen V2"
    )
    assert hostility_detail["record_set_mode"] == "frozen_sample"
    assert hostility_detail["approved_record_set_size"] == 100
    assert len(list_campaigns(web_settings, admin)) == 4

    # Constraint rejections
    invalid_shapes: tuple[tuple[str, str, str, str, str | None, str | None], ...] = (
        (
            "unknown-v2",
            "policy",
            "australian_policy_annotation",
            "0.2.0",
            None,
            None,
        ),
        (
            "hostility-v2",
            "policy",
            "australian_partisan_hostility",
            "0.2.0",
            "hostility-heldout-100",
            expected_sample_sha,
        ),
        (
            "hostility-v2",
            "hostility",
            "australian_policy_annotation",
            "0.2.0",
            "hostility-heldout-100",
            expected_sample_sha,
        ),
        (
            "hostility-v2",
            "hostility",
            "australian_partisan_hostility",
            "0.1.0",
            "hostility-heldout-100",
            expected_sample_sha,
        ),
        (
            "hostility-v2",
            "hostility",
            "australian_partisan_hostility",
            "0.2.0",
            "hostility-development-100",
            expected_sample_sha,
        ),
        (
            "hostility-v2",
            "hostility",
            "australian_partisan_hostility",
            "0.2.0",
            "hostility-heldout-100",
            "0" * 64,
        ),
        (
            "policy-v2",
            "hostility",
            "australian_policy_annotation",
            "0.2.0",
            None,
            None,
        ),
        (
            "policy-v2",
            "policy",
            "australian_partisan_hostility",
            "0.2.0",
            None,
            None,
        ),
        (
            "policy-v2",
            "policy",
            "australian_policy_annotation",
            "0.2.0",
            "hostility-heldout-100",
            None,
        ),
        (
            "policy-v2",
            "policy",
            "australian_policy_annotation",
            "0.2.0",
            None,
            expected_sample_sha,
        ),
    )
    with psycopg.connect(url, row_factory=dict_row) as connection:
        for (
            cfg_id,
            task,
            schema_slug,
            schema_ver,
            rec_set_id,
            rec_set_sha,
        ) in invalid_shapes:
            with pytest.raises(
                psycopg.errors.CheckViolation
            ), connection.transaction():
                connection.execute(
                    """
                    INSERT INTO ai_annotation_campaigns
                      (name,corpus_id,created_by_user_id,configuration_id,
                       prediction_task,annotation_schema_slug,
                       annotation_schema_version,record_set_id,
                       approved_record_set_sha256,provider,configured_model,
                       prompt_version,prompt_sha256,canonical_schema_sha256,
                       prediction_contract_sha256,provider_schema_sha256,
                       generation_config,execution_mode,configuration_class)
                    VALUES ('Invalid Shape',%s,%s,%s,%s,%s,%s,%s,%s,'gemini',
                            'gemini-2.5-flash','hostility-prompt-v2',%s,%s,%s,%s,
                            '{"thinking_budget":0}'::jsonb,'batch','APPROVED_PRODUCTION')
                    """,
                    (
                        corpus_id,
                        admin.user_id,
                        cfg_id,
                        task,
                        schema_slug,
                        schema_ver,
                        rec_set_id,
                        rec_set_sha,
                        hostility_campaign["prompt_sha256"],
                        hostility_campaign["canonical_schema_sha256"],
                        hostility_campaign["prediction_contract_sha256"],
                        hostility_campaign["provider_schema_sha256"],
                    ),
                )

        # Provenance immutability lifecycle boundaries on READY -> SUBMITTED -> IMPORTED
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET
              cache_status='REUSABLE',cache_identifier='cachedContents/ready-cache',
              cache_content_sha256=%s,cache_model='models/gemini-3.1-flash-lite',
              cache_display_name='phase4d-ready-cache',cache_created_at=now(),
              cache_expires_at=now()+interval '24 hours',cache_verified_at=now()
            WHERE id=%s AND status='READY'
            """,
            (policy_config["prompt_sha256"], draft_pre12["id"]),
        )
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET
              status='SUBMITTED',provider_job_id='batches/submitted-policy-001',
              provider_state='BATCH_STATE_PENDING',submitted_at=now()
            WHERE id=%s AND status='READY'
            """,
            (draft_pre12["id"],),
        )

        # Post-submission provenance mutations are rejected
        for mutation_sql, params in (
            (
                """
                UPDATE ai_annotation_campaigns SET
                  configuration_id='hostility-v2',prediction_task='hostility',
                  annotation_schema_slug='australian_partisan_hostility',
                  annotation_schema_version='0.2.0',
                  record_set_id='hostility-heldout-100',
                  approved_record_set_sha256=%s
                WHERE id=%s
                """,
                (expected_sample_sha, draft_pre12["id"]),
            ),
            (
                "UPDATE ai_annotation_campaigns SET configured_model='gemini-2.5-flash' WHERE id=%s",
                (draft_pre12["id"],),
            ),
            (
                "UPDATE ai_annotation_campaigns SET prompt_version='hostility-prompt-v2' WHERE id=%s",
                (draft_pre12["id"],),
            ),
            (
                "UPDATE ai_annotation_campaigns SET prompt_sha256=%s WHERE id=%s",
                ("9" * 64, draft_pre12["id"]),
            ),
            (
                "UPDATE ai_annotation_campaigns SET prediction_contract_sha256=%s WHERE id=%s",
                ("8" * 64, draft_pre12["id"]),
            ),
            (
                "UPDATE ai_annotation_campaigns SET record_set_sha256=%s WHERE id=%s",
                ("7" * 64, draft_pre12["id"]),
            ),
            (
                "UPDATE ai_annotation_campaigns SET planned_count=99 WHERE id=%s",
                (draft_pre12["id"],),
            ),
            (
                "UPDATE ai_annotation_campaigns SET cache_identifier='cachedContents/mutated' WHERE id=%s",
                (draft_pre12["id"],),
            ),
        ):
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="submitted campaign provenance is immutable",
            ), connection.transaction():
                connection.execute(mutation_sql, params)

        with pytest.raises(
            psycopg.errors.RaiseException,
            match="campaign provider job identity is immutable",
        ), connection.transaction():
            connection.execute(
                "UPDATE ai_annotation_campaigns SET provider_job_id='batches/other' WHERE id=%s",
                (draft_pre12["id"],),
            )

        # Normal operational status transitions after submission remain allowed
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='RUNNING',
              provider_state='BATCH_STATE_RUNNING' WHERE id=%s
            """,
            (draft_pre12["id"],),
        )
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='PROVIDER_COMPLETED',
              provider_state='BATCH_STATE_SUCCEEDED',provider_success_count=2,
              provider_completed_at=now() WHERE id=%s
            """,
            (draft_pre12["id"],),
        )
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='COLLECTED',
              artifact_reference='/tmp/collected',artifact_sha256=%s,
              valid_count=2,invalid_count=0,collected_at=now() WHERE id=%s
            """,
            ("6" * 64, draft_pre12["id"]),
        )
        connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='VALIDATED',
              validated_at=now() WHERE id=%s
            """,
            (draft_pre12["id"],),
        )

    # Downgrade guard refuses while hostility campaign exists
    with pytest.raises(RuntimeError, match="downgrade refused: hostility campaigns exist"):
        command.downgrade(config, "20261027_11")

    with psycopg.connect(url) as connection:
        connection.execute(
            "DELETE FROM ai_annotation_campaigns WHERE id=%s",
            (hostility_campaign["id"],),
        )

    command.downgrade(config, "20261027_11")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        downgraded_columns = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name='ai_annotation_campaigns'
                """
            )
        }
        assert not (
            {
                "configuration_id",
                "prediction_task",
                "annotation_schema_slug",
                "annotation_schema_version",
                "record_set_id",
                "approved_record_set_sha256",
            }
            & downgraded_columns
        )
        downgraded_constraints = {
            row["conname"]
            for row in connection.execute(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid='ai_annotation_campaigns'::regclass
                  AND conname IN (
                    'ai_campaign_configuration_id_check',
                    'ai_campaign_prediction_task_check',
                    'ai_campaign_configuration_shape_check'
                  )
                """
            )
        }
        assert not downgraded_constraints
        surviving = connection.execute(
            "SELECT id,status FROM ai_annotation_campaigns ORDER BY id"
        ).fetchall()
        assert [(row["id"], row["status"]) for row in surviving] == [
            (draft_pre12["id"], "VALIDATED"),
            (ready_pre12["id"], "READY"),
            (imported_pre12["id"], "IMPORTED"),
        ]
        # Restored trigger at 20261027_11 still protects submitted campaigns
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="submitted campaign provenance is immutable",
        ), connection.transaction():
            connection.execute(
                "UPDATE ai_annotation_campaigns SET prompt_sha256=%s WHERE id=%s",
                ("a" * 64, draft_pre12["id"]),
            )

    command.upgrade(config, "head")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        reupgraded_rows = connection.execute(
            """
            SELECT id,configuration_id,prediction_task,annotation_schema_slug,
                   annotation_schema_version,record_set_id,approved_record_set_sha256
            FROM ai_annotation_campaigns ORDER BY id
            """
        ).fetchall()
        assert len(reupgraded_rows) == 3
        for row in reupgraded_rows:
            assert row["configuration_id"] == "policy-v2"
            assert row["prediction_task"] == "policy"
            assert row["annotation_schema_slug"] == "australian_policy_annotation"
            assert row["annotation_schema_version"] == "0.2.0"
            assert row["record_set_id"] is None
            assert row["approved_record_set_sha256"] is None


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
    annotator_b, annotator_b_token = authenticate(settings, "annotator-b", password)
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

    browser_assignment_id = int(claims[1]["id"])
    browser_payload = {
        "csrf_token": annotator_b.csrf_token,
        "expected_revision_number": "0",
        "content_status": "substantive_policy",
        "primary_australian_domain": "AU03",
        "secondary_australian_domains": "AU05",
        "specific_australian_issue": "Medicare",
        "topic_uncertain": "false",
        "fallback_explanation": "",
        "unclassifiable_reason": "",
        "annotation_notes": "First browser submission",
    }
    with TestClient(create_app(settings)) as client:
        client.cookies.set(SESSION_COOKIE, annotator_b_token)
        workspace = client.get(f"/assignments/{browser_assignment_id}")
        assert workspace.status_code == 200
        assert 'name="expected_revision_number" value="0"' in workspace.text
        assert "Flag codebook issue" not in workspace.text
        assert "Possible taxonomy/codebook ambiguity" not in workspace.text
        assert "Save draft" in workspace.text
        assert "Annotation notes" in workspace.text
        assert "Revision history" in workspace.text

        first_submission = client.post(
            f"/assignments/{browser_assignment_id}/submit",
            data=browser_payload,
            follow_redirects=False,
        )
        assert first_submission.status_code == 303
        assert first_submission.headers["location"].startswith("/assignments/")
        next_assignment_id = int(
            first_submission.headers["location"].split("/assignments/", 1)[1].split("?", 1)[0]
        )

        submitted_workspace = client.get(f"/assignments/{browser_assignment_id}")
        assert submitted_workspace.status_code == 200
        assert 'name="expected_revision_number" value="1"' in submitted_workspace.text
        assert "Submit revision &amp; next" in submitted_workspace.text
        assert "Save draft" not in submitted_workspace.text

        revision_payload = {
            **browser_payload,
            "expected_revision_number": "1",
            "annotation_notes": "Explicit browser revision",
        }
        revision = client.post(
            f"/assignments/{browser_assignment_id}/submit",
            data=revision_payload,
            follow_redirects=False,
        )
        assert revision.status_code == 303
        assert revision.headers["location"].startswith(
            f"/assignments/{next_assignment_id}"
        )

        stale_revision = client.post(
            f"/assignments/{browser_assignment_id}/submit",
            data=revision_payload,
            follow_redirects=False,
        )
        assert stale_revision.status_code == 409
        assert "changed after the form was loaded" in stale_revision.text

        malformed_revision = client.post(
            f"/assignments/{browser_assignment_id}/submit",
            data={**browser_payload, "expected_revision_number": "not-a-revision"},
            follow_redirects=False,
        )
        assert malformed_revision.status_code == 422
        assert "missing or malformed" in malformed_revision.text

        history_page = client.get(f"/assignments/{browser_assignment_id}/history")
        assert history_page.status_code == 200
        assert "Revision 2" in history_page.text
        assert "Revision 1" in history_page.text

        completed_detail = client.get(f"/assignments/{browser_assignment_id}/detail")
        assert completed_detail.status_code == 200
        assert "Completed Human Annotation" in completed_detail.text
        assert "Policy" in completed_detail.text
        assert "AU03 — Health" in completed_detail.text
        assert "First browser submission" in completed_detail.text
        assert "Explicit browser revision" in completed_detail.text
        assert "Revise Annotation" in completed_detail.text

        with TestClient(create_app(settings)) as other_client:
            other_client.cookies.set(SESSION_COOKIE, authenticate(
                settings, "annotator-a", password
            )[1])
            assert (
                other_client.get(f"/assignments/{browser_assignment_id}/detail").status_code
                == 404
            )

        landing = client.get("/projects")
        assert landing.status_code == 200
        for label in ("To Annotate", "Draft", "Completed", "Skipped", "Total"):
            assert label in landing.text

        overview = client.get(f"/projects/{project_id}?status=completed")
        assert overview.status_code == 200
        assert "Browse annotation work" in overview.text
        assert "To Annotate" in overview.text
        assert "In Progress / Draft" in overview.text
        assert "Completed" in overview.text
        assert "Skipped" in overview.text
        assert "First browser submission" not in overview.text
        assert f"/assignments/{browser_assignment_id}/detail" in overview.text
        assert "Flag codebook issue" not in overview.text

        to_annotate = client.get(f"/projects/{project_id}?status=to_annotate")
        assert to_annotate.status_code == 200
        assert f"/assignments/{next_assignment_id}" in to_annotate.text
        filtered_completed = client.get(
            f"/projects/{project_id}?status=completed&policy_status=policy"
            "&primary_domain=AU03"
        )
        assert f"/assignments/{browser_assignment_id}/detail" in filtered_completed.text

        draft_payload = {
            **browser_payload,
            "expected_revision_number": "0",
            "annotation_notes": "Pending draft",
        }
        draft_response = client.post(
            f"/assignments/{next_assignment_id}/draft",
            data=draft_payload,
            follow_redirects=False,
        )
        assert draft_response.status_code == 303
        assert draft_response.headers["location"] == (
            f"/assignments/{next_assignment_id}?saved=1"
        )
        draft_page = client.get(draft_response.headers["location"])
        assert "Draft saved. You remain on this annotation." in draft_page.text
        in_progress = client.get(f"/projects/{project_id}?status=in_progress")
        assert in_progress.status_code == 200
        assert "Pending draft" not in in_progress.text
        assert f"/assignments/{next_assignment_id}" in in_progress.text

        first_skip = client.post(
            f"/assignments/{next_assignment_id}/skip",
            data={"csrf_token": annotator_b.csrf_token},
            follow_redirects=False,
        )
        assert first_skip.status_code == 303
        assert first_skip.headers["location"].startswith("/assignments/")
        final_assignment_id = int(
            first_skip.headers["location"].split("/assignments/", 1)[1].split("?", 1)[0]
        )
        assert final_assignment_id != next_assignment_id

        final_skip = client.post(
            f"/assignments/{final_assignment_id}/skip",
            data={"csrf_token": annotator_b.csrf_token},
            follow_redirects=False,
        )
        assert final_skip.status_code == 303
        assert final_skip.headers["location"] == (
            f"/projects/{project_id}?queue_complete=1&status=skipped"
        )
        skipped_list = client.get(final_skip.headers["location"])
        assert "No more available Human Annotation tasks." in skipped_list.text
        assert skipped_list.text.count("Annotate now") == 2

        reopen = client.post(
            f"/assignments/{next_assignment_id}/reopen",
            data={"csrf_token": annotator_b.csrf_token},
            follow_redirects=False,
        )
        assert reopen.status_code == 303
        assert reopen.headers["location"] == f"/assignments/{next_assignment_id}?reopened=1"
        reopened_page = client.get(reopen.headers["location"])
        assert "Skipped annotation reopened." in reopened_page.text
        assert "Pending draft" in reopened_page.text

        recovered_submission = client.post(
            f"/assignments/{next_assignment_id}/submit",
            data={**draft_payload, "expected_revision_number": "1"},
            follow_redirects=False,
        )
        assert recovered_submission.status_code == 303
        assert recovered_submission.headers["location"] == (
            f"/projects/{project_id}?queue_complete=1"
        )
        completed_list = client.get(f"/projects/{project_id}?status=completed")
        assert f"/assignments/{next_assignment_id}/detail" in completed_list.text
        remaining_skipped = client.get(f"/projects/{project_id}?status=skipped")
        assert f"/assignments/{final_assignment_id}/reopen" in remaining_skipped.text

    paged = human_annotation_work(
        settings,
        annotator_b,
        project_id,
        status="all",
        page=1,
        page_size=1,
    )
    second_page = human_annotation_work(
        settings,
        annotator_b,
        project_id,
        status="all",
        page=2,
        page_size=1,
    )
    assert paged["page_count"] == 3
    assert len(paged["rows"]) == len(second_page["rows"]) == 1
    assert paged["rows"][0]["assignment_id"] != second_page["rows"][0]["assignment_id"]
    assert paged["counts"] == {
        "total": 3,
        "to_annotate": 0,
        "in_progress": 0,
        "completed": 2,
        "skipped": 1,
    }

    with psycopg.connect(url, row_factory=dict_row) as connection:
        browser_versions = connection.execute(
            """
            SELECT av.revision_number,av.event_type,av.values->>'annotation_notes' AS note
            FROM annotations an JOIN annotation_versions av ON av.annotation_id=an.id
            WHERE an.assignment_id=%s ORDER BY av.revision_number
            """,
            (browser_assignment_id,),
        ).fetchall()
    assert [row["event_type"] for row in browser_versions] == ["submitted", "revised"]
    assert [row["revision_number"] for row in browser_versions] == [1, 2]
    assert [row["note"] for row in browser_versions] == [
        "First browser submission",
        "Explicit browser revision",
    ]

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
        assert "Create a pinned Human Annotation Workspace" in new_project_page.text
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
