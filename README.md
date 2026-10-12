# Australian Hansard Annotation Platform

This repository will support reproducible preprocessing and annotation of Australian
House of Representatives Hansard. The research objective is to study issue-specific,
out-party-directed hostility while retaining enough provenance to reconstruct every
published analytical dataset.

Phases 0 through 3 are complete. Phase 1 provides a read-only, versioned preprocessing
pipeline that emits explicit-schema Parquet datasets and audit/anomaly reports. Phase 2
provides the PostgreSQL 16 corpus schema, Alembic migration, transactional COPY/staging
importer, reconciliation tools, read-oriented views, and real-PostgreSQL tests. Phase 2.5
adds the corpus registry, bounded source-adapter registry, and versioned taxonomy and
annotation-schema templates. Phase 3 adds the secure research annotation web MVP.
Production deployment, adjudication/agreement, LLM workflows, political enrichment,
CAP, hostility coding, and Telegram/Hermes remain deliberately absent.

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

Phase 4A human/LLM evaluation and targeted read-only sampling are documented in
[`docs/PHASE_4A_LLM_EVALUATION.md`](docs/PHASE_4A_LLM_EVALUATION.md). The evaluator consumes
the existing AI-codebook package, checkpoints every request, validates structured output,
and keeps predictions separate from production human annotation tables.

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
and production publication remain unimplemented. The foundation evidence is in
[the Phase 2.5 report](docs/PHASE_2_5_REPORT.md).

## Phase 3 web MVP

Copy `.env.example` to the ignored `.env`, set the database values and a unique
`HANSARD_SESSION_SECRET` of at least 32 characters, then install/build the pinned
assets and migrate explicitly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
npm.cmd ci
npm.cmd run build
docker compose --env-file .env -f compose.dev.yaml up -d --wait postgres
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli migrate
```

Create the first administrator without placing a password on the command line:

```powershell
$env:FIRST_ADMIN_PASSWORD = Read-Host "Temporary password"
.\.venv\Scripts\python.exe -m hansard_annotator.web.cli create-user owner `
  --display-name "Project owner" --admin --password-env FIRST_ADMIN_PASSWORD
Remove-Item Env:FIRST_ADMIN_PASSWORD
```

Start the local application:

```powershell
npm.cmd run dev
```

This launches the FastAPI development server with reload enabled, using the project
virtual environment and `.env`. Open <http://127.0.0.1:8000>. `npm.cmd run build` only
rebuilds the local CSS and HTMX assets; npm is not the application runtime.

The application never migrates on startup. Development/pilot projects may pin the
explicit draft schema and taxonomies; production-mode projects reject drafts. Account
creation, password reset/disable/enable, session revocation, and development seeding are
administrator CLI operations (`python -m hansard_annotator.web.cli --help`).

Run the isolated PostgreSQL suite with `HANSARD_TEST_DATABASE_URL` set, then run the
gated browser workflow against a seeded local instance:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest tests/browser/test_phase3_browser.py
```

See the [Phase 3 report](docs/PHASE_3_REPORT.md), [design](docs/PHASE_3_DESIGN.md),
[security model](docs/PHASE_3_SECURITY_MODEL.md), and
[UI style guide](docs/UI_STYLE_GUIDE.md).

## Conference two-pass prototype

The development-only conference prototype separates fast general-domain coding from a
derived AUKUS screen. General schema `0.2.0` assumes policy, completes entirely non-policy
turns through one checkbox, and hides optional secondary topics. A separate AUKUS project
selects only submitted general annotations containing AU12 as primary or secondary and pins
the exact qualifying annotation version on every derived task.

Apply revision `20260810_05`, load both draft seeds, then seed the projects:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.db.cli migrate
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli schema load `
  config/annotation_schemas/australian_policy_annotation/0.2.0.yaml
.\.venv\Scripts\python.exe -m hansard_annotator.product.cli schema load `
  config/annotation_schemas/australian_aukus_screen/0.1.0.yaml
.\.venv\Scripts\python.exe -m hansard_annotator.web.cli `
  seed-conference-prototype --admin-username owner
```

See [the prototype design and checkpoint](docs/CONFERENCE_TWO_PASS_PROTOTYPE.md). Existing
Phase 3 annotations remain on their original pinned schema and are not converted.

### Export annotated pilot data

Project administrators and project managers can open a project with submitted annotations
and use **Export annotated data**. Choose one of two deterministic outputs:

- **Simple annotated data — CSV:** one speech per row, with separate annotation-code and
  readable topic-label columns for ordinary analysis; or
- **AI codebook drafting package — ZIP:** a richer package containing:

  - `AI_INSTRUCTIONS.md`, ready to give to an AI agent;
  - `annotations.jsonl`, the preferred complete machine-readable records;
  - `annotations.csv`, a spreadsheet-readable copy;
  - `CODEBOOK_CONTEXT.json`, the pinned schema and topic definitions; and
  - `manifest.json`, provenance, privacy notes and SHA-256 checksums.

Only current submitted/revised human annotation versions are exported. Clean speech text,
public parliamentary context and annotation notes are included; annotator identities,
drafts, credentials, raw XML and internal database IDs are excluded. Review free-text notes
before sharing an export externally. The optional AI package uses no AI API and incurs no
model API charge.

## Private pilot deployment

The reviewed Hostinger target is `annotator.polisde.tech`. Production deployment uses
`compose.production.yaml`: Caddy is the only public service, FastAPI runs non-root/read-only,
and PostgreSQL 16 is private with a persistent volume and separate application login. Web
startup never migrates automatically. Follow the guarded
[pilot deployment runbook](docs/PILOT_DEPLOYMENT_RUNBOOK.md) before any VPS mutation.

The deployment remains separate from the existing Hermes compose project and data. Never
expose ports 5432/8000 or run `docker compose down -v`. Hostinger snapshots do not replace
custom-format PostgreSQL backups, checksums and encrypted off-VPS copies.

Repository changes do not update the live domain automatically. The reviewed branch, CI and
manual pilot deployment process is documented in [the CI/CD policy](docs/CI_CD.md).
