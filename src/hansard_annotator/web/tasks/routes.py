"""Personal queues, claiming, workspace, and annotation actions."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from hansard_annotator.web.annotations.ai_review import (
    REVIEW_FILTERS,
    ai_review_queue,
    ai_review_run,
    ai_review_runs,
    ensure_prediction_assignment,
    next_unreviewed_prediction,
    prediction_review_workspace,
    record_outcome_review,
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
from hansard_annotator.web.tasks.frozen_samples import load_frozen_sample
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
        if field_type == "evidence_span":
            raw = form.get(key)
            try:
                decoded = json.loads(str(raw or "[]"))
            except json.JSONDecodeError:
                decoded = None
            payload[key] = decoded
        elif field_type == "multiple_taxonomy":
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
        if field_type == "evidence_span":
            raw = form.get(key)
            try:
                decoded = json.loads(str(raw or "[]"))
            except json.JSONDecodeError:
                decoded = None
            payload[key] = decoded
        elif field_type == "multiple_taxonomy":
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
    suggested = cast(dict[str, object], metadata.get("suggested_values") or {})
    if not suggested:
        suggested = {
            key: prediction.get(key)
            for key in (
                "is_non_policy",
                "primary_australian_domain",
                "secondary_australian_domains",
            )
        }
    payload.update(suggested)
    if (
        prediction.get("prediction_task") == "policy"
        and prediction["primary_australian_domain"] == "AU_OTHER_REVIEW"
    ):
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


@router.get("/ai-review")
def review_projects(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    status: str = "unreviewed",
) -> object:
    if status not in REVIEW_FILTERS:
        status = "unreviewed"
    return _templates(request).TemplateResponse(
        request,
        "annotations/ai_review_projects.html",
        {"principal": principal, "runs": ai_review_runs(settings), "status": status},
    )


@router.get("/ai-review/{run_id}")
def review_project(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    run_id: str,
    status: str = "unreviewed",
    primary_domain: str | None = None,
    is_non_policy: bool | None = None,
    hostility_present: str | None = None,
    hostility_target: str | None = None,
    year: int | None = None,
    hostility_failure: str | None = None,
    sample: str | None = None,
    random_sample: bool = False,
    sample_seed: str | None = None,
) -> object:
    if status not in REVIEW_FILTERS:
        status = "unreviewed"
    run = ai_review_run(settings, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="AI review project not found")
    if hostility_present not in {None, "", "YES", "NO", "UNSURE"}:
        raise HTTPException(status_code=422, detail="invalid hostility presence filter")
    if hostility_target not in {
        None,
        "",
        "OPPOSING_PARTISAN_TARGET",
        "OTHER_POLITICAL_TARGET",
        "NON_POLITICAL_TARGET",
        "UNSURE",
    }:
        raise HTTPException(status_code=422, detail="invalid hostility target filter")
    if hostility_failure not in {None, "", "evidence", "provider", "structured"}:
        raise HTTPException(status_code=422, detail="invalid hostility failure filter")
    if sample not in {None, "", "development", "confirmation"}:
        raise HTTPException(status_code=422, detail="invalid frozen sample filter")
    if year is not None and not 1901 <= year <= 2100:
        raise HTTPException(status_code=422, detail="invalid speech year filter")
    if random_sample and not sample_seed:
        query_values = dict(request.query_params)
        query_values["random_sample"] = "true"
        query_values["sample_seed"] = secrets.token_hex(12)
        return RedirectResponse(
            f"/ai-review/{quote(run_id, safe='')}?{urlencode(query_values)}",
            status_code=303,
        )
    if random_sample and (
        sample_seed is None
        or len(sample_seed) > 64
        or not sample_seed.isascii()
        or not sample_seed.isalnum()
    ):
        raise HTTPException(status_code=422, detail="invalid random review seed")
    sample_turn_keys: list[str] | None = None
    if sample in {"development", "confirmation"}:
        sample_id = (
            "hostility-development-100"
            if sample == "development"
            else "hostility-luna-confirmation-100"
        )
        sample_file = Path(__file__).resolve().parents[4] / "config" / "research_samples" / f"{sample_id}.json"
        frozen_sample = load_frozen_sample(sample_file, sample_id)
        if sample == "confirmation" and frozen_sample.sample_sha256 != (
            "482d2951253840c5a294be028f6a61891693b6826ac3bdbebb087e71aa5c3c48"
        ):
            raise HTTPException(status_code=409, detail="Frozen Luna confirmation sample identity changed")
        sample_turn_keys = [turn.turn_key for turn in frozen_sample.turns]
    rows = ai_review_queue(
        settings,
        principal,
        review_filter=status,
        run_id=run_id,
        prediction_task=str(run["prediction_task"]),
        primary_domain=primary_domain,
        is_non_policy=is_non_policy,
        hostility_present=hostility_present,
        hostility_target=hostility_target,
        speech_year=year,
        hostility_failure=hostility_failure,
        sample_turn_keys=sample_turn_keys,
        random_seed=sample_seed if random_sample else None,
    )
    return _templates(request).TemplateResponse(
        request,
        "annotations/ai_queue.html",
        {
            "principal": principal,
            "selected_run": run,
            "rows": rows,
            "active_filter": status,
            "active_run_id": run_id,
            "primary_domain": primary_domain or "",
            "is_non_policy": is_non_policy,
            "hostility_present": hostility_present or "",
            "hostility_target": hostility_target or "",
            "year": year or "",
            "hostility_failure": hostility_failure or "",
            "sample": sample or "",
            "random_sample": random_sample,
            "sample_seed": sample_seed or "",
        },
    )


@router.get("/ai-reviews")
def legacy_review_queue(
    status: str = "unreviewed",
    run_id: str | None = None,
    primary_domain: str | None = None,
    is_non_policy: bool | None = None,
) -> RedirectResponse:
    """Keep old links usable while moving review navigation to the project selector."""
    if run_id:
        if status not in REVIEW_FILTERS:
            status = "unreviewed"
        query_values: dict[str, str] = {"status": status}
        if primary_domain:
            query_values["primary_domain"] = primary_domain
        if is_non_policy is not None:
            query_values["is_non_policy"] = str(is_non_policy).lower()
        query = urlencode(query_values)
        return RedirectResponse(
            f"/ai-review/{quote(run_id, safe='')}?{query}", status_code=303
        )
    if status not in REVIEW_FILTERS:
        status = "unreviewed"
    return RedirectResponse(f"/ai-review?status={status}", status_code=303)


def _review_workspace_or_http(
    settings: WebSettings,
    principal: Principal,
    prediction_id: str,
    run_id: str | None = None,
) -> dict[str, object]:
    try:
        return prediction_review_workspace(
            settings, principal, prediction_id, expected_run_public_id=run_id
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _next_review_destination(
    settings: WebSettings, prediction_id: str, run_id: str
) -> str:
    next_id = next_unreviewed_prediction(settings, prediction_id)
    return (
        f"/ai-review/{run_id}/{next_id}"
        if next_id
        else f"/ai-review/{run_id}?status=unreviewed"
    )


def _workspace_run_id(workspace: dict[str, object]) -> str:
    prediction = cast(dict[str, object], workspace["prediction"])
    return str(prediction["run_public_id"])


def _prediction_decision_status(payload: dict[str, object], suggested_values: object) -> str:
    suggested = cast(dict[str, object], suggested_values)
    decision_values = {key: payload.get(key) for key in suggested}
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


@router.get("/ai-review/{run_id}/{prediction_id}")
def project_ai_review(
    request: Request,
    run_id: str,
    prediction_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    workspace = _review_workspace_or_http(settings, principal, prediction_id, run_id)
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
    if workspace.get("ai_unavailable"):
        raise HTTPException(status_code=409, detail="no valid AI prediction is available to accept")
    assignment_id = ensure_prediction_assignment(settings, principal, prediction_id)
    payload = _accepted_ai_payload(
        {"fields": workspace["fields"], "ai_prediction": workspace["prediction"],
         "suggested_values": workspace["suggested_values"]}
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
    return RedirectResponse(
        _next_review_destination(settings, prediction_id, _workspace_run_id(workspace)),
        status_code=303,
    )


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
    if workspace.get("ai_unavailable"):
        record_outcome_review(
            settings, principal, prediction_id, "human_annotated_after_ai_failure",
            assignment_id=assignment_id,
            annotation_version_id=cast(int, result["annotation_version_id"]),
        )
    else:
        status = _prediction_decision_status(payload, workspace["suggested_values"])
        record_prediction_review(
            settings,
            principal,
            prediction_id,
            status,
            assignment_id=assignment_id,
            annotation_version_id=cast(int, result["annotation_version_id"]),
        )
    return RedirectResponse(
        _next_review_destination(settings, prediction_id, _workspace_run_id(workspace)),
        status_code=303,
    )


@router.post("/ai-reviews/{prediction_id}/flag")
async def flag_prediction(
    request: Request,
    prediction_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    note: Annotated[str, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    workspace = _review_workspace_or_http(settings, principal, prediction_id)
    if workspace.get("ai_unavailable"):
        raise HTTPException(status_code=409, detail="codebook flags require a valid AI prediction")
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
    workspace = _review_workspace_or_http(settings, principal, prediction_id)
    if workspace.get("ai_unavailable"):
        record_outcome_review(settings, principal, prediction_id, "skipped")
    else:
        record_prediction_review(settings, principal, prediction_id, "skipped")
    return RedirectResponse(
        _next_review_destination(settings, prediction_id, _workspace_run_id(workspace)),
        status_code=303,
    )


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
