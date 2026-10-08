from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from hansard_annotator.llm_eval.costs import aggregate_costs
from hansard_annotator.llm_eval.metrics import compute_metrics
from hansard_annotator.llm_eval.models import ProviderResponse, TokenUsage
from hansard_annotator.llm_eval.package import EvaluationPackage, load_evaluation_package
from hansard_annotator.llm_eval.providers import GeminiProvider, OpenAIProvider, ProviderError
from hansard_annotator.llm_eval.reporting import mismatches
from hansard_annotator.llm_eval.runner import evaluate, make_configuration
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
        "record_id": f"annotation:{number}:revision:1",
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
        }
    ]
    pricing = {
        "format_version": 1,
        "effective_date": "2026-01-01",
        "source": "fixture",
        "models": {
            "openai:test": {
                "input_per_million_usd": 2,
                "cached_input_per_million_usd": 1,
                "output_per_million_usd": 10,
            }
        },
    }
    report = aggregate_costs(results, "openai", "test", pricing)
    assert report["tokens"]["reasoning_tokens"] == 30
    assert report["estimated_cost_usd"] == pytest.approx(0.0028)
    assert report["projected_cost_160k_speeches_usd"] == pytest.approx(448.0)


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
    provider = FakeProvider(failures=1)
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
    failed_provider = FakeProvider(failures=10, error=ProviderError(f"request rejected {secret}"))
    failed = evaluate(package, package.records, failed_provider, "model", output, {}, max_retries=0)
    assert failed[0]["status"] == "api_failure"
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
        generation={},
    )
    assert gemini.returned_model == "returned-gemini" and gemini.usage.reasoning_tokens == 1
    assert "gemini-secret" not in json.dumps(captured[1][2])


def test_run_identity_changes_with_material_configuration(tmp_path: Path) -> None:
    package: EvaluationPackage = load_evaluation_package(_package_dir(tmp_path))
    first, _ = make_configuration(package, "openai", "one", {"temperature": 0})
    second, _ = make_configuration(package, "openai", "two", {"temperature": 0})
    assert first.identity() != second.identity()
