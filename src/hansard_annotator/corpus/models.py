"""Typed records shared by Phase 1 preprocessing modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DiscoveredFile:
    absolute_path: Path
    relative_path: str
    source_file_sha256: str
    byte_size: int
    modification_time_ns: int
    sitting_date: date

    def to_row(self, *, chamber: str) -> dict[str, Any]:
        return {
            "source_file_key": self.source_file_key,
            "source_file": self.relative_path,
            "source_file_sha256": self.source_file_sha256,
            "byte_size": self.byte_size,
            "modification_time_ns": self.modification_time_ns,
            "date": self.sitting_date,
            "chamber": chamber,
        }

    @property
    def source_file_key(self) -> str:
        return f"{self.source_file_sha256}:{self.relative_path}"


@dataclass
class DebateSection:
    section_key: str
    source_file: str
    source_file_sha256: str
    sequence_number: int
    section_type: str
    parent_section_key: str | None
    heading_original: str
    heading_clean: str
    source_fragment_id: str | None
    source_url: str | None
    warning_codes: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return vars(self)


@dataclass
class DebateEvent:
    event_key: str
    source_file: str
    source_file_sha256: str
    sequence_number: int
    date: date
    chamber: str
    element_type: str
    source_fragment_id: str | None
    major_heading_original: str | None
    minor_heading_original: str | None
    source_url: str | None
    attributes_json: str
    payload_json: str
    bill_record_count: int
    warning_codes: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return vars(self)


@dataclass
class SpeechFragment:
    fragment_key: str
    source_fragment_id: str | None
    source_file: str
    source_file_sha256: str
    fragment_projection_sha256: str
    sequence_number: int
    date: date
    chamber: str
    major_section_key: str | None
    minor_section_key: str | None
    major_heading_original: str | None
    minor_heading_original: str | None
    speaker_id_raw: str | None
    speaker_name_raw: str | None
    talktype_raw: str | None
    time_raw: str | None
    parsed_time: time | None
    approximate_wordcount_raw: str | None
    approximate_duration_raw: str | None
    source_url: str | None
    nospeaker_raw: str | None
    attributes_json: str
    text_raw: str
    text_clean: str
    calculated_word_count: int
    block_structure_json: str
    parse_status: str
    warning_codes: list[str]
    boundary_before: bool
    boundary_reason_before: str | None
    business_type: str | None = None
    business_mapping_version: str | None = None
    is_presiding_officer: bool | None = None
    is_collective_speaker: bool | None = None
    is_procedural: bool | None = None
    is_ceremonial: bool | None = None
    is_division_related: bool | None = None
    is_question_time: bool | None = None
    is_question: bool | None = None
    is_answer: bool | None = None
    is_interjection: bool = False
    is_continuation_merged: bool = False
    is_orphan_continuation: bool = False
    has_known_speaker: bool = False
    eligible_50_words: bool = False
    eligible_100_words: bool = False
    eligible_main_analysis: bool | None = None

    def to_row(self) -> dict[str, Any]:
        row = vars(self).copy()
        row.pop("boundary_before")
        return row


@dataclass
class SpeakerTurn:
    turn_key: str
    source_file: str
    source_file_sha256: str
    turn_sequence: int
    date: date
    chamber: str
    first_fragment_sequence: int
    last_fragment_sequence: int
    major_heading_original: str | None
    minor_heading_original: str | None
    speaker_id_raw: str | None
    speaker_name_raw: str | None
    text_raw: str
    text_clean: str
    calculated_word_count: int
    fragment_count: int
    business_type: str | None
    business_mapping_version: str | None
    warning_codes: list[str]
    is_presiding_officer: bool | None
    is_collective_speaker: bool | None
    is_procedural: bool | None
    is_ceremonial: bool | None
    is_division_related: bool | None
    is_question_time: bool | None
    is_question: bool | None
    is_answer: bool | None
    is_interjection: bool
    is_orphan_continuation: bool
    interrupted: bool
    interruption_count: int
    has_known_speaker: bool
    eligible_50_words: bool
    eligible_100_words: bool
    eligible_main_analysis: bool | None

    def to_row(self) -> dict[str, Any]:
        return vars(self)


@dataclass
class TurnFragment:
    turn_key: str
    fragment_key: str
    ordinal: int
    relation_type: str
    merge_reason_code: str
    source_file: str
    fragment_sequence: int

    def to_row(self) -> dict[str, Any]:
        return vars(self)


@dataclass
class Interjection:
    interjection_key: str
    source_fragment_key: str
    source_file: str
    source_file_sha256: str
    sequence_number: int
    date: date
    chamber: str
    speaker_id_raw: str | None
    speaker_name_raw: str | None
    is_collective: bool | None
    text_raw: str
    text_clean: str
    interrupted_turn_key: str | None
    link_confidence: str
    link_reason: str
    major_heading_original: str | None
    minor_heading_original: str | None
    warning_codes: list[str]

    def to_row(self) -> dict[str, Any]:
        return vars(self)


@dataclass
class ContinuationAnomaly:
    anomaly_key: str
    fragment_key: str
    source_file: str
    sequence_number: int
    source_fragment_id: str | None
    speaker_id_raw: str | None
    speaker_name_raw: str | None
    candidate_turn_key: str | None
    candidate_speaker_id_raw: str | None
    candidate_speaker_name_raw: str | None
    reason_code: str
    major_heading_original: str | None
    minor_heading_original: str | None

    def to_row(self) -> dict[str, Any]:
        return vars(self)


@dataclass
class ImageElement:
    image_key: str
    fragment_key: str
    source_file: str
    sequence_number: int
    image_index: int
    attributes_json: str
    alt_text: str | None
    clean_text_replacement: str
    major_heading_original: str | None
    minor_heading_original: str | None

    def to_row(self) -> dict[str, Any]:
        return vars(self)


@dataclass
class FileResult:
    source: DiscoveredFile
    sections: list[DebateSection] = field(default_factory=list)
    events: list[DebateEvent] = field(default_factory=list)
    fragments: list[SpeechFragment] = field(default_factory=list)
    turns: list[SpeakerTurn] = field(default_factory=list)
    turn_fragments: list[TurnFragment] = field(default_factory=list)
    interjections: list[Interjection] = field(default_factory=list)
    continuation_anomalies: list[ContinuationAnomaly] = field(default_factory=list)
    images: list[ImageElement] = field(default_factory=list)
    major_heading_count: int = 0
    minor_heading_count: int = 0
    division_count: int = 0
    bills_element_count: int = 0
    bill_record_count: int = 0
    unknown_top_level_count: int = 0
    source_url_exception_count: int = 0
    element_count: int = 0
    max_depth_seen: int = 0
    observed_tag_counts: dict[str, int] = field(default_factory=dict)
    observed_talktype_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FileFailure:
    source_file: str
    source_file_sha256: str
    error_code: str
    error_message: str


@dataclass
class FileProcessingRecord:
    source_file: str
    source_file_sha256: str
    byte_size: int
    modification_time_ns_before: int
    modification_time_ns_after: int
    status: str
    error_code: str | None
    error_message: str | None
    fragment_count: int
    turn_count: int
    interjection_count: int
    continuation_anomaly_count: int
    warning_count: int
    reconciled: bool

    def to_row(self) -> dict[str, Any]:
        return vars(self)
