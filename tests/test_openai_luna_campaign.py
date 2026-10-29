from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hansard_annotator.llm_eval.hostility import prediction_json_schema
from hansard_annotator.llm_eval.hostility_validation import (
    LUNA_CONFIRMATION_CONTRACT_SHA256,
    LUNA_CONFIRMATION_PROMPT_SHA256,
)
from hansard_annotator.llm_eval.operations import production_record_from_turn
from hansard_annotator.llm_eval.prompt import HOSTILITY_LUNA_PROMPT_VERSION
from hansard_annotator.web.campaigns.configurations import (
    LUNA_PRODUCTION_CONFIGURATION_ID,
    approved_configuration,
)
from hansard_annotator.web.campaigns.openai_batch import (
    OpenAIBatchGateway,
    luna_batch_request,
    luna_custom_id,
)
from hansard_annotator.web.campaigns.phase4e import _collect_openai_chunk


def test_luna_production_configuration_is_exact_and_frozen() -> None:
    configuration = approved_configuration(LUNA_PRODUCTION_CONFIGURATION_ID)
    assert configuration.provider == "openai"
    assert configuration.configured_model == "gpt-6-luna"
    assert configuration.prediction_task == "hostility"
    assert configuration.prompt_version == HOSTILITY_LUNA_PROMPT_VERSION
    assert configuration.prompt_sha256 == LUNA_CONFIRMATION_PROMPT_SHA256
    assert configuration.prediction_contract_sha256 == LUNA_CONFIRMATION_CONTRACT_SHA256
    assert configuration.canonical_schema_sha256 == (
        "959a51a987e0777cb72dc7ad12cd5b896429bef400b79f386764a20ddc3d5669"
    )
    assert configuration.generation_config == {
        "max_output_tokens": 1000,
        "reasoning_effort": "medium",
    }
    assert configuration.execution_mode == "batch"
    assert configuration.record_set_mode == "deterministic_selection"


def test_luna_request_uses_only_frozen_responses_contract() -> None:
    configuration = approved_configuration(LUNA_PRODUCTION_CONFIGURATION_ID)
    schema = prediction_json_schema(HOSTILITY_LUNA_PROMPT_VERSION)
    request = luna_batch_request(
        record_id=f"turn:{'a' * 64}",
        user_prompt="speech prompt",
        system_prompt="frozen instructions",
        schema=schema,
        generation=configuration.generation_config,
    )
    assert request["custom_id"] == "a" * 64
    assert request["url"] == "/v1/responses"
    body = request["body"]
    assert body["model"] == "gpt-6-luna"
    assert body["instructions"] == "frozen instructions"
    assert body["input"] == "speech prompt"
    assert body["max_output_tokens"] == 1000
    assert body["reasoning"] == {"effort": "medium"}
    assert "temperature" not in body
    output_schema = body["text"]["format"]["schema"]
    luna_fields = {"hostility_present", "target", "evidence_spans"}
    assert set(output_schema["properties"]) == {
        "hostility_present",
        "target",
        "evidence_spans",
    }
    assert set(output_schema["required"]) == luna_fields
    assert "reasoning" not in output_schema["properties"]
    with pytest.raises(ValueError, match="frozen"):
        luna_batch_request(
            record_id=f"turn:{'a' * 64}",
            user_prompt="speech prompt",
            system_prompt="frozen instructions",
            schema=schema,
            generation={"max_output_tokens": 750, "reasoning_effort": "medium"},
        )


def test_luna_batch_custom_id_is_a_bounded_turn_key() -> None:
    turn_key = "a" * 64
    assert luna_custom_id(f"turn:{turn_key}") == turn_key
    with pytest.raises(ValueError, match="canonical SHA-256"):
        luna_custom_id("turn:too-long-for-a-canonical-turn-key")


def test_production_web_and_worker_receive_openai_key_from_secret_environment() -> None:
    compose = (Path(__file__).parents[1] / "compose.production.yaml").read_text(
        encoding="utf-8"
    )
    assert "OPENAI_API_KEY: ${OPENAI_API_KEY:-}" in compose
    assert "campaign-worker:" in compose
    assert "environment: *app-environment" in compose


def test_openai_batch_collection_preserves_failure_invalid_and_missing(tmp_path: Path) -> None:
    # Luna's frozen contract accepts exactly these fields and deliberately omits reasoning.
    luna_schema = prediction_json_schema(HOSTILITY_LUNA_PROMPT_VERSION)
    luna_fields = {"hostility_present", "target", "evidence_spans"}
    assert set(luna_schema["properties"]) == luna_fields
    assert set(luna_schema["required"]) == luna_fields

    records = tuple(
        production_record_from_turn(
            {
                "turn_key": f"{index:064x}",
                "date": "2026-01-01",
                "chamber": "House",
                "speaker_name_raw": "Member",
                "major_heading_original": "Business",
                "minor_heading_original": None,
                "business_type": "Debate",
                "text_clean": f"speech {index}",
                "text_raw": None,
            }
        )
        for index in range(3)
    )
    output = {
        "custom_id": luna_custom_id(str(records[0]["record_id"])),
        "response": {
            "status_code": 200,
            "body": {
                "model": "gpt-6-luna",
                "output_text": json.dumps(
                    {"hostility_present": "NO", "target": "N/A", "evidence_spans": []}
                ),
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 3},
                    "total_tokens": 120,
                },
            },
        },
    }
    provider_error = {
        "custom_id": luna_custom_id(str(records[1]["record_id"])),
        "error": {"code": "server_error", "message": "provider failed"},
    }

    class Gateway:
        def retrieve(self, _batch_id: str) -> dict[str, Any]:
            return {"id": "batch_1", "status": "completed", "output_file_id": "file_o", "error_file_id": "file_e"}

        def file_content(self, file_id: str) -> bytes:
            return (json.dumps(output) + "\n").encode() if file_id == "file_o" else (json.dumps(provider_error) + "\n").encode()

    rows, summary = _collect_openai_chunk(
        Gateway(),  # type: ignore[arg-type]
        chunk={"provider_job_id": "batch_1", "provider_input_file_sha256": "a" * 64},
        directory=tmp_path,
        records=records,
    )
    assert [row["status"] for row in rows] == ["success", "provider_failure"]
    assert rows[0]["prediction"] == {
        "hostility_present": "NO",
        "target": "N/A",
        "evidence_spans": [],
    }
    assert summary["coverage"]["valid_structured_predictions"] == 1
    assert summary["coverage"]["failed_provider_responses"] == 1
    assert summary["coverage"]["missing_responses"] == 1
    assert summary["tokens"]["reasoning_tokens"] == 3


def test_openai_gateway_keeps_api_key_out_of_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-key")
    gateway = OpenAIBatchGateway()

    def fail(*_args: object, **_kwargs: object) -> Any:
        raise OSError("connection failed")

    monkeypatch.setattr("hansard_annotator.web.campaigns.openai_batch.urlopen", fail)
    with pytest.raises(Exception) as error:
        gateway.retrieve("batch_123")
    assert "test-secret-key" not in str(error.value)
    assert "batch_123" in str(error.value)
