"""Transactional deterministic taxonomy and annotation-schema seed loaders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.product.seeds import (
    SeedConflictError,
    load_annotation_schema_seed,
    load_taxonomy_seed,
)


def repository_seed_path(path: Path, repository_root: Path) -> str:
    resolved = path.resolve(strict=True)
    root = repository_root.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise SeedConflictError("seed path must be inside the repository")
    return resolved.relative_to(root).as_posix()


def _first(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def load_taxonomy(
    settings: DatabaseSettings,
    path: Path,
    *,
    repository_root: Path,
) -> dict[str, Any]:
    seed = load_taxonomy_seed(path)
    source_path = repository_seed_path(path, repository_root)
    with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
        taxonomy = connection.execute(
            "SELECT * FROM taxonomies WHERE slug=%s", (seed.slug,)
        ).fetchone()
        if taxonomy is None:
            taxonomy_id = int(
                _first(
                    connection.execute(
                        """
                        INSERT INTO taxonomies
                          (slug,name,description,taxonomy_type,jurisdiction,
                           language_code,owner_scope)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
                        """,
                        (
                            seed.slug,
                            seed.name,
                            seed.description,
                            seed.taxonomy_type,
                            seed.jurisdiction,
                            seed.language_code,
                            seed.owner_scope,
                        ),
                    ).fetchone()
                )
            )
        else:
            taxonomy_id = int(taxonomy["id"])
            expected = (
                seed.name,
                seed.description,
                seed.taxonomy_type,
                seed.jurisdiction,
                seed.language_code,
                seed.owner_scope,
            )
            actual = (
                taxonomy["name"],
                taxonomy["description"],
                taxonomy["taxonomy_type"],
                taxonomy["jurisdiction"],
                taxonomy["language_code"],
                taxonomy["owner_scope"],
            )
            if actual != expected:
                raise SeedConflictError(
                    f"taxonomy identity differs for existing slug: {seed.slug}"
                )
        existing = connection.execute(
            """
            SELECT id,status,content_sha256 FROM taxonomy_versions
            WHERE taxonomy_id=%s AND version=%s
            """,
            (taxonomy_id, seed.version),
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != seed.content_sha256:
                raise SeedConflictError(
                    f"taxonomy {seed.slug} {seed.version} has different content"
                )
            connection.rollback()
            return {
                "status": "reused",
                "slug": seed.slug,
                "version": seed.version,
                "content_sha256": seed.content_sha256,
                "label_count": len(seed.labels),
            }
        version_id = int(
            _first(
                connection.execute(
                    """
                    INSERT INTO taxonomy_versions
                      (taxonomy_id,version,status,description,source_name,source_url,
                       attribution_text,licence_note,source_seed_path,content_sha256)
                    VALUES (%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (
                        taxonomy_id,
                        seed.version,
                        seed.version_description,
                        seed.source_name,
                        seed.source_url,
                        seed.attribution_text,
                        seed.licence_note,
                        source_path,
                        seed.content_sha256,
                    ),
                ).fetchone()
            )
        )
        label_ids: dict[str, int] = {}
        for label in seed.labels:
            label_ids[label.code] = int(
                _first(
                    connection.execute(
                        """
                        INSERT INTO taxonomy_labels
                          (taxonomy_version_id,code,label,short_definition,full_definition,
                           inclusion_rules,exclusion_rules,positive_examples,
                           negative_examples,borderline_examples,display_order,
                           is_active,is_analytical,is_fallback,requires_review,metadata)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING id
                        """,
                        (
                            version_id,
                            label.code,
                            label.label,
                            label.short_definition,
                            label.full_definition,
                            Jsonb(list(label.inclusion_rules)),
                            Jsonb(list(label.exclusion_rules)),
                            Jsonb(list(label.positive_examples)),
                            Jsonb(list(label.negative_examples)),
                            Jsonb(list(label.borderline_examples)),
                            label.display_order,
                            label.is_active,
                            label.is_analytical,
                            label.is_fallback,
                            label.requires_review,
                            Jsonb(label.metadata),
                        ),
                    ).fetchone()
                )
            )
        for label in seed.labels:
            if label.parent_code is not None:
                connection.execute(
                    "UPDATE taxonomy_labels SET parent_label_id=%s WHERE id=%s",
                    (label_ids[label.parent_code], label_ids[label.code]),
                )
        if seed.status != "draft":
            connection.execute(
                """
                UPDATE taxonomy_versions SET status=%s,
                  published_at=CASE WHEN %s='published' THEN now() ELSE NULL END
                WHERE id=%s
                """,
                (seed.status, seed.status, version_id),
            )
        connection.commit()
    return {
        "status": "loaded",
        "slug": seed.slug,
        "version": seed.version,
        "content_sha256": seed.content_sha256,
        "label_count": len(seed.labels),
    }


def _taxonomy_version_id(
    connection: psycopg.Connection[Any], slug: str, version: str
) -> int:
    row = connection.execute(
        """
        SELECT tv.id FROM taxonomy_versions tv
        JOIN taxonomies t ON t.id=tv.taxonomy_id
        WHERE t.slug=%s AND tv.version=%s
        """,
        (slug, version),
    ).fetchone()
    if row is None:
        raise SeedConflictError(f"unknown taxonomy reference: {slug} {version}")
    return int(_first(row))


def load_annotation_schema(
    settings: DatabaseSettings,
    path: Path,
    *,
    repository_root: Path,
) -> dict[str, Any]:
    seed = load_annotation_schema_seed(path)
    source_path = repository_seed_path(path, repository_root)
    with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
        taxonomy_ids = {
            (field.taxonomy.slug, field.taxonomy.version): _taxonomy_version_id(
                connection, field.taxonomy.slug, field.taxonomy.version
            )
            for field in seed.fields
            if field.taxonomy is not None
        }
        schema = connection.execute(
            "SELECT * FROM annotation_schemas WHERE slug=%s", (seed.slug,)
        ).fetchone()
        if schema is None:
            schema_id = int(
                _first(
                    connection.execute(
                        """
                        INSERT INTO annotation_schemas
                          (slug,name,description,schema_type)
                        VALUES (%s,%s,%s,%s) RETURNING id
                        """,
                        (seed.slug, seed.name, seed.description, seed.schema_type),
                    ).fetchone()
                )
            )
        else:
            schema_id = int(schema["id"])
            if (
                schema["name"],
                schema["description"],
                schema["schema_type"],
            ) != (seed.name, seed.description, seed.schema_type):
                raise SeedConflictError(
                    f"annotation schema identity differs: {seed.slug}"
                )
        existing = connection.execute(
            """
            SELECT id,status,content_sha256 FROM annotation_schema_versions
            WHERE annotation_schema_id=%s AND version=%s
            """,
            (schema_id, seed.version),
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != seed.content_sha256:
                raise SeedConflictError(
                    f"annotation schema {seed.slug} {seed.version} has different content"
                )
            connection.rollback()
            return {
                "status": "reused",
                "slug": seed.slug,
                "version": seed.version,
                "content_sha256": seed.content_sha256,
                "field_count": len(seed.fields),
            }
        version_id = int(
            _first(
                connection.execute(
                    """
                    INSERT INTO annotation_schema_versions
                      (annotation_schema_id,version,status,description,source_seed_path,
                       content_sha256)
                    VALUES (%s,%s,'draft',%s,%s,%s) RETURNING id
                    """,
                    (
                        schema_id,
                        seed.version,
                        seed.version_description,
                        source_path,
                        seed.content_sha256,
                    ),
                ).fetchone()
            )
        )
        for field in seed.fields:
            taxonomy_id = (
                None
                if field.taxonomy is None
                else taxonomy_ids[(field.taxonomy.slug, field.taxonomy.version)]
            )
            connection.execute(
                """
                INSERT INTO annotation_field_definitions
                  (annotation_schema_version_id,field_key,label,help_text,field_type,
                   display_order,required,taxonomy_version_id,minimum_items,maximum_items,
                   minimum_value,maximum_value,validation_rules,visibility_rules,
                   requirement_rules,ui_hints,is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    version_id,
                    field.field_key,
                    field.label,
                    field.help_text,
                    field.field_type,
                    field.display_order,
                    field.required,
                    taxonomy_id,
                    field.minimum_items,
                    field.maximum_items,
                    field.minimum_value,
                    field.maximum_value,
                    Jsonb(field.validation_rules),
                    Jsonb(field.visibility_rules),
                    Jsonb(field.requirement_rules),
                    Jsonb(field.ui_hints),
                    field.is_active,
                ),
            )
        if seed.status != "draft":
            connection.execute(
                """
                UPDATE annotation_schema_versions SET status=%s,
                  published_at=CASE WHEN %s='published' THEN now() ELSE NULL END
                WHERE id=%s
                """,
                (seed.status, seed.status, version_id),
            )
        connection.commit()
    return {
        "status": "loaded",
        "slug": seed.slug,
        "version": seed.version,
        "content_sha256": seed.content_sha256,
        "field_count": len(seed.fields),
    }
