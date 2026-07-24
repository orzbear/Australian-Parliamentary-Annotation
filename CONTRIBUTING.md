# Contributing

## Scope and approval gates

Work only within the currently approved phase. Phases 0 through 2.5 permit repository
safeguards, read-only audit/preprocessing, generated analytical files outside Git,
the PostgreSQL corpus schema/import/verification layer, and synthetic/processed-data
fixtures. Do not begin Phase 3 authentication, annotation workflows, or web work, and
do not install or deploy production services, without explicit owner approval.

## Raw-data safety

1. Treat `hansard_xml_files/` as read-only.
2. Never run formatters, bulk replacements, moves, deletes, or generators against it.
3. Do not add raw XML with `git add -f`.
4. Before and after any corpus-wide read, compare inventory count, byte total, and the
   audit's `corpus_inventory_sha256`.
5. Use synthetic minimal fixtures. A fixture must state that it is synthetic and must
   not reproduce a full real speech or sitting.
6. Audit output and research exports belong outside the source archive and remain
   ignored unless a small reviewed documentation artefact is intentionally committed.

## Development rules

- Use Python 3.12-compatible code and the checked-in dependency lock.
- Keep discovery deterministic: normalised relative paths sorted ordinally.
- Reject unsafe XML declarations such as `DOCTYPE`; never enable external entities,
  DTD loading, network resolution, or recovery that silently changes source content.
- Fail one file safely and continue the audit; do not hide malformed input.
- Preserve observed facts separately from assumptions and estimates.
- Any future reconstruction rule must carry a version and reason codes. Unknown or
  ambiguous continuations remain unmerged.
- Keep human, LLM, and adjudicated annotation histories separate and append-only in
  future phases.
- Never weaken reconstruction to reduce anomaly counts. A changed methodological rule
  requires a new configuration/pipeline version and a complete reconciliation run.
- Never update or delete accepted corpus/provenance rows. Corrections require a new
  preprocessing or reconstruction run.
- Use Alembic explicitly; application startup must not auto-migrate.
- Bulk corpus ingestion must use bounded PyArrow batches and PostgreSQL COPY/staging,
  never ORM row-at-a-time loops or destructive upserts.
- Database tests must use PostgreSQL. SQLite does not establish PostgreSQL behaviour.
- Keep database credentials in environment variables and `.env`; never print complete
  connection URLs or commit credentials.
- Register source adapters statically by stable key. Do not execute arbitrary installed
  modules or duplicate accepted parsers.
- Load taxonomy and schema YAML safely with strict declarative validation. Published
  versions are immutable; a changed seed requires a new version.
- Keep seed paths repository-relative and never put executable code, SQL, or JavaScript
  in validation rules.
- Treat source, taxonomy, processed-text, annotation, and demo publication rights as
  separate decisions.
- Phase 3 development and a bounded pilot may pin an explicitly identified draft
  taxonomy/schema version. Production or gold-standard annotation requires an explicitly
  published immutable version; later changes require a new version rather than mutation.

## Checks before review

Run:

```powershell
python -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests migrations tools
.\.venv\Scripts\mypy.exe src
git check-ignore -v hansard_xml_files/2010/2010-02-02.xml
git status --short
```

Review the diff for accidental source data, secrets, generated data, or scope creep.
Do not commit unless the owner asks for a commit.
