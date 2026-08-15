from __future__ import annotations

from typing import Any

import pytest
from argon2 import PasswordHasher

from hansard_annotator.web.annotations.validation import validate_annotation
from hansard_annotator.web.security import (
    hash_password,
    make_login_csrf,
    random_token,
    token_digest,
    verify_login_csrf,
    verify_password,
)
from hansard_annotator.web.tasks.schemas import SelectionCriteria


def _fields() -> list[dict[str, Any]]:
    types = {
        "content_status": ("single_taxonomy", 1, True),
        "primary_australian_domain": ("single_taxonomy", 2, False),
        "secondary_australian_domains": ("multiple_taxonomy", 2, False),
        "specific_australian_issue": ("short_text", None, False),
        "topic_uncertain": ("boolean", None, True),
        "fallback_explanation": ("long_text", None, False),
        "unclassifiable_reason": ("long_text", None, False),
        "annotation_notes": ("long_text", None, False),
    }
    return [
        {
            "field_key": key,
            "field_type": value[0],
            "taxonomy_version_id": value[1],
            "required": value[2],
        }
        for key, value in types.items()
    ]


def _valid() -> dict[str, object]:
    return {
        "content_status": "substantive_policy",
        "primary_australian_domain": "AU03",
        "secondary_australian_domains": ["AU05"],
        "specific_australian_issue": "Medicare",
        "topic_uncertain": False,
        "fallback_explanation": None,
        "unclassifiable_reason": None,
        "annotation_notes": None,
    }


def test_argon2id_and_opaque_security_tokens() -> None:
    password = "long enough research password"
    hashed = hash_password(password, 12)
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, password)
    assert not verify_password(hashed, "not the password")
    assert PasswordHasher().verify(hashed, password)
    raw = random_token()
    digest = token_digest(raw, "test-secret")
    assert raw not in digest
    token = make_login_csrf("test-secret")
    assert verify_login_csrf(token, "test-secret")
    assert not verify_login_csrf(token, "different-secret")


def test_annotation_rules_and_canonical_hashing() -> None:
    fields = _fields()
    taxonomies = {
        1: {
            "substantive_policy",
            "procedural",
            "ceremonial_nonpolicy",
            "unclassifiable",
        },
        2: {"AU03", "AU05", "AU07", "AU_OTHER_REVIEW"},
    }
    first = validate_annotation(_valid(), fields, taxonomies, submitting=True)
    second = validate_annotation(
        dict(reversed(list(_valid().items()))), fields, taxonomies, submitting=True
    )
    assert first.valid
    assert first.sha256 == second.sha256

    invalid = _valid()
    invalid["secondary_australian_domains"] = [
        "AU03",
        "AU03",
        "AU_OTHER_REVIEW",
    ]
    outcome = validate_annotation(invalid, fields, taxonomies, submitting=True)
    assert not outcome.valid
    assert "secondary_australian_domains" in outcome.errors

    fallback = _valid()
    fallback["primary_australian_domain"] = "AU_OTHER_REVIEW"
    fallback["fallback_explanation"] = None
    assert "fallback_explanation" in validate_annotation(
        fallback, fields, taxonomies, submitting=True
    ).errors

    unclassifiable = _valid()
    unclassifiable.update(
        {
            "content_status": "unclassifiable",
            "primary_australian_domain": None,
            "secondary_australian_domains": [],
            "specific_australian_issue": None,
        }
    )
    assert "unclassifiable_reason" in validate_annotation(
        unclassifiable, fields, taxonomies, submitting=True
    ).errors

    stale = _valid()
    stale["content_status"] = "procedural"
    assert "_form" in validate_annotation(
        stale, fields, taxonomies, submitting=True
    ).errors

    unknown = _valid()
    unknown["executable_rule"] = "do something"
    assert "_form" in validate_annotation(
        unknown, fields, taxonomies, submitting=False
    ).errors


def test_selection_criteria_rejects_unrestricted_or_invalid_filters() -> None:
    assert SelectionCriteria(years=[2020, 2020, 2019]).years == [2019, 2020]
    with pytest.raises(ValueError):
        SelectionCriteria.model_validate({"minimum_words": 100, "maximum_words": 20})
    with pytest.raises(ValueError):
        SelectionCriteria.model_validate({"arbitrary_sql": "DROP TABLE users"})
    assert SelectionCriteria(source_domain_code="AU12").source_domain_code == "AU12"
    with pytest.raises(ValueError):
        SelectionCriteria(source_domain_code="security OR true")


def test_streamlined_general_and_aukus_schema_rules() -> None:
    general_fields = [
        {
            "field_key": "is_non_policy",
            "field_type": "boolean",
            "required": True,
            "taxonomy_version_id": None,
            "validation_rules": {"default": False},
            "requirement_rules": {},
            "ui_hints": {"control": "checkbox"},
        },
        {
            "field_key": "primary_australian_domain",
            "field_type": "single_taxonomy",
            "required": False,
            "taxonomy_version_id": 2,
            "validation_rules": {},
            "requirement_rules": {
                "required_when": {
                    "field_equals": {"field": "is_non_policy", "value": False}
                },
                "null_unless": {
                    "field_equals": {"field": "is_non_policy", "value": False}
                },
            },
            "ui_hints": {},
        },
        {
            "field_key": "secondary_australian_domains",
            "field_type": "multiple_taxonomy",
            "required": False,
            "taxonomy_version_id": 2,
            "minimum_items": 0,
            "maximum_items": 2,
            "validation_rules": {
                "unique_items": True,
                "different_from_field": "primary_australian_domain",
                "excluded_codes": ["AU_OTHER_REVIEW"],
            },
            "requirement_rules": {
                "null_unless": {
                    "field_equals": {"field": "is_non_policy", "value": False}
                }
            },
            "ui_hints": {"reveal_control_label": "Add secondary topics"},
        },
    ]
    taxonomy = {2: {"AU03", "AU05", "AU12", "AU_OTHER_REVIEW"}}
    missing_primary = validate_annotation(
        {"is_non_policy": False, "secondary_australian_domains": []},
        general_fields,
        taxonomy,
        submitting=True,
    )
    assert "primary_australian_domain" in missing_primary.errors
    policy = validate_annotation(
        {
            "is_non_policy": False,
            "primary_australian_domain": "AU12",
            "secondary_australian_domains": [],
        },
        general_fields,
        taxonomy,
        submitting=True,
    )
    assert policy.valid
    non_policy = validate_annotation(
        {
            "is_non_policy": True,
            "primary_australian_domain": None,
            "secondary_australian_domains": [],
        },
        general_fields,
        taxonomy,
        submitting=True,
    )
    assert non_policy.valid
    stale = validate_annotation(
        {
            "is_non_policy": True,
            "primary_australian_domain": "AU12",
            "secondary_australian_domains": [],
        },
        general_fields,
        taxonomy,
        submitting=True,
    )
    assert "_form" in stale.errors

    aukus_fields = [
        {
            "field_key": "discusses_aukus",
            "field_type": "boolean",
            "required": True,
            "taxonomy_version_id": None,
            "validation_rules": {"default": False},
            "requirement_rules": {},
            "ui_hints": {"control": "checkbox"},
        }
    ]
    screened_no = validate_annotation({}, aukus_fields, {}, submitting=True)
    assert screened_no.valid
    assert screened_no.values["discusses_aukus"] is False
