# Phase 2.6 text-integrity investigation

Status: investigation accepted and narrowed Phase 2.6 implementation approved,
24 July 2026. No cleaner or parser change is warranted.

## Baseline

HEAD is commit `709a9c75f91fde3c131b6b0b9823562e550b92d5`, tagged
`phase-2.5-approved`. The tracked tree is unchanged. The working tree did not literally
start clean because a pre-existing LibreOffice lock file,
`data/review/.~lock.turn_sample.csv#`, is untracked; it was preserved and was not used as
source evidence.

No raw XML, accepted Phase 1 output, database row, migration, taxonomy, or schema was
modified during this investigation.

## Finding: the reported preprocessing whitespace loss is not present

The specified raw XML, accepted Parquet, and PostgreSQL records all contain the expected
spaces. Neither accepted field contains the reported joined forms.

| Evidence | Expected forms present | Alleged forms present |
|---|---|---|
| XML ID `uk.org.publicwhip/debate/2010-06-03.113.17` | `in any party room`; `an impost` | none |
| XML ID `uk.org.publicwhip/debate/2010-06-03.113.27` | `Madam Deputy Speaker, I withdraw` | none |
| Accepted fragment Parquet, sequences 388 and 390 | all three expected forms | none |
| Accepted speaker turn `abaebade…e4ca` | all three expected forms | none |
| PostgreSQL fragments and turns | all three expected forms | zero rows with alleged forms |

The known turn remains David John Bradbury, has four lineage rows (one initial and three
continuations), three separately linked interjections, interruption count three, and
2,458 calculated words. No interjection text appears in the turn.

## Actual extraction operation

The tagged implementation is `extract_speech_text()` in
`src/hansard_annotator/corpus/text_cleaning.py`.

For each block, `_render_inline()`:

1. appends `element.text` unchanged;
2. recursively appends each child's text in document order;
3. appends each `child.tail` unchanged;
4. uses `"".join(pieces)` without inserting or removing boundary characters.

Only after complete block assembly does `raw_whitespace()` normalise Unicode/newline
encoding and trim the outer block edges. `clean_whitespace()` normalises Unicode and
collapses whitespace once for the assembled block. Completed blocks are joined with
two newline characters; table cells use one tab.

The implementation does **not** strip individual text/tail/inline nodes and does not
join nodes with invented spaces. Therefore:

- `text_raw` preserves significant internal source whitespace and document order;
- `text_clean` preserves token boundaries while consistently collapsing block-internal
  whitespace;
- inline nodes do not force spaces;
- source spaces and punctuation adjacency determine inline boundaries;
- images add an alt-text or `[IMAGE]` replacement only to clean text;
- paragraph/list/definition/table boundaries are explicit.

There is no identified function or operation in the accepted cleaner that produces the
three reported losses. Both `text_raw` and `text_clean` are correct for the nominated
records.

## Corpus-wide audit

A read-only audit reparsed all 926 immutable XML files with the tagged cleaner and
compared candidate fragments against accepted Parquet by:

- source-file SHA-256;
- source relative path;
- top-level sequence number;
- source fragment ID.

Results:

- fragments compared: **269,793**;
- candidate-only fragments: **0**;
- accepted-only fragments: **0**;
- raw-text differences: **0**;
- clean-text differences: **0**;
- word-count differences: **0**;
- whitespace-only corrections: **0**;
- punctuation-spacing corrections: **0**;
- Unicode-normalisation-only changes: **0**;
- block-boundary changes: **0**;
- non-whitespace or unexpected changes: **0**;
- years affected: **none**.

This comparison did not rely on a joined-token heuristic: source XML was reparsed, and
stable source identity—not generated fragment keys—was used. Known records were also
checked directly at the XML text/tail level.

## Why existing tests did not settle the review concern

Existing Phase 1 tests already cover nested formatting, paragraphs, lists, tables,
images, and interjection separation, and the accepted outputs demonstrate correct
ordinary spaces. They do not enumerate every Phase 2.6 example as a dedicated unit
matrix, nor do they test a supported review-export command because none exists yet.
Adding the explicit matrix remains useful, but it would lock in current correct behavior
rather than expose or justify a cleaner correction.

## Confirmed export defect

The pre-existing untracked `data/review/turn_sample.csv` has a UTF-8 BOM and parses as
30 rows with 14 columns, including embedded multiline text, but some text has already
been incorrectly transcoded. For the Flynn row the file contains:

```text
CQ + U+0393 + U+00C7 + U+00F6 + projects
```

PostgreSQL contains the correct:

```text
CQ + U+2014 + projects
```

This confirms an unsafe console/PowerShell export path and confirms that PostgreSQL is
not corrupt. Encoding conversion can corrupt Unicode, but it does not explain deletion
of the ordinary ASCII spaces in the three reported examples. The exact artifact or
command that displayed those joined forms is not present in the repository.

The apparent joined words were caused by terminal wrapping/copying or visual
interpretation, not by accepted corpus data. The supported review export therefore
writes CSV directly with Python and never routes speech text through a PowerShell text
pipeline.

## Potential unwanted-space risk

A speculative replacement such as `" ".join(element.itertext())` would introduce spaces
before punctuation and split intentionally adjacent inline characters. Per-node
`.strip()` would lose source boundary spaces. The accepted implementation does neither.
Changing it without a failing source/processed pair would create precisely the risks
Phase 2.6 is intended to prevent.

## Versioning and database risks

Incrementing `PIPELINE_VERSION` changes deterministic fragment, turn, lineage,
interjection, anomaly, section, event, and image keys even when text is identical. A new
run with no semantic correction would:

- manufacture a new corpus version without an identified method change;
- change record/projection identities merely because of the version discriminator;
- consume substantial Parquet/PostgreSQL storage;
- complicate current/all-run view work without correcting any text;
- misstate the provenance of a supposed hotfix.

The old run remains independently reproducible because source and output checksums are
unchanged. A legitimate corrected run can coexist by using a new pipeline version and
new deterministic keys, but only after an actual failing extraction case and approved
algorithmic change exist.

## Proposed algorithm

Retain the existing document-order algorithm unless a reproducible counterexample is
supplied. For any real counterexample:

1. capture the minimal authorised XML element with exact `text` and `tail` values;
2. prove the mismatch in both accepted fields;
3. add a failing regression;
4. assemble text/children/tails in document order;
5. normalise only after full block assembly;
6. verify punctuation and adjacency without inserting spaces;
7. run the stable-identity corpus audit and reject unexpected non-whitespace changes.

## Accepted decision

The owner approved the evidence-based narrowed scope: safe review export,
version-aware views, structural-hint presentation, and review sampling. The full
stable-source-identity reparse found zero differences across all **269,793** fragments.
Creating a new preprocessing run would provide no correction and would unnecessarily
change version-derived identifiers.

The accepted `phase1-full-20260723-v4` preprocessing run therefore remains
authoritative. No speculative cleaner change, parser change, pipeline version, new
processed run, corpus import, duplicate corpus row, or current-run pointer change is
part of Phase 2.6.

## Structural-hint policy for Phase 3

`is_procedural`, `is_ceremonial`, `is_question`, `is_answer`, and
`is_question_time` are preprocessing-derived contextual hints. Phase 3 must not treat
them as gold labels, automatically submit a human `content_status` from them, convert
unknown to false, or hide the rule/configuration provenance that produced them.
