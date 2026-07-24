# Architecture Decisions

## Implemented Phase 2.5 product-foundation amendment

Revision `20260724_02` adds stable corpus identity above accepted preprocessing runs,
with non-null corpus links on preprocessing runs and source files. Existing deterministic
keys, immutable corpus rows, lean provenance, importer, and counts remain unchanged.
`annotation_ready_turns` exposes corpus public ID, slug, and name.

Taxonomies and annotation schemas are independently named and versioned. Canonical seed
hashes and repository-relative source paths support reproducibility; published versions
and their child rows are database-immutable. Schema rules use a closed declarative
grammar and cannot execute code. Phase 3 projects will pin exact schema and taxonomy
versions rather than copying mutable definitions.

The source-adapter registry is static and capability-aware. Its only entry,
`openaustralia_publicwhip_xml`, delegates to the accepted Phase 1 implementation.
ParlaMint is a future boundary mapping, not the internal database model. CAP remains a
future optional enrichment, with subtopic stored and major topic derived through
taxonomy hierarchy; it is absent from the initial schema.

## Implemented Phase 2 corpus database amendment

Phase 2 implements PostgreSQL 16 as the transactional source of truth for the accepted
processed corpus. Internal relational keys are bigint identity values; deterministic
Phase 1 keys remain unique immutable domain identifiers. The importer uses bounded
PyArrow record batches, psycopg 3 COPY into a temporary JSONB staging table, relational
promotion in dependency order, and a single atomic corpus transaction.

The lean-provenance decision remains in force. PostgreSQL stores source checksums,
fragment projections, original attributes, raw/clean text, structured block JSON,
manifest/report/Parquet artefact metadata, and lineage. It does not store full canonical
fragment XML. Image anomaly evidence comes from the accepted manifested CSV and no
remote image is retrieved.

Accepted corpus/provenance tables have database triggers rejecting update/delete.
Corrections require a new preprocessing/reconstruction run. Re-import of the identical
accepted manifest performs checksum/count verification, records `completed_reused`, and
writes zero corpus rows. Same deterministic key with a different canonical row checksum
is a conflict and is never overwritten.

No Redis or broad job framework was introduced. The only development service is a
localhost-bound PostgreSQL 16 container. Phase 3 authentication, application tables,
annotations, and web/API work remain absent.

Status: core architecture implemented through Phase 2 on 23 July 2026. Rows that concern
phases remain subject to their stated approval gates. “Reversible” describes technical
reversibility; changing a research method after coding has started may still require a
new dataset version.

| Decision | Recommended option | Alternatives | Rationale | Risks | Reversible? | Owner approval required? |
|---|---|---|---|---|---|---|
| Application shape | Modular monolith with separately invoked web and worker processes | Microservices | One codebase and database keep permissions, transactions, and label provenance coherent at current scale while preserving module boundaries | Poor module discipline could create coupling | Yes, modules can later be extracted | Yes |
| Frontend | FastAPI + Jinja2 + HTMX + modest JavaScript | React/Next.js SPA | Meets form-heavy, mobile and adjudication needs with one validation/auth/deployment surface | Complex client interactions may eventually strain HTMX | Yes; add an API-driven SPA later | Yes |
| Database | Self-managed PostgreSQL 16 | Supabase; SQLite; managed PostgreSQL | Strong constraints, ranges, JSONB, FTS, concurrency and reproducibility; fits existing VPS and avoids browser-direct data access | Requires database operations and backups | Yes, but migration is material | Yes |
| Database identifiers | `bigint` identity internal PKs; UUID public IDs for exposed domain objects; source IDs stored under scoped unique constraints | UUID-only PKs; source-derived PKs | Compact indexes and fast joins while avoiding sequential external enumeration; source IDs may be missing/duplicated and must remain data | Dual identifiers require consistent API conventions | Partly | Yes |
| Source-file identity | Logical path plus immutable checksum/version rows | Overwrite one row per path; checksum-only identity | Preserves changed upstream files and lets snapshots cite exact bytes | More provenance rows and active-version logic | No once snapshots depend on it | Yes |
| XML parser | `lxml.etree.iterparse` in strict hardened mode; reject DOCTYPE and disable DTD, entities, network and huge trees | `xml.etree.ElementTree`; DOM parse; BeautifulSoup/recovery parsing | Bounded memory, document order, mixed markup handling, explicit security controls, useful diagnostics | Native dependency; unsafe defaults if configuration regresses | Yes behind parser interface | Yes |
| Parser failure policy | File-scoped transaction, record failure, continue corpus | Abort entire run; recover malformed XML silently | Produces a complete quality inventory without partial rows or hidden repairs | Successful run can contain failed files unless quality gate is enforced | Yes | Yes |
| Continuation merging | Conservative deterministic merge by exact known speaker ID; exact normalised name only as restricted fallback; hard heading/structural/substantive-speaker boundaries | Merge by adjacency; merge by name broadly; never merge | Minimises false attribution, which is especially damaging for hostility analysis | Under-merging produces orphan continuations | Method version can change, but outputs must be versioned | Yes |
| Collective interruption exception | Allow only reviewed unknown/collective interruption-like `talktype="speech"` records to leave the prior turn open; record reason | Treat every `speech` as a boundary; broad text heuristic | Sampled XML encodes collective interjections as `speech`, so a literal boundary loses valid chains | Heuristic could misclassify a collective substantive statement | Yes via reconstruction version | Yes |
| Ambiguous continuations | Preserve as standalone orphan/anomaly; do not guess | Attach to nearest speaker; discard | Keeps language and uncertainty visible and auditable | Some true turns remain split until review/rule update | Yes via new reconstruction | Yes |
| Interjection storage | Separate `interjections` row linked to its immutable fragment and optionally interrupted turn | Inline in main text; JSON array on turn only | Prevents speaker misattribution while retaining identity, sequence, text and queryability | Link can be uncertain | Yes, with lineage retained | Yes |
| Turn-fragment lineage | Ordered join table | Array of fragment IDs on turn | Referential integrity, relation type, order, anomaly queries, and future many-to-many flexibility | Extra join | Difficult to simplify later, low downside | No |
| Lean fragment provenance | Keep immutable source XML outside Git; store source-file checksum/version, fragment source ID/sequence, original attributes, raw extracted text, conservative clean text, and a deterministic projection checksum where useful. Do not store canonical fragment XML in PostgreSQL by default; permit bounded anomaly/debug artefacts only when justified | Canonical XML for every fragment; clean text only; raw XML only | Exact source bytes remain anchored by the file hash, while fragment identity and parser projection support audit/reprocessing without duplicating the archive in PostgreSQL | Projection checksum specification must be versioned; external anomaly artefacts need retention/access controls | Yes, but snapshot rules must be versioned | Approved |
| Business-type mapping | Original headings plus versioned exact/anchored configuration; unknowns queued for review | Fuzzy guessing; overwrite headings | Reproducible categories and visible uncertainty | Mapping maintenance workload | Yes via config version | Yes |
| Political metadata | Separate source-cited date-valid affiliation records joined by speaker and speech date | Current-party field on speaker/turn; destructive backfill | Historical party/status/role is time-dependent and may be corrected independently | Identity/range conflicts require curation | No without research data loss | Yes |
| Table partitioning | No initial PostgreSQL partitioning | Partition fragments/turns by year | Estimated 250k fragments is modest; indexes and batching suffice and simpler constraints/migrations are valuable | Later growth may require online repartitioning | Yes, with migration | No, unless owner expects much larger corpora |
| Full-text search | Add GIN `tsvector` only when search is in an accepted phase; basic indexed filters first | External search engine; immediate FTS | PostgreSQL FTS is adequate at estimated scale; avoid an extra service | Building index later takes resources | Yes | No |
| Authentication | Local username/password with Argon2id and opaque PostgreSQL-backed secure sessions | OAuth/OIDC; JWT in browser; HTTP basic | Appropriate for a small initial team; supports revocation, CSRF protection and audit without enterprise complexity | Password reset/admin operations must be secure | Yes; OIDC can be added | Yes |
| Authorisation | Global role ceiling plus explicit project memberships; deny by default | Global role only; database exposed to client | Required for cross-project isolation and future collaborators | Policy bugs can leak unpublished labels | No—core requirement | Yes |
| Human annotation versioning | Stable annotation series plus append-only full versions and current pointer | Mutable row; event log only; JSON diff chain | Simple current reads and complete reconstructable history; corrections never overwrite | More tables/storage and pointer consistency | No once data exists | Yes |
| Human/LLM coexistence | Separate human annotation and LLM annotation tables; adjudication cites versions | One polymorphic mutable label table | Enforces methodological separation, blinding and provider provenance | Some reporting queries need a union/selection layer | No—core research-integrity choice | Yes |
| Evidence representation | Character-offset spans tied to exact input/text hash, with optional excerpt hash | Free text evidence only; copied excerpt only | Machine-checkable and reproducible even when similar text repeats | Text-version mismatch invalidates offsets | Yes with migration/versioning | Yes |
| Task model | One project-turn task with separate coder assignments | Duplicate whole task per coder; one owner on task | Supports balanced overlap, required coder counts, individual leases and blinding | More state transitions | Partly | Yes |
| Task claiming | PostgreSQL transaction using `SELECT ... FOR UPDATE SKIP LOCKED`, leases and idempotency keys | In-memory lock; Redis lock; optimistic claim only | Atomic, durable and sufficient for small concurrency without another service | Requires careful fairness/lease tests | Yes behind service | No |
| Background jobs initially | PostgreSQL `jobs` table plus dedicated worker using leases/heartbeats | Celery/RQ/Dramatiq + Redis; synchronous web work | Durable imports/exports with minimal operations and shared transaction semantics | High-throughput scheduling is less feature-rich | Yes | Yes |
| Redis initially | Do not deploy | Redis for queue/cache/session | No demonstrated need; sessions and low-volume jobs fit PostgreSQL | Later LLM throughput may justify migration | Yes | Yes |
| Future job escalation | Add Dramatiq with Redis only after measured concurrency/retry/scheduling pressure | Celery; RQ; continue PostgreSQL | Dramatiq is comparatively small and reliable; decision deferred until evidence exists | Two queue mechanisms during migration | Yes | No, until threshold is reached |
| Export execution | Always create an asynchronous durable export job, even for small exports | Synchronous response for small jobs | Consistent permissions/audit/manifest/checksum contract; supports streaming and retries | Slight latency and worker dependency | Yes | Yes |
| Export formats | CSV, Parquet and JSONL; Parquet is analytical output, not source of truth | CSV only; store operations in Parquet | Covers interoperability, typed analytics and nested exchange while PostgreSQL remains transactional | Schema/version documentation must cover three formats | Yes | Yes |
| Reverse proxy | Reuse the existing supported HTTPS proxy; if none exists, use Caddy | Always install Caddy; replace with Nginx; expose Uvicorn | Avoids disrupting Hermes; Caddy is the simplest greenfield certificate/proxy choice | Incumbent configuration may constrain routing; unknown until VPS audit | Yes | Yes before deployment |
| Deployment | Docker Compose with web/worker/PostgreSQL on a private network and read-only XML mount | Kubernetes; host services; public database | Appropriate to one VPS and operationally comprehensible | Compose host remains a single failure domain | Yes | Yes |
| Database migrations | Explicit one-shot Alembic release job after backup/compatibility check | Auto-migrate on every web start; manual SQL | Prevents competing instances and enables reviewed rollback/forward plans | Requires release discipline | Yes | No |
| Database backup | Nightly custom-format `pg_dump`, encrypted off-host retention, monitored checksums, quarterly restore drill | Volume snapshot only; local-only dumps; logical replication | Portable point-in-time snapshots at this scale plus proof that restore works | RPO is daily unless WAL archiving is later added | Yes; add WAL/PITR later | Yes |
| Raw archive backup | Separate checksum inventory and off-host/filesystem snapshot; never rely on DB backup | Embed all raw files in DB; single VPS copy | Database provenance points to source bytes, which need independent preservation | Additional storage/retention cost | Yes operationally | Yes |
| Reverse-proxy/API integration for Hermes | Narrow HTTPS service API with rotatable scoped credential and Telegram-user allowlist | Direct database; arbitrary SQL/tool access; shared app password | Maintains one permission/methodology path and isolates compromise | Service credential and replay/idempotency need care | Yes | Yes |
| Telegram scope | Secondary client after stable web workflow; no adjudication/admin/import/export initially | Telegram-first; full feature parity | Long text and complex review work better on web; reduces methodological divergence | Users may expect more bot features | Yes | Yes |
| LLM worker privilege | Narrow database role and approved provider egress only; no shell/browser/Telegram/admin DB | General Hermes agent; worker with broad tools | Speech is untrusted and LLM output must not control infrastructure | Isolation requires deployment work | No—security boundary | Yes |
| LLM result policy | Append raw/parsed/validated output with prompt/model/codebook/input hashes; route to separate human review | Overwrite human label; store parsed label only | Full provenance, resumability, validation and audit | Storage/provider-retention implications | No—research-integrity choice | Yes |
| Audit model | Append-only structured audit events plus immutable research versions | Mutable activity log; application logs only | Supports security investigation and methodological reconstruction | Audit data may contain sensitive metadata and needs retention controls | No | Yes |

## Approval checkpoints

The minimum approval set before preprocessing implementation is:

1. Speaker turns are the primary annotation unit.
2. The conservative continuation rule and collective-interruption exception are accepted.
3. Source provenance/chamber/licence and use of small test fixtures are confirmed.
4. Lean fragment provenance is retained as specified above; canonical fragment XML is
   optional and omitted from PostgreSQL by default.

The minimum approval set before the web application is:

1. FastAPI/Jinja2/HTMX and PostgreSQL are accepted.
2. Local Argon2id authentication, project membership, and role boundaries are accepted.
3. Codebook/taxonomy, coder count, blinding, revision, and adjudication policies are supplied.
4. Async PostgreSQL-backed jobs and exports without initial Redis are accepted.

Production deployment decisions remain conditional on a read-only VPS/Hermes inventory.
