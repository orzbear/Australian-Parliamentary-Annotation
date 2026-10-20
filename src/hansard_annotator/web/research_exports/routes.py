"""Research export hub and controlled download endpoints."""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import enforce_csrf, get_settings, require_admin
from hansard_annotator.web.research_exports.canonical_ai import (
    CanonicalAIExportError,
    create_canonical_full_corpus_export,
)
from hansard_annotator.web.research_exports.service import (
    ResearchDownload,
    ReviewArtifact,
    export_catalog,
    prediction_export,
    provider_refusal_export,
    review_export,
)

router = APIRouter(prefix="/exports")


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _download(bundle: ResearchDownload) -> Response:
    filename = bundle.filename
    if any(character in filename for character in ('"', "\r", "\n", "/", "\\")):
        raise ValueError("unsafe download filename")
    return Response(
        content=bundle.content,
        media_type=bundle.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Exported-At": datetime.now(UTC).isoformat(),
        },
    )


@router.get("")
def exports_page(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> object:
    return _templates(request).TemplateResponse(
        request,
        "exports/index.html",
        {"principal": principal, **export_catalog(settings, principal)},
    )


@router.post("/predictions/{run_id}.csv")
async def download_predictions(
    request: Request,
    run_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> Response:
    await enforce_csrf(request, principal, settings)
    try:
        return _download(prediction_export(settings, principal, run_id))
    except (LookupError, PermissionError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/canonical-ai/{corpus_id}.zip")
async def download_canonical_ai(
    request: Request,
    corpus_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> FileResponse:
    await enforce_csrf(request, principal, settings)
    directory = Path(tempfile.mkdtemp(prefix="canonical-full-corpus-ai-"))
    try:
        export = create_canonical_full_corpus_export(
            settings, principal, corpus_id, directory
        )
    except PermissionError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=403, detail=str(error)) from error
    except LookupError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (CanonicalAIExportError, OSError, ValueError) as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(
        export.zip_path,
        media_type="application/zip",
        filename=f"canonical-full-corpus-ai-{export.manifest['corpus']['slug']}.zip",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True),
    )


@router.post("/reviews/{run_id}")
async def download_review(
    request: Request,
    run_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    artifact: Annotated[str, Form()],
) -> Response:
    await enforce_csrf(request, principal, settings)
    try:
        return _download(review_export(settings, principal, run_id, cast(ReviewArtifact, artifact)))
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/provider-refusals/{run_id}.csv")
async def download_provider_refusals(
    request: Request,
    run_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> Response:
    await enforce_csrf(request, principal, settings)
    try:
        return _download(provider_refusal_export(settings, principal, run_id))
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
