"""Stable primary failure categories for annotation provider attempts."""

from __future__ import annotations

import json
from enum import StrEnum


class FailureCategory(StrEnum):
    EVIDENCE_RESOLUTION_FAILURE = "EVIDENCE_RESOLUTION_FAILURE"
    EVIDENCE_REPAIR_VALIDATION_FAILURE = "EVIDENCE_REPAIR_VALIDATION_FAILURE"
    EVIDENCE_REPAIR_PROVIDER_FAILURE = "EVIDENCE_REPAIR_PROVIDER_FAILURE"
    EVIDENCE_REPAIR_MISSING_RESULT = "EVIDENCE_REPAIR_MISSING_RESULT"
    EVIDENCE_REPAIR_MAPPING_FAILURE = "EVIDENCE_REPAIR_MAPPING_FAILURE"
    JSON_TRUNCATION = "JSON_TRUNCATION"
    STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"
    SEMANTIC_CONTRACT_INVALID = "SEMANTIC_CONTRACT_INVALID"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    MISSING_RESULT = "MISSING_RESULT"


def json_failure_category(error: json.JSONDecodeError, raw_text: str) -> FailureCategory:
    """Distinguish a response cut off at its end from other malformed JSON."""
    end_truncated = error.pos >= max(0, len(raw_text) - 1)
    if end_truncated and error.msg in {
        "Unterminated string starting at",
        "Expecting value",
        "Expecting ',' delimiter",
        "Expecting ':' delimiter",
    }:
        return FailureCategory.JSON_TRUNCATION
    return FailureCategory.STRUCTURED_OUTPUT_INVALID
