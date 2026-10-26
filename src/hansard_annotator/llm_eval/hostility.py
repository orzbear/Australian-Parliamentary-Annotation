"""Hostility task contract, validation, metrics, and disagreement artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from hansard_annotator.llm_eval.models import HostilityPrediction
from hansard_annotator.llm_eval.validation import InvalidPrediction

PRESENCE_CODES = ("YES", "NO", "UNSURE")
TARGET_CODES = (
    "OPPOSING_PARTISAN_TARGET",
    "OTHER_POLITICAL_TARGET",
    "NON_POLITICAL_TARGET",
    "UNSURE",
)
MODEL_TARGET_CODES = (*TARGET_CODES, "N/A")
ISSUE_TYPES = (
    "ACCOUNTABILITY_FALSE_POSITIVE",
    "POLICY_ACTOR_CONFUSION",
    "INFERRED_DISHONESTY",
    "MISSED_RIDICULE",
    "MORAL_CONDEMNATION_BOUNDARY",
    "TARGET_MISCLASSIFICATION",
    "QUOTATION_ATTRIBUTION_ERROR",
    "EVIDENCE_SPAN_ERROR",
    "OTHER",
)


def prediction_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "hostility_present": {"type": "string", "enum": list(PRESENCE_CODES)},
            "target": {"type": "string", "enum": list(MODEL_TARGET_CODES)},
            "evidence_spans": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "start_offset": {"type": "integer", "minimum": 0},
                        "end_offset": {"type": "integer", "minimum": 1},
                        "text": {"type": "string", "minLength": 1},
                    },
                    "required": ["start_offset", "end_offset", "text"],
                },
            },
            "reasoning": {"type": "string", "maxLength": 1000},
        },
        "required": ["hostility_present", "target", "evidence_spans", "reasoning"],
    }


def validate_prediction(raw: object, text: str) -> HostilityPrediction:
    if not isinstance(raw, dict) or set(raw) != {
        "hostility_present",
        "target",
        "evidence_spans",
        "reasoning",
    }:
        raise InvalidPrediction("response fields do not match the hostility schema")
    presence, target = raw["hostility_present"], raw["target"]
    spans, reasoning = raw["evidence_spans"], raw["reasoning"]
    if presence not in PRESENCE_CODES:
        raise InvalidPrediction("hostility_present is unknown")
    if target not in MODEL_TARGET_CODES:
        raise InvalidPrediction("target is unknown")
    if not isinstance(reasoning, str) or len(reasoning) > 1000:
        raise InvalidPrediction("reasoning must be a short string")
    if not isinstance(spans, list):
        raise InvalidPrediction("evidence_spans must be a list")
    validated: list[dict[str, object]] = []
    for span in spans:
        if not isinstance(span, dict) or set(span) != {"start_offset", "end_offset", "text"}:
            raise InvalidPrediction("evidence span fields are invalid")
        start, end, evidence = span["start_offset"], span["end_offset"], span["text"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(evidence, str)
            or start < 0
            or end <= start
            or end > len(text)
            or text[start:end] != evidence
        ):
            raise InvalidPrediction("evidence span offsets/text do not match canonical text")
        validated.append({"start_offset": start, "end_offset": end, "text": evidence})
    if presence == "NO" and (target != "N/A" or validated):
        raise InvalidPrediction("NO requires target N/A and no evidence")
    if presence != "NO" and (target == "N/A" or not validated):
        raise InvalidPrediction("YES/UNSURE requires a target and evidence")
    return HostilityPrediction(presence, target, tuple(validated), reasoning)


def partisan_hostility(values: dict[str, Any]) -> bool:
    return values.get("hostility_present") == "YES" and values.get("target") == (
        "OPPOSING_PARTISAN_TARGET"
    )


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return numerator / denominator if denominator else 0.0


def _binary(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    tp = sum(h and p for h, p in pairs)
    tn = sum(not h and not p for h, p in pairs)
    fp = sum(not h and p for h, p in pairs)
    fn = sum(h and not p for h, p in pairs)
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    return {
        "accuracy": _ratio(tp + tn, len(pairs)),
        "precision": precision,
        "recall": recall,
        "f1": _ratio(2 * precision * recall, precision + recall),
        "confusion_matrix": {
            "true_positive": tp,
            "true_negative": tn,
            "false_positive": fp,
            "false_negative": fn,
        },
    }


def compute_metrics(
    records: tuple[dict[str, Any], ...], results: list[dict[str, Any]]
) -> dict[str, Any]:
    by_id = {str(result["record_id"]): result for result in results}
    statuses = {"success": 0, "invalid": 0, "api_failure": 0, "missing": 0}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for record in records:
        result = by_id.get(str(record["record_id"]))
        if result is None:
            statuses["missing"] += 1
        elif result.get("status") != "success" or not isinstance(result.get("prediction"), dict):
            status = str(result.get("status"))
            statuses[status if status in statuses else "invalid"] += 1
        else:
            statuses["success"] += 1
            pairs.append((record["human_annotation"]["values"], result["prediction"]))
    presence_labels = PRESENCE_CODES
    presence_confusion = {h: {p: 0 for p in presence_labels} for h in presence_labels}
    for human, model in pairs:
        presence_confusion[human["hostility_present"]][model["hostility_present"]] += 1
    identification = _binary(
        [
            (human["hostility_present"] == "YES", model["hostility_present"] == "YES")
            for human, model in pairs
        ]
    )
    identification.update(
        {
            "accuracy": _ratio(
                sum(
                    human["hostility_present"] == model["hostility_present"]
                    for human, model in pairs
                ),
                len(pairs),
            ),
            "confusion_matrix": presence_confusion,
            "human_unsure_count": sum(h["hostility_present"] == "UNSURE" for h, _ in pairs),
            "model_unsure_count": sum(p["hostility_present"] == "UNSURE" for _, p in pairs),
        }
    )
    human_yes = [(h, p) for h, p in pairs if h["hostility_present"] == "YES"]
    target_labels = TARGET_CODES
    target_confusion = {h: {p: 0 for p in MODEL_TARGET_CODES} for h in target_labels}
    exact = 0
    for human, model in human_yes:
        target_confusion[human["target"]][model["target"]] += 1
        exact += human["target"] == model["target"]
    opposing = _binary(
        [
            (h["target"] == "OPPOSING_PARTISAN_TARGET", p["target"] == "OPPOSING_PARTISAN_TARGET")
            for h, p in human_yes
        ]
    )
    target = {
        "human_yes_support": len(human_yes),
        "exact_target_accuracy": _ratio(exact, len(human_yes)),
        "opposing_partisan_target": opposing,
        "confusion_matrix": target_confusion,
        "support_by_category": {
            code: sum(h["target"] == code for h, _ in human_yes) for code in TARGET_CODES
        },
        "low_support_warning": "Do not over-interpret low-support target categories.",
    }
    total = len(records)
    return {
        "task": "hostility",
        "total_records": total,
        "evaluated_records": len(pairs),
        "status_counts": statuses,
        "invalid_rate": _ratio(statuses["invalid"], total),
        "api_failure_rate": _ratio(statuses["api_failure"], total),
        "missing_rate": _ratio(statuses["missing"], total),
        "hostility_identification": identification,
        "target_classification": target,
        "partisan_hostility": _binary(
            [(partisan_hostility(h), partisan_hostility(p)) for h, p in pairs]
        ),
    }


def disagreements(
    records: tuple[dict[str, Any], ...],
    results: list[dict[str, Any]],
    *,
    run_identity: str | None,
    configured_model: str | None,
) -> list[dict[str, Any]]:
    by_id = {str(result["record_id"]): result for result in results}
    output: list[dict[str, Any]] = []
    for record in records:
        result = by_id.get(str(record["record_id"]))
        if not result or result.get("status") != "success":
            continue
        human, model = record["human_annotation"]["values"], result["prediction"]
        if (human["hostility_present"], human.get("target")) == (
            model["hostility_present"],
            None if model["target"] == "N/A" else model["target"],
        ):
            continue
        speech = record["speech"]
        output.append(
            {
                "record_id": record["record_id"],
                "turn_key": speech["turn_key"],
                "speaker": speech.get("speaker_name"),
                "date": speech.get("date"),
                "major_heading": speech.get("major_heading"),
                "minor_heading": speech.get("minor_heading"),
                "text_clean": speech["text"],
                "human_hostility_present": human["hostility_present"],
                "human_target": human.get("target") or "N/A",
                "human_evidence": human["evidence_span"],
                "model_hostility_present": model["hostility_present"],
                "model_target": model["target"],
                "model_evidence": model["evidence_spans"],
                "model_reasoning": model.get("reasoning", ""),
                "human_partisan_hostility": partisan_hostility(human),
                "model_partisan_hostility": partisan_hostility(model),
                "returned_model": result.get("returned_model"),
                "configured_model": configured_model,
                "run_identity": run_identity,
                "adjudication_outcome": "",
                "issue_type": "",
                "rationale": "",
                "human_label_revision_recommended": "",
                "prompt_note": "",
            }
        )
    return output


def write_reports(
    output: Path, metrics: dict[str, Any], costs: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    for name, value in (("metrics.json", metrics), ("cost_report.json", costs)):
        (output / name).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    with (output / "mismatches.jsonl").open("w", encoding="utf-8", newline="\n") as target:
        for row in rows:
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    if rows:
        with (output / "mismatches.csv").open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: json.dumps(value, ensure_ascii=False)
                        if isinstance(value, list)
                        else value
                        for key, value in row.items()
                    }
                )
    else:
        (output / "mismatches.csv").write_text("record_id\n", encoding="utf-8")
    identification, target = metrics["hostility_identification"], metrics["target_classification"]
    partisan = metrics["partisan_hostility"]
    (output / "report.md").write_text(
        "# Hostility development evaluation report\n\n"
        "Development data used for prompt refinement; these are not final or held-out results.\n\n"
        f"- Valid predictions: {metrics['evaluated_records']} / {metrics['total_records']}\n"
        f"- Hostility YES precision/recall/F1: {identification['precision']:.2%} / "
        f"{identification['recall']:.2%} / {identification['f1']:.2%}\n"
        f"- Human-YES target support: {target['human_yes_support']}\n"
        f"- Exact target accuracy: {target['exact_target_accuracy']:.2%}\n"
        f"- Partisan-hostility precision/recall/F1: {partisan['precision']:.2%} / "
        f"{partisan['recall']:.2%} / {partisan['f1']:.2%}\n"
        f"- Disagreements: {len(rows)}\n",
        encoding="utf-8",
    )
