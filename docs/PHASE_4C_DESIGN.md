# Phase 4C prediction import and human review design

Status: implementation checkpoint, 9 October 2026.

## Existing architecture reused

The Phase 3 application stores corpus turns in `speaker_turns`, selects them into
project `tasks`, and gives users `assignments`. Human labels live in `annotations`;
every draft, submission, or revision appends an immutable `annotation_versions` row
and advances the annotation's current-version pointer. Users and project memberships
provide reviewer identity and authorization. Annotation schemas pin ordered field
definitions to versioned taxonomies; primary and secondary domains are values in the
human revision JSON. FastAPI routes, Jinja templates, CSRF checks, and service-level
assignment ownership checks form the existing review surface and audit convention.

## Phase 4C data model

Migration `20261009_06` adds three append-only relations without changing existing
human annotations:

- `model_prediction_runs` stores artifact identity, provider/model/prompt/schema,
  generation and Batch/cache configuration, record-set identity, lifecycle times,
  coverage, usage, pricing, and measured cost. `artifact_sha256` is unique.
- `model_predictions` links one immutable validated result to a run and
  `speaker_turns` row. A run may contain a turn once, while a later run may predict
  that same turn again. It stores the exact four-field prediction, usage, mapping,
  response identity, and artifact provenance.
- `model_prediction_reviews` is an append-only review event stream linking reviewer,
  prediction, turn, assignment, and (for accept/edit) the resulting normal human
  annotation version. Its latest event supplies queue state; history remains intact.

Database triggers reject updates and deletes on all three new tables. Foreign keys
use restrictive deletion. The downgrade is available only while all Phase 4C tables
are empty, preventing accidental loss of research records.

## Import and idempotency

`python -m hansard_annotator.llm_eval.import_predictions ARTIFACT` validates the
frozen Gemini 2.5 Flash / V2 / thinking-budget-zero / Batch provenance, canonical
schema hash, coverage invariants, every local prediction, and every `turn:<turn_key>`
mapping before insertion. The summary and predictions bytes form the artifact hash.
Unknown turns abort the transaction. Reimporting the same artifact returns the
existing run and inserts nothing. Run/turn and run/record uniqueness provides a
second database-level guard. The known schema-regression 500-record directory (or an
artifact carrying its regression marker) is refused explicitly.

Future Batch jobs and cache windows import as separate reproducible runs. This
supports incremental recovery without conflating predictions; a later campaign can
group run IDs externally or through run metadata without weakening immutable identity.

## Human review workflow

The assignment workspace shows speech text, a visibly unapproved AI suggestion,
compact provenance, and the existing human controls. “Accept AI” copies only the
policy/domain values into a normal submitted/revised human annotation and appends an
`accepted` review event referencing that version. A normal submission when a
prediction is present appends `edited`. Flag and skip append `flagged_codebook` and
`skipped` respectively; neither accepts or mutates the prediction. Raw provider
responses are deliberately absent from reviewer templates.

`/ai-reviews` uses predictions as its base set and provides unreviewed, reviewed,
accepted, edited, codebook-flagged, and skipped queues. Unreviewed means that no
review event exists; no placeholder event is created. For predictions with history,
the effective decision state is the event ordered last by `reviewed_at`, then
monotonically increasing review ID. Thus each prediction appears once per queue.
Codebook flags are orthogonal research metadata: the flagged queue uses existence of
any flag event, so a prediction may appear in both an accepted/edited queue and the
flagged queue. The service also accepts narrow run, predicted-domain, and
policy-status filters. A current user's assignment is joined only to supply an
authorized workspace link; its absence does not hide the prediction.

Human annotations remain the canonical human-label dataset. A human-confirmed AI
suggestion is distinguishable by its review event and annotation-version link; it is
never represented by changing a machine prediction.

## Codebook lifecycle

The frozen V2 production prompt and taxonomy do not change during review:

`V2 predictions -> human review -> flagged ambiguity queue -> offline codebook
analysis -> possible V3 -> separate validation -> optional new prediction run`.

No flag automatically adjudicates a label or changes the prompt.

## Cost projection

Full-corpus estimates now extrapolate only token-processing cost per valid record and
add the configured shared cache-storage lifetime once. Reports label this as a
mechanical estimate because a real production campaign may require multiple cache
lifetimes.
