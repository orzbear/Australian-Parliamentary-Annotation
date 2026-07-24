from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hansard_annotator.corpus.business_types import load_business_type_config
from hansard_annotator.corpus.config import load_reconstruction_config
from hansard_annotator.corpus.discovery import (
    DiscoveryError,
    discover_source_files,
    inventory_sha256,
)
from hansard_annotator.corpus.parser import FileParseError, parse_file
from hansard_annotator.corpus.pipeline import PipelineOptions, run_pipeline
from hansard_annotator.corpus.reconciliation import reconcile_file
from hansard_annotator.corpus.reconstruction import reconstruct_file

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "phase1_corpus"
MALFORMED = ROOT / "tests" / "fixtures" / "malformed_corpus"
DOCTYPE = ROOT / "tests" / "fixtures" / "doctype_corpus"
RECONSTRUCTION_CONFIG_PATH = ROOT / "config" / "reconstruction" / "v1.yaml"
BUSINESS_CONFIG_PATH = ROOT / "config" / "business_types" / "v1.yaml"


@pytest.fixture(scope="module")
def reconstruction_config():
    return load_reconstruction_config(RECONSTRUCTION_CONFIG_PATH)


@pytest.fixture(scope="module")
def business_config():
    return load_business_type_config(BUSINESS_CONFIG_PATH)


@pytest.fixture(scope="module")
def reconstructed_2010(reconstruction_config, business_config):
    source = discover_source_files(CORPUS, one_file="2010/2010-02-02.xml")[0]
    result = parse_file(
        source,
        config=reconstruction_config,
        business_config=business_config,
    )
    return reconstruct_file(result, config=reconstruction_config)


def fragment_by_id(result, source_id: str):
    return next(
        fragment
        for fragment in result.fragments
        if fragment.source_fragment_id == source_id
    )


def lineage_turn(result, fragment_key: str):
    relation = next(
        item for item in result.turn_fragments if item.fragment_key == fragment_key
    )
    return next(turn for turn in result.turns if turn.turn_key == relation.turn_key)


def test_discovery_scope_sample_and_changed_manifest(tmp_path: Path) -> None:
    all_files = discover_source_files(CORPUS)
    assert [source.relative_path for source in all_files] == [
        "2010/2010-02-02.xml",
        "2016/2016-08-31.xml",
        "2025/2025-02-04.xml",
    ]
    assert discover_source_files(CORPUS, year=2016)[0].relative_path.startswith("2016/")
    assert len(discover_source_files(CORPUS, sample_size=2)) == 2
    assert inventory_sha256(all_files) == inventory_sha256(
        discover_source_files(CORPUS)
    )
    prior = {source.relative_path: source.source_file_sha256 for source in all_files}
    assert discover_source_files(CORPUS, prior_hashes=prior) == []

    outside = tmp_path / "outside.xml"
    outside.write_text("<debates/>", encoding="utf-8")
    with pytest.raises(DiscoveryError, match="PATH_OUTSIDE_CORPUS_ROOT"):
        discover_source_files(CORPUS, one_file=str(outside))


def test_parser_preserves_structure_text_images_and_warnings(
    reconstruction_config,
    business_config,
) -> None:
    source = discover_source_files(CORPUS, one_file="2016/2016-08-31.xml")[0]
    result = parse_file(
        source,
        config=reconstruction_config,
        business_config=business_config,
    )
    assert len(result.fragments) == 5
    formatting = fragment_by_id(result, "p1/formatting")
    assert "italic" not in formatting.text_clean
    assert "Synthetic fiscal chart" in formatting.text_clean
    assert "[IMAGE]" in formatting.text_clean
    assert "First list item" in formatting.text_clean
    assert "Cell one\tCell two" in formatting.text_clean
    assert len(result.images) == 2
    assert result.images[0].alt_text == "Synthetic fiscal chart"
    assert result.images[1].clean_text_replacement == "[IMAGE]"
    assert "IMAGE_CONTENT_NOT_FETCHED" in formatting.warning_codes

    malformed = fragment_by_id(result, "p1/malformed-fields")
    assert "TIME_MALFORMED" in malformed.warning_codes
    assert "WORDCOUNT_MALFORMED" in malformed.warning_codes
    no_speaker = fragment_by_id(result, "p1/nospeaker")
    assert "SPEAKER_ID_MISSING" in no_speaker.warning_codes
    assert "SPEAKER_NAME_MISSING" in no_speaker.warning_codes
    assert "EMPTY_SPEECH_TEXT" in no_speaker.warning_codes
    duplicates = [
        fragment
        for fragment in result.fragments
        if fragment.source_fragment_id == "p1/duplicate"
    ]
    assert len(duplicates) == 2
    assert all(
        "DUPLICATE_SOURCE_FRAGMENT_ID" in fragment.warning_codes
        for fragment in duplicates
    )


def test_projection_checksum_is_deterministic(reconstruction_config, business_config) -> None:
    source = discover_source_files(CORPUS, one_file="2016/2016-08-31.xml")[0]
    first = parse_file(
        source,
        config=reconstruction_config,
        business_config=business_config,
    )
    second = parse_file(
        source,
        config=reconstruction_config,
        business_config=business_config,
    )
    assert [item.fragment_projection_sha256 for item in first.fragments] == [
        item.fragment_projection_sha256 for item in second.fragments
    ]
    assert [item.fragment_key for item in first.fragments] == [
        item.fragment_key for item in second.fragments
    ]


def test_reconstruction_merges_only_approved_sequences(reconstructed_2010) -> None:
    opening = fragment_by_id(reconstructed_2010, "p1/opening")
    opening_turn = lineage_turn(reconstructed_2010, opening.fragment_key)
    assert opening_turn.fragment_count == 3
    assert opening_turn.interruption_count == 3
    assert "The opening resumes after both interruptions." in opening_turn.text_clean
    assert "The opening resumes once more." in opening_turn.text_clean
    assert "First named interjection!" not in opening_turn.text_clean
    assert "Second named interjection!" not in opening_turn.text_clean
    assert "Opposition members interjecting" not in opening_turn.text_clean

    explicit = fragment_by_id(reconstructed_2010, "p1/collective/explicit")
    assert explicit.is_interjection is True
    linked = next(
        row
        for row in reconstructed_2010.interjections
        if row.source_fragment_key == explicit.fragment_key
    )
    assert linked.link_reason == "COLLECTIVE_INTERJECTION_EXPLICIT_PATTERN"
    assert linked.interrupted_turn_key == opening_turn.turn_key

    broad = fragment_by_id(reconstructed_2010, "p1/collective/response")
    assert broad.is_interjection is False
    after_broad = fragment_by_id(
        reconstructed_2010, "p1/continuation/after-response"
    )
    assert after_broad.is_orphan_continuation is True


def test_reconstruction_boundaries_and_identity_rules(reconstructed_2010) -> None:
    reasons = {
        item.source_fragment_id: item.reason_code
        for item in reconstructed_2010.continuation_anomalies
    }
    assert reasons["p1/continuation/after-heading"] == "MINOR_HEADING_BOUNDARY"
    assert reasons["p1/structural/continuation"] == "DIVISION_BOUNDARY"
    assert reasons["p1/same-name/conflict"] == "KNOWN_SPEAKER_ID_MISMATCH"

    fallback = fragment_by_id(
        reconstructed_2010, "p1/name-fallback/continuation"
    )
    assert fallback.is_continuation_merged is True
    relation = next(
        item
        for item in reconstructed_2010.turn_fragments
        if item.fragment_key == fallback.fragment_key
    )
    assert relation.merge_reason_code == "CONTINUATION_EXACT_NAME_FALLBACK"
    assert reconcile_file(reconstructed_2010) == []


def test_structural_events_and_counts(reconstructed_2010) -> None:
    assert reconstructed_2010.division_count == 1
    assert reconstructed_2010.bills_element_count == 2
    assert reconstructed_2010.bill_record_count == 2
    assert reconstructed_2010.unknown_top_level_count == 1
    assert {event.element_type for event in reconstructed_2010.events} == {
        "division",
        "stage-direction",
        "bills",
    }


@pytest.mark.parametrize(
    ("root", "error_code"),
    [
        (MALFORMED, "XMLSYNTAXERROR"),
        (DOCTYPE, "UNSAFE_DOCTYPE"),
    ],
)
def test_malformed_and_doctype_files_fail_atomically(
    root: Path,
    error_code: str,
    reconstruction_config,
    business_config,
) -> None:
    source = discover_source_files(root)[0]
    with pytest.raises(FileParseError) as caught:
        parse_file(
            source,
            config=reconstruction_config,
            business_config=business_config,
        )
    assert caught.value.code == error_code


def test_configurable_safety_ceiling(reconstruction_config, business_config) -> None:
    source = discover_source_files(CORPUS, one_file="2010/2010-02-02.xml")[0]
    restrictive = replace(
        reconstruction_config,
        safety_limits=replace(reconstruction_config.safety_limits, max_elements=2),
    )
    with pytest.raises(FileParseError, match="element-count"):
        parse_file(source, config=restrictive, business_config=business_config)

    restrictive_text = replace(
        reconstruction_config,
        safety_limits=replace(
            reconstruction_config.safety_limits,
            max_individual_text_chars=5,
        ),
    )
    with pytest.raises(FileParseError) as caught:
        parse_file(source, config=restrictive_text, business_config=business_config)
    assert caught.value.code == "TEXT_SIZE_LIMIT_EXCEEDED"


def test_pipeline_writes_explicit_parquet_reports_and_is_idempotent(
    tmp_path: Path,
) -> None:
    options = PipelineOptions(
        corpus_root=CORPUS,
        output_root=tmp_path,
        reconstruction_config=RECONSTRUCTION_CONFIG_PATH,
        business_config=BUSINESS_CONFIG_PATH,
        run_id="synthetic-idempotency",
        batch_size=3,
    )
    first = run_pipeline(options)
    assert first.manifest["acceptance_gate_passed"] is True
    assert first.manifest["overall_status"] == "completed"
    second = run_pipeline(options)
    assert second.reused_existing_run is True
    assert (
        second.manifest["input_inventory_sha256"]
        == first.manifest["input_inventory_sha256"]
    )

    fragment_file = next(
        (first.run_directory / "speech_fragments").rglob("*.parquet")
    )
    table = pq.read_table(fragment_file)
    assert "fragment_projection_sha256" in table.schema.names
    assert table.schema.field("warning_codes").type.value_type == pa.string()
    required_reports = {
        "file_processing_report.csv",
        "corpus_summary.csv",
        "unknown_headings.csv",
        "malformed_files.csv",
        "missing_speakers.csv",
        "continuation_anomalies.csv",
        "duplicate_ids.csv",
        "image_elements.csv",
        "reconciliation_failures.csv",
        "source_url_exceptions.csv",
    }
    assert required_reports <= {
        path.name for path in (first.run_directory / "reports").iterdir()
    }


def test_pipeline_quality_gate_for_malformed_file(tmp_path: Path) -> None:
    outcome = run_pipeline(
        PipelineOptions(
            corpus_root=MALFORMED,
            output_root=tmp_path,
            reconstruction_config=RECONSTRUCTION_CONFIG_PATH,
            business_config=BUSINESS_CONFIG_PATH,
            run_id="malformed-quality-gate",
        )
    )
    assert outcome.manifest["overall_status"] == "completed_with_errors"
    assert outcome.manifest["acceptance_gate_passed"] is False
    assert outcome.manifest["file_counts"]["failed"] == 1
    report = (
        outcome.run_directory / "reports" / "malformed_files.csv"
    ).read_text(encoding="utf-8")
    assert "XMLSYNTAXERROR" in report
    assert not (outcome.run_directory / "speech_fragments").exists()


def test_pipeline_changed_file_mode_allows_clean_noop(tmp_path: Path) -> None:
    first = run_pipeline(
        PipelineOptions(
            corpus_root=CORPUS,
            output_root=tmp_path,
            reconstruction_config=RECONSTRUCTION_CONFIG_PATH,
            business_config=BUSINESS_CONFIG_PATH,
            run_id="prior-source",
        )
    )
    changed = run_pipeline(
        PipelineOptions(
            corpus_root=CORPUS,
            output_root=tmp_path,
            reconstruction_config=RECONSTRUCTION_CONFIG_PATH,
            business_config=BUSINESS_CONFIG_PATH,
            prior_manifest=first.run_directory / "run_manifest.json",
            run_id="no-changes",
        )
    )
    assert changed.manifest["acceptance_gate_passed"] is True
    assert changed.manifest["file_counts"]["discovered"] == 0
    assert changed.manifest["record_counts"] == {}


def test_manifest_is_valid_json_and_has_versions(tmp_path: Path) -> None:
    outcome = run_pipeline(
        PipelineOptions(
            corpus_root=CORPUS,
            output_root=tmp_path,
            reconstruction_config=RECONSTRUCTION_CONFIG_PATH,
            business_config=BUSINESS_CONFIG_PATH,
            sample_size=1,
            run_id="manifest-test",
        )
    )
    manifest = json.loads(
        (outcome.run_directory / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["pipeline_version"] == "1.0.0"
    assert manifest["configuration_versions"] == {
        "business_types": "1.0.0",
        "reconstruction": "1.0.0",
    }
    assert manifest["output_files"]
