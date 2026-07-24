# Australian Hansard Annotation Platform

This repository will support reproducible preprocessing and annotation of Australian
House of Representatives Hansard. The research objective is to study issue-specific,
out-party-directed hostility while retaining enough provenance to reconstruct every
published analytical dataset.

Phases 0, 1, 2, and 2.5 are complete. Phase 1 provides a read-only, versioned preprocessing
pipeline that emits explicit-schema Parquet datasets and audit/anomaly reports. Phase 2
provides the PostgreSQL 16 corpus schema, Alembic migration, transactional COPY/staging
importer, reconciliation tools, read-oriented views, and real-PostgreSQL tests. Phase 2.5
adds the corpus registry, bounded source-adapter registry, and versioned taxonomy and
annotation-schema templates. Phase 3
has not started: there is no authentication, annotation workflow, web application,
deployment, LLM workflow, political enrichment, or Telegram/Hermes integration.

## Safety invariants

- `hansard_xml_files/` is an immutable input archive and is ignored by Git.
- Never edit, format, rename, move, delete, or generate files inside that directory.
- Audit tooling must open source XML read-only, traverse deterministically, and write
  results only to standard output.
- Tests use synthetic XML. Do not copy full real Hansard files into `tests/`.
- Interjections are independent records in the planned model and must never be inserted
  into a main speaker's text.
- Continuation reconstruction is conservative and versioned. Ambiguous continuations
  remain standalone anomaly records.
- Secrets, database files, exports, Parquet/CSV/JSONL outputs, and generated research
  data must not be committed.

## Phase 0 corpus audit

The audit uses only the Python standard library and does not install dependencies:

```powershell
python tools/corpus_audit.py hansard_xml_files --json
```

For a human-readable summary:

```powershell
python tools/corpus_audit.py hansard_xml_files
```

The command validates paths and dates, hashes every file, performs a streaming
schema/count audit, reports malformed XML without stopping the run, and identifies the
deterministic first/middle/last sample for each year. It does not extract production
records or reconstruct turns. JSON is written to stdout so callers can redirect it to a
location outside the raw archive if needed.

Run Phase 0 tests with:

```powershell
python -m unittest discover -s tests -v
```

See [the implementation plan](docs/IMPLEMENTATION_PLAN.md),
[architecture decisions](docs/ARCHITECTURE_DECISIONS.md), and
[Phase 0 audit report](docs/PHASE_0_AUDIT_REPORT.md).

## Current corpus

The local archive spans 2010–2025. Exact Phase 0 results and limitations are recorded in
`docs/PHASE_0_AUDIT_REPORT.md`. Raw XML is intentionally absent from Git history.

## Phase 1 preprocessing

Create an isolated Python 3.12 environment and install the lock:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
```

Run a deterministic cross-corpus sample:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.corpus.cli `
  hansard_xml_files --sample-size 16 --run-id review-sample
```

Run the full Phase 0-baseline acceptance job:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.corpus.cli `
  hansard_xml_files --run-id phase1-full
```

Outputs are written below `data/processed/<run_id>/`, which is ignored by Git. The
command returns a nonzero exit code when any file fails, is partial/unreconciled, lacks a
terminal status, or the full corpus differs from the Phase 0 baseline.

Quality checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests tools
.\.venv\Scripts\mypy.exe src
```

See [Phase 1 CLI](docs/PHASE_1_CLI.md) and
[Phase 1 report](docs/PHASE_1_REPORT.md) for scopes, outputs, and accepted limitations.

## Phase 2 PostgreSQL corpus

Copy `.env.example` to the ignored `.env`, choose a local password and available
localhost port, then start PostgreSQL:

```powershell
docker compose --env-file .env -f compose.dev.yaml up -d --wait
```

Load `HANSARD_DATABASE_URL` and `HANSARD_PROCESSED_DATA_ROOT` from that local
environment before running commands. Apply migrations explicitly:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli connection-test
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli migrate
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli migration-status
```

Validate, import, and verify the one accepted Phase 1 package:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli dry-run `
  --run-dir data/processed/phase1-full-20260723-v4
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli import-run `
  --run-dir data/processed/phase1-full-20260723-v4
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli verify `
  --run-dir data/processed/phase1-full-20260723-v4
```

Safe read commands include `summary`, `sizes`, `inspect-turn`,
`inspect-fragment`, `continuation-anomalies`, `unlinked-interjections`, and
`sample-turns`. Speech text is truncated unless `--full-text` is explicitly supplied.
The importer accepts only a path below the configured processed-data root and the
canonical accepted run. It verifies all checksums, schemas, reports, and exact counts
before opening an import transaction.

Run database integration tests against a separate real PostgreSQL database:

```powershell
$env:HANSARD_TEST_DATABASE_URL = `
  "postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/hansard_test"
.\.venv\Scripts\python.exe -m pytest -q
```

See [the Phase 2 schema mapping](docs/PHASE_2_SCHEMA_MAPPING.md) and
[Phase 2 report](docs/PHASE_2_REPORT.md). PostgreSQL is the transactional source of
truth for the processed corpus; immutable XML remains authoritative for original bytes.

Export a deterministic, Unicode-safe review sample from the corpus's current
preprocessing run:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli export-review-sample `
  --output data/review/turn_sample_utf8.csv `
  --limit 30 --min-words 50 --max-words 1200 `
  --seed 20260724 --excel-compatible
```

Exports exclude orphan continuations by default; `--include-orphans` opts in. Filters
are available for year, date range, word count, interruption count, and the question
time, procedural, and ceremonial preprocessing hints. Review exports are ignored by
Git and cannot be written under raw XML, accepted processed-run, or backup directories.
The hint fields are contextual preprocessing output, not gold or human labels; `NULL`
is exported as `unknown`.

## Phase 2.5 product foundation

After applying Alembic revision `20260724_02`, inspect and load product metadata:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli adapters list
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli corpora list
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli taxonomy validate `
  config/taxonomies/australian_policy_domains/0.1.0.yaml
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli taxonomy load `
  config/taxonomies/australian_policy_domains/0.1.0.yaml
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli taxonomy load `
  config/taxonomies/content_status/1.0.0.yaml
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli schema validate `
  config/annotation_schemas/australian_policy_annotation/0.1.0.yaml
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli schema load `
  config/annotation_schemas/australian_policy_annotation/0.1.0.yaml
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli verify
```

`taxonomy list/show` and `schema list/show` expose hashes and repository-relative seed
paths. Validation performs no database writes. The sole adapter delegates to the accepted
Australian discovery/preprocessing code; no third-party plugin loading occurs.

Publication boundaries are in [the licensing policy](docs/LICENSING_AND_DATA_POLICY.md)
and terminology in [the glossary](docs/GLOSSARY.md). CAP, other parliament adapters,
projects, assignments, annotations, and all web/API functionality remain unimplemented.
The completed migration and acceptance evidence are in
[the Phase 2.5 report](docs/PHASE_2_5_REPORT.md).
