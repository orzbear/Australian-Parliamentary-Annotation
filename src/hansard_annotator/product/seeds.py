"""Strict typed validation and canonical hashing for product seed YAML."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hansard_annotator.corpus.hashing import canonical_json

SLUG = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
FIELD_TYPES = {
    "single_taxonomy",
    "multiple_taxonomy",
    "boolean",
    "ordinal",
    "short_text",
    "long_text",
    "evidence_span",
    "target_reference",
    "uncertainty",
}
TAXONOMY_TYPES = {
    "policy_domain",
    "content_status",
    "cap_topic",
    "hostility_scale",
    "target_type",
    "issue_vocabulary",
}
VERSION_STATUSES = {"draft", "published", "retired"}
RULE_KEYS = {
    "required_when",
    "null_unless",
    "allowed_when",
    "different_from_field",
    "excluded_codes",
    "unique_items",
    "default",
}
CONDITION_KEYS = {"field_equals", "field_not_equals", "taxonomy_code_equals", "all", "any"}


class SeedValidationError(ValueError):
    pass


class SeedConflictError(RuntimeError):
    pass


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SeedValidationError(f"{context} must be a string-keyed mapping")
    return value


def _keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
    context: str,
) -> None:
    missing = required - value.keys()
    unexpected = value.keys() - required - optional
    if missing:
        raise SeedValidationError(f"{context} missing keys: {sorted(missing)}")
    if unexpected:
        raise SeedValidationError(f"{context} unexpected keys: {sorted(unexpected)}")


def _string(value: Any, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise SeedValidationError(f"{context} must be a non-empty string")
    return value


def _optional_string(value: Any, context: str) -> str | None:
    if value is None:
        return None
    return _string(value, context)


def _string_list(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SeedValidationError(f"{context} must be an array")
    return tuple(_string(item, f"{context} item") for item in value)


def _bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise SeedValidationError(f"{context} must be boolean")
    return value


def _integer(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SeedValidationError(f"{context} must be integer")
    return value


@dataclass(frozen=True)
class TaxonomyLabelSeed:
    code: str
    label: str
    short_definition: str
    full_definition: str | None
    inclusion_rules: tuple[str, ...]
    exclusion_rules: tuple[str, ...]
    positive_examples: tuple[str, ...]
    negative_examples: tuple[str, ...]
    borderline_examples: tuple[str, ...]
    display_order: int
    parent_code: str | None
    is_active: bool
    is_analytical: bool
    is_fallback: bool
    requires_review: bool
    metadata: dict[str, Any]

    def canonical(self) -> dict[str, Any]:
        return {
            "borderline_examples": list(self.borderline_examples),
            "code": self.code,
            "display_order": self.display_order,
            "exclusion_rules": list(self.exclusion_rules),
            "full_definition": self.full_definition,
            "inclusion_rules": list(self.inclusion_rules),
            "is_active": self.is_active,
            "is_analytical": self.is_analytical,
            "is_fallback": self.is_fallback,
            "label": self.label,
            "metadata": self.metadata,
            "negative_examples": list(self.negative_examples),
            "parent_code": self.parent_code,
            "positive_examples": list(self.positive_examples),
            "requires_review": self.requires_review,
            "short_definition": self.short_definition,
        }


@dataclass(frozen=True)
class TaxonomySeed:
    slug: str
    name: str
    description: str
    taxonomy_type: str
    jurisdiction: str | None
    language_code: str
    owner_scope: str
    version: str
    status: str
    version_description: str
    source_name: str | None
    source_url: str | None
    attribution_text: str | None
    licence_note: str | None
    labels: tuple[TaxonomyLabelSeed, ...]
    content_sha256: str

    def canonical(self) -> dict[str, Any]:
        return {
            "attribution_text": self.attribution_text,
            "description": self.description,
            "jurisdiction": self.jurisdiction,
            "labels": [label.canonical() for label in self.labels],
            "language_code": self.language_code,
            "licence_note": self.licence_note,
            "name": self.name,
            "owner_scope": self.owner_scope,
            "slug": self.slug,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "status": self.status,
            "taxonomy_type": self.taxonomy_type,
            "version": self.version,
            "version_description": self.version_description,
        }


@dataclass(frozen=True)
class TaxonomyReference:
    slug: str
    version: str

    def canonical(self) -> dict[str, str]:
        return {"slug": self.slug, "version": self.version}


@dataclass(frozen=True)
class AnnotationFieldSeed:
    field_key: str
    label: str
    help_text: str
    field_type: str
    display_order: int
    required: bool
    taxonomy: TaxonomyReference | None
    minimum_items: int | None
    maximum_items: int | None
    minimum_value: float | None
    maximum_value: float | None
    validation_rules: dict[str, Any]
    visibility_rules: dict[str, Any]
    requirement_rules: dict[str, Any]
    ui_hints: dict[str, Any]
    is_active: bool

    def canonical(self) -> dict[str, Any]:
        return {
            "display_order": self.display_order,
            "field_key": self.field_key,
            "field_type": self.field_type,
            "help_text": self.help_text,
            "is_active": self.is_active,
            "label": self.label,
            "maximum_items": self.maximum_items,
            "maximum_value": self.maximum_value,
            "minimum_items": self.minimum_items,
            "minimum_value": self.minimum_value,
            "required": self.required,
            "requirement_rules": self.requirement_rules,
            "taxonomy": None if self.taxonomy is None else self.taxonomy.canonical(),
            "ui_hints": self.ui_hints,
            "validation_rules": self.validation_rules,
            "visibility_rules": self.visibility_rules,
        }


@dataclass(frozen=True)
class AnnotationSchemaSeed:
    slug: str
    name: str
    description: str
    schema_type: str
    version: str
    status: str
    version_description: str
    fields: tuple[AnnotationFieldSeed, ...]
    content_sha256: str

    def canonical(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "fields": [field.canonical() for field in self.fields],
            "name": self.name,
            "schema_type": self.schema_type,
            "slug": self.slug,
            "status": self.status,
            "version": self.version,
            "version_description": self.version_description,
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as source:
            value = yaml.safe_load(source)
    except (OSError, yaml.YAMLError) as error:
        raise SeedValidationError(f"invalid seed YAML: {error}") from error
    return _mapping(value, "seed")


def _content_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse_taxonomy_label(value: Any, index: int) -> TaxonomyLabelSeed:
    row = _mapping(value, f"labels[{index}]")
    required = {
        "code",
        "label",
        "short_definition",
        "display_order",
        "is_active",
        "is_analytical",
        "is_fallback",
        "requires_review",
    }
    optional = {
        "full_definition",
        "inclusion_rules",
        "exclusion_rules",
        "positive_examples",
        "negative_examples",
        "borderline_examples",
        "parent_code",
        "metadata",
    }
    _keys(row, required=required, optional=optional, context=f"labels[{index}]")
    metadata = _mapping(row.get("metadata", {}), f"labels[{index}].metadata")
    return TaxonomyLabelSeed(
        code=_string(row["code"], f"labels[{index}].code"),
        label=_string(row["label"], f"labels[{index}].label"),
        short_definition=_string(
            row["short_definition"], f"labels[{index}].short_definition"
        ),
        full_definition=_optional_string(
            row.get("full_definition"), f"labels[{index}].full_definition"
        ),
        inclusion_rules=_string_list(
            row.get("inclusion_rules", []), f"labels[{index}].inclusion_rules"
        ),
        exclusion_rules=_string_list(
            row.get("exclusion_rules", []), f"labels[{index}].exclusion_rules"
        ),
        positive_examples=_string_list(
            row.get("positive_examples", []), f"labels[{index}].positive_examples"
        ),
        negative_examples=_string_list(
            row.get("negative_examples", []), f"labels[{index}].negative_examples"
        ),
        borderline_examples=_string_list(
            row.get("borderline_examples", []), f"labels[{index}].borderline_examples"
        ),
        display_order=_integer(row["display_order"], f"labels[{index}].display_order"),
        parent_code=_optional_string(
            row.get("parent_code"), f"labels[{index}].parent_code"
        ),
        is_active=_bool(row["is_active"], f"labels[{index}].is_active"),
        is_analytical=_bool(row["is_analytical"], f"labels[{index}].is_analytical"),
        is_fallback=_bool(row["is_fallback"], f"labels[{index}].is_fallback"),
        requires_review=_bool(
            row["requires_review"], f"labels[{index}].requires_review"
        ),
        metadata=metadata,
    )


def load_taxonomy_seed(path: Path) -> TaxonomySeed:
    raw = _load_yaml(path)
    required = {
        "slug",
        "name",
        "description",
        "taxonomy_type",
        "language_code",
        "owner_scope",
        "version",
        "status",
        "version_description",
        "labels",
    }
    optional = {
        "jurisdiction",
        "source_name",
        "source_url",
        "attribution_text",
        "licence_note",
    }
    _keys(raw, required=required, optional=optional, context="taxonomy")
    slug = _string(raw["slug"], "slug")
    version = _string(raw["version"], "version")
    taxonomy_type = _string(raw["taxonomy_type"], "taxonomy_type")
    status = _string(raw["status"], "status")
    if not SLUG.fullmatch(slug):
        raise SeedValidationError("taxonomy slug must be lowercase snake_case")
    if not VERSION.fullmatch(version):
        raise SeedValidationError("taxonomy version must be semantic x.y.z")
    if taxonomy_type not in TAXONOMY_TYPES:
        raise SeedValidationError(f"unsupported taxonomy type: {taxonomy_type}")
    if status not in VERSION_STATUSES:
        raise SeedValidationError(f"unsupported taxonomy version status: {status}")
    labels_raw = raw["labels"]
    if not isinstance(labels_raw, list) or not labels_raw:
        raise SeedValidationError("labels must be a non-empty array")
    labels = tuple(_parse_taxonomy_label(row, index) for index, row in enumerate(labels_raw))
    codes = [label.code for label in labels]
    orders = [label.display_order for label in labels]
    if len(codes) != len(set(codes)):
        raise SeedValidationError("duplicate taxonomy label code")
    if len(orders) != len(set(orders)):
        raise SeedValidationError("duplicate taxonomy display order")
    code_set = set(codes)
    parent_by_code = {label.code: label.parent_code for label in labels}
    for label in labels:
        if label.parent_code is not None and label.parent_code not in code_set:
            raise SeedValidationError(
                f"unknown parent code for {label.code}: {label.parent_code}"
            )
        seen = {label.code}
        parent = label.parent_code
        while parent is not None:
            if parent in seen:
                raise SeedValidationError(f"cyclic taxonomy hierarchy at {label.code}")
            seen.add(parent)
            parent = parent_by_code[parent]
    partial = TaxonomySeed(
        slug=slug,
        name=_string(raw["name"], "name"),
        description=_string(raw["description"], "description"),
        taxonomy_type=taxonomy_type,
        jurisdiction=_optional_string(raw.get("jurisdiction"), "jurisdiction"),
        language_code=_string(raw["language_code"], "language_code"),
        owner_scope=_string(raw["owner_scope"], "owner_scope"),
        version=version,
        status=status,
        version_description=_string(
            raw["version_description"], "version_description"
        ),
        source_name=_optional_string(raw.get("source_name"), "source_name"),
        source_url=_optional_string(raw.get("source_url"), "source_url"),
        attribution_text=_optional_string(
            raw.get("attribution_text"), "attribution_text"
        ),
        licence_note=_optional_string(raw.get("licence_note"), "licence_note"),
        labels=tuple(sorted(labels, key=lambda label: label.display_order)),
        content_sha256="",
    )
    return TaxonomySeed(
        **{
            **partial.__dict__,
            "content_sha256": _content_hash(partial.canonical()),
        }
    )


def _validate_condition(
    value: Any, *, field_keys: set[str], context: str
) -> dict[str, Any]:
    condition = _mapping(value, context)
    if len(condition) != 1:
        raise SeedValidationError(f"{context} must contain one condition operator")
    operator = next(iter(condition))
    if operator not in CONDITION_KEYS:
        raise SeedValidationError(f"{context} unsupported condition: {operator}")
    operand = condition[operator]
    if operator in {"all", "any"}:
        if not isinstance(operand, list) or not operand:
            raise SeedValidationError(f"{context}.{operator} must be a non-empty array")
        return {
            operator: [
                _validate_condition(
                    item, field_keys=field_keys, context=f"{context}.{operator}"
                )
                for item in operand
            ]
        }
    detail = _mapping(operand, f"{context}.{operator}")
    _keys(
        detail,
        required={"field", "value"},
        optional=set(),
        context=f"{context}.{operator}",
    )
    field = _string(detail["field"], f"{context}.{operator}.field")
    if field not in field_keys:
        raise SeedValidationError(f"{context} references unknown field: {field}")
    if not isinstance(detail["value"], (str, bool, int, float)):
        raise SeedValidationError(f"{context}.{operator}.value must be scalar")
    return {operator: {"field": field, "value": detail["value"]}}


def _validate_rules(
    value: Any, *, field_keys: set[str], context: str
) -> dict[str, Any]:
    rules = _mapping(value, context)
    unknown = rules.keys() - RULE_KEYS
    if unknown:
        raise SeedValidationError(f"{context} unsupported rules: {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key, rule_value in rules.items():
        if key in {"required_when", "null_unless", "allowed_when"}:
            result[key] = _validate_condition(
                rule_value, field_keys=field_keys, context=f"{context}.{key}"
            )
        elif key == "different_from_field":
            other = _string(rule_value, f"{context}.{key}")
            if other not in field_keys:
                raise SeedValidationError(f"{context} references unknown field: {other}")
            result[key] = other
        elif key == "excluded_codes":
            result[key] = list(_string_list(rule_value, f"{context}.{key}"))
        elif key == "unique_items":
            result[key] = _bool(rule_value, f"{context}.{key}")
        elif key == "default":
            if not isinstance(rule_value, (str, bool, int, float)) and rule_value is not None:
                raise SeedValidationError(f"{context}.default must be scalar or null")
            result[key] = rule_value
    return result


def load_annotation_schema_seed(path: Path) -> AnnotationSchemaSeed:
    raw = _load_yaml(path)
    required = {
        "slug",
        "name",
        "description",
        "schema_type",
        "version",
        "status",
        "version_description",
        "fields",
    }
    _keys(raw, required=required, optional=set(), context="annotation schema")
    slug = _string(raw["slug"], "slug")
    version = _string(raw["version"], "version")
    status = _string(raw["status"], "status")
    if not SLUG.fullmatch(slug) or not VERSION.fullmatch(version):
        raise SeedValidationError("invalid schema slug or semantic version")
    if status not in VERSION_STATUSES:
        raise SeedValidationError(f"unsupported schema status: {status}")
    fields_raw = raw["fields"]
    if not isinstance(fields_raw, list) or not fields_raw:
        raise SeedValidationError("fields must be a non-empty array")
    preliminary = [_mapping(row, f"fields[{index}]") for index, row in enumerate(fields_raw)]
    field_keys = {
        _string(row.get("field_key"), "field_key") for row in preliminary
    }
    if len(field_keys) != len(preliminary):
        raise SeedValidationError("duplicate annotation field key")
    fields: list[AnnotationFieldSeed] = []
    for index, row in enumerate(preliminary):
        required = {
            "field_key",
            "label",
            "help_text",
            "field_type",
            "display_order",
            "required",
        }
        optional = {
            "taxonomy",
            "minimum_items",
            "maximum_items",
            "minimum_value",
            "maximum_value",
            "validation_rules",
            "visibility_rules",
            "requirement_rules",
            "ui_hints",
            "is_active",
        }
        _keys(row, required=required, optional=optional, context=f"fields[{index}]")
        field_type = _string(row["field_type"], f"fields[{index}].field_type")
        if field_type not in FIELD_TYPES:
            raise SeedValidationError(f"unsupported field type: {field_type}")
        taxonomy = None
        if row.get("taxonomy") is not None:
            reference = _mapping(row["taxonomy"], f"fields[{index}].taxonomy")
            _keys(
                reference,
                required={"slug", "version"},
                optional=set(),
                context=f"fields[{index}].taxonomy",
            )
            taxonomy = TaxonomyReference(
                slug=_string(reference["slug"], "taxonomy slug"),
                version=_string(reference["version"], "taxonomy version"),
            )
        taxonomy_type = field_type in {"single_taxonomy", "multiple_taxonomy"}
        if taxonomy_type != (taxonomy is not None):
            raise SeedValidationError(
                f"fields[{index}] taxonomy reference does not match field type"
            )
        minimum_items = (
            None
            if row.get("minimum_items") is None
            else _integer(row["minimum_items"], f"fields[{index}].minimum_items")
        )
        maximum_items = (
            None
            if row.get("maximum_items") is None
            else _integer(row["maximum_items"], f"fields[{index}].maximum_items")
        )
        if minimum_items is not None and minimum_items < 0:
            raise SeedValidationError("minimum_items cannot be negative")
        if maximum_items is not None and (
            maximum_items < 0
            or (minimum_items is not None and maximum_items < minimum_items)
        ):
            raise SeedValidationError("invalid maximum_items")
        fields.append(
            AnnotationFieldSeed(
                field_key=_string(row["field_key"], f"fields[{index}].field_key"),
                label=_string(row["label"], f"fields[{index}].label"),
                help_text=_string(row["help_text"], f"fields[{index}].help_text"),
                field_type=field_type,
                display_order=_integer(
                    row["display_order"], f"fields[{index}].display_order"
                ),
                required=_bool(row["required"], f"fields[{index}].required"),
                taxonomy=taxonomy,
                minimum_items=minimum_items,
                maximum_items=maximum_items,
                minimum_value=row.get("minimum_value"),
                maximum_value=row.get("maximum_value"),
                validation_rules=_validate_rules(
                    row.get("validation_rules", {}),
                    field_keys=field_keys,
                    context=f"fields[{index}].validation_rules",
                ),
                visibility_rules=_validate_rules(
                    row.get("visibility_rules", {}),
                    field_keys=field_keys,
                    context=f"fields[{index}].visibility_rules",
                ),
                requirement_rules=_validate_rules(
                    row.get("requirement_rules", {}),
                    field_keys=field_keys,
                    context=f"fields[{index}].requirement_rules",
                ),
                ui_hints=_mapping(
                    row.get("ui_hints", {}), f"fields[{index}].ui_hints"
                ),
                is_active=_bool(
                    row.get("is_active", True), f"fields[{index}].is_active"
                ),
            )
        )
    orders = [field.display_order for field in fields]
    if len(orders) != len(set(orders)):
        raise SeedValidationError("duplicate annotation field display order")
    partial = AnnotationSchemaSeed(
        slug=slug,
        name=_string(raw["name"], "name"),
        description=_string(raw["description"], "description"),
        schema_type=_string(raw["schema_type"], "schema_type"),
        version=version,
        status=status,
        version_description=_string(
            raw["version_description"], "version_description"
        ),
        fields=tuple(sorted(fields, key=lambda field: field.display_order)),
        content_sha256="",
    )
    return AnnotationSchemaSeed(
        **{**partial.__dict__, "content_sha256": _content_hash(partial.canonical())}
    )
