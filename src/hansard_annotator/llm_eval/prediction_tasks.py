"""Task adapters for immutable prediction storage and human review."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from hansard_annotator.llm_eval.hostility import validate_prediction_with_diagnostics
from hansard_annotator.llm_eval.prompt import HOSTILITY_PRODUCTION_PROMPT_VERSION
from hansard_annotator.llm_eval.validation import validate_prediction as validate_policy

PredictionTask = Literal["policy", "hostility"]
POLICY_SCHEMA = ("australian_policy_annotation", "0.2.0")
HOSTILITY_SCHEMA = ("australian_partisan_hostility", "0.2.0")


@dataclass(frozen=True)
class AdaptedPrediction:
    task: PredictionTask
    schema_slug: str
    schema_version: str
    values: dict[str, object]
    reasoning: str


def adapt_policy_prediction(raw: object, domain_codes: tuple[str, ...]) -> AdaptedPrediction:
    value = validate_policy(raw, domain_codes)
    canonical = value.to_dict()
    reasoning = str(canonical.pop("reasoning"))
    return AdaptedPrediction("policy", *POLICY_SCHEMA, canonical, reasoning)


def adapt_hostility_prediction(
    raw: object, speech_text: str, prompt_version: str = "hostility-prompt-v2"
) -> AdaptedPrediction:
    """Validate provider or canonical artifact output and retain exact offsets."""
    canonical_spans: list[dict[str, Any]] | None = None
    validation_input = raw
    if isinstance(raw, dict) and prompt_version != "hostility-prompt-v1":
        spans = raw.get("evidence_spans")
        if isinstance(spans, list) and spans and all(
            isinstance(span, dict)
            and set(span) == {"start_offset", "end_offset", "text"}
            for span in spans
        ):
            canonical_spans = cast(list[dict[str, Any]], spans)
            for span in canonical_spans:
                start = span["start_offset"]
                end = span["end_offset"]
                evidence = span["text"]
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not isinstance(evidence, str)
                    or start < 0
                    or end <= start
                    or end > len(speech_text)
                    or speech_text[start:end] != evidence
                ):
                    raise ValueError(
                        "canonical evidence offsets or text do not match the speech"
                    )
            validation_input = {
                **raw,
                "evidence_spans": [{"text": span["text"]} for span in canonical_spans],
            }
            if (
                prompt_version == HOSTILITY_PRODUCTION_PROMPT_VERSION
                and validation_input.get("reasoning") == ""
            ):
                validation_input = dict(validation_input)
                validation_input.pop("reasoning")
    value, diagnostics = validate_prediction_with_diagnostics(
        validation_input, speech_text, prompt_version
    )
    if diagnostics["evidence_valid"] is not True:
        raise ValueError("hostility evidence must resolve uniquely to canonical text")
    canonical = value.to_dict()
    reasoning = str(canonical.pop("reasoning"))
    canonical["evidence_spans"] = list(
        cast(tuple[dict[str, object], ...], canonical["evidence_spans"])
    )
    if canonical_spans is not None and canonical["evidence_spans"] != canonical_spans:
        raise ValueError("canonical evidence offsets or text do not match the speech")
    return AdaptedPrediction("hostility", *HOSTILITY_SCHEMA, canonical, reasoning)


def human_suggestion(task: PredictionTask, values: Mapping[str, Any]) -> dict[str, object]:
    if task == "policy":
        return {
            "is_non_policy": values.get("is_non_policy"),
            "primary_australian_domain": values.get("primary_australian_domain"),
            "secondary_australian_domains": list(
                values.get("secondary_australian_domains") or []
            ),
        }
    target = values.get("target")
    return {
        "hostility_present": values.get("hostility_present"),
        "target": None if target == "N/A" else target,
        "evidence_span": list(values.get("evidence_spans") or []),
    }


def changed_fields(
    task: PredictionTask, suggested: Mapping[str, Any], human: Mapping[str, Any]
) -> list[str]:
    """Derive comparison metadata; never persist a second source of truth."""
    keys = (
        ("is_non_policy", "primary_australian_domain", "secondary_australian_domains")
        if task == "policy"
        else ("hostility_present", "target", "evidence_span")
    )
    return [key for key in keys if human.get(key) != suggested.get(key)]
