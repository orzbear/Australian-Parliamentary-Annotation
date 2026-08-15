# Phase 3 security model

## Assets and trust boundaries

Protected assets are credentials, opaque sessions, unpublished human annotations,
project membership, immutable corpus provenance, and audit evidence. The browser,
speech content, form payloads, headers, URLs, and HTMX requests are untrusted.
PostgreSQL is the enforcement and persistence boundary; normal web code cannot alter
immutable corpus records.

## Authentication

Passwords use Argon2id through `argon2-cffi`, with a configurable minimum length of 12.
Only hashes are stored. Login failures use one generic response and a dummy hash path
to limit account enumeration. Consecutive failures cause configurable account lockout.
Disabled accounts never authenticate and disabling/resetting revokes every session.
Temporary/reset passwords set `must_change_password`; normal routes redirect that user
to password change.

The browser cookie contains 32 random bytes encoded as an opaque token. PostgreSQL
stores only an HMAC-SHA-256 token digest using the application session pepper. Login
creates a fresh identity; logout revokes it. Idle and absolute expiry are checked on
every authenticated request. Cookies are HttpOnly, SameSite=Lax, path `/`, and Secure
outside local development. User agents are truncated; client IPs are stored only as
peppered hashes.

## CSRF and request security

Every state-changing browser request supplies a session-bound CSRF token derived with
HMAC from a stored per-session random secret. Verification is constant-time. Login uses
a short-lived signed double-submit token because no session exists yet. Form and HTMX
requests share the same server-side checks; JavaScript cannot bypass them.

Trusted hosts are allowlisted. Middleware creates a request ID, emits sanitised logs,
and applies CSP (`default-src 'self'`, no object embedding, no framing), nosniff,
strict referrer, frame denial, and restrictive permissions policy. Errors expose a
request ID and safe message, not traces. Parliamentary speech and notes are Jinja
autoescaped and never marked safe.

## Authorisation

Permission checks live in services as well as routes. Global admin is a database role
assignment; all other access requires an active membership in the target project.
Annotator reads join assignments to the authenticated user, preventing identifier
guessing from exposing peer payloads. Managers see progress but not blind annotation
payloads. Disabled users and paused/archived projects cannot claim.

## Data integrity and audit

Project pins freeze when tasks exist or activation occurs. Tasks reference immutable
turns in the pinned reconstruction run. Annotation versions are protected by database
update/delete rejection and canonical hashes. Audit events are append-only and have a
database trigger rejecting modification. Audit metadata is allowlisted and excludes
secrets, speech bodies, passwords, session/CSRF values, and complete annotation values.

## Operational configuration

Required environment variables cover database URL, environment, session pepper, cookie
security, hosts, base URL, expiries, lockout, and log level. Startup rejects missing or
placeholder secrets outside tests. No secrets are committed or printed. Local
development binds PostgreSQL/web to loopback; production deployment and production
database roles are explicitly deferred.

## Threats and controls

| Threat | Primary controls |
|---|---|
| Credential theft/brute force | Argon2id, generic response, lockout, forced reset |
| Session theft/fixation | random opaque token, HMAC-at-rest, login rotation, expiry, revocation, secure cookie |
| CSRF | session/login tokens, SameSite, POST-only mutations |
| XSS from corpus/notes | autoescape, text rendering, CSP |
| IDOR/cross-coder leak | project/user joins in every payload query, service permissions |
| Duplicate task claim | transaction and `FOR UPDATE SKIP LOCKED` |
| Annotation overwrite | append-only rows, triggers, hashes |
| SQL/filter injection | parameterised SQL and typed allowlists |
| Sensitive logging | central sanitisation, IDs/hashes/counts only |
| Clickjacking/MIME abuse | frame denial, CSP, nosniff |

Password recovery is administrator-controlled. Email recovery, public registration,
API keys, production proxy/TLS/HSTS policy, and external identity providers are outside
Phase 3.
