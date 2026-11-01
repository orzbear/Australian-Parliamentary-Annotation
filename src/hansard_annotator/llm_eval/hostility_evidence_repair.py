"""Evidence-only repair contract for frozen hostility classifications."""

from __future__ import annotations

import json
from typing import Any

from hansard_annotator.llm_eval.failure_categories import FailureCategory
from hansard_annotator.llm_eval.hostility import (
    HostilityValidationError,
    validate_prediction_with_diagnostics,
)
from hansard_annotator.llm_eval.prompt import HOSTILITY_PRODUCTION_PROMPT_VERSION

EVIDENCE_REPAIR_VERSION = "hostility-evidence-repair-v1"
EVIDENCE_REPAIR_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "evidence_spans": {
            "type": "ARRAY",
            "minItems": 1,
            "maxItems": 1,
            "items": {
                "type": "OBJECT",
                "properties": {"text": {"type": "STRING"}},
                "required": ["text"],
                "propertyOrdering": ["text"],
            },
        }
    },
    "required": ["evidence_spans"],
    "propertyOrdering": ["evidence_spans"],
}
EVIDENCE_REPAIR_SYSTEM_PROMPT = """You are repairing evidence for an existing hostility annotation.
Do not reclassify the passage. The supplied hostility_present and target are frozen.
Return only JSON with one field, evidence_spans, containing one short distinctive
verbatim clause or sentence from the canonical speech that supports that frozen
classification. Do not paraphrase, alter punctuation, add explanation, or include
classification fields. Follow the supplied hostility codebook evidence requirements.
"""


def frozen_classification_is_repairable(value: object) -> bool:
    if not isinstance(value, dict) or set(value) not in (
        {"hostility_present", "target"},
        {"hostility_present", "target", "reasoning"},
    ):
        return False
    return (
        value.get("hostility_present") in {"YES", "UNSURE"}
        and value.get("target")
        in {
            "OPPOSING_PARTISAN_TARGET",
            "OTHER_POLITICAL_TARGET",
            "NON_POLITICAL_TARGET",
            "UNSURE",
        }
        and ("reasoning" not in value or isinstance(value.get("reasoning"), str))
    )


def build_evidence_repair_user_prompt(
    *,
    canonical_text: str,
    frozen_classification: dict[str, Any],
    original_evidence: object,
    codebook_context: object,
) -> str:
    if not frozen_classification_is_repairable(frozen_classification):
        raise ValueError("frozen hostility classification is not eligible for evidence repair")
    if not canonical_text.strip():
        raise ValueError("canonical speech text is empty")
    evidence = original_evidence if isinstance(original_evidence, list) else []
    return (
        "FROZEN CLASSIFICATION (do not change):\n"
        + json.dumps(frozen_classification, ensure_ascii=False, sort_keys=True)
        + "\n\nORIGINAL UNRESOLVED EVIDENCE:\n"
        + json.dumps(evidence, ensure_ascii=False)
        + "\n\nFROZEN CODEBOOK CONTEXT (reference only; do not reclassify):\n"
        + json.dumps(codebook_context, ensure_ascii=False, sort_keys=True)
        + "\n\nCANONICAL SPEECH (quote evidence exactly from this text):\n"
        + canonical_text
    )


def validate_evidence_repair(
    raw: object,
    *,
    canonical_text: str,
    frozen_classification: dict[str, Any],
    prompt_version: str = HOSTILITY_PRODUCTION_PROMPT_VERSION,
) -> tuple[dict[str, Any], dict[str, object]]:
    if not frozen_classification_is_repairable(frozen_classification):
        raise ValueError("frozen hostility classification is not eligible for evidence repair")
    if not isinstance(raw, dict) or set(raw) != {"evidence_spans"}:
        raise ValueError("evidence repair output must contain only evidence_spans")
    evidence = raw["evidence_spans"]
    if not isinstance(evidence, list) or len(evidence) != 1:
        raise HostilityValidationError(
            "evidence repair must return exactly one evidence passage",
            FailureCategory.EVIDENCE_REPAIR_VALIDATION_FAILURE,
        )
    combined = {**frozen_classification, "evidence_spans": evidence}
    try:
        prediction, diagnostics = validate_prediction_with_diagnostics(
            combined, canonical_text, prompt_version
        )
    except HostilityValidationError as error:
        raise HostilityValidationError(
            f"evidence repair did not satisfy the frozen hostility evidence contract: {error}",
            FailureCategory.EVIDENCE_REPAIR_VALIDATION_FAILURE,
        ) from error
    if diagnostics.get("evidence_valid") is not True:
        raise HostilityValidationError(
            "repaired evidence did not uniquely resolve to canonical text",
            FailureCategory.EVIDENCE_REPAIR_VALIDATION_FAILURE,
        )
    resolved = prediction.to_dict()
    if any(resolved.get(key) != frozen_classification.get(key) for key in frozen_classification):
        raise ValueError("evidence repair changed frozen hostility classification")
    return resolved, diagnostics
