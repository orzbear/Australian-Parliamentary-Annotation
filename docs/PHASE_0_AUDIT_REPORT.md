# Phase 0 Audit Report

Audit date: 23 July 2026  
Scope: repository safeguards, read-only corpus inventory/schema audit, synthetic fixtures,
tests, and approved provenance amendment only.

## Outcome

Phase 0 is complete subject to owner review. The repository is initialized for Git, the
raw archive is ignored, the corpus audit is deterministic and read-only, and synthetic
fixtures cover the approved structural edge cases. No Phase 1 parser, database schema,
web application, service installation, or deployment was created.

## Repository safeguards

- Git was initialized in the workspace.
- `.gitignore` excludes `/hansard_xml_files/`, secrets, database files/volumes/dumps,
  exports, generated data, Parquet/CSV/JSONL outputs, caches, and local environments.
- `README.md` states the project phase and raw-data safety invariants.
- `CONTRIBUTING.md` defines approval gates, immutable-source rules, fixture restrictions,
  and review checks.
- The only XML files added under `tests/` are small, explicitly synthetic fixtures.

## Audit method

Run:

```powershell
python tools/corpus_audit.py hansard_xml_files --json
```

The Python-standard-library command:

1. resolves a configured corpus root;
2. walks without following symlink directories or files;
3. sorts by normalised relative POSIX path;
4. validates `YYYY/YYYY-MM-DD.xml` and the calendar date;
5. streams SHA-256 for every file;
6. rejects `DOCTYPE`;
7. uses streaming XML events for structural counts only;
8. catches per-file XML failures and continues;
9. compares size and nanosecond modification time before/after each read;
10. emits JSON or a concise summary to standard output only.

It is an audit utility, not the Phase 1 production parser: it does not extract database
records, clean text, reconstruct turns, link interjections, or write reports/data files.

The deterministic schema-drift sample is the first, middle, and last filename in each
year (48 files). Exact structural totals come from all files.

## Exact corpus results

| Measure | Exact result |
|---|---:|
| XML files | 926 |
| Total bytes | 784,583,349 |
| Years | 2010–2025 |
| Valid folder/filename/calendar dates | 926 |
| Path/date validation errors | 0 |
| XML documents parsed | 926 |
| Malformed/unsafe XML files | 0 |
| `<debates>` roots | 926 |
| Speech fragments | 269,793 |
| `talktype="speech"` | 156,123 |
| `talktype="continuation"` | 38,925 |
| `talktype="interjection"` | 74,745 |
| Major headings | 19,973 |
| Minor headings | 78,002 |
| Divisions | 2,848 |
| `<bills>` elements, all depths | 13,087 |
| Top-level `<bills>` elements | 11,504 |
| Bill records | 16,759 |
| Duplicate non-empty speech source IDs | 0 |
| Missing `speakerid` | 1,738 |
| Missing `speakername` | 1,738 |
| Explicit `nospeaker` attribute present | 1,738 |
| Source mutation detected by audit | 0 |

The corpus inventory checksum is:

```text
SHA-256 7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2
```

It hashes, in deterministic discovery order, each relative path, byte length, and
file SHA-256. Two complete audits produced the same value, file count, and byte count.

### Files and bytes by year

| Year | Files | Bytes |
|---:|---:|---:|
| 2010 | 55 | 48,793,343 |
| 2011 | 64 | 60,404,350 |
| 2012 | 63 | 59,820,280 |
| 2013 | 48 | 42,815,601 |
| 2014 | 76 | 62,629,659 |
| 2015 | 75 | 61,936,329 |
| 2016 | 51 | 41,538,663 |
| 2017 | 64 | 55,272,338 |
| 2018 | 65 | 53,271,446 |
| 2019 | 45 | 36,429,681 |
| 2020 | 58 | 46,685,099 |
| 2021 | 67 | 52,275,875 |
| 2022 | 38 | 32,237,814 |
| 2023 | 65 | 55,151,961 |
| 2024 | 61 | 53,337,478 |
| 2025 | 31 | 21,983,432 |

### Time and markup findings across the full corpus

| Time representation | Fragments |
|---|---:|
| `HH:MM` | 244,933 |
| `HH:MM:SS` | 20,442 |
| `unknown` | 2,638 |
| Empty | 1,207 |
| Other malformed/nonstandard | 573 |

All observed speech attribute names are:

`approximate_duration`, `approximate_wordcount`, `id`, `nospeaker`, `speakerid`,
`speakername`, `talktype`, `time`, and `url`.

Observed descendants inside speech include `p` (1,267,221), `i` (108,743), `ul`
(7,905), `li` (1,178), `b` (3,589), `a` (2,518), `dl` (1,286), `dt`/`dd`
(4,701 each), `table` (45), `tr` (1,039), `td` (3,370), `sup` (113), `inline`
(65), and `img` (4). Phase 1 must therefore preserve descendant text generically and
must not assume only paragraphs and italics.

`<bills>` is not exclusively top-level: at least one observed nested form occurs inside
a division. Phase 1 must preserve structural position rather than flattening every bills
container into a top-level event.

## Sampled schema drift

The 48-file first/middle/last-per-year sample confirmed:

- 2010 predominantly uses `HH:MM:SS`; later years predominantly use `HH:MM`.
- `nospeaker` is sparse and historical: 79 sampled 2010 fragments, eight in 2011, one in
  2012, and none in sampled 2013–2025 files. Absence must not be read as an explicit
  false value.
- Missing speaker ID/name follows the same early-year concentration in the sample.
- Empty/nonstandard times occur in multiple years, particularly the sampled 2011, 2015,
  and 2020 files.
- Early files use richer structures including links, bold text, definition lists,
  tables, superscripts, and inline tags; sampled 2013–2025 files mainly use paragraphs,
  italics, and unordered lists.
- Every sampled year contains all three talk types somewhere in its three selected files.
- Collective interruptions can be encoded as `talktype="speech"` with an unknown speaker
  and explicit “members interjecting” language.
- Some `continuation` fragments have no safe prior fragment for the same speaker.
- Heading changes, divisions, bills records, empty minor headings, unknown speakers, and
  presiding/collective contributions all occur and require explicit handling.

These findings describe the deterministic sample. Exact whole-corpus counts above should
not be mistaken for completed Phase 1 semantic reconstruction.

## Synthetic fixtures

Fixtures cover:

- ordinary speeches and multiple/nested blocks;
- continuations;
- named interjections;
- a strict collective-interruption pattern;
- an orphan continuation;
- a heading change;
- unknown and `nospeaker` records;
- a division and vote member;
- a bills container/bill;
- `HH:MM`, `HH:MM:SS`, `unknown`, and empty time values;
- intentionally malformed XML in a separate corpus.

Each fixture is under 20 KB and explicitly states that it is synthetic.

## Test results

Command:

```powershell
python -m unittest discover -s tests -v
```

Result: **8 tests passed**.

Verified:

- audit reads do not alter fixture size, modification time, or SHA-256;
- two audits return the same inventory checksum;
- discovery order is deterministic;
- valid, mismatched, malformed, and impossible folder/filename dates are handled;
- file SHA-256 is reproducible;
- malformed XML is recorded without raising or stopping the audit;
- fixtures contain every required approved edge category;
- fixtures are small and synthetic;
- Git ignores a real path below `hansard_xml_files/`.

Two complete real-corpus audits returned the same inventory checksum and reported zero
source mutations.

## Lean provenance recommendation

The architecture amendment is accepted and incorporated into both decision documents.
The recommended Phase 1 representation is:

- immutable original XML outside Git and mounted/read as source;
- logical source path plus immutable source-file SHA-256/version;
- fragment source ID where present and deterministic source sequence always;
- original attribute map;
- raw extracted text and ordered block boundaries;
- conservative clean text;
- optional deterministic fragment-projection checksum.

The projection checksum should hash a version tag plus a length-delimited encoding of
source-file version, fragment sequence, sorted original attributes, and ordered raw text
blocks. It is a drift/reconciliation aid, not a replacement for the source-file hash.

Full canonical fragment XML should not be stored in PostgreSQL by default. If a future
test demonstrates that source file/version + sequence + attributes + raw text cannot
reproduce or diagnose a material anomaly, retain only the affected serialisation as a
hashed, access-controlled debugging artefact outside PostgreSQL (or in a bounded anomaly
field), with a reason and retention policy.

## Remaining decisions before Phase 1

1. Confirm that the archive is exclusively federal House of Representatives Hansard,
   and approve its provenance/licence/canonical-source statement.
2. Approve the initial strict collective-speaker allowlist and explicit interruption
   patterns as a versioned configuration. The concept is approved; the exact entries
   still require research-owner review.
3. Approve the proposed fragment-projection checksum definition and confirm that
   canonical fragment XML remains off by default.
4. Decide the Phase 1 quality gate for future malformed/unsafe files: recommended is to
   continue the run, fail the affected file, and mark the overall run
   `completed_with_errors`, while prohibiting project import from failed files.
5. Confirm handling for the four observed `<img>` elements. Recommended: preserve their
   original `src` attributes and insert a non-semantic block placeholder, but do not
   fetch remote images during import.
6. Review and accept this Phase 0 audit as the baseline against which Phase 1 will
   reconcile exact counts.

Until these are resolved and Phase 1 is explicitly approved, production parser work must
not begin.
