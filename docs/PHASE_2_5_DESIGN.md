# Phase 2.5 productisation design

Status: internally consistent pre-implementation design, 24 July 2026.

## Scope and preserved foundation

Phase 2.5 extends the accepted Phase 2 PostgreSQL corpus without replacing it. The
following remain authoritative and unchanged:

- immutable XML is the authority for source bytes;
- accepted Phase 1 Parquet/report output is the transformation evidence;
- PostgreSQL is the transactional source of truth for the processed corpus;
- Phase 1 deterministic keys and record checksums remain immutable domain identifiers;
- speaker turns remain the future annotation unit;
- fragment-to-turn lineage and separate interjections remain intact;
- Alembic remains explicit, and the Parquet importer remains independently idempotent.

No parser, reconstruction rule, accepted Parquet schema, corpus text, source checksum,
anomaly classification, or Phase 2 count will change. Phase 3 users, projects,
assignments, annotation rows, adjudication, web/API routes, and authentication remain
absent.

The pre-implementation design originally proposed constraining the first migration to
the fourteen Phase 2 table names so later model imports could not contaminate an empty
migration chain. The Phase 2.5 verification checkpoint determined that this changed the
accepted revision's executable behaviour. Revision `20260723_01` was therefore restored
exactly; revision `20260724_02` now detaches and owns only its Phase 2.5 table metadata.
The compatibility safeguard is consequently implemented without editing Phase 2's
accepted upgrade or downgrade.

## Corpus registry

`corpora` is the stable product identity for a parliamentary collection. It uses an
internal bigint identity and a non-path public UUID, stable unique slug, descriptive
jurisdiction/legislature/chamber/language/source-format fields, conservative licence and
redistribution statuses, source date range, public/active flags, and an optional current
preprocessing-run pointer.

`preprocessing_runs.corpus_id` and `source_files.corpus_id` become required foreign
keys. Reconstruction remains unambiguously linked through its preprocessing run.
`annotation_ready_turns` will expose corpus slug, name, and public UUID.

Migration order is:

1. create `corpora` without a current-run foreign key;
2. insert exactly one Australian House corpus with a fixed public UUID and
   `pending_review` licence/redistribution statuses;
3. add nullable corpus links;
4. temporarily remove the accepted-run mutation trigger, backfill both existing tables,
   and restore the trigger;
5. make links non-null and add indexes/FKs;
6. add the nullable current-run FK, set it to the accepted run, and install a trigger
   requiring the current run to belong to the same corpus;
7. replace `annotation_ready_turns` compatibly.

Actual database dates, not directory assumptions, define the backfilled range. The
migration refuses an ambiguous existing database containing runs or source files that
cannot safely be assigned to the one accepted corpus.

## Source-adapter contract

The internal adapter registry is static Python registration, not a plugin system. A
typed protocol describes stable metadata and optional capabilities:

- adapter/source-format key and adapter version;
- supported languages and chambers;
- discovery, source validation, preprocessing, direct database import, canonical
  export, and incremental-update capability flags;
- provenance description;
- optional operations represented only when supported.

The `openaustralia_publicwhip_xml` adapter delegates discovery and preprocessing to the
existing `corpus.discovery` and `corpus.pipeline` functions. It does not duplicate or
move the parser. Registry construction rejects duplicate stable keys; lookup rejects
unknown keys; metadata listing never parses the corpus or executes third-party code.

Only this Australian adapter is implemented. ParlaMint TEI, Akoma Ntoso, JSONL, and CSV
remain documented possibilities.

## Versioned taxonomy model

`taxonomies` identifies a reusable vocabulary. `taxonomy_versions` stores a version,
status, canonical seed hash, attribution/licensing fields, and relative seed path.
`taxonomy_labels` stores stable codes, definitions, rules/examples as JSON arrays,
hierarchy, flags, display order, and metadata. `taxonomy_label_relations` permits
allowlisted semantic/crosswalk relations without importing any CAP data.

Database constraints enforce unique version/code/order identities, controlled statuses
and types, SHA-256 shape, and allowed relation types. Deferred triggers require parents
and relation endpoints to belong to compatible versions and reject self/cyclic
hierarchies. Published versions and their labels/relations are immutable. Draft
replacement is never an upsert: the ordinary loader returns reused for the same hash and
conflict for a different hash. A separate explicit development-only replacement command
is designed but not enabled by default.

The two Phase 2.5 seeds are:

- draft Australian policy domains `0.1.0`: exactly fourteen analytical labels plus
  non-analytical, fallback, review-required `AU_OTHER_REVIEW`;
- draft content status `1.0.0`: `substantive_policy`, `procedural`,
  `ceremonial_nonpolicy`, and review-required `unclassifiable`.

## Versioned annotation-schema templates

`annotation_schemas`, `annotation_schema_versions`, and
`annotation_field_definitions` describe safe reusable templates. They do not create
projects or annotations. Field types are restricted to the approved allowlist.
Taxonomy-backed fields reference one exact taxonomy version.

Rules use a small declarative JSON grammar:

- conditions: `field_equals`, `field_not_equals`, `taxonomy_code_equals`, combined only
  through `all` or `any`;
- constraints: `required_when`, `null_unless`, `allowed_when`, `different_from_field`,
  `excluded_codes`, `unique_items`, and scalar/default metadata.

No SQL, JavaScript, expression strings, arbitrary functions, or user scripting are
accepted. Validation resolves field references, rejects cycles/unknown keys, validates
taxonomy references, item bounds, and rule shapes, and computes a canonical content
hash.

The draft Australian Policy Annotation `0.1.0` contains eight fields:
`content_status`, `primary_australian_domain`,
`secondary_australian_domains`, `specific_australian_issue`, `topic_uncertain`,
`fallback_explanation`, `unclassifiable_reason`, and `annotation_notes`. Secondary
domains are optional, unique, limited to two, cannot repeat the primary, and exclude
`AU_OTHER_REVIEW`. Non-substantive statuses null policy fields. CAP is absent.

## Deterministic seed loading

Seeds use `yaml.safe_load`, typed frozen dataclasses, strict allowed/required key checks,
NFC-normalised canonical JSON, sorted object keys, declared display order, and SHA-256.
Validation is read-only. Loading occurs in one transaction and:

- inserts a new taxonomy/schema and version;
- returns `reused` for the same slug/version/hash;
- raises a conflict for the same version with different content;
- never mutates a published version;
- records a repository-relative seed path, never an absolute local path.

Taxonomy labels are inserted in deterministic order and parent keys resolve only within
the same version. Annotation fields load only after referenced taxonomy versions exist.

## Phase 3 references

Designed for Phase 3, but not implemented now: an annotation project will pin one
`annotation_schema_version_id`; that schema version already pins exact taxonomy
versions. Submitted annotations will therefore be interpretable without following a
mutable “current” taxonomy. Projects, assignments, annotations, adjudications, and
export snapshots require new explicitly approved tables.

This is intentionally not a generic form builder. The supported field/rule vocabularies
are closed, declarative, reviewable, and tailored to political-text coding. There is no
arbitrary rendering language, scripting, SQL, plugin marketplace, or public schema
authoring surface.

## Optional CAP path

CAP can later use the same taxonomy/version/label hierarchy. Human/model storage would
select a subtopic label; its major topic is derived through `parent_label_id`. A separate
schema field or enrichment layer can pin the CAP version. Crosswalks to Australian
domains use `taxonomy_label_relations`. No CAP label, codebook text, placeholder, or
required field is created in Phase 2.5.

## ParlaMint path

The canonical relational model remains source document → section/event/fragment →
speaker turn with provenance. A future ParlaMint adapter can map TEI documents into that
model, and a canonical exporter can map supported fields back to TEI. Source-specific
metadata can remain JSON/provenance when no common representation exists. This avoids
making TEI XML the internal database schema or rewriting the accepted Australian
pipeline.

## Migration risks and safeguards

- Circular corpus/current-run references require staged constraints and a compatibility
  trigger.
- Accepted-run immutability requires controlled trigger removal/restoration during
  backfill.
- The Phase 2 view must be dropped/recreated without changing its existing columns.
- Published-content immutability must not prevent Alembic DDL; row triggers, rather than
  schema-owner denial, provide application protection.
- Downgrade would destroy registry/template data. It is permitted only when no taxonomy,
  schema, or non-backfilled corpus content has been loaded; otherwise it raises an
  explicit error. This makes test round-trips safe without silently deleting real
  product metadata.
- A database snapshot precedes acceptance migration. Counts, deterministic keys,
  checksums, accepted import reuse, and both verification commands run before and after
  restart.

## Deferred decisions

Implemented in Phase 2.5: registry tables/backfill, adapter protocol/static registry,
taxonomy/schema tables, approved draft seeds/loaders, CLI/verification, glossary,
licensing/demo/interop documentation.

Designed for Phase 3: projects pinning schema versions and renderers consuming the
closed declarative rules.

Deferred: users/authentication/roles, annotation data and workflow, adjudication,
political enrichment, CAP content/crosswalks, extra adapters, ParlaMint import/export,
production roles/backups/deployment, public licensing conclusions, LLM work, and
Telegram/Hermes.
