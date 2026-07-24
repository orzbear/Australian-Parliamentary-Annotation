"""SQLAlchemy 2 models for the immutable Phase 2 corpus."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIME
from sqlalchemy.orm import Mapped, mapped_column

from hansard_annotator.db.base import Base

SHA = r"^[0-9a-f]{64}$"


class IdentityMixin:
    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PreprocessingRun(IdentityMixin, Base):
    __tablename__ = "preprocessing_runs"
    __table_args__ = (
        UniqueConstraint("run_name", "manifest_sha256"),
        CheckConstraint(f"inventory_sha256 ~ '{SHA}'", name="ck_pre_run_inventory_sha"),
        CheckConstraint(f"manifest_sha256 ~ '{SHA}'", name="ck_pre_run_manifest_sha"),
        CheckConstraint("source_file_count >= 0", name="ck_pre_run_file_count"),
        CheckConstraint("source_byte_count >= 0", name="ck_pre_run_byte_count"),
        CheckConstraint(
            "status IN ('importing','accepted','failed')", name="ck_pre_run_status"
        ),
    )
    run_name: Mapped[str] = mapped_column(Text, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    git_commit: Mapped[str | None] = mapped_column(Text)
    inventory_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_hashes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processing_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    processing_completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)


class DatabaseImportRun(IdentityMixin, Base):
    __tablename__ = "database_import_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','completed_reused','failed','rolled_back')",
            name="ck_import_run_status",
        ),
    )
    preprocessing_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    importer_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_directory: Mapped[str] = mapped_column(Text, nullable=False)
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verification_results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)


class SourceFile(IdentityMixin, Base):
    __tablename__ = "source_files"
    __table_args__ = (
        UniqueConstraint("relative_path"),
        CheckConstraint(
            "relative_path ~ '^[0-9]{4}/[0-9]{4}-[0-9]{2}-[0-9]{2}[.]xml$'",
            name="ck_source_file_safe_path",
        ),
        CheckConstraint(
            "folder_year = EXTRACT(YEAR FROM speech_date)::integer",
            name="ck_source_file_year",
        ),
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    speech_date: Mapped[date] = mapped_column(Date, nullable=False)
    chamber: Mapped[str] = mapped_column(Text, nullable=False)
    folder_year: Mapped[int] = mapped_column(Integer, nullable=False)


class SourceFileVersion(IdentityMixin, Base):
    __tablename__ = "source_file_versions"
    __table_args__ = (
        UniqueConstraint("preprocessing_run_id", "source_file_id"),
        UniqueConstraint(
            "preprocessing_run_id", "source_file_id", "source_sha256"
        ),
        UniqueConstraint("source_file_key"),
        CheckConstraint(f"source_sha256 ~ '{SHA}'", name="ck_source_version_sha"),
        CheckConstraint("byte_size >= 0", name="ck_source_version_bytes"),
        CheckConstraint(
            "processing_status IN ('completed','failed')",
            name="ck_source_version_status",
        ),
    )
    source_file_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="RESTRICT"), nullable=False
    )
    preprocessing_run_id: Mapped[int] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    modification_time_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    modification_time_ns_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False)
    reconciled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_url_marker_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class DebateSection(IdentityMixin, Base):
    __tablename__ = "debate_sections"
    __table_args__ = (
        UniqueConstraint("section_key"),
        UniqueConstraint("source_file_version_id", "sequence_number", "section_level"),
        CheckConstraint("section_level IN ('major','minor')", name="ck_section_level"),
        CheckConstraint("sequence_number > 0", name="ck_section_sequence"),
        Index("ix_sections_source_sequence", "source_file_version_id", "sequence_number"),
        Index("ix_sections_heading_normalised", "heading_normalised"),
    )
    section_key: Mapped[str] = mapped_column(String(64), nullable=False)
    preprocessing_run_id: Mapped[int] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_version_id: Mapped[int] = mapped_column(
        ForeignKey("source_file_versions.id", ondelete="RESTRICT"), nullable=False
    )
    parent_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("debate_sections.id", ondelete="RESTRICT")
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    section_level: Mapped[str] = mapped_column(Text, nullable=False)
    heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    heading_normalised: Mapped[str] = mapped_column(Text, nullable=False)
    source_heading_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    warning_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class DebateEvent(IdentityMixin, Base):
    __tablename__ = "debate_events"
    __table_args__ = (
        UniqueConstraint("event_key"),
        UniqueConstraint("source_file_version_id", "sequence_number"),
        CheckConstraint("event_type IN ('bills','division')", name="ck_event_type"),
        CheckConstraint("bill_record_count >= 0", name="ck_event_bill_count"),
        Index("ix_events_source_sequence", "source_file_version_id", "sequence_number"),
    )
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    preprocessing_run_id: Mapped[int] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_version_id: Mapped[int] = mapped_column(
        ForeignKey("source_file_versions.id", ondelete="RESTRICT"), nullable=False
    )
    major_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("debate_sections.id", ondelete="RESTRICT")
    )
    minor_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("debate_sections.id", ondelete="RESTRICT")
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    speech_date: Mapped[date] = mapped_column(Date, nullable=False)
    chamber: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(Text)
    major_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    minor_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    bill_record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    warning_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class SpeechFragment(IdentityMixin, Base):
    __tablename__ = "speech_fragments"
    __table_args__ = (
        UniqueConstraint("fragment_key"),
        UniqueConstraint("source_file_version_id", "sequence_number"),
        CheckConstraint(f"fragment_key ~ '{SHA}'", name="ck_fragment_key"),
        CheckConstraint(
            f"fragment_projection_sha256 ~ '{SHA}'", name="ck_fragment_projection_sha"
        ),
        CheckConstraint(f"record_sha256 ~ '{SHA}'", name="ck_fragment_record_sha"),
        CheckConstraint("calculated_word_count >= 0", name="ck_fragment_word_count"),
        CheckConstraint(
            "talktype_raw IN ('speech','continuation','interjection')",
            name="ck_fragment_talktype",
        ),
        Index("ix_fragments_date", "speech_date"),
        Index("ix_fragments_speaker_id", "speaker_id_raw"),
        Index("ix_fragments_speaker_name", "speaker_name_raw"),
        Index("ix_fragments_source_sequence", "source_file_version_id", "sequence_number"),
        Index("ix_fragments_sections", "major_section_id", "minor_section_id"),
    )
    fragment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    preprocessing_run_id: Mapped[int] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_version_id: Mapped[int] = mapped_column(
        ForeignKey("source_file_versions.id", ondelete="RESTRICT"), nullable=False
    )
    major_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("debate_sections.id", ondelete="RESTRICT")
    )
    minor_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("debate_sections.id", ondelete="RESTRICT")
    )
    source_fragment_id: Mapped[str] = mapped_column(Text, nullable=False)
    fragment_projection_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    speech_date: Mapped[date] = mapped_column(Date, nullable=False)
    chamber: Mapped[str] = mapped_column(Text, nullable=False)
    major_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    minor_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_id_raw: Mapped[str | None] = mapped_column(Text)
    speaker_name_raw: Mapped[str | None] = mapped_column(Text)
    talktype_raw: Mapped[str] = mapped_column(Text, nullable=False)
    time_raw: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_time: Mapped[time | None] = mapped_column(TIME(precision=3))
    approximate_wordcount_raw: Mapped[str] = mapped_column(Text, nullable=False)
    approximate_duration_raw: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    nospeaker_raw: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    text_raw: Mapped[str] = mapped_column(Text, nullable=False)
    text_clean: Mapped[str] = mapped_column(Text, nullable=False)
    calculated_word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    block_structure: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    parse_status: Mapped[str] = mapped_column(Text, nullable=False)
    warning_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    boundary_reason_before: Mapped[str | None] = mapped_column(Text)
    business_type: Mapped[str | None] = mapped_column(Text)
    business_mapping_version: Mapped[str] = mapped_column(Text, nullable=False)
    is_presiding_officer: Mapped[bool | None] = mapped_column(Boolean)
    is_collective_speaker: Mapped[bool | None] = mapped_column(Boolean)
    is_procedural: Mapped[bool | None] = mapped_column(Boolean)
    is_ceremonial: Mapped[bool | None] = mapped_column(Boolean)
    is_division_related: Mapped[bool | None] = mapped_column(Boolean)
    is_question_time: Mapped[bool | None] = mapped_column(Boolean)
    is_question: Mapped[bool | None] = mapped_column(Boolean)
    is_answer: Mapped[bool | None] = mapped_column(Boolean)
    is_interjection: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_continuation_merged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_orphan_continuation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_known_speaker: Mapped[bool] = mapped_column(Boolean, nullable=False)
    eligible_50_words: Mapped[bool] = mapped_column(Boolean, nullable=False)
    eligible_100_words: Mapped[bool] = mapped_column(Boolean, nullable=False)
    eligible_main_analysis: Mapped[bool | None] = mapped_column(Boolean)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ReconstructionRun(IdentityMixin, Base):
    __tablename__ = "reconstruction_runs"
    __table_args__ = (
        UniqueConstraint(
            "preprocessing_run_id",
            "reconstruction_version",
            "structural_rules_hash",
            "collective_allowlist_hash",
            "business_mapping_hash",
        ),
        CheckConstraint(
            "status IN ('importing','accepted','failed')",
            name="ck_reconstruction_status",
        ),
    )
    preprocessing_run_id: Mapped[int] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT"), nullable=False
    )
    reconstruction_version: Mapped[str] = mapped_column(Text, nullable=False)
    structural_rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    collective_allowlist_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    business_mapping_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)


class SpeakerTurn(IdentityMixin, Base):
    __tablename__ = "speaker_turns"
    __table_args__ = (
        UniqueConstraint("turn_key"),
        UniqueConstraint(
            "reconstruction_run_id", "source_file_version_id", "turn_sequence"
        ),
        CheckConstraint("calculated_word_count >= 0", name="ck_turn_word_count"),
        CheckConstraint("fragment_count > 0", name="ck_turn_fragment_count"),
        CheckConstraint("interruption_count >= 0", name="ck_turn_interruption_count"),
        CheckConstraint(
            "interrupted = (interruption_count > 0)", name="ck_turn_interrupted"
        ),
        CheckConstraint(
            "last_fragment_sequence >= first_fragment_sequence",
            name="ck_turn_sequence_range",
        ),
        Index("ix_turns_date", "speech_date"),
        Index("ix_turns_speaker_id", "speaker_id_raw"),
        Index("ix_turns_speaker_name", "speaker_name_raw"),
        Index("ix_turns_business", "business_type"),
        Index("ix_turns_eligibility", "eligible_50_words", "eligible_100_words"),
        Index("ix_turns_source_sequence", "source_file_version_id", "turn_sequence"),
        Index("ix_turns_orphan", "is_orphan_continuation"),
    )
    turn_key: Mapped[str] = mapped_column(String(64), nullable=False)
    reconstruction_run_id: Mapped[int] = mapped_column(
        ForeignKey("reconstruction_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_version_id: Mapped[int] = mapped_column(
        ForeignKey("source_file_versions.id", ondelete="RESTRICT"), nullable=False
    )
    major_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("debate_sections.id", ondelete="RESTRICT")
    )
    minor_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("debate_sections.id", ondelete="RESTRICT")
    )
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    speech_date: Mapped[date] = mapped_column(Date, nullable=False)
    chamber: Mapped[str] = mapped_column(Text, nullable=False)
    first_fragment_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    last_fragment_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    major_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    minor_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_id_raw: Mapped[str | None] = mapped_column(Text)
    speaker_name_raw: Mapped[str | None] = mapped_column(Text)
    text_raw: Mapped[str] = mapped_column(Text, nullable=False)
    text_clean: Mapped[str] = mapped_column(Text, nullable=False)
    calculated_word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fragment_count: Mapped[int] = mapped_column(Integer, nullable=False)
    business_type: Mapped[str | None] = mapped_column(Text)
    business_mapping_version: Mapped[str] = mapped_column(Text, nullable=False)
    warning_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    is_presiding_officer: Mapped[bool | None] = mapped_column(Boolean)
    is_collective_speaker: Mapped[bool | None] = mapped_column(Boolean)
    is_procedural: Mapped[bool | None] = mapped_column(Boolean)
    is_ceremonial: Mapped[bool | None] = mapped_column(Boolean)
    is_division_related: Mapped[bool | None] = mapped_column(Boolean)
    is_question_time: Mapped[bool | None] = mapped_column(Boolean)
    is_question: Mapped[bool | None] = mapped_column(Boolean)
    is_answer: Mapped[bool | None] = mapped_column(Boolean)
    is_interjection: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_orphan_continuation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    interrupted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    interruption_count: Mapped[int] = mapped_column(Integer, nullable=False)
    has_known_speaker: Mapped[bool] = mapped_column(Boolean, nullable=False)
    eligible_50_words: Mapped[bool] = mapped_column(Boolean, nullable=False)
    eligible_100_words: Mapped[bool] = mapped_column(Boolean, nullable=False)
    eligible_main_analysis: Mapped[bool | None] = mapped_column(Boolean)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class SpeakerTurnFragment(Base):
    __tablename__ = "speaker_turn_fragments"
    __table_args__ = (
        UniqueConstraint("turn_id", "fragment_id"),
        CheckConstraint("ordinal > 0", name="ck_lineage_ordinal"),
        CheckConstraint(
            "relation_type IN ('initial','continuation','orphan_continuation')",
            name="ck_lineage_relation",
        ),
        Index("ix_lineage_fragment", "fragment_id"),
    )
    turn_id: Mapped[int] = mapped_column(
        ForeignKey("speaker_turns.id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    fragment_id: Mapped[int] = mapped_column(
        ForeignKey("speech_fragments.id", ondelete="RESTRICT"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)
    merge_reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    fragment_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Interjection(IdentityMixin, Base):
    __tablename__ = "interjections"
    __table_args__ = (
        UniqueConstraint("interjection_key"),
        UniqueConstraint("fragment_id"),
        CheckConstraint("sequence_number > 0", name="ck_interjection_sequence"),
        Index("ix_interjections_turn", "interrupted_turn_id"),
        Index("ix_interjections_source_sequence", "source_file_version_id", "sequence_number"),
        Index("ix_interjections_speaker_id", "speaker_id_raw"),
        Index("ix_interjections_link_reason", "link_reason"),
    )
    interjection_key: Mapped[str] = mapped_column(String(64), nullable=False)
    reconstruction_run_id: Mapped[int] = mapped_column(
        ForeignKey("reconstruction_runs.id", ondelete="RESTRICT"), nullable=False
    )
    fragment_id: Mapped[int] = mapped_column(
        ForeignKey("speech_fragments.id", ondelete="RESTRICT"), nullable=False
    )
    interrupted_turn_id: Mapped[int | None] = mapped_column(
        ForeignKey("speaker_turns.id", ondelete="RESTRICT")
    )
    source_file_version_id: Mapped[int] = mapped_column(
        ForeignKey("source_file_versions.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    speech_date: Mapped[date] = mapped_column(Date, nullable=False)
    chamber: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_id_raw: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_name_raw: Mapped[str] = mapped_column(Text, nullable=False)
    text_raw: Mapped[str] = mapped_column(Text, nullable=False)
    text_clean: Mapped[str] = mapped_column(Text, nullable=False)
    is_collective: Mapped[bool | None] = mapped_column(Boolean)
    link_confidence: Mapped[str] = mapped_column(Text, nullable=False)
    link_reason: Mapped[str] = mapped_column(Text, nullable=False)
    major_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    minor_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    warning_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ContinuationAnomaly(IdentityMixin, Base):
    __tablename__ = "continuation_anomalies"
    __table_args__ = (
        UniqueConstraint("anomaly_key"),
        UniqueConstraint("reconstruction_run_id", "fragment_id"),
        Index("ix_continuation_reason", "reason_code"),
    )
    anomaly_key: Mapped[str] = mapped_column(String(64), nullable=False)
    preprocessing_run_id: Mapped[int] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT"), nullable=False
    )
    reconstruction_run_id: Mapped[int] = mapped_column(
        ForeignKey("reconstruction_runs.id", ondelete="RESTRICT"), nullable=False
    )
    fragment_id: Mapped[int] = mapped_column(
        ForeignKey("speech_fragments.id", ondelete="RESTRICT"), nullable=False
    )
    generated_turn_id: Mapped[int] = mapped_column(
        ForeignKey("speaker_turns.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_turn_id: Mapped[int | None] = mapped_column(
        ForeignKey("speaker_turns.id", ondelete="RESTRICT")
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_fragment_id: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_id_raw: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_name_raw: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_speaker_id_raw: Mapped[str | None] = mapped_column(Text)
    candidate_speaker_name_raw: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    major_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    minor_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ImageAnomaly(IdentityMixin, Base):
    __tablename__ = "image_anomalies"
    __table_args__ = (
        UniqueConstraint("image_key"),
        UniqueConstraint("fragment_id", "element_ordinal"),
        CheckConstraint("element_ordinal > 0", name="ck_image_ordinal"),
    )
    image_key: Mapped[str] = mapped_column(String(64), nullable=False)
    preprocessing_run_id: Mapped[int] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT"), nullable=False
    )
    fragment_id: Mapped[int] = mapped_column(
        ForeignKey("speech_fragments.id", ondelete="RESTRICT"), nullable=False
    )
    element_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    element_attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    alt_text: Mapped[str] = mapped_column(Text, nullable=False)
    clean_text_replacement: Mapped[str] = mapped_column(Text, nullable=False)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    major_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    minor_heading_original: Mapped[str] = mapped_column(Text, nullable=False)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ImportArtifact(IdentityMixin, Base):
    __tablename__ = "import_artifacts"
    __table_args__ = (
        UniqueConstraint("preprocessing_run_id", "relative_path"),
        CheckConstraint(f"sha256 ~ '{SHA}'", name="ck_artifact_sha"),
        CheckConstraint("byte_size >= 0", name="ck_artifact_bytes"),
        CheckConstraint("row_count IS NULL OR row_count >= 0", name="ck_artifact_rows"),
    )
    preprocessing_run_id: Mapped[int] = mapped_column(
        ForeignKey("preprocessing_runs.id", ondelete="RESTRICT"), nullable=False
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    artefact_type: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    parquet_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


__all__ = [
    "Base",
    "ContinuationAnomaly",
    "DatabaseImportRun",
    "DebateEvent",
    "DebateSection",
    "ImageAnomaly",
    "ImportArtifact",
    "Interjection",
    "PreprocessingRun",
    "ReconstructionRun",
    "SourceFile",
    "SourceFileVersion",
    "SpeakerTurn",
    "SpeakerTurnFragment",
    "SpeechFragment",
]
