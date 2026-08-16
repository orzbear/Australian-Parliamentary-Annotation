from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_production_compose_isolates_database_and_hermes() -> None:
    compose_path = ROOT / "compose.production.yaml"
    raw = compose_path.read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    services = compose["services"]

    assert set(services) == {"postgres", "web", "migrate", "caddy"}
    assert "hermes" not in raw.lower()
    assert "ports" not in services["postgres"]
    assert services["postgres"]["networks"] == ["database"]
    assert compose["networks"]["database"]["internal"] is True
    assert services["web"]["read_only"] is True
    assert services["web"]["cap_drop"] == ["ALL"]
    assert services["web"]["environment"]["HANSARD_APP_ENV"] == "production"
    assert services["web"]["environment"]["HANSARD_COOKIE_SECURE"] == "true"
    assert services["caddy"]["ports"] == ["80:80", "443:443", "443:443/udp"]
    assert services["migrate"]["profiles"] == ["tools"]


def test_production_templates_contain_no_real_secrets_or_destructive_volume_down() -> None:
    environment = (ROOT / "deploy" / ".env.production.example").read_text(
        encoding="utf-8"
    )
    runbook = (ROOT / "docs" / "PILOT_DEPLOYMENT_RUNBOOK.md").read_text(
        encoding="utf-8"
    )
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "deploy" / "scripts").glob("*.sh"))
    )

    assert "annotator.polisde.tech" in environment
    assert "GENERATE_WITH_OPENSSL" in environment
    assert "change-me" not in environment
    assert "docker compose down -v" not in scripts
    assert "python -m hansard_annotator.product.cli verify" in scripts
    assert "Never run `docker compose down -v`" in runbook
    assert "5432:5432" not in (ROOT / "compose.production.yaml").read_text(
        encoding="utf-8"
    )


def test_production_runtime_versions_and_https_host_are_pinned() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert "python:3.12.13-slim-bookworm" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "headers={'Host': host}" in dockerfile
    assert "postgres:16.14-bookworm" in compose
    assert "caddy:2.11.4-alpine" in compose
    assert "{$HANSARD_DOMAIN}" in caddy
    assert "reverse_proxy web:8000" in caddy
    assert "Host {$HANSARD_DOMAIN}" in caddy


def test_daily_backup_timer_is_persistent_and_non_destructive() -> None:
    service = (
        ROOT / "deploy/systemd/hansard-annotator-backup.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "deploy/systemd/hansard-annotator-backup.timer"
    ).read_text(encoding="utf-8")

    assert "ExecStart=/bin/sh /opt/hansard-annotator/deploy/scripts/backup.sh" in service
    assert "OnCalendar=*-*-* 03:17:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=15m" in timer
    assert "rm " not in service + timer


def test_github_production_deployment_is_manual_gated_and_serialized() -> None:
    workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(
        encoding="utf-8"
    )
    workflow_model = yaml.load(workflow, Loader=yaml.BaseLoader)

    assert set(workflow_model["on"]) == {"workflow_dispatch"}
    assert set(workflow_model["jobs"]) == {"deploy"}
    assert "workflow_dispatch:" in workflow
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in workflow
    assert 'test "$CONFIRMATION" = "deploy-production"' in workflow
    assert "group: production" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "StrictHostKeyChecking=yes" in workflow
    assert "DEPLOY_SSH_KEY" in workflow
    assert "POSTGRES_PASSWORD" not in workflow
    assert "docker compose down" not in workflow


def test_restricted_release_command_backs_up_before_migration() -> None:
    release = (ROOT / "deploy/host/hansard-release").read_text(encoding="utf-8")
    bootstrap = (ROOT / "deploy/host/bootstrap-github-deployer.sh").read_text(
        encoding="utf-8"
    )

    backup_position = release.index('sh "$APP_DIR/deploy/scripts/backup.sh"')
    migration_position = release.index('sh "$release_dir/deploy/scripts/migrate.sh"')
    assert backup_position < migration_position
    assert "sha256sum" in release
    assert "flock -n" in release
    assert "--no-build web caddy" in release
    assert "docker compose down" not in release
    assert "hansard-postgres-data" not in release
    assert "NOPASSWD: %s" in bootstrap
    assert "restrict %s" in bootstrap
