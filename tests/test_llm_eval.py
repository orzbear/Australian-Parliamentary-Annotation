from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from hansard_annotator.llm_eval.cli import _select
from hansard_annotator.llm_eval.costs import aggregate_costs
from hansard_annotator.llm_eval.metrics import compute_metrics
from hansard_annotator.llm_eval.models import ProviderResponse, TokenUsage
from hansard_annotator.llm_eval.package import EvaluationPackage, load_evaluation_package
from hansard_annotator.llm_eval.prompt import (
    DEFAULT_PROMPT_VERSION,
    PROMPT_V2,
    available_prompt_versions,
    build_system_prompt,
    prompt_hash,
)
from hansard_annotator.llm_eval.providers import GeminiProvider, OpenAIProvider, ProviderError
from hansard_annotator.llm_eval.reporting import mismatches
from hansard_annotator.llm_eval.runner import evaluate, make_configuration
from hansard_annotator.llm_eval.selection import exclusion_hash, load_exclusion_set
from hansard_annotator.llm_eval.validation import InvalidPrediction, validate_prediction

DOMAINS = ("AU01", "AU02", "AU03", "AU_OTHER_REVIEW")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record(
    number: int, *, non_policy: bool = False, domain: str | None = "AU01"
) -> dict[str, Any]:
    values = {
        "is_non_policy": non_policy,
        "primary_australian_domain": None if non_policy else domain,
        "secondary_australian_domains": [],
        "annotation_notes": None,
    }
    return {
        "record_id": (f"annotation:00000000-0000-0000-0000-{number:012d}:revision:1"),
        "speech": {
            "turn_key": f"turn-{number}",
            "text": f"Speech {number} about hospitals",
            "speaker_name": "Research Speaker",
            "date": "2026-01-01",
            "chamber": "House",
            "major_heading": "Debate",
            "minor_heading": None,
            "business_type": None,
        },
        "human_annotation": {
            "status": "submitted",
            "revision": 1,
            "values": values,
            "values_sha256": hashlib.sha256(_canonical(values).encode()).hexdigest(),
        },
        "sample": {"batch_name": "fixture"},
    }


def _package_dir(tmp_path: Path, records: list[dict[str, Any]] | None = None) -> Path:
    target = tmp_path / "package"
    target.mkdir()
    records = records or [_record(1), _record(2, non_policy=True)]
    context = {
        "schema": {
            "slug": "australian_policy_annotation",
            "version": "0.2.0",
            "content_sha256": "schema-hash",
            "fields": [],
        },
        "taxonomies": [
            {
                "slug": "australian_policy_domains",
                "version": "0.1.0",
                "content_sha256": "taxonomy-hash",
                "labels": [{"code": code} for code in DOMAINS],
            }
        ],
    }
    contents = {
        "CODEBOOK_CONTEXT.json": (json.dumps(context) + "\n").encode(),
        "annotations.jsonl": ("".join(_canonical(record) + "\n" for record in records)).encode(),
    }
    for name, value in contents.items():
        (target / name).write_bytes(value)
    snapshot_material = b"".join(
        name.encode("utf-8") + b"\0" + contents[name] for name in sorted(contents)
    )
    manifest = {
        "format": "hansard-ai-codebook-input",
        "format_version": 1,
        "snapshot_sha256": hashlib.sha256(snapshot_material).hexdigest(),
        "record_count": len(records),
        "files": {
            name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
            for name, value in contents.items()
        },
    }
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return target


def test_package_loader_preserves_records_and_validates_checksums(tmp_path: Path) -> None:
    target = _package_dir(tmp_path)
    package = load_evaluation_package(target)
    assert package.records[0]["human_annotation"]["values"]["primary_australian_domain"] == "AU01"
    assert package.records[0]["speech"]["turn_key"] == "turn-1"
    (target / "annotations.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum or size mismatch"):
        load_evaluation_package(target)


def test_package_loader_accepts_export_zip(tmp_path: Path) -> None:
    target = _package_dir(tmp_path)
    archive = tmp_path / "export.zip"
    with ZipFile(archive, "w") as destination:
        for source in target.iterdir():
            destination.write(source, source.name)
    package = load_evaluation_package(archive)
    assert len(package.records) == 2


def test_package_rejects_incompatible_and_malformed_input(tmp_path: Path) -> None:
    target = _package_dir(tmp_path)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    manifest["format_version"] = 99
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported package format version"):
        load_evaluation_package(target)


def test_structured_prediction_validation() -> None:
    valid = validate_prediction(
        {
            "is_non_policy": False,
            "primary_australian_domain": "AU01",
            "secondary_australian_domains": ["AU02"],
            "reasoning": "Tax policy.",
        },
        DOMAINS,
    )
    assert valid.primary_australian_domain == "AU01"
    with pytest.raises(InvalidPrediction, match="require a primary"):
        validate_prediction(
            {
                "is_non_policy": False,
                "primary_australian_domain": None,
                "secondary_australian_domains": [],
                "reasoning": "Unknown.",
            },
            DOMAINS,
        )
    with pytest.raises(InvalidPrediction, match="cannot contain"):
        validate_prediction(
            {
                "is_non_policy": True,
                "primary_australian_domain": "AU01",
                "secondary_australian_domains": [],
                "reasoning": "Wrong.",
            },
            DOMAINS,
        )


def test_metrics_confusions_failures_and_zero_support() -> None:
    records = (
        _record(1),
        _record(2, non_policy=True),
        _record(3, domain="AU02"),
        _record(4, domain="AU02"),
        _record(5),
    )
    results = [
        {
            "record_id": records[0]["record_id"],
            "status": "success",
            "prediction": {"is_non_policy": False, "primary_australian_domain": "AU01"},
        },
        {
            "record_id": records[1]["record_id"],
            "status": "success",
            "prediction": {"is_non_policy": False, "primary_australian_domain": "AU02"},
        },
        {
            "record_id": records[2]["record_id"],
            "status": "success",
            "prediction": {"is_non_policy": True, "primary_australian_domain": None},
        },
        {"record_id": records[3]["record_id"], "status": "invalid", "prediction": None},
    ]
    metrics = compute_metrics(records, results, DOMAINS)
    assert metrics["status_counts"] == {"success": 3, "invalid": 1, "api_failure": 0, "missing": 1}
    assert metrics["policy"]["confusion"] == {
        "true_policy": 1,
        "true_non_policy": 0,
        "false_policy": 1,
        "false_non_policy": 1,
    }
    assert metrics["primary_domain"]["confusion_matrix"]["AU02"]["__NON_POLICY__"] == 1
    assert metrics["primary_domain"]["per_domain"]["AU03"]["support"] == 0
    assert metrics["primary_domain"]["per_domain"]["AU03"]["f1"] == 0


def test_usage_and_cost_aggregation() -> None:
    results = [
        {
            "status": "success",
            "latency_ms": 25,
            "retry_count": 1,
            "usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 200,
                "output_tokens": 100,
                "reasoning_tokens": 30,
                "total_tokens": 1100,
            },
        },
        {
            "status": "invalid",
            "latency_ms": 10,
            "retry_count": 0,
            "usage": {
                "input_tokens": 500,
                "cached_input_tokens": 100,
                "output_tokens": 20,
                "reasoning_tokens": 50,
                "total_tokens": 570,
            },
        },
        {
            "status": "api_failure",
            "latency_ms": 10,
            "retry_count": 0,
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 10,
                "total_tokens": 110,
            },
        },
    ]
    pricing = {
        "format_version": 1,
        "effective_date": "2026-01-01",
        "source": "fixture",
        "models": {
            "gemini:test": {
                "input_per_million_usd": 2,
                "cached_input_per_million_usd": 1,
                "output_per_million_usd": 10,
                "reasoning_billed_as_output": True,
            }
        },
    }
    report = aggregate_costs(results, "gemini", "test", pricing)
    assert report["tokens"]["reasoning_tokens"] == 90
    assert report["billable_tokens"]["output_tokens"] == 210
    assert report["estimated_cost_usd"] == pytest.approx(0.0050)
    assert report["attempted_requests"] == 3
    assert report["successful_requests"] == 1
    assert report["cost_per_attempt_usd"] == pytest.approx(0.0050 / 3)
    assert report["cost_per_valid_annotation_usd"] == pytest.approx(0.0050)
    assert "Mechanical" in report["projection_note"]


class FakeProvider:
    name = "fake"

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.calls = 0
        self.failures = failures
        self.error = error or ProviderError("temporary provider failure")

    def generate(self, **_: object) -> ProviderResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return ProviderResponse(
            {
                "is_non_policy": False,
                "primary_australian_domain": "AU01",
                "secondary_australian_domains": [],
                "reasoning": "Economic policy.",
            },
            TokenUsage(10, 2, 3, 1, 13),
            "fake-v1",
        )


def test_incremental_resume_retry_and_incompatible_configuration(tmp_path: Path) -> None:
    package = load_evaluation_package(_package_dir(tmp_path, [_record(1)]))
    output = tmp_path / "run"
    provider = FakeProvider(
        failures=1,
        error=ProviderError("billed retry", usage=TokenUsage(5, 1, 0, 2, 7)),
    )
    first = evaluate(
        package,
        package.records,
        provider,
        "model-a",
        output,
        {"temperature": 0},
        max_retries=1,
        retry_delay=0,
        sleeper=lambda _: None,
    )
    assert first[0]["status"] == "success" and first[0]["retry_count"] == 1
    assert first[0]["usage"] == {
        "input_tokens": 15,
        "cached_input_tokens": 3,
        "output_tokens": 3,
        "reasoning_tokens": 3,
        "total_tokens": 20,
    }
    assert provider.calls == 2
    evaluate(package, package.records, provider, "model-a", output, {"temperature": 0})
    assert provider.calls == 2
    assert len((output / "predictions.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(ValueError, match="incompatible"):
        evaluate(package, package.records, provider, "model-b", output, {"temperature": 0})


def test_provider_failure_checkpoint_is_retryable_and_secret_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = load_evaluation_package(_package_dir(tmp_path, [_record(1)]))
    secret = "super-secret-api-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    output = tmp_path / "run"
    failed_provider = FakeProvider(
        failures=10,
        error=ProviderError(
            f"request rejected {secret}",
            usage=TokenUsage(10, 2, 0, 3, 13),
            returned_model="billed-failure-model",
            diagnostics={"provider_finish_reason": "SAFETY"},
        ),
    )
    failed = evaluate(package, package.records, failed_provider, "model", output, {}, max_retries=0)
    assert failed[0]["status"] == "api_failure"
    assert failed[0]["usage"]["reasoning_tokens"] == 3
    assert failed[0]["returned_model"] == "billed-failure-model"
    assert secret not in (output / "predictions.jsonl").read_text(encoding="utf-8")
    successful_provider = FakeProvider()
    resumed = evaluate(package, package.records, successful_provider, "model", output, {})
    assert resumed[0]["status"] == "success"
    assert successful_provider.calls == 1


def test_invalid_response_and_deterministic_mismatches(tmp_path: Path) -> None:
    package = load_evaluation_package(_package_dir(tmp_path, [_record(1)]))

    class InvalidProvider(FakeProvider):
        def generate(self, **_: object) -> ProviderResponse:
            return ProviderResponse({"unexpected": True})

    results = evaluate(package, package.records, InvalidProvider(), "model", tmp_path / "run", {})
    assert results[0]["status"] == "invalid"
    valid_results = [
        {
            "record_id": package.records[0]["record_id"],
            "status": "success",
            "prediction": {
                "is_non_policy": False,
                "primary_australian_domain": "AU02",
                "secondary_australian_domains": [],
                "reasoning": "Different boundary.",
            },
            "returned_model": "v1",
        }
    ]
    assert mismatches(package.records, valid_results, snippet_chars=10) == mismatches(
        package.records, valid_results, snippet_chars=10
    )
    assert (
        mismatches(package.records, valid_results, snippet_chars=10)[0]["human_primary_domain"]
        == "AU01"
    )


@pytest.mark.parametrize(
    ("response_text", "expected_type", "parsed"),
    (("[]", "array", True), ('"wrapped"', "string", True), ("{malformed", "string", False)),
)
def test_gemini_invalid_top_level_shapes_persist_bounded_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_text: str,
    expected_type: str,
    parsed: bool,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-secret")
    package = load_evaluation_package(_package_dir(tmp_path, [_record(1)]))

    def transport(
        _url: str, _headers: dict[str, str], payload: dict[str, Any], _timeout: float
    ) -> dict[str, Any]:
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        assert payload["generationConfig"]["responseJsonSchema"]["type"] == "object"
        return {
            "modelVersion": "gemini-2.5-flash",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": response_text}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 2,
                "thoughtsTokenCount": 3,
                "totalTokenCount": 15,
            },
        }

    provider = GeminiProvider(transport=transport)
    output = tmp_path / f"run-{expected_type}-{parsed}"
    results = evaluate(package, package.records, provider, "gemini-2.5-flash", output, {})
    assert results[0]["status"] == "invalid"
    diagnostics = results[0]["diagnostics"]
    assert diagnostics["parsed_top_level_json_type"] == expected_type
    assert diagnostics["json_parse_succeeded"] is parsed
    assert diagnostics["provider_finish_reason"] == "STOP"
    assert diagnostics["response_content_exists"] is True
    assert diagnostics["native_structured_output_requested"] is True
    assert diagnostics["schema_validation_error"] == "response is not an object"
    assert len(diagnostics["structured_response_preview"]) <= 2000


def test_openai_and_gemini_adapters_parse_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    captured: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def openai_transport(
        url: str, headers: dict[str, str], payload: dict[str, Any], _: float
    ) -> dict[str, Any]:
        captured.append((url, headers, payload))
        return {
            "model": "returned-openai",
            "output_text": json.dumps({"ok": True}),
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens_details": {"reasoning_tokens": 1},
            },
        }

    openai = OpenAIProvider(transport=openai_transport).generate(
        model="configured",
        system_prompt="system",
        user_prompt="user",
        schema={"type": "object"},
        generation={},
    )
    assert openai.returned_model == "returned-openai" and openai.usage.cached_input_tokens == 3
    assert "openai-secret" not in json.dumps(captured[0][2])

    def gemini_transport(
        url: str, headers: dict[str, str], payload: dict[str, Any], _: float
    ) -> dict[str, Any]:
        captured.append((url, headers, payload))
        return {
            "modelVersion": "returned-gemini",
            "candidates": [{"content": {"parts": [{"text": json.dumps({"ok": True})}]}}],
            "usageMetadata": {
                "promptTokenCount": 9,
                "cachedContentTokenCount": 2,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 1,
                "totalTokenCount": 12,
            },
        }

    gemini = GeminiProvider(transport=gemini_transport).generate(
        model="configured",
        system_prompt="system",
        user_prompt="user",
        schema={"type": "object"},
        generation={"thinking_mode": "disabled", "thinking_budget": 0},
    )
    assert gemini.returned_model == "returned-gemini" and gemini.usage.reasoning_tokens == 1
    assert "gemini-secret" not in json.dumps(captured[1][2])
    assert captured[1][2]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_run_identity_changes_with_material_configuration(tmp_path: Path) -> None:
    package: EvaluationPackage = load_evaluation_package(_package_dir(tmp_path))
    first, _ = make_configuration(package, "openai", "one", {"temperature": 0})
    second, _ = make_configuration(package, "openai", "two", {"temperature": 0})
    assert first.identity() != second.identity()
    dynamic, _ = make_configuration(
        package,
        "gemini",
        "gemini-2.5-flash",
        {"thinking_mode": "provider_default_dynamic"},
    )
    disabled, _ = make_configuration(
        package,
        "gemini",
        "gemini-2.5-flash",
        {"thinking_mode": "disabled", "thinking_budget": 0},
    )
    assert dynamic.identity() != disabled.identity()


def test_prompt_versions_coexist_with_distinct_hashes_and_run_identities(tmp_path: Path) -> None:
    package = load_evaluation_package(_package_dir(tmp_path))
    assert DEFAULT_PROMPT_VERSION == "phase4a-policy-v1"
    assert available_prompt_versions() == ("phase4a-policy-v1", "phase4a-policy-v2")
    v1 = build_system_prompt(package.context, "phase4a-policy-v1")
    v2 = build_system_prompt(package.context, "phase4a-policy-v2")
    assert v1.startswith("You classify quoted Australian parliamentary speech.")
    assert "Follow this mandatory hierarchy" not in v1
    assert prompt_hash(v1) != prompt_hash(v2)
    v1_config, _ = make_configuration(
        package, "gemini", "gemini-2.5-flash", {}, "phase4a-policy-v1"
    )
    v2_config, _ = make_configuration(
        package, "gemini", "gemini-2.5-flash", {}, "phase4a-policy-v2"
    )
    assert v1_config.prompt_version == "phase4a-policy-v1"
    assert v2_config.prompt_version == "phase4a-policy-v2"
    assert v1_config.identity() != v2_config.identity()


def test_v2_encodes_policy_first_hierarchy_and_research_rules(tmp_path: Path) -> None:
    package = load_evaluation_package(_package_dir(tmp_path))
    prompt = build_system_prompt(package.context, "phase4a-policy-v2")
    step_1 = prompt.index("STEP 1 — SUBSTANTIVE-POLICY GATE")
    step_2 = prompt.index("STEP 2 — PRIMARY POLICY ISSUE")
    step_3 = prompt.index("STEP 3 — PRECEDENCE RULES")
    step_4 = prompt.index("STEP 4 — EXACTLY ONE PRIMARY DOMAIN")
    step_5 = prompt.index("STEP 5 — SECONDARY DOMAINS")
    step_6 = prompt.index("STEP 6 — CONCISE REASONING/EVIDENCE")
    assert step_1 < step_2 < step_3 < step_4 < step_5 < step_6
    assert "is_non_policy=true" in prompt
    assert "primary_australian_domain=null" in prompt
    assert "secondary_australian_domains=[]" in prompt
    assert "veteran-specific institutions" in prompt and "use AU12 as primary" in prompt
    assert "disaster recovery grants" in prompt and "use AU07 as primary" in prompt
    assert "AU_OTHER_REVIEW is not a general uncertainty" in prompt
    assert "ceremonial remarks" in prompt
    assert "Government funding does not automatically pass the policy gate" in prompt


def test_v2_prompt_text_is_frozen() -> None:
    assert (
        hashlib.sha256(PROMPT_V2.encode("utf-8")).hexdigest()
        == "b98b37d248e3b990e6a2feb1f9f6057ccb985471f6ed6e92e39900238076c744"
    )


def test_explicit_exclusion_is_compatible_deterministic_and_changes_run_identity(
    tmp_path: Path,
) -> None:
    package = load_evaluation_package(_package_dir(tmp_path, [_record(1), _record(2), _record(3)]))
    excluded_id = str(package.records[1]["record_id"])
    exclusion_path = tmp_path / "development.txt"
    exclusion_path.write_text(excluded_id + "\n", encoding="utf-8")
    manifest = {
        "development_record_count": 1,
        "record_ids_sha256": exclusion_hash({excluded_id}),
        "source_package_snapshot_sha256": package.manifest["snapshot_sha256"],
    }
    exclusion_path.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    exclusions = load_exclusion_set(
        exclusion_path,
        package_snapshot_sha256=str(package.manifest["snapshot_sha256"]),
        package_record_ids={str(record["record_id"]) for record in package.records},
    )
    first = _select(package.records, [], 17, None, exclusions)
    second = _select(package.records, [], 17, None, exclusions)
    assert first == second
    assert len(first) == 2
    assert excluded_id not in {record["record_id"] for record in first}
    baseline, _ = make_configuration(package, "gemini", "model", {})
    held_out, _ = make_configuration(
        package,
        "gemini",
        "model",
        {},
        exclusion_record_count=exclusions.count,
        exclusion_set_sha256=exclusions.sha256,
    )
    assert held_out.exclusion_record_count == 1
    assert held_out.exclusion_set_sha256 == exclusions.sha256
    assert baseline.identity() != held_out.identity()


@pytest.mark.parametrize(
    "content",
    (
        "not-a-record-id\n",
        "annotation:00000000-0000-0000-0000-000000000001:revision:0\n",
        "annotation:00000000-0000-0000-0000-000000000001:revision:1\n"
        "annotation:00000000-0000-0000-0000-000000000001:revision:1\n",
    ),
)
def test_malformed_or_duplicate_exclusion_files_fail(tmp_path: Path, content: str) -> None:
    package = load_evaluation_package(_package_dir(tmp_path, [_record(1)]))
    path = tmp_path / "bad.txt"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=r"malformed|duplicate"):
        load_exclusion_set(
            path,
            package_snapshot_sha256=str(package.manifest["snapshot_sha256"]),
            package_record_ids={str(package.records[0]["record_id"])},
        )


def test_exclusion_manifest_rejects_an_incompatible_package(tmp_path: Path) -> None:
    package = load_evaluation_package(_package_dir(tmp_path, [_record(1)]))
    record_id = str(package.records[0]["record_id"])
    path = tmp_path / "development.txt"
    path.write_text(record_id + "\n", encoding="utf-8")
    path.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "development_record_count": 1,
                "record_ids_sha256": exclusion_hash({record_id}),
                "source_package_snapshot_sha256": "different-snapshot",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="incompatible"):
        load_exclusion_set(
            path,
            package_snapshot_sha256=str(package.manifest["snapshot_sha256"]),
            package_record_ids={record_id},
        )
