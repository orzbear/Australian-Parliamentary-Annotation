# Phase 2.5 productisation and extensibility report

Status: complete and awaiting review, 24 July 2026.

## 1. Scope

Phase 2.5 extends the accepted Phase 2 corpus with product identity and reusable,
versioned metadata. It adds no users, authentication, projects, assignments,
annotations, adjudication, web/API routes, LLM work, political enrichment, Telegram,
deployment, new parser logic, or second parliament adapter.

The pre-migration design is in `PHASE_2_5_DESIGN.md`. The accepted source XML, Phase 1
outputs, deterministic corpus keys, text, reconstruction, anomaly decisions, and Phase 2
counts were not altered.

## 2. Migration and corpus registry

Alembic revision `20260724_02`, following `20260723_01`, creates the eight product tables,
adds required corpus links to preprocessing runs and source files, backfills the accepted
Australian corpus, and compatibly extends `annotation_ready_turns` with public corpus
identity. Reconstruction runs remain linked through preprocessing runs.

The registered corpus is:

| Field | Value |
|---|---|
| Public UUID | `385a0089-7e50-59c7-bfc6-02ada0e4a139` |
| Slug | `australian-house-representatives-hansard` |
| Name | Australian House of Representatives Hansard |
| Jurisdiction / legislature | Australia / Parliament of Australia |
| Chamber / language | House of Representatives / `en` |
| Source format | `openaustralia_publicwhip_xml` version `1` |
| Actual database date range | 2010-02-02 to 2025-10-29 |
| Licence / redistribution | `pending_review` / `pending_review` |
| Public / active | false / true |
| Current preprocessing run | 3 |

All accepted preprocessing runs and all source-file versions resolve to a compatible
corpus. The current-run constraint rejects a run belonging to another corpus. Downgrade
is supported only while no loaded taxonomy/schema or additional corpus would be lost;
otherwise it refuses with an explicit warning.

## 3. Adapter foundation

A typed, capability-aware, static registry exposes discovery, source validation,
preprocessing, direct database import, canonical export, and incremental-update
capabilities without pretending every adapter supports every operation.

The sole registered adapter is `openaustralia_publicwhip_xml` version `1.0.0`. It
delegates source discovery and preprocessing to the existing Phase 1 functions and does
not duplicate the parser. Metadata inspection performs no corpus parse and no arbitrary
third-party module loading.

## 4. Taxonomies and annotation schema

Explicit safe seed loading stores canonical SHA-256 hashes and repository-relative seed
paths. Same version plus same hash returns `reused`; different content conflicts.
Published versions and their child content are database-immutable.

| Seed | Version/status | Content | SHA-256 |
|---|---|---|---|
| `australian_policy_domains` | 0.1.0 draft | 14 analytical labels plus `AU_OTHER_REVIEW` | `275c2f292038036e3ee351b66b2f78b0b3f989aeed8475fbe3beb447be5fbede` |
| `content_status` | 1.0.0 draft | 4 approved labels | `2b13bcc4b8bfebfdd60b10f3e701633499290c96537d47cde61829c465da636f` |
| `australian_policy_annotation` | 0.1.0 draft | 8 fields | `e8b8d88b84d5d94b7915ff908aea0e47307eb4bf41924f15183d1a3c9cc8d055` |

The schema fields are `content_status`, `primary_australian_domain`,
`secondary_australian_domains`, `specific_australian_issue`, `topic_uncertain`,
`fallback_explanation`, `unclassifiable_reason`, and `annotation_notes`. Declarative
rules encode the substantive-policy requirements/null rules, unique secondary domains
with maximum two, primary/secondary inequality, fallback explanation, and
unclassifiable reason. No executable rule language is accepted.

CAP was not seeded and is not required by the schema. Hierarchy and relation structures
can support a separately authorised future CAP enrichment without redesign.

## 5. Test and compatibility results

Final development checks:

- pytest: **36 passed** against real PostgreSQL 16.9 in **16.87 seconds**;
- Ruff: **all checks passed**;
- strict mypy: **no issues in 41 source files**;
- migration: empty through both revisions, existing Phase 2 backfill, supported
  downgrade/re-upgrade, and protected downgrade all tested;
- CLI: adapter/corpus/taxonomy/schema list, show, validate, load, verification, and
  invalid-input exit behaviour tested.

The acceptance migration preserved the exact Phase 2 counts:

| Dataset | Rows |
|---|---:|
| Source file versions | 926 |
| Debate sections | 97,975 |
| Debate events | 14,352 |
| Speech fragments | 269,793 |
| Speaker turns | 160,577 |
| Turn-fragment lineage | 191,263 |
| Interjections | 78,530 |
| Continuation anomalies | 8,239 |
| Image anomalies | 4 |

All Phase 2 invariants passed, with 77,562 linked and 968 unlinked interjections, the
four accepted continuation-reason counts, and 260 database artifact records.

The identical accepted import returned `completed_reused`, preprocessing run 3,
`corpus_writes: 0`. After PostgreSQL restart, both Phase 2 and Phase 2.5 verification
passed again with zero invariant or linkage failures.

## 6. Backup and immutable-input verification

Before migration, a validated PostgreSQL custom-format logical backup was created at the
ignored local path `backups/hansard_dev_phase25_pre_20260724.dump`:

- size: 486,674,432 bytes;
- SHA-256: `be5ef68e5a9dc8e71d5a846943caa83d8823f661682298efe0068b9782f11fdd`.

An initial `CREATE DATABASE ... TEMPLATE` snapshot process was terminated by PostgreSQL
and automatic recovery completed successfully; no snapshot database remained. The
logical dump was then validated with `pg_restore --list` before migration.

Post-acceptance source checks:

- raw XML: 926 files, 784,583,349 bytes, inventory SHA-256
  `7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2`,
  malformed 0, path errors 0, mutation detected 0;
- accepted Phase 1: 259 manifested artifacts, 1,137,990,532 bytes, manifest SHA-256
  `1d7698829d9d1cdceb0373c5d2fc55a719f2a43e6d5242e38bd99067215680df`,
  all checksums valid and database writes 0.

Both directories and the backup remain ignored by Git.

## 7. Decisions before Phase 3

Owner decisions still required:

1. review, revise if needed, and explicitly publish the draft taxonomy/schema and coder
   codebook;
2. initial users, Argon2id/session lifecycle, account recovery, roles, and project
   permission boundaries;
3. project eligibility, sampling, schema pinning, and corpus-version selection;
4. coder count, assignment/claiming, blinding, revision, disagreement, recoding, and
   adjudication policy;
5. evidence-span and annotation validation/versioning details;
6. development/production database-role separation and audit/retention requirements;
7. source-code, source-data, processed-corpus, annotation, and demo publication decisions.

CAP, production deployment, political metadata, LLM, and Telegram/Hermes decisions are
later optional-phase gates, not prerequisites for the initial Phase 3 workflow unless
the owner changes scope.

## 8. Boundary confirmation

No raw XML or accepted Phase 1 file was modified. No CAP content or placeholder was
imported. No second parliament adapter or ParlaMint implementation was created. Phase 3
was not started.
