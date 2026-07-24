"""Hardened, file-atomic streaming parser for debates-style Hansard XML."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, time
from pathlib import Path
from typing import Any

from lxml import etree

from hansard_annotator.corpus.business_types import (
    BusinessTypeConfig,
)
from hansard_annotator.corpus.config import ReconstructionConfig
from hansard_annotator.corpus.hashing import (
    canonical_json,
    fragment_projection_sha256,
    stable_key,
)
from hansard_annotator.corpus.models import (
    DebateEvent,
    DebateSection,
    DiscoveredFile,
    FileResult,
    ImageElement,
    SpeechFragment,
)
from hansard_annotator.corpus.text_cleaning import (
    clean_whitespace,
    extract_speech_text,
    local_name,
    raw_whitespace,
)

PIPELINE_VERSION = "1.0.0"
DOCTYPE_SCAN_BYTES = 65_536
KNOWN_TOP_LEVEL = {"major-heading", "minor-heading", "speech", "division", "bills"}
KNOWN_TALKTYPES = {"speech", "continuation", "interjection"}
INTEGER_RE = re.compile(r"^\d+$")


class FileParseError(RuntimeError):
    """A sanitised per-file failure; callers must discard partial output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message[:500]


def _parse_time(raw: str | None) -> tuple[time | None, str | None]:
    if raw is None or raw == "" or raw == "unknown":
        return None, "TIME_MISSING_OR_UNKNOWN"
    for pattern, format_string in (
        (r"^\d{2}:\d{2}$", "%H:%M"),
        (r"^\d{2}:\d{2}:\d{2}$", "%H:%M:%S"),
    ):
        if re.fullmatch(pattern, raw):
            try:
                return datetime.strptime(raw, format_string).time(), None
            except ValueError:
                break
    return None, "TIME_MALFORMED"


def _validate_integer(raw: str | None, field_name: str) -> str | None:
    if raw is None or raw == "":
        return f"{field_name}_MISSING"
    if not INTEGER_RE.fullmatch(raw):
        return f"{field_name}_MALFORMED"
    return None


def _normalised_attributes(element: etree._Element) -> dict[str, str]:
    return dict(sorted((str(key), str(value)) for key, value in element.attrib.items()))


def _element_payload(element: etree._Element) -> dict[str, Any]:
    return {
        "element_type": local_name(element.tag),
        "attributes": _normalised_attributes(element),
        "text": raw_whitespace(element.text or ""),
        "children": [_element_payload(child) for child in element],
        "tail": raw_whitespace(element.tail or ""),
    }


def _source_url_exception(url: str | None, expected_marker: str) -> bool:
    return (
        url is not None
        and url != ""
        and expected_marker.casefold() not in url.casefold()
    )


def _clear_element(element: etree._Element) -> None:
    parent = element.getparent()
    element.clear()
    if parent is not None:
        while element.getprevious() is not None:
            del parent[0]


def _check_doctype(path: Path) -> None:
    with path.open("rb") as source:
        prefix = source.read(DOCTYPE_SCAN_BYTES).upper()
    if b"<!DOCTYPE" in prefix:
        raise FileParseError("UNSAFE_DOCTYPE", "DOCTYPE declarations are prohibited")


def _check_text_size(element: etree._Element, maximum_characters: int) -> None:
    for value in (element.text, element.tail):
        if value is not None and len(value) > maximum_characters:
            raise FileParseError(
                "TEXT_SIZE_LIMIT_EXCEEDED",
                "individual XML text node exceeds configured ceiling",
            )


def _apply_business_mapping(
    fragment: SpeechFragment,
    business_config: BusinessTypeConfig,
) -> None:
    rule = business_config.lookup(fragment.major_heading_original)
    fragment.business_mapping_version = business_config.version
    if rule is None:
        fragment.warning_codes.append("UNKNOWN_BUSINESS_HEADING")
        fragment.is_procedural = None
        fragment.is_ceremonial = None
        fragment.is_question_time = None
        fragment.is_division_related = None
        return
    fragment.business_type = rule.business_type
    fragment.is_procedural = rule.is_procedural
    fragment.is_ceremonial = rule.is_ceremonial
    fragment.is_question_time = rule.is_question_time
    fragment.is_division_related = rule.is_division_related


def _create_fragment(
    *,
    element: etree._Element,
    source: DiscoveredFile,
    sequence_number: int,
    chamber: str,
    major_section_key: str | None,
    minor_section_key: str | None,
    major_heading: str | None,
    minor_heading: str | None,
    boundary_before: bool,
    boundary_reason_before: str | None,
    expected_source_url_marker: str,
    business_config: BusinessTypeConfig,
) -> tuple[SpeechFragment, list[ImageElement], bool]:
    attributes = _normalised_attributes(element)
    extracted = extract_speech_text(element)
    warning_codes = list(extracted.warning_codes)

    source_fragment_id = attributes.get("id")
    speaker_id = attributes.get("speakerid")
    speaker_name = attributes.get("speakername")
    talktype = attributes.get("talktype")
    time_raw = attributes.get("time")
    parsed_time, time_warning = _parse_time(time_raw)
    if time_warning:
        warning_codes.append(time_warning)

    for raw, field in (
        (attributes.get("approximate_wordcount"), "WORDCOUNT"),
        (attributes.get("approximate_duration"), "DURATION"),
    ):
        warning = _validate_integer(raw, field)
        if warning:
            warning_codes.append(warning)

    if source_fragment_id is None:
        warning_codes.append("SOURCE_FRAGMENT_ID_MISSING")
    if speaker_id is None:
        warning_codes.append("SPEAKER_ID_MISSING")
    if speaker_name is None:
        warning_codes.append("SPEAKER_NAME_MISSING")
    if talktype not in KNOWN_TALKTYPES:
        warning_codes.append("TALKTYPE_UNKNOWN")

    source_url = attributes.get("url")
    url_exception = _source_url_exception(source_url, expected_source_url_marker)
    if url_exception:
        warning_codes.append("SOURCE_URL_DATASET_MARKER_MISSING")

    fragment_key = stable_key(
        kind="speech_fragment",
        pipeline_version=PIPELINE_VERSION,
        source_file_sha256=source.source_file_sha256,
        sequence_number=sequence_number,
        discriminator=source_fragment_id or "",
    )
    projection_hash = fragment_projection_sha256(
        source_file_sha256=source.source_file_sha256,
        sequence_number=sequence_number,
        element_type="speech",
        raw_attributes=attributes,
        text_raw=extracted.text_raw,
        major_heading_original=major_heading,
        minor_heading_original=minor_heading,
    )
    fragment = SpeechFragment(
        fragment_key=fragment_key,
        source_fragment_id=source_fragment_id,
        source_file=source.relative_path,
        source_file_sha256=source.source_file_sha256,
        fragment_projection_sha256=projection_hash,
        sequence_number=sequence_number,
        date=source.sitting_date,
        chamber=chamber,
        major_section_key=major_section_key,
        minor_section_key=minor_section_key,
        major_heading_original=major_heading,
        minor_heading_original=minor_heading,
        speaker_id_raw=speaker_id,
        speaker_name_raw=speaker_name,
        talktype_raw=talktype,
        time_raw=time_raw,
        parsed_time=parsed_time,
        approximate_wordcount_raw=attributes.get("approximate_wordcount"),
        approximate_duration_raw=attributes.get("approximate_duration"),
        source_url=source_url,
        nospeaker_raw=attributes.get("nospeaker"),
        attributes_json=canonical_json(attributes),
        text_raw=extracted.text_raw,
        text_clean=extracted.text_clean,
        calculated_word_count=extracted.calculated_word_count,
        block_structure_json=extracted.block_structure_json,
        parse_status="parsed",
        warning_codes=sorted(set(warning_codes)),
        boundary_before=boundary_before,
        boundary_reason_before=boundary_reason_before,
        is_interjection=talktype == "interjection",
    )
    _apply_business_mapping(fragment, business_config)

    image_rows = [
        ImageElement(
            image_key=stable_key(
                kind="image_element",
                pipeline_version=PIPELINE_VERSION,
                source_file_sha256=source.source_file_sha256,
                sequence_number=sequence_number,
                discriminator=str(image.index),
            ),
            fragment_key=fragment_key,
            source_file=source.relative_path,
            sequence_number=sequence_number,
            image_index=image.index,
            attributes_json=canonical_json(image.attributes),
            alt_text=image.alt_text,
            clean_text_replacement=image.replacement,
            major_heading_original=major_heading,
            minor_heading_original=minor_heading,
        )
        for image in extracted.images
    ]
    return fragment, image_rows, url_exception


def _create_section(
    *,
    element: etree._Element,
    source: DiscoveredFile,
    sequence_number: int,
    section_type: str,
    parent_section_key: str | None,
    expected_source_url_marker: str,
) -> tuple[DebateSection, bool]:
    heading_original = raw_whitespace("".join(str(text) for text in element.itertext()))
    heading_clean = clean_whitespace(heading_original)
    attributes = _normalised_attributes(element)
    source_url = attributes.get("url")
    url_exception = _source_url_exception(source_url, expected_source_url_marker)
    warnings = ["SOURCE_URL_DATASET_MARKER_MISSING"] if url_exception else []
    if not heading_clean:
        warnings.append("EMPTY_HEADING")
    section_key = stable_key(
        kind=f"{section_type}_section",
        pipeline_version=PIPELINE_VERSION,
        source_file_sha256=source.source_file_sha256,
        sequence_number=sequence_number,
        discriminator=attributes.get("id", ""),
    )
    return (
        DebateSection(
            section_key=section_key,
            source_file=source.relative_path,
            source_file_sha256=source.source_file_sha256,
            sequence_number=sequence_number,
            section_type=section_type,
            parent_section_key=parent_section_key,
            heading_original=heading_original,
            heading_clean=heading_clean,
            source_fragment_id=attributes.get("id"),
            source_url=source_url,
            warning_codes=warnings,
        ),
        url_exception,
    )


def _create_event(
    *,
    element: etree._Element,
    source: DiscoveredFile,
    sequence_number: int,
    chamber: str,
    major_heading: str | None,
    minor_heading: str | None,
    expected_source_url_marker: str,
) -> tuple[DebateEvent, int, int, bool]:
    element_type = local_name(element.tag)
    attributes = _normalised_attributes(element)
    source_url = attributes.get("url")
    url_exception = _source_url_exception(source_url, expected_source_url_marker)
    warnings: list[str] = []
    if url_exception:
        warnings.append("SOURCE_URL_DATASET_MARKER_MISSING")
    if element_type not in {"division", "bills"}:
        warnings.append("UNKNOWN_TOP_LEVEL_EVENT")

    descendants = list(element.iterdescendants())
    bills_count = (1 if element_type == "bills" else 0) + sum(
        local_name(descendant.tag) == "bills" for descendant in descendants
    )
    bill_record_count = sum(
        local_name(descendant.tag) == "bill" for descendant in descendants
    )
    event = DebateEvent(
        event_key=stable_key(
            kind="debate_event",
            pipeline_version=PIPELINE_VERSION,
            source_file_sha256=source.source_file_sha256,
            sequence_number=sequence_number,
            discriminator=element_type,
        ),
        source_file=source.relative_path,
        source_file_sha256=source.source_file_sha256,
        sequence_number=sequence_number,
        date=source.sitting_date,
        chamber=chamber,
        element_type=element_type,
        source_fragment_id=attributes.get("id"),
        major_heading_original=major_heading,
        minor_heading_original=minor_heading,
        source_url=source_url,
        attributes_json=canonical_json(attributes),
        payload_json=canonical_json(_element_payload(element)),
        bill_record_count=bill_record_count,
        warning_codes=warnings,
    )
    return event, bills_count, bill_record_count, url_exception


def parse_file(
    source: DiscoveredFile,
    *,
    config: ReconstructionConfig,
    business_config: BusinessTypeConfig,
) -> FileResult:
    """Parse one source file atomically; exceptions expose no partial result."""
    limits = config.safety_limits
    if source.byte_size > limits.max_file_bytes:
        raise FileParseError(
            "FILE_SIZE_LIMIT_EXCEEDED",
            f"file size {source.byte_size} exceeds configured ceiling",
        )
    _check_doctype(source.absolute_path)

    result = FileResult(source=source)
    sequence_number = 0
    depth = 0
    root_seen = False
    current_major_key: str | None = None
    current_minor_key: str | None = None
    current_major_heading: str | None = None
    current_minor_heading: str | None = None
    boundary_since_last_speech = True
    boundary_reason: str | None = "FILE_START"
    observed_tags: Counter[str] = Counter()
    observed_talktypes: Counter[str] = Counter()

    try:
        context = etree.iterparse(
            str(source.absolute_path),
            events=("start", "end"),
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            recover=False,
            huge_tree=False,
        )
        for event, element in context:
            tag = local_name(element.tag)
            if event == "start":
                depth += 1
                result.element_count += 1
                observed_tags[tag] += 1
                if tag == "speech":
                    observed_talktypes[element.attrib.get("talktype", "<missing>")] += 1
                result.max_depth_seen = max(result.max_depth_seen, depth)
                if result.element_count > limits.max_elements:
                    raise FileParseError(
                        "ELEMENT_COUNT_LIMIT_EXCEEDED",
                        "configured element-count ceiling exceeded",
                    )
                if depth > limits.max_depth:
                    raise FileParseError(
                        "ELEMENT_DEPTH_LIMIT_EXCEEDED",
                        "configured nesting ceiling exceeded",
                    )
                _check_text_size(element, limits.max_individual_text_chars)
                if not root_seen:
                    root_seen = True
                    if tag != "debates":
                        raise FileParseError(
                            "UNEXPECTED_ROOT",
                            f"expected debates root, found {tag}",
                        )
                continue

            # lxml only guarantees complete text/tail content on the end event.
            _check_text_size(element, limits.max_individual_text_chars)
            if depth == 2:
                sequence_number += 1
                if tag == "major-heading":
                    section, url_exception = _create_section(
                        element=element,
                        source=source,
                        sequence_number=sequence_number,
                        section_type="major",
                        parent_section_key=None,
                        expected_source_url_marker=config.expected_source_url_marker,
                    )
                    result.sections.append(section)
                    result.major_heading_count += 1
                    result.source_url_exception_count += int(url_exception)
                    current_major_key = section.section_key
                    current_minor_key = None
                    current_major_heading = section.heading_original
                    current_minor_heading = None
                    boundary_since_last_speech = True
                    boundary_reason = "MAJOR_HEADING_BOUNDARY"
                elif tag == "minor-heading":
                    section, url_exception = _create_section(
                        element=element,
                        source=source,
                        sequence_number=sequence_number,
                        section_type="minor",
                        parent_section_key=current_major_key,
                        expected_source_url_marker=config.expected_source_url_marker,
                    )
                    result.sections.append(section)
                    result.minor_heading_count += 1
                    result.source_url_exception_count += int(url_exception)
                    current_minor_key = section.section_key
                    current_minor_heading = section.heading_original
                    boundary_since_last_speech = True
                    boundary_reason = "MINOR_HEADING_BOUNDARY"
                elif tag == "speech":
                    fragment, images, url_exception = _create_fragment(
                        element=element,
                        source=source,
                        sequence_number=sequence_number,
                        chamber=config.chamber,
                        major_section_key=current_major_key,
                        minor_section_key=current_minor_key,
                        major_heading=current_major_heading,
                        minor_heading=current_minor_heading,
                        boundary_before=boundary_since_last_speech,
                        boundary_reason_before=boundary_reason,
                        expected_source_url_marker=config.expected_source_url_marker,
                        business_config=business_config,
                    )
                    result.fragments.append(fragment)
                    result.images.extend(images)
                    result.source_url_exception_count += int(url_exception)
                    boundary_since_last_speech = False
                    boundary_reason = None
                else:
                    event_record, bills_count, bill_count, url_exception = _create_event(
                        element=element,
                        source=source,
                        sequence_number=sequence_number,
                        chamber=config.chamber,
                        major_heading=current_major_heading,
                        minor_heading=current_minor_heading,
                        expected_source_url_marker=config.expected_source_url_marker,
                    )
                    result.events.append(event_record)
                    result.source_url_exception_count += int(url_exception)
                    result.bills_element_count += bills_count
                    result.bill_record_count += bill_count
                    if tag == "division":
                        result.division_count += 1
                    if tag not in KNOWN_TOP_LEVEL:
                        result.unknown_top_level_count += 1
                    boundary_since_last_speech = True
                    boundary_reason = (
                        "DIVISION_BOUNDARY"
                        if tag == "division"
                        else "BILLS_BOUNDARY"
                        if tag == "bills"
                        else "UNKNOWN_EVENT_BOUNDARY"
                    )
                _clear_element(element)
            depth -= 1
    except FileParseError:
        raise
    except (etree.XMLSyntaxError, OSError, UnicodeError, ValueError) as error:
        raise FileParseError(type(error).__name__.upper(), str(error)) from error

    if not root_seen:
        raise FileParseError("EMPTY_XML_DOCUMENT", "no XML root element found")

    duplicate_ids = {
        source_id
        for source_id, count in Counter(
            fragment.source_fragment_id
            for fragment in result.fragments
            if fragment.source_fragment_id
        ).items()
        if count > 1
    }
    for fragment in result.fragments:
        if fragment.source_fragment_id in duplicate_ids:
            fragment.warning_codes = sorted(
                set([*fragment.warning_codes, "DUPLICATE_SOURCE_FRAGMENT_ID"])
            )
    result.observed_tag_counts = dict(sorted(observed_tags.items()))
    result.observed_talktype_counts = dict(sorted(observed_talktypes.items()))
    return result
