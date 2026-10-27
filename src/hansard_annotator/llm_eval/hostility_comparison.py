"""Deterministic pairwise artifacts for two hostility development runs."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from hansard_annotator.llm_eval.hostility import partisan_hostility


def compare_hostility_runs(
    records: tuple[dict[str, Any], ...],
    first_results: list[dict[str, Any]],
    second_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    first_by_id = {str(row["record_id"]): row for row in first_results}
    second_by_id = {str(row["record_id"]): row for row in second_results}
    rows: list[dict[str, Any]] = []
    for record in records:
        record_id = str(record["record_id"])
        first = first_by_id.get(record_id, {})
        second = second_by_id.get(record_id, {})
        human = record["human_annotation"]["values"]
        first_prediction = first.get("prediction") if first.get("status") == "success" else None
        second_prediction = second.get("prediction") if second.get("status") == "success" else None
        first_correct = _labels_match(human, first_prediction)
        second_correct = _labels_match(human, second_prediction)
        status = _comparison_status(
            human, first_prediction, second_prediction, first_correct, second_correct
        )
        rows.append(
            {
                "record_id": record_id,
                "turn_key": record["speech"]["turn_key"],
                "corrected_human_hostility": human["hostility_present"],
                "corrected_human_target": human.get("target") or "N/A",
                "v1_1_hostility": _value(first_prediction, "hostility_present"),
                "v1_1_target": _value(first_prediction, "target"),
                "v2_hostility": _value(second_prediction, "hostility_present"),
                "v2_target": _value(second_prediction, "target"),
                "human_partisan_hostility": partisan_hostility(human),
                "v1_1_partisan_hostility": partisan_hostility(first_prediction or {}),
                "v2_partisan_hostility": partisan_hostility(second_prediction or {}),
                "v1_1_reasoning": _value(first_prediction, "reasoning"),
                "v2_reasoning": _value(second_prediction, "reasoning"),
                "v1_1_evidence": _value(first_prediction, "evidence_spans", []),
                "v2_evidence": _value(second_prediction, "evidence_spans", []),
                "comparison_status": status,
            }
        )
    return rows


def build_comparison_summary(
    first_metrics: dict[str, Any],
    second_metrics: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    status_counts = Counter(str(row["comparison_status"]) for row in rows)
    sections: dict[str, Any] = {}
    for section in ("hostility_identification", "partisan_hostility"):
        first = first_metrics[section]
        second = second_metrics[section]
        sections[section] = {
            "v1_1": first,
            "v2": second,
            "delta": {
                key: second[key] - first[key]
                for key in ("accuracy", "precision", "recall", "f1")
            },
        }
    sections["target_classification"] = {
        "v1_1": first_metrics["target_classification"],
        "v2": second_metrics["target_classification"],
        "exact_target_accuracy_delta": (
            second_metrics["target_classification"]["exact_target_accuracy"]
            - first_metrics["target_classification"]["exact_target_accuracy"]
        ),
    }
    return {
        "record_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        **sections,
        "evidence_quality": {
            "v1_1": first_metrics["evidence_quality"],
            "v2": second_metrics["evidence_quality"],
        },
    }


def write_hostility_comparison(
    output: Path,
    rows: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "comparison.jsonl").open("w", encoding="utf-8", newline="\n") as target:
        for row in rows:
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    fieldnames = list(rows[0]) if rows else ["record_id"]
    with (output / "comparison.csv").open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in row.items()
                }
            )
    if summary is not None:
        (output / "comparison_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        identification = summary["hostility_identification"]
        partisan = summary["partisan_hostility"]
        target = summary["target_classification"]
        statuses = summary["status_counts"]
        (output / "report.md").write_text(
            "# Corrected-gold hostility V1.1 versus V2\n\n"
            "Both runs use the same corrected 100-record development snapshot. These are not "
            "held-out results.\n\n"
            "| Measure | V1.1 | V2 | Delta |\n"
            "|---|---:|---:|---:|\n"
            f"| Hostility precision | {identification['v1_1']['precision']:.2%} | "
            f"{identification['v2']['precision']:.2%} | "
            f"{identification['delta']['precision']:+.2%} |\n"
            f"| Hostility recall | {identification['v1_1']['recall']:.2%} | "
            f"{identification['v2']['recall']:.2%} | "
            f"{identification['delta']['recall']:+.2%} |\n"
            f"| Hostility F1 | {identification['v1_1']['f1']:.2%} | "
            f"{identification['v2']['f1']:.2%} | "
            f"{identification['delta']['f1']:+.2%} |\n"
            f"| Target exact accuracy | {target['v1_1']['exact_target_accuracy']:.2%} | "
            f"{target['v2']['exact_target_accuracy']:.2%} | "
            f"{target['exact_target_accuracy_delta']:+.2%} |\n"
            f"| Partisan-hostility precision | {partisan['v1_1']['precision']:.2%} | "
            f"{partisan['v2']['precision']:.2%} | "
            f"{partisan['delta']['precision']:+.2%} |\n"
            f"| Partisan-hostility recall | {partisan['v1_1']['recall']:.2%} | "
            f"{partisan['v2']['recall']:.2%} | "
            f"{partisan['delta']['recall']:+.2%} |\n"
            f"| Partisan-hostility F1 | {partisan['v1_1']['f1']:.2%} | "
            f"{partisan['v2']['f1']:.2%} | {partisan['delta']['f1']:+.2%} |\n\n"
            "## Record-level comparison\n\n"
            + "\n".join(f"- {key}: {value}" for key, value in statuses.items())
            + "\n",
            encoding="utf-8",
        )


def _value(prediction: object, key: str, default: object = "") -> object:
    return prediction.get(key, default) if isinstance(prediction, dict) else default


def _labels_match(human: dict[str, Any], prediction: object) -> bool:
    if not isinstance(prediction, dict):
        return False
    return (human["hostility_present"], human.get("target") or "N/A") == (
        prediction.get("hostility_present"),
        prediction.get("target"),
    )


def _comparison_status(
    human: dict[str, Any],
    first: object,
    second: object,
    first_correct: bool,
    second_correct: bool,
) -> str:
    if first_correct and second_correct:
        return "BOTH_CORRECT"
    if first_correct:
        return "V1_ONLY_CORRECT"
    if second_correct:
        return "V2_ONLY_CORRECT"
    if (
        isinstance(first, dict)
        and isinstance(second, dict)
        and first.get("hostility_present") == human["hostility_present"]
        and second.get("hostility_present") == human["hostility_present"]
        and first.get("target") != second.get("target")
    ):
        return "DIFFERENT_TARGET_ONLY"
    return "BOTH_WRONG"
