"""Administrator-only account and audit pages."""

from __future__ import annotations

from typing import Annotated, cast

import psycopg
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.auth.service import (
    change_password,
    create_user,
    revoke_all_sessions,
    set_user_enabled,
)
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import enforce_csrf, get_settings, require_admin

router = APIRouter(prefix="/admin")


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


@router.get("/users")
def users(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> object:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            """
            SELECT u.id,u.public_id,u.username,u.email,u.display_name,u.status,
                   u.must_change_password,u.last_login_at,
                   EXISTS (
                     SELECT 1 FROM user_global_roles ugr JOIN global_roles gr
                       ON gr.id=ugr.global_role_id
                     WHERE ugr.user_id=u.id AND gr.role_key='admin'
                   ) AS is_admin
            FROM users u ORDER BY lower(u.username) LIMIT 200
            """
        ).fetchall()
    return _templates(request).TemplateResponse(
        request,
        "admin/users.html",
        {"principal": principal, "users": [dict(row) for row in rows]},
    )


@router.post("/users")
async def user_create(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    username: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
    email: Annotated[str | None, Form()] = None,
    admin: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    create_user(
        settings,
        username,
        display_name,
        password,
        email=email,
        admin=admin,
        actor_user_id=principal.user_id,
    )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/status")
async def user_status(
    request: Request,
    user_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    enabled: Annotated[bool, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    set_user_enabled(settings, user_id, enabled, principal.user_id)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    request: Request,
    user_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    password: Annotated[str, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    change_password(
        settings,
        user_id,
        password,
        actor_user_id=principal.user_id,
        force_change=True,
    )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/revoke-sessions")
async def sessions_revoke(
    request: Request,
    user_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    revoke_all_sessions(settings, user_id, principal.user_id)
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/audit")
def audit(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> object:
    with psycopg.connect(settings.database.psycopg_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            """
            SELECT ae.event_type,ae.entity_type,ae.metadata,ae.occurred_at,
                   u.username,p.name AS project_name
            FROM audit_events ae LEFT JOIN users u ON u.id=ae.actor_user_id
            LEFT JOIN projects p ON p.id=ae.project_id
            ORDER BY ae.occurred_at DESC,ae.id DESC LIMIT 200
            """
        ).fetchall()
    return _templates(request).TemplateResponse(
        request,
        "admin/audit.html",
        {"principal": principal, "events": [dict(row) for row in rows]},
    )
