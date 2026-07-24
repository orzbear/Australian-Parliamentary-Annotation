"""Transactional, idempotent COPY/staging importer for accepted Phase 1 data."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.db import IMPORTER_VERSION, SCHEMA_VERSION
from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.db.exceptions import ImportConflictError, ImportValidationError
from hansard_annotator.db.import_models import EXPECTED_FIELDS, ValidatedRun
from hansard_annotator.db.staging import copy_dict_payloads, copy_parquet_payloads
from hansard_annotator.db.validator import csv_row_count, validate_run_directory


def _now() -> datetime:
    return datetime.now(UTC)


def _first_value(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _peak_resident_memory_bytes() -> int | None:
    try:
        import psutil

        memory = psutil.Process().memory_info()
        return int(getattr(memory, "peak_wset", memory.rss))
    except (ImportError, OSError):
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _create_attempt(
    settings: DatabaseSettings, validated: ValidatedRun
) -> int:
    with psycopg.connect(settings.psycopg_url, autocommit=True) as connection:
        row = connection.execute(
                """
                INSERT INTO database_import_runs
                  (started_at,status,importer_version,schema_version,manifest_sha256,
                   inventory_sha256,source_directory)
                VALUES (%s,'running',%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    _now(),
                    IMPORTER_VERSION,
                    SCHEMA_VERSION,
                    validated.manifest_sha256,
                    validated.manifest["input_inventory_sha256"],
                    validated.run_dir.name,
                ),
            ).fetchone()
        assert row is not None
        return int(_first_value(row))


def _finish_attempt(
    settings: DatabaseSettings,
    attempt_id: int,
    *,
    status: str,
    preprocessing_run_id: int | None = None,
    counts: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> None:
    summary = None if error is None else str(error).replace("\n", " ")[:500]
    code = None if error is None else type(error).__name__
    with psycopg.connect(settings.psycopg_url, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE database_import_runs
            SET preprocessing_run_id=%s, completed_at=%s, status=%s,
                counts=%s::jsonb, verification_results=%s::jsonb,
                error_code=%s, error_summary=%s
            WHERE id=%s
            """,
            (
                preprocessing_run_id,
                _now(),
                status,
                json.dumps(counts) if counts is not None else None,
                json.dumps(verification) if verification is not None else None,
                code,
                summary,
                attempt_id,
            ),
        )


def _check_conflict(
    connection: psycopg.Connection[Any], table: str, key_column: str, payload_key: str
) -> None:
    allowed = {
        ("source_file_versions", "source_file_key", "source_file_key"),
        ("debate_sections", "section_key", "section_key"),
        ("debate_events", "event_key", "event_key"),
        ("speech_fragments", "fragment_key", "fragment_key"),
        ("speaker_turns", "turn_key", "turn_key"),
        ("interjections", "interjection_key", "interjection_key"),
        ("continuation_anomalies", "anomaly_key", "anomaly_key"),
        ("image_anomalies", "image_key", "image_key"),
    }
    if (table, key_column, payload_key) not in allowed:
        raise AssertionError("unsafe conflict-check identifier")
    row = connection.execute(
        f"""
        SELECT s.payload->>%s, t.record_sha256, s.record_sha256
        FROM stage_payload s JOIN {table} t
          ON t.{key_column}=s.payload->>%s
        WHERE t.record_sha256 <> s.record_sha256
        LIMIT 1
        """,
        (payload_key, payload_key),
    ).fetchone()
    if row:
        raise ImportConflictError(
            f"conflicting deterministic key in {table}: {_first_value(row)}"
        )


def _stage_dataset(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
    name: str,
    *,
    transform: Any = None,
) -> int:
    return copy_parquet_payloads(
        connection,
        validated.run_dir / name,
        columns=[field for field, _ in EXPECTED_FIELDS[name]],
        batch_size=settings.batch_size,
        transform=transform,
    )


def _insert_preprocessing_run(
    connection: psycopg.Connection[Any], validated: ValidatedRun
) -> int:
    manifest = validated.manifest
    git_commit = manifest.get("git_commit") or None
    corpus_row = connection.execute(
        """
        SELECT id FROM corpora
        WHERE slug='australian-house-representatives-hansard' AND is_active
        """
    ).fetchone()
    if corpus_row is None:
        raise ImportValidationError("Australian corpus registry record is missing")
    corpus_id = int(_first_value(corpus_row))
    row = connection.execute(
        """
        INSERT INTO preprocessing_runs
              (corpus_id,run_name,pipeline_version,git_commit,inventory_sha256,manifest_sha256,
               configuration_hashes,source_file_count,source_byte_count,manifest,
               processing_started_at,processing_completed_at,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s,'importing')
            RETURNING id
            """,
            (
                corpus_id,
                manifest["run_id"],
                manifest["pipeline_version"],
                git_commit,
                manifest["input_inventory_sha256"],
                validated.manifest_sha256,
                json.dumps(manifest["configuration_hashes"]),
                len(manifest["source_files"]),
                sum(item["byte_size"] for item in manifest["source_files"]),
                json.dumps(manifest),
                manifest["started_at"],
                manifest["completed_at"],
            ),
        ).fetchone()
    assert row is not None
    return int(_first_value(row))


def _load_source_files(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
    preprocessing_run_id: int,
) -> int:
    report = {
        row["source_file"]: row
        for row in _read_csv(validated.run_dir / "reports" / "file_processing_report.csv")
    }

    def enrich(row: dict[str, Any]) -> dict[str, Any]:
        evidence = report.get(str(row["source_file"]))
        if evidence is None:
            raise ImportValidationError(f"missing file processing evidence: {row['source_file']}")
        row["modification_time_ns_after"] = int(evidence["modification_time_ns_after"])
        row["processing_status"] = evidence["status"]
        row["reconciled"] = evidence["reconciled"] == "True"
        row["source_url_marker_valid"] = True
        return row

    count = _stage_dataset(
        connection, validated, settings, "source_files", transform=enrich
    )
    corpus_id = int(
        _first_value(
            connection.execute(
                "SELECT corpus_id FROM preprocessing_runs WHERE id=%s",
                (preprocessing_run_id,),
            ).fetchone()
        )
    )
    connection.execute(
        """
        INSERT INTO source_files
          (corpus_id,relative_path,speech_date,chamber,folder_year)
        SELECT %s,payload->>'source_file', (payload->>'date')::date,
               payload->>'chamber',
               split_part(payload->>'source_file','/',1)::integer
        FROM stage_payload
        ON CONFLICT (corpus_id,relative_path) DO NOTHING
        """,
        (corpus_id,),
    )
    _check_conflict(
        connection, "source_file_versions", "source_file_key", "source_file_key"
    )
    connection.execute(
        """
        INSERT INTO source_file_versions
          (source_file_key,source_file_id,preprocessing_run_id,source_sha256,byte_size,
           modification_time_ns,modification_time_ns_after,processing_status,reconciled,
           source_url_marker_valid,record_sha256)
        SELECT p.payload->>'source_file_key', sf.id, %s,
               p.payload->>'source_file_sha256', (p.payload->>'byte_size')::bigint,
               (p.payload->>'modification_time_ns')::bigint,
               (p.payload->>'modification_time_ns_after')::bigint,
               p.payload->>'processing_status', (p.payload->>'reconciled')::boolean,
               (p.payload->>'source_url_marker_valid')::boolean, p.record_sha256
        FROM stage_payload p JOIN source_files sf
          ON sf.relative_path=p.payload->>'source_file' AND sf.corpus_id=%s
        """,
        (preprocessing_run_id, corpus_id),
    )
    return count


def _load_sections(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
    preprocessing_run_id: int,
) -> int:
    count = _stage_dataset(connection, validated, settings, "debate_sections")
    _check_conflict(connection, "debate_sections", "section_key", "section_key")
    common = """
      SELECT p.payload->>'section_key', %s, sfv.id, {parent},
        (p.payload->>'sequence_number')::integer, p.payload->>'section_type',
        p.payload->>'heading_original', p.payload->>'heading_clean',
        p.payload->>'source_fragment_id', p.payload->>'source_url',
        ARRAY(SELECT jsonb_array_elements_text(p.payload->'warning_codes')),
        p.record_sha256
      FROM stage_payload p
      JOIN source_files sf ON sf.relative_path=p.payload->>'source_file'
      JOIN source_file_versions sfv ON sfv.source_file_id=sf.id
        AND sfv.preprocessing_run_id=%s
      {parent_join}
      WHERE p.payload->>'section_type'={level}
    """
    connection.execute(
        """
        INSERT INTO debate_sections
          (section_key,preprocessing_run_id,source_file_version_id,parent_section_id,
           sequence_number,section_level,heading_original,heading_normalised,
           source_heading_id,source_url,warning_codes,record_sha256)
        """
        + common.format(
            parent="NULL", parent_join="", level="'major'"
        ),
        (preprocessing_run_id, preprocessing_run_id),
    )
    connection.execute(
        """
        INSERT INTO debate_sections
          (section_key,preprocessing_run_id,source_file_version_id,parent_section_id,
           sequence_number,section_level,heading_original,heading_normalised,
           source_heading_id,source_url,warning_codes,record_sha256)
        """
        + common.format(
            parent="parent.id",
            parent_join="JOIN debate_sections parent ON parent.section_key=p.payload->>'parent_section_key'",
            level="'minor'",
        ),
        (preprocessing_run_id, preprocessing_run_id),
    )
    return count


def _load_events(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
    preprocessing_run_id: int,
) -> int:
    count = _stage_dataset(connection, validated, settings, "debate_events")
    _check_conflict(connection, "debate_events", "event_key", "event_key")
    connection.execute(
        """
        INSERT INTO debate_events
          (event_key,preprocessing_run_id,source_file_version_id,major_section_id,
           minor_section_id,sequence_number,speech_date,chamber,event_type,source_event_id,
           major_heading_original,minor_heading_original,source_url,attributes,payload,
           bill_record_count,warning_codes,record_sha256)
        SELECT p.payload->>'event_key',%s,sfv.id,major.id,minor.id,
          (p.payload->>'sequence_number')::integer,(p.payload->>'date')::date,
          p.payload->>'chamber',p.payload->>'element_type',p.payload->>'source_fragment_id',
          p.payload->>'major_heading_original',p.payload->>'minor_heading_original',
          p.payload->>'source_url',(p.payload->>'attributes_json')::jsonb,
          (p.payload->>'payload_json')::jsonb,(p.payload->>'bill_record_count')::integer,
          ARRAY(SELECT jsonb_array_elements_text(p.payload->'warning_codes')),p.record_sha256
        FROM stage_payload p
        JOIN source_files sf ON sf.relative_path=p.payload->>'source_file'
        JOIN source_file_versions sfv ON sfv.source_file_id=sf.id
          AND sfv.preprocessing_run_id=%s
        LEFT JOIN LATERAL (
          SELECT id FROM debate_sections s WHERE s.source_file_version_id=sfv.id
            AND s.section_level='major'
            AND s.sequence_number < (p.payload->>'sequence_number')::integer
          ORDER BY s.sequence_number DESC LIMIT 1
        ) major ON true
        LEFT JOIN LATERAL (
          SELECT id FROM debate_sections s WHERE s.source_file_version_id=sfv.id
            AND s.section_level='minor'
            AND s.sequence_number < (p.payload->>'sequence_number')::integer
          ORDER BY s.sequence_number DESC LIMIT 1
        ) minor ON true
        """,
        (preprocessing_run_id, preprocessing_run_id),
    )
    return count


def _load_fragments(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
    preprocessing_run_id: int,
) -> int:
    count = _stage_dataset(connection, validated, settings, "speech_fragments")
    _check_conflict(connection, "speech_fragments", "fragment_key", "fragment_key")
    connection.execute(
        """
        INSERT INTO speech_fragments
          (fragment_key,preprocessing_run_id,source_file_version_id,major_section_id,
           minor_section_id,source_fragment_id,fragment_projection_sha256,sequence_number,
           speech_date,chamber,major_heading_original,minor_heading_original,
           speaker_id_raw,speaker_name_raw,talktype_raw,time_raw,parsed_time,
           approximate_wordcount_raw,approximate_duration_raw,source_url,nospeaker_raw,
           attributes,text_raw,text_clean,calculated_word_count,block_structure,
           parse_status,warning_codes,boundary_reason_before,business_type,
           business_mapping_version,is_presiding_officer,is_collective_speaker,
           is_procedural,is_ceremonial,is_division_related,is_question_time,is_question,
           is_answer,is_interjection,is_continuation_merged,is_orphan_continuation,
           has_known_speaker,eligible_50_words,eligible_100_words,eligible_main_analysis,
           record_sha256)
        SELECT p.payload->>'fragment_key',%s,sfv.id,major.id,minor.id,
          p.payload->>'source_fragment_id',p.payload->>'fragment_projection_sha256',
          (p.payload->>'sequence_number')::integer,(p.payload->>'date')::date,
          p.payload->>'chamber',p.payload->>'major_heading_original',
          p.payload->>'minor_heading_original',p.payload->>'speaker_id_raw',
          p.payload->>'speaker_name_raw',p.payload->>'talktype_raw',p.payload->>'time_raw',
          (p.payload->>'parsed_time')::time(3),p.payload->>'approximate_wordcount_raw',
          p.payload->>'approximate_duration_raw',p.payload->>'source_url',
          p.payload->>'nospeaker_raw',(p.payload->>'attributes_json')::jsonb,
          p.payload->>'text_raw',p.payload->>'text_clean',
          (p.payload->>'calculated_word_count')::integer,
          (p.payload->>'block_structure_json')::jsonb,p.payload->>'parse_status',
          ARRAY(SELECT jsonb_array_elements_text(p.payload->'warning_codes')),
          p.payload->>'boundary_reason_before',p.payload->>'business_type',
          p.payload->>'business_mapping_version',
          (p.payload->>'is_presiding_officer')::boolean,
          (p.payload->>'is_collective_speaker')::boolean,
          (p.payload->>'is_procedural')::boolean,(p.payload->>'is_ceremonial')::boolean,
          (p.payload->>'is_division_related')::boolean,
          (p.payload->>'is_question_time')::boolean,(p.payload->>'is_question')::boolean,
          (p.payload->>'is_answer')::boolean,(p.payload->>'is_interjection')::boolean,
          (p.payload->>'is_continuation_merged')::boolean,
          (p.payload->>'is_orphan_continuation')::boolean,
          (p.payload->>'has_known_speaker')::boolean,
          (p.payload->>'eligible_50_words')::boolean,
          (p.payload->>'eligible_100_words')::boolean,
          (p.payload->>'eligible_main_analysis')::boolean,p.record_sha256
        FROM stage_payload p
        JOIN source_files sf ON sf.relative_path=p.payload->>'source_file'
        JOIN source_file_versions sfv ON sfv.source_file_id=sf.id
          AND sfv.preprocessing_run_id=%s
        LEFT JOIN debate_sections major
          ON major.section_key=nullif(p.payload->>'major_section_key','')
        LEFT JOIN debate_sections minor
          ON minor.section_key=nullif(p.payload->>'minor_section_key','')
        """,
        (preprocessing_run_id, preprocessing_run_id),
    )
    return count


def _create_reconstruction_run(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    preprocessing_run_id: int,
) -> int:
    hashes = validated.manifest["configuration_hashes"]
    version = validated.manifest["configuration_versions"]["reconstruction"]
    row = connection.execute(
            """
            INSERT INTO reconstruction_runs
              (preprocessing_run_id,reconstruction_version,structural_rules_hash,
               collective_allowlist_hash,business_mapping_hash,status)
            VALUES (%s,%s,%s,%s,%s,'importing') RETURNING id
            """,
            (
                preprocessing_run_id,
                version,
                hashes["reconstruction"],
                hashes["reconstruction"],
                hashes["business_types"],
            ),
        ).fetchone()
    assert row is not None
    return int(_first_value(row))


def _load_turns(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
    reconstruction_run_id: int,
    preprocessing_run_id: int,
) -> int:
    count = _stage_dataset(connection, validated, settings, "speaker_turns")
    _check_conflict(connection, "speaker_turns", "turn_key", "turn_key")
    connection.execute(
        """
        INSERT INTO speaker_turns
          (turn_key,reconstruction_run_id,source_file_version_id,major_section_id,
           minor_section_id,turn_sequence,speech_date,chamber,first_fragment_sequence,
           last_fragment_sequence,major_heading_original,minor_heading_original,
           speaker_id_raw,speaker_name_raw,text_raw,text_clean,calculated_word_count,
           fragment_count,business_type,business_mapping_version,warning_codes,
           is_presiding_officer,is_collective_speaker,is_procedural,is_ceremonial,
           is_division_related,is_question_time,is_question,is_answer,is_interjection,
           is_orphan_continuation,interrupted,interruption_count,has_known_speaker,
           eligible_50_words,eligible_100_words,eligible_main_analysis,record_sha256)
        SELECT p.payload->>'turn_key',%s,sfv.id,firstf.major_section_id,
          firstf.minor_section_id,(p.payload->>'turn_sequence')::integer,
          (p.payload->>'date')::date,p.payload->>'chamber',
          (p.payload->>'first_fragment_sequence')::integer,
          (p.payload->>'last_fragment_sequence')::integer,
          p.payload->>'major_heading_original',p.payload->>'minor_heading_original',
          p.payload->>'speaker_id_raw',p.payload->>'speaker_name_raw',
          p.payload->>'text_raw',p.payload->>'text_clean',
          (p.payload->>'calculated_word_count')::integer,
          (p.payload->>'fragment_count')::integer,p.payload->>'business_type',
          p.payload->>'business_mapping_version',
          ARRAY(SELECT jsonb_array_elements_text(p.payload->'warning_codes')),
          (p.payload->>'is_presiding_officer')::boolean,
          (p.payload->>'is_collective_speaker')::boolean,
          (p.payload->>'is_procedural')::boolean,(p.payload->>'is_ceremonial')::boolean,
          (p.payload->>'is_division_related')::boolean,
          (p.payload->>'is_question_time')::boolean,(p.payload->>'is_question')::boolean,
          (p.payload->>'is_answer')::boolean,(p.payload->>'is_interjection')::boolean,
          (p.payload->>'is_orphan_continuation')::boolean,
          (p.payload->>'interrupted')::boolean,
          (p.payload->>'interruption_count')::integer,
          (p.payload->>'has_known_speaker')::boolean,
          (p.payload->>'eligible_50_words')::boolean,
          (p.payload->>'eligible_100_words')::boolean,
          (p.payload->>'eligible_main_analysis')::boolean,p.record_sha256
        FROM stage_payload p
        JOIN source_files sf ON sf.relative_path=p.payload->>'source_file'
        JOIN source_file_versions sfv ON sfv.source_file_id=sf.id
          AND sfv.preprocessing_run_id=%s
        JOIN speech_fragments firstf ON firstf.source_file_version_id=sfv.id
          AND firstf.sequence_number=(p.payload->>'first_fragment_sequence')::integer
        """,
        (reconstruction_run_id, preprocessing_run_id),
    )
    return count


def _load_lineage(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
) -> int:
    count = _stage_dataset(connection, validated, settings, "speaker_turn_fragments")
    connection.execute(
        """
        INSERT INTO speaker_turn_fragments
          (turn_id,ordinal,fragment_id,relation_type,merge_reason_code,
           fragment_sequence,record_sha256)
        SELECT t.id,(p.payload->>'ordinal')::integer,f.id,
          p.payload->>'relation_type',p.payload->>'merge_reason_code',
          (p.payload->>'fragment_sequence')::integer,p.record_sha256
        FROM stage_payload p
        JOIN speaker_turns t ON t.turn_key=p.payload->>'turn_key'
        JOIN speech_fragments f ON f.fragment_key=p.payload->>'fragment_key'
        """
    )
    return count


def _load_interjections(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
    reconstruction_run_id: int,
    preprocessing_run_id: int,
) -> int:
    count = _stage_dataset(connection, validated, settings, "interjections")
    _check_conflict(connection, "interjections", "interjection_key", "interjection_key")
    connection.execute(
        """
        INSERT INTO interjections
          (interjection_key,reconstruction_run_id,fragment_id,interrupted_turn_id,
           source_file_version_id,sequence_number,speech_date,chamber,speaker_id_raw,
           speaker_name_raw,text_raw,text_clean,is_collective,link_confidence,link_reason,
           major_heading_original,minor_heading_original,warning_codes,record_sha256)
        SELECT p.payload->>'interjection_key',%s,f.id,t.id,sfv.id,
          (p.payload->>'sequence_number')::integer,(p.payload->>'date')::date,
          p.payload->>'chamber',p.payload->>'speaker_id_raw',p.payload->>'speaker_name_raw',
          p.payload->>'text_raw',p.payload->>'text_clean',
          (p.payload->>'is_collective')::boolean,p.payload->>'link_confidence',
          p.payload->>'link_reason',p.payload->>'major_heading_original',
          p.payload->>'minor_heading_original',
          ARRAY(SELECT jsonb_array_elements_text(p.payload->'warning_codes')),
          p.record_sha256
        FROM stage_payload p
        JOIN speech_fragments f ON f.fragment_key=p.payload->>'source_fragment_key'
        JOIN source_files sf ON sf.relative_path=p.payload->>'source_file'
        JOIN source_file_versions sfv ON sfv.source_file_id=sf.id
          AND sfv.preprocessing_run_id=%s
        LEFT JOIN speaker_turns t ON t.turn_key=p.payload->>'interrupted_turn_key'
        """,
        (reconstruction_run_id, preprocessing_run_id),
    )
    return count


def _load_continuation_anomalies(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    settings: DatabaseSettings,
    preprocessing_run_id: int,
    reconstruction_run_id: int,
) -> int:
    count = _stage_dataset(connection, validated, settings, "continuation_anomalies")
    _check_conflict(
        connection, "continuation_anomalies", "anomaly_key", "anomaly_key"
    )
    connection.execute(
        """
        INSERT INTO continuation_anomalies
          (anomaly_key,preprocessing_run_id,reconstruction_run_id,fragment_id,
           generated_turn_id,candidate_turn_id,sequence_number,source_fragment_id,
           speaker_id_raw,speaker_name_raw,candidate_speaker_id_raw,
           candidate_speaker_name_raw,reason_code,major_heading_original,
           minor_heading_original,record_sha256)
        SELECT p.payload->>'anomaly_key',%s,%s,f.id,generated.id,candidate.id,
          (p.payload->>'sequence_number')::integer,p.payload->>'source_fragment_id',
          p.payload->>'speaker_id_raw',p.payload->>'speaker_name_raw',
          p.payload->>'candidate_speaker_id_raw',p.payload->>'candidate_speaker_name_raw',
          p.payload->>'reason_code',p.payload->>'major_heading_original',
          p.payload->>'minor_heading_original',p.record_sha256
        FROM stage_payload p
        JOIN speech_fragments f ON f.fragment_key=p.payload->>'fragment_key'
        JOIN speaker_turn_fragments stf ON stf.fragment_id=f.id
          AND stf.relation_type='orphan_continuation'
        JOIN speaker_turns generated ON generated.id=stf.turn_id
        LEFT JOIN speaker_turns candidate ON candidate.turn_key=p.payload->>'candidate_turn_key'
        """,
        (preprocessing_run_id, reconstruction_run_id),
    )
    return count


def _load_images(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    preprocessing_run_id: int,
) -> int:
    rows = _read_csv(validated.run_dir / "reports" / "image_elements.csv")
    copy_dict_payloads(connection, rows)
    _check_conflict(connection, "image_anomalies", "image_key", "image_key")
    connection.execute(
        """
        INSERT INTO image_anomalies
          (image_key,preprocessing_run_id,fragment_id,element_ordinal,
           element_attributes,alt_text,clean_text_replacement,source_file,
           sequence_number,major_heading_original,minor_heading_original,record_sha256)
        SELECT p.payload->>'image_key',%s,f.id,(p.payload->>'image_index')::integer,
          (p.payload->>'attributes_json')::jsonb,p.payload->>'alt_text',
          p.payload->>'clean_text_replacement',p.payload->>'source_file',
          (p.payload->>'sequence_number')::integer,p.payload->>'major_heading_original',
          p.payload->>'minor_heading_original',p.record_sha256
        FROM stage_payload p
        JOIN speech_fragments f ON f.fragment_key=p.payload->>'fragment_key'
        """,
        (preprocessing_run_id,),
    )
    return len(rows)


def _load_artifacts(
    connection: psycopg.Connection[Any],
    validated: ValidatedRun,
    preprocessing_run_id: int,
) -> int:
    rows: list[dict[str, Any]] = []
    for entry in validated.manifest["output_files"]:
        relative = entry["path"]
        path = validated.run_dir / relative
        suffix = path.suffix.lower()
        row_count: int | None = None
        schema: dict[str, Any] | None = None
        if suffix == ".parquet":
            import pyarrow.parquet as pq

            metadata = pq.ParquetFile(path).metadata  # type: ignore[no-untyped-call]
            row_count = metadata.num_rows
            schema = {
                "fields": [
                    {"name": field.name, "type": str(field.type), "nullable": field.nullable}
                    for field in pq.read_schema(path)  # type: ignore[no-untyped-call]
                ]
            }
        elif suffix == ".csv":
            row_count = csv_row_count(path)
        rows.append(
            {
                "relative_path": relative,
                "artefact_type": suffix.removeprefix("."),
                "sha256": entry["sha256"],
                "byte_size": entry["bytes"],
                "row_count": row_count,
                "parquet_schema": schema,
            }
        )
    manifest_path = validated.run_dir / "run_manifest.json"
    rows.append(
        {
            "relative_path": "run_manifest.json",
            "artefact_type": "manifest",
            "sha256": validated.manifest_sha256,
            "byte_size": manifest_path.stat().st_size,
            "row_count": None,
            "parquet_schema": None,
        }
    )
    copy_dict_payloads(connection, rows)
    connection.execute(
        """
        INSERT INTO import_artifacts
          (preprocessing_run_id,relative_path,artefact_type,sha256,byte_size,
           row_count,parquet_schema)
        SELECT %s,payload->>'relative_path',payload->>'artefact_type',payload->>'sha256',
          (payload->>'byte_size')::bigint,(payload->>'row_count')::bigint,
          payload->'parquet_schema'
        FROM stage_payload
        """,
        (preprocessing_run_id,),
    )
    return len(rows)


def import_validated_run(
    settings: DatabaseSettings, validated: ValidatedRun
) -> dict[str, Any]:
    from hansard_annotator.db.verification import verify_database

    attempt_id = _create_attempt(settings, validated)
    started = perf_counter()
    try:
        with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
            existing = connection.execute(
                """
                SELECT id,manifest_sha256,status FROM preprocessing_runs
                WHERE run_name=%s
                """,
                (validated.manifest["run_id"],),
            ).fetchone()
            if existing:
                if (
                    existing["manifest_sha256"] != validated.manifest_sha256
                    or existing["status"] != "accepted"
                ):
                    raise ImportConflictError(
                        "run name already exists with different or incomplete content"
                    )
                verification = verify_database(connection, int(existing["id"]), validated)
                result = {
                    "status": "completed_reused",
                    "preprocessing_run_id": int(existing["id"]),
                    "counts": verification["counts"],
                    "verification": verification,
                    "duration_seconds": perf_counter() - started,
                    "peak_resident_memory_bytes": _peak_resident_memory_bytes(),
                    "corpus_writes": 0,
                    "attempt_id": attempt_id,
                }
                connection.rollback()
                _finish_attempt(
                    settings,
                    attempt_id,
                    status="completed_reused",
                    preprocessing_run_id=int(existing["id"]),
                    counts=verification["counts"],
                    verification=verification,
                )
                return result

            connection.execute(
                "SET LOCAL lock_timeout='30s'; SET LOCAL statement_timeout='0'"
            )
            connection.execute(
                "CREATE TEMP TABLE stage_payload "
                "(payload jsonb NOT NULL, record_sha256 char(64) NOT NULL) ON COMMIT DROP"
            )
            pre_id = _insert_preprocessing_run(connection, validated)
            counts: dict[str, int] = {}
            counts["source_files"] = _load_source_files(
                connection, validated, settings, pre_id
            )
            counts["debate_sections"] = _load_sections(
                connection, validated, settings, pre_id
            )
            counts["debate_events"] = _load_events(
                connection, validated, settings, pre_id
            )
            counts["speech_fragments"] = _load_fragments(
                connection, validated, settings, pre_id
            )
            reconstruction_id = _create_reconstruction_run(
                connection, validated, pre_id
            )
            counts["speaker_turns"] = _load_turns(
                connection, validated, settings, reconstruction_id, pre_id
            )
            counts["speaker_turn_fragments"] = _load_lineage(
                connection, validated, settings
            )
            counts["interjections"] = _load_interjections(
                connection, validated, settings, reconstruction_id, pre_id
            )
            counts["continuation_anomalies"] = _load_continuation_anomalies(
                connection, validated, settings, pre_id, reconstruction_id
            )
            counts["image_elements"] = _load_images(connection, validated, pre_id)
            _load_artifacts(connection, validated, pre_id)
            connection.execute(
                "UPDATE reconstruction_runs SET status='accepted' WHERE id=%s",
                (reconstruction_id,),
            )
            connection.execute(
                """
                UPDATE preprocessing_runs SET status='accepted',imported_at=%s
                WHERE id=%s
                """,
                (_now(), pre_id),
            )
            connection.execute(
                """
                UPDATE corpora SET current_preprocessing_run_id=%s,updated_at=now()
                WHERE id=(SELECT corpus_id FROM preprocessing_runs WHERE id=%s)
                """,
                (pre_id, pre_id),
            )
            verification = verify_database(connection, pre_id, validated)
            connection.commit()
        result = {
            "status": "completed",
            "preprocessing_run_id": pre_id,
            "counts": counts,
            "verification": verification,
            "duration_seconds": perf_counter() - started,
            "peak_resident_memory_bytes": _peak_resident_memory_bytes(),
            "largest_batch_size": settings.batch_size,
            "attempt_id": attempt_id,
        }
        _finish_attempt(
            settings,
            attempt_id,
            status="completed",
            preprocessing_run_id=pre_id,
            counts=counts,
            verification=verification,
        )
        return result
    except Exception as error:
        _finish_attempt(settings, attempt_id, status="rolled_back", error=error)
        raise


def import_run(settings: DatabaseSettings, run_dir: Path) -> dict[str, Any]:
    validated = validate_run_directory(run_dir, settings.processed_data_root)
    return import_validated_run(settings, validated)
