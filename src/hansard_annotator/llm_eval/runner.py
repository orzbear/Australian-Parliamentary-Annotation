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
    PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
    prediction_json_schema,
    prompt_hash,
)
from hansard_annotator.llm_eval.providers import ModelProvider, ProviderError
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

    def identity(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def make_configuration(
    package: EvaluationPackage, provider: str, model: str, generation: dict[str, Any]
) -> tuple[RunConfiguration, str]:
    system = build_system_prompt(package.context)
    schema = package.schema
    return RunConfiguration(
        provider,
        model,
        str(package.manifest["snapshot_sha256"]),
        str(schema["version"]),
        str(schema["content_sha256"]),
        PROMPT_VERSION,
        prompt_hash(system),
        generation,
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
) -> list[dict[str, Any]]:
    configuration, system_prompt = make_configuration(package, provider.name, model, generation)
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
            for attempt in range(max_retries + 1):
                try:
                    response = provider.generate(
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=build_user_prompt(record),
                        schema=schema,
                        generation=generation,
                    )
                    error = None
                    break
                except Exception as caught:
                    error = caught
                    if attempt < max_retries:
                        retries += 1
                        sleeper(retry_delay * (attempt + 1))
            latency = round((time.perf_counter() - started) * 1000)
            if response is None:
                result = RequestResult(
                    record_id,
                    "api_failure",
                    None,
                    TokenUsage(),
                    latency,
                    retries,
                    None,
                    type(error).__name__ if error else "ProviderError",
                    _safe_message(error or ProviderError("provider request failed")),
                )
            else:
                try:
                    prediction = validate_prediction(response.raw_prediction, package.domain_codes)
                    result = RequestResult(
                        record_id,
                        "success",
                        prediction,
                        response.usage,
                        latency,
                        retries,
                        response.returned_model,
                    )
                except InvalidPrediction as invalid:
                    result = RequestResult(
                        record_id,
                        "invalid",
                        None,
                        response.usage,
                        latency,
                        retries,
                        response.returned_model,
                        type(invalid).__name__,
                        str(invalid),
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
