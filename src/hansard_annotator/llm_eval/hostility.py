"""Hostility task contract, validation, metrics, and disagreement artifacts."""

from __future__ import annotations

import csv
import json
import unicodedata
from pathlib import Path
from typing import Any

from hansard_annotator.llm_eval.failure_categories import FailureCategory
from hansard_annotator.llm_eval.models import HostilityPrediction
from hansard_annotator.llm_eval.prompt import (
    HOSTILITY_LUNA_PROMPT_VERSION,
    HOSTILITY_PRODUCTION_PROMPT_VERSION,
)
from hansard_annotator.llm_eval.validation import InvalidPrediction

PRESENCE_CODES = ("YES", "NO", "UNSURE")
TARGET_CODES = (
    "OPPOSING_PARTISAN_TARGET",
    "OTHER_POLITICAL_TARGET",
    "NON_POLITICAL_TARGET",
    "UNSURE",
)
MODEL_TARGET_CODES = (*TARGET_CODES, "N/A")
HOSTILITY_PRODUCTION_CONTRACT_VERSION = "hostility-output-v2-production-no-reasoning"
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
MAX_REASONING_LENGTH = 4000


class HostilityValidationError(InvalidPrediction):
    """A hostility response failed a specific structured or semantic contract."""

    def __init__(self, message: str, category: FailureCategory) -> None:
        super().__init__(message)
        self.category = category


_QUOTE_NORMALIZATION = str.maketrans(
    {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "`": "'", "\u00b4": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    }
)
_DASH_NORMALIZATION = str.maketrans(
    {character: "-" for character in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"}
)


def _normalized_evidence_with_offsets(value: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize only quote/dash/space variants and retain source-codepoint bounds."""
    normalized: list[str] = []
    bounds: list[tuple[int, int]] = []
    for offset, character in enumerate(value):
        if character.isspace() or unicodedata.category(character) == "Zs":
            if normalized and normalized[-1] == " ":
                bounds[-1] = (bounds[-1][0], offset + 1)
            else:
                normalized.append(" ")
                bounds.append((offset, offset + 1))
            continue
        mapped = character.translate(_QUOTE_NORMALIZATION).translate(_DASH_NORMALIZATION)
        normalized.append(mapped)
        bounds.append((offset, offset + 1))
    if normalized and normalized[0] == " ":
        normalized.pop(0)
        bounds.pop(0)
    if normalized and normalized[-1] == " ":
        normalized.pop()
        bounds.pop()
    return "".join(normalized), bounds


def prediction_json_schema(prompt_version: str = "hostility-prompt-v1.1") -> dict[str, Any]:
    no_reasoning = prompt_version in {
        HOSTILITY_PRODUCTION_PROMPT_VERSION,
        HOSTILITY_LUNA_PROMPT_VERSION,
    }
    evidence_properties: dict[str, Any] = {
        "text": {"type": "string", "minLength": 1},
    }
    evidence_required = ["text"]
    if prompt_version == "hostility-prompt-v1":
        evidence_properties = {
            "start_offset": {"type": "integer", "minimum": 0},
            "end_offset": {"type": "integer", "minimum": 1},
            **evidence_properties,
        }
        evidence_required = ["start_offset", "end_offset", "text"]
    properties: dict[str, Any] = {
        "hostility_present": {"type": "string", "enum": list(PRESENCE_CODES)},
        "target": {"type": "string", "enum": list(MODEL_TARGET_CODES)},
        "evidence_spans": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": evidence_properties,
                "required": evidence_required,
            },
        },
    }
    required = ["hostility_present", "target", "evidence_spans"]
    if not no_reasoning:
        properties["reasoning"] = {
            "type": "string",
            "maxLength": (
                1000
                if prompt_version in {"hostility-prompt-v1", "hostility-prompt-v1.1"}
                else MAX_REASONING_LENGTH
            ),
        }
        required.append("reasoning")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def validate_prediction(
    raw: object, text: str, prompt_version: str = "hostility-prompt-v1.1"
) -> HostilityPrediction:
    prediction, _ = validate_prediction_with_diagnostics(raw, text, prompt_version)
    return prediction


def validate_prediction_with_diagnostics(
    raw: object, text: str, prompt_version: str = "hostility-prompt-v1.1"
) -> tuple[HostilityPrediction, dict[str, object]]:
    expected_fields = {"hostility_present", "target", "evidence_spans"}
    if prompt_version not in {
        HOSTILITY_PRODUCTION_PROMPT_VERSION,
        HOSTILITY_LUNA_PROMPT_VERSION,
    }:
        expected_fields.add("reasoning")
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise HostilityValidationError(
            "response fields do not match the hostility schema",
            FailureCategory.STRUCTURED_OUTPUT_INVALID,
        )
    presence, target = raw["hostility_present"], raw["target"]
    spans = raw["evidence_spans"]
    reasoning = raw.get("reasoning", "")
    if presence not in PRESENCE_CODES:
        raise HostilityValidationError(
            "hostility_present is unknown", FailureCategory.STRUCTURED_OUTPUT_INVALID
        )
    if target not in MODEL_TARGET_CODES:
        raise HostilityValidationError(
            "target is unknown", FailureCategory.STRUCTURED_OUTPUT_INVALID
        )
    if not isinstance(reasoning, str) or len(reasoning) > MAX_REASONING_LENGTH:
        raise HostilityValidationError(
            "reasoning must be a short string", FailureCategory.STRUCTURED_OUTPUT_INVALID
        )
    if not isinstance(spans, list):
        raise HostilityValidationError(
            "evidence_spans must be a list", FailureCategory.STRUCTURED_OUTPUT_INVALID
        )
    validated: list[dict[str, object]] = []
    unmatched: list[str] = []
    ambiguous: list[str] = []
    model_supplied: list[str] = []
    normalized_text, text_bounds = _normalized_evidence_with_offsets(text)
    for span in spans:
        expected_fields = (
            {"start_offset", "end_offset", "text"}
            if prompt_version == "hostility-prompt-v1"
            else {"text"}
        )
        if not isinstance(span, dict) or set(span) != expected_fields:
            raise HostilityValidationError(
                "evidence span fields are invalid", FailureCategory.STRUCTURED_OUTPUT_INVALID
            )
        evidence = span["text"]
        if not isinstance(evidence, str) or not evidence:
            raise HostilityValidationError(
                "evidence text must be a non-empty string",
                FailureCategory.STRUCTURED_OUTPUT_INVALID,
            )
        model_supplied.append(evidence)
        if prompt_version == "hostility-prompt-v1":
            start, end = span["start_offset"], span["end_offset"]
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or end > len(text)
                or text[start:end] != evidence
            ):
                raise HostilityValidationError(
                    "evidence span offsets/text do not match canonical text",
                    FailureCategory.EVIDENCE_RESOLUTION_FAILURE,
                )
            validated.append({"start_offset": start, "end_offset": end, "text": evidence})
            continue
        normalized_evidence, _ = _normalized_evidence_with_offsets(evidence)
        if not normalized_evidence:
            unmatched.append(evidence)
            continue
        start = normalized_text.find(normalized_evidence)
        if start < 0:
            unmatched.append(evidence)
        elif normalized_text.find(normalized_evidence, start + 1) >= 0:
            ambiguous.append(evidence)
        else:
            end = start + len(normalized_evidence)
            source_start = text_bounds[start][0]
            source_end = text_bounds[end - 1][1]
            canonical_span = text[source_start:source_end]
            validated.append(
                {
                    "start_offset": source_start,
                    "end_offset": source_end,
                    "text": canonical_span,
                }
            )
    if presence == "NO" and (target != "N/A" or spans):
        raise HostilityValidationError(
            "NO requires target N/A and no evidence",
            FailureCategory.SEMANTIC_CONTRACT_INVALID,
        )
    if presence != "NO" and (target == "N/A" or not spans):
        raise HostilityValidationError(
            "YES/UNSURE requires a target and evidence text",
            FailureCategory.SEMANTIC_CONTRACT_INVALID,
        )
    diagnostics: dict[str, object] = {
        "classification_valid": True,
        "evidence_valid": not unmatched and not ambiguous,
        "evidence_unmatched": unmatched,
        "evidence_ambiguous": ambiguous,
        "evidence_requested_count": len(spans),
        "evidence_resolved_count": len(validated),
        "evidence_model_supplied": model_supplied,
        "evidence_verified_canonical": [item["text"] for item in validated],
    }
    return HostilityPrediction(presence, target, tuple(validated), reasoning), diagnostics


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
        "evidence_quality": {
            "classification_valid": len(pairs),
            "valid": sum(
                bool(by_id[str(record["record_id"])].get("diagnostics", {}).get("evidence_valid"))
                for record in records
                if str(record["record_id"]) in by_id
                and by_id[str(record["record_id"])].get("status") == "success"
            ),
            "unmatched": sum(
                bool(
                    by_id[str(record["record_id"])].get("diagnostics", {}).get("evidence_unmatched")
                )
                for record in records
                if str(record["record_id"]) in by_id
            ),
            "ambiguous": sum(
                bool(
                    by_id[str(record["record_id"])].get("diagnostics", {}).get("evidence_ambiguous")
                )
                for record in records
                if str(record["record_id"]) in by_id
            ),
        },
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
        labels_match = (human["hostility_present"], human.get("target")) == (
            model["hostility_present"],
            None if model["target"] == "N/A" else model["target"],
        )
        evidence_valid = result.get("diagnostics", {}).get("evidence_valid")
        if labels_match and evidence_valid is not False:
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
                "classification_valid": result.get("diagnostics", {}).get("classification_valid"),
                "evidence_valid": result.get("diagnostics", {}).get("evidence_valid"),
                "evidence_unmatched": result.get("diagnostics", {}).get("evidence_unmatched", []),
                "evidence_ambiguous": result.get("diagnostics", {}).get("evidence_ambiguous", []),
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
    output: Path,
    metrics: dict[str, Any],
    costs: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    comparative_benchmark: bool = False,
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
    evidence = metrics["evidence_quality"]
    title = (
        "# Frozen comparative hostility candidate report"
        if comparative_benchmark
        else "# Hostility development evaluation report"
    )
    description = (
        "Frozen comparative benchmark set; reference labels are final human-reviewed annotations. "
        "This is not a new untouched held-out set.\n\n"
        if comparative_benchmark
        else "Development data used for prompt refinement; these are not final or held-out results.\n\n"
    )
    (output / "report.md").write_text(
        f"{title}\n\n"
        f"{description}"
        f"- Valid predictions: {metrics['evaluated_records']} / {metrics['total_records']}\n"
        f"- Hostility YES precision/recall/F1: {identification['precision']:.2%} / "
        f"{identification['recall']:.2%} / {identification['f1']:.2%}\n"
        f"- Human-YES target support: {target['human_yes_support']}\n"
        f"- Exact target accuracy: {target['exact_target_accuracy']:.2%}\n"
        f"- Partisan-hostility precision/recall/F1: {partisan['precision']:.2%} / "
        f"{partisan['recall']:.2%} / {partisan['f1']:.2%}\n"
        f"- Classification-valid predictions: {evidence['classification_valid']}\n"
        f"- Evidence valid / unmatched / ambiguous: {evidence['valid']} / "
        f"{evidence['unmatched']} / {evidence['ambiguous']}\n"
        f"- Disagreements: {len(rows)}\n",
        encoding="utf-8",
    )
