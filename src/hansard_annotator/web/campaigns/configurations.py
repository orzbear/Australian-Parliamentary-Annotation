"""Allowlisted AI annotation campaign configurations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from hansard_annotator.llm_eval.hostility import prediction_json_schema as hostility_schema
from hansard_annotator.llm_eval.hostility_validation import (
    CONTRACT_SHA256 as HOSTILITY_CONTRACT_SHA256,
)
from hansard_annotator.llm_eval.hostility_validation import (
    HELDOUT_SAMPLE_ID,
    HELDOUT_SAMPLE_SHA256,
    LUNA_CONFIRMATION_CONTRACT_SHA256,
    LUNA_CONFIRMATION_PROMPT_SHA256,
    LUNA_CONFIRMATION_SAMPLE_ID,
    LUNA_CONFIRMATION_SAMPLE_SHA256,
)
from hansard_annotator.llm_eval.hostility_validation import (
    MODEL as HOSTILITY_MODEL,
)
from hansard_annotator.llm_eval.hostility_validation import (
    PROMPT_SHA256 as HOSTILITY_PROMPT_SHA256,
)
from hansard_annotator.llm_eval.hostility_validation import (
    PROMPT_VERSION as HOSTILITY_PROMPT_VERSION,
)
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_GENERATION,
    APPROVED_PRODUCTION_MODEL,
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
    canonical_hash,
    gemini_batch_schema,
)
from hansard_annotator.llm_eval.prompt import (
    HOSTILITY_LUNA_PROMPT_VERSION,
)
from hansard_annotator.llm_eval.prompt import prediction_json_schema as policy_schema
from hansard_annotator.llm_eval.providers import provider_prediction_schema_hash

POLICY_CONFIGURATION_ID = "policy-v2"
HOSTILITY_CONFIGURATION_ID = "hostility-v2"
LUNA_CONFIRMATION_CONFIGURATION_ID = "hostility-luna-confirmation-100"
LUNA_PRODUCTION_CONFIGURATION_ID = "hostility-luna-production-v1"
GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID = "hostility-gemini-25-flash-production"
HOSTILITY_SCHEMA_SHA256 = "959a51a987e0777cb72dc7ad12cd5b896429bef400b79f386764a20ddc3d5669"


HOSTILITY_PROVIDER_SCHEMA_SHA256 = canonical_hash(
    gemini_batch_schema(hostility_schema(HOSTILITY_PROMPT_VERSION))
)
LUNA_CONFIRMATION_SCHEMA_SHA256 = HOSTILITY_SCHEMA_SHA256
LUNA_CONFIRMATION_PROVIDER_SCHEMA_SHA256 = provider_prediction_schema_hash(
    "openai", hostility_schema(HOSTILITY_LUNA_PROMPT_VERSION)
)


@dataclass(frozen=True)
class ApprovedAIConfiguration:
    configuration_id: str
    display_label: str
    description: str
    prediction_task: Literal["policy", "hostility"]
    annotation_schema_slug: str
    annotation_schema_version: str
    provider: str
    configured_model: str
    prompt_version: str
    prompt_sha256: str
    canonical_schema_sha256: str
    prediction_contract_sha256: str
    provider_schema_sha256: str
    generation_config: dict[str, Any]
    execution_mode: str
    validation_adapter: str
    record_set_mode: Literal["deterministic_selection", "frozen_sample"]
    record_set_id: str | None = None
    record_set_size: int | None = None
    record_set_sha256: str | None = None

    def persistence_values(self) -> dict[str, Any]:
        return asdict(self)


APPROVED_AI_CONFIGURATIONS = {
    GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID: ApprovedAIConfiguration(
        GEMINI_HOSTILITY_PRODUCTION_CONFIGURATION_ID,
        "Hostility Gemini 2.5 Flash Production",
        "Policy-eligible full-corpus hostility classification using the held-out validated Gemini V2 configuration.",
        "hostility",
        "australian_partisan_hostility",
        "0.2.0",
        "gemini",
        HOSTILITY_MODEL,
        HOSTILITY_PROMPT_VERSION,
        HOSTILITY_PROMPT_SHA256,
        HOSTILITY_SCHEMA_SHA256,
        HOSTILITY_CONTRACT_SHA256,
        HOSTILITY_PROVIDER_SCHEMA_SHA256,
        {"thinking_budget": 0},
        "batch",
        "hostility-gemini-25-flash-v2",
        "deterministic_selection",
    ),
    POLICY_CONFIGURATION_ID: ApprovedAIConfiguration(
        POLICY_CONFIGURATION_ID,
        "Policy classification — Frozen V2",
        "Classifies policy/non-policy and AU01-AU14 domains.",
        "policy",
        "australian_policy_annotation",
        "0.2.0",
        "gemini",
        APPROVED_PRODUCTION_MODEL,
        FROZEN_PROMPT_VERSION,
        FROZEN_PROMPT_SHA256,
        FROZEN_CANONICAL_SCHEMA_SHA256,
        "99cafa6c473293dc076793ee6baeca44b6961d7cb11de8f9174517defc4a0879",
        canonical_hash(
            gemini_batch_schema(
                policy_schema(
                    (*tuple(f"AU{number:02d}" for number in range(1, 15)), "AU_OTHER_REVIEW")
                )
            )
        ),
        dict(APPROVED_PRODUCTION_GENERATION),
        "batch",
        "policy-v2",
        "deterministic_selection",
    ),
    HOSTILITY_CONFIGURATION_ID: ApprovedAIConfiguration(
        HOSTILITY_CONFIGURATION_ID,
        "Targeted hostility — Frozen V2",
        "Classifies targeted hostility, target relationship, and supporting evidence.",
        "hostility",
        "australian_partisan_hostility",
        "0.2.0",
        "gemini",
        HOSTILITY_MODEL,
        HOSTILITY_PROMPT_VERSION,
        HOSTILITY_PROMPT_SHA256,
        HOSTILITY_SCHEMA_SHA256,
        HOSTILITY_CONTRACT_SHA256,
        HOSTILITY_PROVIDER_SCHEMA_SHA256,
        {"thinking_budget": 0},
        "batch",
        "hostility-v2",
        "frozen_sample",
        HELDOUT_SAMPLE_ID,
        100,
        HELDOUT_SAMPLE_SHA256,
    ),
    LUNA_PRODUCTION_CONFIGURATION_ID: ApprovedAIConfiguration(
        LUNA_PRODUCTION_CONFIGURATION_ID,
        "Targeted hostility — GPT-6 Luna production V1",
        "Frozen full-corpus production classification using the Luna V1 prompt.",
        "hostility",
        "australian_partisan_hostility",
        "0.2.0",
        "openai",
        "gpt-6-luna",
        HOSTILITY_LUNA_PROMPT_VERSION,
        LUNA_CONFIRMATION_PROMPT_SHA256,
        LUNA_CONFIRMATION_SCHEMA_SHA256,
        LUNA_CONFIRMATION_CONTRACT_SHA256,
        LUNA_CONFIRMATION_PROVIDER_SCHEMA_SHA256,
        {"max_output_tokens": 1000, "reasoning_effort": "medium"},
        "batch",
        "hostility-luna-production-v1",
        "deterministic_selection",
    ),
}
LUNA_CONFIRMATION_CONFIGURATION = ApprovedAIConfiguration(
    LUNA_CONFIRMATION_CONFIGURATION_ID,
    "Targeted hostility — Luna confirmation 100",
    "Imports the frozen local GPT-6 Luna confirmation evaluation for human review.",
    "hostility",
    "australian_partisan_hostility",
    "0.2.0",
    "openai",
    "gpt-6-luna",
    HOSTILITY_LUNA_PROMPT_VERSION,
    LUNA_CONFIRMATION_PROMPT_SHA256,
    LUNA_CONFIRMATION_SCHEMA_SHA256,
    LUNA_CONFIRMATION_CONTRACT_SHA256,
    LUNA_CONFIRMATION_PROVIDER_SCHEMA_SHA256,
    {"max_output_tokens": 1000, "reasoning_effort": "medium"},
    "local_evaluation",
    "hostility-luna-confirmation-v1",
    "frozen_sample",
    LUNA_CONFIRMATION_SAMPLE_ID,
    100,
    LUNA_CONFIRMATION_SAMPLE_SHA256,
)


def approved_configuration(configuration_id: str) -> ApprovedAIConfiguration:
    if configuration_id == LUNA_CONFIRMATION_CONFIGURATION_ID:
        return LUNA_CONFIRMATION_CONFIGURATION
    try:
        return APPROVED_AI_CONFIGURATIONS[configuration_id]
    except KeyError:
        raise ValueError("unknown approved AI annotation configuration") from None


def configuration_for_campaign(campaign: dict[str, Any]) -> ApprovedAIConfiguration:
    identifier = campaign.get("configuration_id") or POLICY_CONFIGURATION_ID
    configuration = approved_configuration(str(identifier))
    expected = configuration.persistence_values()
    for key in (
        "prediction_task",
        "annotation_schema_slug",
        "annotation_schema_version",
        "provider",
        "configured_model",
        "prompt_version",
        "prompt_sha256",
        "canonical_schema_sha256",
        "prediction_contract_sha256",
        "provider_schema_sha256",
        "generation_config",
        "execution_mode",
    ):
        actual = campaign.get(key)
        if (
            actual is None
            and identifier == POLICY_CONFIGURATION_ID
            and key
            in {
                "prediction_task",
                "annotation_schema_slug",
                "annotation_schema_version",
            }
        ):
            actual = expected[key]
        if actual != expected[key]:
            raise ValueError(f"stored campaign differs from approved configuration: {key}")
    if configuration.record_set_mode == "frozen_sample":
        if campaign.get("record_set_id") != configuration.record_set_id:
            raise ValueError(
                "stored campaign differs from approved configuration: record_set_id"
            )
        approved_sample_sha = (
            campaign.get("approved_record_set_sha256")
            if "approved_record_set_sha256" in campaign
            else campaign.get("record_set_sha256")
        )
        if approved_sample_sha != configuration.record_set_sha256:
            raise ValueError(
                "stored campaign differs from approved configuration: approved_record_set_sha256"
            )
        selection = campaign.get("selection_definition")
        if (
            isinstance(selection, dict)
            and selection
            and (
                selection.get("mode") != "frozen_sample"
                or selection.get("sample_id") != configuration.record_set_id
                or selection.get("sample_size") != configuration.record_set_size
                or selection.get("sample_sha256") != configuration.record_set_sha256
            )
        ):
            raise ValueError(
                "stored campaign differs from approved configuration: selection_definition"
            )
        planned_count = campaign.get("planned_count")
        if (
            planned_count is not None
            and (campaign.get("status", "DRAFT") != "DRAFT" or int(planned_count) != 0)
            and int(planned_count) != configuration.record_set_size
        ):
            raise ValueError(
                "stored campaign differs from approved configuration: planned_count"
            )
    elif (
        campaign.get("record_set_id") is not None
        or campaign.get("approved_record_set_sha256") is not None
    ):
        raise ValueError(
            "stored campaign differs from approved configuration: record_set_id"
        )
    return configuration
