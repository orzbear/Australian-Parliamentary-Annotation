# Hostinger private-pilot deployment runbook

Status: prepared locally; not yet executed on the VPS.  
Domain: `annotator.polisde.tech` -> `187.52.115.175`.  
Host: Ubuntu 24.04 LTS, 2 vCPU, 8 GB RAM, 100 GB NVMe.

## Safety boundary

The existing Hostinger Hermes service is outside this deployment's mutation scope:

- compose project: `hermes-agent-vtxm`;
- compose directory: `/docker/hermes-agent-vtxm`;
- persistent bind mount: `/docker/hermes-agent-vtxm/data`;
- public port: TCP 32769;
- restart policy: `unless-stopped`.

Do not edit that compose file, join its Docker network, move its data, change port 32769, or
restart its container. The Hansard stack uses compose project `hansard-annotator`, its own
networks and named volumes, and `/opt/hansard-annotator` on the host.

Never run `docker compose down -v`: `-v` deletes persistent database/certificate volumes.

## Architecture

Only Caddy publishes host ports. PostgreSQL has no host port and is attached to an internal
Docker network. The web service is non-root, read-only, capability-free and reachable only
through Caddy. Caddy obtains and renews HTTPS certificates for the dedicated subdomain.

PostgreSQL uses an owner role for explicit migration/backup operations and a separate
application login for the web service. The role-init script reapplies grants after restore
and migration. Web startup never runs migrations.

## 1. Inputs still required

Before VPS mutation, choose:

1. an email address for ACME certificate notices;
2. an approved encrypted off-VPS backup destination;
3. whether the current Hermes TCP 32769 exposure must remain open to the world or can be
   restricted to documented Hostinger source addresses;
4. an SSH firewall policy that will not lock out the owner if their public IP changes.

Hostinger weekly snapshots supplement but do not replace PostgreSQL logical backups.

## 2. Prepare a local release and database backup

Run the complete test, lint, type and production-compose checks before packaging. Create a
new custom-format dump of the current local database; do not use the older pre-migration
backup. Verify `pg_restore --list` and SHA-256 before transfer.

The release archive must contain only:

- `Dockerfile`, `compose.production.yaml`, `alembic.ini`;
- `deploy/`, `src/`, `migrations/`, `config/`;
- `requirements.lock`, `pyproject.toml`, and `README.md`.

It must not contain `.env`, raw XML, processed data, existing backups, exports, Git metadata,
browser results, caches, private keys or credentials.

## 3. Create isolated host directories

Run on the VPS:

```bash
mkdir -p /opt/hansard-annotator
mkdir -p /var/backups/hansard-annotator
chmod 700 /var/backups/hansard-annotator
```

Transfer the reviewed release archive and verified database dump to these exact directories.
Extract application files only under `/opt/hansard-annotator`.

## 4. Create production secrets

From `/opt/hansard-annotator`:

```bash
cp deploy/.env.production.example .env.production
chmod 600 .env.production
```

Generate three different URL-safe secrets by running this separately for
`POSTGRES_PASSWORD`, `HANSARD_APP_DB_PASSWORD`, and `HANSARD_SESSION_SECRET`:

```bash
openssl rand -hex 32
```

Put an operational email in `ACME_EMAIL`. Never paste secrets into chat, commit them, pass
them as command-line arguments, or print the completed file. Then run:

```bash
sh deploy/scripts/preflight.sh
```

Preflight requires mode 600, rejects placeholders, confirms DNS and validates Compose without
printing secrets.

## 5. Build without starting public services

```bash
docker compose --env-file .env.production -f compose.production.yaml build web migrate
```

Review the service boundary:

```bash
docker compose --env-file .env.production -f compose.production.yaml config --services
```

Expected services: `postgres`, `web`, `migrate`, `caddy`. Hermes must not appear.

## 6. Initialise and restore PostgreSQL

Start only the private database:

```bash
docker compose --env-file .env.production -f compose.production.yaml up -d postgres
```

Confirm it is healthy, then perform the guarded initial restore:

```bash
sh deploy/scripts/initial-restore.sh /var/backups/hansard-annotator/INITIAL.dump
```

The restore script refuses to run if any public table already exists. It does not drop or
overwrite an existing application database. Apply current migrations and grants explicitly:

```bash
sh deploy/scripts/migrate.sh
```

Create a fresh post-restore backup before exposing the web service:

```bash
sh deploy/scripts/backup.sh
```

Copy the dump and `.sha256` off the VPS before continuing.

## 7. Start HTTPS application services

```bash
docker compose --env-file .env.production -f compose.production.yaml up -d web caddy
```

Do not run `up` from `/docker/hermes-agent-vtxm`, and do not use the Hermes compose file.
Inspect only the new stack:

```bash
docker compose --env-file .env.production -f compose.production.yaml ps
```

```bash
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100 web caddy
```

Run the verification suite:

```bash
sh deploy/scripts/verify.sh
```

## 8. Firewall checkpoint

After HTTPS works, create a Hostinger firewall with default-deny inbound policy and explicit
allow rules. At minimum:

- TCP 22 for SSH (restrict only if a stable safe source/range and recovery path exist);
- TCP 80 from all addresses for HTTPS redirects/ACME;
- TCP 443 from all addresses;
- UDP 443 from all addresses for HTTP/3, or remove the UDP publish if not desired;
- TCP 32769 with the same reachability Hermes currently requires until Hostinger documents
  a narrower safe source range.

Never allow TCP 5432 or TCP 8000. Confirm SSH in a second terminal before applying any rule
that could close the current session.

## 9. Application acceptance

1. Open `https://annotator.polisde.tech` on desktop and mobile networks.
2. Confirm a valid certificate and HTTPS-only session cookie.
3. Sign in as the existing owner; rotate/reset the password if necessary.
4. Confirm legacy development projects are clearly paused/archived.
5. Create `Conference codebook pilot 01`, mode `pilot`, schema `0.2.0`.
6. Generate the deterministic 50-turn batch.
7. Download both simple CSV and AI-codebook ZIP and verify their content.
8. Confirm Hermes remains running and responsive.

## 10. Backups and monitoring

Run `sh deploy/scripts/backup.sh` daily using a root-owned systemd timer or cron entry. The
script creates a custom-format dump, checksum and mode-600 files without overwriting. It does
not delete old backups. Add retention only after the owner approves daily/weekly/monthly
periods and off-VPS copies are demonstrably working.

Install the reviewed daily timer (03:17 UTC plus up to 15 minutes of randomized delay):

```bash
install -m 644 deploy/systemd/hansard-annotator-backup.service /etc/systemd/system/
install -m 644 deploy/systemd/hansard-annotator-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hansard-annotator-backup.timer
systemctl list-timers hansard-annotator-backup.timer
```

Run `sh deploy/scripts/verify-backup.sh /absolute/path/to/file.dump` after setup changes. At
least quarterly, restore a backup into an isolated database and run full verification.

Monitor disk, memory, container health, certificate renewal and backup age. The VPS currently
has no swap; keep container limits and add swap only as a separately reviewed host operation.

## 11. Rollback

For an application/proxy failure, leave PostgreSQL running and stop only the new public
services:

```bash
docker compose --env-file .env.production -f compose.production.yaml stop caddy web
```

This does not touch Hermes or delete data. Preserve logs, take a database backup if healthy,
and diagnose before rebuilding. Database migrations are forward-only unless their reviewed
downgrade explicitly proves safe; test destructive recovery only in an isolated database.
