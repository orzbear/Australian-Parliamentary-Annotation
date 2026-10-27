from __future__ import annotations

from pathlib import Path

import pytest

from hansard_annotator.llm_eval.hostility_validation import (
    HELDOUT_SAMPLE_SHA256,
    validate_heldout_definition,
)
from hansard_annotator.llm_eval.prediction_tasks import (
    adapt_hostility_prediction,
    changed_fields,
    human_suggestion,
)

ROOT = Path(__file__).resolve().parents[1]


def test_hostility_v2_adapter_resolves_and_maps_evidence() -> None:
    text = "The minister is hopeless. The bill is ordinary."
    adapted = adapt_hostility_prediction(
        {
            "hostility_present": "YES",
            "target": "OPPOSING_PARTISAN_TARGET",
            "evidence_spans": [{"text": "minister is hopeless"}],
            "reasoning": "Direct competence attack.",
        },
        text,
    )
    assert adapted.task == "hostility"
    assert adapted.values["evidence_spans"] == [
        {"start_offset": 4, "end_offset": 24, "text": "minister is hopeless"}
    ]
    assert human_suggestion("hostility", adapted.values) == {
        "hostility_present": "YES",
        "target": "OPPOSING_PARTISAN_TARGET",
        "evidence_span": adapted.values["evidence_spans"],
    }


def test_hostility_v2_adapter_rejects_ambiguous_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        adapt_hostility_prediction(
            {
                "hostility_present": "YES",
                "target": "OPPOSING_PARTISAN_TARGET",
                "evidence_spans": [{"text": "hopeless"}],
                "reasoning": "Repeated text.",
            },
            "hopeless and hopeless",
        )


def test_hostility_changes_are_derived() -> None:
    suggested = {"hostility_present": "NO", "target": None, "evidence_span": []}
    human = {"hostility_present": "UNSURE", "target": "UNCLEAR", "evidence_span": []}
    assert changed_fields("hostility", suggested, human) == ["hostility_present", "target"]


def test_heldout_validation_identity_is_allowlisted_without_execution() -> None:
    identity = validate_heldout_definition(
        ROOT / "config/research_samples/hostility-heldout-100.json"
    )
    assert identity.sample_sha256 == HELDOUT_SAMPLE_SHA256
