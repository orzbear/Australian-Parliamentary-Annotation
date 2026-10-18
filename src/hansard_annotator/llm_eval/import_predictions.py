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


def _prediction_artifact_summary(directory: Path) -> dict[str, Any]:
    if (
        directory.name == INVALID_ARTIFACT_DIRECTORY
        or (directory / "schema_regression.md").exists()
    ):
        raise ValueError("schema-invalid 500-record pilot is explicitly prohibited from import")
    summary = json.loads((directory / "operational_summary.json").read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError("artifact operational summary is not an object")
    provenance = summary.get("provenance", {})
    expected = {
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
    if (
        not coverage.get("accounting_invariant_ok")
        or coverage.get("invalid_structured_predictions") != 0
    ):
        raise ValueError("artifact coverage or structured validation is not importable")
    return summary


def iter_prediction_artifact(directory: Path) -> Iterator[dict[str, Any]]:
    with (directory / "predictions.jsonl").open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("status") != "success":
                raise ValueError(f"prediction line {number} is not successful")
            prediction = value.get("prediction")
            if not isinstance(prediction, dict):
                raise ValueError(f"prediction line {number} has no prediction object")
            validate_prediction(prediction, DOMAIN_CODES)
            record_id = value.get("record_id")
            if not isinstance(record_id, str) or not record_id.startswith("turn:"):
                raise ValueError(
                    f"prediction line {number} has an invalid production record ID"
                )
            yield value


def load_prediction_artifact(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Compatibility loader for small historical artifacts; scalable import streams."""
    summary = _prediction_artifact_summary(directory)
    rows = list(iter_prediction_artifact(directory))
    if len(rows) != int(summary["coverage"].get("valid_structured_predictions", -1)):
        raise ValueError("artifact row count does not match operational summary")
    return summary, rows, _artifact_hash(directory)


def inspect_prediction_artifact(directory: Path) -> tuple[dict[str, Any], int, str]:
    """Validate and count a large artifact without retaining its prediction rows."""
    summary = _prediction_artifact_summary(directory)
    row_count = sum(1 for _row in iter_prediction_artifact(directory))
    if row_count != int(summary["coverage"].get("valid_structured_predictions", -1)):
        raise ValueError("artifact row count does not match operational summary")
    return summary, row_count, _artifact_hash(directory)


def _batched_rows(directory: Path) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for value in iter_prediction_artifact(directory):
        batch.append(value)
        if len(batch) == IMPORT_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _turn_ids_for_rows(
    connection: psycopg.Connection[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, int]:
    turn_keys = [str(row["record_id"]).removeprefix("turn:") for row in rows]
    turns = connection.execute(
        "SELECT id,turn_key FROM speaker_turns WHERE turn_key=ANY(%s)", (turn_keys,)
    ).fetchall()
    return {str(row["turn_key"]): int(row["id"]) for row in turns}


def _prediction_insert_values(
    *, run_id: int, directory: Path, rows: list[dict[str, Any]], turn_ids: dict[str, int]
) -> list[tuple[object, ...]]:
    values: list[tuple[object, ...]] = []
    for row in rows:
        prediction = row["prediction"]
        response = row.get("provider_response")
        response_id = response.get("responseId") if isinstance(response, dict) else None
        usage = row.get("usage", {})
        turn_key = str(row["record_id"]).removeprefix("turn:")
        values.append(
            (
                run_id, turn_ids[turn_key], row["record_id"], prediction["is_non_policy"],
                prediction["primary_australian_domain"],
                prediction["secondary_australian_domains"], prediction["reasoning"],
                Jsonb(response) if response is not None else None,
                f"{directory}/predictions.jsonl#{row.get('request_index')}", response_id,
                usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0),
                usage.get("output_tokens", 0), usage.get("reasoning_tokens", 0),
                row.get("request_index"), row.get("response_index"), row.get("returned_model"),
            )
        )
    return values


def import_predictions(
    directory: Path, settings: DatabaseSettings, *, dry_run: bool
) -> dict[str, Any]:
    summary = _prediction_artifact_summary(directory)
    artifact_sha256 = _artifact_hash(directory)
    with psycopg.connect(settings.psycopg_url, row_factory=dict_row) as connection:
        existing_run = connection.execute(
            "SELECT id FROM model_prediction_runs WHERE artifact_sha256=%s", (artifact_sha256,)
        ).fetchone()
        existing_predictions = 0
        if existing_run:
            count = connection.execute(
                "SELECT count(*) AS count FROM model_predictions WHERE run_id=%s",
                (existing_run["id"],),
            ).fetchone()
            existing_predictions = int(count["count"] if count else 0)
        artifact_records = 0
        matching_turns = 0
        unknown_count = 0
        unknown: list[str] = []
        for batch in _batched_rows(directory):
            artifact_records += len(batch)
            turn_ids = _turn_ids_for_rows(connection, batch)
            matching_turns += len(turn_ids)
            for row in batch:
                turn_key = str(row["record_id"]).removeprefix("turn:")
                if turn_key not in turn_ids:
                    unknown_count += 1
                    if len(unknown) < 100:
                        unknown.append(turn_key)
        expected_records = int(summary["coverage"].get("valid_structured_predictions", -1))
        if artifact_records != expected_records:
            raise ValueError("artifact row count does not match operational summary")
        result = {
            "artifact_records": artifact_records,
            "valid_records": artifact_records,
            "matching_db_turns": matching_turns,
            "unknown_turns": unknown,
            "unknown_turn_count": unknown_count,
            "existing_predictions": existing_predictions,
            "inserted_runs": 0,
            "inserted_predictions": 0,
            "skipped_rows": existing_predictions,
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
              (artifact_sha256,provider,configured_model,returned_model,prompt_version,
               prompt_sha256,canonical_schema_sha256,prediction_contract_sha256,
               provider_schema_sha256,generation_config,execution_mode,provider_job_id,
               cache_identifier,pricing_format_version,pricing_effective_date,
               record_set_sha256,started_at,completed_at,requested_count,
               provider_success_count,provider_failure_count,valid_count,invalid_count,
               input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,
               token_processing_cost_usd,cache_storage_cost_usd,total_cost_usd,status,metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                artifact_sha256,
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
                Jsonb({"source_directory": str(directory)}),
            ),
        ).fetchone()
        assert run is not None
        insert_cursor = connection.cursor()
        inserted = 0
        for batch in _batched_rows(directory):
            turn_ids = _turn_ids_for_rows(connection, batch)
            values = _prediction_insert_values(
                run_id=int(run["id"]), directory=directory, rows=batch, turn_ids=turn_ids
            )
            insert_cursor.executemany(
                """
                INSERT INTO model_predictions
                  (run_id,speaker_turn_id,record_id,is_non_policy,primary_australian_domain,
                   secondary_australian_domains,reasoning,validation_status,
                   raw_provider_response,artifact_reference,provider_response_id,input_tokens,
                   cached_input_tokens,output_tokens,reasoning_tokens,request_index,response_index,
                   returned_model)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'success',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                values,
            )
            inserted += len(values)
        result.update(inserted_runs=1, inserted_predictions=inserted, skipped_rows=0)
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
