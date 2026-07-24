# Phase 1 CLI

The Phase 1 command is read-only with respect to the source corpus. It writes generated
Parquet, CSV, and JSON only below the configured output root.

## Installation

Use Python 3.12:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
```

The package exposes both:

```powershell
.\.venv\Scripts\hansard-preprocess.exe --help
.\.venv\Scripts\python.exe -m hansard_annotator.corpus.cli --help
```

## Common runs

Deterministic bounded sample:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.corpus.cli `
  hansard_xml_files `
  --sample-size 16 `
  --run-id phase1-sample-16
```

One year:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.corpus.cli `
  hansard_xml_files --year 2016 --run-id phase1-2016
```

One corpus-relative file:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.corpus.cli `
  hansard_xml_files `
  --file 2010/2010-02-02.xml `
  --run-id phase1-one-file
```

Only new or changed source files relative to an earlier manifest:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.corpus.cli `
  hansard_xml_files `
  --prior-manifest data/processed/phase1-full/run_manifest.json `
  --run-id phase1-changed
```

Full Phase 0-baseline acceptance:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.corpus.cli `
  hansard_xml_files --run-id phase1-full
```

Use `--output-root` to change `data/processed`, `--batch-size` to reduce/increase
in-memory Parquet batches, and explicit `--reconstruction-config`/`--business-config`
paths to process with another reviewed version.

## Selection and identity behavior

- Discovery never follows symlinks and accepts only `YYYY/YYYY-MM-DD.xml`.
- `--file` is resolved and refused if it leaves the corpus root.
- `--sample-size N` selects evenly spaced indices from the sorted corpus.
- A prior manifest compares SHA-256, never modification time.
- The default run ID derives from input inventory and configuration hashes. A completed,
  matching run is reused without rewriting output. An existing incomplete/mismatched
  directory is refused rather than overwritten.

## Output layout

```text
data/processed/<run_id>/
├── source_files/year=YYYY/*.parquet
├── debate_sections/year=YYYY/*.parquet
├── debate_events/year=YYYY/*.parquet
├── speech_fragments/year=YYYY/*.parquet
├── speaker_turns/year=YYYY/*.parquet
├── speaker_turn_fragments/year=YYYY/*.parquet
├── interjections/year=YYYY/*.parquet
├── continuation_anomalies/year=YYYY/*.parquet
├── reports/
│   ├── file_processing_report.csv
│   ├── corpus_summary.csv
│   ├── unknown_headings.csv
│   ├── malformed_files.csv
│   ├── missing_speakers.csv
│   ├── continuation_anomalies.csv
│   ├── duplicate_ids.csv
│   ├── image_elements.csv
│   ├── reconciliation_failures.csv
│   └── source_url_exceptions.csv
└── run_manifest.json
```

All core Parquet tables use explicit schemas. Partition buffers flush at year boundaries.
The manifest records output checksums, file/record/warning/error counts, environment and
configuration versions, duration, peak RSS, and the full-run Phase 0 comparison.

## Exit behavior

- `0`: every selected file has a terminal successful/reconciled status and, for the
  canonical unfiltered corpus, every Phase 0 baseline measure matches.
- `1`: processing completed but the acceptance gate failed.
- `2`: invocation/setup failed before a completed run could be produced.

A malformed or unsafe file is recorded, discarded atomically, and does not prevent later
files from being attempted. Its run status is `completed_with_errors`.

