from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hansard_annotator.llm_eval.comparison import compare_many, compare_runs, write_comparison
from hansard_annotator.llm_eval.pairwise import analyse_pairwise, write_pairwise


def _write_run(
    path: Path,
    *,
    provider: str,
    model: str,
    ids: list[str],
    predictions: list[dict[str, Any]] | None = None,
) -> None:
    path.mkdir()
    configuration: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "package_snapshot_sha256": "package",
        "schema_version": "0.2.0",
        "schema_sha256": "schema",
        "prompt_version": "phase4a-policy-v2",
        "prompt_sha256": "prompt",
        "exclusion_record_count": 30,
        "exclusion_set_sha256": "exclusions",
        "generation": {},
    }
    (path / "run_metadata.json").write_text(
        json.dumps({"configuration": configuration, "run_identity": f"{provider}-identity"}),
        encoding="utf-8",
    )
    (path / "predictions.jsonl").write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                predictions
                or [
                    {
                        "record_id": record_id,
                        "status": "success",
                        "prediction": {
                            "is_non_policy": False,
                            "primary_australian_domain": "AU01",
                            "secondary_australian_domains": [],
                            "reasoning": "fixture",
                        },
                    }
                    for record_id in ids
                ]
            )
        ),
        encoding="utf-8",
    )
    metrics = {
        "total_records": len(ids),
        "evaluated_records": len(ids),
        "api_failure_rate": 0.0,
        "invalid_rate": 0.0,
        "policy": {"accuracy": 0.9, "precision": 0.8, "recall": 0.7, "f1": 0.75},
        "primary_domain": {
            "exact_match_accuracy": 0.6,
            "macro_f1": 0.5,
            "per_domain": {"AU01": {"support": len(ids), "recall": 0.6}},
        },
    }
    (path / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    costs = {
        "estimated_cost_usd": 0.12,
        "cost_per_valid_annotation_usd": 0.001,
        "projected_cost_160k_attempts_usd": 160.0,
    }
    (path / "cost_report.json").write_text(json.dumps(costs), encoding="utf-8")
    (path / "mismatches.jsonl").write_text('{"record_id":"one"}\n', encoding="utf-8")


def test_comparison_requires_and_reports_same_frozen_120_records(tmp_path: Path) -> None:
    ids = [f"record-{number}" for number in range(120)]
    gemini = tmp_path / "gemini"
    luna = tmp_path / "luna"
    _write_run(gemini, provider="gemini", model="gemini-2.5-flash", ids=ids)
    _write_run(luna, provider="openai", model="gpt-6-luna", ids=ids)
    comparison = compare_runs(gemini, luna)
    assert comparison["record_count"] == 120
    assert comparison["models"][1]["valid_output_rate"] == 1.0
    assert comparison["models"][1]["human_model_disagreement_count"] == 1
    assert "adjudications are not used" in comparison["methodological_note"]
    output = tmp_path / "comparison"
    write_comparison(output, comparison)
    assert "does not select a winner" in (output / "comparison.md").read_text(encoding="utf-8")


def test_comparison_rejects_different_record_ids(tmp_path: Path) -> None:
    ids = [f"record-{number}" for number in range(120)]
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_run(first, provider="gemini", model="gemini-2.5-flash", ids=ids)
    _write_run(second, provider="openai", model="gpt-6-luna", ids=[*ids[:-1], "different"])
    with pytest.raises(ValueError, match="exact same record IDs"):
        compare_runs(first, second)


def test_comparison_rejects_incompatible_prompt(tmp_path: Path) -> None:
    ids = [f"record-{number}" for number in range(120)]
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_run(first, provider="gemini", model="gemini-2.5-flash", ids=ids)
    _write_run(second, provider="openai", model="gpt-6-luna", ids=ids)
    metadata = json.loads((second / "run_metadata.json").read_text(encoding="utf-8"))
    metadata["configuration"]["prompt_sha256"] = "changed"
    (second / "run_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="prompt_sha256"):
        compare_runs(first, second)


def test_five_model_comparison_and_frontier_preserve_two_model_support(tmp_path: Path) -> None:
    ids = [f"record-{number}" for number in range(120)]
    specifications = (
        ("gemini", "gemini-2.5-flash"),
        ("gemini", "gemini-2.5-flash-lite"),
        ("gemini", "gemini-3.5-flash-lite"),
        ("openai", "gpt-6-luna"),
        ("openai", "gpt-5.4-mini"),
    )
    runs = []
    for number, (provider, model) in enumerate(specifications):
        run = tmp_path / f"run-{number}"
        _write_run(run, provider=provider, model=model, ids=ids)
        runs.append(run)
    comparison = compare_many(runs)
    assert len(comparison["models"]) == 5
    assert set(comparison["quality_cost_frontier_dominated_by"]) == {
        "primary_domain_exact_match",
        "primary_domain_macro_f1",
        "policy_f1",
    }
    assert len(comparison["relative_total_cost_ratios"]) == 5
    assert len(compare_runs(runs[0], runs[1])["models"]) == 2


def test_pairwise_categories_domain_deltas_and_report(tmp_path: Path) -> None:
    ids = [f"record-{number}" for number in range(120)]

    def rows(domains: dict[int, str]) -> list[dict[str, Any]]:
        return [
            {
                "record_id": record_id,
                "status": "success",
                "prediction": {
                    "is_non_policy": False,
                    "primary_australian_domain": domains.get(number, "AU01"),
                    "secondary_australian_domains": [],
                    "reasoning": "fixture",
                },
            }
            for number, record_id in enumerate(ids)
        ]

    gemini = tmp_path / "gemini"
    luna = tmp_path / "luna"
    _write_run(
        gemini,
        provider="gemini",
        model="gemini-2.5-flash",
        ids=ids,
        predictions=rows({2: "AU02", 3: "AU02", 4: "AU02"}),
    )
    _write_run(
        luna,
        provider="openai",
        model="gpt-6-luna",
        ids=ids,
        predictions=rows({1: "AU02", 3: "AU02", 4: "AU03"}),
    )
    records = tuple(
        {
            "record_id": record_id,
            "human_annotation": {
                "values": {"is_non_policy": False, "primary_australian_domain": "AU01"}
            },
        }
        for record_id in ids
    )
    analysis = analyse_pairwise(records, gemini, luna)
    categories = analysis["primary_domain_on_human_policy_records"]["categories"]
    assert categories == {
        "both_correct": 116,
        "gemini_only_correct": 1,
        "luna_only_correct": 1,
        "both_wrong_same_answer": 1,
        "both_wrong_different_answer": 1,
    }
    assert analysis["primary_domain_on_human_policy_records"]["model_vs_model_disagreements"] == 3
    delta = analysis["primary_domain_on_human_policy_records"]["per_human_domain"]["AU01"]
    assert delta["errors_by_both"] == 2
    output = tmp_path / "output"
    write_pairwise(output, analysis)
    report = (output / "pairwise_analysis.md").read_text(encoding="utf-8")
    assert "post-hoc Gemini adjudication is not used" in report
    assert "No automatic winner is selected" in report
