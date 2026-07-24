"""Explicit Phase 2 database command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import text

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.db.engine import create_database_engine
from hansard_annotator.db.importer import import_validated_run
from hansard_annotator.db.queries import (
    continuation_anomalies,
    corpus_summary,
    inspect_fragment,
    inspect_turn,
    sample_turns,
    unlinked_interjections,
)
from hansard_annotator.db.validator import validate_run_directory
from hansard_annotator.db.verification import database_sizes, verify_database

DEFAULT_RUN = Path("data/processed/phase1-full-20260723-v4")


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hansard-db")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("connection-test")
    commands.add_parser("migration-status")
    commands.add_parser("migrate")
    dry = commands.add_parser("dry-run")
    dry.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    load = commands.add_parser("import-run")
    load.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    verify = commands.add_parser("verify")
    verify.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    commands.add_parser("summary")
    sizes = commands.add_parser("sizes")
    sizes.set_defaults()
    turn = commands.add_parser("inspect-turn")
    turn.add_argument("turn_key")
    turn.add_argument("--full-text", action="store_true")
    fragment = commands.add_parser("inspect-fragment")
    fragment.add_argument("fragment_key")
    fragment.add_argument("--full-text", action="store_true")
    anomalies = commands.add_parser("continuation-anomalies")
    anomalies.add_argument("--limit", type=int, default=50)
    unlinked = commands.add_parser("unlinked-interjections")
    unlinked.add_argument("--limit", type=int, default=50)
    sample = commands.add_parser("sample-turns")
    sample.add_argument("year", type=int)
    sample.add_argument("--limit", type=int, default=10)
    return result


def _accepted_run_id(connection: psycopg.Connection[Any]) -> int:
    row = connection.execute(
        "SELECT id FROM preprocessing_runs WHERE status='accepted' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("no accepted preprocessing run is installed")
    return int(row["id"] if isinstance(row, dict) else row[0])


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        settings = DatabaseSettings.from_environment()
        if arguments.command == "connection-test":
            with psycopg.connect(settings.psycopg_url) as connection:
                version = connection.execute(
                    "SELECT current_database(),current_setting('server_version')"
                ).fetchone()
            assert version is not None
            _json({"ok": True, "database": version[0], "server_version": version[1]})
            return 0
        if arguments.command == "migrate":
            command.upgrade(Config("alembic.ini"), "head")
            _json({"ok": True, "revision": "head"})
            return 0
        if arguments.command == "migration-status":
            engine = create_database_engine(settings)
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
            _json({"revision": revision})
            return 0
        if arguments.command == "dry-run":
            validated = validate_run_directory(
                arguments.run_dir, settings.processed_data_root
            )
            _json(
                {
                    "ok": True,
                    "run_id": validated.manifest["run_id"],
                    "manifest_sha256": validated.manifest_sha256,
                    "artefact_count": validated.artefact_count,
                    "artefact_bytes": validated.artefact_bytes,
                    "dataset_rows": validated.dataset_rows,
                    "database_writes": 0,
                }
            )
            return 0
        if arguments.command == "import-run":
            validated = validate_run_directory(
                arguments.run_dir, settings.processed_data_root
            )
            _json(import_validated_run(settings, validated))
            return 0
        if arguments.command == "verify":
            validated = validate_run_directory(
                arguments.run_dir, settings.processed_data_root
            )
            with psycopg.connect(
                settings.psycopg_url, row_factory=dict_row
            ) as connection:
                _json(verify_database(connection, _accepted_run_id(connection), validated))
            return 0

        engine = create_database_engine(settings)
        if arguments.command == "summary":
            _json(corpus_summary(engine))
        elif arguments.command == "sizes":
            with psycopg.connect(
                settings.psycopg_url, row_factory=dict_row
            ) as connection:
                _json(database_sizes(connection))
        elif arguments.command == "inspect-turn":
            _json(inspect_turn(engine, arguments.turn_key, include_full_text=arguments.full_text))
        elif arguments.command == "inspect-fragment":
            _json(
                inspect_fragment(
                    engine, arguments.fragment_key, include_full_text=arguments.full_text
                )
            )
        elif arguments.command == "continuation-anomalies":
            _json(continuation_anomalies(engine, limit=arguments.limit))
        elif arguments.command == "unlinked-interjections":
            _json(unlinked_interjections(engine, limit=arguments.limit))
        elif arguments.command == "sample-turns":
            _json(sample_turns(engine, arguments.year, limit=arguments.limit))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
