"""Validated metadata for an accepted Phase 1 import."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACCEPTED_RUN_NAME = "phase1-full-20260723-v4"
ACCEPTED_INVENTORY_SHA256 = (
    "7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2"
)
ACCEPTED_SOURCE_BYTES = 784_583_349
ACCEPTED_COUNTS = {
    "source_files": 926,
    "debate_sections": 97_975,
    "debate_events": 14_352,
    "speech_fragments": 269_793,
    "speaker_turns": 160_577,
    "speaker_turn_fragments": 191_263,
    "interjections": 78_530,
    "continuation_anomalies": 8_239,
    "image_elements": 4,
}

EXPECTED_FIELDS: dict[str, list[tuple[str, str]]] = {
    "source_files": [
        ("source_file_key", "string"), ("source_file", "string"),
        ("source_file_sha256", "string"), ("byte_size", "int64"),
        ("modification_time_ns", "int64"), ("date", "date32[day]"),
        ("chamber", "string"),
    ],
    "debate_sections": [
        ("section_key", "string"), ("source_file", "string"),
        ("source_file_sha256", "string"), ("sequence_number", "int32"),
        ("section_type", "string"), ("parent_section_key", "string"),
        ("heading_original", "string"), ("heading_clean", "string"),
        ("source_fragment_id", "string"), ("source_url", "string"),
        ("warning_codes", "list<element: string>"),
    ],
    "debate_events": [
        ("event_key", "string"), ("source_file", "string"),
        ("source_file_sha256", "string"), ("sequence_number", "int32"),
        ("date", "date32[day]"), ("chamber", "string"),
        ("element_type", "string"), ("source_fragment_id", "string"),
        ("major_heading_original", "string"), ("minor_heading_original", "string"),
        ("source_url", "string"), ("attributes_json", "string"),
        ("payload_json", "string"), ("bill_record_count", "int32"),
        ("warning_codes", "list<element: string>"),
    ],
    "speech_fragments": [
        ("fragment_key", "string"), ("source_fragment_id", "string"),
        ("source_file", "string"), ("source_file_sha256", "string"),
        ("fragment_projection_sha256", "string"), ("sequence_number", "int32"),
        ("date", "date32[day]"), ("chamber", "string"),
        ("major_section_key", "string"), ("minor_section_key", "string"),
        ("major_heading_original", "string"), ("minor_heading_original", "string"),
        ("speaker_id_raw", "string"), ("speaker_name_raw", "string"),
        ("talktype_raw", "string"), ("time_raw", "string"),
        ("parsed_time", "time32[ms]"), ("approximate_wordcount_raw", "string"),
        ("approximate_duration_raw", "string"), ("source_url", "string"),
        ("nospeaker_raw", "string"), ("attributes_json", "string"),
        ("text_raw", "string"), ("text_clean", "string"),
        ("calculated_word_count", "int32"), ("block_structure_json", "string"),
        ("parse_status", "string"), ("warning_codes", "list<element: string>"),
        ("boundary_reason_before", "string"), ("business_type", "string"),
        ("business_mapping_version", "string"), ("is_presiding_officer", "bool"),
        ("is_collective_speaker", "bool"), ("is_procedural", "bool"),
        ("is_ceremonial", "bool"), ("is_division_related", "bool"),
        ("is_question_time", "bool"), ("is_question", "bool"),
        ("is_answer", "bool"), ("is_interjection", "bool"),
        ("is_continuation_merged", "bool"), ("is_orphan_continuation", "bool"),
        ("has_known_speaker", "bool"), ("eligible_50_words", "bool"),
        ("eligible_100_words", "bool"), ("eligible_main_analysis", "bool"),
    ],
    "speaker_turns": [
        ("turn_key", "string"), ("source_file", "string"),
        ("source_file_sha256", "string"), ("turn_sequence", "int32"),
        ("date", "date32[day]"), ("chamber", "string"),
        ("first_fragment_sequence", "int32"), ("last_fragment_sequence", "int32"),
        ("major_heading_original", "string"), ("minor_heading_original", "string"),
        ("speaker_id_raw", "string"), ("speaker_name_raw", "string"),
        ("text_raw", "string"), ("text_clean", "string"),
        ("calculated_word_count", "int32"), ("fragment_count", "int32"),
        ("business_type", "string"), ("business_mapping_version", "string"),
        ("warning_codes", "list<element: string>"), ("is_presiding_officer", "bool"),
        ("is_collective_speaker", "bool"), ("is_procedural", "bool"),
        ("is_ceremonial", "bool"), ("is_division_related", "bool"),
        ("is_question_time", "bool"), ("is_question", "bool"),
        ("is_answer", "bool"), ("is_interjection", "bool"),
        ("is_orphan_continuation", "bool"), ("interrupted", "bool"),
        ("interruption_count", "int32"), ("has_known_speaker", "bool"),
        ("eligible_50_words", "bool"), ("eligible_100_words", "bool"),
        ("eligible_main_analysis", "bool"),
    ],
    "speaker_turn_fragments": [
        ("turn_key", "string"), ("fragment_key", "string"), ("ordinal", "int32"),
        ("relation_type", "string"), ("merge_reason_code", "string"),
        ("source_file", "string"), ("fragment_sequence", "int32"),
    ],
    "interjections": [
        ("interjection_key", "string"), ("source_fragment_key", "string"),
        ("source_file", "string"), ("source_file_sha256", "string"),
        ("sequence_number", "int32"), ("date", "date32[day]"),
        ("chamber", "string"), ("speaker_id_raw", "string"),
        ("speaker_name_raw", "string"), ("is_collective", "bool"),
        ("text_raw", "string"), ("text_clean", "string"),
        ("interrupted_turn_key", "string"), ("link_confidence", "string"),
        ("link_reason", "string"), ("major_heading_original", "string"),
        ("minor_heading_original", "string"),
        ("warning_codes", "list<element: string>"),
    ],
    "continuation_anomalies": [
        ("anomaly_key", "string"), ("fragment_key", "string"),
        ("source_file", "string"), ("sequence_number", "int32"),
        ("source_fragment_id", "string"), ("speaker_id_raw", "string"),
        ("speaker_name_raw", "string"), ("candidate_turn_key", "string"),
        ("candidate_speaker_id_raw", "string"),
        ("candidate_speaker_name_raw", "string"), ("reason_code", "string"),
        ("major_heading_original", "string"), ("minor_heading_original", "string"),
    ],
}


@dataclass(frozen=True)
class ValidatedRun:
    run_dir: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    artefact_count: int
    artefact_bytes: int
    dataset_rows: dict[str, int]
