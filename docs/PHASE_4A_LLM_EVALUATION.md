# Phase 4A: human gold-set / LLM evaluation

## Purpose and scope

Phase 4A compares configurable LLM classifications with current submitted human
annotations. Human annotations are the evaluation reference, but a mismatch does not prove
that either side is correct. Review artifacts support researcher assessment of model errors,
possible human miscoding, ambiguous boundaries, codebook weaknesses, and underrepresented
categories.

This is offline research tooling. It does not write human annotations, create production LLM
tables, select a production model, adjudicate disagreements, rewrite the codebook, or run the
full corpus.

## Reused infrastructure

The evaluator consumes the deterministic Phase 3 `ai_codebook` export as a directory or ZIP.
It verifies the package format/version, manifest file sizes and SHA-256 checksums, record
count, schema/taxonomy structure, stable record and turn identifiers, and each canonical
human-value hash. `annotations.jsonl`, `CODEBOOK_CONTEXT.json`, and `manifest.json` remain the
source of truth; no second evaluation dataset format exists.

Targeted candidate discovery extends the existing `hansard-db export-review-sample` command,
including its seeded ordering and existing date, length, metadata, interruption, and orphan
filters.

## Architecture and structured output

`hansard_annotator.llm_eval` contains a strict package loader, versioned prompt and JSON
schema, provider-independent prediction/usage models, Gemini and OpenAI adapters, annotation
validation, an incremental runner, metrics/cost aggregation, deterministic mismatch reports,
and a CLI. The transport is injectable, so tests use fakes and never call a provider.

Both adapters request provider-native JSON-schema output. A prediction contains
`is_non_policy`, `primary_australian_domain`, up to two unique secondary domains, and a short
diagnostic reason. Unknown fields/codes, contradictory non-policy/domain combinations, and
other schema violations are recorded as `invalid`, never coerced. Provider failures are
separately `api_failure`; absent checkpoints are `missing`.

Policy metrics treat **policy** as the positive class and report accuracy, precision, recall,
F1, and confusion counts. Primary-domain metrics apply to human-policy records and report
exact match, per-domain precision/recall/F1/support, macro-F1, and a confusion matrix. Every
taxonomy category remains visible when support is zero.

## Reproducibility and resume

`run_metadata.json` records the provider, configured model, export snapshot, schema version
and hash, prompt version and hash, complete generation/thinking settings, UTC timestamp,
software Git revision, and canonical run-identity hash. Returned model versions are stored
per request when exposed.

`predictions.jsonl` is appended and flushed after every request. Resuming an identical run
skips successful records; invalid responses and API failures remain retryable. A changed
provider, model, export, schema, prompt, or material generation setting is rejected in the
same run directory. Use a unique ignored `data/review` directory per model/configuration.

## Security, usage, and cost

Credentials come only from `OPENAI_API_KEY`, `GEMINI_API_KEY`, or `GOOGLE_API_KEY`. They are
placed in request headers, never metadata/results. Provider errors are reduced to safe
messages and configured secret values are defensively redacted.

Every request records provider-reported input, cached-input, output, reasoning/thinking and
total tokens when available, plus latency, retries, status, and returned model. Cost reports
project cost per speech, 1,000 speeches, and 160,000 speeches.

Pricing is never hidden in scoring code. Before a paid run, add verified current prices to
`config/llm_eval/pricing.yaml` under an exact `provider:model` key:

```yaml
format_version: 1
effective_date: "YYYY-MM-DD"
source: "Provider pricing page checked by researcher"
models:
  "gemini:YOUR_MODEL":
    input_per_million_usd: 0.0
    cached_input_per_million_usd: 0.0
    output_per_million_usd: 0.0
```

Without an exact entry, token totals remain available and monetary estimates are `null`,
preventing stale or guessed prices from being presented as actual cost.

## Evaluation commands

Validate configuration without an API call by adding `--dry-run`. Remove it only after
checking the selection, model, pricing, and output directory.

### Five-record smoke test

```powershell
python -m hansard_annotator.llm_eval.cli evaluate `
  --input scripts/conference-codebook-pilot-01-ai-codebook-input-d1d13e895705 `
  --provider gemini --model YOUR_MODEL `
  --output data/review/phase4a-gemini-smoke --limit 5
```

### Existing 30-record pilot

```powershell
python -m hansard_annotator.llm_eval.cli evaluate `
  --input scripts/conference-codebook-pilot-01-ai-codebook-input-d1d13e895705 `
  --provider gemini --model YOUR_MODEL `
  --output data/review/phase4a-gemini-pilot --limit 30
```

### Full current benchmark

```powershell
python -m hansard_annotator.llm_eval.cli evaluate `
  --input scripts/conference-codebook-pilot-01-ai-codebook-input-d1d13e895705 `
  --provider gemini --model YOUR_MODEL `
  --output data/review/phase4a-gemini-full
```

Use `--provider openai` with an OpenAI model for the same pipeline. `--subset-seed` gives
deterministic hash-based selection before `--limit`; repeatable `--record-id` selects explicit
records. Resume is on by default. `--no-resume` rejects an existing run. Provider-specific
thinking settings are `--reasoning-effort` (OpenAI) and `--thinking-budget` (Gemini).

Each run contains `run_metadata.json`, `predictions.jsonl`, `metrics.json`,
`cost_report.json`, `mismatches.jsonl`, `mismatches.csv`, and `report.md`. Mismatches include
stable IDs, parliamentary context, a configurable text snippet, both label sets, reasoning,
model identifiers, and run identity. They never mark either decision correct.

## Targeted sample discovery

These commands only export candidate speeches for human review; they never infer or assign
labels.

```powershell
python -m hansard_annotator.db.cli export-review-sample `
  --output data/review/superannuation-or-pension.csv --limit 100 --seed 20261008 `
  --keyword superannuation --keyword pension --keyword-mode any

python -m hansard_annotator.db.cli export-review-sample `
  --output data/review/au12-human-coded.csv --limit 100 --seed 20261008 `
  --annotation-project conference-codebook-pilot-01 --primary-domain AU12

python -m hansard_annotator.db.cli export-review-sample `
  --output data/review/non-policy-human-coded.csv --limit 100 --seed 20261008 `
  --annotation-project conference-codebook-pilot-01 --annotation-status non-policy
```

Human-label filters require an explicit source project slug and inspect only its current
submitted/revised versions. They can combine with keyword and existing corpus filters. No
index, search service, full-text ranking, or vector search is introduced.

## Limitations and exclusions

Human labels are not adjudicated gold truth. Secondary-domain agreement is retained for
review but is not a required headline metric. Cost estimates require researcher-verified
pricing. Provider/model parameter support, returned version detail, and usage fields vary.

Phase 4A excludes full-corpus annotation, production routing/tables, automatic model
selection, correction, adjudication or codebook rewriting, hostility coding, CAP, political
enrichment, Telegram/Hermes work, general search infrastructure, and frontend changes.
