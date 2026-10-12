"""Phase 4B operational planning and explicitly guarded Gemini submissions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hansard_annotator.llm_eval.costs import load_pricing
from hansard_annotator.llm_eval.operations import (
    audit_prompt_structure,
    build_batch_create_request,
    build_cache_create_request,
    canonical_hash,
    collect_inline_batch,
    duration_seconds,
    gemini_batch_schema,
    parse_batch_status,
    production_record_from_turn,
    project_production_cost,
    simulate_costs,
    submission_batch_name,
    validate_production_plan,
)
from hansard_annotator.llm_eval.package import load_evaluation_package
from hansard_annotator.llm_eval.prompt import (
    build_system_prompt,
    prediction_json_schema,
    prompt_hash,
)
from hansard_annotator.llm_eval.providers import _http_json
from hansard_annotator.llm_eval.selection import load_exclusion_set


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 4B Gemini production operations")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "prepare-batch"):
        item = sub.add_parser(name)
        item.add_argument("--input", type=Path, required=True)
        item.add_argument("--output", type=Path, required=True)
        item.add_argument("--prompt-version", default="phase4a-policy-v2")
        item.add_argument("--limit", type=int)
        item.add_argument("--exclude-record-ids", type=Path)
    audit = sub.choices["audit"]
    audit.add_argument("--measured-input-tokens", type=int, required=True)
    batch = sub.choices["prepare-batch"]
    batch.add_argument("--model", default="gemini-2.5-flash")
    batch.add_argument("--cached-content")
    batch.add_argument("--display-name", required=True)
    batch.add_argument("--pricing", type=Path, default=Path("config/llm_eval/pricing.yaml"))
    production = sub.add_parser("prepare-production-batch")
    production.add_argument("--corpus", type=Path, required=True)
    production.add_argument("--context-input", type=Path, required=True)
    production.add_argument("--exclude-package", type=Path, required=True)
    production.add_argument("--output", type=Path, required=True)
    production.add_argument("--limit", type=int, default=500)
    production.add_argument("--subset-seed", type=int, default=4_002)
    production.add_argument("--model", default="gemini-2.5-flash")
    production.add_argument("--cached-content")
    production.add_argument("--display-name", required=True)
    production.add_argument("--pricing", type=Path, default=Path("config/llm_eval/pricing.yaml"))
    simulate = sub.add_parser("simulate")
    simulate.add_argument("--pricing", type=Path, default=Path("config/llm_eval/pricing.yaml"))
    simulate.add_argument("--records", type=int, default=160577)
    simulate.add_argument("--input-tokens-per-record", type=float, default=581870 / 120)
    simulate.add_argument("--output-tokens-per-record", type=float, default=11916 / 120)
    simulate.add_argument("--prefix-tokens", type=int, required=True)
    simulate.add_argument("--ttl-hours", type=float, default=24.0)
    simulate.add_argument("--output", type=Path, required=True)
    cache = sub.add_parser("prepare-cache")
    cache.add_argument("--input", type=Path, required=True)
    cache.add_argument("--output", type=Path, required=True)
    cache.add_argument("--model", default="gemini-2.5-flash")
    cache.add_argument("--ttl-seconds", type=int, default=86400)
    cache.add_argument("--display-name", required=True)
    cache.add_argument("--token-count", type=int, required=True)
    submit = sub.add_parser("submit-plan")
    submit.add_argument("--plan", type=Path, required=True)
    submit.add_argument("--kind", choices=("batch", "cache"), required=True)
    submit.add_argument("--model", default="gemini-2.5-flash")
    submit.add_argument("--result", type=Path, required=True)
    submit.add_argument("--confirm-paid-submit", action="store_true")
    status = sub.add_parser("batch-status")
    status_source = status.add_mutually_exclusive_group(required=True)
    status_source.add_argument("--submission", type=Path)
    status_source.add_argument("--batch-name")
    collect = sub.add_parser("collect-batch")
    collect_source = collect.add_mutually_exclusive_group(required=True)
    collect_source.add_argument("--submission", type=Path)
    collect_source.add_argument("--batch-name")
    collect.add_argument("--plan", type=Path, required=True)
    collect.add_argument("--cache-submission", type=Path)
    collect.add_argument("--pricing", type=Path, default=Path("config/llm_eval/pricing.yaml"))
    collect.add_argument("--output", type=Path, required=True)
    return parser


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _batch_name(arguments: argparse.Namespace) -> str:
    if arguments.batch_name:
        name = str(arguments.batch_name)
    else:
        receipt = json.loads(arguments.submission.read_text(encoding="utf-8"))
        name = submission_batch_name(receipt)
    if not name.startswith("batches/"):
        raise ValueError("Gemini batch name must begin with batches/")
    return name


def _get_batch(name: str) -> dict[str, Any]:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY is not set")
    request = Request(
        f"https://generativelanguage.googleapis.com/v1beta/{name}",
        headers={"x-goog-api-key": key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=120.0) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:2000]
        raise ValueError(f"Gemini status HTTP {error.code}: {body}") from None
    except (URLError, TimeoutError) as error:
        raise ValueError(f"Gemini status request failed: {type(error).__name__}") from None
    if not isinstance(value, dict):
        raise ValueError("Gemini status response is not an object")
    return value


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )


def _collection_summary(
    batch_value: dict[str, Any],
    plan: dict[str, Any],
    results: list[dict[str, Any]],
    coverage: dict[str, int],
    pricing: dict[str, Any],
    cache_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    status = parse_batch_status(batch_value)
    successful_count = int(str(status["successful_count"]))
    failed_count = int(str(status["failed_count"]))
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    tokens = {
        field: sum(
            int(usage.get(field) or 0)
            for result in results
            if isinstance((usage := result.get("usage")), dict)
        )
        for field in token_fields
    }
    valid = sum(result.get("status") == "success" for result in results)
    invalid = sum(result.get("status") == "invalid" for result in results)
    provider_failures = sum(result.get("status") == "provider_failure" for result in results)
    entry = pricing["models"]["gemini:gemini-2.5-flash"]
    rates = entry["modes"]["batch"]
    uncached = max(0, tokens["input_tokens"] - tokens["cached_input_tokens"])
    input_cost = uncached * float(rates["input_per_million_usd"]) / 1_000_000
    cached_cost = (
        tokens["cached_input_tokens"] * float(rates["cached_input_per_million_usd"]) / 1_000_000
    )
    output_cost = (
        (tokens["output_tokens"] + tokens["reasoning_tokens"])
        * float(rates["output_per_million_usd"])
        / 1_000_000
    )
    storage_cost = 0.0
    cache_tokens = 0
    ttl_hours = 0.0
    if cache_receipt:
        cache = cache_receipt.get("response", {})
        usage = cache.get("usageMetadata", {}) if isinstance(cache, dict) else {}
        cache_tokens = int(usage.get("totalTokenCount") or 0) if isinstance(usage, dict) else 0
        ttl_seconds = (
            duration_seconds(cache.get("createTime"), cache.get("expireTime"))
            if isinstance(cache, dict)
            else None
        )
        ttl_hours = (ttl_seconds or 0) / 3600
        storage_cost = (
            cache_tokens
            * ttl_hours
            * float(entry["cache_storage_per_million_token_hours_usd"])
            / 1_000_000
        )
    processing = input_cost + cached_cost + output_cost
    total = processing + storage_cost
    elapsed = duration_seconds(status["creation_time"], status["completion_time"])
    projection = (
        project_production_cost(
            token_processing_cost_usd=processing,
            valid_records=valid,
            target_records=160_577,
            shared_cache_storage_cost_usd=storage_cost,
        )
        if valid
        else None
    )
    return {
        "provenance": {
            **plan["provenance"],
            "batch_id": status["batch_name"],
            "pricing_source": entry.get("source"),
        },
        "batch_status": status,
        "coverage": {
            **coverage,
            "successful_provider_responses": successful_count,
            "failed_provider_responses": failed_count,
            "valid_structured_predictions": valid,
            "invalid_structured_predictions": invalid,
            "provider_failures_collected": provider_failures,
            "accounting_invariant_ok": (
                coverage["planned_requests"]
                == successful_count + failed_count
                == len(results) - coverage["duplicate_mappings"]
            ),
        },
        "tokens": {
            **tokens,
            "uncached_input_tokens": uncached,
            "cached_input_percentage": tokens["cached_input_tokens"] / tokens["input_tokens"]
            if tokens["input_tokens"]
            else 0.0,
            "average_input_tokens_per_record": tokens["input_tokens"] / len(results)
            if results
            else 0.0,
            "average_output_tokens_per_record": tokens["output_tokens"] / len(results)
            if results
            else 0.0,
            "prior_implicit_cache_percentage": 0.4814,
            "theoretical_explicit_cache_percentage": 0.8519,
        },
        "cost": {
            "batch_uncached_input_cost_usd": input_cost,
            "cached_input_cost_usd": cached_cost,
            "output_including_reasoning_cost_usd": output_cost,
            "token_processing_cost_usd": processing,
            "explicit_cache_tokens": cache_tokens,
            "explicit_cache_ttl_hours": ttl_hours,
            "explicit_cache_storage_cost_usd": storage_cost,
            "total_pilot_cost_usd": total,
            "cost_per_valid_annotation_usd": total / valid if valid else None,
            "projected_variable_token_processing_160577_usd": (
                projection["projected_variable_token_processing_usd"] if projection else None
            ),
            "projected_shared_cache_storage_usd": storage_cost,
            "projected_160577_cost_usd": (
                projection["projected_total_usd"] if projection else None
            ),
            "projection_note": (
                "Mechanical variable per-valid-record extrapolation plus one shared cache "
                "lifetime; actual billing may require multiple configurable cache lifecycles."
            ),
        },
        "performance": {
            "submission_time": status["creation_time"],
            "completion_time": status["completion_time"],
            "wall_clock_seconds": elapsed,
            "requests_per_minute": coverage["planned_requests"] / (elapsed / 60)
            if elapsed
            else None,
            "requests_per_hour": coverage["planned_requests"] / (elapsed / 3600)
            if elapsed
            else None,
            "retry_count": 0,
        },
    }


def _report(summary: dict[str, Any]) -> str:
    coverage = summary["coverage"]
    tokens = summary["tokens"]
    cost = summary["cost"]
    performance = summary["performance"]
    warning = "PASS" if coverage["accounting_invariant_ok"] else "**DISCREPANCY DETECTED**"
    return f"""# Phase 4B Batch pilot operational report

This report validates operations only. It does not compare predictions with human labels.

## Coverage ({warning})

- Planned/provider requests: {coverage["planned_requests"]} / {coverage["provider_request_count"]}
- Provider successes/failures: {coverage["successful_provider_responses"]} / {coverage["failed_provider_responses"]}
- Collected/missing/duplicate: {coverage["collected_responses"]} / {coverage["missing_responses"]} / {coverage["duplicate_mappings"]}
- Valid/invalid structured predictions: {coverage["valid_structured_predictions"]} / {coverage["invalid_structured_predictions"]}

## Token and cache telemetry

- Input/cached/uncached: {tokens["input_tokens"]} / {tokens["cached_input_tokens"]} / {tokens["uncached_input_tokens"]}
- Output/reasoning: {tokens["output_tokens"]} / {tokens["reasoning_tokens"]}
- Actual cached-input share: {tokens["cached_input_percentage"]:.2%}
- Prior implicit / theoretical explicit: 48.14% / 85.19%

## Actual cost

- Batch uncached input: USD {cost["batch_uncached_input_cost_usd"]:.8f}
- Cached input: USD {cost["cached_input_cost_usd"]:.8f}
- Output including reasoning: USD {cost["output_including_reasoning_cost_usd"]:.8f}
- Token processing: USD {cost["token_processing_cost_usd"]:.8f}
- Explicit-cache storage: USD {cost["explicit_cache_storage_cost_usd"]:.8f}
- Total pilot: USD {cost["total_pilot_cost_usd"]:.8f}
- Cost per valid annotation: {cost["cost_per_valid_annotation_usd"]}
- Mechanical 160,577-record variable processing projection: {cost["projected_variable_token_processing_160577_usd"]}
- One shared cache-lifetime projection: {cost["projected_shared_cache_storage_usd"]}
- Mechanical combined projection: {cost["projected_160577_cost_usd"]}

## Performance

- Submission/completion: {performance["submission_time"]} / {performance["completion_time"]}
- Wall-clock seconds: {performance["wall_clock_seconds"]}
- Requests/minute and hour: {performance["requests_per_minute"]} / {performance["requests_per_hour"]}
- Client retries: 0 (asynchronous Batch job; no per-request client retries)
"""


def _records(arguments: argparse.Namespace) -> tuple[Any, tuple[dict[str, Any], ...]]:
    package = load_evaluation_package(arguments.input)
    records = package.records
    if arguments.exclude_record_ids:
        excluded = load_exclusion_set(
            arguments.exclude_record_ids,
            package_snapshot_sha256=str(package.manifest["snapshot_sha256"]),
            package_record_ids={str(record["record_id"]) for record in records},
        )
        records = tuple(
            record for record in records if str(record["record_id"]) not in excluded.record_ids
        )
    if arguments.limit is not None:
        records = records[: arguments.limit]
    return package, records


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "audit":
            package, records = _records(args)
            audit = audit_prompt_structure(
                package.context,
                records,
                prompt_version=args.prompt_version,
                measured_input_tokens=args.measured_input_tokens,
            )
            _write(args.output, audit.to_dict())
        elif args.command == "simulate":
            pricing = load_pricing(args.pricing)
            entry = pricing["models"]["gemini:gemini-2.5-flash"]
            modes = entry["modes"]
            theoretical = args.prefix_tokens / args.input_tokens_per_record
            shares = [0.4814, 0.60, 0.70, 0.80, 0.85, 0.90, theoretical]
            scenarios = []
            for mode in ("standard", "batch"):
                rates = modes[mode]
                for share in shares:
                    calculated = simulate_costs(
                        records=args.records,
                        input_tokens_per_record=args.input_tokens_per_record,
                        output_tokens_per_record=args.output_tokens_per_record,
                        cached_share=min(1.0, share),
                        input_rate=float(rates["input_per_million_usd"]),
                        cached_rate=float(rates["cached_input_per_million_usd"]),
                        output_rate=float(rates["output_per_million_usd"]),
                        explicit_cache_tokens=args.prefix_tokens if share == theoretical else 0,
                        ttl_hours=args.ttl_hours if share == theoretical else 0,
                        storage_rate=float(entry["cache_storage_per_million_token_hours_usd"]),
                    )
                    result: dict[str, object] = dict(calculated)
                    result["mode"] = mode
                    result["scenario"] = (
                        "theoretical_explicit_prefix"
                        if share == theoretical
                        else f"cached_{share:.2%}"
                    )
                    scenarios.append(result)
            _write(
                args.output, {"estimate": True, "pricing_provenance": entry, "scenarios": scenarios}
            )
        elif args.command == "prepare-cache":
            package = load_evaluation_package(args.input)
            system = build_system_prompt(package.context, "phase4a-policy-v2")
            request = build_cache_create_request(
                model=args.model,
                system_prompt=system,
                ttl_seconds=args.ttl_seconds,
                display_name=args.display_name,
            )
            _write(
                args.output,
                {
                    "request": request,
                    "model": args.model,
                    "content_sha256": prompt_hash(system),
                    "token_count": args.token_count,
                    "ttl_seconds": args.ttl_seconds,
                    "cache_identifier": None,
                },
            )
        elif args.command in {"prepare-batch", "prepare-production-batch"}:
            if args.command == "prepare-production-batch":
                import pyarrow.dataset as ds

                package = load_evaluation_package(args.context_input)
                excluded_package = load_evaluation_package(args.exclude_package)
                excluded_turns = {
                    str(record["speech"]["turn_key"]) for record in excluded_package.records
                }
                columns = [
                    "turn_key",
                    "date",
                    "chamber",
                    "speaker_name_raw",
                    "major_heading_original",
                    "minor_heading_original",
                    "business_type",
                    "text_clean",
                    "text_raw",
                    "eligible_main_analysis",
                ]
                dataset_factory: Any = ds.dataset
                field_factory: Any = ds.field  # type: ignore[attr-defined]
                table = dataset_factory(
                    args.corpus, format="parquet", partitioning="hive"
                ).to_table(
                    columns=columns,
                    filter=field_factory("eligible_main_analysis") == True,  # noqa: E712
                )
                rows = [
                    row for row in table.to_pylist() if str(row["turn_key"]) not in excluded_turns
                ]
                rows.sort(
                    key=lambda row: hashlib.sha256(
                        f"{row['turn_key']}:{args.subset_seed}".encode()
                    ).hexdigest()
                )
                records = tuple(production_record_from_turn(row) for row in rows[: args.limit])
                prompt_version = "phase4a-policy-v2"
            else:
                package, records = _records(args)
                prompt_version = args.prompt_version
            system = build_system_prompt(package.context, prompt_version)
            schema = prediction_json_schema(package.domain_codes)
            pricing = load_pricing(args.pricing)
            pricing_entry = pricing["models"][f"gemini:{args.model}"]
            request = build_batch_create_request(
                model=args.model,
                system_prompt=system,
                records=records,
                schema=schema,
                display_name=args.display_name,
                cached_content=args.cached_content,
            )
            provenance = {
                "provider": "gemini",
                "model": args.model,
                "prompt_version": prompt_version,
                "assembled_prompt_sha256": prompt_hash(system),
                "canonical_schema_sha256": str(package.schema["content_sha256"]),
                "prediction_contract_sha256": canonical_hash(schema),
                "provider_schema_sha256": canonical_hash(gemini_batch_schema(schema)),
                "thinking_budget": 0,
                "execution_mode": "batch",
                "cached_content": args.cached_content,
                "record_count": len(records),
                "record_ids_sha256": canonical_hash(
                    [str(record["record_id"]) for record in records]
                ),
                "created_at": datetime.now(UTC).isoformat(),
                "pricing_format_version": pricing["format_version"],
                "pricing_effective_date": pricing_entry.get(
                    "effective_date", pricing.get("effective_date")
                ),
            }
            _write(args.output, {"provenance": provenance, "request": request})
        elif args.command == "batch-status":
            status_value = _get_batch(_batch_name(args))
            print(json.dumps(parse_batch_status(status_value), indent=2, sort_keys=True))
        elif args.command == "collect-batch":
            batch_name = _batch_name(args)
            batch_value = _get_batch(batch_name)
            status = parse_batch_status(batch_value)
            if not status["terminal"]:
                print(f"batch is not ready for collection: {status['state']}")
                return 2
            if not status["successful_terminal"]:
                raise ValueError(f"batch did not succeed: {status['state']}; {status['error']}")
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            planned = plan["request"]["batch"]["input_config"]["requests"]["requests"]
            provider_schema = planned[0]["request"]["generationConfig"]["responseSchema"]
            primary = provider_schema["properties"]["primary_australian_domain"]
            domain_codes = tuple(value for value in primary["enum"] if isinstance(value, str))
            results, coverage = collect_inline_batch(
                batch_value=batch_value,
                plan=plan,
                domain_codes=domain_codes,
            )
            provenance = plan["provenance"]
            for result in results:
                result["provider"] = provenance["provider"]
                result["configured_model"] = provenance["model"]
                result["prompt_version"] = provenance["prompt_version"]
                result["prompt_sha256"] = provenance["assembled_prompt_sha256"]
                result["schema_sha256"] = provenance["canonical_schema_sha256"]
                result["thinking_budget"] = provenance["thinking_budget"]
                result["execution_mode"] = provenance["execution_mode"]
                result["cache_identifier"] = provenance["cached_content"]
                result["batch_id"] = batch_name
                result["pricing_effective_date"] = provenance["pricing_effective_date"]
            pricing = load_pricing(args.pricing)
            cache_receipt = (
                json.loads(args.cache_submission.read_text(encoding="utf-8"))
                if args.cache_submission
                else None
            )
            summary = _collection_summary(
                batch_value, plan, results, coverage, pricing, cache_receipt
            )
            args.output.mkdir(parents=True, exist_ok=True)
            _write_jsonl(args.output / "predictions.jsonl", results)
            failures = [result for result in results if result.get("status") != "success"]
            if failures:
                _write_jsonl(args.output / "failures.jsonl", failures)
            _write(args.output / "operational_summary.json", summary)
            (args.output / "operational_report.md").write_text(_report(summary), encoding="utf-8")
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            if not args.confirm_paid_submit:
                raise ValueError("submission blocked; pass --confirm-paid-submit explicitly")
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY is not set")
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            if args.kind == "batch":
                validate_production_plan(plan)
            payload = plan["request"]
            endpoint = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{args.model}:batchGenerateContent"
                if args.kind == "batch"
                else "https://generativelanguage.googleapis.com/v1beta/cachedContents"
            )
            response = _http_json(
                endpoint,
                {"x-goog-api-key": key, "Content-Type": "application/json"},
                payload,
                120.0,
            )
            _write(
                args.result, {"submitted_at": datetime.now(UTC).isoformat(), "response": response}
            )
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
