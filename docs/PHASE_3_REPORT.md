# Phase 3 completion report

Date: 24 July 2026  
Branch: `phase-3-web-mvp`  
Starting checkpoint: `phase-2.6-approved` (`b451d2e26cc4e33e781b621240805718f85cc034`)

## Outcome and scope

Phase 3 delivers the secure, responsive annotation web MVP on the accepted corpus and
product foundation. It implements administrator-created accounts, secure browser sessions,
project and role isolation, immutable version pins, deterministic batches, concurrency-safe
task claiming, human drafts/submissions, skip/flag workflows, append-only revisions, audit
events, and personal/project dashboards.

The design gate was completed before the migration and routes in
`PHASE_3_DESIGN.md`, `PHASE_3_SECURITY_MODEL.md`, and `UI_STYLE_GUIDE.md`.
No Phase 4 or later feature was implemented.

## Migration and database

Alembic revision `20260724_04` follows `20260724_03` without modifying an accepted
migration. It creates:

- identity/security: `users`, `user_credentials`, `global_roles`,
  `user_global_roles`, `web_sessions`;
- project work: `projects`, `project_taxonomy_pins`, `project_memberships`,
  `batches`, `tasks`, `assignments`;
- research history: `annotations`, `annotation_versions`, `audit_events`.

Foreign keys bind projects to the accepted corpus, preprocessing run, schema and taxonomy
versions; tasks bind to immutable speaker turns; assignments bind users to tasks; annotation
series bind to assignments and append-only versions. Database constraints/triggers enforce
case-insensitive user uniqueness, status compatibility, pin immutability, append-only
research/audit history, and safe downgrade refusal when Phase 3 data exists.

Before migration, a custom-format development backup was created and its restore catalogue
validated:

- file: `backups/hansard_dev_phase3_pre_20260724.dump` (ignored);
- bytes: 595,834,103;
- SHA-256: `081337CF7CB804A80E460AAAB9162D44227B74D112751B867DD3E5F878F69423`.

The applied development revision is `20260724_04`.

## Authentication and permissions

Passwords use Argon2id and the configurable minimum is 12 characters. Login failures are
generic, failed counts and temporary lockouts persist, disabled users cannot sign in, and
password reset/disable revoke sessions. The browser receives only a random opaque token;
the database stores an HMAC digest. Sessions enforce idle and absolute expiry and carry a
session-bound CSRF token. Cookies are HTTP-only and SameSite=Lax, with Secure mandatory
outside local development.

The global `admin` role governs account/system operations. Each project has one active
membership role per user: `project_manager`, `annotator`, `adjudicator`, or `viewer`.
The Phase 3 role exists for future compatibility but exposes no adjudication feature.
All project reads/writes check membership server-side. Annotators can read and mutate only
their own assignments and annotation payloads; progress views expose aggregates only.

Administrator CLI commands cover create/reset/disable/enable/list/revoke sessions. There
is no registration, email recovery, or public API key.

## Project and annotation workflow

The seeded development project `phase3-interface-pilot` pins:

- accepted run `phase1-full-20260723-v4`, pipeline `1.0.0`;
- schema `australian_policy_annotation 0.1.0`;
- taxonomy hashes for `australian_policy_domains 0.1.0` and
  `content_status 1.0.0`.

Its deterministic batch uses seed `20260724` and contains 50 tasks. Filters are parsed from
a typed allowlist; user SQL is impossible. Preview does not create tasks. Repeating an
identical generation request reuses the batch, while the same name with different criteria
is rejected. Tasks explicitly query the project's preprocessing run.

Claims run in a transaction with `FOR UPDATE SKIP LOCKED`. Manual assign, withdraw/reassign,
skip and reason-required flag operations are audited. The declarative renderer supports the
accepted field types and conditional rules without executable schema code. Both browser and
server enforce required/conditional fields; only the server is authoritative.

One logical human annotation exists per assignment. Canonical values and hashes are stored
in append-only `annotation_versions`; identical draft saves are no-ops, submission adds a
version, and old versions cannot be updated/deleted. Human data are not mixed with LLM or
adjudicated labels.

## Interface

The local, pinned Tailwind/HTMX build produces a deliberate research-workspace design:
calm neutral surfaces, indigo primary actions, readable speech measure, clear status colours,
visible focus, error summaries, labels and help text. The annotation workspace uses a
two-column desktop layout and a single-column mobile layout with no horizontal overflow at
390 px. Conditional fields, dirty-state unload warning, Ctrl/Cmd+S draft save and
Ctrl/Cmd+Enter submit-next are implemented.

The application serves no CDN resources. Parliamentary text is Jinja-escaped and is never
treated as HTML. CSP, nosniff, referrer, frame, permissions and request-ID headers are set.

## Verification evidence

- Full pytest suite against isolated PostgreSQL: **51 passed, 1 browser-gate skipped**.
- Live Playwright Chromium workflow: **1 passed in 4.94 s**.
- PostgreSQL Phase 3 integration tests: migration/relations, auth/lockout/session/disable,
  project/batch/concurrent claim/annotation/permissions, and browser CSRF/headers all pass.
- Concurrent claim test: two independent connections claimed two distinct assignments;
  no duplicate claim occurred.
- Ruff: **all checks passed**.
- Strict mypy: **success, 79 source files**.
- Pinned asset build: successful.
- Controlled application plus PostgreSQL restart: existing opaque session resolved; draft
  version count remained **5 before / 5 after**.
- Phase 2 database verifier: `ok: true`, no invariant failures.
- Phase 2.5 product verifier: `ok: true`, no orphan registry links.
- Accepted Phase 1 package dry-run: `ok: true`, 259 artefacts / 1,137,990,532
  bytes, zero writes, manifest SHA-256
  `1d7698829d9d1cdceb0373c5d2fc55a719f2a43e6d5242e38bd99067215680df`.
- Raw read-only audit: **926 files**, **784,583,349 bytes**, inventory SHA-256
  `7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2`,
  and zero source mutations. This exactly matches the accepted baseline.
- Git diff from `phase-2.6-approved` contains no change under parser/corpus code,
  configuration seeds, or revisions `01`–`03`; the ignored accepted processed package
  passed the complete read-only manifest/checksum dry-run above.

Development evidence after browser runs contains 1 user, 1 project, 2 taxonomy pins,
1 batch, 50 tasks/assignments, 5 annotation series, 9 versions, and 72 audit events.
A final metadata check found zero password/session-token/speech-text key matches and zero
oversized audit records.

## Screenshots

Synthetic/test display text was substituted before capture. The seven reviewed images are:

- `docs/screenshots/phase3/login-desktop.png`;
- `dashboard-desktop.png`;
- `project-overview-desktop.png`;
- `annotation-workspace-desktop.png`;
- `annotation-workspace-mobile.png`;
- `validation-error.png`;
- `submitted-success.png`.

They contain no passwords, absolute paths, private notes, or unpublished full-corpus excerpts.

## Remaining decisions and limitations

Before Phase 4, the owner must:

1. review/publish or revise the still-draft codebook, schema and taxonomies;
2. approve coder count/overlap and assignment balancing;
3. define blinding release, manager label visibility and adjudicator eligibility;
4. define revision/recode, disagreement, adjudication and gold-label policies;
5. define evidence-span requirements and treatment of skipped/flagged work;
6. approve pilot operating procedures, account ownership and retention.

Production hardening/deployment remains separate: HTTPS proxy, domain, backup/restore
objectives, off-host retention, monitoring, capacity and VPS/Hermes isolation require a
future approved phase. The development account password was ephemeral and is intentionally
not recorded; the owner should reset/create the review account through the CLI.

All three loaded definitions remain `draft`. CAP, hostility, political enrichment,
agreement/adjudication, LLM annotation, Telegram/Hermes, a second adapter, public
registration and production deployment are absent. Immutable corpus records, raw XML and
accepted Phase 1 outputs were unchanged. Phase 4 was not started.
