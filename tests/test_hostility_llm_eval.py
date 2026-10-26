from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hansard_annotator.llm_eval.hostility import (
    compute_metrics,
    disagreements,
    partisan_hostility,
    prediction_json_schema,
    validate_prediction,
    write_reports,
)
from hansard_annotator.llm_eval.models import ProviderResponse
from hansard_annotator.llm_eval.package import load_evaluation_package
from hansard_annotator.llm_eval.prompt import build_system_prompt, prompt_hash
from hansard_annotator.llm_eval.runner import evaluate, make_configuration
from hansard_annotator.llm_eval.validation import InvalidPrediction


def _record(number: int, presence: str, target: str | None, text: str) -> dict[str, Any]:
    evidence = (
        [] if presence == "NO" else [{"start_offset": 0, "end_offset": len(text), "text": text}]
    )
    values = {"hostility_present": presence, "target": target, "evidence_span": evidence}
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "record_id": f"annotation:{number}",
        "speech": {"turn_key": f"turn-{number}", "text": text, "speaker_name": "Speaker"},
        "human_annotation": {
            "values": values,
            "values_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        },
    }


def _package(tmp_path: Path) -> Path:
    root = tmp_path / "package"
    root.mkdir()
    context = {
        "schema": {
            "slug": "australian_partisan_hostility",
            "version": "0.2.0",
            "content_sha256": "a" * 64,
            "fields": [
                {"field_key": key} for key in ("hostility_present", "target", "evidence_span")
            ],
        },
        "taxonomies": [
            {
                "slug": "hostility_presence",
                "labels": [{"code": code} for code in ("YES", "NO", "UNSURE")],
            },
            {
                "slug": "hostility_target",
                "labels": [
                    {"code": code}
                    for code in (
                        "OPPOSING_PARTISAN_TARGET",
                        "OTHER_POLITICAL_TARGET",
                        "NON_POLITICAL_TARGET",
                        "UNSURE",
                    )
                ],
            },
        ],
    }
    records = [
        _record(1, "NO", None, "The minister must act."),
        _record(2, "YES", "OPPOSING_PARTISAN_TARGET", "The government is incompetent."),
        _record(3, "UNSURE", "UNSURE", "That conduct may be contemptible."),
    ]
    files = {
        "CODEBOOK_CONTEXT.json": json.dumps(context).encode(),
        "annotations.jsonl": "".join(json.dumps(row) + "\n" for row in records).encode(),
    }
    metadata = {
        name: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in files.items()
    }
    snapshot = hashlib.sha256(
        b"".join(name.encode() + b"\0" + files[name] for name in sorted(files))
    ).hexdigest()
    manifest = {
        "format": "hansard-ai-codebook-input",
        "format_version": 1,
        "record_count": 3,
        "snapshot_sha256": snapshot,
        "files": metadata,
        "corpus": {"run_name": "phase1-full-20260723-v4"},
    }
    for name, content in files.items():
        (root / name).write_bytes(content)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _raw(presence: str, target: str, text: str, evidence: bool = True) -> dict[str, Any]:
    return {
        "hostility_present": presence,
        "target": target,
        "evidence_spans": (
            [{"start_offset": 0, "end_offset": len(text), "text": text}] if evidence else []
        ),
        "reasoning": "Diagnostic.",
    }


def test_hostility_package_dispatch_prompt_contract_and_run_identity(tmp_path: Path) -> None:
    package = load_evaluation_package(_package(tmp_path))
    assert package.task == "hostility"
    assert package.schema["version"] == "0.2.0"
    prompt = build_system_prompt(package.context, "hostility-prompt-v1")
    assert "MANDATORY ACCOUNTABILITY BOUNDARY" in prompt
    assert prompt_hash(prompt) == prompt_hash(
        build_system_prompt(package.context, "hostility-prompt-v1")
    )
    assert prediction_json_schema()["additionalProperties"] is False
    first, _ = make_configuration(
        package, "gemini", "model", {"temperature": 0}, "hostility-prompt-v1"
    )
    second, _ = make_configuration(
        package, "gemini", "model", {"temperature": 0}, "hostility-prompt-v1"
    )
    assert first.prediction_contract_sha256 is not None
    assert first.identity() == second.identity()
    with pytest.raises(ValueError, match="policy prompts cannot"):
        build_system_prompt(package.context, "phase4a-policy-v1")


def test_hostility_structured_output_invariants_and_offsets() -> None:
    text = "The government is incompetent."
    assert validate_prediction(_raw("YES", "OPPOSING_PARTISAN_TARGET", text), text).target == (
        "OPPOSING_PARTISAN_TARGET"
    )
    assert validate_prediction(_raw("NO", "N/A", text, False), text).evidence_spans == ()
    assert validate_prediction(_raw("UNSURE", "UNSURE", text), text).hostility_present == "UNSURE"
    for raw in (
        _raw("NO", "OPPOSING_PARTISAN_TARGET", text, False),
        _raw("NO", "N/A", text),
        _raw("YES", "N/A", text),
        _raw("YES", "OPPOSING_PARTISAN_TARGET", text, False),
        _raw("UNSURE", "UNSURE", text, False),
        {
            **_raw("YES", "OPPOSING_PARTISAN_TARGET", text),
            "evidence_spans": [{"start_offset": 0, "end_offset": 3, "text": "bad"}],
        },
    ):
        with pytest.raises(InvalidPrediction):
            validate_prediction(raw, text)


def test_hostility_metrics_and_derived_partisan_label() -> None:
    records = tuple(
        [
            _record(1, "NO", None, "Failure."),
            _record(2, "YES", "OPPOSING_PARTISAN_TARGET", "Incompetent."),
            _record(3, "YES", "OTHER_POLITICAL_TARGET", "Dishonest."),
        ]
    )
    results = [
        {
            "record_id": "annotation:1",
            "status": "success",
            "prediction": _raw("NO", "N/A", "Failure.", False),
        },
        {
            "record_id": "annotation:2",
            "status": "success",
            "prediction": _raw("YES", "OPPOSING_PARTISAN_TARGET", "Incompetent."),
        },
        {
            "record_id": "annotation:3",
            "status": "success",
            "prediction": _raw("YES", "NON_POLITICAL_TARGET", "Dishonest."),
        },
    ]
    metrics = compute_metrics(records, results)
    assert metrics["hostility_identification"]["f1"] == 1.0
    assert metrics["target_classification"]["human_yes_support"] == 2
    assert metrics["target_classification"]["exact_target_accuracy"] == 0.5
    assert metrics["partisan_hostility"]["f1"] == 1.0
    assert partisan_hostility(records[1]["human_annotation"]["values"])


def test_disagreement_artifacts_are_deterministic(tmp_path: Path) -> None:
    records = (_record(1, "YES", "OTHER_POLITICAL_TARGET", "Dishonest."),)
    results = [
        {
            "record_id": "annotation:1",
            "status": "success",
            "prediction": _raw("YES", "NON_POLITICAL_TARGET", "Dishonest."),
            "returned_model": "model",
        }
    ]
    rows = disagreements(records, results, run_identity="run", configured_model="model")
    assert rows[0]["adjudication_outcome"] == ""
    assert rows[0]["human_evidence"] and rows[0]["model_evidence"]
    costs = {"estimated_cost_usd": 0}
    metrics = compute_metrics(records, results)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    write_reports(first, metrics, costs, rows)
    write_reports(second, metrics, costs, rows)
    for name in ("mismatches.csv", "mismatches.jsonl", "metrics.json", "report.md"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


class _Provider:
    name = "fake"

    def generate(self, **kwargs: object) -> ProviderResponse:
        text = str(kwargs["user_prompt"]).split("SPEECH:\n", 1)[1]
        return ProviderResponse(_raw("NO", "N/A", text, False), returned_model="fake-1")


def test_hostility_smoke_uses_shared_runner_without_paid_calls(tmp_path: Path) -> None:
    package = load_evaluation_package(_package(tmp_path))
    results = evaluate(
        package,
        package.records[:1],
        _Provider(),
        "fake-1",
        tmp_path / "run",
        {},
        prompt_version="hostility-prompt-v1",
        sleeper=lambda _: None,
    )
    assert results[0]["status"] == "success"
    assert results[0]["prediction"]["hostility_present"] == "NO"
