# Phase 4D AI annotation campaigns

Status: implementation checkpoint, 13 October 2026.

## Purpose and boundaries

Phase 4D makes the validated Phase 4A–4C model workflow operable through the existing
FastAPI/Jinja application. It adds persistent campaign planning and recovery without
replacing the provider-independent evaluation, Gemini Batch, prediction import, or AI
Review implementations. It does not launch continuous annotation, introduce Redis, edit
the codebook, or select a V3 prompt.

## Approved production configuration

Paid web submission is fail-closed to this exact identity:

- provider/model: `gemini` / `gemini-3.1-flash-lite`;
- prompt: `phase4a-policy-v2`;
- prompt SHA-256: `a41027b001f10b61074c196996606c86b5dac4c0788ae185a05ff68fcf3b7cc6`;
- canonical annotation schema SHA-256:
  `9c94bab9cd2976559c79b1c31991aef4138a2071a5d3b3d6186a26448f117126`;
- prediction contract SHA-256:
  `99cafa6c473293dc076793ee6baeca44b6961d7cb11de8f9174517defc4a0879`;
- Gemini Batch schema SHA-256:
  `980b0ed6d6002a8eadd326f84942ba60dcdf8f2abd9e70ea6aaecf7d78de3c22`;
- thinking level `minimal`, Batch execution, and an explicit verified Gemini cache.

The UI labels this `APPROVED PRODUCTION CONFIGURATION`. A configuration differing in
any material field is classified as `RESEARCH`; the present web workflow does not submit
research configurations. V2 and its hashes are read-only.

### Model-selection decision

The historical evaluation baseline remains Gemini 2.5 Flash with frozen V2 and
`thinking_budget=0`. Newly planned Phase 4D campaigns use Gemini 3.1 Flash-Lite with the
same frozen V2 prompt and `thinking_level=minimal`. Historical campaign and prediction-run
rows retain their stored model, thinking configuration, costs, cache identity, and provider
provenance.

The frozen 120-record model-comparison benchmark produced the following original-reference
results; it was previously used for model comparison and is not a pristine untouched holdout:

| Model | Policy F1 | Primary exact | Primary macro-F1 | Normalized Batch projection |
|---|---:|---:|---:|---:|
| Gemini 2.5 Flash | 97.56% | 85.44% | 77.29% | $57.18 |
| Gemini 3.1 Flash-Lite | 98.02% | 85.44% | 74.33% | $36.46 |

The normalized estimate is approximately 36.24% lower for Flash-Lite. A separate post-hoc
targeted review of its five new disagreements found one likely genuine regression, two likely
Flash-Lite corrections, two ambiguous cases, and no systematic new failure mode. These
judgements do not replace or adjust the historical benchmark metrics.

## Persistent model

Migration `20261013_07` adds:

- `ai_annotation_campaigns`, which stores creator, corpus, configuration identity,
  selection definition, aggregate eligibility counts, record-set hash, token/cost plan,
  provider/cache/job identity, lifecycle timestamps, validation counts, failure metadata,
  artifact identity, and the final `model_prediction_runs` link;
- `ai_annotation_campaign_records`, which pins each selected speaker turn, stable record
  ID, deterministic order, and record hash. Unique constraints prohibit duplicate turns,
  record IDs, and order positions inside one campaign.

Campaign rows are operational records and therefore update as the lifecycle advances.
A database trigger freezes configuration, selection, job identity, and final import link
after submission. Selected records are append-only. Restrictive foreign keys preserve
corpus, user, turn, and prediction-run provenance. Downgrade refuses to destroy campaign
data.

Migration `20261017_08` adds append-only targeted-recovery lineage. One recovery-attempt
row owns its deterministic unresolved-set hash, attempt number, provider/cache identity,
cost telemetry, and separate artifact references. Recovery-record rows pin the campaign
record, original request index, and original validation error. A partial unique index permits
only one active attempt for the same unresolved set, while a failed or still-invalid collected
attempt can be followed by a new numbered attempt.

## State machine

The service—not route handlers—owns transitions:

```text
DRAFT -> PLANNED -> READY -> SUBMITTED -> RUNNING
  -> PROVIDER_COMPLETED -> COLLECTED -> VALIDATED -> IMPORTED
```

`FAILED` and `CANCELLED` are explicit. Recovery transitions are narrowly allowed from a
failed provider/collection/validation operation, while imported and cancelled campaigns
are terminal. Browser refreshes do not assign state directly.

## Configuration-aware eligibility

Planning reads turns from the selected corpus's current preprocessing run. AI eligibility is
content-neutral: a turn is usable when the existing prompt mapper can obtain non-blank cleaned
or raw speech text. The legacy `eligible_main_analysis` field must not gate AI campaigns because
it encodes length, speaker, business-type, procedural, ceremonial, and reconstruction rules that
would pre-filter the Policy/Non-Policy task. A turn is also excluded when an immutable prediction
already exists for the same provider, model, prompt version/hash, canonical schema, prediction
contract, provider schema, generation configuration, and execution mode. Thus the existing 100
identical V2 predictions are excluded, while a genuinely different future model or configuration
may annotate the same turn.

Human annotation exclusion is optional and defaults on. It is stored in the selection
definition rather than treated as a global prohibition. Selected turns are ordered by
`SHA256(turn_key + ':' + seed), turn_key`; the ordered stable record IDs produce the
record-set SHA-256. Database uniqueness provides a second duplicate barrier.

The selection definition stores the eligibility-rule identifier and a deterministic breakdown
of every other exclusion. Campaigns planned with the superseded main-analysis rule are displayed
for audit but are blocked before cache creation or paid submission; they must be replaced with a
new plan rather than silently changing their frozen record set.

## Free planning and cost estimation

Creating and planning a campaign perform no provider call. The plan displays corpus,
exclusion, eligible and selected counts, a ten-record preview, configuration hashes,
cache identity, record-set hash, token estimates, processing cost, cache-storage cost,
and the combined estimate.

Cost calculation reuses `llm_eval.operations.simulate_costs` and the researcher-maintained
Batch pricing configuration. Its token/cache parameters are explicitly calibrated from the
frozen 120-record Gemini 3.1 Flash-Lite benchmark. They remain mechanical planning
estimates, not billing guarantees.

## Paid submission and provider lifecycle

Only an administrator can reach campaign mutations. Submission requires the exact typed
phrase `START AI CAMPAIGN`, READY state, no existing provider job, the approved production
identity, matching record count/hash, and configured server-side credentials.

Planning performs no provider operation. It reports whether a compatible cache is required,
locally reusable, or unavailable. A new campaign may inherit only cache provenance created
and verified by an earlier exact-configuration campaign. Model and thinking configuration
participate in that identity, so a Gemini 2.5 Flash cache can never be reused for Gemini 3.1
Flash-Lite. During confirmed submission the
manager fetches that resource metadata from Gemini and checks its immutable name, model,
hash-derived display identity, creation time, and expiry. Missing, expired, short-lived, or
incompatible resources are never reused.

If no reusable cache remains, the same confirmed submission action uses Phase 4B
`build_cache_create_request` to create a 24-hour cache from the exact canonical assembled V2
system prompt. Its content SHA-256 is computed locally before the request. The returned
identifier, model, display identity, creation/expiry times, verification time, safe usage
metadata, and local content hash are persisted before Batch submission. A failed Batch
submission therefore leaves a verified cache that can be reused on retry. Cache provenance
becomes immutable with the submitted campaign.

The Phase 4B `build_batch_create_request` and `validate_production_plan` functions build and
revalidate the request. The provider job ID is written immediately after submission and is
unique. It is never replaced. API keys come only from the server environment and are never
stored or rendered.

Provider status refresh reuses Phase 4B status parsing. The HTTP request returns after one
status lookup; it never waits for Batch completion. A stored provider job makes interrupted
campaigns recoverable without resubmission. While a campaign is `SUBMITTED` or `RUNNING`,
the browser refreshes only the CSRF-protected provider-status fragment every seven seconds.
The fragment stops polling as soon as the state becomes actionable or terminal, and a manual
refresh remains available. Mutation forms disable their submit controls immediately and the
backend state/idempotency checks remain authoritative.

## Collection, validation, and import

Collection reuses `collect_inline_batch` and the Phase 4B operational summary. Artifacts are
written below the controlled `HANSARD_CAMPAIGN_ARTIFACT_ROOT` (default
`data/review/phase4d`) using the campaign UUID; user paths are never accepted. Collection
preserves mapping errors, failures, coverage, usage, cost, and the provider job identity.
The campaign stores bounded operational coverage/token/cost metadata for recovery and UI
status, while the complete artifacts remain the reproducibility source of record.

Local validation calls the existing Phase 4C `load_prediction_artifact`; missing, duplicate,
invalid, or incomplete results block import. Import calls the existing idempotent
`import_predictions` service, then links the campaign to the resulting immutable
`model_prediction_runs` row. It never inserts predictions in a route handler. The linked
run opens in the existing AI Review queue. Review pages identify a run primarily by its
campaign name, model, prompt, import time, and terminal-decision progress while retaining the
immutable UUID under technical details. Runs remain separate and sort newest-first. Partial
full-review, edited-case, and codebook-flag CSVs are explicitly labelled; summary JSON, audit
Markdown, and audit ZIP artifacts require zero unreviewed predictions.

When original provider coverage is complete but a result is structurally invalid or
provider-failed, an administrator may explicitly approve a paid retry of only the unresolved
record IDs. Every attempt is written under `recovery/attempt-NNN`; original Batch responses,
predictions, failures, summaries, and costs are never rewritten. The effective artifact is a
derived view: the latest successful recovery replaces its original result, otherwise the
original remains effective. Import remains all-or-nothing and is enabled only when this view
contains every planned record with zero missing, duplicate, or invalid results. Recovery
usage/cost stays separately attributable and is added to the displayed campaign total.

Before another paid retry, the campaign UI detects whether an approved deterministic local
structural repair applies. The initial allow-list contains only
`remove_primary_from_secondary_v1`: if a valid primary domain code is repeated in the
secondary-domain list, the identical secondary occurrence is removed. Policy status, primary
domain, every other secondary domain, reasoning, and all other prediction fields remain
unchanged, and the existing strict validator must accept the result before it becomes
effective. The stage writes a new append-only derived directory containing effective
predictions and a separately hashed `structural_repairs.jsonl` with exact before/after values,
source validation details, rule ID, and rule version. Original campaign and provider-recovery
artifacts remain byte-identical. Unrepairable records remain invalid and eligible for targeted
provider recovery; the UI does not encourage another paid retry while this approved repair is
available.

## Idempotency and recovery

- READY plus a null provider job is required for first submission.
- `provider_job_id` is unique and immutable.
- Selected records and record-set identity are pinned before submission.
- A collected artifact reference resumes validation without resubmission.
- Validation is repeat-safe in the same state.
- Phase 4C artifact hashing and database uniqueness make import idempotent.
- A campaign can link to one prediction run, and a run can link to one campaign.
- An active unresolved-set recovery is unique, so double-clicks cannot submit a second job.
- Recovery display names are deterministic; a lost submission receipt is recovered by
  listing Gemini batches and matching the exact display name and model before any new submit.
- Compatible live explicit caches are verified and reused. An unavailable cache is recreated
  from the same frozen V2 canonical prefix and its provenance is stored on that attempt.

There is no polling worker or Redis. Provider lookup, collection, validation, and import are
explicit short web actions and remain available through the Phase 4B/4C CLIs for operational
recovery.

## Permissions and security

Reviewers can see the shared read-only codebook and use existing authorized Human
Annotation and AI Review workspaces. Campaign creation, planning, paid submission,
collection, validation, import, and research exports require the existing global admin
role. All POST actions retain session authentication and CSRF checks. Downloads accept only
validated IDs or fixed artifact names and never expose credentials, environment values, or
internal artifact paths.

## Relationship to existing phases

- Phase 4B remains the source of Batch request/schema construction, frozen-plan validation,
  cost simulation, status parsing, collection, and structured validation.
- Phase 4C remains the source of immutable prediction import, `model_prediction_runs`, AI
  Review, annotation-version linkage, and review audit.
- Phase 4D stores orchestration and provenance only; it does not duplicate predictions or
  human labels.
