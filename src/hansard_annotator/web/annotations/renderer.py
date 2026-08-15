"""Load schema-driven rendering metadata without executable rules."""

from __future__ import annotations

from typing import Any

import psycopg


def load_schema(
    connection: psycopg.Connection[Any], schema_version_id: int
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    fields = connection.execute(
        """
        SELECT id,field_key,label,help_text,field_type,display_order,required,
               taxonomy_version_id,minimum_items,maximum_items,validation_rules,
               visibility_rules,requirement_rules,ui_hints
        FROM annotation_field_definitions
        WHERE annotation_schema_version_id=%s AND is_active
        ORDER BY display_order
        """,
        (schema_version_id,),
    ).fetchall()
    taxonomy_ids = sorted(
        {
            int(field["taxonomy_version_id"])
            for field in fields
            if field["taxonomy_version_id"] is not None
        }
    )
    labels: dict[int, list[dict[str, Any]]] = {}
    for taxonomy_id in taxonomy_ids:
        labels[taxonomy_id] = [
            dict(row)
            for row in connection.execute(
                """
                SELECT code,label,short_definition,is_fallback,requires_review
                FROM taxonomy_labels
                WHERE taxonomy_version_id=%s AND is_active
                ORDER BY display_order
                """,
                (taxonomy_id,),
            ).fetchall()
        ]
    return [dict(field) for field in fields], labels
