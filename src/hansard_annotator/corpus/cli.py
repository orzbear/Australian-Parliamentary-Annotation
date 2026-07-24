"""Command-line interface for the Phase 1 preprocessing pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hansard_annotator.corpus.pipeline import PipelineOptions, run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only preprocessing for Australian Federal House Hansard. "
            "Generated outputs are written outside the raw corpus."
        )
    )
    parser.add_argument(
        "corpus_root",
        nargs="?",
        type=Path,
        default=Path("hansard_xml_files"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/processed"),
    )
    parser.add_argument(
        "--reconstruction-config",
        type=Path,
        default=Path("config/reconstruction/v1.yaml"),
    )
    parser.add_argument(
        "--business-config",
        type=Path,
        default=Path("config/business_types/v1.yaml"),
    )
    parser.add_argument("--year", type=int)
    parser.add_argument(
        "--file",
        dest="one_file",
        help="One corpus-relative file, for example 2010/2010-02-02.xml",
    )
    parser.add_argument("--sample-size", type=int)
    parser.add_argument("--prior-manifest", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--batch-size", type=int, default=5_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outcome = run_pipeline(
            PipelineOptions(
                corpus_root=args.corpus_root,
                output_root=args.output_root,
                reconstruction_config=args.reconstruction_config,
                business_config=args.business_config,
                year=args.year,
                one_file=args.one_file,
                sample_size=args.sample_size,
                prior_manifest=args.prior_manifest,
                run_id=args.run_id,
                batch_size=args.batch_size,
            )
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"preprocessing failed: {error}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "run_directory": str(outcome.run_directory),
                "run_id": outcome.manifest["run_id"],
                "overall_status": outcome.manifest["overall_status"],
                "acceptance_gate_passed": outcome.manifest[
                    "acceptance_gate_passed"
                ],
                "reused_existing_run": outcome.reused_existing_run,
                "record_counts": outcome.manifest["record_counts"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if outcome.manifest["acceptance_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

