"""Research-only pairwise error analysis for compatible Phase 4A runs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from hansard_annotator.llm_eval.comparison import compare_runs
from hansard_annotator.llm_eval.package import load_evaluation_package

CATEGORIES = (
    "both_correct",
    "gemini_only_correct",
    "luna_only_correct",
    "both_wrong_same_answer",
    "both_wrong_different_answer",
)


def _predictions(run_dir: Path) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for number, line in enumerate((run_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines(), 1):
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("record_id"), str):
            raise ValueError(f"invalid prediction at {run_dir} line {number}")
        if row["record_id"] in values:
            raise ValueError(f"duplicate prediction record_id in {run_dir}: {row['record_id']}")
        prediction = row.get("prediction")
        if row.get("status") != "success" or not isinstance(prediction, dict):
            raise ValueError(f"pairwise analysis requires a valid prediction for {row['record_id']}")
        values[row["record_id"]] = prediction
    return values


def _category(first_correct: bool, second_correct: bool, same_answer: bool) -> str:
    if first_correct and second_correct:
        return "both_correct"
    if first_correct:
        return "gemini_only_correct"
    if second_correct:
        return "luna_only_correct"
    return "both_wrong_same_answer" if same_answer else "both_wrong_different_answer"


def _counts(categories: list[str]) -> dict[str, int]:
    counted = Counter(categories)
    return {category: counted[category] for category in CATEGORIES}


def analyse_pairwise(
    records: tuple[dict[str, Any], ...], gemini_dir: Path, luna_dir: Path
) -> dict[str, Any]:
    comparison = compare_runs(gemini_dir, luna_dir)
    gemini = _predictions(gemini_dir)
    luna = _predictions(luna_dir)
    records_by_id = {str(record["record_id"]): record for record in records}
    expected_ids = set(gemini)
    if set(luna) != expected_ids or set(records_by_id) != expected_ids:
        raise ValueError("package and both runs must contain the exact same 120 record IDs")

    policy_categories: list[str] = []
    domain_categories: list[str] = []
    rows: list[dict[str, Any]] = []
    domain_delta: dict[str, Counter[str]] = {}
    domain_totals: Counter[str] = Counter()
    domain_correct: dict[str, Counter[str]] = {
        "gemini": Counter(),
        "luna": Counter(),
    }
    policy_model_disagreements = 0
    domain_model_disagreements = 0

    for record_id in sorted(expected_ids):
        human = records_by_id[record_id]["human_annotation"]["values"]
        gemini_prediction = gemini[record_id]
        luna_prediction = luna[record_id]
        human_non_policy = bool(human["is_non_policy"])
        gemini_non_policy = bool(gemini_prediction["is_non_policy"])
        luna_non_policy = bool(luna_prediction["is_non_policy"])
        policy_category = _category(
            gemini_non_policy == human_non_policy,
            luna_non_policy == human_non_policy,
            gemini_non_policy == luna_non_policy,
        )
        policy_categories.append(policy_category)
        policy_model_disagreements += gemini_non_policy != luna_non_policy

        domain_category: str | None = None
        human_domain = human.get("primary_australian_domain")
        gemini_domain = (
            "__NON_POLICY__"
            if gemini_non_policy
            else gemini_prediction.get("primary_australian_domain")
        )
        luna_domain = (
            "__NON_POLICY__" if luna_non_policy else luna_prediction.get("primary_australian_domain")
        )
        if not human_non_policy:
            human_domain_text = str(human_domain)
            domain_category = _category(
                gemini_domain == human_domain_text,
                luna_domain == human_domain_text,
                gemini_domain == luna_domain,
            )
            domain_categories.append(domain_category)
            domain_model_disagreements += gemini_domain != luna_domain
            domain_totals[human_domain_text] += 1
            domain_correct["gemini"][human_domain_text] += gemini_domain == human_domain_text
            domain_correct["luna"][human_domain_text] += luna_domain == human_domain_text
            domain_delta.setdefault(human_domain_text, Counter())[domain_category] += 1

        rows.append(
            {
                "record_id": record_id,
                "human_policy_status": "non-policy" if human_non_policy else "policy",
                "gemini_policy_status": "non-policy" if gemini_non_policy else "policy",
                "luna_policy_status": "non-policy" if luna_non_policy else "policy",
                "policy_pairwise_category": policy_category,
                "human_primary_domain": human_domain,
                "gemini_primary_domain": gemini_domain,
                "luna_primary_domain": luna_domain,
                "domain_pairwise_category": domain_category,
            }
        )

    per_domain = {
        code: {
            "support": domain_totals[code],
            "gemini_exact_matches": domain_correct["gemini"][code],
            "gemini_exact_match_rate": domain_correct["gemini"][code] / domain_totals[code],
            "luna_exact_matches": domain_correct["luna"][code],
            "luna_exact_match_rate": domain_correct["luna"][code] / domain_totals[code],
            "gemini_only_correct": domain_delta[code]["gemini_only_correct"],
            "luna_only_correct": domain_delta[code]["luna_only_correct"],
            "errors_by_both": (
                domain_delta[code]["both_wrong_same_answer"]
                + domain_delta[code]["both_wrong_different_answer"]
            ),
        }
        for code in sorted(domain_totals)
    }
    gemini_cost = float(comparison["models"][0]["total_benchmark_cost_usd"])
    luna_cost = float(comparison["models"][1]["total_benchmark_cost_usd"])
    return {
        "comparison_type": "research-only-pairwise-error-analysis",
        "record_count": len(expected_ids),
        "human_policy_record_count": len(domain_categories),
        "prompt_version": comparison["prompt_version"],
        "prompt_sha256": comparison["prompt_sha256"],
        "policy": {
            "categories": _counts(policy_categories),
            "model_vs_model_disagreements": policy_model_disagreements,
        },
        "primary_domain_on_human_policy_records": {
            "categories": _counts(domain_categories),
            "model_vs_model_disagreements": domain_model_disagreements,
            "per_human_domain": per_domain,
        },
        "cost": {
            "gemini": comparison["models"][0],
            "luna": comparison["models"][1],
            "gemini_to_luna_total_cost_ratio": gemini_cost / luna_cost,
            "luna_to_gemini_total_cost_ratio": luna_cost / gemini_cost,
        },
        "records": rows,
        "methodological_note": (
            "Both models were evaluated on the same 120 held-out model-selection records. "
            "Original human labels are the shared reference; post-hoc Gemini adjudication is "
            "not used. This is diagnostic analysis, not another independent test set, and it "
            "does not select a winner. Cost projections are mechanical extrapolations."
        ),
    }


def _category_table(values: dict[str, int]) -> str:
    labels = {
        "both_correct": "Both exactly match human reference",
        "gemini_only_correct": "Gemini matches; Luna does not",
        "luna_only_correct": "Luna matches; Gemini does not",
        "both_wrong_same_answer": "Neither matches; same model answer",
        "both_wrong_different_answer": "Neither matches; models disagree",
    }
    return "\n".join(f"| {labels[key]} | {values[key]} |" for key in CATEGORIES)


def write_pairwise(output: Path, analysis: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "pairwise_analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    policy = analysis["policy"]
    domain = analysis["primary_domain_on_human_policy_records"]
    per_domain_rows = "\n".join(
        "| {code} | {support} | {g_rate:.2%} | {l_rate:.2%} | {g_only} | {l_only} | {both} |".format(
            code=code,
            support=value["support"],
            g_rate=value["gemini_exact_match_rate"],
            l_rate=value["luna_exact_match_rate"],
            g_only=value["gemini_only_correct"],
            l_only=value["luna_only_correct"],
            both=value["errors_by_both"],
        )
        for code, value in domain["per_human_domain"].items()
    )
    gemini_cost = analysis["cost"]["gemini"]
    luna_cost = analysis["cost"]["luna"]
    report = f"""# Phase 4A frozen held-out pairwise error analysis

## Method

{analysis["methodological_note"]}

## Policy/non-policy pairwise categories (n={analysis["record_count"]})

| Category | Count |
|---|---:|
{_category_table(policy["categories"])}

- Model-vs-model policy disagreements: {policy["model_vs_model_disagreements"]}

## Primary-domain pairwise categories (human-policy records, n={analysis["human_policy_record_count"]})

| Category | Count |
|---|---:|
{_category_table(domain["categories"])}

- Model-vs-model primary-domain disagreements: {domain["model_vs_model_disagreements"]}

## Domain deltas and exact-match rates

| Human domain | Support | Gemini exact | Luna exact | Gemini-only correct | Luna-only correct | Errors by both |
|---|---:|---:|---:|---:|---:|---:|
{per_domain_rows}

## Cost comparison

| Model | Total cost USD | Cost per valid record USD | Mechanical 160k projection USD |
|---|---:|---:|---:|
| Gemini 2.5 Flash | {gemini_cost["total_benchmark_cost_usd"]} | {gemini_cost["cost_per_valid_prediction_usd"]} | {gemini_cost["mechanical_160k_cost_estimate_usd"]} |
| GPT-6 Luna | {luna_cost["total_benchmark_cost_usd"]} | {luna_cost["cost_per_valid_prediction_usd"]} | {luna_cost["mechanical_160k_cost_estimate_usd"]} |

- Gemini/Luna total-cost ratio: {analysis["cost"]["gemini_to_luna_total_cost_ratio"]:.4f}x
- Luna/Gemini total-cost ratio: {analysis["cost"]["luna_to_gemini_total_cost_ratio"]:.4f}x

No automatic winner is selected.
"""
    (output / "pairwise_analysis.md").write_text(report, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create Phase 4A pairwise error analysis")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--gemini-run", type=Path, required=True)
    parser.add_argument("--luna-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        package = load_evaluation_package(arguments.input)
        run_ids = set(_predictions(arguments.gemini_run))
        records = tuple(
            record for record in package.records if str(record["record_id"]) in run_ids
        )
        analysis = analyse_pairwise(records, arguments.gemini_run, arguments.luna_run)
        write_pairwise(arguments.output, analysis)
        print(json.dumps({key: value for key, value in analysis.items() if key != "records"}, indent=2))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
