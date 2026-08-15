# Conference two-pass annotation prototype

Status: approved prototype design, 10 August 2026.

## Objective

Reduce coder friction for the conference project without overwriting the accepted Phase 3
pilot or beginning agreement/adjudication work. The prototype separates broad policy-domain
coding from narrow Australian-issue coding.

## Pass 1: general policy domain

Schema `australian_policy_annotation 0.2.0` is a new draft; version `0.1.0` remains
unchanged. A turn is treated as policy unless the coder checks **Entirely non-policy**.
Policy turns require one primary Australian domain. Secondary domains are optional and are
hidden behind **Add secondary topics**. The Australian-specific free-text field and the
four-way content-status question are absent. `AU_OTHER_REVIEW` still requires an explanation.

Checking non-policy hides and clears policy fields. Submitting it records a completed human
annotation and advances to the next task; it does not use the assignment `skipped` state,
which continues to mean unfinished work.

## Pass 2: AUKUS screen

Schema `australian_aukus_screen 0.1.0` is a separate draft. Its first field is a fast
checkbox recording whether the speech substantively discusses AUKUS, followed by optional
notes. It belongs to a separate derived project with independent tasks, assignments,
annotations and progress.

The initial derived batch selects only current submitted Pass 1 annotation versions where
`AU12` occurs as the primary or a secondary domain and `is_non_policy` is false. The filter
uses stored JSON values, never speech-text heuristics.

## Provenance and reproducibility

A derived project points to one source project. Both projects must pin the same corpus and
preprocessing run. Every derived task records the exact immutable source
`annotation_version` that qualified its speaker turn. A selection fingerprint includes the
turn key, source annotation-version identity and values hash.

Revising Pass 1 after generation does not mutate the derived batch. A refreshed selection
requires a new named batch/project decision, making differences auditable. Existing Phase 3
projects, tasks and annotations are not migrated or rewritten.

## Explicit boundaries

This is sequential human coding, not double coding, agreement, adjudication, consensus or
gold-label production. It adds no CAP, hostility, LLM, political enrichment, Telegram,
second parliament adapter or deployment work. A broader Australian-issue taxonomy remains
a later codebook decision; this prototype contains AUKUS only.

## Implemented checkpoint

The prototype is implemented on branch `phase-3-conference-prototype` at Alembic revision
`20260810_05`. The migration adds nullable `projects.source_project_id` and
`tasks.source_annotation_version_id` links plus database triggers enforcing same-corpus/run,
same-turn, source-project, and submitted-version compatibility. Existing projects and tasks
retain null values and are unchanged.

The development database contains:

- active development project `conference-general-pass`, pinned to
  `australian_policy_annotation 0.2.0`, with a deterministic 50-task sample;
- active derived project `conference-aukus-pass`, pinned to
  `australian_aukus_screen 0.1.0` and linked to the general pass;
- one derived AUKUS task created during browser acceptance.

The three general submissions and one AUKUS submission currently visible in these projects
are development/browser evidence, not research annotations. Create a fresh pilot project and
batch before collecting conference data.

The pre-migration custom-format backup is ignored at
`backups/hansard_dev_pre_two_pass_20260810.dump` (595,908,556 bytes), SHA-256
`18C1B7A0139C7C455BDC476E9FE257A5374AA2177FCF7B375B5C578C316B1315`; its restore catalogue
was readable.

Verification on 10 August 2026:

- complete pytest/PostgreSQL suite: 53 passed, 2 gated browser tests skipped;
- live Chromium suite: 2 passed;
- focused source-revision test proves a later Pass 1 revision does not change the pinned
  Pass 2 source version;
- Ruff and strict mypy pass;
- Phase 2/product verification and the accepted Phase 1 dry-run pass;
- raw audit remains 926 files, 784,583,349 bytes, baseline inventory hash, zero mutations.

## Trying the prototype

Sign in with the existing `owner` account and open **Conference general policy pass**.
Claim a task, choose one primary topic (or check entirely non-policy), and optionally reveal
secondary topics. After one or more submitted AU12 annotations, open **Conference AUKUS
second pass** and generate a batch with:

```json
{"source_domain_code":"AU12","limit":50,"seed":20260810}
```

The development-only CLI setup is idempotent after the two draft schemas are loaded:

```powershell
.\.venv\Scripts\python.exe -m hansard_annotator.web.cli `
  seed-conference-prototype --admin-username owner --task-count 50 --seed 20260810
```

## General annotated-data export amendment

Project managers can download current submitted annotations from the project page. A simple
flattened CSV supports ordinary analysis; a separate AI-codebook option adds JSONL, pinned
schema/taxonomy definitions, ready-to-use AI instructions and a checksum manifest. Neither
output includes drafts, annotator identities, credentials, internal database IDs, canonical
fragment XML or raw source XML. Free-text annotation notes are included, so the owner must
review them before external sharing.

The download is bounded to 2,000 annotations, generated in memory, protected by project-role
authorization and CSRF, and recorded as a sanitised audit event. Identical choices and current
annotation versions produce identical output bytes and snapshot hashes. The export design is
extensible to later approved formats but implements no model call, prompt execution, automatic
codebook publication or Phase 4 workflow.
