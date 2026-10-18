# Phase 4E full-corpus orchestration

Phase 4E keeps `ai_annotation_campaigns` as the logical provenance and import unit while
persisting immutable child jobs in `ai_annotation_campaign_batches`. Existing rows default to
`orchestration_mode=LEGACY`; the Phase 4D single-job, recovery, repair, validation, import, and
review paths remain valid.

The production identities remain frozen: prompt
`a41027b001f10b61074c196996606c86b5dac4c0788ae185a05ff68fcf3b7cc6`, canonical
annotation schema `9c94bab9cd2976559c79b1c31991aef4138a2071a5d3b3d6186a26448f117126`,
prediction contract `99cafa6c473293dc076793ee6baeca44b6961d7cb11de8f9174517defc4a0879`,
and Gemini provider schema
`980b0ed6d6002a8eadd326f84942ba60dcdf8f2abd9e70ea6aaecf7d78de3c22`.

## Planning and authorization

Create a normal campaign and plan it with human exclusion cleared. Same-configuration
predictions remain excluded by the existing SQL predicate. `plan-chunks` streams the immutable
campaign ordering with a server-side cursor and uses `phase4e-full-input-v1` to estimate each
record from its actual UTF-8 prompt length plus the frozen invariant prefix. A chunk closes at
either the configured record limit or safe token limit. Boundaries are contiguous, hashes use
the ordered stable record IDs, and only ordinal ranges are stored—no second 160k-row mapping.

The defaults are controlled by:

- `HANSARD_GEMINI_ENQUEUED_TOKEN_CEILING=10000000`
- `HANSARD_GEMINI_ENQUEUED_TOKEN_SAFETY_FACTOR=0.90`
- `HANSARD_GEMINI_MAX_CHUNK_RECORDS=1000`

Planning and viewing never call Gemini. The admin must type `START FULL PRODUCTION` and approve
an immutable spend ceiling. Authorization records the user, time, record count, token ceiling,
safety factor, spend ceiling, and exact frozen configuration.

## Worker and quota safety

`hansard-campaign-worker --watch` repeatedly advances authorized campaigns one durable state at
a time. PostgreSQL campaign advisory locks and child-row locks serialize reservation. A child is
first reserved as `SUBMITTING`; the next tick searches Gemini by deterministic display name
before submitting, which closes the lost-receipt duplicate window. Quota/capacity refusal stores
a bounded event and returns the child to `PENDING`. Active estimated tokens include submitting,
submitted, running, and provider-completed jobs and may not exceed the configured ceiling times
the safety factor. Actual cost plus the next child's allocated estimate may not exceed the
authorization ceiling. Pause prevents new reservations without changing active jobs.

The worker verifies or replaces the existing canonical explicit cache before every submission.
Each child freezes the exact cache resource and metadata used; replacement for a later child
does not rewrite earlier provenance.

## Artifacts and completeness

Child artifacts are independently stored under `chunks/chunk-NNNNNN` with plan, provider
response, predictions, failures, and summary files. Campaign aggregation reads one bounded child
at a time, matches every response to the immutable campaign ordering, and writes `effective/`
with a manifest, predictions, operational summary, and structural-repair lineage. The only local
repair remains `remove_primary_from_secondary_v1`, followed by the unchanged strict validator.

Coverage reports original-valid, structurally-repaired, unresolved-invalid, provider-failed,
missing, and duplicate counts separately. Validation/import remain blocked unless record
identities reconcile and all planned predictions are effective-valid. The existing recovery
path operates on the aggregate unresolved set and retains separate paid-recovery provenance.

## Import memory and atomicity

The Phase 4C importer now hashes and validates JSONL incrementally, resolves turn IDs in batches
of 1,000, and inserts with bounded `executemany` batches. It deliberately performs two streaming
passes inside one database transaction: the first validates all records and mappings, and the
second creates one run and inserts predictions. Any failure rolls back the run and all rows, so
partially visible logical campaigns are impossible.

The production Compose stack includes a hardened worker using the same read-only filesystem,
dropped capabilities, non-root user, PostgreSQL network, and campaign-artifact volume as the web
service. With no authorized campaigns it performs no provider operation.
