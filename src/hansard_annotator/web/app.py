"""FastAPI application factory for the Phase 3 annotation MVP."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.auth.routes import router as auth_router
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import get_settings, require_principal
from hansard_annotator.web.middleware import security_middleware
from hansard_annotator.web.projects.routes import router as projects_router
from hansard_annotator.web.projects.service import list_projects
from hansard_annotator.web.tasks.routes import router as tasks_router
from hansard_annotator.web.tasks.service import personal_dashboard
from hansard_annotator.web.users.routes import router as users_router

PACKAGE_ROOT = Path(__file__).parent


def create_app(settings: WebSettings | None = None) -> FastAPI:
    resolved = settings or WebSettings.from_environment()
    app = FastAPI(
        title="Hansard Research Workspace",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(resolved.allowed_hosts))
    app.middleware("http")(security_middleware)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(tasks_router)
    app.include_router(users_router)

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness(
        web_settings: Annotated[WebSettings, Depends(get_settings)],
    ) -> object:
        try:
            with psycopg.connect(
                web_settings.database.psycopg_url, connect_timeout=3
            ) as connection:
                connection.execute("SELECT 1").fetchone()
        except psycopg.Error:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.get("/")
    def dashboard(
        request: Request,
        principal: Annotated[Principal, Depends(require_principal)],
        web_settings: Annotated[WebSettings, Depends(get_settings)],
    ) -> object:
        if principal.must_change_password:
            return RedirectResponse("/change-password", status_code=303)
        return app.state.templates.TemplateResponse(
            request,
            "dashboard/index.html",
            {
                "principal": principal,
                **personal_dashboard(web_settings, principal),
                "projects": list_projects(web_settings, principal),
            },
        )

    return app
