"""Personal queues, claiming, workspace, and annotation actions."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from hansard_annotator.web.annotations.service import (
    AnnotationValidationError,
    revision_history,
    save_annotation,
    schema_form,
)
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import enforce_csrf, get_settings, require_principal
from hansard_annotator.web.tasks.service import (
    claim_next,
    get_assignment,
    set_assignment_outcome,
)

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


@router.post("/projects/{project_id}/claim-next")
async def claim(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    assignment = claim_next(settings, principal, project_id)
    destination = (
        f"/assignments/{assignment['id']}" if assignment else f"/projects/{project_id}"
    )
    return RedirectResponse(destination, status_code=303)


@router.get("/assignments/{assignment_id}")
def assignment_workspace(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    assignment = get_assignment(settings, principal, assignment_id)
    form = schema_form(settings, principal, assignment_id)
    return _templates(request).TemplateResponse(
        request,
        "annotations/workspace.html",
        {"principal": principal, "assignment": assignment, **form, "errors": {}},
    )


async def _annotation_payload(
    request: Request,
    settings: WebSettings,
    principal: Principal,
    assignment_id: int,
) -> dict[str, object]:
    metadata = schema_form(settings, principal, assignment_id)
    form = await request.form()
    payload: dict[str, object] = {}
    fields = cast(list[dict[str, Any]], metadata["fields"])
    for field in fields:
        key = str(field["field_key"])
        field_type = str(field["field_type"])
        if field_type == "multiple_taxonomy":
            payload[key] = [str(item) for item in form.getlist(key)]
        elif field_type in {"boolean", "uncertainty"}:
            raw_values = [str(item).lower() for item in form.getlist(key)]
            payload[key] = None if not raw_values else "true" in raw_values
        else:
            raw = form.get(key)
            payload[key] = str(raw) if raw is not None else None
    return payload


async def _save(
    request: Request,
    assignment_id: int,
    settings: WebSettings,
    principal: Principal,
    event_type: str,
) -> object:
    await enforce_csrf(request, principal, settings)
    payload = await _annotation_payload(request, settings, principal, assignment_id)
    try:
        result = save_annotation(
            settings, principal, assignment_id, payload, event_type=event_type
        )
    except AnnotationValidationError as error:
        assignment = get_assignment(settings, principal, assignment_id)
        metadata: dict[str, Any] = schema_form(settings, principal, assignment_id)
        metadata["values"] = error.outcome.values
        return _templates(request).TemplateResponse(
            request,
            "annotations/workspace.html",
            {
                "principal": principal,
                "assignment": assignment,
                **metadata,
                "errors": error.outcome.errors,
            },
            status_code=422,
        )
    if request.headers.get("HX-Request") == "true" and event_type == "draft_saved":
        return _templates(request).TemplateResponse(
            request,
            "annotations/_save_status.html",
            {"result": result},
        )
    if event_type in {"submitted", "revised"}:
        next_assignment = claim_next(
            settings, principal, int(get_assignment_project(settings, assignment_id))
        )
        if next_assignment:
            return RedirectResponse(
                f"/assignments/{next_assignment['id']}?submitted=1", status_code=303
            )
        return RedirectResponse("/?submitted=1", status_code=303)
    return RedirectResponse(f"/assignments/{assignment_id}?saved=1", status_code=303)


def get_assignment_project(settings: WebSettings, assignment_id: int) -> int:
    import psycopg

    with psycopg.connect(settings.database.psycopg_url) as connection:
        row = connection.execute(
            """
            SELECT t.project_id FROM assignments a JOIN tasks t ON t.id=a.task_id
            WHERE a.id=%s
            """,
            (assignment_id,),
        ).fetchone()
    if row is None:
        raise LookupError("assignment not found")
    return int(row[0])


@router.post("/assignments/{assignment_id}/draft")
async def save_draft(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    return await _save(request, assignment_id, settings, principal, "draft_saved")


@router.post("/assignments/{assignment_id}/submit")
async def submit(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    return await _save(request, assignment_id, settings, principal, "submitted")


@router.post("/assignments/{assignment_id}/skip")
async def skip(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    set_assignment_outcome(settings, principal, assignment_id, "skipped")
    return RedirectResponse("/", status_code=303)


@router.post("/assignments/{assignment_id}/flag")
async def flag(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    reason: Annotated[str, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    set_assignment_outcome(
        settings, principal, assignment_id, "flagged", reason=reason
    )
    return RedirectResponse("/", status_code=303)


@router.get("/assignments/{assignment_id}/history")
def history(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    return _templates(request).TemplateResponse(
        request,
        "annotations/history.html",
        {
            "principal": principal,
            "assignment_id": assignment_id,
            "history": revision_history(settings, principal, assignment_id),
        },
    )
