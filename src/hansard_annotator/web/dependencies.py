"""FastAPI authentication and CSRF dependencies."""

from __future__ import annotations

from typing import Annotated, cast

import psycopg
from fastapi import Depends, HTTPException, Request, status

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.auth.service import resolve_session
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.security import SESSION_COOKIE, verify_csrf


def get_settings(request: Request) -> WebSettings:
    return cast(WebSettings, request.app.state.settings)


def optional_principal(
    request: Request, settings: Annotated[WebSettings, Depends(get_settings)]
) -> Principal | None:
    return resolve_session(settings, request.cookies.get(SESSION_COOKIE))


def require_principal(
    principal: Annotated[Principal | None, Depends(optional_principal)],
) -> Principal:
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return principal


def require_admin(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return principal


async def enforce_csrf(
    request: Request, principal: Principal, settings: WebSettings
) -> None:
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied:
        form = await request.form()
        value = form.get("csrf_token")
        supplied = str(value) if value is not None else None
    raw_token = request.cookies.get(SESSION_COOKIE, "")
    with psycopg.connect(settings.database.psycopg_url) as connection:
        row = connection.execute(
            "SELECT csrf_secret_hash FROM web_sessions WHERE id=%s AND revoked_at IS NULL",
            (principal.session_id,),
        ).fetchone()
    if row is None or not verify_csrf(
        supplied, raw_token, str(row[0]), settings.session_secret
    ):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
