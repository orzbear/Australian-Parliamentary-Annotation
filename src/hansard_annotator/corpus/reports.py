"""CSV anomaly/quality reports and run-level counters."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from hansard_annotator.corpus.hashing import canonical_json
from hansard_annotator.corpus.models import (
    FileFailure,
    FileProcessingRecord,
    FileResult,
)
from hansard_annotator.corpus.reconciliation import ReconciliationFailure

REPORT_FIELDS: dict[str, list[str]] = {
    "file_processing_report.csv": [
        "source_file",
        "source_file_sha256",
        "byte_size",
        "modification_time_ns_before",
        "modification_time_ns_after",
        "status",
        "error_code",
        "error_message",
        "fragment_count",
        "turn_count",
        "interjection_count",
        "continuation_anomaly_count",
        "warning_count",
        "reconciled",
    ],
    "corpus_summary.csv": ["measure", "value"],
    "unknown_headings.csv": [
        "major_heading_original",
        "occurrence_count",
        "first_source_file",
    ],
    "malformed_files.csv": [
        "source_file",
        "source_file_sha256",
        "error_code",
        "error_message",
    ],
    "missing_speakers.csv": [
        "source_file",
        "sequence_number",
        "fragment_key",
        "source_fragment_id",
        "speaker_id_raw",
        "speaker_name_raw",
        "nospeaker_raw",
    ],
    "continuation_anomalies.csv": [
        "anomaly_key",
        "fragment_key",
        "source_file",
        "sequence_number",
        "source_fragment_id",
        "speaker_id_raw",
        "speaker_name_raw",
        "candidate_turn_key",
        "candidate_speaker_id_raw",
        "candidate_speaker_name_raw",
        "reason_code",
        "major_heading_original",
        "minor_heading_original",
    ],
    "duplicate_ids.csv": [
        "source_file",
        "sequence_number",
        "fragment_key",
        "source_fragment_id",
    ],
    "image_elements.csv": [
        "image_key",
        "fragment_key",
        "source_file",
        "sequence_number",
        "image_index",
        "attributes_json",
        "alt_text",
        "clean_text_replacement",
        "major_heading_original",
        "minor_heading_original",
    ],
    "reconciliation_failures.csv": ["source_file", "failure_code", "detail"],
    "source_url_exceptions.csv": [
        "source_file",
        "sequence_number",
        "record_type",
        "source_url",
        "warning_code",
    ],
}


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return canonical_json(value)
    return value


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


class ReportCollector:
    def __init__(self) -> None:
        self.processing_records: list[FileProcessingRecord] = []
        self.file_failures: list[FileFailure] = []
        self.reconciliation_failures: list[ReconciliationFailure] = []
        self.missing_speakers: list[dict[str, Any]] = []
        self.continuation_anomalies: list[dict[str, Any]] = []
        self.duplicate_ids: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []
        self.source_url_exceptions: list[dict[str, Any]] = []
        self.unknown_headings: Counter[str] = Counter()
        self.unknown_heading_first_file: dict[str, str] = {}
        self.observed_tags: Counter[str] = Counter()
        self.observed_talktypes: Counter[str] = Counter()
        self.output_counts: Counter[str] = Counter()
        self.warning_counts: Counter[str] = Counter()
        self.reconstruction_counts: Counter[str] = Counter()

    def observe_result(self, result: FileResult, *, included_in_output: bool) -> None:
        self.observed_tags.update(result.observed_tag_counts)
        self.observed_talktypes.update(result.observed_talktype_counts)
        for fragment in result.fragments:
            self.warning_counts.update(fragment.warning_codes)
            if "UNKNOWN_BUSINESS_HEADING" in fragment.warning_codes:
                heading = fragment.major_heading_original or "<missing>"
                self.unknown_headings[heading] += 1
                self.unknown_heading_first_file.setdefault(
                    heading, fragment.source_file
                )
            if fragment.speaker_id_raw is None or fragment.speaker_name_raw is None:
                self.missing_speakers.append(
                    {
                        "source_file": fragment.source_file,
                        "sequence_number": fragment.sequence_number,
                        "fragment_key": fragment.fragment_key,
                        "source_fragment_id": fragment.source_fragment_id,
                        "speaker_id_raw": fragment.speaker_id_raw,
                        "speaker_name_raw": fragment.speaker_name_raw,
                        "nospeaker_raw": fragment.nospeaker_raw,
                    }
                )
            if "DUPLICATE_SOURCE_FRAGMENT_ID" in fragment.warning_codes:
                self.duplicate_ids.append(
                    {
                        "source_file": fragment.source_file,
                        "sequence_number": fragment.sequence_number,
                        "fragment_key": fragment.fragment_key,
                        "source_fragment_id": fragment.source_fragment_id,
                    }
                )
            if "SOURCE_URL_DATASET_MARKER_MISSING" in fragment.warning_codes:
                self.source_url_exceptions.append(
                    {
                        "source_file": fragment.source_file,
                        "sequence_number": fragment.sequence_number,
                        "record_type": "speech",
                        "source_url": fragment.source_url,
                        "warning_code": "SOURCE_URL_DATASET_MARKER_MISSING",
                    }
                )
            if fragment.is_continuation_merged:
                self.reconstruction_counts["continuations_merged"] += 1
            if fragment.is_orphan_continuation:
                self.reconstruction_counts["continuations_orphaned"] += 1

        for section in result.sections:
            self.warning_counts.update(section.warning_codes)
            if "SOURCE_URL_DATASET_MARKER_MISSING" in section.warning_codes:
                self.source_url_exceptions.append(
                    {
                        "source_file": section.source_file,
                        "sequence_number": section.sequence_number,
                        "record_type": section.section_type + "_heading",
                        "source_url": section.source_url,
                        "warning_code": "SOURCE_URL_DATASET_MARKER_MISSING",
                    }
                )
        for event in result.events:
            self.warning_counts.update(event.warning_codes)
            if "SOURCE_URL_DATASET_MARKER_MISSING" in event.warning_codes:
                self.source_url_exceptions.append(
                    {
                        "source_file": event.source_file,
                        "sequence_number": event.sequence_number,
                        "record_type": event.element_type,
                        "source_url": event.source_url,
                        "warning_code": "SOURCE_URL_DATASET_MARKER_MISSING",
                    }
                )

        self.continuation_anomalies.extend(
            anomaly.to_row() for anomaly in result.continuation_anomalies
        )
        self.images.extend(image.to_row() for image in result.images)
        self.reconstruction_counts["interjections_total"] += len(result.interjections)
        self.reconstruction_counts["interjections_linked"] += sum(
            interjection.interrupted_turn_key is not None
            for interjection in result.interjections
        )
        self.reconstruction_counts["collective_interjections"] += sum(
            interjection.is_collective is True
            for interjection in result.interjections
        )
        self.reconstruction_counts["collective_exception_applications"] += sum(
            interjection.link_reason == "COLLECTIVE_INTERJECTION_EXPLICIT_PATTERN"
            for interjection in result.interjections
        )

        if included_in_output:
            self.output_counts.update(
                {
                    "source_files": 1,
                    "debate_sections": len(result.sections),
                    "debate_events": len(result.events),
                    "speech_fragments": len(result.fragments),
                    "speaker_turns": len(result.turns),
                    "speaker_turn_fragments": len(result.turn_fragments),
                    "interjections": len(result.interjections),
                    "continuation_anomalies": len(result.continuation_anomalies),
                    "image_elements": len(result.images),
                }
            )

    def summary_rows(self, *, discovered_files: int, discovered_bytes: int) -> list[dict[str, Any]]:
        values: dict[str, Any] = {
            "discovered_files": discovered_files,
            "discovered_bytes": discovered_bytes,
            **{f"observed_tag_{key}": value for key, value in self.observed_tags.items()},
            **{
                f"observed_talktype_{key}": value
                for key, value in self.observed_talktypes.items()
            },
            **{f"output_{key}": value for key, value in self.output_counts.items()},
            **{
                f"reconstruction_{key}": value
                for key, value in self.reconstruction_counts.items()
            },
            "warning_occurrences": sum(self.warning_counts.values()),
            "warning_code_count": len(self.warning_counts),
            "failed_files": len(self.file_failures),
            "reconciliation_failure_count": len(self.reconciliation_failures),
            "unknown_business_heading_count": len(self.unknown_headings),
            "missing_speaker_fragment_count": len(self.missing_speakers),
            "duplicate_id_fragment_count": len(self.duplicate_ids),
            "image_element_count": len(self.images),
            "source_url_exception_count": len(self.source_url_exceptions),
        }
        return [
            {"measure": key, "value": value}
            for key, value in sorted(values.items())
        ]

    def write(self, report_directory: Path, *, discovered_files: int, discovered_bytes: int) -> None:
        unknown_rows = [
            {
                "major_heading_original": heading,
                "occurrence_count": count,
                "first_source_file": self.unknown_heading_first_file[heading],
            }
            for heading, count in sorted(self.unknown_headings.items())
        ]
        rows_by_name: dict[str, list[dict[str, Any]]] = {
            "file_processing_report.csv": [
                record.to_row() for record in self.processing_records
            ],
            "corpus_summary.csv": self.summary_rows(
                discovered_files=discovered_files,
                discovered_bytes=discovered_bytes,
            ),
            "unknown_headings.csv": unknown_rows,
            "malformed_files.csv": [
                vars(failure) for failure in self.file_failures
            ],
            "missing_speakers.csv": self.missing_speakers,
            "continuation_anomalies.csv": self.continuation_anomalies,
            "duplicate_ids.csv": self.duplicate_ids,
            "image_elements.csv": self.images,
            "reconciliation_failures.csv": [
                failure.to_row() for failure in self.reconciliation_failures
            ],
            "source_url_exceptions.csv": self.source_url_exceptions,
        }
        for filename, fieldnames in REPORT_FIELDS.items():
            write_csv(report_directory / filename, fieldnames, rows_by_name[filename])
