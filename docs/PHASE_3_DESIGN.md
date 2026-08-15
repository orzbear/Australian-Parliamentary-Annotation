# Phase 3 web annotation MVP design

Status: implementation gate approved by internal consistency review, 24 July 2026.

## Architecture and modules

The application is a FastAPI modular monolith over the existing SQLAlchemy/psycopg
PostgreSQL foundation. Jinja2 renders complete pages; HTMX progressively enhances
forms and queues, and small local JavaScript modules handle conditional fields,
keyboard shortcuts, dirty-state warnings, and mobile behaviour. The browser never
receives database credentials or an unrestricted query surface.

`web.auth` owns accounts, credentials, sessions, and global administration.
`web.projects` owns projects, memberships, provenance pins, and lifecycle transitions.
`web.tasks` owns criteria validation, batch generation, assignment, claiming, skip,
flag, and progress queries. `web.annotations` owns schema rendering, canonical
validation, and append-only revisions. `web.audit` is the single sanitised audit
writer. Corpus and taxonomy reads reuse existing tables and never duplicate parser or
seed logic.

## Identity, roles, and lifecycle

Accounts are administrator-created. `admin` is a seeded global role. A project
membership has exactly one active role: `project_manager`, `annotator`,
`adjudicator`, or `viewer`. Admin bypass is explicit and audited. Adjudicator is
reserved for visibility only; Phase 3 exposes no coder payload or adjudication action.

Projects move `draft -> active <-> paused -> archived`. They pin one corpus, a
preprocessing run belonging to that corpus, one schema version, its content hash, and
the referenced taxonomy-version IDs and hashes. Pins may change only while draft and
taskless. Development/pilot projects may pin draft definitions; production requires
published definitions. Activation or first task freezes all pins.

## Batch, task, and assignment lifecycle

Batch criteria are a typed allowlist: dates, years, word bounds, orphan policy, three
nullable hint filters, interruption minimum, limit, and seed. Preview counts in SQL
without speech text. Generation selects from the project's explicit run, orders by a
seeded deterministic hash plus turn key, fingerprints ordered keys, and inserts with
conflict protection. It never uses the moving current-run view.

Tasks move `available -> in_progress -> submitted|flagged|excluded`. Assignments move
`assigned -> claimed -> in_progress -> submitted|skipped|flagged|withdrawn`. Phase 3
creates one assignment per selected task, while the schema permits future independent
assignments. Claim-next locks eligible assignment rows using `FOR UPDATE SKIP LOCKED`
and commits the claim and task-state change together. Skipped assignments remain in the
owner's queue; flagging requires a reason.

## Annotation model and validation

One logical human annotation belongs to one assignment and the project's pinned schema.
Every save appends an `annotation_versions` row containing canonical JSON, SHA-256,
validation evidence, actor, event, and revision number. Identical draft saves reuse the
current revision. Submission atomically appends evidence and updates annotation,
assignment, and task states. Explicit revision adds a row; no prior version is edited.

The renderer reads active field definitions and taxonomy labels from PostgreSQL.
Supported types are `single_taxonomy`, `multiple_taxonomy`, `boolean`, `uncertainty`,
`short_text`, and `long_text`; any other type fails visibly. Validation interprets only
the stored allowlisted declarative operators. Drafts may omit required values but reject
unknown keys/types and stale values. Submissions enforce required/conditional rules,
two unique secondary domains, primary/secondary inequality, excluded fallback
secondary, fallback explanation, and unclassifiable reason.

## Audit policy

Authentication, account administration, session revocation, project lifecycle and
membership changes, batch preview/generation, assignment/claim/skip/flag, draft saves,
submissions, and revisions create audit events. Metadata contains identifiers, counts,
state transitions, hashes, and request IDs—not passwords, tokens, raw IP addresses,
full speech text, or complete annotation notes.

## Route and permission matrix

| Surface | Admin | Manager | Annotator | Adjudicator | Viewer |
|---|---|---|---|---|---|
| Account administration/audit | manage | no | no | no | no |
| Project overview/provenance | all | own | own | own | own |
| Project configuration/lifecycle | all | own | no | no | no |
| Memberships/batches/assignments | all | own | no | no | no |
| Aggregate progress | all | own | own-safe | own-safe | own-safe |
| Claim/open/save/submit/skip/flag | support | no | personal only | no | no |
| Personal revisions | all for support | no payload | personal only | no | no |

All state changes are POST routes with CSRF and service-level permission checks.
Cross-project and cross-annotator identifiers return a non-disclosing 404/403.

## Pages and responsive workspace

Pages include login/password change, personal dashboard and queues, project list,
project overview/provenance/progress, project create/configure/members, batch
preview/generation, assignment management, annotation workspace/history, and admin
users/audit. Desktop uses a 62/38 speech/form grid with sticky context/actions. At
tablet/mobile widths it becomes context, speech, form, actions in one column without
horizontal overflow. Speech is escaped, selectable plain text with preserved
paragraphs. Structural booleans are labelled “preprocessing hints.”

## Accessibility and testing

Semantic landmarks, labelled controls, accessible summaries, visible focus, large touch
targets, an `aria-live` save region, reduced-motion handling, and keyboard-operable
flows target WCAG 2.1 AA practice. Testing combines pure validation/security tests,
real-PostgreSQL migration/service/concurrency tests, FastAPI request tests, and
Playwright desktop/mobile/keyboard workflows. Manual acceptance includes 200% zoom,
360–390 px width, focus order, selection/copying, and overflow review.

## Explicit Phase 4 deferrals

No double-coding protocol, payload unblinding, agreement statistic, adjudication case,
consensus/gold label, CAP, hostility, LLM, political enrichment, Telegram/Hermes,
second adapter, research export, or production deployment is implemented.
