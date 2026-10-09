"""Incremental, compatibility-checked evaluation execution."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hansard_annotator.llm_eval.models import RequestResult, TokenUsage
from hansard_annotator.llm_eval.package import EvaluationPackage
from hansard_annotator.llm_eval.prompt import (
    DEFAULT_PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
    prediction_json_schema,
    prompt_hash,
)
from hansard_annotator.llm_eval.providers import (
    ModelProvider,
    ProviderError,
    provider_prediction_schema_hash,
)
from hansard_annotator.llm_eval.validation import InvalidPrediction, validate_prediction


@dataclass(frozen=True)
class RunConfiguration:
    provider: str
    model: str
    package_snapshot_sha256: str
    schema_version: str
    schema_sha256: str
    prompt_version: str
    prompt_sha256: str
    generation: dict[str, Any]
    exclusion_record_count: int = 0
    exclusion_set_sha256: str | None = None
    provider_prediction_schema_sha256: str | None = None

    def identity(self) -> str:
        values = asdict(self)
        if self.exclusion_record_count == 0 and self.exclusion_set_sha256 is None:
            values.pop("exclusion_record_count")
            values.pop("exclusion_set_sha256")
        if self.provider_prediction_schema_sha256 is None:
            values.pop("provider_prediction_schema_sha256")
        payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def make_configuration(
    package: EvaluationPackage,
    provider: str,
    model: str,
    generation: dict[str, Any],
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    exclusion_record_count: int = 0,
    exclusion_set_sha256: str | None = None,
) -> tuple[RunConfiguration, str]:
    system = build_system_prompt(package.context, prompt_version)
    schema = package.schema
    prediction_schema = prediction_json_schema(package.domain_codes)
    return RunConfiguration(
        provider,
        model,
        str(package.manifest["snapshot_sha256"]),
        str(schema["version"]),
        str(schema["content_sha256"]),
        prompt_version,
        prompt_hash(system),
        generation,
        exclusion_record_count,
        exclusion_set_sha256,
        (
            provider_prediction_schema_hash(provider, prediction_schema)
            if provider == "openai"
            else None
        ),
    ), system


def initialise_run(output: Path, configuration: RunConfiguration, *, resume: bool) -> None:
    output.mkdir(parents=True, exist_ok=True)
    metadata_path = output / "run_metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("run_identity") != configuration.identity():
            raise ValueError("existing run directory has an incompatible run configuration")
        if not resume:
            raise FileExistsError(
                "run directory already exists; enable resume or choose another directory"
            )
        return
    metadata = {
        "format_version": 1,
        "run_identity": configuration.identity(),
        "configuration": asdict(configuration),
        "started_at": datetime.now(UTC).isoformat(),
        "software_git_revision": _git_revision(),
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_checkpoints(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    results: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            result = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"malformed checkpoint line {number}") from error
        if not isinstance(result, dict) or not isinstance(result.get("record_id"), str):
            raise ValueError(f"invalid checkpoint line {number}")
        results[result["record_id"]] = result
    return results


def _safe_message(error: Exception) -> str:
    if isinstance(error, (InvalidPrediction, ProviderError)):
        message = str(error)
        for variable in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            secret = os.environ.get(variable)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message
    return "provider request failed"


def _sum_usage(values: list[TokenUsage]) -> TokenUsage:
    def total(field: str) -> int | None:
        present = [getattr(value, field) for value in values if getattr(value, field) is not None]
        return sum(present) if present else None

    return TokenUsage(
        total("input_tokens"),
        total("cached_input_tokens"),
        total("output_tokens"),
        total("reasoning_tokens"),
        total("total_tokens"),
    )


def evaluate(
    package: EvaluationPackage,
    records: tuple[dict[str, Any], ...],
    provider: ModelProvider,
    model: str,
    output: Path,
    generation: dict[str, Any],
    *,
    resume: bool = True,
    max_retries: int = 2,
    retry_delay: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    exclusion_record_count: int = 0,
    exclusion_set_sha256: str | None = None,
) -> list[dict[str, Any]]:
    configuration, system_prompt = make_configuration(
        package,
        provider.name,
        model,
        generation,
        prompt_version,
        exclusion_record_count,
        exclusion_set_sha256,
    )
    initialise_run(output, configuration, resume=resume)
    checkpoint_path = output / "predictions.jsonl"
    checkpoints = load_checkpoints(checkpoint_path)
    schema = prediction_json_schema(package.domain_codes)
    with checkpoint_path.open("a", encoding="utf-8", newline="\n") as destination:
        for record in records:
            record_id = str(record["record_id"])
            previous = checkpoints.get(record_id)
            if previous is not None and previous.get("status") == "success":
                continue
            started = time.perf_counter()
            response = None
            error: Exception | None = None
            retries = 0
            attempt_usage: list[TokenUsage] = []
            for attempt in range(max_retries + 1):
                try:
                    response = provider.generate(
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=build_user_prompt(record),
                        schema=schema,
                        generation=generation,
                    )
                    attempt_usage.append(response.usage)
                    error = None
                    break
                except Exception as caught:
                    error = caught
                    if isinstance(caught, ProviderError):
                        attempt_usage.append(caught.usage)
                    retryable = isinstance(caught, ProviderError) and caught.retryable
                    if attempt < max_retries and retryable:
                        retries += 1
                        sleeper(retry_delay * (attempt + 1))
                    else:
                        break
            latency = round((time.perf_counter() - started) * 1000)
            if response is None:
                failure_usage = _sum_usage(attempt_usage)
                failure_model = error.returned_model if isinstance(error, ProviderError) else None
                failure_diagnostics = error.diagnostics if isinstance(error, ProviderError) else {}
                result = RequestResult(
                    record_id,
                    "api_failure",
                    None,
                    failure_usage,
                    latency,
                    retries,
                    failure_model,
                    type(error).__name__ if error else "ProviderError",
                    _safe_message(error or ProviderError("provider request failed")),
                    failure_diagnostics,
                )
            else:
                try:
                    prediction = validate_prediction(response.raw_prediction, package.domain_codes)
                    result = RequestResult(
                        record_id,
                        "success",
                        prediction,
                        _sum_usage(attempt_usage),
                        latency,
                        retries,
                        response.returned_model,
                        diagnostics=response.diagnostics,
                    )
                except InvalidPrediction as invalid:
                    diagnostics = dict(response.diagnostics)
                    diagnostics["schema_validation_error"] = str(invalid)
                    result = RequestResult(
                        record_id,
                        "invalid",
                        None,
                        _sum_usage(attempt_usage),
                        latency,
                        retries,
                        response.returned_model,
                        type(invalid).__name__,
                        str(invalid),
                        diagnostics,
                    )
            value = result.to_dict()
            destination.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            destination.flush()
            os.fsync(destination.fileno())
            checkpoints[record_id] = value
    return [
        checkpoints[str(record["record_id"])]
        for record in records
        if str(record["record_id"]) in checkpoints
    ]
