"""Bounded-memory Phase 4E full-corpus chunk planning and orchestration."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.llm_eval.costs import load_pricing
from hansard_annotator.llm_eval.hostility import prediction_json_schema as hostility_schema
from hansard_annotator.llm_eval.hostility import validate_prediction_with_diagnostics
from hansard_annotator.llm_eval.operations import (
    canonical_hash,
    collect_inline_batch,
    parse_batch_status,
    production_record_from_turn,
)
from hansard_annotator.llm_eval.ops_cli import _collection_summary
from hansard_annotator.llm_eval.prompt import (
    HOSTILITY_LUNA_PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
    prompt_hash,
)
from hansard_annotator.llm_eval.providers import provider_prediction_schema_hash
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.campaigns.configurations import LUNA_PRODUCTION_CONFIGURATION_ID
from hansard_annotator.web.campaigns.openai_batch import (
    MAX_OPENAI_BATCH_FILE_BYTES,
    MAX_OPENAI_BATCH_REQUESTS,
    OpenAIBatchGateway,
    luna_batch_request,
    luna_custom_id,
)
from hansard_annotator.web.campaigns.recovery import (
    STRUCTURAL_REPAIR_RULE,
    apply_structural_repairs,
    artifact_sha256,
    read_jsonl,
    structural_repair_count,
)
from hansard_annotator.web.campaigns.service import (
    CampaignError,
    GeminiGateway,
    LiveGeminiGateway,
    _artifact_directory,
    _campaign_configuration,
    _configuration_context,
    _production_domain_codes,
    _provider_plan,
    cache_plan_status,
    configuration_class,
    ensure_campaign_cache,
)
from hansard_annotator.web.codebook.service import ROOT
from hansard_annotator.web.config import WebSettings

FULL_PRODUCTION_CONFIRMATION = "START FULL PRODUCTION"
LUNA_PRODUCTION_CONFIRMATION = "Launch paid Luna production annotation"
CHUNK_PLANNER_VERSION = "phase4e-full-input-v1"
DEFAULT_ENQUEUED_TOKEN_CEILING = 10_000_000
DEFAULT_SAFETY_FACTOR = 0.90
DEFAULT_MAX_CHUNK_RECORDS = 1_000
INVARIANT_INPUT_TOKENS = 4_131
CHARACTERS_PER_TOKEN = 3
INSERT_BATCH_SIZE = 1_000
ACTIVE_BATCH_STATUSES = {"SUBMITTING", "SUBMITTED", "RUNNING", "PROVIDER_COMPLETED"}
PHASE4E_ADVISORY_LOCK_NAMESPACE = 0x50483445


@dataclass(frozen=True)
class ChunkSpec:
    chunk_number: int
    first_selection_order: int
    last_selection_order: int
    record_count: int
    record_set_sha256: str
    estimated_input_tokens: int


@dataclass(frozen=True)
class TickResult:
    action: str
    campaign_id: str
    chunk_number: int | None = None
    detail: str | None = None


class BatchFinder(Protocol):
    def find_batch(self, display_name: str, model: str) -> str | None: ...


def _collect_openai_chunk(
    gateway: OpenAIBatchGateway,
    *,
    chunk: dict[str, Any],
    directory: Path,
    records: tuple[dict[str, Any], ...],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    batch = gateway.retrieve(str(chunk["provider_job_id"]))
    output_id = batch.get("output_file_id")
    error_id = batch.get("error_file_id")
    planned_id_map = {
        luna_custom_id(str(record["record_id"])): str(record["record_id"])
        for record in records
    }
    if len(planned_id_map) != len(records):
        raise CampaignError("Luna Batch turn-key mapping is not unique")
    responses: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    output_bytes = gateway.file_content(str(output_id)) if isinstance(output_id, str) else b""
    error_bytes = gateway.file_content(str(error_id)) if isinstance(error_id, str) else b""
    for content in (output_bytes, error_bytes):
        for raw_line in content.splitlines():
            if not raw_line:
                continue
            try:
                value = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(value, dict) or not isinstance(value.get("custom_id"), str):
                continue
            custom_id = str(value["custom_id"])
            if custom_id not in planned_id_map:
                continue
            record_id = planned_id_map[custom_id]
            if record_id in responses:
                duplicate_count += 1
            else:
                responses[record_id] = value

    rows: list[dict[str, Any]] = []
    token_totals = {key: 0 for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")}
    provider_successes = 0
    provider_failures = 0
    invalid = 0
    evidence_failures = 0
    for request_index, record in enumerate(records):
        record_id = str(record["record_id"])
        item = responses.get(record_id)
        if item is None:
            continue  # The campaign effective builder records this as missing.
        response_value = item.get("response")
        response = cast(dict[str, Any], response_value) if isinstance(response_value, dict) else {}
        error = item.get("error")
        body = response.get("body")
        if not isinstance(body, dict) or error is not None or response.get("status_code") != 200:
            provider_failures += 1
            rows.append({
                "record_id": record_id, "turn_key": record_id.removeprefix("turn:"),
                "request_index": request_index, "status": "provider_failure",
                "prediction": None, "provider_error": error or response.get("status_code") or "OpenAI response missing",
                "provider_response": item,
            })
            continue
        provider_successes += 1
        usage_value = body.get("usage")
        usage = cast(dict[str, Any], usage_value) if isinstance(usage_value, dict) else {}
        input_details_value = usage.get("input_tokens_details")
        input_details = (
            cast(dict[str, Any], input_details_value)
            if isinstance(input_details_value, dict)
            else {}
        )
        output_details_value = usage.get("output_tokens_details")
        output_details = (
            cast(dict[str, Any], output_details_value)
            if isinstance(output_details_value, dict)
            else {}
        )
        values = {
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": input_details.get("cached_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": output_details.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        for key, value in values.items():
            token_totals[key] += value if isinstance(value, int) and value >= 0 else 0
        output_text = body.get("output_text")
        if not isinstance(output_text, str):
            output_items = body.get("output")
            if isinstance(output_items, list):
                output_text = next((part.get("text") for message in output_items if isinstance(message, dict) and message.get("type") == "message" for part in message.get("content", []) if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)), None)
        try:
            parsed = json.loads(output_text) if isinstance(output_text, str) else None
            prediction, diagnostics = validate_prediction_with_diagnostics(
                parsed, str(record["speech"]["text"]), HOSTILITY_LUNA_PROMPT_VERSION
            )
            if diagnostics.get("evidence_valid") is not True:
                raise ValueError("hostility evidence must resolve uniquely to canonical text")
            status = "success"
            output = prediction.to_dict()
            output.pop("reasoning", None)
            evidence_spans = output.get("evidence_spans")
            output["evidence_spans"] = (
                list(evidence_spans) if isinstance(evidence_spans, (list, tuple)) else []
            )
            validation_error = None
        except (json.JSONDecodeError, TypeError, ValueError) as validation_error_value:
            status = "invalid"
            output = None
            validation_error = str(validation_error_value)
            invalid += 1
            if validation_error == "hostility evidence must resolve uniquely to canonical text":
                evidence_failures += 1
        rows.append({
            "record_id": record_id, "turn_key": record_id.removeprefix("turn:"),
            "request_index": request_index, "status": status, "prediction": output,
            "validation_error": validation_error, "provider_error": None,
            "usage": values, "returned_model": body.get("model"),
            "provider_response": item,
        })

    pricing = load_pricing(ROOT / "config/llm_eval/pricing.yaml")
    entry = pricing["models"]["openai:gpt-6-luna"]["modes"]["batch"]
    input_cost = token_totals["input_tokens"] * float(entry["input_per_million_usd"]) / 1_000_000
    cached_cost = token_totals["cached_input_tokens"] * float(entry["cached_input_per_million_usd"]) / 1_000_000
    output_cost = token_totals["output_tokens"] * float(entry["output_per_million_usd"]) / 1_000_000
    total_cost = input_cost + cached_cost + output_cost
    coverage = {
        "planned_requests": len(planned_id_map), "provider_request_count": len(responses),
        "collected_responses": len(responses), "missing_responses": len(planned_id_map) - len(responses),
        "duplicate_mappings": duplicate_count,
        "successful_provider_responses": provider_successes,
        "failed_provider_responses": provider_failures,
        "valid_structured_predictions": sum(row["status"] == "success" for row in rows),
        "invalid_structured_predictions": invalid,
        "evidence_resolution_failures": evidence_failures,
        "accounting_invariant_ok": len(responses) + len(planned_id_map) - len(responses) == len(planned_id_map) and duplicate_count == 0,
    }
    summary = {
        "provenance": {"provider": "openai", "model": "gpt-6-luna", "provider_batch_id": chunk["provider_job_id"], "input_file_sha256": chunk["provider_input_file_sha256"], "output_file_id": output_id, "error_file_id": error_id, "output_file_sha256": hashlib.sha256(output_bytes).hexdigest() if output_bytes else None, "error_file_sha256": hashlib.sha256(error_bytes).hexdigest() if error_bytes else None},
        "batch_status": batch,
        "coverage": coverage,
        "tokens": token_totals,
        "cost": {"batch_uncached_input_cost_usd": input_cost, "cached_input_cost_usd": cached_cost, "output_including_reasoning_cost_usd": output_cost, "token_processing_cost_usd": total_cost, "explicit_cache_storage_cost_usd": 0.0, "total_pilot_cost_usd": total_cost, "cost_per_valid_annotation_usd": total_cost / coverage["valid_structured_predictions"] if coverage["valid_structured_predictions"] else None},
    }
    (directory / "provider_response.json").write_text(json.dumps({"batch": batch, "output_file_sha256": summary["provenance"]["output_file_sha256"], "error_file_sha256": summary["provenance"]["error_file_sha256"]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rows, summary


def configured_orchestration_limits() -> tuple[int, float, int]:
    ceiling = int(
        os.getenv(
            "HANSARD_GEMINI_ENQUEUED_TOKEN_CEILING",
            str(DEFAULT_ENQUEUED_TOKEN_CEILING),
        )
    )
    safety = float(
        os.getenv("HANSARD_GEMINI_ENQUEUED_TOKEN_SAFETY_FACTOR", str(DEFAULT_SAFETY_FACTOR))
    )
    maximum = int(
        os.getenv("HANSARD_GEMINI_MAX_CHUNK_RECORDS", str(DEFAULT_MAX_CHUNK_RECORDS))
    )
    if ceiling < 1 or not 0 < safety <= 1 or maximum < 1:
        raise ValueError("invalid Phase 4E orchestration limits")
    return ceiling, safety, maximum


def estimate_record_input_tokens(text: str) -> int:
    """Conservative deterministic full-input estimate, including the cached prefix."""
    return INVARIANT_INPUT_TOKENS + max(1, math.ceil(len(text.encode("utf-8")) / CHARACTERS_PER_TOKEN))


def _luna_input_tokens(record: dict[str, Any], system_prompt: str) -> int:
    payload = system_prompt + "\n" + build_user_prompt(record)
    return max(1, math.ceil(len(payload.encode("utf-8")) / CHARACTERS_PER_TOKEN))


def _luna_prepared_requests(
    campaign: dict[str, Any], records: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], ...]:
    configuration = _campaign_configuration(campaign)
    if configuration.configuration_id != LUNA_PRODUCTION_CONFIGURATION_ID:
        raise CampaignError("OpenAI batch preparation is restricted to frozen Luna production")
    context = _configuration_context(configuration)
    system_prompt = build_system_prompt(context, configuration.prompt_version)
    if prompt_hash(system_prompt) != configuration.prompt_sha256:
        raise CampaignError("frozen Luna prompt hash differs from the approved configuration")
    schema = hostility_schema(HOSTILITY_LUNA_PROMPT_VERSION)
    if canonical_hash(schema) != configuration.prediction_contract_sha256:
        raise CampaignError("frozen Luna output contract hash differs")
    if provider_prediction_schema_hash("openai", schema) != configuration.provider_schema_sha256:
        raise CampaignError("frozen Luna provider schema hash differs")
    return tuple(
        luna_batch_request(
            record_id=str(record["record_id"]),
            user_prompt=build_user_prompt(record),
            system_prompt=system_prompt,
            schema=schema,
            generation=configuration.generation_config,
        )
        for record in records
    )


def deterministic_chunks(
    records: Iterable[dict[str, Any]],
    *,
    max_records: int,
    max_input_tokens: int,
) -> Iterator[ChunkSpec]:
    """Partition ordered metadata without retaining more than one chunk of IDs."""
    if max_records < 1 or max_input_tokens < 1:
        raise ValueError("chunk limits must be positive")
    chunk_number = 1
    ids: list[str] = []
    first = -1
    last = -1
    tokens = 0

    def emit() -> ChunkSpec:
        return ChunkSpec(
            chunk_number=chunk_number,
            first_selection_order=first,
            last_selection_order=last,
            record_count=len(ids),
            record_set_sha256=canonical_hash(ids),
            estimated_input_tokens=tokens,
        )

    for expected_order, record in enumerate(records):
        order = int(record["selection_order"])
        if order != expected_order:
            raise CampaignError("campaign record ordering has a gap or duplicate")
        record_id = str(record["record_id"])
        estimated = int(record["estimated_input_tokens"])
        if estimated > max_input_tokens:
            raise CampaignError(
                f"record {record_id} exceeds the configured safe enqueued-token ceiling"
            )
        if ids and (len(ids) >= max_records or tokens + estimated > max_input_tokens):
            yield emit()
            chunk_number += 1
            ids = []
            tokens = 0
            first = -1
        if not ids:
            first = order
        ids.append(record_id)
        last = order
        tokens += estimated
    if ids:
        yield emit()


def active_estimated_tokens(chunks: Iterable[dict[str, Any]]) -> int:
    return sum(
        int(chunk["estimated_input_tokens"])
        for chunk in chunks
        if chunk.get("status") in ACTIVE_BATCH_STATUSES
    )


def submission_fits(
    *, active_tokens: int, candidate_tokens: int, ceiling: int, safety_factor: float
) -> bool:
    safe_ceiling = math.floor(ceiling * safety_factor)
    return active_tokens + candidate_tokens <= safe_ceiling


def committed_spend(
    *,
    chunks: Iterable[dict[str, Any]],
    campaign_estimate: Decimal,
    planned_count: int,
) -> Decimal:
    """Return completed actual cost plus conservative reservations for unfinished work."""
    if planned_count < 1:
        raise ValueError("planned count must be positive")

    def allocation(record_count: int) -> Decimal:
        return campaign_estimate * Decimal(record_count) / Decimal(planned_count)

    committed = Decimal(0)
    for chunk in chunks:
        status = chunk.get("status")
        if status == "PENDING":
            continue
        reserved = allocation(int(chunk["record_count"]))
        actual = Decimal(str(chunk.get("actual_cost_usd") or 0))
        committed += actual if status == "COLLECTED" else max(reserved, actual)
    return committed


def projected_spend_with_candidate(
    *,
    chunks: Iterable[dict[str, Any]],
    campaign_estimate: Decimal,
    planned_count: int,
    candidate_record_count: int,
) -> Decimal:
    """Return committed spend plus the pending candidate's reserved allocation."""
    return committed_spend(
        chunks=chunks,
        campaign_estimate=campaign_estimate,
        planned_count=planned_count,
    ) + campaign_estimate * Decimal(candidate_record_count) / Decimal(planned_count)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _child_artifact_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in (
        "plan.json",
        "requests.jsonl",
        "provider_response.json",
        "predictions.jsonl",
        "failures.jsonl",
        "summary.json",
    ):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        with (directory / name).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _ordered_chunk_rows(directory: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(directory / "predictions.jsonl")
    rows.sort(key=lambda row: int(row.get("request_index", -1)))
    return rows


class _RecordSetHasher:
    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self._count = 0

    def add(self, record_id: str) -> None:
        if self._count:
            self._digest.update(b",")
        self._digest.update(
            json.dumps(record_id, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        self._count += 1

    def hexdigest(self) -> str:
        digest = self._digest.copy()
        digest.update(b"]")
        return digest.hexdigest()


def build_campaign_effective_artifact(
    *,
    chunk_directories: Iterable[Path],
    expected_record_ids: Iterable[str],
    output_directory: Path,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Stream a verified campaign-level effective artifact from bounded child chunks."""
    if output_directory.exists():
        raise CampaignError("campaign effective artifact already exists")
    output_directory.mkdir(parents=True)
    prediction_path = output_directory / "predictions.jsonl"
    repair_path = output_directory / "structural_repairs.jsonl"
    manifest: list[dict[str, Any]] = []
    counts = {
        "planned": 0,
        "original_valid": 0,
        "structurally_repaired": 0,
        "provider_refused": 0,
        "unresolved_invalid": 0,
        "provider_failed": 0,
        "missing": 0,
        "duplicate": 0,
    }
    tokens = {key: 0 for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")}
    costs = {"token_processing_cost_usd": 0.0, "explicit_cache_storage_cost_usd": 0.0, "total_pilot_cost_usd": 0.0}
    expected = iter(expected_record_ids)
    record_set_hasher = _RecordSetHasher()
    domain_codes = _production_domain_codes()
    exhausted = object()
    with prediction_path.open("w", encoding="utf-8", newline="\n") as predictions, repair_path.open(
        "w", encoding="utf-8", newline="\n"
    ) as repairs:
        for chunk_number, directory in enumerate(chunk_directories, 1):
            summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
            rows = _ordered_chunk_rows(directory)
            planned_in_chunk = int(summary.get("coverage", {}).get("planned_requests") or 0)
            manifest.append(
                {
                    "chunk_number": chunk_number,
                    "artifact_reference": str(directory),
                    "artifact_sha256": _child_artifact_sha256(directory),
                    "record_count": len(rows),
                }
            )
            for key in tokens:
                tokens[key] += int(summary.get("tokens", {}).get(key) or 0)
            for key in costs:
                costs[key] += float(summary.get("cost", {}).get(key) or 0)
            indexed: dict[int, list[dict[str, Any]]] = {}
            for row in rows:
                index = row.get("request_index")
                if isinstance(index, int):
                    indexed.setdefault(index, []).append(row)
                else:
                    counts["duplicate"] += 1
            counts["duplicate"] += sum(
                len(values)
                for index, values in indexed.items()
                if index < 0 or index >= planned_in_chunk
            )
            for local_index in range(planned_in_chunk):
                expected_id = next(expected, exhausted)
                if expected_id is exhausted:
                    counts["duplicate"] += 1
                    continue
                counts["planned"] += 1
                record_set_hasher.add(str(expected_id))
                candidates = indexed.get(local_index, [])
                if not candidates:
                    counts["missing"] += 1
                    predictions.write(
                        json.dumps(
                            {
                                "record_id": expected_id,
                                "request_index": counts["planned"] - 1,
                                "chunk_number": chunk_number,
                                "chunk_request_index": local_index,
                                "status": "missing",
                                "prediction": None,
                                "provider_error": "provider response missing",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    continue
                if len(candidates) > 1:
                    counts["duplicate"] += len(candidates) - 1
                row = candidates[0]
                actual_id = row.get("record_id")
                if actual_id != expected_id:
                    counts["missing"] += 1
                    counts["duplicate"] += 1
                    predictions.write(
                        json.dumps(
                            {
                                "record_id": expected_id,
                                "request_index": counts["planned"] - 1,
                                "chunk_number": chunk_number,
                                "chunk_request_index": local_index,
                                "status": "missing",
                                "prediction": None,
                                "provider_error": "provider response mapped to another record",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    continue
                global_request_index = counts["planned"] - 1
                repaired_rows, lineage = apply_structural_repairs(
                    [row], domain_codes
                )
                effective = repaired_rows[0]
                effective["chunk_number"] = chunk_number
                effective["chunk_request_index"] = effective.get("request_index")
                effective["request_index"] = global_request_index
                if lineage:
                    counts["structurally_repaired"] += 1
                    repairs.write(json.dumps(lineage[0], ensure_ascii=False, sort_keys=True) + "\n")
                elif effective.get("status") == "success":
                    counts["original_valid"] += 1
                elif effective.get("status") == "provider_failure":
                    counts["provider_failed"] += 1
                elif effective.get("status") == "provider_refusal":
                    counts["provider_refused"] += 1
                else:
                    counts["unresolved_invalid"] += 1
                predictions.write(json.dumps(effective, ensure_ascii=False, sort_keys=True) + "\n")
        for _missing_id in expected:
            counts["planned"] += 1
            counts["missing"] += 1
            record_set_hasher.add(str(_missing_id))
            predictions.write(
                json.dumps(
                    {
                        "record_id": str(_missing_id),
                        "request_index": counts["planned"] - 1,
                        "status": "missing",
                        "prediction": None,
                        "provider_error": "no child Batch range covered this record",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    valid = counts["original_valid"] + counts["structurally_repaired"]
    complete = (
        valid + counts["provider_refused"] == counts["planned"]
        and not counts["unresolved_invalid"]
        and not counts["provider_failed"]
        and not counts["missing"]
        and not counts["duplicate"]
    )
    summary = {
        "provenance": {
            **provenance,
            "structural_repair_rules": [STRUCTURAL_REPAIR_RULE],
            "record_ids_sha256": record_set_hasher.hexdigest(),
        },
        "coverage": {
            "planned_requests": counts["planned"],
            "valid_structured_predictions": valid,
            "invalid_structured_predictions": counts["unresolved_invalid"],
            "successful_provider_responses": counts["original_valid"] + counts["structurally_repaired"] + counts["unresolved_invalid"],
            "failed_provider_responses": counts["provider_failed"],
            "collected_responses": counts["planned"] - counts["missing"],
            "missing_responses": counts["missing"],
            "duplicate_mappings": counts["duplicate"],
            "accounting_invariant_ok": complete,
            "valid": counts["original_valid"],
            "invalid": counts["unresolved_invalid"],
            "failed": counts["provider_failed"],
            "resolved_records": valid + counts["provider_refused"],
            **counts,
        },
        "tokens": tokens,
        "cost": costs,
        "batch_status": {"model": provenance.get("model"), "state": "CAMPAIGN_AGGREGATED"},
        "structural_repair": {
            "rule_id": STRUCTURAL_REPAIR_RULE,
            "repaired_count": counts["structurally_repaired"],
            "lineage_file": repair_path.name,
            "lineage_sha256": _hash_file(repair_path),
        },
    }
    (output_directory / "manifest.json").write_text(
        json.dumps({"chunks": manifest}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_directory / "operational_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary["artifact_sha256"] = artifact_sha256(output_directory)
    return summary


def _require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise PermissionError("administrator or research access required")


def plan_campaign_chunks(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    enqueued_token_ceiling: int,
    safety_factor: float,
    max_chunk_records: int,
) -> dict[str, Any]:
    """Persist deterministic contiguous ranges using a server-side cursor."""
    _require_admin(principal)
    safe_tokens = math.floor(enqueued_token_ceiling * safety_factor)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign is None:
            raise LookupError("AI annotation campaign not found")
        if campaign["orchestration_mode"] == "CHUNKED":
            return dict(campaign)
        selection = campaign.get("selection_definition")
        if campaign["status"] != "PLANNED" or campaign["orchestration_mode"] != "LEGACY":
            raise CampaignError("only a newly planned legacy campaign can be chunked")
        if not isinstance(selection, dict) or selection.get("exclude_human_annotated") is not False:
            raise CampaignError("full production requires human-annotation exclusion OFF")
        if configuration_class(dict(campaign)) != "APPROVED_PRODUCTION":
            raise CampaignError("full production requires the approved frozen configuration")
        configuration = _campaign_configuration(dict(campaign))
        is_luna = configuration.configuration_id == LUNA_PRODUCTION_CONFIGURATION_ID
        if configuration.prediction_task != "policy" and not is_luna:
            raise CampaignError("full production orchestration is only supported for policy campaigns")
        if is_luna and campaign["cache_status"] != "NOT_REQUIRED":
            raise CampaignError("Luna production must not use a Gemini cache")

        def records() -> Iterator[dict[str, Any]]:
            with connection.cursor(name=f"phase4e_chunks_{campaign['id']}") as cursor:
                cursor.itersize = INSERT_BATCH_SIZE
                cursor.execute(
                    """
                    SELECT record.selection_order,record.record_id,
                           COALESCE(turn.text_clean,turn.text_raw,'') AS prompt_text
                    FROM ai_annotation_campaign_records record
                    JOIN speaker_turns turn ON turn.id=record.speaker_turn_id
                    WHERE record.campaign_id=%s ORDER BY record.selection_order
                    """,
                    (campaign["id"],),
                )
                for value in cursor:
                    row = dict(value)
                    input_tokens = estimate_record_input_tokens(str(row["prompt_text"]))
                    yield {
                        "selection_order": row["selection_order"],
                        "record_id": row["record_id"],
                        "estimated_input_tokens": input_tokens,
                    }

        chunks = deterministic_chunks(
            records(), max_records=max_chunk_records, max_input_tokens=safe_tokens
        )
        count = 0
        estimated = 0
        for chunk in chunks:
            count += 1
            estimated += chunk.estimated_input_tokens
            batch = connection.execute(
                """
                INSERT INTO ai_annotation_campaign_batches
                  (campaign_id,chunk_number,first_selection_order,last_selection_order,
                   record_count,record_set_sha256,estimated_input_tokens,planner_version,
                   provider,configured_model,prompt_version,prompt_sha256,
                   canonical_schema_sha256,prediction_contract_sha256,
                   provider_schema_sha256,generation_config,execution_mode,
                   submission_display_name)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    campaign["id"], chunk.chunk_number, chunk.first_selection_order,
                    chunk.last_selection_order, chunk.record_count, chunk.record_set_sha256,
                    chunk.estimated_input_tokens, CHUNK_PLANNER_VERSION, campaign["provider"],
                    campaign["configured_model"], campaign["prompt_version"],
                    campaign["prompt_sha256"], campaign["canonical_schema_sha256"],
                    campaign["prediction_contract_sha256"], campaign["provider_schema_sha256"],
                    Jsonb(campaign["generation_config"]), campaign["execution_mode"],
                    f"phase4e-{public_id}-chunk-{chunk.chunk_number:06d}",
                ),
            ).fetchone()
            if batch is None:
                raise CampaignError("child Batch preparation did not persist")
            if is_luna:
                chunk_records = _records_for_chunk(connection, dict(batch))
                requests = _luna_prepared_requests(dict(campaign), chunk_records)
                directory = _chunk_directory(public_id, chunk.chunk_number)
                request_bytes = b"".join(
                    (json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                    for item in requests
                )
                if len(requests) > MAX_OPENAI_BATCH_REQUESTS:
                    raise CampaignError("prepared Luna Batch exceeds OpenAI's request-count limit")
                if len(request_bytes) > MAX_OPENAI_BATCH_FILE_BYTES:
                    raise CampaignError("prepared Luna Batch exceeds OpenAI's input-file size limit")
                directory.mkdir(parents=True, exist_ok=True)
                request_path = directory / "requests.jsonl"
                plan_path = directory / "plan.json"
                if request_path.exists() and request_path.read_bytes() != request_bytes:
                    raise CampaignError("prepared Luna request file conflicts with this chunk")
                request_path.write_bytes(request_bytes)
                request_sha256 = _hash_file(request_path)
                plan = {
                    "provider": "openai",
                    "endpoint": "/v1/responses",
                    "campaign_public_id": public_id,
                    "chunk_number": chunk.chunk_number,
                    "record_count": chunk.record_count,
                    "record_set_sha256": chunk.record_set_sha256,
                    "request_file_sha256": request_sha256,
                    "provenance": {
                        key: campaign[key]
                        for key in (
                            "provider", "configured_model", "prompt_version", "prompt_sha256",
                            "canonical_schema_sha256", "prediction_contract_sha256",
                            "provider_schema_sha256", "generation_config", "execution_mode",
                        )
                    },
                }
                plan_bytes = (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode("utf-8")
                if plan_path.exists() and plan_path.read_bytes() != plan_bytes:
                    raise CampaignError("prepared Luna plan conflicts with this chunk")
                plan_path.write_bytes(plan_bytes)
                connection.execute(
                    "UPDATE ai_annotation_campaign_batches "
                    "SET provider_input_file_sha256=%s WHERE id=%s",
                    (request_sha256, batch["id"]),
                )
        if not count:
            raise CampaignError("campaign contains no records to chunk")
        if is_luna:
            pricing = load_pricing(ROOT / "config/llm_eval/pricing.yaml")
            rates = pricing["models"]["openai:gpt-6-luna"]["modes"]["batch"]
            output_estimate = int(campaign["planned_count"]) * 150
            input_cost = estimated * float(rates["input_per_million_usd"]) / 1_000_000
            output_cost = output_estimate * float(rates["output_per_million_usd"]) / 1_000_000
            processing_estimate = input_cost + output_cost
            estimate_note = "Prepared request bytes estimated at UTF-8 bytes/3; output planning allowance 150 tokens/record; actual charges use returned provider usage."
        else:
            output_estimate = int(campaign["estimated_output_tokens"] or 0)
            processing_estimate = campaign["estimated_processing_cost_usd"]
            estimate_note = None
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET orchestration_mode='CHUNKED',
              orchestration_status='PLANNED',chunk_planner_version=%s,
              max_chunk_records=%s,enqueued_token_ceiling=%s,
              enqueued_token_safety_factor=%s,
              estimated_input_tokens=%s,estimated_cached_input_tokens=%s,
              estimated_uncached_input_tokens=%s,estimated_output_tokens=%s,
              estimated_processing_cost_usd=%s,estimated_cache_storage_cost_usd=%s,
              estimated_total_cost_usd=%s,
              operational_metadata=operational_metadata || %s
            WHERE id=%s RETURNING *
            """,
            (
                CHUNK_PLANNER_VERSION, max_chunk_records, enqueued_token_ceiling,
                safety_factor,
                estimated if is_luna else campaign["estimated_input_tokens"],
                0 if is_luna else campaign["estimated_cached_input_tokens"],
                estimated if is_luna else campaign["estimated_uncached_input_tokens"],
                output_estimate,
                processing_estimate,
                0 if is_luna else campaign["estimated_cache_storage_cost_usd"],
                processing_estimate if is_luna else campaign["estimated_total_cost_usd"],
                Jsonb({"phase4e": {"chunk_count": count,
                    "estimated_full_input_tokens": estimated,
                    "safe_active_token_ceiling": safe_tokens,
                    "estimate_method": estimate_note,
                    "pricing_effective_date": pricing.get("effective_date") if is_luna else None}}), campaign["id"],
            ),
        ).fetchone()
        assert updated is not None
        return dict(updated)


def authorize_full_production(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    confirmation: str,
    spend_ceiling_usd: Decimal,
) -> dict[str, Any]:
    _require_admin(principal)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign is None:
            raise LookupError("AI annotation campaign not found")
        if campaign["orchestration_status"] != "PLANNED" or campaign["authorized_at"] is not None:
            raise CampaignError("campaign is not awaiting full-production authorization")
        configuration = _campaign_configuration(dict(campaign))
        expected_confirmation = (
            LUNA_PRODUCTION_CONFIRMATION
            if configuration.configuration_id == LUNA_PRODUCTION_CONFIGURATION_ID
            else FULL_PRODUCTION_CONFIRMATION
        )
        if confirmation != expected_confirmation:
            raise CampaignError(f"authorization requires typing {expected_confirmation}")
        if configuration.prediction_task != "policy" and (
            configuration.configuration_id != LUNA_PRODUCTION_CONFIGURATION_ID
        ):
            raise CampaignError("full production orchestration is only supported for policy campaigns")
        estimate = Decimal(str(campaign["estimated_total_cost_usd"] or 0))
        if spend_ceiling_usd < estimate:
            raise CampaignError("authorization ceiling is below the current campaign estimate")
        identity = {
            key: campaign[key]
            for key in (
                "provider", "configured_model", "prompt_version", "prompt_sha256",
                "canonical_schema_sha256", "prediction_contract_sha256",
                "provider_schema_sha256", "generation_config", "execution_mode",
            )
        }
        identity["authorized_estimates"] = {
            "record_count": int(campaign["planned_count"]),
            "processing_cost_usd": str(
                campaign["estimated_processing_cost_usd"] or 0
            ),
            "cache_storage_cost_usd": str(
                campaign["estimated_cache_storage_cost_usd"] or 0
            ),
            "total_cost_usd": str(estimate),
        }
        identity["record_set_sha256"] = campaign["record_set_sha256"]
        identity["corpus_run_name"] = "phase1-full-20260723-v4" if configuration.configuration_id == LUNA_PRODUCTION_CONFIGURATION_ID else None
        updated = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET status='READY',orchestration_status='RUNNING',
              authorized_by_user_id=%s,authorized_at=now(),authorized_record_count=planned_count,
              authorized_spend_ceiling_usd=%s,authorized_configuration=%s
            WHERE id=%s AND authorized_at IS NULL RETURNING *
            """,
            (principal.user_id, spend_ceiling_usd, Jsonb(identity), campaign["id"]),
        ).fetchone()
        if updated is None:
            raise CampaignError("campaign authorization was already recorded")
        return dict(updated)


def set_orchestration_paused(
    settings: WebSettings, principal: Principal, public_id: str, *, paused: bool
) -> dict[str, Any]:
    _require_admin(principal)
    expected = "RUNNING" if paused else "PAUSED"
    target = "PAUSED" if paused else "RUNNING"
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            UPDATE ai_annotation_campaigns SET orchestration_status=%s,
              paused_at=CASE WHEN %s THEN now() ELSE NULL END
            WHERE public_id::text=%s AND orchestration_mode='CHUNKED'
              AND orchestration_status=%s RETURNING *
            """,
            (target, paused, public_id, expected),
        ).fetchone()
        if row is None:
            raise CampaignError("campaign orchestration cannot make that pause transition")
        return dict(row)


def refresh_campaign_batch_status(
    settings: WebSettings,
    principal: Principal,
    public_id: str,
    *,
    gateway: OpenAIBatchGateway | None = None,
) -> int:
    """Refresh already-submitted OpenAI batches; this function cannot submit or retry."""
    _require_admin(principal)
    provider = gateway or OpenAIBatchGateway()
    refreshed = 0
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign = connection.execute(
            "SELECT id,configuration_id FROM ai_annotation_campaigns WHERE public_id::text=%s",
            (public_id,),
        ).fetchone()
        if campaign is None:
            raise LookupError("AI annotation campaign not found")
        if campaign["configuration_id"] != LUNA_PRODUCTION_CONFIGURATION_ID:
            raise CampaignError("read-only Batch refresh is available for Luna production campaigns")
        chunks = connection.execute(
            """SELECT id,chunk_number,provider_job_id FROM ai_annotation_campaign_batches
               WHERE campaign_id=%s AND status IN ('SUBMITTED','RUNNING')
               ORDER BY chunk_number""",
            (campaign["id"],),
        ).fetchall()
        for chunk in chunks:
            batch = provider.retrieve(str(chunk["provider_job_id"]))
            state = str(batch.get("status") or "unknown")
            terminal = state in {"completed", "failed", "cancelled", "expired"}
            target = "PROVIDER_COMPLETED" if terminal else "RUNNING" if state == "in_progress" else "SUBMITTED"
            counts = batch.get("request_counts")
            counts = counts if isinstance(counts, dict) else {}
            connection.execute(
                """UPDATE ai_annotation_campaign_batches SET status=%s,provider_state=%s,
                   provider_success_count=%s,provider_failure_count=%s,
                   provider_output_file_id=COALESCE(provider_output_file_id,%s),
                   provider_error_file_id=COALESCE(provider_error_file_id,%s),
                   provider_completed_at=CASE WHEN %s THEN COALESCE(provider_completed_at,now()) ELSE provider_completed_at END,
                   failure_metadata=CASE WHEN %s THEN failure_metadata || %s ELSE failure_metadata END
                   WHERE id=%s""",
                (target, state, int(counts.get("completed") or 0), int(counts.get("failed") or 0),
                 batch.get("output_file_id"), batch.get("error_file_id"), terminal,
                 state in {"failed", "cancelled", "expired"}, Jsonb({"provider_error": batch.get("errors")}), chunk["id"]),
            )
            refreshed += 1
    return refreshed


def _records_for_chunk(
    connection: psycopg.Connection[dict[str, Any]], chunk: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        """
        SELECT turn.turn_key,turn.speech_date AS date,turn.chamber,turn.speaker_name_raw,
               turn.major_heading_original,turn.minor_heading_original,turn.business_type,
               turn.text_clean,turn.text_raw
        FROM ai_annotation_campaign_records record
        JOIN speaker_turns turn ON turn.id=record.speaker_turn_id
        WHERE record.campaign_id=%s AND record.selection_order BETWEEN %s AND %s
        ORDER BY record.selection_order
        """,
        (chunk["campaign_id"], chunk["first_selection_order"], chunk["last_selection_order"]),
    ).fetchall()
    return tuple(production_record_from_turn(dict(row)) for row in rows)


def _chunk_directory(public_id: str, chunk_number: int) -> Path:
    root = _artifact_directory(public_id) / "chunks"
    target = (root / f"chunk-{chunk_number:06d}").resolve()
    if target.parent != root.resolve():
        raise CampaignError("unsafe campaign chunk artifact path")
    return target


def _is_quota_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(value in message for value in ("429", "quota", "capacity", "resource_exhausted"))


def _campaign_committed_spend(
    connection: psycopg.Connection[dict[str, Any]], campaign: dict[str, Any]
) -> Decimal:
    rows = connection.execute(
        """
        SELECT status,record_count,
               processing_cost_usd+cache_cost_usd AS actual_cost_usd
        FROM ai_annotation_campaign_batches WHERE campaign_id=%s
        """,
        (campaign["id"],),
    ).fetchall()
    return committed_spend(
        chunks=(dict(row) for row in rows),
        campaign_estimate=Decimal(str(campaign["estimated_total_cost_usd"] or 0)),
        planned_count=int(campaign["planned_count"]),
    )


def _cache_receipt_for_chunk(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """Reconstruct storage-billing provenance only for a cache created by this child."""
    metadata = chunk.get("cache_metadata")
    if not isinstance(metadata, dict) or not metadata.get("created_for_phase4e_chunk"):
        return None
    return {
        "response": {
            "createTime": str(chunk["cache_created_at"]),
            "expireTime": str(chunk["cache_expires_at"]),
            "usageMetadata": metadata.get("usageMetadata", {}),
        }
    }


def _finalize_main_pass(
    connection: psycopg.Connection[dict[str, Any]], campaign: dict[str, Any]
) -> dict[str, Any]:
    chunks = connection.execute(
        """
        SELECT chunk_number,artifact_reference FROM ai_annotation_campaign_batches
        WHERE campaign_id=%s ORDER BY chunk_number
        """,
        (campaign["id"],),
    ).fetchall()
    if not chunks or any(not row.get("artifact_reference") for row in chunks):
        raise CampaignError("not every child Batch has an immutable artifact")

    def expected_ids() -> Iterator[str]:
        with connection.cursor(name=f"phase4e_effective_{campaign['id']}") as cursor:
            cursor.itersize = INSERT_BATCH_SIZE
            cursor.execute(
                """
                SELECT record_id FROM ai_annotation_campaign_records
                WHERE campaign_id=%s ORDER BY selection_order
                """,
                (campaign["id"],),
            )
            for row in cursor:
                yield str(row["record_id"])

    pricing = load_pricing(ROOT / "config/llm_eval/pricing.yaml")
    pricing_entry = pricing["models"][f"{campaign['provider']}:{campaign['configured_model']}"]
    provenance = {
        "provider": campaign["provider"],
        "model": campaign["configured_model"],
        "prompt_version": campaign["prompt_version"],
        "assembled_prompt_sha256": campaign["prompt_sha256"],
        "canonical_schema_sha256": campaign["canonical_schema_sha256"],
        "prediction_contract_sha256": campaign["prediction_contract_sha256"],
        "provider_schema_sha256": campaign["provider_schema_sha256"],
        **campaign["generation_config"],
        "execution_mode": campaign["execution_mode"],
        "batch_id": f"phase4e:{campaign['public_id']}",
        "cached_content": None,
        "pricing_format_version": 1,
        "pricing_effective_date": pricing_entry.get(
            "effective_date", pricing.get("effective_date")
        ),
    }
    output = _artifact_directory(str(campaign["public_id"])) / "effective"
    if output.exists():
        try:
            summary = json.loads(
                (output / "operational_summary.json").read_text(encoding="utf-8")
            )
            if not isinstance(summary, dict):
                raise TypeError("summary is not an object")
            summary["artifact_sha256"] = artifact_sha256(output)
        except (OSError, ValueError, TypeError) as error:
            raise CampaignError(
                "existing campaign effective artifact is incomplete or invalid"
            ) from error
    else:
        summary = build_campaign_effective_artifact(
            chunk_directories=(Path(str(row["artifact_reference"])) for row in chunks),
            expected_record_ids=expected_ids(),
            output_directory=output,
            provenance=provenance,
        )
    if summary["provenance"]["record_ids_sha256"] != campaign["record_set_sha256"]:
        raise CampaignError("campaign-level effective record identity hash does not match plan")
    coverage = summary["coverage"]
    target = "COMPLETE" if coverage["accounting_invariant_ok"] else "RECOVERY_REQUIRED"
    updated = connection.execute(
        """
        UPDATE ai_annotation_campaigns SET status='COLLECTED',
          orchestration_status=%s,artifact_reference=%s,artifact_sha256=%s,
          collected_at=now(),provider_success_count=%s,provider_failure_count=%s,
          valid_count=%s,invalid_count=%s,operational_metadata=%s
        WHERE id=%s RETURNING *
        """,
        (
            target,
            str(output),
            summary["artifact_sha256"],
            coverage["successful_provider_responses"],
            coverage["failed_provider_responses"],
            coverage["valid_structured_predictions"],
            max(
                0,
                int(coverage["planned_requests"])
                - int(coverage["valid_structured_predictions"])
                - int(coverage.get("provider_refused") or 0),
            ),
            Jsonb(
                {
                    "coverage": coverage,
                    "tokens": summary["tokens"],
                    "cost": summary["cost"],
                    "phase4e_manifest": "manifest.json",
                }
            ),
            campaign["id"],
        ),
    ).fetchone()
    assert updated is not None
    return dict(updated)


def _orchestration_tick_locked(
    settings: WebSettings,
    public_id: str,
    *,
    gateway: Any | None = None,
) -> TickResult:
    """Advance one state while the caller holds this campaign's session lock.

    Call only through :func:`orchestration_tick`; row locks below protect each
    transaction while that outer advisory lock spans provider operations.
    """
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign_row = connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE public_id::text=%s FOR UPDATE",
            (public_id,),
        ).fetchone()
        if campaign_row is None:
            raise LookupError("AI annotation campaign not found")
        campaign = dict(campaign_row)
        if campaign["orchestration_mode"] != "CHUNKED" or campaign["authorized_at"] is None:
            raise CampaignError("campaign is not authorized for chunk orchestration")
        if campaign["orchestration_status"] == "PAUSED":
            return TickResult("paused", public_id)
        if campaign["orchestration_status"] not in {"RUNNING", "MAIN_COMPLETE"}:
            return TickResult("idle", public_id)
        chunk_row = connection.execute(
            """
            SELECT * FROM ai_annotation_campaign_batches
            WHERE campaign_id=%s AND status IN
              ('PROVIDER_COMPLETED','SUBMITTING')
            ORDER BY CASE status WHEN 'PROVIDER_COMPLETED' THEN 0 ELSE 1 END,
                     chunk_number LIMIT 1 FOR UPDATE
            """,
            (campaign["id"],),
        ).fetchone()
        if chunk_row is None and campaign["orchestration_status"] == "RUNNING":
            active = connection.execute(
                """
                SELECT COALESCE(sum(estimated_input_tokens),0) AS tokens
                FROM ai_annotation_campaign_batches
                WHERE campaign_id=%s AND status IN
                  ('SUBMITTING','SUBMITTED','RUNNING','PROVIDER_COMPLETED')
                """,
                (campaign["id"],),
            ).fetchone()
            pending = connection.execute(
                """
                SELECT * FROM ai_annotation_campaign_batches
                WHERE campaign_id=%s AND status='PENDING'
                ORDER BY chunk_number LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                (campaign["id"],),
            ).fetchone()
            wait_action: str | None = None
            if pending is not None:
                active_tokens = int(active["tokens"] if active else 0)
                if not submission_fits(
                    active_tokens=active_tokens,
                    candidate_tokens=int(pending["estimated_input_tokens"]),
                    ceiling=int(campaign["enqueued_token_ceiling"]),
                    safety_factor=float(campaign["enqueued_token_safety_factor"]),
                ):
                    wait_action = "quota-wait"
                else:
                    projected = _campaign_committed_spend(
                        connection, campaign
                    ) + Decimal(
                        str(campaign["estimated_total_cost_usd"] or 0)
                    ) * Decimal(int(pending["record_count"])) / Decimal(
                        int(campaign["planned_count"])
                    )
                    if projected > Decimal(str(campaign["authorized_spend_ceiling_usd"])):
                        wait_action = "spend-ceiling"
                    else:
                        connection.execute(
                            """
                            UPDATE ai_annotation_campaign_batches SET status='SUBMITTING',
                              submission_started_at=COALESCE(submission_started_at,now())
                            WHERE id=%s AND status='PENDING'
                            """,
                            (pending["id"],),
                        )
                        return TickResult("reserved", public_id, int(pending["chunk_number"]))
            active_row = connection.execute(
                """
                SELECT * FROM ai_annotation_campaign_batches
                WHERE campaign_id=%s AND status IN ('SUBMITTED','RUNNING')
                ORDER BY chunk_number LIMIT 1 FOR UPDATE
                """,
                (campaign["id"],),
            ).fetchone()
            if active_row is not None:
                chunk_row = active_row
            elif pending is not None and wait_action is not None:
                return TickResult(wait_action, public_id, int(pending["chunk_number"]))
            else:
                incomplete = connection.execute(
                    """
                    SELECT count(*) AS count FROM ai_annotation_campaign_batches
                    WHERE campaign_id=%s AND status<>'COLLECTED'
                    """,
                    (campaign["id"],),
                ).fetchone()
                if incomplete and int(incomplete["count"]) == 0:
                    _finalize_main_pass(connection, campaign)
                    return TickResult("main-complete", public_id)
                return TickResult("idle", public_id)
        if chunk_row is None:
            return TickResult("idle", public_id)
        chunk = dict(chunk_row)
        is_luna = campaign.get("configuration_id") == LUNA_PRODUCTION_CONFIGURATION_ID
        provider: Any = gateway if gateway is not None else (OpenAIBatchGateway() if is_luna else LiveGeminiGateway())

        if chunk["status"] in {"SUBMITTED", "RUNNING"}:
            if is_luna:
                batch = provider.retrieve(str(chunk["provider_job_id"]))
                state = str(batch.get("status") or "unknown")
                target = (
                    "PROVIDER_COMPLETED" if state in {"completed", "failed", "cancelled", "expired"}
                    else "RUNNING" if state == "in_progress"
                    else "SUBMITTED"
                )
                request_counts = batch.get("request_counts")
                request_counts = request_counts if isinstance(request_counts, dict) else {}
                connection.execute(
                    """UPDATE ai_annotation_campaign_batches SET status=%s,provider_state=%s,
                       provider_success_count=%s,provider_failure_count=%s,
                       provider_output_file_id=COALESCE(provider_output_file_id,%s),
                       provider_error_file_id=COALESCE(provider_error_file_id,%s),
                       provider_completed_at=CASE WHEN %s THEN COALESCE(provider_completed_at,now())
                         ELSE provider_completed_at END,
                       failure_metadata=CASE WHEN %s THEN failure_metadata || %s ELSE failure_metadata END
                       WHERE id=%s""",
                    (target, state, int(request_counts.get("completed") or 0),
                     int(request_counts.get("failed") or 0), batch.get("output_file_id"),
                     batch.get("error_file_id"), state in {"completed", "failed", "cancelled", "expired"},
                     state in {"failed", "cancelled", "expired"}, Jsonb({"provider_error": batch.get("errors")}), chunk["id"]),
                )
                return TickResult("refreshed", public_id, int(chunk["chunk_number"]), target)
            status = parse_batch_status(provider.status(str(chunk["provider_job_id"])))
            target = "PROVIDER_COMPLETED" if status["successful_terminal"] else (
                "FAILED" if status["terminal"] else "RUNNING"
            )
            connection.execute(
                """
                UPDATE ai_annotation_campaign_batches SET status=%s,provider_state=%s,
                  provider_success_count=%s,provider_failure_count=%s,
                  provider_completed_at=CASE WHEN %s THEN COALESCE(provider_completed_at,now())
                    ELSE provider_completed_at END,
                  failure_metadata=CASE WHEN %s THEN %s ELSE failure_metadata END
                WHERE id=%s
                """,
                (target, status["state"], status["successful_count"], status["failed_count"],
                 bool(status["terminal"]), target == "FAILED", Jsonb({"provider_error": status["error"]}), chunk["id"]),
            )
            return TickResult("refreshed", public_id, int(chunk["chunk_number"]), target)

        records = _records_for_chunk(connection, chunk)
        if len(records) != int(chunk["record_count"]):
            raise CampaignError("child Batch membership no longer matches its immutable range")

        if chunk["status"] == "PROVIDER_COMPLETED":
            directory = _chunk_directory(public_id, int(chunk["chunk_number"]))
            if is_luna:
                try:
                    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
                    regenerated = _luna_prepared_requests(campaign, records)
                    request_bytes = b"".join(
                        (json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                        for item in regenerated
                    )
                    if (
                        not isinstance(plan, dict)
                        or plan.get("record_set_sha256") != chunk["record_set_sha256"]
                        or plan.get("request_file_sha256") != chunk["provider_input_file_sha256"]
                        or _hash_file(directory / "requests.jsonl") != chunk["provider_input_file_sha256"]
                        or hashlib.sha256(request_bytes).hexdigest() != chunk["provider_input_file_sha256"]
                    ):
                        raise CampaignError("prepared Luna request identity no longer matches stored provenance")
                except (OSError, ValueError, TypeError) as error:
                    raise CampaignError("prepared Luna request artifact is missing or invalid") from error
            else:
                plan = _provider_plan(
                    campaign, records, expected_record_set_sha256=str(chunk["record_set_sha256"]),
                    cache_identifier=str(chunk["cache_identifier"]),
                    display_name=str(chunk["submission_display_name"]),
                )
            if (directory / "summary.json").exists():
                try:
                    stored_plan = json.loads(
                        (directory / "plan.json").read_text(encoding="utf-8")
                    )
                    if stored_plan != plan:
                        raise CampaignError(
                            "existing child artifact belongs to a different immutable plan"
                        )
                    summary = json.loads(
                        (directory / "summary.json").read_text(encoding="utf-8")
                    )
                    if not isinstance(summary, dict):
                        raise TypeError("summary is not an object")
                    results = _ordered_chunk_rows(directory)
                except (OSError, ValueError, TypeError) as error:
                    raise CampaignError(
                        "existing child artifact is incomplete or invalid"
                    ) from error
            else:
                if is_luna:
                    results, summary = _collect_openai_chunk(
                        cast(OpenAIBatchGateway, provider), chunk=chunk, directory=directory, records=records
                    )
                    summary["provenance"].update(plan["provenance"])
                    summary["provenance"].update({
                        "prompt_version": campaign["prompt_version"],
                        "prompt_sha256": campaign["prompt_sha256"],
                        "canonical_schema_sha256": campaign["canonical_schema_sha256"],
                        "prediction_contract_sha256": campaign["prediction_contract_sha256"],
                        "provider_schema_sha256": campaign["provider_schema_sha256"],
                        **campaign["generation_config"],
                        "execution_mode": campaign["execution_mode"],
                        "record_count": len(records),
                        "record_ids_sha256": canonical_hash([record["record_id"] for record in records]),
                    })
                else:
                    response = provider.status(str(chunk["provider_job_id"]))
                    results, coverage = collect_inline_batch(
                        batch_value=response, plan=plan,
                        domain_codes=_production_domain_codes(),
                    )
                    directory.mkdir(parents=True, exist_ok=True)
                    pricing = load_pricing(ROOT / "config/llm_eval/pricing.yaml")
                    summary = _collection_summary(
                        response, plan, results, coverage, pricing,
                        _cache_receipt_for_chunk(chunk),
                    )
                    (directory / "plan.json").write_text(
                        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                    )
                    requests = plan["request"]["batch"]["input_config"]["requests"]["requests"]
                    (directory / "requests.jsonl").write_text(
                        "".join(json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n" for request in requests),
                        encoding="utf-8",
                    )
                    (directory / "provider_response.json").write_text(
                        json.dumps(response, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                    )
                (directory / "predictions.jsonl").write_text(
                    "".join(
                        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                        for row in results
                    ),
                    encoding="utf-8",
                )
                failures = [row for row in results if row.get("status") != "success"]
                (directory / "failures.jsonl").write_text(
                    "".join(
                        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                        for row in failures
                    ),
                    encoding="utf-8",
                )
                (directory / "summary.json").write_text(
                    json.dumps(summary, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            digest = _child_artifact_sha256(directory)
            token = summary["tokens"]
            cost = summary["cost"]
            summary_coverage = summary["coverage"]
            connection.execute(
                """
                UPDATE ai_annotation_campaign_batches SET status='COLLECTED',collected_at=now(),
                  artifact_reference=%s,artifact_sha256=%s,valid_count=%s,repaired_count=%s,
                  invalid_count=%s,provider_failure_count=%s,evidence_failure_count=%s,
                  missing_count=%s,duplicate_count=%s,input_tokens=%s,cached_input_tokens=%s,
                  output_tokens=%s,reasoning_tokens=%s,processing_cost_usd=%s,cache_cost_usd=%s,
                  provider_output_file_sha256=%s,provider_error_file_sha256=%s
                WHERE id=%s AND status='PROVIDER_COMPLETED'
                """,
                (str(directory), digest, summary_coverage["valid_structured_predictions"],
                 structural_repair_count(results, _production_domain_codes()),
                 summary_coverage["invalid_structured_predictions"],
                 summary_coverage["failed_provider_responses"],
                 summary_coverage.get("evidence_resolution_failures", 0),
                 summary_coverage["missing_responses"],
                 summary_coverage["duplicate_mappings"], token["input_tokens"], token["cached_input_tokens"],
                 token["output_tokens"], token["reasoning_tokens"], cost["token_processing_cost_usd"],
                 cost["explicit_cache_storage_cost_usd"],
                 summary["provenance"].get("output_file_sha256"),
                 summary["provenance"].get("error_file_sha256"), chunk["id"]),
            )
            return TickResult("collected", public_id, int(chunk["chunk_number"]))

    # SUBMITTING is intentionally completed outside its reservation transaction.
    # Recheck the immutable ceiling immediately before any cache/provider operation;
    # completed chunks may have acquired actual cost since this child was reserved.
    with psycopg.connect(
        settings.database.psycopg_url, row_factory=dict_row
    ) as spend_connection:
        latest_campaign_row = spend_connection.execute(
            "SELECT * FROM ai_annotation_campaigns WHERE id=%s FOR UPDATE",
            (campaign["id"],),
        ).fetchone()
        if latest_campaign_row is None:
            raise CampaignError("campaign disappeared before child submission")
        latest_campaign = dict(latest_campaign_row)
        if _campaign_committed_spend(
            spend_connection, latest_campaign
        ) > Decimal(str(latest_campaign["authorized_spend_ceiling_usd"])):
            spend_connection.execute(
                """
                UPDATE ai_annotation_campaign_batches SET status='PENDING'
                WHERE id=%s AND status='SUBMITTING' AND provider_job_id IS NULL
                """,
                (chunk["id"],),
            )
            return TickResult(
                "spend-ceiling", public_id, int(chunk["chunk_number"])
            )

    if is_luna:
        ensured_campaign = campaign
    else:
        worker_principal = Principal(
            user_id=int(campaign["authorized_by_user_id"]), public_id=campaign["public_id"],
            username="phase4e-worker", display_name="Phase 4E worker", must_change_password=False,
            is_admin=True, session_id=0, csrf_token="worker",
        )
        prior_cache_identifier = campaign.get("cache_identifier")
        prior_cache_was_reusable = cache_plan_status(campaign) == "REUSABLE"
        ensured_campaign = ensure_campaign_cache(
            settings, worker_principal, public_id, gateway=cast(GeminiGateway, provider)
        )
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        campaign = dict(ensured_campaign)
        current_chunk = connection.execute(
            """
            SELECT * FROM ai_annotation_campaign_batches
            WHERE campaign_id=%s AND chunk_number=%s FOR UPDATE
            """,
            (campaign["id"], chunk["chunk_number"]),
        ).fetchone()
        if current_chunk is None:
            raise CampaignError("reserved child Batch disappeared")
        chunk = dict(current_chunk)
        if chunk["status"] != "SUBMITTING":
            return TickResult("already-advanced", public_id, int(chunk["chunk_number"]))
        records = _records_for_chunk(connection, chunk)
        cache = {
            key: campaign[key]
            for key in (
                "cache_identifier", "cache_content_sha256", "cache_model",
                "cache_display_name", "cache_created_at", "cache_expires_at",
                "cache_verified_at", "cache_metadata",
            )
        }
        if is_luna:
            directory = _chunk_directory(public_id, int(chunk["chunk_number"]))
            request_path = directory / "requests.jsonl"
            request_sha256 = _hash_file(request_path)
            regenerated = _luna_prepared_requests(campaign, records)
            regenerated_bytes = b"".join(
                (json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                for item in regenerated
            )
            if request_sha256 != chunk["provider_input_file_sha256"] or hashlib.sha256(regenerated_bytes).hexdigest() != request_sha256:
                raise CampaignError("prepared Luna request file no longer matches campaign provenance")
            plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
            found = provider.find_batch(public_id, int(chunk["chunk_number"]))
            provider_job_id = found.get("id") if isinstance(found, dict) else None
            batch_state = str(found.get("status") or "validating") if isinstance(found, dict) else "validating"
            failure_metadata = chunk.get("failure_metadata")
            submission_uncertain = isinstance(failure_metadata, dict) and bool(failure_metadata.get("submission_outcome_uncertain"))
            if provider_job_id is None and submission_uncertain:
                return TickResult("awaiting-submission-reconciliation", public_id, int(chunk["chunk_number"]))
            input_file_id = chunk.get("provider_input_file_id")
            if provider_job_id is None:
                if not isinstance(input_file_id, str):
                    try:
                        input_file_id = provider.upload_requests(request_path)
                        connection.execute(
                            "UPDATE ai_annotation_campaign_batches SET provider_input_file_id=%s WHERE id=%s AND provider_input_file_id IS NULL",
                            (input_file_id, chunk["id"]),
                        )
                    except Exception as error:
                        connection.execute(
                            """UPDATE ai_annotation_campaign_batches SET retry_count=retry_count+1,
                               status=CASE WHEN retry_count>=2 THEN 'FAILED' ELSE 'PENDING' END,
                               failure_metadata=failure_metadata || %s WHERE id=%s AND status='SUBMITTING'""",
                            (Jsonb({"input_file_upload_error": str(error)[:1000]}), chunk["id"]),
                        )
                        return TickResult("input-upload-failed", public_id, int(chunk["chunk_number"]))
                try:
                    submitted = provider.submit(
                        str(input_file_id), campaign_id=public_id,
                        chunk_number=int(chunk["chunk_number"]),
                    )
                    provider_job_id = submitted.get("id")
                    if not isinstance(provider_job_id, str):
                        raise CampaignError("OpenAI Batch response omitted the batch id")
                    batch_state = str(submitted.get("status") or "validating")
                except Exception as error:
                    connection.execute(
                        """UPDATE ai_annotation_campaign_batches
                           SET failure_metadata=failure_metadata || %s
                           WHERE id=%s AND status='SUBMITTING' AND provider_job_id IS NULL""",
                        (Jsonb({"submission_outcome_uncertain": True, "submission_error": str(error)[:1000]}), chunk["id"]),
                    )
                    return TickResult("submission-uncertain", public_id, int(chunk["chunk_number"]))
            connection.execute(
                """UPDATE ai_annotation_campaign_batches SET status='SUBMITTED',provider_job_id=%s,
                   provider_state=%s,submitted_at=COALESCE(submitted_at,now())
                   WHERE id=%s AND status='SUBMITTING' AND provider_job_id IS NULL""",
                (provider_job_id, batch_state, chunk["id"]),
            )
            return TickResult("submitted", public_id, int(chunk["chunk_number"]), provider_job_id)

        raw_cache_metadata = cache["cache_metadata"]
        cache["cache_metadata"] = {
            **(cast(dict[str, Any], raw_cache_metadata) if isinstance(raw_cache_metadata, dict) else {}),
            "created_for_phase4e_chunk": (not prior_cache_was_reusable or cache["cache_identifier"] != prior_cache_identifier),
        }
        plan = _provider_plan(campaign, records, expected_record_set_sha256=str(chunk["record_set_sha256"]), cache_identifier=str(cache["cache_identifier"]), display_name=str(chunk["submission_display_name"]))
        finder = cast(BatchFinder, provider)
        provider_job_id = finder.find_batch(str(chunk["submission_display_name"]), str(chunk["configured_model"]))
        failure_metadata = chunk.get("failure_metadata")
        submission_uncertain = (
            isinstance(failure_metadata, dict)
            and bool(failure_metadata.get("submission_outcome_uncertain"))
        )
        if provider_job_id is None and submission_uncertain:
            return TickResult(
                "awaiting-submission-reconciliation",
                public_id,
                int(chunk["chunk_number"]),
            )
        try:
            provider_job_id = provider_job_id or provider.submit(plan, str(chunk["configured_model"]))
        except Exception as error:
            if _is_quota_error(error):
                connection.execute("UPDATE ai_annotation_campaign_batches SET status='PENDING',failure_metadata=failure_metadata || %s WHERE id=%s AND status='SUBMITTING'", (Jsonb({"quota_event": str(error)[:1000]}), chunk["id"]))
                return TickResult("quota-refused", public_id, int(chunk["chunk_number"]))
            connection.execute(
                """
                UPDATE ai_annotation_campaign_batches
                SET failure_metadata=failure_metadata || %s
                WHERE id=%s AND status='SUBMITTING' AND provider_job_id IS NULL
                """,
                (
                    Jsonb(
                        {
                            "submission_outcome_uncertain": True,
                            "submission_error": str(error)[:1000],
                        }
                    ),
                    chunk["id"],
                ),
            )
            return TickResult(
                "submission-uncertain", public_id, int(chunk["chunk_number"])
            )
        connection.execute(
            """
            UPDATE ai_annotation_campaign_batches SET status='SUBMITTED',provider_job_id=%s,
              provider_state='BATCH_STATE_PENDING',cache_identifier=%s,
              cache_content_sha256=%s,cache_model=%s,cache_display_name=%s,
              cache_created_at=%s,cache_expires_at=%s,cache_verified_at=%s,
              cache_metadata=%s,submitted_at=now()
            WHERE id=%s AND status='SUBMITTING' AND provider_job_id IS NULL
            """,
            (provider_job_id, cache["cache_identifier"], cache["cache_content_sha256"],
             cache["cache_model"], cache["cache_display_name"], cache["cache_created_at"],
             cache["cache_expires_at"], cache["cache_verified_at"], Jsonb(cache["cache_metadata"]),
             chunk["id"]),
        )
        return TickResult("submitted", public_id, int(chunk["chunk_number"]), provider_job_id)


def orchestration_tick(
    settings: WebSettings,
    public_id: str,
    *,
    gateway: Any | None = None,
) -> TickResult:
    """Advance one state while holding a session lock across any provider operation."""
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as lock_connection:
        campaign = lock_connection.execute(
            "SELECT id FROM ai_annotation_campaigns WHERE public_id::text=%s",
            (public_id,),
        ).fetchone()
        if campaign is None:
            raise LookupError("AI annotation campaign not found")
        campaign_id = int(campaign["id"])
        locked = lock_connection.execute(
            "SELECT pg_try_advisory_lock(%s::integer,%s::integer) AS locked",
            (PHASE4E_ADVISORY_LOCK_NAMESPACE, campaign_id),
        ).fetchone()
        if not locked or not locked["locked"]:
            return TickResult("locked", public_id)
        try:
            return _orchestration_tick_locked(settings, public_id, gateway=gateway)
        finally:
            lock_connection.execute(
                "SELECT pg_advisory_unlock(%s::integer,%s::integer)",
                (PHASE4E_ADVISORY_LOCK_NAMESPACE, campaign_id),
            )
