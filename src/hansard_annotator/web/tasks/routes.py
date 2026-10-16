"""Personal queues, claiming, workspace, and annotation actions."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from hansard_annotator.web.annotations.ai_review import (
    REVIEW_FILTERS,
    RUN_FILTERS,
    ai_review_queue,
    ai_review_runs,
    ensure_prediction_assignment,
    next_unreviewed_prediction,
    prediction_review_workspace,
    record_prediction_review,
)
from hansard_annotator.web.annotations.service import (
    AnnotationRevisionError,
    AnnotationValidationError,
    revision_history,
    save_annotation,
    schema_form,
)
from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import enforce_csrf, get_settings, require_principal
from hansard_annotator.web.projects.permissions import PermissionDenied
from hansard_annotator.web.tasks.service import (
    assignment_progress,
    claim_next,
    get_assignment,
    human_annotation_detail,
    next_human_assignment,
    reopen_skipped_assignment,
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
        f"/assignments/{assignment['id']}" if assignment else f"/projects/{project_id}?no_tasks=1"
    )
    return RedirectResponse(destination, status_code=303)


@router.post("/projects/{project_id}/next-assignment")
async def next_assignment_route(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    exclude_assignment_id: Annotated[int | None, Form()] = None,
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    assignment = next_human_assignment(
        settings,
        principal,
        project_id,
        exclude_assignment_id=exclude_assignment_id,
    )
    destination = (
        f"/assignments/{assignment['id']}"
        if assignment
        else f"/projects/{project_id}?queue_complete=1"
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
        {
            "principal": principal,
            "assignment": assignment,
            **form,
            "progress": assignment_progress(
                settings, principal, cast(int, assignment["project_id"])
            ),
            "errors": {},
        },
    )


@router.get("/assignments/{assignment_id}/detail")
def completed_annotation_detail(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    try:
        annotation = human_annotation_detail(settings, principal, assignment_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, PermissionDenied) as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return _templates(request).TemplateResponse(
        request,
        "annotations/detail.html",
        {"principal": principal, "annotation": annotation},
    )


@router.post("/assignments/{assignment_id}/reopen")
async def reopen_assignment(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        reopen_skipped_assignment(settings, principal, assignment_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(f"/assignments/{assignment_id}?reopened=1", status_code=303)


async def _annotation_payload(
    request: Request,
    settings: WebSettings,
    principal: Principal,
    assignment_id: int,
) -> tuple[dict[str, object], dict[str, object], int]:
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
    raw_revision = form.get("expected_revision_number")
    try:
        expected_revision = int(str(raw_revision))
    except (TypeError, ValueError) as error:
        raise AnnotationRevisionError(
            "The annotation revision is missing or malformed. Reload the form and try again.",
            status_code=422,
        ) from error
    if expected_revision < 0:
        raise AnnotationRevisionError(
            "The annotation revision is invalid. Reload the form and try again.",
            status_code=422,
        )
    return payload, metadata, expected_revision


async def _payload_for_fields(request: Request, fields: list[dict[str, Any]]) -> dict[str, object]:
    form = await request.form()
    payload: dict[str, object] = {}
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
            payload[key] = str(raw) if raw else None
    return payload


async def _save(
    request: Request,
    assignment_id: int,
    settings: WebSettings,
    principal: Principal,
    event_type: str,
) -> object:
    await enforce_csrf(request, principal, settings)
    try:
        payload, metadata, expected_revision = await _annotation_payload(
            request, settings, principal, assignment_id
        )
        effective_event_type = (
            "revised"
            if event_type == "submitted"
            and metadata["annotation_status"] in {"submitted", "revised"}
            else event_type
        )
        result = save_annotation(
            settings,
            principal,
            assignment_id,
            payload,
            event_type=effective_event_type,
            expected_revision_number=expected_revision,
        )
    except AnnotationRevisionError as error:
        assignment = get_assignment(settings, principal, assignment_id)
        current_metadata = schema_form(settings, principal, assignment_id)
        return _templates(request).TemplateResponse(
            request,
            "annotations/workspace.html",
            {
                "principal": principal,
                "assignment": assignment,
                **current_metadata,
                "progress": assignment_progress(
                    settings, principal, cast(int, assignment["project_id"])
                ),
                "errors": {"_form": [str(error)]},
            },
            status_code=error.status_code,
        )
    except AnnotationValidationError as error:
        assignment = get_assignment(settings, principal, assignment_id)
        invalid_metadata: dict[str, Any] = schema_form(settings, principal, assignment_id)
        invalid_metadata["values"] = error.outcome.values
        return _templates(request).TemplateResponse(
            request,
            "annotations/workspace.html",
            {
                "principal": principal,
                "assignment": assignment,
                **invalid_metadata,
                "progress": assignment_progress(
                    settings, principal, cast(int, assignment["project_id"])
                ),
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
    if effective_event_type in {"submitted", "revised"}:
        project_id = int(get_assignment_project(settings, assignment_id))
        next_assignment = next_human_assignment(
            settings,
            principal,
            project_id,
            exclude_assignment_id=assignment_id,
        )
        if next_assignment:
            return RedirectResponse(
                f"/assignments/{next_assignment['id']}?submitted=1", status_code=303
            )
        return RedirectResponse(
            f"/projects/{project_id}?queue_complete=1", status_code=303
        )
    return RedirectResponse(f"/assignments/{assignment_id}?saved=1", status_code=303)


def _accepted_ai_payload(metadata: dict[str, object]) -> dict[str, object]:
    prediction = cast(dict[str, object] | None, metadata.get("ai_prediction"))
    if prediction is None:
        raise LookupError("assignment has no model prediction")
    payload: dict[str, object] = {}
    for field in cast(list[dict[str, Any]], metadata["fields"]):
        key = str(field["field_key"])
        field_type = str(field["field_type"])
        payload[key] = [] if field_type == "multiple_taxonomy" else None
    payload["is_non_policy"] = bool(prediction["is_non_policy"])
    payload["primary_australian_domain"] = prediction["primary_australian_domain"]
    payload["secondary_australian_domains"] = list(
        cast(list[str], prediction["secondary_australian_domains"])
    )
    if prediction["primary_australian_domain"] == "AU_OTHER_REVIEW":
        payload["fallback_explanation"] = prediction["reasoning"]
    return payload


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
    project_id = int(get_assignment_project(settings, assignment_id))
    set_assignment_outcome(settings, principal, assignment_id, "skipped")
    next_assignment = next_human_assignment(
        settings,
        principal,
        project_id,
        exclude_assignment_id=assignment_id,
    )
    destination = (
        f"/assignments/{next_assignment['id']}?skipped=1"
        if next_assignment
        else f"/projects/{project_id}?queue_complete=1&status=skipped"
    )
    return RedirectResponse(destination, status_code=303)


@router.post("/assignments/{assignment_id}/flag")
async def flag(
    request: Request,
    assignment_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    reason: Annotated[str, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    set_assignment_outcome(settings, principal, assignment_id, "flagged", reason=reason)
    return RedirectResponse("/", status_code=303)


@router.get("/ai-reviews")
def review_queue(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    status: str = "unreviewed",
    run_id: str | None = None,
    primary_domain: str | None = None,
    is_non_policy: bool | None = None,
    runs: str = "all",
) -> object:
    if status not in REVIEW_FILTERS:
        status = "unreviewed"
    if runs not in RUN_FILTERS:
        runs = "all"
    rows = ai_review_queue(
        settings,
        principal,
        review_filter=status,
        run_id=run_id,
        primary_domain=primary_domain,
        is_non_policy=is_non_policy,
    )
    return _templates(request).TemplateResponse(
        request,
        "annotations/ai_queue.html",
        {
            "principal": principal,
            "rows": rows,
            "active_filter": status,
            "runs": ai_review_runs(settings, run_filter=runs),
            "active_run_filter": runs,
            "active_run_id": run_id,
        },
    )


def _review_workspace_or_http(
    settings: WebSettings, principal: Principal, prediction_id: str
) -> dict[str, object]:
    try:
        return prediction_review_workspace(settings, principal, prediction_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _next_review_destination(settings: WebSettings, prediction_id: str) -> str:
    next_id = next_unreviewed_prediction(settings, prediction_id)
    return f"/ai-reviews/{next_id}" if next_id else "/ai-reviews?status=unreviewed"


def _prediction_decision_status(payload: dict[str, object], suggested_values: object) -> str:
    decision_values = {
        key: payload.get(key)
        for key in (
            "is_non_policy",
            "primary_australian_domain",
            "secondary_australian_domains",
        )
    }
    return "accepted" if decision_values == suggested_values else "edited"


@router.get("/ai-reviews/{prediction_id}")
def individual_ai_review(
    request: Request,
    prediction_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    workspace = _review_workspace_or_http(settings, principal, prediction_id)
    return _templates(request).TemplateResponse(
        request,
        "annotations/ai_workspace.html",
        {"principal": principal, **workspace, "errors": {}},
    )


@router.post("/ai-reviews/{prediction_id}/accept")
async def accept_prediction(
    request: Request,
    prediction_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    workspace = _review_workspace_or_http(settings, principal, prediction_id)
    assignment_id = ensure_prediction_assignment(settings, principal, prediction_id)
    payload = _accepted_ai_payload(
        {"fields": workspace["fields"], "ai_prediction": workspace["prediction"]}
    )
    event_type = "revised" if workspace["human_annotation"] else "submitted"
    result = save_annotation(settings, principal, assignment_id, payload, event_type=event_type)
    record_prediction_review(
        settings,
        principal,
        prediction_id,
        "accepted",
        assignment_id=assignment_id,
        annotation_version_id=cast(int, result["annotation_version_id"]),
    )
    return RedirectResponse(_next_review_destination(settings, prediction_id), status_code=303)


@router.post("/ai-reviews/{prediction_id}/edit")
async def edit_prediction(
    request: Request,
    prediction_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    await enforce_csrf(request, principal, settings)
    workspace = _review_workspace_or_http(settings, principal, prediction_id)
    fields = cast(list[dict[str, Any]], workspace["fields"])
    payload = await _payload_for_fields(request, fields)
    assignment_id = ensure_prediction_assignment(settings, principal, prediction_id)
    event_type = "revised" if workspace["human_annotation"] else "submitted"
    try:
        result = save_annotation(settings, principal, assignment_id, payload, event_type=event_type)
    except AnnotationValidationError as error:
        workspace["values"] = error.outcome.values
        return _templates(request).TemplateResponse(
            request,
            "annotations/ai_workspace.html",
            {"principal": principal, **workspace, "errors": error.outcome.errors},
            status_code=422,
        )
    status = _prediction_decision_status(payload, workspace["suggested_values"])
    record_prediction_review(
        settings,
        principal,
        prediction_id,
        status,
        assignment_id=assignment_id,
        annotation_version_id=cast(int, result["annotation_version_id"]),
    )
    return RedirectResponse(_next_review_destination(settings, prediction_id), status_code=303)


@router.post("/ai-reviews/{prediction_id}/flag")
async def flag_prediction(
    request: Request,
    prediction_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    note: Annotated[str, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    _review_workspace_or_http(settings, principal, prediction_id)
    if not note.strip():
        raise HTTPException(status_code=422, detail="codebook flag note is required")
    record_prediction_review(settings, principal, prediction_id, "flagged_codebook", note=note)
    return RedirectResponse(f"/ai-reviews/{prediction_id}?flagged=1", status_code=303)


@router.post("/ai-reviews/{prediction_id}/skip")
async def skip_prediction(
    request: Request,
    prediction_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    _review_workspace_or_http(settings, principal, prediction_id)
    record_prediction_review(settings, principal, prediction_id, "skipped")
    return RedirectResponse(_next_review_destination(settings, prediction_id), status_code=303)


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
