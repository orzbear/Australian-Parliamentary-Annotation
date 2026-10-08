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
    result.add_argument("--subset-seed", type=int)
    result.add_argument("--temperature", type=float, default=0.0)
    result.add_argument("--max-output-tokens", type=int, default=1000)
    result.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high"))
    result.add_argument("--thinking-budget", type=int)
    result.add_argument("--max-retries", type=int, default=2)
    result.add_argument("--retry-delay", type=float, default=1.0)
    result.add_argument("--snippet-chars", type=int, default=1000)
    result.add_argument("--pricing", type=Path, default=Path("config/llm_eval/pricing.yaml"))
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--no-resume", action="store_true")
    return result


def _select(
    records: tuple[dict[str, Any], ...], record_ids: list[str], seed: int | None, limit: int | None
) -> tuple[dict[str, Any], ...]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    selected = records
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
        records = _select(
            package.records, arguments.record_id, arguments.subset_seed, arguments.limit
        )
        generation = {
            "temperature": arguments.temperature,
            "max_output_tokens": arguments.max_output_tokens,
        }
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
        )
        if arguments.dry_run:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "records": len(records),
                        "provider": arguments.provider,
                        "model": arguments.model,
                        "prompt_version": arguments.prompt_version,
                        "generation": generation,
                        "run_identity": configuration.identity(),
                        "package_snapshot_sha256": package.manifest["snapshot_sha256"],
                        "schema_version": package.schema["version"],
                        "schema_sha256": package.schema["content_sha256"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        pricing = load_pricing(arguments.pricing)
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
