"""Transparent dependency-free classification metrics."""

from __future__ import annotations

from typing import Any


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return numerator / denominator if denominator else 0.0


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    return {
        "precision": precision,
        "recall": recall,
        "f1": _ratio(2 * precision * recall, precision + recall),
    }


def compute_metrics(
    records: tuple[dict[str, Any], ...],
    results: list[dict[str, Any]],
    domain_codes: tuple[str, ...],
) -> dict[str, Any]:
    by_id = {str(result["record_id"]): result for result in results}
    total = len(records)
    valid_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    statuses = {"success": 0, "invalid": 0, "api_failure": 0, "missing": 0}
    for record in records:
        result = by_id.get(str(record["record_id"]))
        if result is None:
            statuses["missing"] += 1
        elif result.get("status") != "success" or not isinstance(result.get("prediction"), dict):
            statuses[str(result.get("status"))] += 1
        else:
            statuses["success"] += 1
            valid_pairs.append((record["human_annotation"]["values"], result["prediction"]))

    tp = sum(not h["is_non_policy"] and not p["is_non_policy"] for h, p in valid_pairs)
    tn = sum(h["is_non_policy"] and p["is_non_policy"] for h, p in valid_pairs)
    fp = sum(h["is_non_policy"] and not p["is_non_policy"] for h, p in valid_pairs)
    fn = sum(not h["is_non_policy"] and p["is_non_policy"] for h, p in valid_pairs)
    policy = {
        "positive_class": "policy",
        "accuracy": _ratio(tp + tn, len(valid_pairs)),
        **_prf(tp, fp, fn),
        "confusion": {
            "true_policy": tp,
            "true_non_policy": tn,
            "false_policy": fp,
            "false_non_policy": fn,
        },
    }

    columns = (*domain_codes, "__NON_POLICY__")
    confusion = {human: {predicted: 0 for predicted in columns} for human in domain_codes}
    policy_domain_pairs = [(h, p) for h, p in valid_pairs if not h["is_non_policy"]]
    exact = 0
    for human, prediction in policy_domain_pairs:
        human_domain = str(human["primary_australian_domain"])
        predicted_domain = (
            "__NON_POLICY__"
            if prediction["is_non_policy"]
            else str(prediction["primary_australian_domain"])
        )
        confusion[human_domain][predicted_domain] += 1
        exact += human_domain == predicted_domain
    per_domain: dict[str, dict[str, float | int]] = {}
    for code in domain_codes:
        domain_tp = confusion[code][code]
        domain_fn = sum(confusion[code].values()) - domain_tp
        domain_fp = sum(
            (int(confusion[other][code]) for other in domain_codes if other != code),
            start=0,
        )
        per_domain[code] = {
            "support": sum(confusion[code].values()),
            **_prf(domain_tp, domain_fp, domain_fn),
        }
    macro_f1 = _ratio(sum(float(value["f1"]) for value in per_domain.values()), len(domain_codes))
    return {
        "total_records": total,
        "evaluated_records": len(valid_pairs),
        "status_counts": statuses,
        "invalid_rate": _ratio(statuses["invalid"], total),
        "api_failure_rate": _ratio(statuses["api_failure"], total),
        "missing_rate": _ratio(statuses["missing"], total),
        "policy": policy,
        "primary_domain": {
            "exact_match_accuracy": _ratio(exact, len(policy_domain_pairs)),
            "evaluated_policy_records": len(policy_domain_pairs),
            "macro_f1": macro_f1,
            "per_domain": per_domain,
            "confusion_matrix": confusion,
        },
    }
