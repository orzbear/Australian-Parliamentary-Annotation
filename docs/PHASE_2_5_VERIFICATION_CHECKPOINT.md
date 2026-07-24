# Phase 2.5 verification checkpoint

Status: verification complete, 24 July 2026. Phase 3 remains unauthorised.

## 1. Verdict on revision `20260723_01`

Revision `20260723_01_phase2_corpus.py` **had been changed after Phase 2
acceptance**. The repository has no commits (`git log` is empty) and every repository
file remains untracked, so Git cannot provide the accepted blob or a historical diff.
The accepted form was reconstructed from the Phase 2 implementation/report and the
documented Phase 2.5 compatibility change.

The only post-acceptance change was the replacement of unrestricted
`Base.metadata.create_all/drop_all` with a hard-coded list of the fourteen Phase 2
tables. No trigger, constraint, index, view, column, default, or SQL function text in
revision 01 was changed.

## 2. Exact migration diff and impact

These were every added/changed lines in revision 01 before this checkpoint:

```diff
+PHASE_2_TABLES = (
+    "preprocessing_runs",
+    "database_import_runs",
+    "source_files",
+    "source_file_versions",
+    "debate_sections",
+    "debate_events",
+    "speech_fragments",
+    "reconstruction_runs",
+    "speaker_turns",
+    "speaker_turn_fragments",
+    "interjections",
+    "continuation_anomalies",
+    "image_anomalies",
+    "import_artifacts",
+)

-    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)
+    Base.metadata.create_all(
+        bind=op.get_bind(),
+        tables=[Base.metadata.tables[name] for name in PHASE_2_TABLES],
+        checkfirst=False,
+    )

-    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
+    Base.metadata.drop_all(
+        bind=op.get_bind(),
+        tables=[Base.metadata.tables[name] for name in reversed(PHASE_2_TABLES)],
+        checkfirst=False,
+    )
```

The table tuple and `tables=` arguments affected upgrade schema creation and downgrade
table removal. They did not change the fourteen Phase 2 tables' definitions, constraints,
indexes, triggers, views, or defaults. In a clean Phase 2-only Python process the
original and changed revisions created the same database. If Phase 2.5 ORM models had
already entered shared metadata, however, the original revision would also act on those
tables while the changed revision would not. The modification therefore changed actual
conditional upgrade/downgrade behaviour; it was not merely formatting or import syntax.

## 3. Corrective action

Revision 01 was restored to its accepted executable form:

```python
Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)
Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
```

The compatibility handling now resides wholly in revision `20260724_02` and Phase 2.5
model code. Revision 02 temporarily detaches its eight product tables from shared
metadata, retains their table objects across repeated Alembic module reloads, and creates
or drops them only while revision 02 executes.

An automated PostgreSQL test upgrades an empty database to revision 01 twice, capturing
and comparing an exact ordered fingerprint of Phase 2 columns, types, defaults,
identity/null properties, constraints, indexes, views, and triggers. Both fingerprints
are equal. The full empty chain, downgrade/re-upgrade, and protected downgrade also pass.
No Git history was rewritten.

## 4. Database identity audit

No migration or application code assigns, compares, or selects
`preprocessing_run_id = 3`, `current_preprocessing_run_id = 3`, or
`reconstruction_run_id = 3`. An automated repository scan of `migrations/**/*.py` and
`src/**/*.py` fails on such an expression.

Revision 02 now selects the accepted current run using all of:

- corpus slug `australian-house-representatives-hansard`;
- run name `phase1-full-20260723-v4`;
- manifest SHA-256
  `1d7698829d9d1cdceb0373c5d2fc55a719f2a43e6d5242e38bd99067215680df`;
- inventory SHA-256
  `7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2`.

The corpus public UUID is deterministic:
`385a0089-7e50-59c7-bfc6-02ada0e4a139`. The migration refuses a database containing an
accepted run when that stable identity is absent. It never uses sequence values as
product identity.

The pre-report repository search found three database-record-related literal occurrences
of the number 3:

1. `docs/PHASE_2_5_REPORT.md` records that the accepted development database's current
   preprocessing row happened to be 3.
2. The same report records that import reuse returned observed preprocessing row 3.
3. The non-default-identity integration test asserts `run_id != 3`.

The first two are historical observations, not executable assumptions. The third is a
negative safety assertion. Other occurrences of 3 concern methodological counts,
versions, fixture ordinals, Phase 3 prose, or continuation statistics rather than
database identity.

## 5. Non-default-identity integration result

The PostgreSQL migration test restarts the preprocessing identity at 1000, inserts an
earlier failed dummy row at 1000, inserts the accepted run at 1001, restarts source-file
identity at 2000, and then applies revision 02. The migrated corpus selects run 1001,
links both preprocessing rows and source file 2000 to the corpus, and does not depend on
row 3. Result: **passed**.

## 6. Backup checksum

Backup: ignored `backups/hansard_dev_phase25_pre_20260724.dump`.

- Expected and actual size: **486,674,432 bytes**.
- Expected and actual SHA-256:
  `be5ef68e5a9dc8e71d5a846943caa83d8823f661682298efe0068b9782f11fdd`.

Result: **exact match**.

## 7. Full restore result

The dump was fully restored with `pg_restore --exit-on-error` into the unique temporary
database `hansard_restore_check_20260724_checkpoint`. The command exited 0 in
**89.944 seconds**. It emitted no warnings or ignored fatal errors. This was an actual
data restore, not `pg_restore --list`; `hansard_dev` was neither overwritten nor
modified by the restore.

The restored database reported revision `20260723_01`. Phase 2 verification ran against
the restored rows and accepted Phase 1 package before any Phase 2.5 migration.

## 8. Pre-migration Phase 2 verification

Result: **passed**, zero invariant failures.

| Record | Count |
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

Additional accepted evidence: 260 artifact rows, 77,562 linked interjections, 968
unlinked interjections, and exact continuation reasons 7,050 / 1,089 / 97 / 3.

## 9. Post-migration Phase 2 verification

Revision `20260724_02` applied to the restored database in **1.004 seconds**. Phase 2
verification passed with the same exact counts, artifact count, linkage statistics,
continuation reasons, and zero invariant failures. The accepted import then returned
`completed_reused` with `corpus_writes: 0`.

## 10. Post-migration Phase 2.5 verification

Product verification passed before and after PostgreSQL restart:

- one active Australian corpus;
- current run name and both accepted hashes match;
- one linked preprocessing run and 926 linked source files in the restored backup;
- zero unlinked preprocessing runs or incompatible source versions;
- 14 analytical Australian labels plus non-analytical fallback;
- four content-status labels;
- eight annotation-schema fields;
- CAP not required;
- no Phase 3 tables.

Loaded content hashes:

| Object | Version | Status | Count | SHA-256 |
|---|---|---|---:|---|
| `australian_policy_domains` | 0.1.0 | draft | 15 labels / 14 analytical | `275c2f292038036e3ee351b66b2f78b0b3f989aeed8475fbe3beb447be5fbede` |
| `content_status` | 1.0.0 | draft | 4 labels | `2b13bcc4b8bfebfdd60b10f3e701633499290c96537d47cde61829c465da636f` |
| `australian_policy_annotation` | 0.1.0 | draft | 8 fields | `e8b8d88b84d5d94b7915ff908aea0e47307eb4bf41924f15183d1a3c9cc8d055` |

## 11. Durations

- Full custom-format restore: **89.944 seconds**.
- Revision 02 migration: **1.004 seconds**.
- Pre-migration Phase 2 verification: **1.848 seconds**.
- Post-migration Phase 2 verification: **1.724 seconds**.
- Post-restart Phase 2 verification: **0.926 seconds**.
- Post-restart embedded Phase 2.5/Phase 2 verification: **0.871 seconds**.

## 12. Final automated checks

- pytest: **37 passed** against real PostgreSQL 16.9 in **17.20 seconds**.
- Ruff: **all checks passed**.
- strict mypy: **no issues in 41 source files**.

Tests cover repeatable Phase 2 schema fingerprints, empty chains, downgrade/re-upgrade,
non-default identities, stable accepted-run selection, draft statuses, import reuse, and
Phase 2/2.5 verification. The full backup restore remains an explicit checkpoint
operation because it uses a 486 MB private ignored backup and a temporary database.

## 13. Draft-version confirmation

`australian_policy_domains` 0.1.0, `content_status` 1.0.0, and
`australian_policy_annotation` 0.1.0 all remain **draft**. Nothing was published,
renamed, or version-bumped.

The documented rule is: Phase 3 development may use a pinned draft; a bounded pilot may
use a specifically pinned draft; production or gold-standard annotation requires an
explicitly published immutable version; later changes require a new version.

## 14. Temporary database removal

After all evidence above was written, PostgreSQL connections to the exact temporary name
were terminated and `hansard_restore_check_20260724_checkpoint` was dropped. A
`pg_database` query returned remaining count **0**. The backup remains ignored and
`hansard_dev` remains present.

## 15. Immutable-input confirmation

Raw XML remains unchanged: 926 files, 784,583,349 bytes, inventory SHA-256
`7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2`,
and `mutation detected: 0`.

Accepted Phase 1 outputs remain unchanged: 259 artifacts, 1,137,990,532 bytes, manifest
SHA-256 `1d7698829d9d1cdceb0373c5d2fc55a719f2a43e6d5242e38bd99067215680df`,
all checksums valid, database writes 0.

## 16. Phase boundary

Phase 3 was not started. No feature, user, authentication, project, assignment,
annotation, adjudication, API, web, LLM, political-enrichment, Telegram, or deployment
work was added. This checkpoint changed only migration compatibility/identity safety,
verification tests, and documentation.
