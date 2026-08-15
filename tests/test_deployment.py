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
