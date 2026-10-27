"""Offline Phase 4B prompt audit, cost simulation, and Gemini Batch construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hansard_annotator.llm_eval.hostility import validate_prediction_with_diagnostics
from hansard_annotator.llm_eval.prompt import build_system_prompt, build_user_prompt
from hansard_annotator.llm_eval.validation import InvalidPrediction, validate_prediction

FROZEN_MODEL = "gemini-2.5-flash"
FROZEN_PROMPT_VERSION = "phase4a-policy-v2"
FROZEN_PROMPT_SHA256 = "a41027b001f10b61074c196996606c86b5dac4c0788ae185a05ff68fcf3b7cc6"
FROZEN_CANONICAL_SCHEMA_SHA256 = "9c94bab9cd2976559c79b1c31991aef4138a2071a5d3b3d6186a26448f117126"
APPROVED_PRODUCTION_MODEL = "gemini-3.1-flash-lite"
APPROVED_PRODUCTION_GENERATION = {"thinking_level": "minimal"}
PREDICTION_FIELDS = {
    "is_non_policy",
    "primary_australian_domain",
    "secondary_australian_domains",
    "reasoning",
}
FROZEN_HOSTILITY_PROMPT_VERSION = "hostility-prompt-v2"
FROZEN_HOSTILITY_PROMPT_SHA256 = (
    "185f5162348ae3b3d735ea43247da18cf6b3c1bb12f8a72cad57e38ce31cb1a8"
)
FROZEN_HOSTILITY_CANONICAL_SCHEMA_SHA256 = (
    "959a51a987e0777cb72dc7ad12cd5b896429bef400b79f386764a20ddc3d5669"
)
FROZEN_HOSTILITY_CONTRACT_SHA256 = (
    "dc716b49e0759ba7063973038b234f6696e778c4d90ee4a405d9600a2435b11d"
)
HOSTILITY_PREDICTION_FIELDS = {
    "hostility_present",
    "target",
    "evidence_spans",
    "reasoning",
}


def canonical_hash(value: object) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptAudit:
    system_characters: int
    average_user_characters: float
    average_input_tokens: float
    estimated_invariant_tokens: int
    estimated_variable_tokens: float
    theoretical_cache_share: float
    method: str

    def to_dict(self) -> dict[str, object]:
        return {
            "system_characters": self.system_characters,
            "average_user_characters": self.average_user_characters,
            "average_input_tokens": self.average_input_tokens,
            "estimated_invariant_tokens": self.estimated_invariant_tokens,
            "estimated_variable_tokens_per_record": self.estimated_variable_tokens,
            "theoretical_cache_share": self.theoretical_cache_share,
            "method": self.method,
        }


def audit_prompt_structure(
    context: dict[str, Any],
    records: tuple[dict[str, Any], ...],
    *,
    prompt_version: str,
    measured_input_tokens: int,
) -> PromptAudit:
    """Estimate the split without a paid countTokens request.

    Gemini telemetry supplies the exact aggregate input total, while UTF-8 prompt character
    shares allocate that total between the byte-identical system prefix and variable requests.
    """
    if not records or measured_input_tokens < 0:
        raise ValueError("records and non-negative measured token telemetry are required")
    system = build_system_prompt(context, prompt_version)
    users = [build_user_prompt(record) for record in records]
    average_user_chars = sum(len(value) for value in users) / len(users)
    average_tokens = measured_input_tokens / len(records)
    invariant_share = len(system) / (len(system) + average_user_chars)
    invariant_tokens = round(average_tokens * invariant_share)
    variable_tokens = average_tokens - invariant_tokens
    return PromptAudit(
        len(system),
        average_user_chars,
        average_tokens,
        invariant_tokens,
        variable_tokens,
        invariant_tokens / average_tokens if average_tokens else 0.0,
        "Measured Gemini aggregate tokens allocated by exact assembled-text character share; "
        "an estimate because offline Gemini tokenization is unavailable without an API call.",
    )


def simulate_costs(
    *,
    records: int,
    input_tokens_per_record: float,
    output_tokens_per_record: float,
    cached_share: float,
    input_rate: float,
    cached_rate: float,
    output_rate: float,
    explicit_cache_tokens: int = 0,
    ttl_hours: float = 0.0,
    storage_rate: float = 0.0,
) -> dict[str, float | int]:
    if records < 0 or not 0 <= cached_share <= 1 or ttl_hours < 0:
        raise ValueError("invalid cost simulation inputs")
    total_input = input_tokens_per_record * records
    cached = total_input * cached_share
    uncached = total_input - cached
    processing = (
        uncached * input_rate
        + cached * cached_rate
        + output_tokens_per_record * records * output_rate
    ) / 1_000_000
    storage = explicit_cache_tokens * ttl_hours * storage_rate / 1_000_000
    return {
        "records": records,
        "cached_share": cached_share,
        "uncached_input_tokens": round(uncached),
        "cached_input_tokens": round(cached),
        "output_tokens": round(output_tokens_per_record * records),
        "token_processing_cost_usd": processing,
        "explicit_cache_storage_cost_usd": storage,
        "estimated_total_cost_usd": processing + storage,
    }


def project_production_cost(
    *,
    token_processing_cost_usd: float,
    valid_records: int,
    target_records: int,
    shared_cache_storage_cost_usd: float,
) -> dict[str, float]:
    """Mechanically project variable processing and add shared cache storage once."""
    if valid_records <= 0 or target_records < 0 or shared_cache_storage_cost_usd < 0:
        raise ValueError("invalid production cost projection inputs")
    variable = token_processing_cost_usd / valid_records * target_records
    return {
        "projected_variable_token_processing_usd": variable,
        "projected_shared_cache_storage_usd": shared_cache_storage_cost_usd,
        "projected_total_usd": variable + shared_cache_storage_cost_usd,
    }


def gemini_batch_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Derive Gemini Batch's supported Schema form from the canonical prediction contract."""
    properties = schema["properties"]
    if set(properties) == {"hostility_present", "target", "evidence_spans", "reasoning"}:
        evidence = properties["evidence_spans"]["items"]
        return {
            "type": "OBJECT",
            "properties": {
                "hostility_present": {
                    "type": "STRING",
                    "enum": properties["hostility_present"]["enum"],
                },
                "target": {"type": "STRING", "enum": properties["target"]["enum"]},
                "evidence_spans": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {"text": {"type": "STRING"}},
                        "required": evidence["required"],
                        "propertyOrdering": evidence["required"],
                    },
                },
                "reasoning": {"type": "STRING"},
            },
            "required": schema["required"],
            "propertyOrdering": schema["required"],
        }
    primary = properties["primary_australian_domain"]
    secondary = properties["secondary_australian_domains"]
    return {
        "type": "OBJECT",
        "properties": {
            "is_non_policy": {"type": "BOOLEAN"},
            "primary_australian_domain": {
                "type": "STRING",
                "nullable": True,
                "enum": [value for value in primary["enum"] if value is not None],
            },
            "secondary_australian_domains": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                    "enum": secondary["items"]["enum"],
                },
                "maxItems": secondary["maxItems"],
            },
            "reasoning": {"type": "STRING"},
        },
        "required": list(schema["required"]),
        "propertyOrdering": list(schema["required"]),
    }


def gemini_generation_config(
    schema: dict[str, Any],
    *,
    max_output_tokens: int = 1000,
    generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested = {"thinking_budget": 0} if generation is None else generation
    if requested == {"thinking_budget": 0}:
        thinking: dict[str, Any] = {"thinkingBudget": 0}
    elif requested == APPROVED_PRODUCTION_GENERATION:
        thinking = {"thinkingLevel": "minimal"}
    else:
        raise ValueError("unsupported Gemini Batch thinking configuration")
    return {
        "responseMimeType": "application/json",
        "responseSchema": gemini_batch_schema(schema),
        "temperature": 0.0,
        "maxOutputTokens": max_output_tokens,
        "thinkingConfig": thinking,
    }


def validate_production_plan(plan: dict[str, Any]) -> None:
    """Fail closed before any paid Batch submission of an approved frozen configuration."""
    provenance = plan.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("Batch plan has no provenance")
    model = provenance.get("model")
    prompt_version = provenance.get("prompt_version")
    if prompt_version == FROZEN_HOSTILITY_PROMPT_VERSION:
        supported_generation: dict[str, dict[str, Any]] = {
            FROZEN_MODEL: {"thinking_budget": 0},
        }
        expected = {
            "prompt_version": FROZEN_HOSTILITY_PROMPT_VERSION,
            "assembled_prompt_sha256": FROZEN_HOSTILITY_PROMPT_SHA256,
            "canonical_schema_sha256": FROZEN_HOSTILITY_CANONICAL_SCHEMA_SHA256,
        }
        if "prediction_contract_sha256" in provenance:
            expected["prediction_contract_sha256"] = FROZEN_HOSTILITY_CONTRACT_SHA256
        expected_fields = HOSTILITY_PREDICTION_FIELDS
    else:
        supported_generation = {
            FROZEN_MODEL: {"thinking_budget": 0},
            APPROVED_PRODUCTION_MODEL: APPROVED_PRODUCTION_GENERATION,
        }
        expected = {
            "prompt_version": FROZEN_PROMPT_VERSION,
            "assembled_prompt_sha256": FROZEN_PROMPT_SHA256,
            "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
        }
        expected_fields = PREDICTION_FIELDS
    if not isinstance(model, str) or model not in supported_generation:
        raise ValueError("paid Batch submission blocked: unexpected model")
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise ValueError(f"paid Batch submission blocked: unexpected {key}")
    cache_identifier = provenance.get("cached_content")
    if (
        not isinstance(cache_identifier, str)
        or not cache_identifier.startswith("cachedContents/")
        or "REPLACE" in cache_identifier
    ):
        raise ValueError("paid Batch submission blocked: invalid or placeholder cache identifier")
    requests = (
        plan.get("request", {})
        .get("batch", {})
        .get("input_config", {})
        .get("requests", {})
        .get("requests", [])
    )
    if not isinstance(requests, list) or not requests:
        raise ValueError("paid Batch submission blocked: plan has no requests")
    expected_generation = supported_generation[model]
    provenance_generation = {
        key: provenance[key]
        for key in ("thinking_budget", "thinking_level")
        if key in provenance
    }
    if provenance_generation != expected_generation:
        raise ValueError("paid Batch submission blocked: unexpected thinking configuration")
    expected_thinking = (
        {"thinkingBudget": 0}
        if "thinking_budget" in expected_generation
        else {"thinkingLevel": "minimal"}
    )
    for item in requests:
        config = (
            item.get("request", {}).get("generationConfig", {}) if isinstance(item, dict) else {}
        )
        schema = config.get("responseSchema") if isinstance(config, dict) else None
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict) or set(properties) != expected_fields:
            raise ValueError("paid Batch submission blocked: unexpected prediction field set")
        if provenance.get("provider_schema_sha256") != canonical_hash(schema):
            raise ValueError("paid Batch submission blocked: provider schema hash mismatch")
        if config.get("thinkingConfig") != expected_thinking:
            raise ValueError("paid Batch submission blocked: unexpected thinking configuration")


def build_cache_create_request(
    *, model: str, system_prompt: str, ttl_seconds: int, display_name: str
) -> dict[str, Any]:
    if ttl_seconds < 1:
        raise ValueError("cache TTL must be positive")
    return {
        "model": f"models/{model}",
        "displayName": display_name,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "ttl": f"{ttl_seconds}s",
    }


def build_batch_create_request(
    *,
    model: str,
    system_prompt: str,
    records: tuple[dict[str, Any], ...],
    schema: dict[str, Any],
    display_name: str,
    cached_content: str | None = None,
    generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    for record in records:
        request: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": build_user_prompt(record)}]}],
            "generationConfig": gemini_generation_config(schema, generation=generation),
        }
        if cached_content is None:
            request["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        else:
            request["cachedContent"] = cached_content
        requests.append(
            {
                "request": request,
                "metadata": {"key": str(record["record_id"])},
            }
        )
    return {
        "batch": {
            "display_name": display_name,
            "input_config": {"requests": {"requests": requests}},
        }
    }


def validate_batch_mapping(
    requested_record_ids: tuple[str, ...], responses: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    expected = set(requested_record_ids)
    mapped: dict[str, dict[str, Any]] = {}
    for response in responses:
        metadata = response.get("metadata")
        key = metadata.get("key") if isinstance(metadata, dict) else None
        if not isinstance(key, str) or key not in expected or key in mapped:
            raise ValueError("batch response contains a missing, unknown, or duplicate record ID")
        mapped[key] = response
    if set(mapped) != expected:
        raise ValueError("batch response is incomplete")
    return mapped


def production_record_from_turn(row: dict[str, Any]) -> dict[str, Any]:
    """Map a Phase 1 speaker turn to the provider-independent prompt record shape."""
    turn_key = str(row["turn_key"])
    return {
        "record_id": f"turn:{turn_key}",
        "speech": {
            "turn_key": turn_key,
            "date": str(row.get("date") or ""),
            "chamber": row.get("chamber"),
            "speaker_name": row.get("speaker_name_raw"),
            "major_heading": row.get("major_heading_original"),
            "minor_heading": row.get("minor_heading_original"),
            "business_type": row.get("business_type"),
            "text": row.get("text_clean") or row.get("text_raw") or "",
        },
    }


TERMINAL_BATCH_STATES = {
    "BATCH_STATE_SUCCEEDED",
    "BATCH_STATE_FAILED",
    "BATCH_STATE_CANCELLED",
    "BATCH_STATE_EXPIRED",
}


def submission_batch_name(receipt: dict[str, Any]) -> str:
    response = receipt.get("response")
    if not isinstance(response, dict):
        raise ValueError("submission receipt has no response object")
    name = response.get("name")
    if not isinstance(name, str) or not name.startswith("batches/"):
        raise ValueError("submission receipt has no valid Gemini batch name")
    return name


def batch_resource(value: dict[str, Any]) -> dict[str, Any]:
    """Unwrap either a batch resource or its long-running-operation envelope."""
    response = value.get("response")
    if isinstance(response, dict) and isinstance(response.get("state"), str):
        return response
    metadata = value.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("state"), str):
        merged = dict(metadata)
        if isinstance(response, dict) and "output" in response:
            merged["output"] = response["output"]
        return merged
    return value


def parse_batch_status(value: dict[str, Any]) -> dict[str, object]:
    batch = batch_resource(value)
    stats = batch.get("batchStats") if isinstance(batch.get("batchStats"), dict) else {}

    def count(key: str) -> int:
        raw = stats.get(key) if isinstance(stats, dict) else None
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    output = batch.get("output") if isinstance(batch.get("output"), dict) else {}
    inline = output.get("inlinedResponses") if isinstance(output, dict) else None
    output_reference: object = output.get("responsesFile") if isinstance(output, dict) else None
    if output_reference is None and isinstance(inline, dict):
        values = inline.get("inlinedResponses")
        output_reference = f"inline:{len(values)}" if isinstance(values, list) else "inline"
    state = str(batch.get("state") or "BATCH_STATE_UNSPECIFIED")
    return {
        "batch_name": batch.get("name"),
        "display_name": batch.get("displayName"),
        "model": batch.get("model"),
        "state": state,
        "terminal": state in TERMINAL_BATCH_STATES,
        "successful_terminal": state == "BATCH_STATE_SUCCEEDED",
        "creation_time": batch.get("createTime"),
        "update_time": batch.get("updateTime"),
        "completion_time": batch.get("endTime"),
        "total_request_count": count("requestCount"),
        "pending_count": count("pendingRequestCount"),
        "successful_count": count("successfulRequestCount"),
        "failed_count": count("failedRequestCount"),
        "cancelled_count": count("cancelledRequestCount"),
        "output_reference": output_reference,
        "error": batch.get("error") or value.get("error"),
    }


def _gemini_text(response: dict[str, Any]) -> object:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    candidate = candidates[0]
    content = candidate.get("content") if isinstance(candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return None
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
    return None


def collect_inline_batch(
    *, batch_value: dict[str, Any], plan: dict[str, Any], domain_codes: tuple[str, ...],
    prediction_task: str = "policy", records_by_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    status = parse_batch_status(batch_value)
    if not status["terminal"]:
        raise ValueError(f"batch is not terminal: {status['state']}")
    if not status["successful_terminal"]:
        raise ValueError(f"batch did not succeed: {status['state']}; error={status['error']}")
    planned = plan["request"]["batch"]["input_config"]["requests"]["requests"]
    planned_keys = [str(item["metadata"]["key"]) for item in planned]
    planned_indexes = {key: index for index, key in enumerate(planned_keys)}
    batch = batch_resource(batch_value)
    output = batch.get("output")
    inline = output.get("inlinedResponses") if isinstance(output, dict) else None
    responses = inline.get("inlinedResponses") if isinstance(inline, dict) else None
    if not isinstance(responses, list):
        raise ValueError("successful inline batch contains no inline responses")
    seen: set[str] = set()
    duplicate_count = 0
    results: list[dict[str, Any]] = []
    for index, item in enumerate(responses):
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        key = metadata.get("key") if isinstance(metadata, dict) else None
        if not isinstance(key, str) or key not in set(planned_keys):
            results.append(
                {
                    "record_id": key,
                    "request_index": None,
                    "response_index": index,
                    "status": "mapping_error",
                    "provider_error": "missing or unknown metadata key",
                }
            )
            continue
        if key in seen:
            duplicate_count += 1
            results.append(
                {
                    "record_id": key,
                    "request_index": planned_indexes[key],
                    "response_index": index,
                    "status": "mapping_error",
                    "provider_error": "duplicate metadata key",
                }
            )
            continue
        seen.add(key)
        error = item.get("error")
        response = item.get("response")
        if isinstance(error, dict) or not isinstance(response, dict):
            results.append(
                {
                    "record_id": key,
                    "request_index": planned_indexes[key],
                    "response_index": index,
                    "status": "provider_failure",
                    "provider_error": error or "missing response",
                }
            )
            continue
        usage_value = response.get("usageMetadata")
        usage: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
        raw_text = _gemini_text(response)
        prediction: object = None
        validation_error: str | None = None
        try:
            parsed = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
            if prediction_task == "hostility":
                record = (records_by_id or {}).get(key)
                if record is None:
                    raise InvalidPrediction("hostility validation has no canonical speech record")
                validated, diagnostics = validate_prediction_with_diagnostics(
                    parsed, str(record["speech"]["text"]), "hostility-prompt-v2"
                )
                if diagnostics["evidence_valid"] is not True:
                    raise InvalidPrediction(
                        "hostility evidence must resolve uniquely to canonical text"
                    )
                validated.to_dict()
                prediction = parsed
            else:
                prediction = validate_prediction(parsed, domain_codes).to_dict()
            result_status = "success"
        except (json.JSONDecodeError, InvalidPrediction, TypeError) as error_value:
            result_status = "invalid"
            validation_error = str(error_value)
        results.append(
            {
                "record_id": key,
                "turn_key": key.removeprefix("turn:"),
                "request_index": planned_indexes[key],
                "response_index": index,
                "status": result_status,
                "prediction": prediction,
                "validation_error": validation_error,
                "provider_error": None,
                "usage": {
                    "input_tokens": _coerce_int(usage.get("promptTokenCount")),
                    "cached_input_tokens": _coerce_int(usage.get("cachedContentTokenCount")),
                    "output_tokens": _coerce_int(usage.get("candidatesTokenCount")),
                    "reasoning_tokens": _coerce_int(usage.get("thoughtsTokenCount")),
                    "total_tokens": _coerce_int(usage.get("totalTokenCount")),
                },
                "returned_model": response.get("modelVersion"),
                "provider_response": response,
            }
        )
    missing = len(set(planned_keys) - seen)
    coverage = {
        "planned_requests": len(planned_keys),
        "provider_request_count": _coerce_int(status["total_request_count"]),
        "collected_responses": len(responses),
        "missing_responses": missing,
        "duplicate_mappings": duplicate_count,
    }
    return results, coverage


def _coerce_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def duration_seconds(start: object, end: object) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return (
            datetime.fromisoformat(end.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        return None
