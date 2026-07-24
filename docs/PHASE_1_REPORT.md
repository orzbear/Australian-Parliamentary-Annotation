# Phase 1 Preprocessing Report

Completion date: 23 July 2026  
Accepted scope: preprocessing only. Phase 2 was not started.

## Outcome

Phase 1 passed its full-corpus acceptance gate. The pipeline discovered, hashed, parsed,
reconstructed, reconciled, and wrote all 926 files without modifying the source archive.
All Phase 0 baseline counts and the inventory SHA-256 match exactly. Every generated
output checksum in the manifest was independently verified.

Canonical accepted run:

```text
data/processed/phase1-full-20260723-v4/
```

Generated data are ignored by Git and are not intended for commit.

## Package architecture

The package lives under `src/hansard_annotator/corpus/`:

| Module | Responsibility |
|---|---|
| `config.py` | Typed versioned configuration and hashes |
| `discovery.py` | Safe deterministic scope selection, path/date validation, streamed SHA-256, prior-manifest comparison |
| `models.py` | Typed dataclasses for all core record types |
| `hashing.py` | NFC-normalised canonical JSON, projection checksums, stable keys |
| `parser.py` | lxml file-atomic streaming parser, safety ceilings, headings/fragments/events |
| `text_cleaning.py` | Raw/clean block extraction, inline/list/table/image handling, word counts |
| `business_types.py` | Versioned exact heading mapping |
| `reconstruction.py` | Conservative turn reconstruction and separate interjections |
| `reconciliation.py` | Per-file source/output invariants |
| `parquet.py` | Explicit PyArrow schemas and bounded year-partitioned writing |
| `reports.py` | CSV quality/anomaly reports and counters |
| `pipeline.py` | Run lifecycle, acceptance gate, manifest, performance evidence |
| `cli.py` | Whole corpus/year/file/sample/changed-file interface |

No database, migration, web, Docker, or deployment module was added.

## Safety and reproducibility

- lxml uses `resolve_entities=False`, `load_dtd=False`, `no_network=True`,
  `recover=False`, and `huge_tree=False`.
- `DOCTYPE` is rejected before parsing.
- Configurable ceilings cover bytes, elements, nesting depth, and individual text nodes.
- A failed file exposes no partial records; later files continue.
- Every source file is hashed before processing and rehashed afterward. Size and
  nanosecond modification time are also compared.
- Stable keys include record kind, pipeline version, file SHA-256, source sequence, and
  a deterministic discriminator.
- Fragment projections hash approved NFC-normalised canonical JSON with sorted keys and
  no timestamps or environment paths.
- Full canonical fragment XML is not stored.
- Parquet schemas are explicit and year buffers flush when a year completes.
- Completed matching run IDs are reused; mismatched/incomplete output is not overwritten.

## Validation commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests tools
.\.venv\Scripts\mypy.exe src
```

Results:

- pytest: **21 passed**
- Ruff: **all checks passed**
- mypy strict: **no issues in 15 source files**

The tests cover discovery/order/scope, folder/filename dates, changed-file selection,
ordinary/multi-paragraph/nested/list/table speech, images with/without alt, multiple
interjections, strict collective interruption, a non-preserving “Hear, hear!” response,
orphan/boundary/identity continuations, unknown and `nospeaker` records, malformed
time/count, duplicate IDs, empty speech, malformed XML, DOCTYPE/entity input, divisions,
bills, unknown events, projection determinism, Parquet schemas, idempotent reruns,
interjection exclusion, continuation reconciliation, and the malformed-file quality gate.
Changed-file mode also has an explicit successful no-op test when a prior manifest shows
that no inputs changed.

## Real-data staging runs

The 16-file deterministic cross-year sample completed with:

- 4,598 fragments;
- 2,574 turns;
- 1,441 stored interjections;
- 583 merged continuations;
- 48 orphan continuations;
- zero failed/unreconciled files;
- zero duplicate IDs or source-URL marker exceptions.

The two image-bearing files (`2010-05-26` and `2010-06-03`) were run and manually
reviewed separately before full acceptance.

## Exact full-corpus records

| Dataset | Records |
|---|---:|
| Source files | 926 |
| Debate sections | 97,975 |
| Debate events | 14,352 |
| Speech fragments | 269,793 |
| Speaker turns | 160,577 |
| Turn-fragment lineage | 191,263 |
| Stored interjections | 78,530 |
| Continuation anomalies | 8,239 |
| Image anomalies | 4 |

Debate events consist of 11,504 top-level bills containers and 2,848 divisions. No
unknown top-level event occurred in the real corpus. Nested bills remain inside their
parent event payload; the exact corpus contains 13,087 bills containers at all depths
and 16,759 individual bill records.

## Phase 0 reconciliation

Every accepted baseline measure matched:

| Measure | Phase 0 | Phase 1 | Match |
|---|---:|---:|---|
| Files | 926 | 926 | Yes |
| Bytes | 784,583,349 | 784,583,349 | Yes |
| Inventory SHA-256 | `7121a0…144e2` | `7121a0…144e2` | Yes |
| Speech fragments | 269,793 | 269,793 | Yes |
| `speech` | 156,123 | 156,123 | Yes |
| `continuation` | 38,925 | 38,925 | Yes |
| `interjection` | 74,745 | 74,745 | Yes |
| Major headings | 19,973 | 19,973 | Yes |
| Minor headings | 78,002 | 78,002 | Yes |
| Divisions | 2,848 | 2,848 | Yes |
| Bill records | 16,759 | 16,759 | Yes |

All 926 files have terminal `completed` and reconciled statuses. There are zero malformed
files, partial files, reconciliation failures, duplicate non-empty IDs, or source URL
dataset-marker exceptions.

## Continuations and interjections

Of 38,925 continuations:

- 30,686 (78.84%) merged under approved identity/boundary rules;
- 8,239 (21.16%) remained standalone orphan turns.

Turn lineage reconciles exactly:

```text
160,577 initial/orphan turns + 30,686 merged continuations
= 191,263 turn-fragment rows
```

The output contains 78,530 interjection records:

- all 74,745 source `talktype="interjection"` fragments remain separate;
- 3,785 allowlisted collective `talktype="speech"` fragments matched an explicit
  interjection pattern and were also treated as separate interjections;
- 3,773 collective exceptions preserved and linked to an active turn;
- 12 matched collective records had no active turn and therefore preserved nothing;
- 77,562 interjections linked to an interrupted turn;
- 968 remained unlinked with an explicit reason.

No interjection fragment has turn lineage, and no interjection text is inserted into
speaker-turn text.

## Image review

Exactly four real `<img>` elements occur, two in `2010/2010-05-26.xml` sequence 554 and
two in `2010/2010-06-03.xml` sequence 384. Manual review confirmed:

- all four have a `src` attribute and no alt text;
- all attributes appear in `image_elements.csv` and block metadata;
- each image produces one literal `[IMAGE]` block placeholder;
- the surrounding text order is preserved;
- no remote image was fetched and no content was inferred.

## Unresolved anomalies

These are retained outputs, not acceptance failures:

### Continuations

| Reason | Count |
|---|---:|
| Known speaker IDs differ | 7,050 |
| Identity missing or ambiguous | 1,089 |
| No active candidate | 97 |
| Division boundary | 3 |

The known-ID category is concentrated in 2011 (4,504) and 2012 (1,085). Manual inspection
shows sequences where alternating named participants—including presiding-officer and
main-speaker fragments—are all labelled `continuation`. Merging across those different
known IDs would violate the approved rule, so the pipeline correctly leaves them
standalone. This requires future research review, not a silent Phase 1 heuristic.

### Business headings

The exact mapping intentionally leaves 547 distinct major headings unmapped, affecting
57,674 fragments. The largest are `STATEMENTS BY MEMBERS` (20,374), `CONSTITUENCY
STATEMENTS` (10,953), `PERSONAL EXPLANATIONS` (3,538), `GRIEVANCE DEBATE` (1,920), and
`STATEMENTS ON INDULGENCE` (1,642). They remain original, warned, and listed for a future
reviewed mapping version.

### Missing/malformed source fields

- 1,738 fragments lack speaker ID and name.
- 3,845 times are missing/empty/`unknown`.
- 573 times are malformed/nonstandard.
- 7 speech fragments have empty clean text.
- 4 image elements have no alt text and use `[IMAGE]`.

No missing/malformed field was invented or silently coerced.

### Explicitly unknown Phase 1 flags

Historical speaker roles are unavailable, so `is_presiding_officer` remains unknown.
`is_question` and `is_answer` also remain unknown rather than being inferred from
alternation. `eligible_main_analysis` remains unknown when procedural/ceremonial mapping
is unknown. Speaker collectivity is also null when an unknown speaker does not match the
reviewed collective allowlist; it is not silently classified as an individual. Political
enrichment remains a later phase.

## Performance and output integrity

Accepted run measurements:

- processing duration: **99.262 seconds**;
- peak resident memory: **462,327,808 bytes** (about 440.9 MiB);
- generated files covered by manifest checksums: **259**;
- generated bytes excluding manifest: **1,137,990,532**;
- independently recomputed output checksum mismatches: **0**;
- leftover temporary files: **0**.

Repeating the accepted full command with the same run ID returned
`reused_existing_run=true` and did not rewrite output.

An initial full run retained underfilled partition buffers across years and reached
1.72 GB RSS. Year-boundary flushing reduced peak RSS by about 73% without changing any
record count. The corrected run is the accepted result.

## Raw-source verification

A fresh post-run Phase 0 audit returned:

- 926 files;
- 784,583,349 bytes;
- inventory SHA-256
  `7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2`;
- zero detected source mutations.

The preprocessing process also compared each file's SHA-256, size, and modification time
before and after processing. No source file changed.

## Phase boundary

Phase 2 was not started. No PostgreSQL schema/migration, web dashboard, Docker service,
deployment, authentication, annotation workflow, LLM worker, or Telegram/Hermes client
was implemented.
