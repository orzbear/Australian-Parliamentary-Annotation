"""SQLAlchemy models introduced by the Phase 2.5 structural migration."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hansard_annotator.db.base import Base

SHA = r"^[0-9a-f]{64}$"


class ProductIdentityMixin:
    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Corpus(ProductIdentityMixin, Base):
    __tablename__ = "corpora"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("slug"),
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_corpus_slug"
        ),
        CheckConstraint(
            "language_code ~ '^[a-z]{2,3}(?:-[A-Z]{2})?$'",
            name="ck_corpus_language",
        ),
        CheckConstraint("date_from <= date_to", name="ck_corpus_dates"),
        CheckConstraint(
            "licence_status IN ('confirmed','pending_review','restricted','unknown')",
            name="ck_corpus_licence",
        ),
        CheckConstraint(
            "redistribution_status IN "
            "('redistributable','metadata_only','software_only','restricted','pending_review')",
            name="ck_corpus_redistribution",
        ),
        Index("ix_corpora_active", "is_active"),
    )
    public_id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("gen_random_uuid()"), nullable=False
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(Text, nullable=False)
    legislature: Mapped[str] = mapped_column(Text, nullable=False)
    chamber: Mapped[str] = mapped_column(Text, nullable=False)
    language_code: Mapped[str] = mapped_column(String(16), nullable=False)
    source_format_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_format_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_description: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_url: Mapped[str | None] = mapped_column(Text)
    licence_status: Mapped[str] = mapped_column(Text, nullable=False)
    licence_note: Mapped[str | None] = mapped_column(Text)
    redistribution_status: Mapped[str] = mapped_column(Text, nullable=False)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_preprocessing_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Taxonomy(ProductIdentityMixin, Base):
    __tablename__ = "taxonomies"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("slug"),
        CheckConstraint(
            "taxonomy_type IN "
            "('policy_domain','content_status','cap_topic','hostility_scale',"
            "'target_type','issue_vocabulary')",
            name="ck_taxonomy_type",
        ),
        CheckConstraint(
            "owner_scope IN ('platform','corpus','project','external')",
            name="ck_taxonomy_owner_scope",
        ),
    )
    public_id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("gen_random_uuid()"), nullable=False
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    taxonomy_type: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(Text)
    language_code: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_scope: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TaxonomyVersion(ProductIdentityMixin, Base):
    __tablename__ = "taxonomy_versions"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("taxonomy_id", "version"),
        CheckConstraint(
            "status IN ('draft','published','retired')",
            name="ck_taxonomy_version_status",
        ),
        CheckConstraint(f"content_sha256 ~ '{SHA}'", name="ck_taxonomy_version_sha"),
    )
    public_id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("gen_random_uuid()"), nullable=False
    )
    taxonomy_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomies.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    attribution_text: Mapped[str | None] = mapped_column(Text)
    licence_note: Mapped[str | None] = mapped_column(Text)
    source_seed_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaxonomyLabel(ProductIdentityMixin, Base):
    __tablename__ = "taxonomy_labels"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("taxonomy_version_id", "code"),
        UniqueConstraint("taxonomy_version_id", "display_order"),
        CheckConstraint("display_order >= 0", name="ck_taxonomy_label_order"),
        CheckConstraint(
            "jsonb_typeof(inclusion_rules)='array' "
            "AND jsonb_typeof(exclusion_rules)='array' "
            "AND jsonb_typeof(positive_examples)='array' "
            "AND jsonb_typeof(negative_examples)='array' "
            "AND jsonb_typeof(borderline_examples)='array'",
            name="ck_taxonomy_label_arrays",
        ),
        Index("ix_taxonomy_labels_version", "taxonomy_version_id"),
    )
    public_id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("gen_random_uuid()"), nullable=False
    )
    taxonomy_version_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    short_definition: Mapped[str] = mapped_column(Text, nullable=False)
    full_definition: Mapped[str | None] = mapped_column(Text)
    inclusion_rules: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    exclusion_rules: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    positive_examples: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    negative_examples: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    borderline_examples: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_label_id: Mapped[int | None] = mapped_column(
        ForeignKey("taxonomy_labels.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_analytical: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False
    )


class TaxonomyLabelRelation(ProductIdentityMixin, Base):
    __tablename__ = "taxonomy_label_relations"
    __table_args__ = (
        UniqueConstraint("source_label_id", "target_label_id", "relation_type"),
        CheckConstraint(
            "source_label_id <> target_label_id", name="ck_taxonomy_relation_self"
        ),
        CheckConstraint(
            "relation_type IN "
            "('broader_than','narrower_than','related_to','maps_to','derived_from')",
            name="ck_taxonomy_relation_type",
        ),
    )
    source_label_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomy_labels.id", ondelete="RESTRICT"), nullable=False
    )
    target_label_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomy_labels.id", ondelete="RESTRICT"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)
    relation_note: Mapped[str | None] = mapped_column(Text)


class AnnotationSchema(ProductIdentityMixin, Base):
    __tablename__ = "annotation_schemas"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("slug"),
        CheckConstraint(
            "schema_type IN ('policy_annotation','content_coding','enrichment')",
            name="ck_annotation_schema_type",
        ),
    )
    public_id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("gen_random_uuid()"), nullable=False
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    schema_type: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnnotationSchemaVersion(ProductIdentityMixin, Base):
    __tablename__ = "annotation_schema_versions"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("annotation_schema_id", "version"),
        CheckConstraint(
            "status IN ('draft','published','retired')",
            name="ck_annotation_schema_version_status",
        ),
        CheckConstraint(
            f"content_sha256 ~ '{SHA}'", name="ck_annotation_schema_version_sha"
        ),
    )
    public_id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("gen_random_uuid()"), nullable=False
    )
    annotation_schema_id: Mapped[int] = mapped_column(
        ForeignKey("annotation_schemas.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_seed_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnnotationFieldDefinition(ProductIdentityMixin, Base):
    __tablename__ = "annotation_field_definitions"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("annotation_schema_version_id", "field_key"),
        UniqueConstraint("annotation_schema_version_id", "display_order"),
        CheckConstraint(
            "field_type IN "
            "('single_taxonomy','multiple_taxonomy','boolean','ordinal','short_text',"
            "'long_text','evidence_span','target_reference','uncertainty')",
            name="ck_annotation_field_type",
        ),
        CheckConstraint(
            "display_order >= 0", name="ck_annotation_field_display_order"
        ),
        CheckConstraint(
            "minimum_items IS NULL OR minimum_items >= 0",
            name="ck_annotation_field_min_items",
        ),
        CheckConstraint(
            "maximum_items IS NULL OR maximum_items >= minimum_items",
            name="ck_annotation_field_max_items",
        ),
        CheckConstraint(
            "jsonb_typeof(validation_rules)='object' "
            "AND jsonb_typeof(visibility_rules)='object' "
            "AND jsonb_typeof(requirement_rules)='object' "
            "AND jsonb_typeof(ui_hints)='object'",
            name="ck_annotation_field_rule_objects",
        ),
    )
    public_id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("gen_random_uuid()"), nullable=False
    )
    annotation_schema_version_id: Mapped[int] = mapped_column(
        ForeignKey("annotation_schema_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    help_text: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str] = mapped_column(Text, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    taxonomy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("taxonomy_versions.id", ondelete="RESTRICT")
    )
    minimum_items: Mapped[int | None] = mapped_column(Integer)
    maximum_items: Mapped[int | None] = mapped_column(Integer)
    minimum_value: Mapped[Decimal | None] = mapped_column(Numeric)
    maximum_value: Mapped[Decimal | None] = mapped_column(Numeric)
    validation_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    visibility_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requirement_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ui_hints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)


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

# Alembic revision 20260724_02 temporarily detaches these tables from the shared
# Phase 2 metadata so revision 20260723_01 retains its accepted create_all/drop_all
# behaviour. Keep the actual table objects available across repeated Alembic module
# reloads in one process.
PRODUCT_TABLES = {
    name: Base.metadata.tables[name] for name in PRODUCT_TABLE_NAMES
}
