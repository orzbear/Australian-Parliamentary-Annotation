"""Server-side project permission checks."""

from __future__ import annotations

from typing import Any

import psycopg

from hansard_annotator.web.auth.models import Principal

MANAGER_ROLES = {"project_manager"}
ANNOTATOR_ROLES = {"annotator"}
VIEW_ROLES = {"project_manager", "annotator", "adjudicator", "viewer"}


class PermissionDenied(Exception):
    """The authenticated actor lacks the requested capability."""


def require_project_role(
    connection: psycopg.Connection[Any],
    principal: Principal,
    project_id: int,
    allowed: set[str],
) -> str:
    if principal.is_admin:
        return "admin"
    row = connection.execute(
        """
        SELECT project_role FROM project_memberships
        WHERE project_id=%s AND user_id=%s AND status='active'
        """,
        (project_id, principal.user_id),
    ).fetchone()
    role = (
        row["project_role"] if isinstance(row, dict) else row[0]
    ) if row is not None else None
    if role not in allowed:
        raise PermissionDenied("You do not have permission for this project")
    return str(role)
