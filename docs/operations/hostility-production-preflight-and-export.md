# Hostility production preflight and canonical export

Both commands are local database readers. They make no provider calls and write
no campaign, prediction, or annotation records. Run them from an application
container/release with the matching database URL. Use a stable operator-selected
seed for a new full-corpus selection; do not reuse the reserved 1,000-record
pilot seed.

## Read-only preflight

The operator must look up and confirm the Google project's current documented
enqueued Batch token limit before invoking the command. The CLI requires that
value explicitly; it does not infer the project's tier. The optional concurrent
budget can further constrain one planned submission. The report includes the
exact remaining canonical turn IDs, membership counts, any active campaign
overlap, the deterministic chunk plan, estimated tokens, frozen batch pricing,
and a SHA-256 checksum. Output projection uses exact frozen-configuration pilot
usage. Cost is conservatively estimated with uncached input; the cached-pilot
mix is shown separately and does not assume cache availability beyond provider
expiration.

Example, replacing the corpus UUID, seed, and provider quota with operator
confirmed values:

```sh
docker compose --env-file .env.production -f compose.production.yaml run --rm --no-deps web \
  python -m hansard_annotator.llm_eval.hostility_preflight \
  --corpus-public-id <corpus-public-uuid> \
  --seed <new-stable-full-campaign-seed> \
  --google-enqueued-token-limit <documented-project-token-limit> \
  --max-concurrent-enqueued-tokens <operator-approved-concurrent-budget> \
  --output /tmp/hostility-production-preflight.json
```

The output path must be writable in the container (for example `/tmp`). Review
`readiness.blocking_issues`; a successful process exit does not override an
explicitly false readiness result.

## Canonical hostility export

This separate export combines only imported runs whose entire Gemini hostility
configuration matches the frozen production configuration. It emits one row
per current canonical turn, keeps AI and human resolution values separate, and
fails if a turn has overlapping eligible prediction/outcome provenance. It is
streamed from a repeatable-read, read-only server-side cursor and publishes the
CSV, explicit-schema Parquet, and checksummed manifest atomically.

```sh
docker compose --env-file .env.production -f compose.production.yaml run --rm --no-deps web \
  python -m hansard_annotator.web.research_exports.canonical_hostility \
  --corpus-public-id <corpus-public-uuid> \
  --output /var/lib/hansard-export-work/canonical-hostility-<snapshot-name>
```

The output directory must be absent or empty. This is a command-line export,
not an HTTP request, so the work does not depend on a reverse-proxy request
timeout. Ensure adequate free space on the export work volume before running.
The existing policy canonical export route and schema are unchanged.
