"""Phase 2.5 product-foundation verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from hansard_annotator.db.import_models import ValidatedRun
from hansard_annotator.db.verification import verify_database


class ProductVerificationError(RuntimeError):
    pass


def _scalar(connection: psycopg.Connection[Any], sql: str, args: tuple[Any, ...] = ()) -> int:
    row = connection.execute(sql, args).fetchone()
    assert row is not None
    return int(next(iter(row.values())) if isinstance(row, dict) else row[0])


def verify_product_foundation(
    connection: psycopg.Connection[Any],
) -> dict[str, Any]:
    corpus = connection.execute(
        """
        SELECT id,public_id,slug,current_preprocessing_run_id
        FROM corpora
        WHERE slug='australian-house-representatives-hansard' AND is_active
        """
    ).fetchone()
    if corpus is None:
        raise ProductVerificationError("active Australian corpus count is not one")
    corpus_id = int(corpus["id"] if isinstance(corpus, dict) else corpus[0])
    if _scalar(
        connection,
        "SELECT count(*) FROM corpora WHERE slug=%s AND is_active",
        ("australian-house-representatives-hansard",),
    ) != 1:
        raise ProductVerificationError("active Australian corpus count is not one")
    unlinked_runs = _scalar(
        connection, "SELECT count(*) FROM preprocessing_runs WHERE corpus_id IS NULL"
    )
    unlinked_sources = _scalar(
        connection,
        """
        SELECT count(*) FROM source_file_versions sfv
        JOIN source_files sf ON sf.id=sfv.source_file_id
        JOIN preprocessing_runs pr ON pr.id=sfv.preprocessing_run_id
        WHERE sf.corpus_id IS NULL OR pr.corpus_id IS NULL
          OR sf.corpus_id<>pr.corpus_id
        """,
    )
    if unlinked_runs or unlinked_sources:
        raise ProductVerificationError("corpus linkage is incomplete")
    current = connection.execute(
        """
        SELECT c.current_preprocessing_run_id,pr.id,pr.status,pr.manifest,
               pr.manifest_sha256
        FROM corpora c LEFT JOIN preprocessing_runs pr
          ON pr.id=c.current_preprocessing_run_id AND pr.corpus_id=c.id
        WHERE c.id=%s
        """,
        (corpus_id,),
    ).fetchone()
    assert current is not None
    current_id = current["id"] if isinstance(current, dict) else current[1]
    current_status = current["status"] if isinstance(current, dict) else current[2]
    if current_id is None or current_status != "accepted":
        raise ProductVerificationError("current accepted preprocessing run is incompatible")
    manifest = current["manifest"] if isinstance(current, dict) else current[3]
    manifest_sha256 = (
        current["manifest_sha256"] if isinstance(current, dict) else current[4]
    )
    artifact_count = _scalar(
        connection,
        "SELECT count(*) FROM import_artifacts WHERE preprocessing_run_id=%s",
        (int(current_id),),
    )
    validated = ValidatedRun(
        run_dir=Path("."),
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        artefact_count=max(0, artifact_count - 1),
        artefact_bytes=0,
        dataset_rows={
            key: int(value)
            for key, value in manifest["record_counts"].items()
            if key != "image_elements"
        },
    )
    phase2 = verify_database(connection, int(current_id), validated)

    analytical = _scalar(
        connection,
        """
        SELECT count(*) FROM taxonomy_labels label
        JOIN taxonomy_versions version ON version.id=label.taxonomy_version_id
        JOIN taxonomies taxonomy ON taxonomy.id=version.taxonomy_id
        WHERE taxonomy.slug='australian_policy_domains'
          AND version.version='0.1.0' AND label.is_analytical
        """,
    )
    fallback = connection.execute(
        """
        SELECT label.is_analytical,label.is_fallback,label.requires_review
        FROM taxonomy_labels label
        JOIN taxonomy_versions version ON version.id=label.taxonomy_version_id
        JOIN taxonomies taxonomy ON taxonomy.id=version.taxonomy_id
        WHERE taxonomy.slug='australian_policy_domains'
          AND version.version='0.1.0' AND label.code='AU_OTHER_REVIEW'
        """
    ).fetchone()
    statuses = _scalar(
        connection,
        """
        SELECT count(*) FROM taxonomy_labels label
        JOIN taxonomy_versions version ON version.id=label.taxonomy_version_id
        JOIN taxonomies taxonomy ON taxonomy.id=version.taxonomy_id
        WHERE taxonomy.slug='content_status' AND version.version='1.0.0'
        """,
    )
    if analytical != 14 or fallback is None or statuses != 4:
        raise ProductVerificationError("expected taxonomy content is incomplete")
    fallback_values = tuple(fallback.values()) if isinstance(fallback, dict) else fallback
    if fallback_values != (False, True, True):
        raise ProductVerificationError("AU_OTHER_REVIEW flags are invalid")

    fields = connection.execute(
        """
        SELECT field.field_key,taxonomy.slug AS taxonomy_slug,
               taxonomy_version.version AS taxonomy_version
        FROM annotation_field_definitions field
        JOIN annotation_schema_versions schema_version
          ON schema_version.id=field.annotation_schema_version_id
        JOIN annotation_schemas schema
          ON schema.id=schema_version.annotation_schema_id
        LEFT JOIN taxonomy_versions taxonomy_version
          ON taxonomy_version.id=field.taxonomy_version_id
        LEFT JOIN taxonomies taxonomy ON taxonomy.id=taxonomy_version.taxonomy_id
        WHERE schema.slug='australian_policy_annotation'
          AND schema_version.version='0.1.0'
        ORDER BY field.display_order
        """
    ).fetchall()
    expected_fields = {
        "content_status",
        "primary_australian_domain",
        "secondary_australian_domains",
        "specific_australian_issue",
        "topic_uncertain",
        "fallback_explanation",
        "unclassifiable_reason",
        "annotation_notes",
    }
    actual_fields = {
        row["field_key"] if isinstance(row, dict) else row[0] for row in fields
    }
    taxonomy_slugs = {
        row["taxonomy_slug"] if isinstance(row, dict) else row[1]
        for row in fields
        if (row["taxonomy_slug"] if isinstance(row, dict) else row[1]) is not None
    }
    if actual_fields != expected_fields or "cap" in " ".join(taxonomy_slugs).lower():
        raise ProductVerificationError("initial annotation schema is invalid or requires CAP")
    phase3_relations = {
        "users",
        "projects",
        "batches",
        "tasks",
        "assignments",
        "annotations",
        "annotation_versions",
    }
    phase3_rows = connection.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_name=ANY(%s)
            """,
            (list(phase3_relations),),
        ).fetchall()
    existing_phase3 = {
        row["table_name"] if isinstance(row, dict) else row[0]
        for row in phase3_rows
    }
    return {
        "ok": True,
        "corpus_slug": "australian-house-representatives-hansard",
        "preprocessing_runs_without_corpus": unlinked_runs,
        "source_versions_without_compatible_corpus": unlinked_sources,
        "australian_analytical_domain_labels": analytical,
        "content_status_labels": statuses,
        "annotation_schema_fields": len(fields),
        "cap_required": False,
        "phase3_tables": sorted(existing_phase3),
        "phase2": phase2,
    }
