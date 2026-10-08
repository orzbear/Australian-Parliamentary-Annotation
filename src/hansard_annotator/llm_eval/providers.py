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
        if not isinstance(text, str):
            raise ProviderError("provider response contains no structured output")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = text
        usage = _dict(data.get("usage"))
        input_details = _dict(usage.get("input_tokens_details"))
        output_details = _dict(usage.get("output_tokens_details"))
        return ProviderResponse(
            raw,
            TokenUsage(
                _int(usage.get("input_tokens")),
                _int(input_details.get("cached_tokens")),
                _int(usage.get("output_tokens")),
                _int(output_details.get("reasoning_tokens")),
                _int(usage.get("total_tokens")),
            ),
            str(data["model"]) if data.get("model") else None,
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
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("provider response contains no structured output") from None
        try:
            raw = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            raw = text
        usage = _dict(data.get("usageMetadata"))
        return ProviderResponse(
            raw,
            TokenUsage(
                _int(usage.get("promptTokenCount")),
                _int(usage.get("cachedContentTokenCount")),
                _int(usage.get("candidatesTokenCount")),
                _int(usage.get("thoughtsTokenCount")),
                _int(usage.get("totalTokenCount")),
            ),
            str(data["modelVersion"]) if data.get("modelVersion") else None,
        )


def provider_for(name: str) -> ModelProvider:
    if name == "openai":
        return OpenAIProvider()
    if name == "gemini":
        return GeminiProvider()
    raise ValueError(f"unsupported provider: {name}")
