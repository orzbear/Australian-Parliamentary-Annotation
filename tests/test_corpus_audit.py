from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.corpus_audit import (
    audit_corpus,
    discover_xml_files,
    sha256_file,
    validate_archive_path,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "corpus"
MALFORMED_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "malformed_corpus"
RAW_ROOT = REPOSITORY_ROOT / "hansard_xml_files"


def snapshot(paths: list[Path]) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in paths:
        stat = path.stat()
        result[path.as_posix()] = (stat.st_size, stat.st_mtime_ns, sha256_file(path))
    return result


class DiscoveryAndValidationTests(unittest.TestCase):
    def test_discovery_is_deterministic(self) -> None:
        first = [
            path.relative_to(FIXTURE_ROOT).as_posix()
            for path in discover_xml_files(FIXTURE_ROOT)
        ]
        second = [
            path.relative_to(FIXTURE_ROOT).as_posix()
            for path in discover_xml_files(FIXTURE_ROOT)
        ]
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            [
                "2010/2010-02-02.xml",
                "2016/2016-08-31.xml",
                "2025/2025-02-04.xml",
            ],
        )

    def test_filename_folder_date_validation(self) -> None:
        valid = validate_archive_path(
            FIXTURE_ROOT / "2010" / "2010-02-02.xml", FIXTURE_ROOT
        )
        self.assertTrue(valid.valid)
        self.assertEqual(valid.date_value, "2010-02-02")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mismatch = root / "2011" / "2010-02-02.xml"
            mismatch.parent.mkdir()
            mismatch.touch()
            result = validate_archive_path(mismatch, root)
            self.assertFalse(result.valid)
            self.assertIn("folder_filename_year_mismatch", result.errors)

            bad_date = root / "2010" / "2010-02-30.xml"
            bad_date.parent.mkdir()
            bad_date.touch()
            result = validate_archive_path(bad_date, root)
            self.assertFalse(result.valid)
            self.assertIn("invalid_calendar_date", result.errors)

            malformed = root / "2010" / "not-a-date.xml"
            malformed.touch()
            result = validate_archive_path(malformed, root)
            self.assertFalse(result.valid)
            self.assertIn("malformed_filename", result.errors)

    def test_checksums_are_reproducible(self) -> None:
        path = FIXTURE_ROOT / "2016" / "2016-08-31.xml"
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(sha256_file(path), expected)
        self.assertEqual(sha256_file(path), sha256_file(path))


class AuditSafetyAndCoverageTests(unittest.TestCase):
    def test_audit_does_not_modify_source_files(self) -> None:
        files = discover_xml_files(FIXTURE_ROOT)
        before = snapshot(files)
        first = audit_corpus(FIXTURE_ROOT)
        second = audit_corpus(FIXTURE_ROOT)
        after = snapshot(files)

        self.assertEqual(before, after)
        self.assertEqual(first["corpus_inventory_sha256"], second["corpus_inventory_sha256"])
        self.assertEqual(first["source_mutation_detected"], [])
        self.assertEqual(second["source_mutation_detected"], [])

    def test_malformed_xml_is_reported_without_raising(self) -> None:
        result = audit_corpus(MALFORMED_ROOT)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(len(result["malformed_xml"]), 1)
        self.assertEqual(
            result["malformed_xml"][0]["path"], "2010/2010-02-03.xml"
        )
        self.assertEqual(result["source_mutation_detected"], [])

    def test_synthetic_fixtures_cover_approved_edge_categories(self) -> None:
        result = audit_corpus(FIXTURE_ROOT)
        counts = result["exact_counts_successful_files"]

        self.assertEqual(result["file_count"], 3)
        self.assertGreaterEqual(counts["talktypes"].get("speech", 0), 1)
        self.assertGreaterEqual(counts["talktypes"].get("continuation", 0), 3)
        self.assertGreaterEqual(counts["talktypes"].get("interjection", 0), 2)
        self.assertEqual(counts["tags"].get("division"), 1)
        self.assertEqual(counts["tags"].get("bills"), 1)
        self.assertGreaterEqual(
            counts["missing_speech_attributes"].get("speakerid", 0), 1
        )

        early_text = (FIXTURE_ROOT / "2010" / "2010-02-02.xml").read_text(
            encoding="utf-8"
        )
        required_markers = (
            'speakername="Opposition Members"',
            "members interjecting",
            'id="synthetic/2010/orphan/1"',
            "Changed Heading",
            'speakername="Unknown Member"',
            "<division ",
            "<bills>",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, early_text)

    def test_fixtures_are_small_and_explicitly_synthetic(self) -> None:
        for path in discover_xml_files(FIXTURE_ROOT) + discover_xml_files(MALFORMED_ROOT):
            with self.subTest(path=path):
                self.assertLess(path.stat().st_size, 20_000)
                self.assertIn(b"Synthetic", path.read_bytes()[:500])

    def test_raw_xml_directory_is_ignored_by_git(self) -> None:
        raw_example = RAW_ROOT / "2010" / "2010-02-02.xml"
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", str(raw_example)],
            cwd=REPOSITORY_ROOT,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "XDG_CONFIG_HOME": str(REPOSITORY_ROOT / "tests" / "fixtures"),
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
