"""Environment-only web application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hansard_annotator.db.config import DatabaseSettings

DEFAULT_PRODUCTION_EXPORT_WORK_ROOT = Path("/var/lib/hansard-export-work")
DEFAULT_LOCAL_EXPORT_WORK_ROOT = Path("data/exports/work")
DEFAULT_EXPORT_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MINIMUM_EXPORT_MIN_FREE_BYTES = 256 * 1024 * 1024
MAXIMUM_EXPORT_MIN_FREE_BYTES = 1024 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class WebSettings:
    database: DatabaseSettings
    environment: str
    session_secret: str
    cookie_secure: bool
    allowed_hosts: tuple[str, ...]
    base_url: str
    idle_minutes: int
    absolute_hours: int
    minimum_password_length: int
    lockout_failures: int
    lockout_minutes: int
    log_level: str
    export_work_root: Path = DEFAULT_LOCAL_EXPORT_WORK_ROOT
    export_min_free_bytes: int = DEFAULT_EXPORT_MIN_FREE_BYTES

    @classmethod
    def from_environment(cls) -> WebSettings:
        environment = os.getenv("HANSARD_APP_ENV", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise ValueError("HANSARD_APP_ENV must be development, test, or production")
        secret = os.getenv("HANSARD_SESSION_SECRET", "").strip()
        if environment != "test" and (
            len(secret) < 32 or "change-me" in secret.lower()
        ):
            raise ValueError(
                "HANSARD_SESSION_SECRET must be a non-placeholder value of at least "
                "32 characters"
            )
        if environment == "test" and not secret:
            secret = "test-only-session-secret-not-for-production"
        cookie_secure = _boolean(
            os.getenv(
                "HANSARD_COOKIE_SECURE",
                "false" if environment in {"development", "test"} else "true",
            )
        )
        hosts = tuple(
            host.strip()
            for host in os.getenv(
                "HANSARD_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver"
            ).split(",")
            if host.strip()
        )
        if not hosts:
            raise ValueError("HANSARD_ALLOWED_HOSTS must contain at least one host")
        export_work_root = _export_work_root(environment)
        return cls(
            database=DatabaseSettings.from_environment(),
            environment=environment,
            session_secret=secret,
            cookie_secure=cookie_secure,
            allowed_hosts=hosts,
            base_url=os.getenv("HANSARD_BASE_URL", "http://127.0.0.1:8000").rstrip(
                "/"
            ),
            idle_minutes=_bounded_int("HANSARD_SESSION_IDLE_MINUTES", 60, 5, 1440),
            absolute_hours=_bounded_int("HANSARD_SESSION_ABSOLUTE_HOURS", 12, 1, 168),
            minimum_password_length=_bounded_int(
                "HANSARD_MINIMUM_PASSWORD_LENGTH", 12, 12, 128
            ),
            lockout_failures=_bounded_int(
                "HANSARD_LOGIN_LOCKOUT_FAILURES", 5, 2, 100
            ),
            lockout_minutes=_bounded_int(
                "HANSARD_LOGIN_LOCKOUT_MINUTES", 15, 1, 1440
            ),
            log_level=os.getenv("HANSARD_LOG_LEVEL", "INFO").upper(),
            export_work_root=export_work_root,
            export_min_free_bytes=_bounded_int(
                "HANSARD_EXPORT_MIN_FREE_BYTES",
                DEFAULT_EXPORT_MIN_FREE_BYTES,
                MINIMUM_EXPORT_MIN_FREE_BYTES,
                MAXIMUM_EXPORT_MIN_FREE_BYTES,
            ),
        )


def _export_work_root(environment: str) -> Path:
    raw = os.getenv("HANSARD_EXPORT_WORK_ROOT", "").strip()
    if environment == "production":
        if not raw:
            raise ValueError("HANSARD_EXPORT_WORK_ROOT must be explicitly set in production")
        normalised = raw.replace("\\", "/").rstrip("/") or "/"
        if normalised in {"/tmp", "/var/tmp"} or normalised.startswith(("/tmp/", "/var/tmp/")):
            raise ValueError(
                "HANSARD_EXPORT_WORK_ROOT must be a disk-backed path outside /tmp in production"
            )
        return Path(raw)
    if raw:
        return Path(raw).resolve()
    return DEFAULT_LOCAL_EXPORT_WORK_ROOT.resolve()


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(value: str) -> bool:
    normalised = value.strip().lower()
    if normalised not in {"true", "false", "1", "0"}:
        raise ValueError("boolean setting must be true/false or 1/0")
    return normalised in {"true", "1"}
