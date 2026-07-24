"""Safe Phase 2.5 product registry and seed CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.adapters import get_adapter, list_adapters
from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.product.loaders import (
    load_annotation_schema,
    load_taxonomy,
)
from hansard_annotator.product.seeds import (
    load_annotation_schema_seed,
    load_taxonomy_seed,
)
from hansard_annotator.product.verification import verify_product_foundation


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str))


def _parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hansard-product")
    groups = result.add_subparsers(dest="group", required=True)
    adapters = groups.add_parser("adapters").add_subparsers(
        dest="action", required=True
    )
    adapters.add_parser("list")
    adapter_show = adapters.add_parser("show")
    adapter_show.add_argument("key")
    corpora = groups.add_parser("corpora").add_subparsers(
        dest="action", required=True
    )
    corpora.add_parser("list")
    corpus_show = corpora.add_parser("show")
    corpus_show.add_argument("slug")
    for group_name in ("taxonomy", "schema"):
        commands = groups.add_parser(group_name).add_subparsers(
            dest="action", required=True
        )
        for action in ("validate", "load"):
            command_parser = commands.add_parser(action)
            command_parser.add_argument("path", type=Path)
        commands.add_parser("list")
        show = commands.add_parser("show")
        show.add_argument("slug")
        show.add_argument("--version")
    groups.add_parser("verify")
    return result


def _adapter_metadata(adapter: Any) -> dict[str, Any]:
    return {
        "adapter_key": adapter.adapter_key,
        "adapter_version": adapter.adapter_version,
        "source_format_key": adapter.source_format_key,
        "source_format_version": adapter.source_format_version,
        "supported_languages": adapter.supported_languages,
        "supported_chambers": adapter.supported_chambers,
        "capabilities": vars(adapter.describe_capabilities()),
        "provenance": adapter.describe_provenance(),
    }


def _database_query(
    settings: DatabaseSettings, sql: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.group == "adapters":
            if arguments.action == "list":
                _print([_adapter_metadata(adapter) for adapter in list_adapters()])
            else:
                _print(_adapter_metadata(get_adapter(arguments.key)))
            return 0

        settings = DatabaseSettings.from_environment()
        if arguments.group == "corpora":
            where = "" if arguments.action == "list" else " WHERE slug=%s"
            parameters = () if arguments.action == "list" else (arguments.slug,)
            rows = _database_query(
                settings,
                """
                SELECT public_id,slug,name,jurisdiction,legislature,chamber,
                  language_code,source_format_key,licence_status,
                  redistribution_status,date_from,date_to,is_public,is_active,
                  current_preprocessing_run_id
                FROM corpora
                """
                + where
                + " ORDER BY slug",
                parameters,
            )
            if arguments.action == "show" and not rows:
                raise ValueError(f"unknown corpus: {arguments.slug}")
            _print(rows if arguments.action == "list" else rows[0])
            return 0
        if arguments.group == "taxonomy":
            if arguments.action == "validate":
                taxonomy_seed = load_taxonomy_seed(arguments.path)
                _print(
                    {
                        "ok": True,
                        "slug": taxonomy_seed.slug,
                        "version": taxonomy_seed.version,
                        "label_count": len(taxonomy_seed.labels),
                        "content_sha256": taxonomy_seed.content_sha256,
                        "database_writes": 0,
                    }
                )
            elif arguments.action == "load":
                _print(
                    load_taxonomy(
                        settings, arguments.path, repository_root=Path.cwd()
                    )
                )
            else:
                where = "" if arguments.action == "list" else " WHERE t.slug=%s"
                parameters = () if arguments.action == "list" else (arguments.slug,)
                version_filter = (
                    ""
                    if arguments.action == "list" or arguments.version is None
                    else " AND tv.version=%s"
                )
                if version_filter:
                    parameters += (arguments.version,)
                rows = _database_query(
                    settings,
                    """
                    SELECT t.slug,t.name,t.taxonomy_type,t.jurisdiction,
                      tv.version,tv.status,tv.content_sha256,tv.source_seed_path,
                      count(label.id) AS label_count
                    FROM taxonomies t JOIN taxonomy_versions tv ON tv.taxonomy_id=t.id
                    LEFT JOIN taxonomy_labels label ON label.taxonomy_version_id=tv.id
                    """
                    + where
                    + version_filter
                    + " GROUP BY t.id,tv.id ORDER BY t.slug,tv.version",
                    parameters,
                )
                if arguments.action == "show" and not rows:
                    raise ValueError(f"unknown taxonomy: {arguments.slug}")
                _print(rows if arguments.action == "list" else rows[0])
            return 0
        if arguments.group == "schema":
            if arguments.action == "validate":
                schema_seed = load_annotation_schema_seed(arguments.path)
                _print(
                    {
                        "ok": True,
                        "slug": schema_seed.slug,
                        "version": schema_seed.version,
                        "field_count": len(schema_seed.fields),
                        "content_sha256": schema_seed.content_sha256,
                        "database_writes": 0,
                    }
                )
            elif arguments.action == "load":
                _print(
                    load_annotation_schema(
                        settings, arguments.path, repository_root=Path.cwd()
                    )
                )
            else:
                where = "" if arguments.action == "list" else " WHERE s.slug=%s"
                parameters = () if arguments.action == "list" else (arguments.slug,)
                version_filter = (
                    ""
                    if arguments.action == "list" or arguments.version is None
                    else " AND sv.version=%s"
                )
                if version_filter:
                    parameters += (arguments.version,)
                rows = _database_query(
                    settings,
                    """
                    SELECT s.slug,s.name,s.schema_type,sv.version,sv.status,
                      sv.content_sha256,sv.source_seed_path,count(field.id) AS field_count
                    FROM annotation_schemas s
                    JOIN annotation_schema_versions sv ON sv.annotation_schema_id=s.id
                    LEFT JOIN annotation_field_definitions field
                      ON field.annotation_schema_version_id=sv.id
                    """
                    + where
                    + version_filter
                    + " GROUP BY s.id,sv.id ORDER BY s.slug,sv.version",
                    parameters,
                )
                if arguments.action == "show" and not rows:
                    raise ValueError(f"unknown annotation schema: {arguments.slug}")
                _print(rows if arguments.action == "list" else rows[0])
            return 0
        with psycopg.connect(
            settings.psycopg_url, row_factory=dict_row
        ) as connection:
            _print(verify_product_foundation(connection))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
