from __future__ import annotations

from typing import Any

import pytest

from hansard_annotator.llm_eval.hostility import (
    HOSTILITY_PRODUCTION_CONTRACT_VERSION,
)
from hansard_annotator.llm_eval.hostility import (
    prediction_json_schema as hostility_schema,
)
from hansard_annotator.llm_eval.hostility_validation import HELDOUT_SAMPLE_SHA256
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_GENERATION,
    APPROVED_PRODUCTION_MODEL,
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_HOSTILITY_CANONICAL_SCHEMA_SHA256,
    FROZEN_MODEL,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
    OPTIMIZED_HOSTILITY_CONTRACT_SHA256,
    OPTIMIZED_HOSTILITY_PROMPT_SHA256,
    OPTIMIZED_HOSTILITY_PROVIDER_SCHEMA_SHA256,
    OPTIMIZED_HOSTILITY_RECORD_IDS_SHA256,
    PREDICTION_FIELDS,
    audit_prompt_structure,
    build_batch_create_request,
    build_cache_create_request,
    canonical_hash,
    collect_inline_batch,
    gemini_batch_schema,
    parse_batch_status,
    production_record_from_turn,
    project_production_cost,
    simulate_costs,
    submission_batch_name,
    validate_batch_mapping,
    validate_production_plan,
)
from hansard_annotator.llm_eval.prompt import (
    HOSTILITY_PRODUCTION_PROMPT_VERSION,
    build_user_prompt,
    prediction_json_schema,
)
from hansard_annotator.llm_eval.providers import gemini_request_payload


def _record(number: int) -> dict[str, Any]:
    return {
        "record_id": f"record-{number}",
        "speech": {
            "date": "2026-01-01",
            "chamber": "House",
            "speaker_name": "Speaker",
            "major_heading": "Debate",
            "minor_heading": None,
            "business_type": None,
            "text": f"Speech {number}",
        },
    }


def _context() -> dict[str, Any]:
    return {
        "schema": {"version": "0.2.0"},
        "taxonomies": [{"labels": [{"code": "AU01"}]}],
    }


def test_cost_simulation_separates_processing_and_storage() -> None:
    value = simulate_costs(
        records=100,
        input_tokens_per_record=1000,
        output_tokens_per_record=100,
        cached_share=0.8,
        input_rate=0.30,
        cached_rate=0.03,
        output_rate=2.50,
        explicit_cache_tokens=800,
        ttl_hours=2,
        storage_rate=1.0,
    )
    assert value["uncached_input_tokens"] == 20_000
    assert value["cached_input_tokens"] == 80_000
    assert value["token_processing_cost_usd"] == pytest.approx(0.0334)
    assert value["explicit_cache_storage_cost_usd"] == pytest.approx(0.0016)
    assert value["estimated_total_cost_usd"] == pytest.approx(0.035)


def test_full_corpus_projection_does_not_multiply_shared_cache_storage() -> None:
    value = project_production_cost(
        token_processing_cost_usd=0.04195965,
        valid_records=100,
        target_records=160_577,
        shared_cache_storage_cost_usd=0.10019823,
    )
    assert value["projected_variable_token_processing_usd"] == pytest.approx(67.3775471805)
    assert value["projected_shared_cache_storage_usd"] == pytest.approx(0.10019823)
    assert value["projected_total_usd"] == pytest.approx(67.4777454105)


def test_prompt_audit_detects_stable_prefix_and_hashable_variable_content() -> None:
    audit = audit_prompt_structure(
        _context(),
        (_record(1), _record(2)),
        prompt_version="phase4a-policy-v2",
        measured_input_tokens=2000,
    )
    assert audit.system_characters > audit.average_user_characters
    assert audit.estimated_invariant_tokens + audit.estimated_variable_tokens == 1000
    assert 0 < audit.theoretical_cache_share < 1


def test_batch_preserves_schema_thinking_prompt_and_record_mapping() -> None:
    schema = prediction_json_schema(("AU01", "AU02", "AU_OTHER_REVIEW"))
    records = (_record(1), _record(2))
    payload = build_batch_create_request(
        model="gemini-2.5-flash",
        system_prompt="frozen-system",
        records=records,
        schema=schema,
        display_name="pilot",
    )
    requests = payload["batch"]["input_config"]["requests"]["requests"]
    assert [item["metadata"]["key"] for item in requests] == ["record-1", "record-2"]
    request = requests[0]["request"]
    assert request["systemInstruction"]["parts"][0]["text"] == "frozen-system"
    assert request["generationConfig"]["responseSchema"] == gemini_batch_schema(schema)
    assert set(request["generationConfig"]["responseSchema"]["properties"]) == PREDICTION_FIELDS
    assert "responseJsonSchema" not in request["generationConfig"]
    assert request["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert request["generationConfig"]["responseMimeType"] == "application/json"

    cached = build_batch_create_request(
        model="gemini-2.5-flash",
        system_prompt="frozen-system",
        records=records,
        schema=schema,
        display_name="pilot-cached",
        cached_content="cachedContents/fixture",
    )
    cached_request = cached["batch"]["input_config"]["requests"]["requests"][0]["request"]
    assert cached_request["cachedContent"] == "cachedContents/fixture"
    assert "systemInstruction" not in cached_request


def test_gemini_31_batch_uses_minimal_level_without_thinking_budget() -> None:
    schema = prediction_json_schema(("AU01", "AU_OTHER_REVIEW"))
    payload = build_batch_create_request(
        model=APPROVED_PRODUCTION_MODEL,
        system_prompt="frozen-system",
        records=(_record(1),),
        schema=schema,
        display_name="production",
        cached_content="cachedContents/gemini31",
        generation=APPROVED_PRODUCTION_GENERATION,
    )
    request = payload["batch"]["input_config"]["requests"]["requests"][0]["request"]
    assert request["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "minimal"
    }
    assert "thinkingBudget" not in request["generationConfig"]["thinkingConfig"]

    plan = {
        "provenance": {
            "model": APPROVED_PRODUCTION_MODEL,
            "prompt_version": FROZEN_PROMPT_VERSION,
            "assembled_prompt_sha256": FROZEN_PROMPT_SHA256,
            "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
            "thinking_level": "minimal",
            "cached_content": "cachedContents/gemini31",
            "provider_schema_sha256": canonical_hash(
                request["generationConfig"]["responseSchema"]
            ),
        },
        "request": payload,
    }
    validate_production_plan(plan)

    plan["provenance"]["thinking_budget"] = 0
    with pytest.raises(ValueError, match="thinking configuration"):
        validate_production_plan(plan)


def test_cache_request_and_batch_mapping_recovery_guards() -> None:
    cache = build_cache_create_request(
        model="gemini-2.5-flash",
        system_prompt="same text",
        ttl_seconds=3600,
        display_name="pilot",
    )
    assert cache["model"] == "models/gemini-2.5-flash"
    assert cache["systemInstruction"]["parts"][0]["text"] == "same text"
    assert cache["ttl"] == "3600s"
    mapped = validate_batch_mapping(
        ("record-1", "record-2"),
        [
            {"metadata": {"key": "record-2"}, "response": {}},
            {"metadata": {"key": "record-1"}, "response": {}},
        ],
    )
    assert tuple(sorted(mapped)) == ("record-1", "record-2")
    with pytest.raises(ValueError, match="incomplete"):
        validate_batch_mapping(("record-1", "record-2"), [{"metadata": {"key": "record-1"}}])


def test_submission_requires_explicit_confirmation(tmp_path: Any) -> None:
    from hansard_annotator.llm_eval.ops_cli import main

    plan = tmp_path / "plan.json"
    plan.write_text('{"request": {}}', encoding="utf-8")
    assert (
        main(
            [
                "submit-plan",
                "--plan",
                str(plan),
                "--kind",
                "batch",
                "--result",
                str(tmp_path / "result.json"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "result.json").exists()


def test_production_turn_mapping_preserves_stable_identity_and_prompt_fields() -> None:
    record = production_record_from_turn(
        {
            "turn_key": "abc123",
            "date": "2026-01-02",
            "chamber": "House",
            "speaker_name_raw": "A Speaker",
            "major_heading_original": "HEADING",
            "minor_heading_original": "Topic",
            "business_type": "debate",
            "text_clean": "Clean speech text",
            "text_raw": "Raw speech text",
        }
    )
    assert record["record_id"] == "turn:abc123"
    assert record["speech"]["text"] == "Clean speech text"


def _batch_plan() -> dict[str, Any]:
    schema = prediction_json_schema(("AU01", "AU02", "AU_OTHER_REVIEW"))
    return {
        "provenance": {"record_count": 2},
        "request": build_batch_create_request(
            model="gemini-2.5-flash",
            system_prompt="system",
            records=(_record(1), _record(2)),
            schema=schema,
            display_name="pilot",
        ),
    }


def _gemini_response(domain: str = "AU01") -> dict[str, Any]:
    import json

    return {
        "modelVersion": "gemini-2.5-flash",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "is_non_policy": False,
                                    "primary_australian_domain": domain,
                                    "secondary_australian_domains": [],
                                    "reasoning": "Policy content.",
                                }
                            )
                        }
                    ]
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "cachedContentTokenCount": 80,
            "candidatesTokenCount": 10,
            "thoughtsTokenCount": 0,
            "totalTokenCount": 110,
        },
    }


def test_submission_and_status_parsing_support_operation_envelope() -> None:
    receipt = {"response": {"name": "batches/example"}}
    assert submission_batch_name(receipt) == "batches/example"
    pending = {
        "metadata": {
            "name": "batches/example",
            "displayName": "pilot",
            "model": "models/gemini-2.5-flash",
            "state": "BATCH_STATE_RUNNING",
            "batchStats": {"requestCount": "2", "pendingRequestCount": "2"},
        }
    }
    status = parse_batch_status(pending)
    assert status["terminal"] is False
    assert status["total_request_count"] == 2
    assert status["pending_count"] == 2


def test_collection_maps_keys_validates_and_aggregates_usage() -> None:
    batch = {
        "response": {
            "name": "batches/example",
            "state": "BATCH_STATE_SUCCEEDED",
            "batchStats": {"requestCount": "2", "successfulRequestCount": "2"},
            "output": {
                "inlinedResponses": {
                    "inlinedResponses": [
                        {"metadata": {"key": "record-2"}, "response": _gemini_response("AU02")},
                        {"metadata": {"key": "record-1"}, "response": _gemini_response()},
                    ]
                }
            },
        }
    }
    results, coverage = collect_inline_batch(
        batch_value=batch,
        plan=_batch_plan(),
        domain_codes=("AU01", "AU02", "AU_OTHER_REVIEW"),
    )
    assert {result["record_id"] for result in results} == {"record-1", "record-2"}
    assert all(result["status"] == "success" for result in results)
    assert sum(result["usage"]["cached_input_tokens"] for result in results) == 160
    assert coverage["missing_responses"] == 0


def test_collection_flags_missing_duplicates_failures_and_invalid_predictions() -> None:
    batch = {
        "response": {
            "state": "BATCH_STATE_SUCCEEDED",
            "batchStats": {
                "requestCount": "2",
                "successfulRequestCount": "1",
                "failedRequestCount": "1",
            },
            "output": {
                "inlinedResponses": {
                    "inlinedResponses": [
                        {"metadata": {"key": "record-1"}, "response": _gemini_response("BAD")},
                        {
                            "metadata": {"key": "record-1"},
                            "error": {"code": 13, "message": "failed"},
                        },
                    ]
                }
            },
        }
    }
    results, coverage = collect_inline_batch(
        batch_value=batch,
        plan=_batch_plan(),
        domain_codes=("AU01", "AU02", "AU_OTHER_REVIEW"),
    )
    assert results[0]["status"] == "invalid"
    assert results[1]["status"] == "mapping_error"
    assert coverage["duplicate_mappings"] == 1
    assert coverage["missing_responses"] == 1


@pytest.mark.parametrize("state", ["BATCH_STATE_FAILED", "BATCH_STATE_EXPIRED"])
def test_terminal_failed_gemini_job_can_be_collected_without_fabricating_results(
    state: str,
) -> None:
    batch = {
        "name": "batches/failed-child",
        "displayName": "phase4e-child",
        "model": "models/gemini-2.5-flash",
        "state": state,
        "error": {"message": "provider job terminated"},
        "batchStats": {"requestCount": "2", "successfulRequestCount": "0", "failedRequestCount": "2"},
    }
    with pytest.raises(ValueError, match="did not succeed"):
        collect_inline_batch(
            batch_value=batch,
            plan=_batch_plan(),
            domain_codes=("AU01", "AU02", "AU_OTHER_REVIEW"),
        )
    results, coverage = collect_inline_batch(
        batch_value=batch,
        plan=_batch_plan(),
        domain_codes=("AU01", "AU02", "AU_OTHER_REVIEW"),
        allow_unsuccessful_terminal=True,
    )
    assert results == []
    assert coverage["planned_requests"] == 2
    assert coverage["collected_responses"] == 0
    assert coverage["missing_responses"] == 2


def test_terminal_failed_job_preserves_available_response_and_marks_only_absence_missing() -> None:
    results, coverage = collect_inline_batch(
        batch_value={
            "state": "BATCH_STATE_FAILED",
            "error": {"message": "partial provider failure"},
            "batchStats": {"requestCount": "2", "successfulRequestCount": "1", "failedRequestCount": "1"},
            "output": {"inlinedResponses": {"inlinedResponses": [
                {"metadata": {"key": "record-1"}, "response": _gemini_response()}
            ]}},
        },
        plan=_batch_plan(),
        domain_codes=("AU01", "AU02", "AU_OTHER_REVIEW"),
        allow_unsuccessful_terminal=True,
    )
    assert [result["record_id"] for result in results] == ["record-1"]
    assert results[0]["status"] == "success"
    assert coverage["collected_responses"] == 1
    assert coverage["missing_responses"] == 1


@pytest.mark.parametrize("state", ["BATCH_STATE_PENDING", "BATCH_STATE_RUNNING"])
def test_collection_refuses_non_terminal_batches(state: str) -> None:
    with pytest.raises(ValueError, match="not terminal"):
        collect_inline_batch(
            batch_value={"state": state},
            plan=_batch_plan(),
            domain_codes=("AU01", "AU02", "AU_OTHER_REVIEW"),
        )


def test_paid_batch_guard_accepts_only_frozen_canonical_contract() -> None:
    plan = _batch_plan()
    plan["provenance"] = {
        "model": FROZEN_MODEL,
        "prompt_version": FROZEN_PROMPT_VERSION,
        "assembled_prompt_sha256": FROZEN_PROMPT_SHA256,
        "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
        "thinking_budget": 0,
        "cached_content": "cachedContents/test",
        "provider_schema_sha256": canonical_hash(
            plan["request"]["batch"]["input_config"]["requests"]["requests"][0]["request"][
                "generationConfig"
            ]["responseSchema"]
        ),
    }
    validate_production_plan(plan)
    wrong = _batch_plan()
    wrong["provenance"] = dict(plan["provenance"])
    wrong["provenance"]["canonical_schema_sha256"] = "wrong"
    with pytest.raises(ValueError, match="canonical_schema_sha256"):
        validate_production_plan(wrong)
    old_schema = _batch_plan()
    old_schema["provenance"] = dict(plan["provenance"])
    request = old_schema["request"]["batch"]["input_config"]["requests"]["requests"][0]
    request["request"]["generationConfig"]["responseSchema"]["properties"] = {
        "is_non_policy": {},
        "annotation_notes": {},
        "fallback_explanation": {},
    }
    with pytest.raises(ValueError, match="field set"):
        validate_production_plan(old_schema)


@pytest.mark.parametrize(("variant", "limit", "cache"), [("A", 1000, None), ("B", 500, None), ("C", 500, "cachedContents/opt")])
def test_paid_batch_guard_accepts_only_frozen_hostility_optimization_variants(
    variant: str, limit: int, cache: str | None
) -> None:
    import json
    from pathlib import Path

    sample = json.loads(
        Path("config/research_samples/hostility-heldout-100.json").read_text(encoding="utf-8")
    )
    records = tuple(
        {
            "record_id": f"turn:{turn['turn_key']}",
            "speech": {"text": "Example speech."},
        }
        for turn in sample["turns"]
    )
    schema = hostility_schema(HOSTILITY_PRODUCTION_PROMPT_VERSION)
    batch = build_batch_create_request(
        model=FROZEN_MODEL,
        system_prompt="fixed hostility system prompt",
        records=records,
        schema=schema,
        display_name=f"hostility-optimization-{variant}",
        cached_content=cache,
        generation={"thinking_budget": 0},
        max_output_tokens=limit,
    )
    plan = {
        "provenance": {
            "model": FROZEN_MODEL,
            "provider": "gemini",
            "prediction_task": "hostility",
            "prompt_version": HOSTILITY_PRODUCTION_PROMPT_VERSION,
            "assembled_prompt_sha256": OPTIMIZED_HOSTILITY_PROMPT_SHA256,
            "canonical_schema_sha256": FROZEN_HOSTILITY_CANONICAL_SCHEMA_SHA256,
            "prediction_contract_sha256": OPTIMIZED_HOSTILITY_CONTRACT_SHA256,
            "prediction_contract_version": HOSTILITY_PRODUCTION_CONTRACT_VERSION,
            "provider_schema_sha256": OPTIMIZED_HOSTILITY_PROVIDER_SCHEMA_SHA256,
            "max_output_tokens": limit,
            "optimization_variant": variant,
            "cached_content": cache,
            "cached_content_prefix_sha256": (
                OPTIMIZED_HOSTILITY_PROMPT_SHA256 if variant == "C" else None
            ),
            "thinking_budget": 0,
            "temperature": 0.0,
            "record_count": 100,
            "record_ids_sha256": OPTIMIZED_HOSTILITY_RECORD_IDS_SHA256,
            "benchmark_set_sha256": HELDOUT_SAMPLE_SHA256,
        },
        "request": batch,
    }
    validate_production_plan(plan)
    if variant == "C":
        assert "cachedContent" in batch["batch"]["input_config"]["requests"]["requests"][0]["request"]


def test_batch_semantics_derive_from_phase4a_prediction_contract() -> None:
    schema = prediction_json_schema(("AU01", "AU02", "AU_OTHER_REVIEW"))
    batch = build_batch_create_request(
        model="gemini-2.5-flash",
        system_prompt="identical system",
        records=(_record(1),),
        schema=schema,
        display_name="equivalence",
    )
    request = batch["batch"]["input_config"]["requests"]["requests"][0]["request"]
    synchronous = gemini_request_payload(
        system_prompt="identical system",
        user_prompt=build_user_prompt(_record(1)),
        schema=schema,
        generation={"thinking_budget": 0},
    )
    assert request["systemInstruction"]["parts"][0]["text"] == "identical system"
    assert request["systemInstruction"] == synchronous["systemInstruction"]
    assert request["contents"] == synchronous["contents"]
    assert (
        request["generationConfig"]["thinkingConfig"]
        == synchronous["generationConfig"]["thinkingConfig"]
    )
    transformed = request["generationConfig"]["responseSchema"]
    assert set(transformed["properties"]) == set(schema["properties"]) == PREDICTION_FIELDS
    assert transformed["properties"]["primary_australian_domain"]["enum"] == [
        "AU01",
        "AU02",
        "AU_OTHER_REVIEW",
    ]


def test_batch_collection_validates_no_reasoning_hostility_and_preserves_evidence() -> None:
    import json

    from hansard_annotator.llm_eval.prompt import build_system_prompt

    speech = "The government is incompetent."
    record = {
        **_record(1),
        "record_id": "turn:production-test",
        "speech": {**_record(1)["speech"], "text": speech},
    }
    schema = hostility_schema(HOSTILITY_PRODUCTION_PROMPT_VERSION)
    system = build_system_prompt(
        {
            "schema": {"slug": "australian_partisan_hostility"},
            "taxonomies": [],
        },
        HOSTILITY_PRODUCTION_PROMPT_VERSION,
    )
    plan = {
        "provenance": {
            "prediction_task": "hostility",
            "prompt_version": HOSTILITY_PRODUCTION_PROMPT_VERSION,
        },
        "request": build_batch_create_request(
            model=FROZEN_MODEL,
            system_prompt=system,
            records=(record,),
            schema=schema,
            display_name="production-output-contract-test",
            max_output_tokens=500,
        ),
    }
    batch = {
        "response": {
            "state": "BATCH_STATE_SUCCEEDED",
            "batchStats": {"requestCount": "1", "successfulRequestCount": "1"},
            "output": {
                "inlinedResponses": {
                    "inlinedResponses": [
                        {
                            "metadata": {"key": "turn:production-test"},
                            "response": {
                                "modelVersion": FROZEN_MODEL,
                                "candidates": [
                                    {
                                        "content": {
                                            "parts": [
                                                {
                                                    "text": json.dumps(
                                                        {
                                                            "hostility_present": "YES",
                                                            "target": "OPPOSING_PARTISAN_TARGET",
                                                            "evidence_spans": [
                                                                {"text": "government is incompetent"}
                                                            ],
                                                        }
                                                    )
                                                }
                                            ]
                                        }
                                    }
                                ],
                                "usageMetadata": {"promptTokenCount": 2000},
                            },
                        }
                    ]
                }
            },
        }
    }
    results, _ = collect_inline_batch(
        batch_value=batch,
        plan=plan,
        domain_codes=(),
        prediction_task="hostility",
    )
    assert results[0]["status"] == "success"
    prediction = results[0]["prediction"]
    assert isinstance(prediction, dict)
    assert set(prediction) == {"hostility_present", "target", "evidence_spans"}
    assert list(prediction["evidence_spans"]) == [
        {"start_offset": 4, "end_offset": 29, "text": "government is incompetent"}
    ]
    assert results[0]["failure_category"] is None
    assert results[0]["diagnostics"]["evidence_model_supplied"] == [
        "government is incompetent"
    ]
    assert results[0]["diagnostics"]["evidence_verified_canonical"] == [
        "government is incompetent"
    ]


def test_failure_category_distinguishes_truncation_from_invalid_json() -> None:
    import json

    from hansard_annotator.llm_eval.failure_categories import (
        FailureCategory,
        json_failure_category,
    )

    with pytest.raises(json.JSONDecodeError) as truncated:
        json.loads('{"hostility_present":"YES"')
    assert json_failure_category(truncated.value, '{"hostility_present":"YES"') == (
        FailureCategory.JSON_TRUNCATION
    )
    with pytest.raises(json.JSONDecodeError) as malformed:
        json.loads("{not json}")
    assert json_failure_category(malformed.value, "{not json}") == (
        FailureCategory.STRUCTURED_OUTPUT_INVALID
    )


def test_evidence_repair_collector_merges_only_frozen_classification() -> None:
    import json

    from hansard_annotator.llm_eval.hostility_evidence_repair import (
        EVIDENCE_REPAIR_SCHEMA,
        EVIDENCE_REPAIR_SYSTEM_PROMPT,
    )

    record = _record(1)
    record["record_id"] = "turn:turn-1"
    record["speech"]["text"] = "They are deceitful liars."
    plan = {
        "provenance": {
            "prediction_task": "hostility_evidence_repair",
            "prompt_version": HOSTILITY_PRODUCTION_PROMPT_VERSION,
        },
        "request": build_batch_create_request(
            model=FROZEN_MODEL,
            system_prompt=EVIDENCE_REPAIR_SYSTEM_PROMPT,
            records=(record,),
            schema=EVIDENCE_REPAIR_SCHEMA,
            display_name="evidence-repair-test",
            max_output_tokens=500,
        ),
    }
    batch = {
        "response": {
            "state": "BATCH_STATE_SUCCEEDED",
            "batchStats": {"requestCount": "1", "successfulRequestCount": "1"},
            "output": {
                "inlinedResponses": {
                    "inlinedResponses": [
                        {
                            "metadata": {"key": "turn:turn-1"},
                            "response": {
                                "candidates": [{
                                    "content": {"parts": [{"text": json.dumps({"evidence_spans": [{"text": "deceitful liars"}]})}]}
                                }],
                                "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 4},
                            },
                        }
                    ]
                }
            },
        }
    }
    frozen = {
        "turn:turn-1": {
            "hostility_present": "YES",
            "target": "OPPOSING_PARTISAN_TARGET",
        }
    }
    results, coverage = collect_inline_batch(
        batch_value=batch,
        plan=plan,
        domain_codes=(),
        prediction_task="hostility_evidence_repair",
        records_by_id={"turn:turn-1": record},
        frozen_classifications_by_id=frozen,
    )
    assert results[0]["status"] == "success"
    assert results[0]["prediction"]["hostility_present"] == "YES"
    assert results[0]["prediction"]["target"] == "OPPOSING_PARTISAN_TARGET"
    assert results[0]["prediction"]["evidence_spans"][0]["text"] == "deceitful liars"
    assert coverage["failure_category_counts"]["EVIDENCE_REPAIR_VALIDATION_FAILURE"] == 0


def test_read_only_hostility_failure_diagnosis_counts_categories(tmp_path: Any) -> None:
    import json
    from pathlib import Path

    from hansard_annotator.llm_eval.ops_cli import _diagnose_hostility_failures

    directory = Path(tmp_path) / "artifact"
    directory.mkdir()
    (directory / "operational_summary.json").write_text(
        json.dumps({"coverage": {"planned_requests": 2, "evidence_resolution_failures": 1}}),
        encoding="utf-8",
    )
    (directory / "predictions.jsonl").write_text(
        json.dumps(
            {
                "record_id": "turn:1",
                "status": "invalid",
                "failure_category": "EVIDENCE_RESOLUTION_FAILURE",
                "validation_error": "evidence could not be resolved",
            }
        )
        + "\n"
        + json.dumps({"record_id": "turn:2", "status": "success"})
        + "\n",
        encoding="utf-8",
    )
    result = _diagnose_hostility_failures(directory)
    assert result["mode"] == "read_only_diagnosis"
    assert result["category_counts_in_artifact"]["EVIDENCE_RESOLUTION_FAILURE"] == 1
    assert result["failures"][0]["record_id"] == "turn:1"


def test_collector_rejects_old_annotation_field_shape() -> None:
    old = _gemini_response()
    old["candidates"][0]["content"]["parts"][0]["text"] = (
        '{"is_non_policy": false, "primary_australian_domain": "AU01", '
        '"secondary_australian_domains": [], "annotation_notes": null, '
        '"fallback_explanation": null}'
    )
    batch = {
        "response": {
            "state": "BATCH_STATE_SUCCEEDED",
            "batchStats": {"requestCount": "2", "successfulRequestCount": "2"},
            "output": {
                "inlinedResponses": {
                    "inlinedResponses": [
                        {"metadata": {"key": "record-1"}, "response": old},
                        {"metadata": {"key": "record-2"}, "response": _gemini_response()},
                    ]
                }
            },
        }
    }
    results, _ = collect_inline_batch(
        batch_value=batch,
        plan=_batch_plan(),
        domain_codes=("AU01", "AU02", "AU_OTHER_REVIEW"),
    )
    assert results[0]["status"] == "invalid"
    assert results[0]["validation_error"] == "response fields do not match the annotation schema"
