"""Conservative, versioned speaker-turn reconstruction."""

from __future__ import annotations

import re
from dataclasses import dataclass

from hansard_annotator.corpus.config import ReconstructionConfig
from hansard_annotator.corpus.hashing import normalise_unicode, stable_key
from hansard_annotator.corpus.models import (
    ContinuationAnomaly,
    FileResult,
    Interjection,
    SpeakerTurn,
    SpeechFragment,
    TurnFragment,
)
from hansard_annotator.corpus.parser import PIPELINE_VERSION
from hansard_annotator.corpus.text_cleaning import count_words

BLOCKING_IDENTITY_WARNINGS = {
    "DUPLICATE_SOURCE_FRAGMENT_ID",
    "SOURCE_FRAGMENT_ID_MISSING",
}


@dataclass
class Candidate:
    turn: SpeakerTurn
    identity_fragment: SpeechFragment


def normalise_identity(value: str | None) -> str:
    return " ".join(normalise_unicode(value or "").split()).casefold()


def _known_speaker_id(value: str | None, config: ReconstructionConfig) -> bool:
    return normalise_identity(value) not in {
        normalise_identity(item) for item in config.unknown_speaker_ids
    }


def _collective_name(value: str | None, config: ReconstructionConfig) -> bool:
    normalised = normalise_identity(value)
    return normalised in {
        normalise_identity(item) for item in config.collective_speaker_allowlist
    }


def _valid_fallback_name(value: str | None, config: ReconstructionConfig) -> bool:
    normalised = normalise_identity(value)
    return bool(normalised) and normalised not in {
        *{normalise_identity(item) for item in config.unknown_speaker_names},
        *{normalise_identity(item) for item in config.collective_speaker_allowlist},
    }


def _collective_interruption_match(
    fragment: SpeechFragment,
    config: ReconstructionConfig,
) -> bool:
    if _known_speaker_id(fragment.speaker_id_raw, config):
        return False
    if not _collective_name(fragment.speaker_name_raw, config):
        return False
    name_key = next(
        (
            name
            for name in config.collective_speaker_allowlist
            if normalise_identity(name) == normalise_identity(fragment.speaker_name_raw)
        ),
        None,
    )
    if name_key is None:
        return False
    pattern = config.collective_interruption_patterns.get(name_key)
    if pattern is None:
        return False
    return re.fullmatch(pattern, " ".join(fragment.text_clean.split())) is not None


def _identity_match(
    candidate: SpeechFragment,
    continuation: SpeechFragment,
    config: ReconstructionConfig,
) -> tuple[bool, str]:
    if BLOCKING_IDENTITY_WARNINGS.intersection(candidate.warning_codes) or (
        BLOCKING_IDENTITY_WARNINGS.intersection(continuation.warning_codes)
    ):
        return False, "BLOCKING_IDENTITY_WARNING"

    candidate_id_known = _known_speaker_id(candidate.speaker_id_raw, config)
    continuation_id_known = _known_speaker_id(continuation.speaker_id_raw, config)
    if candidate_id_known and continuation_id_known:
        if candidate.speaker_id_raw == continuation.speaker_id_raw:
            return True, "CONTINUATION_SPEAKER_ID_MATCH"
        return False, "KNOWN_SPEAKER_ID_MISMATCH"

    candidate_name = normalise_identity(candidate.speaker_name_raw)
    continuation_name = normalise_identity(continuation.speaker_name_raw)
    if (
        candidate_name == continuation_name
        and _valid_fallback_name(candidate.speaker_name_raw, config)
        and _valid_fallback_name(continuation.speaker_name_raw, config)
    ):
        return True, "CONTINUATION_EXACT_NAME_FALLBACK"
    return False, "IDENTITY_MISSING_OR_AMBIGUOUS"


def _apply_fragment_flags(
    fragment: SpeechFragment,
    config: ReconstructionConfig,
) -> None:
    if _collective_name(fragment.speaker_name_raw, config):
        fragment.is_collective_speaker = True
    elif _known_speaker_id(fragment.speaker_id_raw, config):
        fragment.is_collective_speaker = False
    else:
        fragment.is_collective_speaker = None
    fragment.has_known_speaker = _known_speaker_id(fragment.speaker_id_raw, config)
    fragment.eligible_50_words = fragment.calculated_word_count >= 50
    fragment.eligible_100_words = fragment.calculated_word_count >= 100
    if fragment.is_procedural is None or fragment.is_ceremonial is None:
        fragment.eligible_main_analysis = None
    else:
        fragment.eligible_main_analysis = bool(
            fragment.eligible_50_words
            and fragment.has_known_speaker
            and not fragment.is_collective_speaker
            and not fragment.is_interjection
            and fragment.talktype_raw != "continuation"
            and not fragment.is_procedural
            and not fragment.is_ceremonial
        )


def _create_turn(
    fragment: SpeechFragment,
    *,
    turn_sequence: int,
    orphan: bool,
) -> SpeakerTurn:
    return SpeakerTurn(
        turn_key=stable_key(
            kind="speaker_turn",
            pipeline_version=PIPELINE_VERSION,
            source_file_sha256=fragment.source_file_sha256,
            sequence_number=fragment.sequence_number,
            discriminator="orphan" if orphan else "initial",
        ),
        source_file=fragment.source_file,
        source_file_sha256=fragment.source_file_sha256,
        turn_sequence=turn_sequence,
        date=fragment.date,
        chamber=fragment.chamber,
        first_fragment_sequence=fragment.sequence_number,
        last_fragment_sequence=fragment.sequence_number,
        major_heading_original=fragment.major_heading_original,
        minor_heading_original=fragment.minor_heading_original,
        speaker_id_raw=fragment.speaker_id_raw,
        speaker_name_raw=fragment.speaker_name_raw,
        text_raw=fragment.text_raw,
        text_clean=fragment.text_clean,
        calculated_word_count=fragment.calculated_word_count,
        fragment_count=1,
        business_type=fragment.business_type,
        business_mapping_version=fragment.business_mapping_version,
        warning_codes=list(fragment.warning_codes),
        is_presiding_officer=fragment.is_presiding_officer,
        is_collective_speaker=fragment.is_collective_speaker,
        is_procedural=fragment.is_procedural,
        is_ceremonial=fragment.is_ceremonial,
        is_division_related=fragment.is_division_related,
        is_question_time=fragment.is_question_time,
        is_question=fragment.is_question,
        is_answer=fragment.is_answer,
        is_interjection=False,
        is_orphan_continuation=orphan,
        interrupted=False,
        interruption_count=0,
        has_known_speaker=fragment.has_known_speaker,
        eligible_50_words=fragment.eligible_50_words,
        eligible_100_words=fragment.eligible_100_words,
        eligible_main_analysis=fragment.eligible_main_analysis,
    )


def _refresh_turn_eligibility(turn: SpeakerTurn) -> None:
    turn.calculated_word_count = count_words(turn.text_clean)
    turn.eligible_50_words = turn.calculated_word_count >= 50
    turn.eligible_100_words = turn.calculated_word_count >= 100
    if turn.is_procedural is None or turn.is_ceremonial is None:
        turn.eligible_main_analysis = None
    else:
        turn.eligible_main_analysis = bool(
            turn.eligible_50_words
            and turn.has_known_speaker
            and not turn.is_collective_speaker
            and not turn.is_procedural
            and not turn.is_ceremonial
            and not turn.is_orphan_continuation
        )


def _create_interjection(
    fragment: SpeechFragment,
    *,
    candidate: Candidate | None,
    is_collective: bool | None,
    link_reason: str,
) -> Interjection:
    linked = candidate is not None
    return Interjection(
        interjection_key=stable_key(
            kind="interjection",
            pipeline_version=PIPELINE_VERSION,
            source_file_sha256=fragment.source_file_sha256,
            sequence_number=fragment.sequence_number,
            discriminator="collective" if is_collective else "individual",
        ),
        source_fragment_key=fragment.fragment_key,
        source_file=fragment.source_file,
        source_file_sha256=fragment.source_file_sha256,
        sequence_number=fragment.sequence_number,
        date=fragment.date,
        chamber=fragment.chamber,
        speaker_id_raw=fragment.speaker_id_raw,
        speaker_name_raw=fragment.speaker_name_raw,
        is_collective=is_collective,
        text_raw=fragment.text_raw,
        text_clean=fragment.text_clean,
        interrupted_turn_key=candidate.turn.turn_key if candidate else None,
        link_confidence="high" if linked else "none",
        link_reason=link_reason,
        major_heading_original=fragment.major_heading_original,
        minor_heading_original=fragment.minor_heading_original,
        warning_codes=list(fragment.warning_codes),
    )


def reconstruct_file(
    result: FileResult,
    *,
    config: ReconstructionConfig,
) -> FileResult:
    candidate: Candidate | None = None
    turn_sequence = 0

    for fragment in result.fragments:
        _apply_fragment_flags(fragment, config)
        previous_candidate = candidate
        if fragment.boundary_before:
            candidate = None

        is_collective_exception = (
            fragment.talktype_raw == "speech"
            and _collective_interruption_match(fragment, config)
        )
        is_ordinary_interjection = fragment.talktype_raw == "interjection"

        if is_ordinary_interjection or is_collective_exception:
            fragment.is_interjection = True
            active = candidate
            if is_collective_exception and active is not None:
                link_reason = config.collective_interruption_reason_code
                fragment.warning_codes = sorted(
                    set(
                        [
                            *fragment.warning_codes,
                            config.collective_interruption_reason_code,
                        ]
                    )
                )
            elif active is not None:
                link_reason = "ACTIVE_TURN_SAME_SCOPE"
            elif fragment.boundary_before:
                link_reason = fragment.boundary_reason_before or "BOUNDARY_BEFORE_INTERJECTION"
            else:
                link_reason = "NO_ACTIVE_TURN"
            interjection = _create_interjection(
                fragment,
                candidate=active,
                is_collective=fragment.is_collective_speaker,
                link_reason=link_reason,
            )
            result.interjections.append(interjection)
            if active is not None:
                active.turn.interrupted = True
                active.turn.interruption_count += 1
            continue

        if fragment.talktype_raw == "continuation":
            if fragment.boundary_before:
                merge = False
                reason = fragment.boundary_reason_before or "BOUNDARY_BEFORE_CONTINUATION"
            elif candidate is None:
                merge = False
                reason = "NO_ACTIVE_CANDIDATE"
            elif (
                candidate.turn.major_heading_original != fragment.major_heading_original
                or candidate.turn.minor_heading_original != fragment.minor_heading_original
            ):
                merge = False
                reason = "HEADING_SCOPE_CHANGED"
            else:
                merge, reason = _identity_match(
                    candidate.identity_fragment,
                    fragment,
                    config,
                )

            if merge and candidate is not None:
                turn = candidate.turn
                separator_raw = "\n\n" if turn.text_raw and fragment.text_raw else ""
                separator_clean = "\n\n" if turn.text_clean and fragment.text_clean else ""
                turn.text_raw += separator_raw + fragment.text_raw
                turn.text_clean += separator_clean + fragment.text_clean
                turn.last_fragment_sequence = fragment.sequence_number
                turn.fragment_count += 1
                turn.warning_codes = sorted(set([*turn.warning_codes, *fragment.warning_codes]))
                fragment.is_continuation_merged = True
                result.turn_fragments.append(
                    TurnFragment(
                        turn_key=turn.turn_key,
                        fragment_key=fragment.fragment_key,
                        ordinal=turn.fragment_count,
                        relation_type="continuation",
                        merge_reason_code=reason,
                        source_file=fragment.source_file,
                        fragment_sequence=fragment.sequence_number,
                    )
                )
                _refresh_turn_eligibility(turn)
                continue

            fragment.is_orphan_continuation = True
            turn_sequence += 1
            orphan_turn = _create_turn(
                fragment,
                turn_sequence=turn_sequence,
                orphan=True,
            )
            result.turns.append(orphan_turn)
            result.turn_fragments.append(
                TurnFragment(
                    turn_key=orphan_turn.turn_key,
                    fragment_key=fragment.fragment_key,
                    ordinal=1,
                    relation_type="orphan_continuation",
                    merge_reason_code=reason,
                    source_file=fragment.source_file,
                    fragment_sequence=fragment.sequence_number,
                )
            )
            result.continuation_anomalies.append(
                ContinuationAnomaly(
                    anomaly_key=stable_key(
                        kind="continuation_anomaly",
                        pipeline_version=PIPELINE_VERSION,
                        source_file_sha256=fragment.source_file_sha256,
                        sequence_number=fragment.sequence_number,
                        discriminator=reason,
                    ),
                    fragment_key=fragment.fragment_key,
                    source_file=fragment.source_file,
                    sequence_number=fragment.sequence_number,
                    source_fragment_id=fragment.source_fragment_id,
                    speaker_id_raw=fragment.speaker_id_raw,
                    speaker_name_raw=fragment.speaker_name_raw,
                    candidate_turn_key=(
                        previous_candidate.turn.turn_key if previous_candidate else None
                    ),
                    candidate_speaker_id_raw=(
                        previous_candidate.identity_fragment.speaker_id_raw
                        if previous_candidate
                        else None
                    ),
                    candidate_speaker_name_raw=(
                        previous_candidate.identity_fragment.speaker_name_raw
                        if previous_candidate
                        else None
                    ),
                    reason_code=reason,
                    major_heading_original=fragment.major_heading_original,
                    minor_heading_original=fragment.minor_heading_original,
                )
            )
            if (
                _valid_fallback_name(fragment.speaker_name_raw, config)
                and not BLOCKING_IDENTITY_WARNINGS.intersection(fragment.warning_codes)
            ):
                candidate = Candidate(orphan_turn, fragment)
            else:
                candidate = None
            continue

        turn_sequence += 1
        turn = _create_turn(fragment, turn_sequence=turn_sequence, orphan=False)
        result.turns.append(turn)
        result.turn_fragments.append(
            TurnFragment(
                turn_key=turn.turn_key,
                fragment_key=fragment.fragment_key,
                ordinal=1,
                relation_type="initial",
                merge_reason_code="INITIAL_SPEECH",
                source_file=fragment.source_file,
                fragment_sequence=fragment.sequence_number,
            )
        )
        if (
            fragment.talktype_raw == "speech"
            and _valid_fallback_name(fragment.speaker_name_raw, config)
            and not fragment.is_collective_speaker
            and fragment.nospeaker_raw != "true"
        ):
            candidate = Candidate(turn, fragment)
        else:
            candidate = None

    return result
