"""Transactional import of validated Phase 4B prediction artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hansard_annotator.db.config import DatabaseSettings
from hansard_annotator.llm_eval.operations import (
    APPROVED_PRODUCTION_GENERATION,
    APPROVED_PRODUCTION_MODEL,
    FROZEN_CANONICAL_SCHEMA_SHA256,
    FROZEN_MODEL,
    FROZEN_PROMPT_SHA256,
    FROZEN_PROMPT_VERSION,
)
from hansard_annotator.llm_eval.prediction_tasks import adapt_hostility_prediction
from hansard_annotator.llm_eval.validation import validate_prediction

INVALID_ARTIFACT_DIRECTORY = "batch_pilot_500_results"
DOMAIN_CODES = (*tuple(f"AU{number:02d}" for number in range(1, 15)), "AU_OTHER_REVIEW")
IMPORT_BATCH_SIZE = 1_000


def _generation_identity(provenance: dict[str, Any]) -> dict[str, Any]:
    supported = {
        FROZEN_MODEL: {"thinking_budget": 0},
        APPROVED_PRODUCTION_MODEL: APPROVED_PRODUCTION_GENERATION,
    }
    model = provenance.get("model")
    if not isinstance(model, str) or model not in supported:
        raise ValueError("artifact provenance mismatch: model")
    generation = {
        key: provenance[key]
        for key in ("thinking_budget", "thinking_level")
        if key in provenance
    }
    if generation != supported[model]:
        raise ValueError("artifact provenance mismatch: thinking configuration")
    return generation


def _artifact_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in ("operational_summary.json", "predictions.jsonl"):
        digest.update(name.encode())
        digest.update(b"\0")
        with (directory / name).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _prediction_artifact_summary(
    directory: Path,
    expected_configuration: dict[str, Any] | None = None,
    *,
    prediction_task: str = "policy",
) -> dict[str, Any]:
    if (
        directory.name == INVALID_ARTIFACT_DIRECTORY
        or (directory / "schema_regression.md").exists()
    ):
        raise ValueError("schema-invalid 500-record pilot is explicitly prohibited from import")
    summary = json.loads((directory / "operational_summary.json").read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError("artifact operational summary is not an object")
    provenance = summary.get("provenance", {})
    expected = expected_configuration or {
        "provider": "gemini",
        "prompt_version": FROZEN_PROMPT_VERSION,
        "assembled_prompt_sha256": FROZEN_PROMPT_SHA256,
        "canonical_schema_sha256": FROZEN_CANONICAL_SCHEMA_SHA256,
        "execution_mode": "batch",
    }
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise ValueError(f"artifact provenance mismatch: {key}")
    _generation_identity(provenance)
    coverage = summary.get("coverage", {})
    invalid = int(coverage.get("invalid_structured_predictions") or 0)
    planned = int(coverage.get("planned_requests") or 0)
    valid = int(coverage.get("valid_structured_predictions") or 0)
    refused = int(coverage.get("provider_refused") or 0)
    terminal_invalid_supported = (
        prediction_task == "hostility"
        and invalid > 0
        and valid + refused + invalid == planned
        and not any(
            int(coverage.get(key) or 0)
            for key in (
                "failed_provider_responses", "missing_responses", "duplicate_mappings"
            )
        )
    )
    if not coverage.get("accounting_invariant_ok") and not terminal_invalid_supported:
        raise ValueError("artifact coverage or structured validation is not importable")
    if invalid != 0 and not terminal_invalid_supported:
        raise ValueError("artifact coverage or structured validation is not importable")
    if (refused and valid + refused + invalid != planned) or (
        planned and valid + refused + invalid != planned
    ):
        raise ValueError("artifact outcomes do not match planned coverage")
    if refused:
        refusal_metadata = summary.get("provider_refusals")
        refusal_path = directory / "provider_refusals.jsonl"
        if not isinstance(refusal_metadata, dict) or not refusal_path.is_file():
            raise ValueError("artifact provider-refusal provenance is missing")
        refusal_bytes = refusal_path.read_bytes()
        if hashlib.sha256(refusal_bytes).hexdigest() != refusal_metadata.get(
            "lineage_sha256"
        ):
            raise ValueError("artifact provider-refusal provenance hash mismatch")
        record_ids = refusal_metadata.get("record_ids")
        if (
            not isinstance(record_ids, list)
            or len(record_ids) != refused
            or any(not isinstance(record_id, str) for record_id in record_ids)
            or len(set(record_ids)) != refused
            or hashlib.sha256(
                json.dumps(record_ids, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            != refusal_metadata.get("record_ids_sha256")
        ):
            raise ValueError("artifact provider-refusal record identity mismatch")
    return summary


def _has_candidate_text(response: object) -> bool:
    candidates = response.get("candidates") if isinstance(response, dict) else None
    if not isinstance(candidates, list):
        return False
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if isinstance(parts, list) and any(
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and bool(part["text"].strip())
            for part in parts
        ):
            return True
    return False


def _validate_refusal_response(response: object) -> None:
    feedback = response.get("promptFeedback") if isinstance(response, dict) else None
    if (
        not isinstance(feedback, dict)
        or feedback.get("blockReason") != "PROHIBITED_CONTENT"
        or _has_candidate_text(response)
    ):
        raise ValueError("provider-refusal evidence is not a prohibited-content abstention")


def iter_prediction_artifact(
    directory: Path, *, prediction_task: str = "policy"
) -> Iterator[dict[str, Any]]:
    with (directory / "predictions.jsonl").open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"prediction line {number} is not an object")
            if value.get("status") == "provider_refusal":
                if value.get("prediction") is not None:
                    raise ValueError(f"prediction line {number} synthesizes a refusal prediction")
                resolution = value.get("terminal_resolution")
                if not isinstance(resolution, dict) or resolution.get("type") != "PROVIDER_REFUSAL":
                    raise ValueError(f"prediction line {number} has invalid refusal lineage")
                _validate_refusal_response(value.get("provider_response"))
                continue
            if value.get("status") == "invalid":
                # Kept out of the prediction stream. The dedicated terminal-outcome
                # importer below accepts only the frozen hostility evidence failure.
                continue
            if value.get("status") != "success":
                raise ValueError(f"prediction line {number} is not successful")
            prediction = value.get("prediction")
            if not isinstance(prediction, dict):
                raise ValueError(f"prediction line {number} has no prediction object")
            if prediction_task == "policy":
                validate_prediction(prediction, DOMAIN_CODES)
            elif prediction_task != "hostility":
                raise ValueError("unsupported prediction task")
            record_id = value.get("record_id")
            if not isinstance(record_id, str) or not record_id.startswith("turn:"):
                raise ValueError(
                    f"prediction line {number} has an invalid production record ID"
                )
            yield value


def iter_terminal_invalid_artifact(
    directory: Path, *, prediction_task: str
) -> Iterator[dict[str, Any]]:
    if prediction_task != "hostility":
        return
    with (directory / "predictions.jsonl").open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("status") != "invalid":
                continue
            if value.get("validation_error") != (
                "hostility evidence must resolve uniquely to canonical text"
            ):
                raise ValueError(
                    f"prediction line {number} has an unsupported terminal invalid outcome"
                )
            record_id = value.get("record_id")
            response = value.get("provider_response")
            if (
                not isinstance(record_id, str)
                or not record_id.startswith("turn:")
                or not isinstance(response, dict)
                or value.get("prediction") is None
            ):
                raise ValueError(f"prediction line {number} has incomplete invalid provenance")
            yield value


def iter_provider_refusal_artifact(directory: Path) -> Iterator[dict[str, Any]]:
    summary = json.loads(
        (directory / "operational_summary.json").read_text(encoding="utf-8")
    )
    provenance = summary.get("provenance") if isinstance(summary, dict) else None
    expected = int(summary.get("coverage", {}).get("provider_refused") or 0)
    if expected == 0:
        return
    metadata = summary.get("provider_refusals")
    expected_ids = metadata.get("record_ids") if isinstance(metadata, dict) else None
    if not isinstance(expected_ids, list):
        raise ValueError("provider refusal metadata has no record IDs")
    seen = 0
    seen_ids: list[str] = []
    with (directory / "provider_refusals.jsonl").open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            value = json.loads(line)
            if (
                not isinstance(value, dict)
                or value.get("resolution") != "PROVIDER_REFUSAL"
                or value.get("reason") != "PROHIBITED_CONTENT"
                or value.get("configuration_provenance") != provenance
            ):
                raise ValueError(f"provider refusal line {number} has invalid provenance")
            record_id = value.get("record_id")
            if not isinstance(record_id, str) or not record_id.startswith("turn:"):
                raise ValueError(f"provider refusal line {number} has an invalid record ID")
            attempts = value.get("attempts")
            if not isinstance(attempts, list) or not attempts:
                raise ValueError(f"provider refusal line {number} has no attempt evidence")
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    raise ValueError(f"provider refusal line {number} has malformed evidence")
                _validate_refusal_response(attempt.get("provider_response"))
            seen += 1
            seen_ids.append(record_id)
            yield value
    if seen != expected or seen_ids != expected_ids:
        raise ValueError("provider refusal rows do not match operational summary")


def load_prediction_artifact(
    directory: Path,
    *,
    prediction_task: str = "policy",
    expected_configuration: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Compatibility loader for small historical artifacts; scalable import streams."""
    summary = _prediction_artifact_summary(
        directory, expected_configuration, prediction_task=prediction_task
    )
    rows = list(iter_prediction_artifact(directory, prediction_task=prediction_task))
    if len(rows) != int(summary["coverage"].get("valid_structured_predictions", -1)):
        raise ValueError("artifact row count does not match operational summary")
    return summary, rows, _artifact_hash(directory)


def inspect_prediction_artifact(
    directory: Path,
    *,
    prediction_task: str = "policy",
    expected_configuration: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, str]:
    """Validate and count a large artifact without retaining its prediction rows."""
    summary = _prediction_artifact_summary(
        directory, expected_configuration, prediction_task=prediction_task
    )
    row_count = sum(
        1 for _row in iter_prediction_artifact(directory, prediction_task=prediction_task)
    )
    if row_count != int(summary["coverage"].get("valid_structured_predictions", -1)):
        raise ValueError("artifact row count does not match operational summary")
    return summary, row_count, _artifact_hash(directory)


def _batched_rows(
    directory: Path, *, prediction_task: str = "policy"
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for value in iter_prediction_artifact(directory, prediction_task=prediction_task):
        batch.append(value)
        if len(batch) == IMPORT_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _batched_refusals(directory: Path) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for value in iter_provider_refusal_artifact(directory):
        batch.append(value)
        if len(batch) == IMPORT_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _batched_terminal_invalid(
    directory: Path, *, prediction_task: str
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for value in iter_terminal_invalid_artifact(directory, prediction_task=prediction_task):
        batch.append(value)
        if len(batch) == IMPORT_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _turns_for_rows(
    connection: psycopg.Connection[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    turn_keys = [str(row["record_id"]).removeprefix("turn:") for row in rows]
    turns = connection.execute(
        "SELECT id,turn_key,text_clean,text_raw FROM speaker_turns WHERE turn_key=ANY(%s)",
        (turn_keys,),
    ).fetchall()
    return {str(row["turn_key"]): dict(row) for row in turns}


def _prediction_insert_values(
    *, run_id: int, directory: Path, rows: list[dict[str, Any]],
    turns: dict[str, dict[str, Any]], prediction_task: str
) -> list[tuple[object, ...]]:
    values: list[tuple[object, ...]] = []
    for row in rows:
        prediction = row["prediction"]
        response = row.get("provider_response")
        response_id = response.get("responseId") if isinstance(response, dict) else None
        usage = row.get("usage", {})
        turn_key = str(row["record_id"]).removeprefix("turn:")
        if prediction_task == "hostility":
            text = str(turns[turn_key].get("text_clean") or turns[turn_key].get("text_raw") or "")
            adapted = adapt_hostility_prediction(prediction, text)
            policy_values: tuple[object, object, object] = (None, None, None)
            canonical_values = adapted.values
            reasoning = adapted.reasoning
        else:
            policy_values = (
                prediction["is_non_policy"], prediction["primary_australian_domain"],
                prediction["secondary_australian_domains"],
            )
            canonical_values = {
                "is_non_policy": prediction["is_non_policy"],
                "primary_australian_domain": prediction["primary_australian_domain"],
                "secondary_australian_domains": prediction["secondary_australian_domains"],
            }
            reasoning = prediction["reasoning"]
        values.append(
            (
                run_id, turns[turn_key]["id"], row["record_id"], prediction_task,
                *policy_values, Jsonb(canonical_values), reasoning,
                Jsonb(response) if response is not None else None,
                f"{directory}/predictions.jsonl#{row.get('request_index')}", response_id,
                usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0),
                usage.get("output_tokens", 0), usage.get("reasoning_tokens", 0),
                row.get("request_index"), row.get("response_index"), row.get("returned_model"),
            )
        )
    return values


def import_predictions(
    directory: Path, settings: DatabaseSettings, *, dry_run: bool,
    prediction_task: str = "policy",
    annotation_schema_slug: str = "australian_policy_annotation",
    annotation_schema_version: str = "0.2.0",
    expected_configuration: dict[str, Any] | None = None,
    terminal_recovery_history: dict[str, list[dict[str, Any]]] | None = None,
    campaign_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = _prediction_artifact_summary(
        directory, expected_configuration, prediction_task=prediction_task
    )
    artifact_sha256 = _artifact_hash(directory)
    with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
        existing_run = connection.execute(
            "SELECT id FROM model_prediction_runs WHERE artifact_sha256=%s", (artifact_sha256,)
        ).fetchone()
        existing_predictions = 0
        existing_refusals = 0
        existing_terminal_invalid = 0
        if existing_run:
            count = connection.execute(
                "SELECT count(*) AS count FROM model_predictions WHERE run_id=%s",
                (existing_run["id"],),
            ).fetchone()
            existing_predictions = int(count["count"] if count else 0)
            refusal_count = connection.execute(
                "SELECT count(*) AS count FROM model_prediction_abstentions "
                "WHERE run_id=%s AND resolution_type='PROVIDER_REFUSAL'",
                (existing_run["id"],),
            ).fetchone()
            existing_refusals = int(refusal_count["count"] if refusal_count else 0)
            invalid_count_row = connection.execute(
                "SELECT count(*) AS count FROM model_prediction_abstentions "
                "WHERE run_id=%s AND resolution_type='INVALID_OUTPUT'",
                (existing_run["id"],),
            ).fetchone()
            existing_terminal_invalid = int(
                invalid_count_row["count"] if invalid_count_row else 0
            )
        artifact_records = 0
        refusal_records = 0
        matching_turns = 0
        unknown_count = 0
        unknown: list[str] = []
        for batch in _batched_rows(directory, prediction_task=prediction_task):
            artifact_records += len(batch)
            turns = _turns_for_rows(connection, batch)
            matching_turns += len(turns)
            for row in batch:
                turn_key = str(row["record_id"]).removeprefix("turn:")
                if turn_key not in turns:
                    unknown_count += 1
                    if len(unknown) < 100:
                        unknown.append(turn_key)
        for batch in _batched_refusals(directory):
            refusal_records += len(batch)
            turns = _turns_for_rows(connection, batch)
            matching_turns += len(turns)
            for row in batch:
                turn_key = str(row["record_id"]).removeprefix("turn:")
                if turn_key not in turns:
                    unknown_count += 1
                    if len(unknown) < 100:
                        unknown.append(turn_key)
        terminal_invalid_records = 0
        for batch in _batched_terminal_invalid(directory, prediction_task=prediction_task):
            terminal_invalid_records += len(batch)
            turns = _turns_for_rows(connection, batch)
            matching_turns += len(turns)
            for row in batch:
                turn_key = str(row["record_id"]).removeprefix("turn:")
                if turn_key not in turns:
                    unknown_count += 1
                    if len(unknown) < 100:
                        unknown.append(turn_key)
        expected_records = int(summary["coverage"].get("valid_structured_predictions", -1))
        expected_refusals = int(summary["coverage"].get("provider_refused") or 0)
        if artifact_records != expected_records:
            raise ValueError("artifact row count does not match operational summary")
        if refusal_records != expected_refusals:
            raise ValueError("artifact refusal count does not match operational summary")
        if terminal_invalid_records != int(
            summary["coverage"].get("invalid_structured_predictions") or 0
        ):
            raise ValueError("artifact terminal invalid count does not match operational summary")
        result = {
            "artifact_records": artifact_records,
            "valid_records": artifact_records,
            "provider_refusal_records": refusal_records,
            "terminal_invalid_records": terminal_invalid_records,
            "resolved_records": artifact_records + refusal_records + terminal_invalid_records,
            "matching_db_turns": matching_turns,
            "unknown_turns": unknown,
            "unknown_turn_count": unknown_count,
            "existing_predictions": existing_predictions,
            "existing_provider_refusals": existing_refusals,
            "existing_terminal_invalid_outcomes": existing_terminal_invalid,
            "inserted_runs": 0,
            "inserted_predictions": 0,
            "inserted_provider_refusals": 0,
            "inserted_terminal_invalid_outcomes": 0,
            "skipped_rows": existing_predictions + existing_refusals + existing_terminal_invalid,
            "dry_run": dry_run,
            "artifact_sha256": artifact_sha256,
        }
        if unknown_count:
            raise ValueError(f"artifact contains {unknown_count} unknown turn keys")
        if dry_run or existing_run:
            return result
        provenance = summary["provenance"]
        generation = _generation_identity(provenance)
        coverage = summary["coverage"]
        tokens = summary["tokens"]
        cost = summary["cost"]
        status = summary["batch_status"]
        run = connection.execute(
            """
            INSERT INTO model_prediction_runs
              (artifact_sha256,prediction_task,annotation_schema_slug,
               annotation_schema_version,provider,configured_model,returned_model,prompt_version,
               prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
               provider_schema_sha256,generation_config,execution_mode,provider_job_id,
               cache_identifier,pricing_format_version,pricing_effective_date,
               record_set_sha256,started_at,completed_at,requested_count,
               provider_success_count,provider_failure_count,valid_count,invalid_count,
               input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,
               token_processing_cost_usd,cache_storage_cost_usd,total_cost_usd,status,metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                artifact_sha256,
                prediction_task,
                annotation_schema_slug,
                annotation_schema_version,
                provenance["provider"],
                provenance["model"],
                status.get("model"),
                provenance["prompt_version"],
                provenance["assembled_prompt_sha256"],
                provenance["canonical_schema_sha256"],
                provenance.get("prediction_contract_sha256"),
                provenance.get("provider_schema_sha256"),
                Jsonb(generation),
                provenance["execution_mode"],
                provenance["batch_id"],
                provenance.get("cached_content"),
                provenance.get("pricing_format_version"),
                provenance.get("pricing_effective_date"),
                provenance["record_ids_sha256"],
                status.get("creation_time"),
                status.get("completion_time"),
                coverage["planned_requests"],
                coverage["successful_provider_responses"],
                coverage["failed_provider_responses"],
                coverage["valid_structured_predictions"],
                coverage["invalid_structured_predictions"],
                tokens["input_tokens"],
                tokens["cached_input_tokens"],
                tokens["output_tokens"],
                tokens["reasoning_tokens"],
                cost["token_processing_cost_usd"],
                cost["explicit_cache_storage_cost_usd"],
                cost["total_pilot_cost_usd"],
                "imported",
                Jsonb(
                    {
                        "source_directory": str(directory),
                        "provider_refusals": summary.get("provider_refusals", {}),
                    }
                ),
            ),
        ).fetchone()
        assert run is not None
        insert_cursor = connection.cursor()
        inserted = 0
        for batch in _batched_rows(directory, prediction_task=prediction_task):
            turns = _turns_for_rows(connection, batch)
            values = _prediction_insert_values(
                run_id=int(run["id"]), directory=directory, rows=batch, turns=turns,
                prediction_task=prediction_task,
            )
            insert_cursor.executemany(
                """
                INSERT INTO model_predictions
                  (run_id,speaker_turn_id,record_id,prediction_task,is_non_policy,primary_australian_domain,
                   secondary_australian_domains,prediction_values,reasoning,validation_status,
                   raw_provider_response,artifact_reference,provider_response_id,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,request_index,response_index,
                   returned_model)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'success',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                values,
            )
            inserted += len(values)
        inserted_refusals = 0
        refusal_cursor = connection.cursor()
        for batch in _batched_refusals(directory):
            turns = _turns_for_rows(connection, batch)
            values = [
                (
                    int(run["id"]),
                    turns[str(row["record_id"]).removeprefix("turn:")]["id"],
                    row["record_id"],
                    row["reason"],
                    Jsonb(row),
                    f"{directory}/provider_refusals.jsonl",
                )
                for row in batch
            ]
            refusal_cursor.executemany(
                """
                INSERT INTO model_prediction_abstentions
                  (run_id,speaker_turn_id,record_id,resolution_reason,
                   outcome_provenance,artifact_reference)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                values,
            )
            inserted_refusals += len(values)
        inserted_terminal_invalid = 0
        for batch in _batched_terminal_invalid(directory, prediction_task=prediction_task):
            turns = _turns_for_rows(connection, batch)
            values = [
                (
                    int(run["id"]),
                    turns[str(row["record_id"]).removeprefix("turn:")]["id"],
                    row["record_id"],
                    "EVIDENCE_RESOLUTION_FAILED",
                    Jsonb({
                        **row,
                        "outcome": "INVALID_OUTPUT",
                        "category": "EVIDENCE_RESOLUTION_FAILED",
                        "recovery_attempt_history": (terminal_recovery_history or {}).get(
                            str(row["record_id"]), row.get("recovery_attempt_history", [])
                        ),
                        "attempt_count": max(
                            int(row.get("recovery_attempt_number") or 0),
                            len((terminal_recovery_history or {}).get(
                                str(row["record_id"]), row.get("recovery_attempt_history", [])
                            )),
                        ) + 1,
                        "final_validation_error": row.get("validation_error"),
                        "campaign": campaign_provenance or {},
                    }),
                    f"{directory}/predictions.jsonl#{row.get('request_index')}",
                )
                for row in batch
            ]
            refusal_cursor.executemany(
                """
                INSERT INTO model_prediction_abstentions
                  (run_id,speaker_turn_id,record_id,resolution_type,resolution_reason,
                   outcome_provenance,artifact_reference)
                VALUES (%s,%s,%s,'INVALID_OUTPUT',%s,%s,%s)
                """,
                values,
            )
            inserted_terminal_invalid += len(values)
        result.update(
            inserted_runs=1,
            inserted_predictions=inserted,
            inserted_provider_refusals=inserted_refusals,
            inserted_terminal_invalid_outcomes=inserted_terminal_invalid,
            skipped_rows=0,
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import validated model predictions")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        result = import_predictions(
            arguments.artifact, DatabaseSettings.from_environment(), dry_run=arguments.dry_run
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
