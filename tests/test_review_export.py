from __future__ import annotations

import csv
from pathlib import Path

import pytest

from hansard_annotator.db.review_export import (
    EXPORT_COLUMNS,
    validate_output_path,
    write_review_csv,
)


def test_csv_writer_preserves_punctuation_multiline_and_column_alignment(
    tmp_path: Path,
) -> None:
    text = (
        "\u201cQuoted\u201d, it\u2019s safe \u2014 and en\u2013dashed\u2026"
        '\n\nSecond "paragraph", intact.'
    )
    row: dict[str, object] = dict.fromkeys(EXPORT_COLUMNS)
    row.update(
        {
            "turn_key": "a" * 64,
            "speech_date": "2026-07-24",
            "calculated_word_count": 12,
            "interrupted": False,
            "interruption_count": 0,
            "is_orphan_continuation": False,
            "procedural_hint": None,
            "ceremonial_hint": True,
            "source_file": "2026/2026-07-24.xml",
            "source_url": "https://example.invalid/turn",
            "run_name": "fixture",
            "pipeline_version": "1.0.0",
            "corpus_slug": "fixture-corpus",
            "text_clean": text,
        }
    )
    target = tmp_path / "review.csv"
    write_review_csv(target, [row], excel_compatible=True)

    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    with target.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        parsed = list(reader)
        assert tuple(reader.fieldnames or ()) == EXPORT_COLUMNS
    assert len(parsed) == 1
    assert len(parsed[0]) == len(EXPORT_COLUMNS)
    assert parsed[0]["text_clean"] == text
    assert parsed[0]["source_file"] == "2026/2026-07-24.xml"
    assert parsed[0]["procedural_hint"] == "unknown"
    assert parsed[0]["ceremonial_hint"] == "true"


@pytest.mark.parametrize(
    "relative",
    (
        Path("hansard_xml_files/review.csv"),
        Path("data/processed/accepted/review.csv"),
        Path("backups/review.csv"),
    ),
)
def test_review_path_rejects_protected_trees(tmp_path: Path, relative: Path) -> None:
    with pytest.raises(ValueError, match="forbidden directory"):
        validate_output_path(tmp_path / relative, repository_root=tmp_path)
