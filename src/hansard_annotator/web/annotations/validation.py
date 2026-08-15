"""Authoritative validation for the initial declarative annotation schema."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SUPPORTED_TYPES = {
    "single_taxonomy",
    "multiple_taxonomy",
    "boolean",
    "uncertainty",
    "short_text",
    "long_text",
}


@dataclass(frozen=True)
class ValidationOutcome:
    values: dict[str, object]
    errors: dict[str, list[str]]
    canonical_json: str
    sha256: str

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_annotation(
    payload: dict[str, object],
    fields: list[dict[str, Any]],
    taxonomy_codes: dict[int, set[str]],
    *,
    submitting: bool,
) -> ValidationOutcome:
    definitions = {str(field["field_key"]): field for field in fields}
    unknown = sorted(set(payload) - set(definitions))
    errors: dict[str, list[str]] = {}
    if unknown:
        errors["_form"] = [f"Unknown annotation fields: {', '.join(unknown)}"]
    values: dict[str, object] = {}
    for key, field in definitions.items():
        field_type = str(field["field_type"])
        if field_type not in SUPPORTED_TYPES:
            errors.setdefault("_form", []).append(
                f"Unsupported stored field type: {field_type}"
            )
            continue
        raw = payload.get(key)
        if field_type in {"single_taxonomy", "short_text", "long_text"}:
            if raw is not None and not isinstance(raw, str):
                errors.setdefault(key, []).append("Must be text")
                continue
            value = raw.strip() if isinstance(raw, str) else None
            values[key] = value or None
        elif field_type == "multiple_taxonomy":
            if raw is None:
                values[key] = []
            elif not isinstance(raw, list) or not all(
                isinstance(item, str) for item in raw
            ):
                errors.setdefault(key, []).append("Must be a list of choices")
            else:
                values[key] = [item for item in raw if item]
        else:
            if raw is not None and not isinstance(raw, bool):
                errors.setdefault(key, []).append("Must be true or false")
            else:
                values[key] = raw

        if (
            values.get(key) is None
            and field_type in {"boolean", "uncertainty"}
            and field.get("ui_hints", {}).get("control") == "checkbox"
            and "default" in field.get("validation_rules", {})
        ):
            values[key] = field["validation_rules"]["default"]

        taxonomy_id = field.get("taxonomy_version_id")
        if taxonomy_id and key in values:
            allowed = taxonomy_codes[int(taxonomy_id)]
            candidate = values[key]
            candidates = candidate if isinstance(candidate, list) else [candidate]
            if any(item is not None and item not in allowed for item in candidates):
                errors.setdefault(key, []).append("Contains an invalid taxonomy code")

    for key, field in definitions.items():
        rule_value = values.get(key)
        validation_rules = field.get("validation_rules") or {}
        requirement_rules = field.get("requirement_rules") or {}
        if isinstance(rule_value, list):
            maximum = field.get("maximum_items")
            minimum = field.get("minimum_items")
            if maximum is not None and len(rule_value) > int(maximum):
                errors.setdefault(key, []).append(
                    f"Select no more than {maximum} choices"
                )
            if submitting and minimum is not None and len(rule_value) < int(minimum):
                errors.setdefault(key, []).append(
                    f"Select at least {minimum} choices"
                )
            if validation_rules.get("unique_items") and len(set(rule_value)) != len(rule_value):
                errors.setdefault(key, []).append("Choices must be unique")
            excluded = set(validation_rules.get("excluded_codes", []))
            if excluded.intersection(rule_value):
                errors.setdefault(key, []).append("Contains an excluded choice")
            other_key = validation_rules.get("different_from_field")
            if other_key and values.get(str(other_key)) in rule_value:
                errors.setdefault(key, []).append(
                    "Must not repeat the primary choice"
                )
        null_unless = requirement_rules.get("null_unless")
        if null_unless and not _condition_matches(null_unless, values) and _present(rule_value):
            errors.setdefault("_form", []).append(
                f"{field.get('label', key)} must be empty in this context"
            )
        required_when = requirement_rules.get("required_when")
        if (
            submitting
            and required_when
            and _condition_matches(required_when, values)
            and not _present(rule_value)
        ):
            errors.setdefault(key, []).append("This field is required")

    if "content_status" in definitions:
        status = values.get("content_status")
        substantive = status == "substantive_policy"
        policy_fields = (
            "primary_australian_domain",
            "secondary_australian_domains",
            "specific_australian_issue",
        )
        if not substantive and any(_present(values.get(key)) for key in policy_fields):
            errors.setdefault("_form", []).append(
                "Policy-domain and issue fields must be empty for non-substantive content"
            )
        primary = values.get("primary_australian_domain")
        secondary_value = values.get("secondary_australian_domains", [])
        secondary = secondary_value if isinstance(secondary_value, list) else []
        if len(secondary) > 2:
            errors.setdefault("secondary_australian_domains", []).append(
                "Select no more than two secondary domains"
            )
        if len(set(secondary)) != len(secondary):
            errors.setdefault("secondary_australian_domains", []).append(
                "Secondary domains must be unique"
            )
        if primary and primary in secondary:
            errors.setdefault("secondary_australian_domains", []).append(
                "Primary domain cannot also be secondary"
            )
        if "AU_OTHER_REVIEW" in secondary:
            errors.setdefault("secondary_australian_domains", []).append(
                "Other substantive policy cannot be secondary"
            )
        if primary == "AU_OTHER_REVIEW" and not _present(
            values.get("fallback_explanation")
        ):
            errors.setdefault("fallback_explanation", []).append(
                "Explain why no analytical domain applies"
            )
        if status == "unclassifiable" and not _present(
            values.get("unclassifiable_reason")
        ):
            errors.setdefault("unclassifiable_reason", []).append(
                "Explain why the speech is unclassifiable"
            )
        if submitting:
            if not _present(status):
                errors.setdefault("content_status", []).append(
                    "Content status is required"
                )
            if substantive and not _present(primary):
                errors.setdefault("primary_australian_domain", []).append(
                    "Primary domain is required for substantive policy"
                )
            if values.get("topic_uncertain") is None:
                errors.setdefault("topic_uncertain", []).append(
                    "Choose whether the topic is uncertain"
                )
    if submitting:
        for key, field in definitions.items():
            if field["required"] and values.get(key) is None:
                errors.setdefault(key, []).append("This field is required")

    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ValidationOutcome(
        values=values,
        errors=errors,
        canonical_json=canonical,
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _present(value: object) -> bool:
    return value not in (None, "", [])


def _condition_matches(condition: object, values: dict[str, object]) -> bool:
    if not isinstance(condition, dict) or len(condition) != 1:
        return False
    operator, operand = next(iter(condition.items()))
    if operator in {"all", "any"} and isinstance(operand, list):
        matches = [_condition_matches(item, values) for item in operand]
        return all(matches) if operator == "all" else any(matches)
    if operator not in {
        "field_equals",
        "field_not_equals",
        "taxonomy_code_equals",
    } or not isinstance(operand, dict):
        return False
    field = str(operand.get("field", ""))
    expected = operand.get("value")
    actual = values.get(field)
    if operator == "field_not_equals":
        return actual != expected
    if isinstance(actual, list):
        return expected in actual
    return actual == expected
