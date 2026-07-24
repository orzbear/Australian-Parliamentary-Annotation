"""SQL- and Python-level corpus reconciliation."""

from __future__ import annotations

from time import perf_counter
from typing import Any

import psycopg

from hansard_annotator.db.exceptions import VerificationError
from hansard_annotator.db.import_models import (
    ACCEPTED_COUNTS,
    ACCEPTED_RUN_NAME,
    ValidatedRun,
)

COUNT_SQL = {
    "source_files": """
      SELECT count(*) FROM source_file_versions WHERE preprocessing_run_id=%s
    """,
    "debate_sections": """
      SELECT count(*) FROM debate_sections WHERE preprocessing_run_id=%s
    """,
    "debate_events": """
      SELECT count(*) FROM debate_events WHERE preprocessing_run_id=%s
    """,
    "speech_fragments": """
      SELECT count(*) FROM speech_fragments WHERE preprocessing_run_id=%s
    """,
    "speaker_turns": """
      SELECT count(*) FROM speaker_turns t JOIN reconstruction_runs r
        ON r.id=t.reconstruction_run_id WHERE r.preprocessing_run_id=%s
    """,
    "speaker_turn_fragments": """
      SELECT count(*) FROM speaker_turn_fragments stf
      JOIN speaker_turns t ON t.id=stf.turn_id
      JOIN reconstruction_runs r ON r.id=t.reconstruction_run_id
      WHERE r.preprocessing_run_id=%s
    """,
    "interjections": """
      SELECT count(*) FROM interjections i JOIN reconstruction_runs r
        ON r.id=i.reconstruction_run_id WHERE r.preprocessing_run_id=%s
    """,
    "continuation_anomalies": """
      SELECT count(*) FROM continuation_anomalies WHERE preprocessing_run_id=%s
    """,
    "image_elements": """
      SELECT count(*) FROM image_anomalies WHERE preprocessing_run_id=%s
    """,
}

INVARIANTS = {
    "fragment_source_references": """
      SELECT count(*) FROM speech_fragments f
      LEFT JOIN source_file_versions sfv ON sfv.id=f.source_file_version_id
      WHERE f.preprocessing_run_id=%s
        AND (sfv.id IS NULL OR sfv.preprocessing_run_id<>f.preprocessing_run_id)
    """,
    "turn_source_and_reconstruction": """
      SELECT count(*) FROM speaker_turns t
      JOIN reconstruction_runs r ON r.id=t.reconstruction_run_id
      LEFT JOIN source_file_versions sfv ON sfv.id=t.source_file_version_id
      WHERE r.preprocessing_run_id=%s
        AND (sfv.id IS NULL OR sfv.preprocessing_run_id<>r.preprocessing_run_id)
    """,
    "lineage_references_and_compatibility": """
      SELECT count(*) FROM speaker_turn_fragments stf
      JOIN speaker_turns t ON t.id=stf.turn_id
      JOIN reconstruction_runs r ON r.id=t.reconstruction_run_id
      JOIN speech_fragments f ON f.id=stf.fragment_id
      WHERE r.preprocessing_run_id=%s
        AND (f.preprocessing_run_id<>r.preprocessing_run_id
          OR f.source_file_version_id<>t.source_file_version_id
          OR f.sequence_number<>stf.fragment_sequence)
    """,
    "interjection_in_lineage": """
      SELECT count(*) FROM interjections i
      JOIN reconstruction_runs r ON r.id=i.reconstruction_run_id
      JOIN speaker_turn_fragments stf ON stf.fragment_id=i.fragment_id
      WHERE r.preprocessing_run_id=%s
    """,
    "continuation_double_count": """
      SELECT count(*) FROM speech_fragments f
      LEFT JOIN speaker_turn_fragments stf ON stf.fragment_id=f.id
      LEFT JOIN continuation_anomalies ca ON ca.fragment_id=f.id
      WHERE f.preprocessing_run_id=%s AND f.talktype_raw='continuation'
        AND ((stf.relation_type='continuation' AND ca.id IS NOT NULL)
          OR (f.is_continuation_merged AND f.is_orphan_continuation))
    """,
    "fragment_disposition": """
      SELECT count(*) FROM speech_fragments f
      LEFT JOIN speaker_turn_fragments stf ON stf.fragment_id=f.id
      LEFT JOIN interjections i ON i.fragment_id=f.id
      WHERE f.preprocessing_run_id=%s
        AND ((stf.fragment_id IS NULL AND i.id IS NULL)
          OR (stf.fragment_id IS NOT NULL AND i.id IS NOT NULL))
    """,
    "source_file_status": """
      SELECT count(*) FROM source_file_versions
      WHERE preprocessing_run_id=%s
        AND (processing_status<>'completed' OR NOT reconciled
          OR NOT source_url_marker_valid)
    """,
    "turn_lineage_counts": """
      SELECT count(*) FROM turns_with_fragment_counts v
      JOIN speaker_turns t ON t.id=v.id
      JOIN reconstruction_runs r ON r.id=t.reconstruction_run_id
      WHERE r.preprocessing_run_id=%s AND NOT v.count_matches
    """,
    "orphan_anomaly_correspondence": """
      SELECT abs(
        (SELECT count(*) FROM speaker_turns t JOIN reconstruction_runs r
          ON r.id=t.reconstruction_run_id
          WHERE r.preprocessing_run_id=%s AND t.is_orphan_continuation)
        -
        (SELECT count(*) FROM continuation_anomalies
          WHERE preprocessing_run_id=%s)
      )
    """,
    "artifact_metadata": """
      SELECT count(*) FROM import_artifacts
      WHERE preprocessing_run_id=%s AND
        (sha256 !~ '^[0-9a-f]{64}$' OR byte_size < 0)
    """,
}


def _scalar(
    connection: psycopg.Connection[Any], sql: str, parameters: tuple[Any, ...]
) -> int:
    row = connection.execute(sql, parameters).fetchone()
    assert row is not None
    if isinstance(row, dict):
        return int(next(iter(row.values())))
    return int(row[0])


def verify_database(
    connection: psycopg.Connection[Any],
    preprocessing_run_id: int,
    validated: ValidatedRun | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    run = connection.execute(
        """
        SELECT run_name,status,inventory_sha256,manifest_sha256,source_file_count,
               source_byte_count
        FROM preprocessing_runs WHERE id=%s
        """,
        (preprocessing_run_id,),
    ).fetchone()
    if run is None:
        raise VerificationError("preprocessing run does not exist")
    get = (lambda key: run[key]) if isinstance(run, dict) else None
    status = get("status") if get else run[1]
    inventory = get("inventory_sha256") if get else run[2]
    if status != "accepted":
        raise VerificationError("preprocessing run is not accepted")
    if validated and inventory != validated.manifest["input_inventory_sha256"]:
        raise VerificationError("database inventory SHA-256 differs from manifest")

    counts = {
        name: _scalar(connection, sql, (preprocessing_run_id,))
        for name, sql in COUNT_SQL.items()
    }
    expected_counts = (
        ACCEPTED_COUNTS
        if validated is None
        else {name: int(validated.manifest["record_counts"][name]) for name in counts}
    )
    mismatched_counts = {
        name: {"actual": value, "expected": expected_counts[name]}
        for name, value in counts.items()
        if value != expected_counts[name]
    }
    if mismatched_counts:
        raise VerificationError(f"accepted count mismatch: {mismatched_counts}")

    invariant_failures: dict[str, int] = {}
    for name, sql in INVARIANTS.items():
        parameters = (
            (preprocessing_run_id, preprocessing_run_id)
            if name == "orphan_anomaly_correspondence"
            else (preprocessing_run_id,)
        )
        failures = _scalar(connection, sql, parameters)
        if failures:
            invariant_failures[name] = failures
    if invariant_failures:
        raise VerificationError(f"reconciliation failures: {invariant_failures}")

    reason_rows = connection.execute(
        """
        SELECT reason_code,count(*) AS count FROM continuation_anomalies
        WHERE preprocessing_run_id=%s GROUP BY reason_code ORDER BY reason_code
        """,
        (preprocessing_run_id,),
    ).fetchall()
    reason_counts = {
        (row["reason_code"] if isinstance(row, dict) else row[0]):
        int(row["count"] if isinstance(row, dict) else row[1])
        for row in reason_rows
    }
    expected_reasons = {
        "KNOWN_SPEAKER_ID_MISMATCH": 7050,
        "IDENTITY_MISSING_OR_AMBIGUOUS": 1089,
        "NO_ACTIVE_CANDIDATE": 97,
        "DIVISION_BOUNDARY": 3,
    }
    enforce_accepted_statistics = (
        validated is None or validated.manifest.get("run_id") == ACCEPTED_RUN_NAME
    )
    if enforce_accepted_statistics and reason_counts != expected_reasons:
        raise VerificationError(f"continuation reason mismatch: {reason_counts}")

    linked = _scalar(
        connection,
        """
        SELECT count(*) FROM interjections i JOIN reconstruction_runs r
          ON r.id=i.reconstruction_run_id
        WHERE r.preprocessing_run_id=%s AND i.interrupted_turn_id IS NOT NULL
        """,
        (preprocessing_run_id,),
    )
    if enforce_accepted_statistics and linked != 77_562:
        raise VerificationError(f"linked interjection mismatch: {linked}")
    artifact_count = _scalar(
        connection,
        "SELECT count(*) FROM import_artifacts WHERE preprocessing_run_id=%s",
        (preprocessing_run_id,),
    )
    expected_artifacts = (validated.artefact_count + 1) if validated else 260
    if artifact_count != expected_artifacts:
        raise VerificationError(
            f"import artefact count {artifact_count} != {expected_artifacts}"
        )

    return {
        "ok": True,
        "counts": counts,
        "invariant_failures": {},
        "continuation_reason_counts": reason_counts,
        "linked_interjections": linked,
        "unlinked_interjections": counts["interjections"] - linked,
        "artifact_count": artifact_count,
        "duration_seconds": perf_counter() - started,
    }


def database_sizes(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    database = connection.execute(
        "SELECT current_database() AS name, pg_database_size(current_database()) AS bytes"
    ).fetchone()
    assert database is not None
    rows = connection.execute(
        """
        SELECT relname,
          pg_relation_size(relid) AS table_bytes,
          pg_indexes_size(relid) AS index_bytes,
          pg_total_relation_size(relid) AS total_bytes
        FROM pg_catalog.pg_statio_user_tables ORDER BY relname
        """
    ).fetchall()
    return {
        "database": dict(database) if isinstance(database, dict) else {
            "name": database[0], "bytes": database[1]
        },
        "relations": [
            dict(row) if isinstance(row, dict) else {
                "relname": row[0],
                "table_bytes": row[1],
                "index_bytes": row[2],
                "total_bytes": row[3],
            }
            for row in rows
        ],
    }
