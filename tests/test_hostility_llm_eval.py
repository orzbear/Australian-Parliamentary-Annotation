from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hansard_annotator.llm_eval.cli import _validate_luna_output_limit
from hansard_annotator.llm_eval.hostility import (
    MAX_REASONING_LENGTH,
    compute_metrics,
    disagreements,
    partisan_hostility,
    prediction_json_schema,
    validate_prediction,
    validate_prediction_with_diagnostics,
    write_reports,
)
from hansard_annotator.llm_eval.hostility_comparison import (
    build_comparison_summary,
    compare_hostility_runs,
    write_hostility_comparison,
)
from hansard_annotator.llm_eval.models import ProviderResponse
from hansard_annotator.llm_eval.package import load_evaluation_package
from hansard_annotator.llm_eval.prompt import (
    HOSTILITY_LUNA_CONTRACT_VERSION,
    HOSTILITY_LUNA_PROMPT_VERSION,
    HOSTILITY_PRODUCTION_PROMPT_VERSION,
    HOSTILITY_PROMPT_V1_1,
    HOSTILITY_PROMPT_V2,
    build_system_prompt,
    build_user_prompt,
    prompt_hash,
)
from hansard_annotator.llm_eval.providers import OpenAIProvider
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
        "evidence_spans": ([{"text": text}] if evidence else []),
        "reasoning": "Diagnostic.",
    }


def test_hostility_package_dispatch_prompt_contract_and_run_identity(tmp_path: Path) -> None:
    package = load_evaluation_package(_package(tmp_path))
    assert package.task == "hostility"
    assert package.schema["version"] == "0.2.0"
    prompt = build_system_prompt(package.context, "hostility-prompt-v1.1")
    assert "MANDATORY ACCOUNTABILITY BOUNDARY" in prompt
    assert prompt_hash(prompt) == prompt_hash(
        build_system_prompt(package.context, "hostility-prompt-v1.1")
    )
    assert prediction_json_schema()["additionalProperties"] is False
    new_evidence = prediction_json_schema()["properties"]["evidence_spans"]["items"]
    old_evidence = prediction_json_schema("hostility-prompt-v1")["properties"]["evidence_spans"][
        "items"
    ]
    assert set(new_evidence["properties"]) == {"text"}
    assert set(old_evidence["properties"]) == {"start_offset", "end_offset", "text"}
    first, _ = make_configuration(
        package, "gemini", "model", {"temperature": 0}, "hostility-prompt-v1.1"
    )
    second, _ = make_configuration(
        package, "gemini", "model", {"temperature": 0}, "hostility-prompt-v1.1"
    )
    assert first.prediction_contract_sha256 is not None
    assert first.identity() == second.identity()
    with pytest.raises(ValueError, match="policy prompts cannot"):
        build_system_prompt(package.context, "phase4a-policy-v1")


def test_production_no_reasoning_contract_preserves_rules_and_evidence_resolution() -> None:
    schema = prediction_json_schema(HOSTILITY_PRODUCTION_PROMPT_VERSION)
    context = json.loads(
        Path("config/llm_eval/hostility-v2-codebook-context.json").read_text(encoding="utf-8")
    )
    prompt = build_system_prompt(context, HOSTILITY_PRODUCTION_PROMPT_VERSION)
    assert set(schema["properties"]) == {"hostility_present", "target", "evidence_spans"}
    assert schema["required"] == ["hostility_present", "target", "evidence_spans"]
    assert "MANDATORY ACCOUNTABILITY BOUNDARY" in prompt
    assert "TARGET (classify only after YES or UNSURE)" in prompt
    assert "EVIDENCE:" in prompt
    assert "reasoning" not in prompt.lower()
    assert prompt_hash(prompt) == "42cea86b33e561b2ed8bec4ac57740e6132b614d577896e9b6ea8a0d92fc83b8"
    contract = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(contract.encode()).hexdigest() == (
        "c4f52db4fddb092a0f51fa949fa0ccc9b730ae11e2bedf42d27f5933eecd21e4"
    )
    raw = {
        "hostility_present": "YES",
        "target": "OPPOSING_PARTISAN_TARGET",
        "evidence_spans": [{"text": "government is incompetent"}],
    }
    prediction, diagnostics = validate_prediction_with_diagnostics(
        raw,
        "The government is incompetent.",
        HOSTILITY_PRODUCTION_PROMPT_VERSION,
    )
    assert prediction.hostility_present == "YES"
    assert prediction.target == "OPPOSING_PARTISAN_TARGET"
    assert prediction.evidence_spans[0]["start_offset"] == 4
    assert diagnostics["evidence_valid"] is True
    with pytest.raises(InvalidPrediction, match="response fields"):
        validate_prediction_with_diagnostics(
            {**raw, "reasoning": "unexpected"},
            "The government is incompetent.",
            HOSTILITY_PRODUCTION_PROMPT_VERSION,
        )


def test_luna_prompt_v1_is_distinct_and_uses_reasoning_free_structured_contract(
    tmp_path: Path,
) -> None:
    package = load_evaluation_package(_package(tmp_path))
    context = json.loads(
        Path("config/llm_eval/hostility-v2-codebook-context.json").read_text(encoding="utf-8")
    )
    prompt = build_system_prompt(context, HOSTILITY_LUNA_PROMPT_VERSION)
    schema = prediction_json_schema(HOSTILITY_LUNA_PROMPT_VERSION)
    assert HOSTILITY_LUNA_PROMPT_VERSION == "hostility-prompt-luna-v1"
    assert "DECISION PROCEDURE — FOLLOW IN ORDER" in prompt
    for required in (
        "Routine adversarial parliamentary criticism",
        "ordinary partisan contrast",
        "An opposing party is not hostile merely because it is the opposing party",
        "smallest exact verbatim",
        "target must be N/A",
    ):
        assert required in prompt
    assert set(schema["properties"]) == {"hostility_present", "target", "evidence_spans"}
    rendered = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        "c4f52db4fddb092a0f51fa949fa0ccc9b730ae11e2bedf42d27f5933eecd21e4"
    )
    assert prompt_hash(prompt) == "daeb12f872bb908112fc449ba5a0b0a444fa8624dcc9f805555e3e9a73bcc964"
    config, _ = make_configuration(
        package,
        "openai",
        "gpt-6-luna",
        {"max_output_tokens": 500, "reasoning_effort": "medium"},
        HOSTILITY_LUNA_PROMPT_VERSION,
    )
    production_config, _ = make_configuration(
        package,
        "openai",
        "gpt-6-luna",
        {"max_output_tokens": 500, "reasoning_effort": "medium"},
        HOSTILITY_PRODUCTION_PROMPT_VERSION,
    )
    assert config.prediction_contract_version == HOSTILITY_LUNA_CONTRACT_VERSION
    assert config.identity() != production_config.identity()
    assert config.prediction_contract_sha256 == (
        "c4f52db4fddb092a0f51fa949fa0ccc9b730ae11e2bedf42d27f5933eecd21e4"
    )


@pytest.mark.parametrize("max_output_tokens", [500, 1000])
def test_luna_prompt_v1_accepts_controlled_output_limits(max_output_tokens: int) -> None:
    _validate_luna_output_limit(max_output_tokens)


@pytest.mark.parametrize("max_output_tokens", [750, 1500])
def test_luna_prompt_v1_rejects_unapproved_output_limits(max_output_tokens: int) -> None:
    with pytest.raises(ValueError, match="supports max_output_tokens=500 or 1000"):
        _validate_luna_output_limit(max_output_tokens)


def test_production_cache_prefix_and_variable_record_serialization_are_stable(
    tmp_path: Path,
) -> None:
    package = load_evaluation_package(_package(tmp_path))
    first = build_system_prompt(package.context, HOSTILITY_PRODUCTION_PROMPT_VERSION)
    second = build_system_prompt(package.context, HOSTILITY_PRODUCTION_PROMPT_VERSION)
    users = [build_user_prompt(record) for record in package.records]
    from hansard_annotator.llm_eval.operations import (
        build_batch_create_request,
        build_cache_create_request,
    )

    cache_request = build_cache_create_request(
        model="gemini-2.5-flash",
        system_prompt=first,
        ttl_seconds=3600,
        display_name="hostility-prefix-test",
    )
    batch_request = build_batch_create_request(
        model="gemini-2.5-flash",
        system_prompt=second,
        records=package.records,
        schema=prediction_json_schema(HOSTILITY_PRODUCTION_PROMPT_VERSION),
        display_name="hostility-prefix-test",
        cached_content="cachedContents/test",
        max_output_tokens=500,
    )["batch"]["input_config"]["requests"]["requests"][0]["request"]
    assert first.encode("utf-8") == second.encode("utf-8")
    assert all("SPEECH:" not in first for _ in package.records)
    assert all(user.startswith("METADATA:\n") and "\n\nSPEECH:\n" in user for user in users)
    assert len(set(users)) == len(users)
    assert cache_request["systemInstruction"]["parts"][0]["text"] == first
    assert batch_request["cachedContent"] == "cachedContents/test"
    assert "systemInstruction" not in batch_request


@pytest.mark.parametrize("max_output_tokens", [1000, 500])
def test_production_output_ceiling_is_applied_to_gemini_schema_request(
    tmp_path: Path, max_output_tokens: int
) -> None:
    from hansard_annotator.llm_eval.operations import build_batch_create_request

    package = load_evaluation_package(_package(tmp_path))
    schema = prediction_json_schema(HOSTILITY_PRODUCTION_PROMPT_VERSION)
    request = build_batch_create_request(
        model="gemini-2.5-flash",
        system_prompt=build_system_prompt(package.context, HOSTILITY_PRODUCTION_PROMPT_VERSION),
        records=package.records,
        schema=schema,
        display_name="hostility-format-test",
        max_output_tokens=max_output_tokens,
        generation={"thinking_budget": 0},
    )["batch"]["input_config"]["requests"]["requests"][0]["request"]
    assert request["generationConfig"]["maxOutputTokens"] == max_output_tokens
    assert set(request["generationConfig"]["responseSchema"]["properties"]) == {
        "hostility_present",
        "target",
        "evidence_spans",
    }
    assert request["generationConfig"]["responseSchema"]["required"] == [
        "hostility_present",
        "target",
        "evidence_spans",
    ]


def test_production_no_reasoning_run_artifact_keeps_only_three_prediction_fields(
    tmp_path: Path,
) -> None:
    package = load_evaluation_package(_package(tmp_path))

    class Provider:
        name = "gemini"

        def generate(self, **kwargs: Any) -> ProviderResponse:
            assert "reasoning" not in kwargs["schema"]["properties"]
            return ProviderResponse(
                {
                    "hostility_present": "YES",
                    "target": "OPPOSING_PARTISAN_TARGET",
                    "evidence_spans": [{"text": "government is incompetent"}],
                }
            )

    evaluate(
        package,
        (package.records[1],),
        Provider(),  # type: ignore[arg-type]
        "gemini-2.5-flash",
        tmp_path / "no-reasoning-run",
        {"temperature": 0.0, "thinking_budget": 0, "max_output_tokens": 1000},
        prompt_version=HOSTILITY_PRODUCTION_PROMPT_VERSION,
    )
    result = json.loads(
        (tmp_path / "no-reasoning-run" / "predictions.jsonl").read_text(encoding="utf-8")
    )
    assert set(result["prediction"]) == {"hostility_present", "target", "evidence_spans"}


def test_corrected_hostility_package_validates_when_local_export_is_present() -> None:
    path = Path(
        "data/review/hostility-development-100-ai-codebook-input-508e297185bd"
    )
    if not path.exists():
        pytest.skip("local corrected hostility development export is required")
    package = load_evaluation_package(path)
    assert len(package.records) == 100
    assert package.manifest["snapshot_sha256"] == (
        "508e297185bd8bd82c25cb86014b6264de66e6032ee99780eeb265eb33b714dd"
    )
    assert package.schema["slug"] == "australian_partisan_hostility"
    assert package.schema["version"] == "0.2.0"
    assert package.manifest["corpus"]["run_name"] == "phase1-full-20260723-v4"
    generation = {
        "max_output_tokens": 1000,
        "temperature": 0.0,
        "thinking_mode": "disabled",
        "thinking_budget": 0,
    }
    v1, v1_prompt = make_configuration(
        package, "gemini", "gemini-2.5-flash", generation, "hostility-prompt-v1.1"
    )
    v2, v2_prompt = make_configuration(
        package, "gemini", "gemini-2.5-flash", generation, "hostility-prompt-v2"
    )
    assert prompt_hash(v1_prompt) == (
        "2cd88c94a0bdb811069c3ca126859a493e39b9f075a65b4d5edc3a049461bfa0"
    )
    assert v1.prediction_contract_sha256 == (
        "fc64d0bcbf138afe7385dbef9cf8d9902e377140c148268cba357649d7339367"
    )
    assert prompt_hash(v2_prompt) == (
        "185f5162348ae3b3d735ea43247da18cf6b3c1bb12f8a72cad57e38ce31cb1a8"
    )
    assert v2.prediction_contract_sha256 == (
        "dc716b49e0759ba7063973038b234f6696e778c4d90ee4a405d9600a2435b11d"
    )


def test_v1_1_identity_is_immutable_and_v2_is_frozen(tmp_path: Path) -> None:
    package = load_evaluation_package(_package(tmp_path))
    v2_prompt = build_system_prompt(package.context, "hostility-prompt-v2")
    v1, _ = make_configuration(
        package, "gemini", "gemini-2.5-flash", {}, "hostility-prompt-v1.1"
    )
    v2, _ = make_configuration(
        package, "gemini", "gemini-2.5-flash", {}, "hostility-prompt-v2"
    )
    assert hashlib.sha256(HOSTILITY_PROMPT_V1_1.encode()).hexdigest() == (
        "dc3ef4f2465bd7012ce892d6fe641739c598301cf0e520a8f13a8edd9c988ae8"
    )
    assert v1.prediction_contract_sha256 == (
        "fc64d0bcbf138afe7385dbef9cf8d9902e377140c148268cba357649d7339367"
    )
    assert "Do not infer incompetence" in v2_prompt
    assert hashlib.sha256(HOSTILITY_PROMPT_V2.encode()).hexdigest() == (
        "3fd1b40f1c696a3af01a2b2d02012b5ffb154775a45f6b6d68dc6dfddb793806"
    )
    assert v2.prediction_contract_sha256 == (
        "dc716b49e0759ba7063973038b234f6696e778c4d90ee4a405d9600a2435b11d"
    )
    assert prediction_json_schema("hostility-prompt-v2")["properties"]["evidence_spans"] == (
        prediction_json_schema("hostility-prompt-v1.1")["properties"]["evidence_spans"]
    )


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
            "evidence_spans": [{"text": ""}],
        },
    ):
        with pytest.raises(InvalidPrediction):
            validate_prediction(raw, text)


def test_reasoning_telemetry_has_a_larger_but_finite_runtime_limit() -> None:
    text = "The minister must act."
    longer = _raw("NO", "N/A", text, False)
    longer["reasoning"] = "x" * 1001
    assert validate_prediction(longer, text).reasoning == "x" * 1001
    excessive = {**longer, "reasoning": "x" * (MAX_REASONING_LENGTH + 1)}
    with pytest.raises(InvalidPrediction, match="reasoning must be a short string"):
        validate_prediction(excessive, text)


def test_evidence_text_resolution_unique_unicode_and_multiline() -> None:
    text = "Opening — naïve claim.\nSecond line is cruel. Closing."
    quote = "naïve claim.\nSecond line is cruel."
    prediction, diagnostics = validate_prediction_with_diagnostics(
        _raw("YES", "OTHER_POLITICAL_TARGET", quote), text
    )
    start = text.index(quote)
    assert prediction.evidence_spans == (
        {"start_offset": start, "end_offset": start + len(quote), "text": quote},
    )
    assert text[start : start + len(quote)] == quote
    assert diagnostics["classification_valid"] is True
    assert diagnostics["evidence_valid"] is True


def test_unmatched_and_ambiguous_evidence_preserve_valid_classification() -> None:
    unmatched, unmatched_diagnostics = validate_prediction_with_diagnostics(
        _raw("YES", "OPPOSING_PARTISAN_TARGET", "not verbatim"), "Canonical speech."
    )
    ambiguous, ambiguous_diagnostics = validate_prediction_with_diagnostics(
        _raw("UNSURE", "UNSURE", "repeat"), "repeat then repeat"
    )
    assert unmatched.hostility_present == "YES" and unmatched.evidence_spans == ()
    assert unmatched_diagnostics["evidence_valid"] is False
    assert unmatched_diagnostics["evidence_unmatched"] == ["not verbatim"]
    assert ambiguous.hostility_present == "UNSURE" and ambiguous.evidence_spans == ()
    assert ambiguous_diagnostics["evidence_valid"] is False
    assert ambiguous_diagnostics["evidence_ambiguous"] == ["repeat"]


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


def test_v1_1_v2_comparison_artifacts_are_deterministic(tmp_path: Path) -> None:
    records = (
        _record(1, "NO", None, "Failure."),
        _record(2, "YES", "OPPOSING_PARTISAN_TARGET", "Incompetent."),
    )
    first = [
        {"record_id": "annotation:1", "status": "success", "prediction": _raw("YES", "OTHER_POLITICAL_TARGET", "Failure.")},
        {"record_id": "annotation:2", "status": "success", "prediction": _raw("YES", "OPPOSING_PARTISAN_TARGET", "Incompetent.")},
    ]
    second = [
        {"record_id": "annotation:1", "status": "success", "prediction": _raw("NO", "N/A", "Failure.", False)},
        {"record_id": "annotation:2", "status": "success", "prediction": _raw("YES", "OTHER_POLITICAL_TARGET", "Incompetent.")},
    ]
    rows = compare_hostility_runs(records, first, second)
    assert [row["comparison_status"] for row in rows] == [
        "V2_ONLY_CORRECT",
        "V1_ONLY_CORRECT",
    ]
    metrics = compute_metrics(records, first)
    summary = build_comparison_summary(metrics, compute_metrics(records, second), rows)
    one, two = tmp_path / "one", tmp_path / "two"
    write_hostility_comparison(one, rows, summary)
    write_hostility_comparison(two, rows, summary)
    for name in ("comparison.csv", "comparison.jsonl", "comparison_summary.json", "report.md"):
        assert (one / name).read_bytes() == (two / name).read_bytes()


class _Provider:
    name = "fake"

    def generate(self, **kwargs: object) -> ProviderResponse:
        text = str(kwargs["user_prompt"]).split("SPEECH:\n", 1)[1]
        return ProviderResponse(_raw("NO", "N/A", text, False), returned_model="fake-1")


class _UnmatchedEvidenceProvider:
    name = "fake"

    def generate(self, **kwargs: object) -> ProviderResponse:
        return ProviderResponse(
            _raw("YES", "OPPOSING_PARTISAN_TARGET", "not copied from speech"),
            returned_model="fake-1",
        )


def test_hostility_smoke_uses_shared_runner_without_paid_calls(tmp_path: Path) -> None:
    package = load_evaluation_package(_package(tmp_path))
    results = evaluate(
        package,
        package.records[:1],
        _Provider(),
        "fake-1",
        tmp_path / "run",
        {},
        prompt_version="hostility-prompt-v1.1",
        sleeper=lambda _: None,
    )
    assert results[0]["status"] == "success"
    assert results[0]["prediction"]["hostility_present"] == "NO"


def test_runner_separates_evidence_failure_from_classification_failure(tmp_path: Path) -> None:
    package = load_evaluation_package(_package(tmp_path))
    results = evaluate(
        package,
        package.records[:1],
        _UnmatchedEvidenceProvider(),
        "fake-1",
        tmp_path / "unmatched-run",
        {},
        prompt_version="hostility-prompt-v1.1",
        sleeper=lambda _: None,
    )
    assert results[0]["status"] == "success"
    assert results[0]["prediction"]["hostility_present"] == "YES"
    assert results[0]["diagnostics"]["classification_valid"] is True
    assert results[0]["diagnostics"]["evidence_valid"] is False
    assert results[0]["diagnostics"]["evidence_unmatched"] == ["not copied from speech"]


def test_gpt6_luna_responses_schema_and_low_reasoning_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    captured: dict[str, Any] = {}
    response_prediction = {
        "hostility_present": "YES",
        "target": "OPPOSING_PARTISAN_TARGET",
        "evidence_spans": [{"text": "attacked"}],
        "reasoning": "Explicitly hostile language.",
    }

    def transport(
        url: str, headers: dict[str, str], payload: dict[str, Any], _: float
    ) -> dict[str, Any]:
        captured.update({"url": url, "headers": headers, "payload": payload})
        return {
            "model": "gpt-6-luna-2026-10-29",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(response_prediction)}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 40,
                "output_tokens": 12,
                "total_tokens": 52,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        }

    response = OpenAIProvider(transport=transport).generate(
        model="gpt-6-luna",
        system_prompt="hostility system prompt",
        user_prompt="METADATA: {}\n\nSPEECH:\nThey attacked our party.",
        schema=prediction_json_schema("hostility-prompt-v2"),
        generation={"max_output_tokens": 1000, "reasoning_effort": "low"},
    )
    request = captured["payload"]
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert request["model"] == "gpt-6-luna"
    assert request["reasoning"] == {"effort": "low"}
    assert request["max_output_tokens"] == 1000
    assert "temperature" not in request
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"] == prediction_json_schema("hostility-prompt-v2")
    assert "test-secret" not in json.dumps(request)
    assert response.returned_model == "gpt-6-luna-2026-10-29"
    assert response.usage.cached_input_tokens == 4
    assert response.usage.reasoning_tokens == 3
    parsed, diagnostics = validate_prediction_with_diagnostics(
        response.raw_prediction, "They attacked our party.", "hostility-prompt-v2"
    )
    assert parsed.hostility_present == "YES"
    assert parsed.evidence_spans[0]["text"] == "attacked"
    assert diagnostics["evidence_valid"] is True


def test_luna_openai_request_omits_reasoning_from_structured_response_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    captured: dict[str, Any] = {}
    raw = {
        "hostility_present": "YES",
        "target": "OPPOSING_PARTISAN_TARGET",
        "evidence_spans": [{"text": "is incompetent"}],
    }

    def transport(
        url: str, headers: dict[str, str], payload: dict[str, Any], _: float
    ) -> dict[str, Any]:
        captured.update(payload)
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(raw)}],
                }
            ],
            "usage": {"input_tokens": 40, "output_tokens": 8, "total_tokens": 48},
        }

    response = OpenAIProvider(transport=transport).generate(
        model="gpt-6-luna",
        system_prompt="Luna prompt v1",
        user_prompt="METADATA: {}\n\nSPEECH:\nThe minister is incompetent.",
        schema=prediction_json_schema(HOSTILITY_LUNA_PROMPT_VERSION),
        generation={"max_output_tokens": 500, "reasoning_effort": "medium"},
    )
    request_schema = captured["text"]["format"]["schema"]
    assert set(request_schema["properties"]) == {"hostility_present", "target", "evidence_spans"}
    assert "reasoning" not in request_schema["required"]
    assert captured["reasoning"] == {"effort": "medium"}
    assert captured["max_output_tokens"] == 500
    prediction, diagnostics = validate_prediction_with_diagnostics(
        response.raw_prediction,
        "The minister is incompetent.",
        HOSTILITY_LUNA_PROMPT_VERSION,
    )
    assert prediction.reasoning == ""
    assert diagnostics["evidence_valid"] is True
