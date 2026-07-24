"""Explicit PyArrow schemas and bounded partitioned writers."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

STRING_LIST = pa.list_(pa.string())

SCHEMAS: dict[str, pa.Schema] = {
    "source_files": pa.schema(
        [
            ("source_file_key", pa.string()),
            ("source_file", pa.string()),
            ("source_file_sha256", pa.string()),
            ("byte_size", pa.int64()),
            ("modification_time_ns", pa.int64()),
            ("date", pa.date32()),
            ("chamber", pa.string()),
        ]
    ),
    "debate_sections": pa.schema(
        [
            ("section_key", pa.string()),
            ("source_file", pa.string()),
            ("source_file_sha256", pa.string()),
            ("sequence_number", pa.int32()),
            ("section_type", pa.string()),
            ("parent_section_key", pa.string()),
            ("heading_original", pa.string()),
            ("heading_clean", pa.string()),
            ("source_fragment_id", pa.string()),
            ("source_url", pa.string()),
            ("warning_codes", STRING_LIST),
        ]
    ),
    "debate_events": pa.schema(
        [
            ("event_key", pa.string()),
            ("source_file", pa.string()),
            ("source_file_sha256", pa.string()),
            ("sequence_number", pa.int32()),
            ("date", pa.date32()),
            ("chamber", pa.string()),
            ("element_type", pa.string()),
            ("source_fragment_id", pa.string()),
            ("major_heading_original", pa.string()),
            ("minor_heading_original", pa.string()),
            ("source_url", pa.string()),
            ("attributes_json", pa.string()),
            ("payload_json", pa.string()),
            ("bill_record_count", pa.int32()),
            ("warning_codes", STRING_LIST),
        ]
    ),
    "speech_fragments": pa.schema(
        [
            ("fragment_key", pa.string()),
            ("source_fragment_id", pa.string()),
            ("source_file", pa.string()),
            ("source_file_sha256", pa.string()),
            ("fragment_projection_sha256", pa.string()),
            ("sequence_number", pa.int32()),
            ("date", pa.date32()),
            ("chamber", pa.string()),
            ("major_section_key", pa.string()),
            ("minor_section_key", pa.string()),
            ("major_heading_original", pa.string()),
            ("minor_heading_original", pa.string()),
            ("speaker_id_raw", pa.string()),
            ("speaker_name_raw", pa.string()),
            ("talktype_raw", pa.string()),
            ("time_raw", pa.string()),
            ("parsed_time", pa.time32("s")),
            ("approximate_wordcount_raw", pa.string()),
            ("approximate_duration_raw", pa.string()),
            ("source_url", pa.string()),
            ("nospeaker_raw", pa.string()),
            ("attributes_json", pa.string()),
            ("text_raw", pa.string()),
            ("text_clean", pa.string()),
            ("calculated_word_count", pa.int32()),
            ("block_structure_json", pa.string()),
            ("parse_status", pa.string()),
            ("warning_codes", STRING_LIST),
            ("boundary_reason_before", pa.string()),
            ("business_type", pa.string()),
            ("business_mapping_version", pa.string()),
            ("is_presiding_officer", pa.bool_()),
            ("is_collective_speaker", pa.bool_()),
            ("is_procedural", pa.bool_()),
            ("is_ceremonial", pa.bool_()),
            ("is_division_related", pa.bool_()),
            ("is_question_time", pa.bool_()),
            ("is_question", pa.bool_()),
            ("is_answer", pa.bool_()),
            ("is_interjection", pa.bool_()),
            ("is_continuation_merged", pa.bool_()),
            ("is_orphan_continuation", pa.bool_()),
            ("has_known_speaker", pa.bool_()),
            ("eligible_50_words", pa.bool_()),
            ("eligible_100_words", pa.bool_()),
            ("eligible_main_analysis", pa.bool_()),
        ]
    ),
    "speaker_turns": pa.schema(
        [
            ("turn_key", pa.string()),
            ("source_file", pa.string()),
            ("source_file_sha256", pa.string()),
            ("turn_sequence", pa.int32()),
            ("date", pa.date32()),
            ("chamber", pa.string()),
            ("first_fragment_sequence", pa.int32()),
            ("last_fragment_sequence", pa.int32()),
            ("major_heading_original", pa.string()),
            ("minor_heading_original", pa.string()),
            ("speaker_id_raw", pa.string()),
            ("speaker_name_raw", pa.string()),
            ("text_raw", pa.string()),
            ("text_clean", pa.string()),
            ("calculated_word_count", pa.int32()),
            ("fragment_count", pa.int32()),
            ("business_type", pa.string()),
            ("business_mapping_version", pa.string()),
            ("warning_codes", STRING_LIST),
            ("is_presiding_officer", pa.bool_()),
            ("is_collective_speaker", pa.bool_()),
            ("is_procedural", pa.bool_()),
            ("is_ceremonial", pa.bool_()),
            ("is_division_related", pa.bool_()),
            ("is_question_time", pa.bool_()),
            ("is_question", pa.bool_()),
            ("is_answer", pa.bool_()),
            ("is_interjection", pa.bool_()),
            ("is_orphan_continuation", pa.bool_()),
            ("interrupted", pa.bool_()),
            ("interruption_count", pa.int32()),
            ("has_known_speaker", pa.bool_()),
            ("eligible_50_words", pa.bool_()),
            ("eligible_100_words", pa.bool_()),
            ("eligible_main_analysis", pa.bool_()),
        ]
    ),
    "speaker_turn_fragments": pa.schema(
        [
            ("turn_key", pa.string()),
            ("fragment_key", pa.string()),
            ("ordinal", pa.int32()),
            ("relation_type", pa.string()),
            ("merge_reason_code", pa.string()),
            ("source_file", pa.string()),
            ("fragment_sequence", pa.int32()),
        ]
    ),
    "interjections": pa.schema(
        [
            ("interjection_key", pa.string()),
            ("source_fragment_key", pa.string()),
            ("source_file", pa.string()),
            ("source_file_sha256", pa.string()),
            ("sequence_number", pa.int32()),
            ("date", pa.date32()),
            ("chamber", pa.string()),
            ("speaker_id_raw", pa.string()),
            ("speaker_name_raw", pa.string()),
            ("is_collective", pa.bool_()),
            ("text_raw", pa.string()),
            ("text_clean", pa.string()),
            ("interrupted_turn_key", pa.string()),
            ("link_confidence", pa.string()),
            ("link_reason", pa.string()),
            ("major_heading_original", pa.string()),
            ("minor_heading_original", pa.string()),
            ("warning_codes", STRING_LIST),
        ]
    ),
    "continuation_anomalies": pa.schema(
        [
            ("anomaly_key", pa.string()),
            ("fragment_key", pa.string()),
            ("source_file", pa.string()),
            ("sequence_number", pa.int32()),
            ("source_fragment_id", pa.string()),
            ("speaker_id_raw", pa.string()),
            ("speaker_name_raw", pa.string()),
            ("candidate_turn_key", pa.string()),
            ("candidate_speaker_id_raw", pa.string()),
            ("candidate_speaker_name_raw", pa.string()),
            ("reason_code", pa.string()),
            ("major_heading_original", pa.string()),
            ("minor_heading_original", pa.string()),
        ]
    ),
}


class PartitionedParquetWriter:
    """Buffer a bounded number of rows and write explicit year partitions."""

    def __init__(self, output_root: Path, *, batch_size: int = 5_000) -> None:
        self.output_root = output_root
        self.batch_size = batch_size
        self._buffers: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        self._parts: dict[tuple[str, int], int] = defaultdict(int)

    def append(
        self,
        dataset: str,
        year: int,
        rows: list[dict[str, Any]],
    ) -> None:
        if dataset not in SCHEMAS:
            raise KeyError(f"unknown Parquet dataset: {dataset}")
        if not rows:
            return
        key = (dataset, year)
        self._buffers[key].extend(rows)
        while len(self._buffers[key]) >= self.batch_size:
            chunk = self._buffers[key][: self.batch_size]
            del self._buffers[key][: self.batch_size]
            self._write_chunk(dataset, year, chunk)

    def _write_chunk(
        self,
        dataset: str,
        year: int,
        rows: list[dict[str, Any]],
    ) -> None:
        key = (dataset, year)
        part_number = self._parts[key]
        self._parts[key] += 1
        directory = self.output_root / dataset / f"year={year:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"part-{part_number:05d}.parquet"
        temporary = destination.with_suffix(".parquet.tmp")
        table = pa.Table.from_pylist(rows, schema=SCHEMAS[dataset])
        pq.write_table(  # type: ignore[no-untyped-call]
            table,
            temporary,
            compression="zstd",
            use_dictionary=True,
            write_statistics=True,
        )
        temporary.replace(destination)

    def close(self) -> None:
        for (dataset, year), rows in sorted(self._buffers.items()):
            if rows:
                self._write_chunk(dataset, year, rows)
        self._buffers.clear()

    def flush_year(self, year: int) -> None:
        """Flush all underfilled buffers for a completed year partition."""
        for (dataset, buffered_year), rows in sorted(self._buffers.items()):
            if buffered_year == year and rows:
                self._write_chunk(dataset, buffered_year, rows)
                rows.clear()
