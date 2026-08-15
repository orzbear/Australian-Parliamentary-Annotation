"""Project and batch browser routes."""

from __future__ import annotations

import json
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import enforce_csrf, get_settings, require_principal
from hansard_annotator.web.exports.service import (
    AnnotationExportBundle,
    ExportType,
    build_ai_codebook_export,
    build_annotation_export,
)
from hansard_annotator.web.projects.permissions import PermissionDenied
from hansard_annotator.web.projects.service import (
    create_project,
    get_project,
    list_projects,
    project_options,
    set_membership,
    transition_project,
)
from hansard_annotator.web.tasks.schemas import SelectionCriteria
from hansard_annotator.web.tasks.service import (
    assign_to_user,
    generate_batch,
    preview_batch,
)

router = APIRouter(prefix="/projects")


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


@router.get("")
def project_list(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    return _templates(request).TemplateResponse(
        request,
        "projects/list.html",
        {"principal": principal, "projects": list_projects(settings, principal)},
    )


@router.get("/new")
def project_new(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    if not principal.is_admin:
        return RedirectResponse("/projects", status_code=303)
    return _templates(request).TemplateResponse(
        request,
        "projects/new.html",
        {"principal": principal, **project_options(settings)},
    )


@router.post("")
async def project_create(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    corpus_id: Annotated[int, Form()],
    preprocessing_run_id: Annotated[int, Form()],
    annotation_schema_version_id: Annotated[int, Form()],
    source_project_id: Annotated[int | None, Form()] = None,
) -> object:
    await enforce_csrf(request, principal, settings)
    project = create_project(
        settings,
        principal,
        slug=slug,
        name=name,
        description=description,
        mode=mode,
        corpus_id=corpus_id,
        preprocessing_run_id=preprocessing_run_id,
        annotation_schema_version_id=annotation_schema_version_id,
        source_project_id=source_project_id,
    )
    return RedirectResponse(f"/projects/{project['id']}", status_code=303)


@router.get("/{project_id}")
def project_detail(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    return _templates(request).TemplateResponse(
        request,
        "projects/detail.html",
        {
            "principal": principal,
            "project": get_project(settings, principal, project_id),
        },
    )


@router.post("/{project_id}/lifecycle")
async def project_lifecycle(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    target: Annotated[str, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    transition_project(settings, principal, project_id, target)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _download_response(bundle: AnnotationExportBundle) -> Response:
    return Response(
        content=bundle.content,
        media_type=bundle.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{project_id}/exports/annotations")
async def project_annotation_export(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    export_type: Annotated[str, Form()],
) -> Response:
    await enforce_csrf(request, principal, settings)
    if export_type not in {"simple_csv", "ai_codebook"}:
        raise HTTPException(status_code=422, detail="Unknown annotation export type")
    try:
        bundle = build_annotation_export(
            settings, principal, project_id, cast(ExportType, export_type)
        )
    except PermissionDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _download_response(bundle)


@router.post("/{project_id}/exports/ai-codebook")
async def project_ai_codebook_export(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> Response:
    """Retain existing bookmarked/forms behavior during the prototype amendment."""
    await enforce_csrf(request, principal, settings)
    try:
        bundle = build_ai_codebook_export(settings, principal, project_id)
    except PermissionDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _download_response(bundle)


@router.post("/{project_id}/members")
async def project_member(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    user_id: Annotated[int, Form()],
    role: Annotated[str, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    set_membership(settings, principal, project_id, user_id, role)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _criteria(raw: str) -> SelectionCriteria:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("criteria must be valid JSON") from error
    try:
        return SelectionCriteria.model_validate(value)
    except ValidationError as error:
        raise ValueError(str(error)) from error


@router.post("/{project_id}/batches/preview")
async def batch_preview(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    criteria_json: Annotated[str, Form()],
) -> object:
    await enforce_csrf(request, principal, settings)
    result = preview_batch(settings, principal, project_id, _criteria(criteria_json))
    return _templates(request).TemplateResponse(
        request, "projects/_batch_preview.html", {"result": result}
    )


@router.post("/{project_id}/batches")
async def batch_generate(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    name: Annotated[str, Form()],
    criteria_json: Annotated[str, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    generate_batch(
        settings,
        principal,
        project_id,
        name=name,
        criteria=_criteria(criteria_json),
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/assignments")
async def assignment_allocate(
    request: Request,
    project_id: int,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_principal)],
    assignment_id: Annotated[int, Form()],
    user_id: Annotated[int, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    assign_to_user(settings, principal, project_id, assignment_id, user_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)
