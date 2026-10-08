"""Typed values shared by the evaluation pipeline and provider adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Status = Literal["success", "invalid", "api_failure"]


@dataclass(frozen=True)
class StructuredPrediction:
    is_non_policy: bool
    primary_australian_domain: str | None
    secondary_australian_domains: tuple[str, ...] = ()
    reasoning: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderResponse:
    raw_prediction: object
    usage: TokenUsage = field(default_factory=TokenUsage)
    returned_model: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestResult:
    record_id: str
    status: Status
    prediction: StructuredPrediction | None
    usage: TokenUsage
    latency_ms: int
    retry_count: int
    returned_model: str | None
    error_type: str | None = None
    error_message: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["prediction"] = self.prediction.to_dict() if self.prediction else None
        result["usage"] = self.usage.to_dict()
        return result
