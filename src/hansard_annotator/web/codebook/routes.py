"""Read-only Codebook & Taxonomy routes."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.codebook.service import (
    DownloadKind,
    canonical_download,
    production_codebook,
)
from hansard_annotator.web.dependencies import require_admin, require_principal

router = APIRouter(prefix="/codebook")


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


@router.get("")
def codebook_page(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
) -> object:
    return _templates(request).TemplateResponse(
        request,
        "codebook/index.html",
        {"principal": principal, **production_codebook()},
    )


@router.get("/downloads/{kind}")
def codebook_download(
    kind: str,
    _principal: Annotated[Principal, Depends(require_admin)],
) -> Response:
    try:
        artifact = canonical_download(cast(DownloadKind, kind))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        artifact.content,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
