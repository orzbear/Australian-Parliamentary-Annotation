"""Sanitised append-only audit event writer."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

FORBIDDEN_METADATA_KEYS = {
    "password",
    "password_hash",
    "session",
    "session_token",
    "csrf",
    "speech",
    "text_clean",
    "annotation_values",
    "notes",
}


def record_event(
    connection: psycopg.Connection[Any],
    event_type: str,
    entity_type: str,
    *,
    actor_user_id: int | None = None,
    entity_public_id: UUID | str | None = None,
    project_id: int | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    safe = metadata or {}
    if FORBIDDEN_METADATA_KEYS.intersection(key.lower() for key in safe):
        raise ValueError("audit metadata contains a forbidden sensitive key")
    connection.execute(
        """
        INSERT INTO audit_events
          (actor_user_id,event_type,entity_type,entity_public_id,project_id,metadata)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (
            actor_user_id,
            event_type,
            entity_type,
            entity_public_id,
            project_id,
            Jsonb(safe),
        ),
    )
