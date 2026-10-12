"""Offline comparison of two Phase 4A runs on an identical frozen record set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _prediction_ids(run_dir: Path) -> set[str]:
    ids: set[str] = set()
    for number, line in enumerate((run_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines(), 1):
        value = json.loads(line)
        record_id = value.get("record_id") if isinstance(value, dict) else None
        if not isinstance(record_id, str):
            raise ValueError(f"invalid prediction at {run_dir} line {number}")
        if record_id in ids:
            raise ValueError(f"duplicate prediction record_id in {run_dir}: {record_id}")
        ids.add(record_id)
    return ids


def _disagreement_count(run_dir: Path) -> int:
    return sum(
        bool(line.strip())
        for line in (run_dir / "mismatches.jsonl").read_text(encoding="utf-8").splitlines()
    )


def _run_summary(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = _object(run_dir / "run_metadata.json")
    configuration = metadata.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError(f"missing run configuration in {run_dir}")
    metrics = _object(run_dir / "metrics.json")
    costs = _object(run_dir / "cost_report.json")
    total = int(metrics["total_records"])
    valid = int(metrics["evaluated_records"])
    if len(_prediction_ids(run_dir)) != total:
        raise ValueError(f"prediction count does not match selected record count in {run_dir}")
    policy = metrics["policy"]
    domain = metrics["primary_domain"]
    return configuration, {
        "provider": configuration["provider"],
        "model": configuration["model"],
        "run_identity": metadata["run_identity"],
        "selected_records": total,
        "valid_predictions": valid,
        "valid_output_rate": valid / total if total else 0.0,
        "api_failure_rate": metrics["api_failure_rate"],
        "invalid_structured_output_rate": metrics["invalid_rate"],
        "policy_accuracy": policy["accuracy"],
        "policy_precision": policy["precision"],
        "policy_recall": policy["recall"],
        "policy_f1": policy["f1"],
        "primary_domain_exact_match": domain["exact_match_accuracy"],
        "primary_domain_macro_f1": domain["macro_f1"],
        "primary_domain_per_domain": {
            code: {
                "support": values["support"],
                "exact_match_rate": values["recall"],
            }
            for code, values in domain["per_domain"].items()
        },
        "human_model_disagreement_count": _disagreement_count(run_dir),
        "total_benchmark_cost_usd": costs["estimated_cost_usd"],
        "cost_per_valid_prediction_usd": costs["cost_per_valid_annotation_usd"],
        "mechanical_160k_cost_estimate_usd": costs["projected_cost_160k_attempts_usd"],
    }


def compare_many(run_dirs: list[Path]) -> dict[str, Any]:
    if len(run_dirs) < 2:
        raise ValueError("comparison requires at least two runs")
    loaded = [_run_summary(run_dir) for run_dir in run_dirs]
    first_configuration, _ = loaded[0]
    compatibility_fields = (
        "package_snapshot_sha256",
        "schema_version",
        "schema_sha256",
        "prompt_version",
        "prompt_sha256",
        "exclusion_record_count",
        "exclusion_set_sha256",
    )
    for configuration, _ in loaded[1:]:
        mismatched = [
            field
            for field in compatibility_fields
            if first_configuration.get(field) != configuration.get(field)
        ]
        if mismatched:
            raise ValueError(f"runs are not benchmark-compatible: {', '.join(mismatched)}")
    first_ids = _prediction_ids(run_dirs[0])
    if any(_prediction_ids(run_dir) != first_ids for run_dir in run_dirs[1:]):
        raise ValueError("runs do not contain the exact same record IDs")
    if len(first_ids) != 120:
        raise ValueError(f"expected the frozen 120-record set, found {len(first_ids)} records")
    models = [summary for _, summary in loaded]
    quality_metrics = (
        "primary_domain_exact_match",
        "primary_domain_macro_f1",
        "policy_f1",
    )
    frontier: dict[str, dict[str, list[str]]] = {}
    for metric in quality_metrics:
        dominated_by: dict[str, list[str]] = {}
        for candidate in models:
            candidate_key = f"{candidate['provider']}:{candidate['model']}"
            candidate_cost = float(candidate["total_benchmark_cost_usd"])
            dominators = []
            for other in models:
                if other is candidate:
                    continue
                other_cost = float(other["total_benchmark_cost_usd"])
                if (
                    float(other[metric]) >= float(candidate[metric])
                    and other_cost <= candidate_cost
                    and (float(other[metric]) > float(candidate[metric]) or other_cost < candidate_cost)
                ):
                    dominators.append(f"{other['provider']}:{other['model']}")
            dominated_by[candidate_key] = sorted(dominators)
        frontier[metric] = dominated_by
    costs = {
        f"{model['provider']}:{model['model']}": float(model["total_benchmark_cost_usd"])
        for model in models
    }
    return {
        "comparison_type": "original-human-reference-model-comparison",
        "record_count": len(first_ids),
        "package_snapshot_sha256": first_configuration["package_snapshot_sha256"],
        "prompt_version": first_configuration["prompt_version"],
        "prompt_sha256": first_configuration["prompt_sha256"],
        "exclusion_set_sha256": first_configuration["exclusion_set_sha256"],
        "models": models,
        "relative_total_cost_ratios": {
            left: {right: left_cost / right_cost for right, right_cost in costs.items()}
            for left, left_cost in costs.items()
        },
        "quality_cost_frontier_dominated_by": frontier,
        "methodological_note": (
            "All models use the same 120 held-out model-selection records; this is not a new "
            "independent test set. Original human annotations are the shared reference. "
            "Post-hoc Gemini adjudications are not used, and this report does not select a "
            "winner. Cost projections are mechanical linear extrapolations, not production "
            "forecasts."
        ),
    }


def compare_runs(first_dir: Path, second_dir: Path) -> dict[str, Any]:
    """Backward-compatible two-run comparison."""
    return compare_many([first_dir, second_dir])


def write_comparison(output: Path, comparison: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    rows = []
    for model in comparison["models"]:
        rows.append(
            "| {provider}:{name} | {valid:.2%} | {api:.2%} | {invalid:.2%} | {acc:.2%} | {precision:.2%} | "
            "{recall:.2%} | {f1:.2%} | {exact:.2%} | {macro:.2%} | {disagreements} | "
            "{cost} | {per_valid} | {projection} |".format(
                provider=model["provider"],
                name=model["model"],
                valid=model["valid_output_rate"],
                api=model["api_failure_rate"],
                invalid=model["invalid_structured_output_rate"],
                acc=model["policy_accuracy"],
                precision=model["policy_precision"],
                recall=model["policy_recall"],
                f1=model["policy_f1"],
                exact=model["primary_domain_exact_match"],
                macro=model["primary_domain_macro_f1"],
                disagreements=model["human_model_disagreement_count"],
                cost=model["total_benchmark_cost_usd"],
                per_valid=model["cost_per_valid_prediction_usd"],
                projection=model["mechanical_160k_cost_estimate_usd"],
            )
        )
    rows_text = "\n".join(rows)
    frontier_rows = "\n".join(
        f"| {metric} | {model} | {', '.join(dominators) if dominators else 'not dominated'} |"
        for metric, values in comparison["quality_cost_frontier_dominated_by"].items()
        for model, dominators in values.items()
    )
    domain_rows = "\n".join(
        f"| {model['provider']}:{model['model']} | {code} | {values['support']} | {values['exact_match_rate']:.2%} |"
        for model in comparison["models"]
        for code, values in model["primary_domain_per_domain"].items()
        if values["support"]
    )
    report = f"""# Phase 4A frozen held-out model comparison

Records: {comparison["record_count"]}  
Prompt: `{comparison["prompt_version"]}`  

| Model | Valid output | API failure | Invalid output | Policy accuracy | Precision | Recall | F1 | Domain exact | Domain macro-F1 | Disagreements | Cost USD | Cost/valid USD | Mechanical 160k USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{rows_text}

## Diagnostic quality/cost frontier

| Quality metric | Model | Dominated by |
|---|---|---|
{frontier_rows}

Dominance is metric-specific: another model must be at least as accurate, no more expensive, and strictly better on quality or cost. This is descriptive and does not select an overall winner.

## Per-domain exact-match rates

| Model | Human domain | Support | Exact match |
|---|---|---:|---:|
{domain_rows}

{comparison["methodological_note"]}
"""
    (output / "comparison.md").write_text(report, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare compatible Phase 4A model runs")
    parser.add_argument("--gemini-run", type=Path)
    parser.add_argument("--openai-run", type=Path)
    parser.add_argument("--run", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        run_dirs = list(arguments.run)
        if arguments.gemini_run is not None or arguments.openai_run is not None:
            if arguments.gemini_run is None or arguments.openai_run is None:
                raise ValueError("both --gemini-run and --openai-run are required together")
            run_dirs.extend((arguments.gemini_run, arguments.openai_run))
        comparison = compare_many(run_dirs)
        write_comparison(arguments.output, comparison)
        print(json.dumps(comparison, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
