from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from hansard_annotator.adapters.registry import (
    AdapterRegistry,
    AdapterRegistryError,
    get_adapter,
    list_adapters,
)
from hansard_annotator.product.seeds import (
    SeedValidationError,
    load_annotation_schema_seed,
    load_taxonomy_seed,
)

POLICY = Path("config/taxonomies/australian_policy_domains/0.1.0.yaml")
STATUS = Path("config/taxonomies/content_status/1.0.0.yaml")
SCHEMA = Path("config/annotation_schemas/australian_policy_annotation/0.1.0.yaml")


def _yaml(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


def test_adapter_registry_is_closed_and_metadata_only() -> None:
    adapters = list_adapters()
    assert [adapter.adapter_key for adapter in adapters] == ["openaustralia_publicwhip_xml"]
    adapter = get_adapter("openaustralia_publicwhip_xml")
    assert adapter.source_format_key == "openaustralia_publicwhip_xml"
    assert adapter.describe_capabilities().preprocessing
    assert adapter.describe_provenance()["licence_status"] == "pending_review"
    with pytest.raises(AdapterRegistryError, match="unsupported"):
        get_adapter("parlamint")
    with pytest.raises(AdapterRegistryError, match="duplicate"):
        AdapterRegistry((adapter, adapter))


def test_migrations_and_application_do_not_hardcode_database_row_three() -> None:
    forbidden = re.compile(
        r"\b(?:preprocessing_run_id|current_preprocessing_run_id|"
        r"reconstruction_run_id|preprocessing_runs\.id|reconstruction_runs\.id)"
        r"\s*(?:=|==|:)\s*3\b",
        re.IGNORECASE,
    )
    matches: list[str] = []
    for root in (Path("migrations"), Path("src")):
        for path in sorted(root.rglob("*.py")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if forbidden.search(line):
                    matches.append(f"{path.as_posix()}:{line_number}:{line.strip()}")
    assert matches == []


def test_australian_adapter_delegates_to_existing_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_adapter("openaustralia_publicwhip_xml")
    marker = object()
    monkeypatch.setattr(
        "hansard_annotator.adapters.australia.openaustralia_hansard.run_pipeline",
        lambda options: marker,
    )
    assert adapter.preprocess(object()) is marker  # type: ignore[attr-defined,arg-type]


def test_taxonomy_seeds_are_deterministic_and_have_approved_counts() -> None:
    first = load_taxonomy_seed(POLICY)
    second = load_taxonomy_seed(POLICY)
    statuses = load_taxonomy_seed(STATUS)
    assert first.content_sha256 == second.content_sha256
    assert len(first.labels) == 15
    assert sum(label.is_analytical for label in first.labels) == 14
    fallback = next(label for label in first.labels if label.code == "AU_OTHER_REVIEW")
    assert not fallback.is_analytical
    assert fallback.is_fallback and fallback.requires_review
    assert [label.code for label in statuses.labels] == [
        "substantive_policy",
        "procedural",
        "ceremonial_nonpolicy",
        "unclassifiable",
    ]


def test_taxonomy_validation_rejects_duplicate_and_cyclic_labels(
    tmp_path: Path,
) -> None:
    duplicate = _yaml(POLICY)
    labels = duplicate["labels"]
    assert isinstance(labels, list)
    assert isinstance(labels[1], dict) and isinstance(labels[0], dict)
    labels[1]["code"] = labels[0]["code"]
    duplicate_path = tmp_path / "duplicate.yaml"
    _write_yaml(duplicate_path, duplicate)
    with pytest.raises(SeedValidationError, match="duplicate"):
        load_taxonomy_seed(duplicate_path)

    cycle = _yaml(POLICY)
    cycle_labels = cycle["labels"]
    assert isinstance(cycle_labels, list)
    assert isinstance(cycle_labels[0], dict) and isinstance(cycle_labels[1], dict)
    cycle_labels[0]["parent_code"] = "AU02"
    cycle_labels[1]["parent_code"] = "AU01"
    cycle_path = tmp_path / "cycle.yaml"
    _write_yaml(cycle_path, cycle)
    with pytest.raises(SeedValidationError, match="cyclic"):
        load_taxonomy_seed(cycle_path)


def test_initial_annotation_schema_encodes_rules_and_excludes_cap() -> None:
    seed = load_annotation_schema_seed(SCHEMA)
    assert len(seed.fields) == 8
    assert not any("cap" in field.field_key.lower() for field in seed.fields)
    fields = {field.field_key: field for field in seed.fields}
    secondary = fields["secondary_australian_domains"]
    assert secondary.minimum_items == 0
    assert secondary.maximum_items == 2
    assert secondary.validation_rules == {
        "unique_items": True,
        "different_from_field": "primary_australian_domain",
        "excluded_codes": ["AU_OTHER_REVIEW"],
    }
    assert "required_when" in fields["primary_australian_domain"].requirement_rules
    assert "null_unless" in fields["primary_australian_domain"].requirement_rules
    assert "null_unless" in secondary.requirement_rules
    assert "required_when" in fields["fallback_explanation"].requirement_rules
    assert "required_when" in fields["unclassifiable_reason"].requirement_rules


def test_annotation_schema_rejects_unknown_types_rules_and_taxonomy_shape(
    tmp_path: Path,
) -> None:
    for name, mutate, match in (
        (
            "type",
            lambda field: field.__setitem__("field_type", "javascript"),
            "unsupported field type",
        ),
        (
            "rule",
            lambda field: field.__setitem__("validation_rules", {"execute_sql": "SELECT 1"}),
            "unsupported rules",
        ),
        (
            "maximum",
            lambda field: field.__setitem__("maximum_items", -1),
            "invalid maximum_items",
        ),
    ):
        value = _yaml(SCHEMA)
        fields = value["fields"]
        assert isinstance(fields, list) and isinstance(fields[2], dict)
        mutate(fields[2])
        path = tmp_path / f"{name}.yaml"
        _write_yaml(path, value)
        with pytest.raises(SeedValidationError, match=match):
            load_annotation_schema_seed(path)

    seed = load_annotation_schema_seed(SCHEMA)
    changed = replace(seed.fields[0], field_key="duplicate")
    assert changed.field_key == "duplicate"

    duplicate = _yaml(SCHEMA)
    duplicate_fields = duplicate["fields"]
    assert isinstance(duplicate_fields, list)
    duplicate_fields.append(dict(duplicate_fields[0]))
    duplicate_path = tmp_path / "duplicate-fields.yaml"
    _write_yaml(duplicate_path, duplicate)
    with pytest.raises(SeedValidationError, match="duplicate annotation field key"):
        load_annotation_schema_seed(duplicate_path)

    malformed_path = tmp_path / "malformed.yaml"
    malformed_path.write_text("fields: [\n", encoding="utf-8")
    with pytest.raises(SeedValidationError):
        load_annotation_schema_seed(malformed_path)
