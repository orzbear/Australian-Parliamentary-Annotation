"""Versioned pricing metadata and token/cost aggregation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _price(entry: dict[str, Any], key: str, fallback: float = 0.0) -> float:
    value = entry.get(key, fallback)
    if not isinstance(value, int | float):
        raise ValueError(f"pricing field {key} must be numeric")
    return float(value)


def load_pricing(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("format_version") != 1
        or not isinstance(value.get("models"), dict)
    ):
        raise ValueError("pricing file must contain format_version 1 and a models mapping")
    return value


def aggregate_costs(
    results: list[dict[str, Any]], provider: str, model: str, pricing: dict[str, Any]
) -> dict[str, Any]:
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    tokens = {
        field: sum(int(result.get("usage", {}).get(field) or 0) for result in results)
        for field in fields
    }
    successful = sum(result.get("status") == "success" for result in results)
    attempts = len(results)
    entry = pricing["models"].get(f"{provider}:{model}")
    estimated: float | None = None
    billable_output_tokens = tokens["output_tokens"]
    if isinstance(entry, dict):
        if entry.get("reasoning_billed_as_output") is True:
            billable_output_tokens += tokens["reasoning_tokens"]
        input_uncached = max(0, tokens["input_tokens"] - tokens["cached_input_tokens"])
        estimated = (
            input_uncached * _price(entry, "input_per_million_usd")
            + tokens["cached_input_tokens"]
            * _price(entry, "cached_input_per_million_usd", _price(entry, "input_per_million_usd"))
            + billable_output_tokens * _price(entry, "output_per_million_usd")
        ) / 1_000_000
    per_attempt = estimated / attempts if estimated is not None and attempts else None
    per_valid = estimated / successful if estimated is not None and successful else None
    return {
        "pricing_format_version": pricing["format_version"],
        "pricing_effective_date": pricing.get("effective_date"),
        "pricing_source": pricing.get("source"),
        "pricing_model_key": f"{provider}:{model}",
        "pricing_found": entry is not None,
        "tokens": tokens,
        "billable_tokens": {
            "uncached_input_tokens": max(0, tokens["input_tokens"] - tokens["cached_input_tokens"]),
            "cached_input_tokens": tokens["cached_input_tokens"],
            "output_tokens": billable_output_tokens,
            "reasoning_tokens_billed_as_output": (
                tokens["reasoning_tokens"]
                if isinstance(entry, dict) and entry.get("reasoning_billed_as_output") is True
                else 0
            ),
        },
        "attempted_requests": attempts,
        "successful_requests": successful,
        "estimated_cost_usd": estimated,
        "cost_per_attempt_usd": per_attempt,
        "cost_per_valid_annotation_usd": per_valid,
        "projected_cost_1k_attempts_usd": per_attempt * 1_000 if per_attempt is not None else None,
        "projected_cost_160k_attempts_usd": per_attempt * 160_000
        if per_attempt is not None
        else None,
        "projected_cost_1k_valid_annotations_usd": per_valid * 1_000
        if per_valid is not None
        else None,
        "projected_cost_160k_valid_annotations_usd": per_valid * 160_000
        if per_valid is not None
        else None,
        "projection_note": (
            "Mechanical linear extrapolations from this run; small samples are not reliable "
            "production forecasts."
        ),
        "total_latency_ms": sum(int(result.get("latency_ms", 0)) for result in results),
        "total_retries": sum(int(result.get("retry_count", 0)) for result in results),
    }
