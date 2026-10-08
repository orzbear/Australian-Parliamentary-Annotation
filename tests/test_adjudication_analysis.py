from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hansard_annotator.llm_eval.adjudication import analyse, write_analysis


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _prediction(record_id: str, non_policy: bool, primary: str | None) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "status": "success",
        "prediction": {
            "is_non_policy": non_policy,
            "primary_australian_domain": primary,
            "secondary_australian_domains": [],
            "reasoning": "fixture",
        },
    }


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    predictions = [
        _prediction("one", True, None),
        _prediction("two", False, "AU02"),
        _prediction("three", False, "AU03"),
        _prediction("four", True, None),
    ]
    (run_dir / "predictions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in predictions), encoding="utf-8"
    )
    metrics = {
        "total_records": 4,
        "evaluated_records": 4,
        "policy": {"accuracy": 0.5, "precision": 0.5, "recall": 0.5, "f1": 0.5},
        "primary_domain": {
            "exact_match_accuracy": 0.5,
            "macro_f1": 0.4,
            "per_domain": {
                code: {"support": 0, "precision": 0, "recall": 0, "f1": 0}
                for code in ("AU01", "AU02", "AU03")
            },
        },
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_dir / "report.md").write_text("original immutable report\n", encoding="utf-8")
    mismatch_rows = [
        {
            "record_id": "one",
            "human_is_non_policy": "False",
            "model_is_non_policy": "True",
            "human_primary_domain": "AU01",
            "model_primary_domain": "",
        },
        {
            "record_id": "two",
            "human_is_non_policy": "False",
            "model_is_non_policy": "False",
            "human_primary_domain": "AU01",
            "model_primary_domain": "AU02",
        },
    ]
    _write_csv(run_dir / "mismatches.csv", mismatch_rows)
    adjudication_rows = [
        {
            "record_id": "one",
            "human_policy_status": "policy",
            "human_primary_domain": "AU01",
            "model_policy_status": "non-policy",
            "model_primary_domain": "",
            "adjudication_outcome": "human_needs_revision",
            "adjudicated_policy_status": "non-policy",
            "adjudicated_primary_domain": "",
            "issue_type": "policy_gate",
            "rationale": "Researcher decision.",
            "human_label_revision_recommended": "yes",
            "taxonomy_note": "",
        },
        {
            "record_id": "two",
            "human_policy_status": "policy",
            "human_primary_domain": "AU01",
            "model_policy_status": "policy",
            "model_primary_domain": "AU02",
            "adjudication_outcome": "codebook_ambiguous",
            "adjudicated_policy_status": "policy",
            "adjudicated_primary_domain": "",
            "issue_type": "domain_boundary",
            "rationale": "Researcher found both defensible.",
            "human_label_revision_recommended": "no",
            "taxonomy_note": "Review later.",
        },
    ]
    adjudication = tmp_path / "adjudication.csv"
    _write_csv(adjudication, adjudication_rows)
    return run_dir, adjudication


def test_complete_adjudication_analysis_and_original_report_preservation(tmp_path: Path) -> None:
    run_dir, adjudication = _fixture(tmp_path)
    original_hash = hashlib.sha256((run_dir / "report.md").read_bytes()).hexdigest()
    summary = analyse(run_dir, adjudication)
    assert summary["adjudication_outcomes"]["total_adjudicated_mismatches"] == 2
    assert summary["adjudication_outcomes"]["human_needs_revision"] == 1
    assert summary["adjudication_outcomes"]["codebook_ambiguous"] == 1
    assert summary["adjudicated_policy_gate"]["accuracy"] == 1
    assert summary["adjudicated_policy_gate"]["policy_labels_changed_by_adjudication"] == 1
    assert summary["adjudicated_primary_domain"]["resolvable_policy_domain_cases"] == 1
    assert summary["adjudicated_primary_domain"]["excluded_unresolved_taxonomy_ambiguity"] == 1
    assert summary["adjudicated_primary_domain"]["exact_match_accuracy"] == 1
    write_analysis(run_dir, summary)
    assert (run_dir / "adjudicated_report.md").is_file()
    assert (run_dir / "adjudicated_summary.json").is_file()
    assert "| AU03 | 1 | 100.00% |" in (run_dir / "adjudicated_report.md").read_text(
        encoding="utf-8"
    )
    assert hashlib.sha256((run_dir / "report.md").read_bytes()).hexdigest() == original_hash


def test_missing_adjudication_row_fails(tmp_path: Path) -> None:
    run_dir, adjudication = _fixture(tmp_path)
    rows = list(csv.DictReader(adjudication.open(encoding="utf-8", newline="")))
    _write_csv(adjudication, [dict(rows[0])])
    with pytest.raises(ValueError, match="missing adjudication rows"):
        analyse(run_dir, adjudication)


def test_duplicate_adjudication_row_fails(tmp_path: Path) -> None:
    run_dir, adjudication = _fixture(tmp_path)
    rows = [dict(row) for row in csv.DictReader(adjudication.open(encoding="utf-8", newline=""))]
    _write_csv(adjudication, [*rows, rows[0]])
    with pytest.raises(ValueError, match="duplicate record_id"):
        analyse(run_dir, adjudication)
