"""Login, logout, and password-change browser routes."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.auth.service import (
    AuthenticationError,
    authenticate,
    change_password,
    revoke_session,
)
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import (
    enforce_csrf,
    get_settings,
    optional_principal,
    require_principal,
)
from hansard_annotator.web.security import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    make_login_csrf,
    verify_login_csrf,
)

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


@router.get("/login")
def login_page(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal | None, Depends(optional_principal)],
) -> object:
    if principal:
        return RedirectResponse("/", status_code=303)
    token = make_login_csrf(settings.session_secret)
    response = _templates(request).TemplateResponse(
        request, "auth/login.html", {"login_csrf": token}
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=900,
    )
    return response


@router.post("/login")
def login(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> object:
    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE)
    if (
        not cookie_token
        or csrf_token != cookie_token
        or not verify_login_csrf(csrf_token, settings.session_secret)
    ):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    try:
        principal, session_token = authenticate(
            settings,
            username,
            password,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    except AuthenticationError:
        token = make_login_csrf(settings.session_secret)
        response = _templates(request).TemplateResponse(
            request,
            "auth/login.html",
            {"login_csrf": token, "error": "Username or password was not accepted."},
            status_code=400,
        )
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=900,
        )
        return response
    destination = "/change-password" if principal.must_change_password else "/"
    redirect = RedirectResponse(destination, status_code=303)
    redirect.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.absolute_hours * 3600,
        path="/",
    )
    redirect.delete_cookie(LOGIN_CSRF_COOKIE)
    return redirect


@router.post("/logout")
async def logout(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    revoke_session(settings, principal.session_id, principal.user_id)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/change-password")
def change_password_page(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    return _templates(request).TemplateResponse(
        request, "auth/change_password.html", {"principal": principal}
    )


@router.post("/change-password")
async def change_password_action(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> object:
    await enforce_csrf(request, principal, settings)
    if new_password != confirm_password:
        return _templates(request).TemplateResponse(
            request,
            "auth/change_password.html",
            {"principal": principal, "error": "Passwords do not match."},
            status_code=422,
        )
    try:
        change_password(
            settings,
            principal.user_id,
            new_password,
            actor_user_id=principal.user_id,
        )
    except ValueError as error:
        return _templates(request).TemplateResponse(
            request,
            "auth/change_password.html",
            {"principal": principal, "error": str(error)},
            status_code=422,
        )
    response = RedirectResponse("/login?password_changed=1", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
