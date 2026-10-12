"""Deterministic machine-readable and Markdown evaluation reports."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def mismatches(
    records: tuple[dict[str, Any], ...],
    results: list[dict[str, Any]],
    *,
    snippet_chars: int,
    run_identity: str | None = None,
    configured_model: str | None = None,
) -> list[dict[str, Any]]:
    by_id = {result["record_id"]: result for result in results}
    output: list[dict[str, Any]] = []
    for record in records:
        result = by_id.get(record["record_id"])
        if not result or result.get("status") != "success":
            continue
        human = record["human_annotation"]["values"]
        prediction = result["prediction"]
        if human["is_non_policy"] == prediction["is_non_policy"] and human.get(
            "primary_australian_domain"
        ) == prediction.get("primary_australian_domain"):
            continue
        speech = record["speech"]
        output.append(
            {
                "record_id": record["record_id"],
                "turn_key": speech["turn_key"],
                "speaker": speech.get("speaker_name"),
                "date": speech.get("date"),
                "chamber": speech.get("chamber"),
                "major_heading": speech.get("major_heading"),
                "minor_heading": speech.get("minor_heading"),
                "business_type": speech.get("business_type"),
                "speech_text": speech["text"][:snippet_chars] if snippet_chars else speech["text"],
                "human_is_non_policy": human["is_non_policy"],
                "model_is_non_policy": prediction["is_non_policy"],
                "human_primary_domain": human.get("primary_australian_domain"),
                "model_primary_domain": prediction.get("primary_australian_domain"),
                "human_secondary_domains": human.get("secondary_australian_domains", []),
                "model_secondary_domains": prediction.get("secondary_australian_domains", []),
                "model_reasoning": prediction.get("reasoning"),
                "returned_model": result.get("returned_model"),
                "configured_model": configured_model,
                "run_identity": run_identity,
            }
        )
    return output


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_reports(
    output: Path,
    metrics: dict[str, Any],
    costs: dict[str, Any],
    mismatch_rows: list[dict[str, Any]],
) -> None:
    _write_json(output / "metrics.json", metrics)
    _write_json(output / "cost_report.json", costs)
    with (output / "mismatches.jsonl").open("w", encoding="utf-8", newline="\n") as destination:
        for row in mismatch_rows:
            destination.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    if mismatch_rows:
        with (output / "mismatches.csv").open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=list(mismatch_rows[0]))
            writer.writeheader()
            for source in mismatch_rows:
                row = dict(source)
                row["human_secondary_domains"] = " | ".join(source["human_secondary_domains"])
                row["model_secondary_domains"] = " | ".join(source["model_secondary_domains"])
                writer.writerow(row)
    else:
        (output / "mismatches.csv").write_text("record_id\n", encoding="utf-8")
    policy = metrics["policy"]
    domain = metrics["primary_domain"]
    report = f"""# Phase 4A evaluation report

Human annotations are the evaluation reference. A mismatch does not establish that either the human or model label is correct.

## Coverage

- Total selected records: {metrics["total_records"]}
- Valid evaluated records: {metrics["evaluated_records"]}
- Invalid structured outputs: {metrics["status_counts"]["invalid"]} ({metrics["invalid_rate"]:.2%})
- API failures: {metrics["status_counts"]["api_failure"]} ({metrics["api_failure_rate"]:.2%})
- Missing predictions: {metrics["status_counts"]["missing"]} ({metrics["missing_rate"]:.2%})

## Agreement

- Policy/non-policy accuracy: {policy["accuracy"]:.2%}
- Policy precision / recall / F1: {policy["precision"]:.2%} / {policy["recall"]:.2%} / {policy["f1"]:.2%}
- Primary-domain exact match: {domain["exact_match_accuracy"]:.2%}
- Primary-domain macro-F1: {domain["macro_f1"]:.2%}
- Human/model disagreements: {len(mismatch_rows)}

## Cost

- Estimated total USD: {costs["estimated_cost_usd"] if costs["estimated_cost_usd"] is not None else "unavailable (no matching pricing entry)"}
- Cost per attempt USD: {costs["cost_per_attempt_usd"] if costs["cost_per_attempt_usd"] is not None else "unavailable"}
- Cost per valid annotation USD: {costs["cost_per_valid_annotation_usd"] if costs["cost_per_valid_annotation_usd"] is not None else "unavailable"}
- Projected 1,000 attempts USD: {costs["projected_cost_1k_attempts_usd"] if costs["projected_cost_1k_attempts_usd"] is not None else "unavailable"}
- Projected 160,000 attempts USD: {costs["projected_cost_160k_attempts_usd"] if costs["projected_cost_160k_attempts_usd"] is not None else "unavailable"}
- Projected 1,000 valid annotations USD: {costs["projected_cost_1k_valid_annotations_usd"] if costs["projected_cost_1k_valid_annotations_usd"] is not None else "unavailable"}
- Projected 160,000 valid annotations USD: {costs["projected_cost_160k_valid_annotations_usd"] if costs["projected_cost_160k_valid_annotations_usd"] is not None else "unavailable"}

{costs["projection_note"]}
"""
    (output / "report.md").write_text(report, encoding="utf-8")
