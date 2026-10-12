"""Research-only post-hoc analysis of manually adjudicated mismatches."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from hansard_annotator.llm_eval.metrics import compute_metrics

OUTCOMES = (
    "human_correct",
    "human_needs_revision",
    "model_error",
    "codebook_ambiguous",
)
POLICY_STATUSES = ("policy", "non-policy")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} line {number} must contain an object")
        output.append(value)
    return output


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return [dict(row) for row in csv.DictReader(source)]


def _unique_by_id(rows: list[dict[str, Any]] | list[dict[str, str]], label: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for row in rows:
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"{label} contains a missing record_id")
        if record_id in output:
            raise ValueError(f"{label} contains duplicate record_id: {record_id}")
        output[record_id] = row
    return output


def _validate_adjudications(
    mismatch_rows: list[dict[str, str]], adjudication_rows: list[dict[str, str]]
) -> dict[str, dict[str, str]]:
    mismatches = _unique_by_id(mismatch_rows, "original mismatches")
    adjudications = _unique_by_id(adjudication_rows, "adjudications")
    missing = set(mismatches) - set(adjudications)
    unexpected = set(adjudications) - set(mismatches)
    if missing:
        raise ValueError(f"missing adjudication rows: {', '.join(sorted(missing))}")
    if unexpected:
        raise ValueError(f"unexpected adjudication rows: {', '.join(sorted(unexpected))}")
    output: dict[str, dict[str, str]] = {}
    for record_id, generic_row in adjudications.items():
        row = generic_row
        mismatch = mismatches[record_id]
        outcome = row.get("adjudication_outcome", "")
        status = row.get("adjudicated_policy_status", "")
        if outcome not in OUTCOMES:
            raise ValueError(f"invalid adjudication_outcome for {record_id}")
        if status not in POLICY_STATUSES:
            raise ValueError(f"invalid adjudicated_policy_status for {record_id}")
        if row.get("human_policy_status") not in POLICY_STATUSES:
            raise ValueError(f"invalid human_policy_status for {record_id}")
        if row.get("model_policy_status") not in POLICY_STATUSES:
            raise ValueError(f"invalid model_policy_status for {record_id}")
        if not row.get("issue_type") or not row.get("rationale"):
            raise ValueError(f"missing issue_type or rationale for {record_id}")
        if row.get("human_label_revision_recommended") not in {"yes", "no"}:
            raise ValueError(f"invalid human_label_revision_recommended for {record_id}")
        primary = row.get("adjudicated_primary_domain", "")
        if status == "non-policy" and primary:
            raise ValueError(f"non-policy adjudication has a primary domain for {record_id}")
        if status == "policy" and not primary and outcome != "codebook_ambiguous":
            raise ValueError(f"policy adjudication is missing a primary domain for {record_id}")
        expected_human_status = (
            "non-policy" if mismatch.get("human_is_non_policy") == "True" else "policy"
        )
        expected_model_status = (
            "non-policy" if mismatch.get("model_is_non_policy") == "True" else "policy"
        )
        factual_pairs = (
            (row.get("human_policy_status"), expected_human_status),
            (row.get("human_primary_domain", ""), mismatch.get("human_primary_domain", "")),
            (row.get("model_policy_status"), expected_model_status),
            (row.get("model_primary_domain", ""), mismatch.get("model_primary_domain", "")),
        )
        if any(actual != expected for actual, expected in factual_pairs):
            raise ValueError(f"adjudication factual fields differ from mismatch for {record_id}")
        output[record_id] = row
    return output


def _metric_record(record_id: str, status: str, primary: str) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "human_annotation": {
            "values": {
                "is_non_policy": status == "non-policy",
                "primary_australian_domain": primary or None,
            }
        },
    }


def _metric_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": result["record_id"],
        "status": result["status"],
        "prediction": result["prediction"],
    }


def _issue_summary(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        groups[row["issue_type"]].append(row["adjudication_outcome"])
    output: list[dict[str, object]] = []
    for issue_type in sorted(groups):
        tendencies = Counter(
            "favoured model prediction"
            if outcome == "human_needs_revision"
            else "remained ambiguous"
            if outcome == "codebook_ambiguous"
            else "favoured human label"
            for outcome in groups[issue_type]
        )
        maximum = max(tendencies.values())
        leaders = [name for name, count in tendencies.items() if count == maximum]
        output.append(
            {
                "issue_type": issue_type,
                "count": len(groups[issue_type]),
                "general_tendency": leaders[0] if len(leaders) == 1 else "mixed",
            }
        )
    return output


def analyse(run_dir: Path, adjudication_path: Path) -> dict[str, Any]:
    original = _read_json(run_dir / "metrics.json")
    predictions = _read_jsonl(run_dir / "predictions.jsonl")
    mismatch_rows = _read_csv(run_dir / "mismatches.csv")
    adjudication_rows = _read_csv(adjudication_path)
    adjudications = _validate_adjudications(mismatch_rows, adjudication_rows)
    prediction_by_id = _unique_by_id(predictions, "predictions")
    valid_predictions = {
        record_id: result
        for record_id, result in prediction_by_id.items()
        if result.get("status") == "success" and isinstance(result.get("prediction"), dict)
    }
    if len(valid_predictions) != int(original["evaluated_records"]):
        raise ValueError("valid prediction count differs from original metrics")
    if not set(adjudications) <= set(valid_predictions):
        raise ValueError("an adjudicated mismatch has no valid prediction")

    domain_codes = tuple(str(code) for code in original["primary_domain"]["per_domain"])
    policy_records: list[dict[str, Any]] = []
    metric_results: list[dict[str, Any]] = []
    domain_records: list[dict[str, Any]] = []
    domain_results: list[dict[str, Any]] = []
    policy_changes = 0
    unresolved_domain_ids: list[str] = []
    for record_id, result in valid_predictions.items():
        prediction = result["prediction"]
        row = adjudications.get(record_id)
        if row is None:
            reference_status = "non-policy" if prediction["is_non_policy"] else "policy"
            reference_primary = str(prediction.get("primary_australian_domain") or "")
        else:
            reference_status = row["adjudicated_policy_status"]
            reference_primary = row["adjudicated_primary_domain"]
            policy_changes += reference_status != row["human_policy_status"]
        policy_primary = (
            reference_primary
            if reference_status == "non-policy" or reference_primary
            else domain_codes[0]
        )
        policy_records.append(_metric_record(record_id, reference_status, policy_primary))
        metric_result = _metric_result(result)
        metric_results.append(metric_result)
        unresolved = (
            row is not None
            and row["adjudication_outcome"] == "codebook_ambiguous"
            and reference_status == "policy"
            and not reference_primary
        )
        if unresolved:
            unresolved_domain_ids.append(record_id)
        else:
            domain_records.append(_metric_record(record_id, reference_status, reference_primary))
            domain_results.append(metric_result)

    policy_metrics = compute_metrics(tuple(policy_records), metric_results, domain_codes)["policy"]
    domain_metrics = compute_metrics(tuple(domain_records), domain_results, domain_codes)[
        "primary_domain"
    ]
    outcome_counts = {outcome: 0 for outcome in OUTCOMES}
    for row in adjudication_rows:
        outcome_counts[row["adjudication_outcome"]] += 1
    issue_summary = _issue_summary(adjudication_rows)
    original_policy = original["policy"]
    original_domain = original["primary_domain"]
    return {
        "analysis_type": "post-hoc adjudication/error analysis, not the original held-out benchmark",
        "original_human_reference_held_out_result": {
            "selected_records": original["total_records"],
            "valid_predictions": original["evaluated_records"],
            "policy_accuracy": original_policy["accuracy"],
            "policy_precision": original_policy["precision"],
            "policy_recall": original_policy["recall"],
            "policy_f1": original_policy["f1"],
            "primary_domain_exact_match": original_domain["exact_match_accuracy"],
            "primary_domain_macro_f1": original_domain["macro_f1"],
            "original_disagreement_count": len(mismatch_rows),
        },
        "adjudication_outcomes": {
            **outcome_counts,
            "total_adjudicated_mismatches": len(adjudication_rows),
            "original_agreements_not_manually_disputed": len(valid_predictions)
            - len(adjudication_rows),
            "human_label_revision_recommendations": sum(
                row["human_label_revision_recommended"] == "yes" for row in adjudication_rows
            ),
            "unresolved_taxonomy_or_codebook_cases": outcome_counts["codebook_ambiguous"],
        },
        "adjudicated_policy_gate": {
            **policy_metrics,
            "policy_labels_changed_by_adjudication": policy_changes,
        },
        "adjudicated_primary_domain": {
            **domain_metrics,
            "resolvable_policy_domain_cases": domain_metrics["evaluated_policy_records"],
            "excluded_unresolved_taxonomy_ambiguity": len(unresolved_domain_ids),
            "excluded_record_ids": sorted(unresolved_domain_ids),
        },
        "issue_type_counts": dict(
            sorted(Counter(row["issue_type"] for row in adjudication_rows).items())
        ),
        "issue_type_summary": issue_summary,
        "methodological_warning": (
            "The 120-record set remains the held-out validation/model-selection set. "
            "Adjudication occurred after observing model disagreements, so these adjusted "
            "metrics are diagnostic and post-hoc and do not replace the original human-reference "
            "benchmark. phase4a-policy-v2 remains frozen; future substantive prompt changes "
            "require V3 and a new evaluation strategy."
        ),
    }


def _percent(value: Any) -> str:
    return f"{float(value):.2%}"


def write_analysis(run_dir: Path, summary: dict[str, Any]) -> None:
    (run_dir / "adjudicated_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    original = summary["original_human_reference_held_out_result"]
    outcomes = summary["adjudication_outcomes"]
    policy = summary["adjudicated_policy_gate"]
    domain = summary["adjudicated_primary_domain"]
    issue_rows = "\n".join(
        f"| {row['issue_type']} | {row['count']} | {row['general_tendency']} |"
        for row in summary["issue_type_summary"]
    )
    domain_rows = "\n".join(
        f"| {code} | {values['support']} | {_percent(values['f1'])} |"
        for code, values in domain["per_domain"].items()
    )
    report = f"""# Phase 4A held-out post-hoc adjudication analysis

**This is post-hoc adjudication/error analysis, not the original held-out benchmark.**

## Original human-reference held-out result

These values are repeated from the frozen original `metrics.json`; they are not replaced or recalculated here.

- Selected records: {original["selected_records"]}
- Valid predictions: {original["valid_predictions"]}
- Policy/non-policy accuracy: {_percent(original["policy_accuracy"])}
- Policy precision: {_percent(original["policy_precision"])}
- Policy recall: {_percent(original["policy_recall"])}
- Policy F1: {_percent(original["policy_f1"])}
- Primary-domain exact match: {_percent(original["primary_domain_exact_match"])}
- Primary-domain macro-F1: {_percent(original["primary_domain_macro_f1"])}
- Original disagreements: {original["original_disagreement_count"]}

## Adjudication outcomes

- Total adjudicated mismatches: {outcomes["total_adjudicated_mismatches"]}
- Original agreements not manually disputed: {outcomes["original_agreements_not_manually_disputed"]}
- Human correct: {outcomes["human_correct"]}
- Human needs revision: {outcomes["human_needs_revision"]}
- Model error: {outcomes["model_error"]}
- Codebook ambiguous: {outcomes["codebook_ambiguous"]}
- Human-label revision recommendations: {outcomes["human_label_revision_recommendations"]}
- Unresolved taxonomy/codebook cases: {outcomes["unresolved_taxonomy_or_codebook_cases"]}

## Adjudicated policy-gate analysis

- Accuracy: {_percent(policy["accuracy"])}
- Precision: {_percent(policy["precision"])}
- Recall: {_percent(policy["recall"])}
- F1: {_percent(policy["f1"])}
- Policy labels changed by adjudication: {policy["policy_labels_changed_by_adjudication"]}

## Adjudicated primary-domain analysis

- Resolvable policy-domain cases: {domain["resolvable_policy_domain_cases"]}
- Excluded unresolved taxonomy ambiguity: {domain["excluded_unresolved_taxonomy_ambiguity"]}
- Exact match: {_percent(domain["exact_match_accuracy"])}
- Macro-F1: {_percent(domain["macro_f1"])}

| Domain | Support | F1 |
|---|---:|---:|
{domain_rows}

## Error-analysis summary

| Issue type | Count | General adjudication tendency |
|---|---:|---|
{issue_rows}

## Methodological warning

{summary["methodological_warning"]}
"""
    (run_dir / "adjudicated_report.md").write_text(report, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post-hoc analysis of held-out adjudications")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--adjudication", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        summary = analyse(arguments.run_dir, arguments.adjudication)
        write_analysis(arguments.run_dir, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
