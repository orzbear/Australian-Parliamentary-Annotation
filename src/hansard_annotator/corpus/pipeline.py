"""Phase 1 orchestration: parse, reconstruct, reconcile, and write outputs."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from hansard_annotator import __version__
from hansard_annotator.corpus.business_types import load_business_type_config
from hansard_annotator.corpus.config import load_reconstruction_config
from hansard_annotator.corpus.discovery import (
    discover_source_files,
    inventory_sha256,
    load_prior_hashes,
    sha256_file,
)
from hansard_annotator.corpus.hashing import canonical_json
from hansard_annotator.corpus.models import (
    FileFailure,
    FileProcessingRecord,
)
from hansard_annotator.corpus.parquet import PartitionedParquetWriter
from hansard_annotator.corpus.parser import PIPELINE_VERSION, FileParseError, parse_file
from hansard_annotator.corpus.reconciliation import (
    ReconciliationFailure,
    reconcile_file,
)
from hansard_annotator.corpus.reconstruction import reconstruct_file
from hansard_annotator.corpus.reports import ReportCollector

PHASE_0_BASELINE = {
    "file_count": 926,
    "total_bytes": 784_583_349,
    "inventory_sha256": "7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2",
    "speech": 269_793,
    "talktype_speech": 156_123,
    "talktype_continuation": 38_925,
    "talktype_interjection": 74_745,
    "major-heading": 19_973,
    "minor-heading": 78_002,
    "division": 2_848,
    "bill": 16_759,
}


@dataclass(frozen=True)
class PipelineOptions:
    corpus_root: Path
    output_root: Path
    reconstruction_config: Path
    business_config: Path
    year: int | None = None
    one_file: str | None = None
    sample_size: int | None = None
    prior_manifest: Path | None = None
    run_id: str | None = None
    batch_size: int = 5_000

    @property
    def is_full_run(self) -> bool:
        return (
            self.year is None
            and self.one_file is None
            and self.sample_size is None
            and self.prior_manifest is None
        )

    @property
    def expects_phase_0_baseline(self) -> bool:
        return self.is_full_run and self.corpus_root.resolve().name == "hansard_xml_files"


@dataclass(frozen=True)
class PipelineOutcome:
    run_directory: Path
    manifest: dict[str, Any]
    reused_existing_run: bool


def _git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _dependencies() -> dict[str, str]:
    names = ("lxml", "pyarrow", "PyYAML", "psutil")
    return {
        name: importlib.metadata.version(name)
        for name in names
    }


def _output_checksums(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        if path.name == "run_manifest.json" or path.suffix == ".tmp":
            continue
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2, sort_keys=True)
        destination.write("\n")
    temporary.replace(path)


def _default_run_id(
    *,
    inventory_hash: str,
    reconstruction_hash: str,
    business_hash: str,
) -> str:
    key = hashlib.sha256(
        canonical_json(
            {
                "business": business_hash,
                "inventory": inventory_hash,
                "pipeline": PIPELINE_VERSION,
                "reconstruction": reconstruction_hash,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"phase1-{key[:16]}"


def _baseline_comparison(
    *,
    file_count: int,
    total_bytes: int,
    inventory_hash: str,
    observed_tags: Counter[str],
    observed_talktypes: Counter[str],
) -> dict[str, dict[str, Any]]:
    actuals: dict[str, Any] = {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "inventory_sha256": inventory_hash,
        "speech": observed_tags["speech"],
        "talktype_speech": observed_talktypes["speech"],
        "talktype_continuation": observed_talktypes["continuation"],
        "talktype_interjection": observed_talktypes["interjection"],
        "major-heading": observed_tags["major-heading"],
        "minor-heading": observed_tags["minor-heading"],
        "division": observed_tags["division"],
        "bill": observed_tags["bill"],
    }
    return {
        key: {
            "expected": expected,
            "actual": actuals[key],
            "match": expected == actuals[key],
        }
        for key, expected in PHASE_0_BASELINE.items()
    }


def _existing_run(
    run_directory: Path,
    *,
    inventory_hash: str,
    reconstruction_hash: str,
    business_hash: str,
) -> PipelineOutcome | None:
    manifest_path = run_directory / "run_manifest.json"
    if not manifest_path.is_file():
        return None
    with manifest_path.open("r", encoding="utf-8") as source:
        manifest: dict[str, Any] = json.load(source)
    expected_config = {
        "business_types": business_hash,
        "reconstruction": reconstruction_hash,
    }
    if (
        manifest.get("input_inventory_sha256") == inventory_hash
        and manifest.get("pipeline_version") == PIPELINE_VERSION
        and manifest.get("configuration_hashes") == expected_config
        and manifest.get("overall_status") == "completed"
        and manifest.get("acceptance_gate_passed") is True
    ):
        return PipelineOutcome(run_directory, manifest, True)
    raise RuntimeError(
        f"run directory already exists but is not a reusable successful run: {run_directory}"
    )


def run_pipeline(options: PipelineOptions) -> PipelineOutcome:
    started_wall = datetime.now(UTC)
    started_perf = time.perf_counter()
    process = psutil.Process()
    peak_rss = process.memory_info().rss

    reconstruction_config = load_reconstruction_config(
        options.reconstruction_config.resolve(strict=True)
    )
    business_config = load_business_type_config(
        options.business_config.resolve(strict=True)
    )
    prior_hashes = (
        load_prior_hashes(options.prior_manifest.resolve(strict=True))
        if options.prior_manifest
        else None
    )
    sources = discover_source_files(
        options.corpus_root,
        year=options.year,
        one_file=options.one_file,
        sample_size=options.sample_size,
        prior_hashes=prior_hashes,
    )
    if not sources and options.prior_manifest is None:
        raise RuntimeError("no source files selected")

    input_inventory_hash = inventory_sha256(sources)
    total_bytes = sum(source.byte_size for source in sources)
    run_id = options.run_id or _default_run_id(
        inventory_hash=input_inventory_hash,
        reconstruction_hash=reconstruction_config.sha256,
        business_hash=business_config.sha256,
    )
    final_directory = options.output_root.resolve() / run_id
    existing = _existing_run(
        final_directory,
        inventory_hash=input_inventory_hash,
        reconstruction_hash=reconstruction_config.sha256,
        business_hash=business_config.sha256,
    ) if final_directory.exists() else None
    if existing is not None:
        return existing

    working_directory = options.output_root.resolve() / f".{run_id}.in_progress"
    if working_directory.exists():
        raise RuntimeError(
            f"in-progress directory already exists; inspect it before retrying: {working_directory}"
        )
    working_directory.mkdir(parents=True)

    writer = PartitionedParquetWriter(
        working_directory,
        batch_size=options.batch_size,
    )
    reports = ReportCollector()
    for source in sources:
        writer.append(
            "source_files",
            source.sitting_date.year,
            [source.to_row(chamber=reconstruction_config.chamber)],
        )

    active_year: int | None = None
    for source in sources:
        if active_year is not None and source.sitting_date.year != active_year:
            writer.flush_year(active_year)
        active_year = source.sitting_date.year
        before = source.absolute_path.stat()
        failure: FileFailure | None = None
        result = None
        reconciliation_failures: list[ReconciliationFailure] = []
        status = "completed"
        try:
            result = parse_file(
                source,
                config=reconstruction_config,
                business_config=business_config,
            )
            reconstruct_file(result, config=reconstruction_config)
            reconciliation_failures = reconcile_file(result)
            after_hash = sha256_file(source.absolute_path)
            after = source.absolute_path.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or source.source_file_sha256 != after_hash
            ):
                reconciliation_failures.append(
                    ReconciliationFailure(
                        source.relative_path,
                        "SOURCE_CHANGED_DURING_PROCESSING",
                        "size, modification time, or SHA-256 changed",
                    )
                )
            if reconciliation_failures:
                status = "unreconciled"
                reports.reconciliation_failures.extend(reconciliation_failures)
            else:
                year = source.sitting_date.year
                writer.append(
                    "debate_sections",
                    year,
                    [record.to_row() for record in result.sections],
                )
                writer.append(
                    "debate_events",
                    year,
                    [record.to_row() for record in result.events],
                )
                writer.append(
                    "speech_fragments",
                    year,
                    [record.to_row() for record in result.fragments],
                )
                writer.append(
                    "speaker_turns",
                    year,
                    [record.to_row() for record in result.turns],
                )
                writer.append(
                    "speaker_turn_fragments",
                    year,
                    [record.to_row() for record in result.turn_fragments],
                )
                writer.append(
                    "interjections",
                    year,
                    [record.to_row() for record in result.interjections],
                )
                writer.append(
                    "continuation_anomalies",
                    year,
                    [record.to_row() for record in result.continuation_anomalies],
                )
            reports.observe_result(result, included_in_output=not reconciliation_failures)
        except FileParseError as error:
            after = source.absolute_path.stat()
            status = "failed"
            failure = FileFailure(
                source_file=source.relative_path,
                source_file_sha256=source.source_file_sha256,
                error_code=error.code,
                error_message=error.safe_message,
            )
            reports.file_failures.append(failure)

        warning_count = (
            sum(len(fragment.warning_codes) for fragment in result.fragments)
            + sum(len(section.warning_codes) for section in result.sections)
            + sum(len(event.warning_codes) for event in result.events)
            if result is not None
            else 0
        )
        reports.processing_records.append(
            FileProcessingRecord(
                source_file=source.relative_path,
                source_file_sha256=source.source_file_sha256,
                byte_size=source.byte_size,
                modification_time_ns_before=before.st_mtime_ns,
                modification_time_ns_after=after.st_mtime_ns,
                status=status,
                error_code=failure.error_code if failure else None,
                error_message=failure.error_message if failure else None,
                fragment_count=len(result.fragments) if result else 0,
                turn_count=len(result.turns) if result else 0,
                interjection_count=len(result.interjections) if result else 0,
                continuation_anomaly_count=(
                    len(result.continuation_anomalies) if result else 0
                ),
                warning_count=warning_count,
                reconciled=status == "completed",
            )
        )
        peak_rss = max(peak_rss, process.memory_info().rss)

    if active_year is not None:
        writer.flush_year(active_year)
    writer.close()
    reports.write(
        working_directory / "reports",
        discovered_files=len(sources),
        discovered_bytes=total_bytes,
    )

    all_terminal = len(reports.processing_records) == len(sources)
    all_reconciled = all(record.reconciled for record in reports.processing_records)
    no_failures = not reports.file_failures
    acceptance_gate_passed = all_terminal and all_reconciled and no_failures
    baseline = (
        _baseline_comparison(
            file_count=len(sources),
            total_bytes=total_bytes,
            inventory_hash=input_inventory_hash,
            observed_tags=reports.observed_tags,
            observed_talktypes=reports.observed_talktypes,
        )
        if options.expects_phase_0_baseline
        else None
    )
    if baseline is not None and not all(
        item["match"] for item in baseline.values()
    ):
        acceptance_gate_passed = False

    completed_wall = datetime.now(UTC)
    output_checksums = _output_checksums(working_directory)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "pipeline_version": PIPELINE_VERSION,
        "package_version": __version__,
        "git_commit": _git_commit(),
        "chamber": reconstruction_config.chamber,
        "provenance": (
            "OpenAustralia/PublicWhip-style normalised XML derived from "
            "Australian Parliament ParlInfo"
        ),
        "input_inventory_sha256": input_inventory_hash,
        "configuration_hashes": {
            "business_types": business_config.sha256,
            "reconstruction": reconstruction_config.sha256,
        },
        "configuration_versions": {
            "business_types": business_config.version,
            "reconstruction": reconstruction_config.version,
        },
        "python_version": platform.python_version(),
        "dependency_versions": _dependencies(),
        "started_at": started_wall.isoformat(),
        "completed_at": completed_wall.isoformat(),
        "processing_duration_seconds": round(time.perf_counter() - started_perf, 6),
        "peak_resident_memory_bytes": peak_rss,
        "selected_scope": {
            "year": options.year,
            "one_file": options.one_file,
            "sample_size": options.sample_size,
            "prior_manifest": (
                str(options.prior_manifest) if options.prior_manifest else None
            ),
            "is_full_run": options.is_full_run,
            "phase_0_baseline_expected": options.expects_phase_0_baseline,
        },
        "file_counts": {
            "discovered": len(sources),
            "terminal": len(reports.processing_records),
            "completed": sum(
                record.status == "completed"
                for record in reports.processing_records
            ),
            "failed": len(reports.file_failures),
            "unreconciled": sum(
                record.status == "unreconciled"
                for record in reports.processing_records
            ),
        },
        "record_counts": dict(sorted(reports.output_counts.items())),
        "observed_tag_counts": dict(sorted(reports.observed_tags.items())),
        "observed_talktype_counts": dict(
            sorted(reports.observed_talktypes.items())
        ),
        "reconstruction_counts": dict(
            sorted(reports.reconstruction_counts.items())
        ),
        "warning_counts": dict(sorted(reports.warning_counts.items())),
        "error_count": (
            len(reports.file_failures) + len(reports.reconciliation_failures)
        ),
        "phase_0_baseline_comparison": baseline,
        "output_files": output_checksums,
        "overall_status": (
            "completed" if acceptance_gate_passed else "completed_with_errors"
        ),
        "acceptance_gate_passed": acceptance_gate_passed,
        "source_files": [
            {
                "source_file": source.relative_path,
                "source_file_sha256": source.source_file_sha256,
                "byte_size": source.byte_size,
                "modification_time_ns": source.modification_time_ns,
            }
            for source in sources
        ],
    }
    _write_json(working_directory / "run_manifest.json", manifest)
    working_directory.rename(final_directory)
    return PipelineOutcome(final_directory, manifest, False)
