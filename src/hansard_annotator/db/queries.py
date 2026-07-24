"""Bounded, parameterised research inspection queries."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, text


def corpus_summary(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM corpus_year_summary ORDER BY year")
            ).mappings()
        ]


def inspect_turn(
    engine: Engine, turn_key: str, *, include_full_text: bool = False
) -> dict[str, Any] | None:
    limit = 1_000_000 if include_full_text else 500
    with engine.connect() as connection:
        turn = connection.execute(
            text(
                """
                SELECT t.turn_key,t.speech_date,t.chamber,t.speaker_id_raw,
                  t.speaker_name_raw,t.major_heading_original,t.minor_heading_original,
                  t.calculated_word_count,t.fragment_count,t.interrupted,
                  t.interruption_count,t.is_orphan_continuation,t.warning_codes,
                  left(t.text_clean,:limit) AS text_clean,
                  length(t.text_clean)>:limit AS text_truncated,sf.relative_path
                FROM speaker_turns t
                JOIN source_file_versions sfv ON sfv.id=t.source_file_version_id
                JOIN source_files sf ON sf.id=sfv.source_file_id
                WHERE t.turn_key=:key
                """
            ),
            {"key": turn_key, "limit": limit},
        ).mappings().first()
        if turn is None:
            return None
        lineage = connection.execute(
            text(
                """
                SELECT stf.ordinal,stf.relation_type,stf.merge_reason_code,
                  f.fragment_key,f.sequence_number,f.talktype_raw,
                  left(f.text_clean,:limit) AS text_clean
                FROM speaker_turns t
                JOIN speaker_turn_fragments stf ON stf.turn_id=t.id
                JOIN speech_fragments f ON f.id=stf.fragment_id
                WHERE t.turn_key=:key ORDER BY stf.ordinal
                """
            ),
            {"key": turn_key, "limit": limit},
        ).mappings()
        return {**dict(turn), "lineage": [dict(row) for row in lineage]}


def inspect_fragment(
    engine: Engine, fragment_key: str, *, include_full_text: bool = False
) -> dict[str, Any] | None:
    limit = 1_000_000 if include_full_text else 500
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT f.fragment_key,f.source_fragment_id,f.sequence_number,f.speech_date,
                  f.chamber,f.speaker_id_raw,f.speaker_name_raw,f.talktype_raw,f.time_raw,
                  f.parsed_time,f.calculated_word_count,f.parse_status,f.warning_codes,
                  f.is_interjection,f.is_continuation_merged,f.is_orphan_continuation,
                  left(f.text_clean,:limit) AS text_clean,
                  length(f.text_clean)>:limit AS text_truncated,sf.relative_path,
                  t.turn_key,i.interjection_key
                FROM speech_fragments f
                JOIN source_file_versions sfv ON sfv.id=f.source_file_version_id
                JOIN source_files sf ON sf.id=sfv.source_file_id
                LEFT JOIN speaker_turn_fragments stf ON stf.fragment_id=f.id
                LEFT JOIN speaker_turns t ON t.id=stf.turn_id
                LEFT JOIN interjections i ON i.fragment_id=f.id
                WHERE f.fragment_key=:key
                """
            ),
            {"key": fragment_key, "limit": limit},
        ).mappings().first()
        return None if row is None else dict(row)


def continuation_anomalies(engine: Engine, *, limit: int = 50) -> list[dict[str, Any]]:
    bounded = max(1, min(limit, 500))
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    SELECT ca.anomaly_key,f.fragment_key,ca.reason_code,ca.sequence_number,
                      ca.speaker_id_raw,ca.speaker_name_raw,sf.relative_path,
                      generated.turn_key AS generated_turn_key,
                      candidate.turn_key AS candidate_turn_key
                    FROM continuation_anomalies ca
                    JOIN speech_fragments f ON f.id=ca.fragment_id
                    JOIN source_file_versions sfv ON sfv.id=f.source_file_version_id
                    JOIN source_files sf ON sf.id=sfv.source_file_id
                    JOIN speaker_turns generated ON generated.id=ca.generated_turn_id
                    LEFT JOIN speaker_turns candidate ON candidate.id=ca.candidate_turn_id
                    ORDER BY sf.relative_path,ca.sequence_number LIMIT :limit
                    """
                ),
                {"limit": bounded},
            ).mappings()
        ]


def unlinked_interjections(engine: Engine, *, limit: int = 50) -> list[dict[str, Any]]:
    bounded = max(1, min(limit, 500))
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    SELECT i.interjection_key,i.sequence_number,i.speaker_id_raw,
                      i.speaker_name_raw,i.link_confidence,i.link_reason,
                      left(i.text_clean,500) AS text_clean,sf.relative_path
                    FROM interjections i
                    JOIN source_file_versions sfv ON sfv.id=i.source_file_version_id
                    JOIN source_files sf ON sf.id=sfv.source_file_id
                    WHERE i.interrupted_turn_id IS NULL
                    ORDER BY sf.relative_path,i.sequence_number LIMIT :limit
                    """
                ),
                {"limit": bounded},
            ).mappings()
        ]


def sample_turns(engine: Engine, year: int, *, limit: int = 10) -> list[dict[str, Any]]:
    bounded = max(1, min(limit, 100))
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    SELECT turn_key,speech_date,speaker_name_raw,business_type,
                      calculated_word_count,is_orphan_continuation,
                      left(text_clean,500) AS text_clean
                    FROM speaker_turns
                    WHERE EXTRACT(YEAR FROM speech_date)=:year
                    ORDER BY turn_key LIMIT :limit
                    """
                ),
                {"year": year, "limit": bounded},
            ).mappings()
        ]
