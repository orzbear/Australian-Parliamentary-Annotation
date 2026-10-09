"""Command-line entry point for Phase 4A evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from hansard_annotator.llm_eval.costs import aggregate_costs, load_pricing
from hansard_annotator.llm_eval.metrics import compute_metrics
from hansard_annotator.llm_eval.package import load_evaluation_package
from hansard_annotator.llm_eval.prompt import DEFAULT_PROMPT_VERSION, available_prompt_versions
from hansard_annotator.llm_eval.providers import provider_for
from hansard_annotator.llm_eval.reporting import mismatches, write_reports
from hansard_annotator.llm_eval.runner import evaluate, make_configuration
from hansard_annotator.llm_eval.selection import ExclusionSet, load_exclusion_set


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Evaluate LLM policy labels against a Phase 3 human-annotation export"
    )
    result.add_argument("command", nargs="?", choices=("evaluate",), default="evaluate")
    result.add_argument(
        "--input", type=Path, required=True, help="AI-codebook export directory or ZIP"
    )
    result.add_argument("--provider", choices=("gemini", "openai"), required=True)
    result.add_argument("--model", required=True)
    result.add_argument(
        "--prompt-version",
        choices=available_prompt_versions(),
        default=DEFAULT_PROMPT_VERSION,
    )
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--limit", type=int)
    result.add_argument("--record-id", action="append", default=[])
    result.add_argument("--exclude-record-ids", type=Path)
    result.add_argument("--subset-seed", type=int)
    result.add_argument("--temperature", type=float)
    result.add_argument("--max-output-tokens", type=int, default=1000)
    result.add_argument(
        "--reasoning-effort", choices=("none", "minimal", "low", "medium", "high")
    )
    result.add_argument("--thinking-budget", type=int)
    result.add_argument("--max-retries", type=int, default=2)
    result.add_argument("--retry-delay", type=float, default=1.0)
    result.add_argument("--snippet-chars", type=int, default=1000)
    result.add_argument("--pricing", type=Path, default=Path("config/llm_eval/pricing.yaml"))
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--no-resume", action="store_true")
    return result


def _select(
    records: tuple[dict[str, Any], ...],
    record_ids: list[str],
    seed: int | None,
    limit: int | None,
    exclusions: ExclusionSet | None = None,
) -> tuple[dict[str, Any], ...]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    selected = tuple(
        record
        for record in records
        if exclusions is None or str(record["record_id"]) not in exclusions.record_ids
    )
    if record_ids:
        wanted = set(record_ids)
        selected = tuple(record for record in records if record["record_id"] in wanted)
        missing = wanted - {str(record["record_id"]) for record in selected}
        if missing:
            raise ValueError(f"unknown record IDs: {', '.join(sorted(missing))}")
    if seed is not None:
        selected = tuple(
            sorted(
                selected,
                key=lambda record: (
                    hashlib.sha256(f"{record['record_id']}:{seed}".encode()).hexdigest(),
                    record["record_id"],
                ),
            )
        )
    return selected[:limit] if limit is not None else selected


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if arguments.retry_delay < 0:
            raise ValueError("retry_delay must be non-negative")
        if arguments.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if arguments.snippet_chars < 0:
            raise ValueError("snippet_chars must be non-negative")
        if arguments.thinking_budget not in {None, 0}:
            raise ValueError("Phase 4A currently supports only thinking_budget=0")
        if arguments.provider != "gemini" and arguments.thinking_budget is not None:
            raise ValueError("thinking_budget is only valid for Gemini")
        if arguments.provider != "openai" and arguments.reasoning_effort is not None:
            raise ValueError("reasoning_effort is only valid for OpenAI")
        package = load_evaluation_package(arguments.input)
        exclusions = (
            load_exclusion_set(
                arguments.exclude_record_ids,
                package_snapshot_sha256=str(package.manifest["snapshot_sha256"]),
                package_record_ids={str(record["record_id"]) for record in package.records},
            )
            if arguments.exclude_record_ids is not None
            else None
        )
        records = _select(
            package.records,
            arguments.record_id,
            arguments.subset_seed,
            arguments.limit,
            exclusions,
        )
        excluded_count = exclusions.count if exclusions is not None else 0
        exclusion_sha256 = exclusions.sha256 if exclusions is not None else None
        generation = {"max_output_tokens": arguments.max_output_tokens}
        if arguments.temperature is not None:
            generation["temperature"] = arguments.temperature
        elif arguments.provider == "gemini":
            generation["temperature"] = 0.0
        if arguments.reasoning_effort is not None:
            generation["reasoning_effort"] = arguments.reasoning_effort
        if arguments.provider == "gemini":
            generation["thinking_mode"] = (
                "disabled" if arguments.thinking_budget == 0 else "provider_default_dynamic"
            )
            if arguments.thinking_budget == 0:
                generation["thinking_budget"] = 0
        provider = provider_for(arguments.provider)
        configuration, _ = make_configuration(
            package,
            arguments.provider,
            arguments.model,
            generation,
            arguments.prompt_version,
            excluded_count,
            exclusion_sha256,
        )
        pricing = load_pricing(arguments.pricing)
        if arguments.dry_run:
            pricing_check = aggregate_costs([], arguments.provider, arguments.model, pricing)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "records": len(records),
                        "package_records": len(package.records),
                        "excluded_records": excluded_count,
                        "selected_records": len(records),
                        "exclusion_set_sha256": exclusion_sha256,
                        "provider": arguments.provider,
                        "model": arguments.model,
                        "prompt_version": arguments.prompt_version,
                        "generation": generation,
                        "run_identity": configuration.identity(),
                        "package_snapshot_sha256": package.manifest["snapshot_sha256"],
                        "schema_version": package.schema["version"],
                        "schema_sha256": package.schema["content_sha256"],
                        "provider_prediction_schema_sha256": (
                            configuration.provider_prediction_schema_sha256
                        ),
                        "pricing_found": pricing_check["pricing_found"],
                        "pricing_effective_date": pricing_check["pricing_effective_date"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        results = evaluate(
            package,
            records,
            provider,
            arguments.model,
            arguments.output,
            generation,
            resume=not arguments.no_resume,
            max_retries=arguments.max_retries,
            retry_delay=arguments.retry_delay,
            prompt_version=arguments.prompt_version,
            exclusion_record_count=excluded_count,
            exclusion_set_sha256=exclusion_sha256,
        )
        metric_values = compute_metrics(records, results, package.domain_codes)
        cost_values = aggregate_costs(results, arguments.provider, arguments.model, pricing)
        mismatch_values = mismatches(
            records,
            results,
            snippet_chars=arguments.snippet_chars,
            run_identity=configuration.identity(),
            configured_model=arguments.model,
        )
        write_reports(arguments.output, metric_values, cost_values, mismatch_values)
        print(
            json.dumps(
                {
                    "ok": True,
                    "run_directory": str(arguments.output),
                    "records": len(records),
                    "evaluated": metric_values["evaluated_records"],
                    "mismatches": len(mismatch_values),
                    "estimated_cost_usd": cost_values["estimated_cost_usd"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
