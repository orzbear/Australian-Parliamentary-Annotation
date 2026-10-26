from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from hansard_annotator.product.seeds import load_annotation_schema_seed, load_taxonomy_seed
from hansard_annotator.web.annotations.validation import validate_annotation
from hansard_annotator.web.exports.service import _derived_annotation_values, _simple_csv
from hansard_annotator.web.tasks.service import (
    _applicable_human_work_filters,
    _version_summary,
)

ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "config/annotation_schemas/australian_partisan_hostility/0.2.0.yaml"
TEXT = "The government deliberately misled Australians. Its policy costs too much."


def _fields() -> tuple[list[dict[str, Any]], dict[int, set[str]]]:
    seed = load_annotation_schema_seed(SCHEMA)
    ids: dict[tuple[str, str], int] = {}
    codes: dict[int, set[str]] = {}
    fields: list[dict[str, Any]] = []
    for definition in seed.fields:
        taxonomy_id = None
        if definition.taxonomy:
            reference = (definition.taxonomy.slug, definition.taxonomy.version)
            taxonomy_id = ids.setdefault(reference, len(ids) + 1)
            taxonomy = load_taxonomy_seed(
                ROOT
                / "config/taxonomies"
                / definition.taxonomy.slug
                / f"{definition.taxonomy.version}.yaml"
            )
            codes[taxonomy_id] = {label.code for label in taxonomy.labels}
        fields.append({**definition.canonical(), "taxonomy_version_id": taxonomy_id})
    return fields, codes


def _span(start: int, end: int) -> dict[str, object]:
    return {"start_offset": start, "end_offset": end, "text": TEXT[start:end]}


def _validate(payload: dict[str, object], *, submitting: bool = True):
    fields, codes = _fields()
    return validate_annotation(
        payload,
        fields,
        codes,
        submitting=submitting,
        authoritative_text=TEXT,
    )


def test_simplified_v02_schema_loads_and_removes_old_fields() -> None:
    schema = load_annotation_schema_seed(SCHEMA)
    assert schema.version == "0.2.0"
    assert [field.field_key for field in schema.fields] == [
        "hostility_present",
        "target",
        "evidence_span",
    ]
    removed = {
        "target_type",
        "target_text",
        "partisan_target",
        "hostility_type",
        "confidence",
        "boundary_case",
        "boundary_case_note",
    }
    assert removed.isdisjoint(field.field_key for field in schema.fields)


def test_no_clears_downstream_while_yes_and_unsure_require_evidence() -> None:
    assert _validate(
        {"hostility_present": "NO", "target": None, "evidence_span": []}
    ).valid
    stale = _validate(
        {
            "hostility_present": "NO",
            "target": "NON_POLITICAL_TARGET",
            "evidence_span": [_span(0, 14)],
        }
    )
    assert "_form" in stale.errors

    for presence in ("YES", "UNSURE"):
        missing = _validate(
            {"hostility_present": presence, "target": None, "evidence_span": []}
        )
        assert "target" in missing.errors
        assert "evidence_span" in missing.errors
        target = "OPPOSING_PARTISAN_TARGET" if presence == "YES" else "UNSURE"
        assert _validate(
            {
                "hostility_present": presence,
                "target": target,
                "evidence_span": [_span(0, 47)],
            }
        ).valid


def test_evidence_is_authoritative_ordered_and_supports_multiple_spans() -> None:
    payload = {
        "hostility_present": "YES",
        "target": "OPPOSING_PARTISAN_TARGET",
        "evidence_span": [_span(0, 14), _span(28, 47)],
    }
    assert _validate(payload).valid
    mismatch = {**payload, "evidence_span": [{**_span(0, 14), "text": "forged"}]}
    assert "evidence_span" in _validate(mismatch).errors
    overlap = {**payload, "evidence_span": [_span(0, 14), _span(10, 20)]}
    assert "evidence_span" in _validate(overlap).errors
    out_of_range = {
        **payload,
        "evidence_span": [
            {"start_offset": 0, "end_offset": 999, "text": TEXT}
        ],
    }
    assert "evidence_span" in _validate(out_of_range).errors


def test_workspace_supports_highlight_restore_removal_and_exact_copy() -> None:
    template = (ROOT / "src/hansard_annotator/web/templates/annotations/workspace.html").read_text(encoding="utf-8")
    script = (ROOT / "src/hansard_annotator/web/static/src/app.js").read_text(encoding="utf-8")
    base = (ROOT / "src/hansard_annotator/web/templates/base.html").read_text(encoding="utf-8")
    assert "data-speech-text" in template
    assert "data-add-evidence" in template
    assert "data-evidence-input" in template
    assert "data-copy-speech" in template
    assert "JSON.parse(evidenceInput.value" in script
    assert 'document.createElement("mark")' in script
    assert 'remove.textContent = "Remove"' in script
    assert "navigator.clipboard?.writeText" in script
    assert "legacyCopy(canonicalSpeech)" in script
    assert 'document.execCommand("copy")' in script
    assert "Copy failed" in script
    assert "v=20261024-hostility-evidence-2" in base
    assert script.index('copyButton?.addEventListener("click", copySpeech)') < script.index("if (!form) return")
    assert "innerHTML" not in script


def test_hostility_project_hides_and_ignores_policy_queue_filters() -> None:
    detail = (ROOT / "src/hansard_annotator/web/templates/projects/detail.html").read_text(
        encoding="utf-8"
    )
    assert "{% if human_work.supports_policy_status %}" in detail
    assert "{% if human_work.supports_primary_domain %}" in detail
    assert "Hostility {{ row.hostility_present }}" in detail
    assert _applicable_human_work_filters(
        {"hostility_present", "target", "evidence_span"},
        "policy",
        "AU12",
    ) == (None, None, False, False)


def test_policy_queue_filters_and_revision_summaries_remain_available() -> None:
    assert _applicable_human_work_filters(
        {"is_non_policy", "primary_australian_domain"},
        "non_policy",
        "AU12",
    ) == ("non_policy", "AU12", True, True)
    assert _version_summary({"is_non_policy": False})["summary_label"] == "Policy"
    hostility = _version_summary({"hostility_present": "UNSURE"})
    assert hostility["summary_label"] == "Hostility: UNSURE"
    assert hostility["has_policy_summary"] is False
    for template_name in ("detail.html", "history.html"):
        template = (
            ROOT / "src/hansard_annotator/web/templates/annotations" / template_name
        ).read_text(encoding="utf-8")
        assert "has_policy_summary" in template


def test_partisan_hostility_derivation_and_simplified_csv() -> None:
    combinations = [
        ("YES", "OPPOSING_PARTISAN_TARGET", True),
        ("YES", "OTHER_POLITICAL_TARGET", False),
        ("YES", "NON_POLITICAL_TARGET", False),
        ("UNSURE", "OPPOSING_PARTISAN_TARGET", False),
        ("NO", None, False),
    ]
    for presence, target, expected in combinations:
        values = {"hostility_present": presence, "target": target}
        assert _derived_annotation_values(values) == {"partisan_hostility": expected}

    values = {
        "hostility_present": "YES",
        "target": "OPPOSING_PARTISAN_TARGET",
        "evidence_span": [_span(0, 14), _span(28, 47)],
    }
    record: dict[str, object] = {
        "record_id": "annotation:1:revision:1",
        "speech": {
            "turn_key": "turn-1",
            "date": "2026-01-01",
            "chamber": "house",
            "speaker_name": "Member",
            "major_heading": "Debate",
            "minor_heading": None,
            "business_type": "debate",
            "word_count": 8,
            "interruption_count": 0,
            "text": TEXT,
        },
        "human_annotation": {
            "status": "submitted",
            "revision": 1,
            "values_sha256": "abc",
            "values": values,
            "readable_fields": [],
        },
        "derived_values": _derived_annotation_values(values),
        "sample": {"batch_name": "preview"},
    }
    fields = [
        {"field_key": key, "taxonomy_version_id": None}
        for key in ("hostility_present", "target", "evidence_span")
    ]
    row = next(
        csv.DictReader(io.StringIO(_simple_csv([record], fields).decode("utf-8-sig")))
    )
    assert row["partisan_hostility"] == "true"
    assert '"start_offset":0' in row["evidence_span"]
    assert "target_type" not in row
