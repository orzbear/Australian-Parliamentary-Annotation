# Phase 2.6 annotation-readiness report

Date: 24 July 2026  
Scope: narrowed Phase 2.6 only

## Outcome

The suspected preprocessing whitespace defect was not reproducible. Immutable raw XML,
accepted Parquet, and PostgreSQL all contain the correct text. A stable-source-identity
reparse compared all **269,793** fragments and found zero raw-text, clean-text,
word-count, whitespace-only, Unicode-normalisation, block-boundary, or non-whitespace
differences.

The apparent joined words resulted from terminal wrapping/copying or visual
interpretation, not accepted corpus data. The separate review-file mojibake came from
an unsafe console/PowerShell transcoding path: the old file contained incorrect
U+0393/U+00C7/U+00F6 characters where PostgreSQL held U+2014. The exact historical
command was not retained, but database and source corruption were excluded.

No cleaner or parser was changed. No pipeline version, processed corpus run,
preprocessing database row, duplicate corpus row, or current-run pointer was created or
changed. `phase1-full-20260723-v4`, pipeline `1.0.0`, remains authoritative.

## Supported review export

`python -m hansard_annotator.db.cli export-review-sample` now:

- selects `current_annotation_ready_turns`;
- writes standards-compliant CSV directly in Python as UTF-8, with a BOM when
  `--excel-compatible` is selected;
- puts `text_clean` last and preserves commas, quotes, paragraph breaks, curly
  punctuation, ellipses, en dashes, and em dashes;
- samples deterministically using a supplied seed and a stable turn-key tie-breaker;
- excludes orphan continuations by default and supports `--include-orphans`;
- supports year, date-range, minimum/maximum-word, question-time-hint,
  procedural-hint, ceremonial-hint, and minimum-interruption filters;
- derives `interrupted` from `interruption_count > 0`;
- exports nullable hints as `true`, `false`, or `unknown`;
- uses only parameterised, application-defined SQL;
- refuses existing files and paths under raw XML, configured processed data, repository
  processed data, or backups;
- creates permitted parent review directories and reports no speech text or secrets.

`data/review/` and workspace test scratch data are ignored by Git.

## Structural-hint policy

The immutable columns `is_procedural`, `is_ceremonial`, `is_question`, `is_answer`, and
`is_question_time` remain unchanged. Export presentation maps the first two to
`procedural_hint` and `ceremonial_hint`.

These are preprocessing-derived contextual hints, not gold or human research labels.
Phase 3 must not automatically create a human `content_status` from them, convert
unknown to false, or hide their rule/configuration provenance.

## Sampling defaults

The default is the current corpus run, non-orphan turns, minimum 50 words, no maximum
word threshold, no hint/date/year/interruption restriction, and a 30-row limit.
Supplying a seed makes selection deterministic. These are review-export choices, not
permanent research exclusions.

## Version-aware views

Alembic revision `20260724_03` was added; accepted revisions `20260723_01` and
`20260724_02` were not edited.

- `annotation_ready_turns` preserves its existing columns and adds
  `preprocessing_run_id`, `pipeline_version`, and `is_current_corpus_version`.
- `current_annotation_ready_turns` selects only rows whose run matches
  `corpora.current_preprocessing_run_id`.
- Historical rows remain queryable from `annotation_ready_turns` with explicit run
  identity; a two-run PostgreSQL test confirms historical and current rows do not
  silently duplicate in the current view.
- The migration downgrades to the exact Phase 2.5 view shape.

The development database advanced from `20260724_02` to `20260724_03`. Its current
corpus pointer remains `phase1-full-20260723-v4`; it still contains exactly one
preprocessing run.

## CSV acceptance

Generated ignored artifact:
`data/review/turn_sample_utf8.csv`

Parameters: 30 rows, 50–1,200 words, seed `20260724`, Excel-compatible.

Programmatic read-back confirmed:

- UTF-8 BOM present;
- 30 data rows and 19 columns;
- every parsed row has exactly 19 fields;
- all turn keys are unique valid 64-character lowercase hexadecimal keys;
- source-file dates match speech dates;
- `source_file` and `text_clean` remain separate;
- embedded paragraph breaks and Unicode punctuation survive;
- no known mojibake sequence occurs;
- no orphan continuation is present.

The real PostgreSQL Flynn export test preserves `CQ—projects`; the dash is U+2014.
The test matrix also round-trips curly quotation marks/apostrophes, an en dash, an em
dash, an ellipsis, commas, literal quotes, and a two-paragraph field.

The David John Bradbury turn
`abaebade6746a8d146b3970ec6e3ef76f03a823b5b6720b17ea67ebfdcb1e4ca`
still has source `2010/2010-06-03.xml`, speech text in `text_clean`, and the exact forms
`in any party room`, `an impost`, and `Madam Deputy Speaker, I withdraw`. Its
interruption count is three and all three interjections remain separately linked.

## Verification

- Full pytest suite against PostgreSQL 16.9: **44 passed in 23.46 s**.
- Focused export and PostgreSQL suite: **16 passed in 28.53 s**.
- Ruff: **all checks passed**.
- strict mypy: **success, 42 source files**.
- Phase 2 verification: **OK** with 926 source files, 269,793 fragments, 160,577
  turns, 191,263 lineage rows, 78,530 interjections, and no invariant failures.
- Phase 2.5 product verification: **OK**; expected taxonomy/schema seed counts remain
  valid and no Phase 3 tables exist.
- Export row-count checks confirmed no preprocessing, turn, or fragment row creation.
- Deterministic same-seed samples were byte-identical; distinct seeds produced distinct
  valid samples.
- Default orphan exclusion, explicit orphan inclusion, nullable hint output, unsafe
  path rejection, and current-run selection passed.

## Immutability and phase boundary

The raw audit still reports **926 files**, **784,583,349 bytes**, and inventory SHA-256
`7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2`.
No raw XML was modified.

The accepted Phase 1 manifest SHA-256 remains
`1d7698829d9d1cdceb0373c5d2fc55a719f2a43e6d5242e38bd99067215680df`.
No accepted Phase 1 output was modified.

All taxonomy and annotation-schema versions remain `draft`. Phase 3 was not started.
