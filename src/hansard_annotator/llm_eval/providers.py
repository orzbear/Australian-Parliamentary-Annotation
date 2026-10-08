"""Provider adapters with injectable transports and sanitised failures."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hansard_annotator.llm_eval.models import ProviderResponse, TokenUsage

JsonTransport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class ProviderError(RuntimeError):
    """A safe provider error that never includes credentials or response bodies."""

    def __init__(
        self,
        message: str,
        *,
        usage: TokenUsage | None = None,
        returned_model: str | None = None,
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage or TokenUsage()
        self.returned_model = returned_model
        self.diagnostics = diagnostics or {}


class ModelProvider(Protocol):
    name: str

    def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        generation: dict[str, Any],
    ) -> ProviderResponse: ...


def _http_json(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ProviderError(f"provider HTTP error {error.code}") from None
    except (URLError, TimeoutError):
        raise ProviderError("provider network request failed") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderError("provider returned malformed JSON") from None
    if not isinstance(value, dict):
        raise ProviderError("provider returned an unexpected response")
    return value


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    return type(value).__name__


def _bounded(value: object, limit: int = 2000) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = repr(value)
    for variable in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        secret = os.environ.get(variable)
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]")
    return rendered[:limit]


def _parse_structured_text(
    text: object, *, finish_reason: object
) -> tuple[object, dict[str, object]]:
    content_exists = isinstance(text, str) and bool(text)
    parsed = False
    raw: object = text
    if isinstance(text, str):
        try:
            raw = json.loads(text)
            parsed = True
        except json.JSONDecodeError:
            pass
    diagnostics: dict[str, object] = {
        "parsed_top_level_json_type": _json_type(raw),
        "json_parse_succeeded": parsed,
        "response_content_exists": content_exists,
        "response_text_length": len(text) if isinstance(text, str) else 0,
        "provider_finish_reason": str(finish_reason) if finish_reason is not None else None,
        "structured_response_preview": _bounded(raw),
        "structured_response_preview_truncated": len(_bounded(raw, 10_000_000)) > 2000,
    }
    return raw, diagnostics


@dataclass
class OpenAIProvider:
    transport: JsonTransport = _http_json
    timeout: float = 120.0
    name: str = "openai"

    def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        generation: dict[str, Any],
    ) -> ProviderResponse:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set")
        payload: dict[str, Any] = {
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "policy_annotation",
                    "strict": True,
                    "schema": schema,
                }
            },
            "temperature": generation.get("temperature", 0.0),
            "max_output_tokens": generation.get("max_output_tokens", 1000),
        }
        if generation.get("reasoning_effort") is not None:
            payload["reasoning"] = {"effort": generation["reasoning_effort"]}
        data = self.transport(
            "https://api.openai.com/v1/responses",
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            payload,
            self.timeout,
        )
        text = data.get("output_text")
        if not isinstance(text, str):
            output = data.get("output")
            if isinstance(output, list):
                text = next(
                    (
                        part.get("text")
                        for item in output
                        if isinstance(item, dict)
                        for part in item.get("content", [])
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ),
                    None,
                )
        usage = _dict(data.get("usage"))
        input_details = _dict(usage.get("input_tokens_details"))
        output_details = _dict(usage.get("output_tokens_details"))
        token_usage = TokenUsage(
            _int(usage.get("input_tokens")),
            _int(input_details.get("cached_tokens")),
            _int(usage.get("output_tokens")),
            _int(output_details.get("reasoning_tokens")),
            _int(usage.get("total_tokens")),
        )
        returned_model = str(data["model"]) if data.get("model") else None
        raw, diagnostics = _parse_structured_text(text, finish_reason=data.get("status"))
        if not isinstance(text, str):
            raise ProviderError(
                "provider response contains no structured output",
                usage=token_usage,
                returned_model=returned_model,
                diagnostics=diagnostics,
            )
        return ProviderResponse(
            raw,
            token_usage,
            returned_model,
            diagnostics,
        )


@dataclass
class GeminiProvider:
    transport: JsonTransport = _http_json
    timeout: float = 120.0
    name: str = "gemini"

    def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        generation: dict[str, Any],
    ) -> ProviderResponse:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise ProviderError("GEMINI_API_KEY or GOOGLE_API_KEY is not set")
        config = {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "temperature": generation.get("temperature", 0.0),
            "maxOutputTokens": generation.get("max_output_tokens", 1000),
        }
        if generation.get("thinking_budget") is not None:
            config["thinkingConfig"] = {"thinkingBudget": generation["thinking_budget"]}
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": config,
        }
        data = self.transport(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": key, "Content-Type": "application/json"},
            payload,
            self.timeout,
        )
        usage = _dict(data.get("usageMetadata"))
        token_usage = TokenUsage(
            _int(usage.get("promptTokenCount")),
            _int(usage.get("cachedContentTokenCount")),
            _int(usage.get("candidatesTokenCount")),
            _int(usage.get("thoughtsTokenCount")),
            _int(usage.get("totalTokenCount")),
        )
        returned_model = str(data["modelVersion"]) if data.get("modelVersion") else None
        candidates = data.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        candidate_dict = _dict(candidate)
        prompt_feedback = _dict(data.get("promptFeedback"))
        finish_reason = candidate_dict.get("finishReason") or prompt_feedback.get("blockReason")
        content = _dict(candidate_dict.get("content"))
        parts = content.get("parts")
        first_part = parts[0] if isinstance(parts, list) and parts else {}
        text = _dict(first_part).get("text")
        raw, diagnostics = _parse_structured_text(text, finish_reason=finish_reason)
        diagnostics["native_structured_output_requested"] = True
        diagnostics["response_mime_type"] = "application/json"
        if not isinstance(text, str):
            raise ProviderError(
                "provider response contains no structured output",
                usage=token_usage,
                returned_model=returned_model,
                diagnostics=diagnostics,
            )
        return ProviderResponse(
            raw,
            token_usage,
            returned_model,
            diagnostics,
        )


def provider_for(name: str) -> ModelProvider:
    if name == "openai":
        return OpenAIProvider()
    if name == "gemini":
        return GeminiProvider()
    raise ValueError(f"unsupported provider: {name}")
