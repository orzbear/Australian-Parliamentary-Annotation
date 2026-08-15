"""Account, login, and opaque server-side session services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from hansard_annotator.web.audit.service import record_event
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.security import (
    csrf_token,
    hash_password,
    random_token,
    token_digest,
    verify_password,
)


class AuthenticationError(Exception):
    """Generic authentication rejection."""


def create_user(
    settings: WebSettings,
    username: str,
    display_name: str,
    password: str,
    *,
    email: str | None = None,
    admin: bool = False,
    must_change_password: bool = True,
    actor_user_id: int | None = None,
) -> dict[str, object]:
    username = username.strip()
    display_name = display_name.strip()
    if not username or len(username) > 100 or not display_name:
        raise ValueError("username and display name are required")
    password_hash = hash_password(password, settings.minimum_password_length)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        user = connection.execute(
            """
            INSERT INTO users (username,email,display_name,must_change_password)
            VALUES (%s,%s,%s,%s)
            RETURNING id,public_id,username,display_name,status,must_change_password
            """,
            (username, email.strip() if email else None, display_name, must_change_password),
        ).fetchone()
        assert user is not None
        connection.execute(
            "INSERT INTO user_credentials (user_id,password_hash) VALUES (%s,%s)",
            (user["id"], password_hash),
        )
        if admin:
            connection.execute(
                """
                INSERT INTO user_global_roles
                  (user_id,global_role_id,granted_by_user_id)
                SELECT %s,id,%s FROM global_roles WHERE role_key='admin'
                """,
                (user["id"], actor_user_id),
            )
        record_event(
            connection,
            "account_created",
            "user",
            actor_user_id=actor_user_id,
            entity_public_id=user["public_id"],
            metadata={"username": username, "admin": admin},
        )
        return dict(user)


def authenticate(
    settings: WebSettings,
    username: str,
    password: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[Principal, str]:
    now = datetime.now(UTC)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        user = connection.execute(
            """
            SELECT u.id,u.public_id,u.username,u.display_name,u.status,
                   u.must_change_password,c.password_hash,c.failed_login_count,
                   c.locked_until,
                   EXISTS (
                     SELECT 1 FROM user_global_roles ugr
                     JOIN global_roles gr ON gr.id=ugr.global_role_id
                     WHERE ugr.user_id=u.id AND gr.role_key='admin'
                   ) AS is_admin
            FROM users u JOIN user_credentials c ON c.user_id=u.id
            WHERE lower(u.username)=lower(%s)
            """,
            (username.strip(),),
        ).fetchone()
        locked = bool(user and user["locked_until"] and user["locked_until"] > now)
        valid = verify_password(user["password_hash"] if user else None, password)
        if not user or not valid or locked or user["status"] != "active":
            if user and not locked:
                failures = int(user["failed_login_count"]) + 1
                locked_until = (
                    now + timedelta(minutes=settings.lockout_minutes)
                    if failures >= settings.lockout_failures
                    else None
                )
                connection.execute(
                    """
                    UPDATE user_credentials
                    SET failed_login_count=%s,locked_until=%s,updated_at=now()
                    WHERE user_id=%s
                    """,
                    (failures, locked_until, user["id"]),
                )
            record_event(
                connection,
                "login_failed",
                "authentication",
                actor_user_id=user["id"] if user else None,
                metadata={"reason": "credentials_rejected"},
            )
            connection.commit()
            raise AuthenticationError("Sign-in failed")

        connection.execute(
            """
            UPDATE user_credentials
            SET failed_login_count=0,locked_until=NULL,updated_at=now()
            WHERE user_id=%s
            """,
            (user["id"],),
        )
        connection.execute(
            "UPDATE users SET last_login_at=now(),updated_at=now() WHERE id=%s",
            (user["id"],),
        )
        raw_token = random_token()
        raw_csrf = random_token()
        token_hash = token_digest(raw_token, settings.session_secret)
        csrf_hash = token_digest(raw_csrf, settings.session_secret)
        ip_hash = (
            token_digest(ip_address, settings.session_secret) if ip_address else None
        )
        session = connection.execute(
            """
            INSERT INTO web_sessions
              (user_id,token_hash,csrf_secret_hash,idle_expires_at,
               absolute_expires_at,user_agent_summary,created_ip_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                user["id"],
                token_hash,
                csrf_hash,
                now + timedelta(minutes=settings.idle_minutes),
                now + timedelta(hours=settings.absolute_hours),
                (user_agent or "")[:255] or None,
                ip_hash,
            ),
        ).fetchone()
        assert session is not None
        record_event(
            connection,
            "login_succeeded",
            "authentication",
            actor_user_id=user["id"],
            metadata={"session_id": session["id"]},
        )
        principal = Principal(
            user_id=user["id"],
            public_id=user["public_id"],
            username=user["username"],
            display_name=user["display_name"],
            must_change_password=user["must_change_password"],
            is_admin=user["is_admin"],
            session_id=session["id"],
            csrf_token=csrf_token(raw_token, csrf_hash, settings.session_secret),
        )
        return principal, raw_token


def resolve_session(settings: WebSettings, raw_token: str | None) -> Principal | None:
    if not raw_token:
        return None
    digest = token_digest(raw_token, settings.session_secret)
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT s.id AS session_id,s.csrf_secret_hash,s.absolute_expires_at,
                   u.id AS user_id,u.public_id,u.username,u.display_name,
                   u.must_change_password,u.status,
                   EXISTS (
                     SELECT 1 FROM user_global_roles ugr
                     JOIN global_roles gr ON gr.id=ugr.global_role_id
                     WHERE ugr.user_id=u.id AND gr.role_key='admin'
                   ) AS is_admin
            FROM web_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=%s AND s.revoked_at IS NULL
              AND s.idle_expires_at>now() AND s.absolute_expires_at>now()
            """,
            (digest,),
        ).fetchone()
        if row is None or row["status"] != "active":
            return None
        idle_expiry = min(
            datetime.now(UTC) + timedelta(minutes=settings.idle_minutes),
            row["absolute_expires_at"],
        )
        connection.execute(
            "UPDATE web_sessions SET last_seen_at=now(),idle_expires_at=%s WHERE id=%s",
            (idle_expiry, row["session_id"]),
        )
        return Principal(
            user_id=row["user_id"],
            public_id=row["public_id"],
            username=row["username"],
            display_name=row["display_name"],
            must_change_password=row["must_change_password"],
            is_admin=row["is_admin"],
            session_id=row["session_id"],
            csrf_token=csrf_token(
                raw_token, row["csrf_secret_hash"], settings.session_secret
            ),
        )


def revoke_session(settings: WebSettings, session_id: int, actor_user_id: int) -> None:
    with psycopg.connect(settings.database.psycopg_url) as connection:
        connection.execute(
            "UPDATE web_sessions SET revoked_at=now() WHERE id=%s AND revoked_at IS NULL",
            (session_id,),
        )
        record_event(
            connection,
            "logout",
            "session",
            actor_user_id=actor_user_id,
            metadata={"session_id": session_id},
        )


def revoke_all_sessions(
    settings: WebSettings, user_id: int, actor_user_id: int | None
) -> int:
    with psycopg.connect(settings.database.psycopg_url) as connection:
        cursor = connection.execute(
            """
            UPDATE web_sessions SET revoked_at=now()
            WHERE user_id=%s AND revoked_at IS NULL
            """,
            (user_id,),
        )
        record_event(
            connection,
            "sessions_revoked",
            "user",
            actor_user_id=actor_user_id,
            metadata={"user_id": user_id, "count": cursor.rowcount},
        )
        return cursor.rowcount


def change_password(
    settings: WebSettings,
    user_id: int,
    new_password: str,
    *,
    actor_user_id: int,
    force_change: bool = False,
) -> None:
    password_hash = hash_password(new_password, settings.minimum_password_length)
    with psycopg.connect(settings.database.psycopg_url) as connection:
        connection.execute(
            """
            UPDATE user_credentials
            SET password_hash=%s,password_changed_at=now(),failed_login_count=0,
                locked_until=NULL,updated_at=now()
            WHERE user_id=%s
            """,
            (password_hash, user_id),
        )
        connection.execute(
            """
            UPDATE users SET must_change_password=%s,updated_at=now() WHERE id=%s
            """,
            (force_change, user_id),
        )
        connection.execute(
            "UPDATE web_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",
            (user_id,),
        )
        record_event(
            connection,
            "password_changed" if user_id == actor_user_id else "password_reset",
            "user",
            actor_user_id=actor_user_id,
            metadata={"user_id": user_id, "forced_change": force_change},
        )


def set_user_enabled(
    settings: WebSettings, user_id: int, enabled: bool, actor_user_id: int
) -> None:
    with psycopg.connect(settings.database.psycopg_url) as connection:
        connection.execute(
            """
            UPDATE users
            SET status=%s,disabled_at=CASE WHEN %s THEN NULL ELSE now() END,
                updated_at=now()
            WHERE id=%s
            """,
            ("active" if enabled else "disabled", enabled, user_id),
        )
        if not enabled:
            connection.execute(
                """
                UPDATE web_sessions SET revoked_at=now()
                WHERE user_id=%s AND revoked_at IS NULL
                """,
                (user_id,),
            )
        record_event(
            connection,
            "account_enabled" if enabled else "account_disabled",
            "user",
            actor_user_id=actor_user_id,
            metadata={"user_id": user_id},
        )
