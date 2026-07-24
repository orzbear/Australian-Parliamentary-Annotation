"""Deterministic identifiers and checksums."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


def normalise_unicode(value: str) -> str:
    """Normalise parser-level strings to NFC for stable derived records."""
    return unicodedata.normalize("NFC", value)


def normalise_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalise_unicode(value)
    if isinstance(value, Mapping):
        return {
            normalise_unicode(str(key)): normalise_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [normalise_json_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Return canonical UTF-8-compatible JSON with no insignificant whitespace."""
    return json.dumps(
        normalise_json_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_key(
    *,
    kind: str,
    pipeline_version: str,
    source_file_sha256: str,
    sequence_number: int,
    discriminator: str = "",
) -> str:
    return sha256_text(
        canonical_json(
            {
                "discriminator": discriminator,
                "kind": kind,
                "pipeline_version": pipeline_version,
                "sequence_number": sequence_number,
                "source_file_sha256": source_file_sha256,
            }
        )
    )


def fragment_projection_sha256(
    *,
    source_file_sha256: str,
    sequence_number: int,
    element_type: str,
    raw_attributes: Mapping[str, str],
    text_raw: str,
    major_heading_original: str | None,
    minor_heading_original: str | None,
) -> str:
    """Hash the owner-approved, NFC-normalised canonical fragment projection."""
    projection = {
        "source_file_sha256": source_file_sha256,
        "sequence_number": sequence_number,
        "element_type": element_type,
        "raw_attributes": dict(sorted(raw_attributes.items())),
        "text_raw": text_raw,
        "major_heading_original": major_heading_original,
        "minor_heading_original": minor_heading_original,
    }
    return sha256_text(canonical_json(projection))

