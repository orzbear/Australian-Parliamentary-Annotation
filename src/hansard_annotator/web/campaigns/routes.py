"""Administrator-only AI annotation campaign routes."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from hansard_annotator.web.auth.models import Principal
from hansard_annotator.web.campaigns.phase4e import (
    authorize_full_production,
    configured_orchestration_limits,
    orchestration_tick,
    plan_campaign_chunks,
    refresh_campaign_batch_status,
    set_orchestration_paused,
)
from hansard_annotator.web.campaigns.service import (
    DEFAULT_EXCLUDE_HUMAN_ANNOTATED,
    CampaignError,
    approved_campaign_configurations,
    collect_campaign,
    collect_recovery,
    create_campaign,
    credential_status,
    get_campaign,
    import_campaign,
    list_campaigns,
    list_corpora,
    plan_campaign,
    production_configuration,
    recover_recovery_submission,
    refresh_campaign,
    refresh_recovery,
    repair_campaign_results,
    resolve_campaign_provider_refusals,
    submit_campaign,
    submit_recovery,
    transition_campaign,
    validate_campaign,
)
from hansard_annotator.web.config import WebSettings
from hansard_annotator.web.dependencies import enforce_csrf, get_settings, require_admin

router = APIRouter(prefix="/ai-annotation")


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    return HTTPException(status_code=409, detail=str(error))


def _notice_message(code: str | None, campaign: dict[str, object]) -> str | None:
    messages = {
        "plan-created": "Plan created.",
        "campaign-approved": "Campaign approved.",
        "batch-submitted": "Batch submitted.",
        "status-refreshed": "Provider status refreshed.",
        "results-collected": "Results collected.",
        "recovery-submitted": "Paid targeted recovery submitted.",
        "recovery-status-refreshed": "Recovery provider status refreshed.",
        "recovery-collected": "Recovery results collected and effective validation refreshed.",
        "structural-repair-applied": (
            "Approved deterministic structural repairs applied locally; no provider call was made."
        ),
        "provider-refusals-resolved": (
            "Confirmed provider refusals recorded as terminal abstentions; no provider call was made."
        ),
        "chunks-planned": "Deterministic full-corpus child Batches planned offline.",
        "full-authorized": "Full-production orchestration authorized.",
        "orchestration-paused": "New child Batch submissions paused.",
        "orchestration-resumed": "Child Batch orchestration resumed.",
        "orchestration-ticked": "One orchestration iteration completed.",
        "campaign-cancelled": "Campaign cancelled.",
    }
    if code == "validation-passed":
        return f"Validation passed: {campaign.get('valid_count', 0)} / {campaign.get('planned_count', 0)}."
    if code == "predictions-imported":
        return f"Predictions imported: {campaign.get('valid_count', 0)}."
    return messages.get(code or "")


@router.get("")
def campaign_list(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> object:
    return _templates(request).TemplateResponse(
        request,
        "campaigns/list.html",
        {
            "principal": principal,
            "campaigns": list_campaigns(settings, principal),
            "credentials": credential_status(),
        },
    )


@router.get("/new")
def campaign_new(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> object:
    return _templates(request).TemplateResponse(
        request,
        "campaigns/new.html",
        {
            "principal": principal,
            "corpora": list_corpora(settings),
            "configuration": production_configuration(),
            "configurations": approved_campaign_configurations(),
            "credentials": credential_status(),
        },
    )


@router.post("")
async def campaign_create(
    request: Request,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    name: Annotated[str, Form()],
    corpus_id: Annotated[int, Form()],
    configuration_id: Annotated[str, Form()] = "policy-v2",
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        campaign = create_campaign(
            settings,
            principal,
            name=name,
            corpus_id=corpus_id,
            configuration_id=configuration_id,
        )
    except (CampaignError, LookupError, PermissionError) as error:
        raise _http_error(error) from error
    return RedirectResponse(f"/ai-annotation/{campaign['public_id']}", status_code=303)


@router.get("/{campaign_id}")
def campaign_detail(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    notice: str | None = None,
    chunk_page: int = 1,
) -> object:
    try:
        campaign = get_campaign(
            settings, principal, campaign_id, chunk_page=chunk_page
        )
    except (CampaignError, LookupError, PermissionError) as error:
        raise _http_error(error) from error
    ceiling, safety_factor, max_chunk_records = configured_orchestration_limits()
    return _templates(request).TemplateResponse(
        request,
        "campaigns/detail.html",
        {
            "principal": principal,
            "campaign": campaign,
            "credentials": credential_status(),
            "notice_message": _notice_message(notice, campaign),
            "phase4e_defaults": {
                "enqueued_token_ceiling": ceiling,
                "safety_factor": safety_factor,
                "max_chunk_records": max_chunk_records,
            },
        },
    )


@router.post("/{campaign_id}/provider-status")
async def campaign_provider_status(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> object:
    await enforce_csrf(request, principal, settings)
    try:
        campaign = get_campaign(settings, principal, campaign_id)
        active_recovery = campaign.get("active_recovery")
        if isinstance(active_recovery, dict) and active_recovery.get("status") in {
            "SUBMITTED",
            "RUNNING",
        }:
            refresh_recovery(settings, principal, campaign_id)
            campaign = get_campaign(settings, principal, campaign_id)
        elif campaign["status"] in {"SUBMITTED", "RUNNING"}:
            refresh_campaign(settings, principal, campaign_id)
            campaign = get_campaign(settings, principal, campaign_id)
    except (CampaignError, LookupError, PermissionError, ValueError) as error:
        raise _http_error(error) from error
    return _templates(request).TemplateResponse(
        request,
        "campaigns/_provider_status.html",
        {"principal": principal, "campaign": campaign},
    )


@router.post("/{campaign_id}/plan")
async def campaign_plan(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    planned_count: Annotated[int, Form()],
    seed: Annotated[str, Form()],
    exclude_human_annotated: Annotated[bool, Form()] = DEFAULT_EXCLUDE_HUMAN_ANNOTATED,
    retry_provider_refusals: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        plan_campaign(
            settings,
            principal,
            campaign_id,
            limit=planned_count,
            seed=seed,
            exclude_human_annotated=exclude_human_annotated,
            retry_provider_refusals=retry_provider_refusals,
        )
    except (CampaignError, LookupError, PermissionError) as error:
        raise _http_error(error) from error
    return RedirectResponse(
        f"/ai-annotation/{campaign_id}?notice=plan-created", status_code=303
    )


@router.post("/{campaign_id}/ready")
async def campaign_ready(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        transition_campaign(settings, principal, campaign_id, "READY")
    except (CampaignError, LookupError, PermissionError) as error:
        raise _http_error(error) from error
    return RedirectResponse(
        f"/ai-annotation/{campaign_id}?notice=campaign-approved", status_code=303
    )


@router.post("/{campaign_id}/cancel")
async def campaign_cancel(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        transition_campaign(settings, principal, campaign_id, "CANCELLED")
    except (CampaignError, LookupError, PermissionError) as error:
        raise _http_error(error) from error
    return RedirectResponse(
        f"/ai-annotation/{campaign_id}?notice=campaign-cancelled", status_code=303
    )


async def _action(
    request: Request,
    settings: WebSettings,
    principal: Principal,
    campaign_id: str,
    operation: str,
    confirmation: str | None = None,
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        if operation == "submit":
            submit_campaign(settings, principal, campaign_id, confirmation or "")
        elif operation == "refresh":
            refresh_campaign(settings, principal, campaign_id)
        elif operation == "collect":
            collect_campaign(settings, principal, campaign_id)
        elif operation == "validate":
            validate_campaign(settings, principal, campaign_id)
        elif operation == "repair-structural":
            repair_campaign_results(settings, principal, campaign_id)
        elif operation == "resolve-provider-refusals":
            resolve_campaign_provider_refusals(settings, principal, campaign_id)
        elif operation == "orchestration-pause":
            set_orchestration_paused(
                settings, principal, campaign_id, paused=True
            )
        elif operation == "orchestration-resume":
            set_orchestration_paused(
                settings, principal, campaign_id, paused=False
            )
        elif operation == "orchestration-tick":
            orchestration_tick(settings, campaign_id)
        elif operation == "import":
            import_campaign(settings, principal, campaign_id)
        elif operation == "retry-invalid":
            submit_recovery(settings, principal, campaign_id, confirmation or "")
        elif operation == "recovery-refresh":
            refresh_recovery(settings, principal, campaign_id)
        elif operation == "recovery-resume":
            recover_recovery_submission(settings, principal, campaign_id)
        elif operation == "recovery-collect":
            collect_recovery(settings, principal, campaign_id)
        else:
            raise CampaignError("unknown campaign action")
    except (CampaignError, LookupError, PermissionError, ValueError) as error:
        raise _http_error(error) from error
    notice = {
        "submit": "batch-submitted",
        "refresh": "status-refreshed",
        "collect": "results-collected",
        "validate": "validation-passed",
        "repair-structural": "structural-repair-applied",
        "resolve-provider-refusals": "provider-refusals-resolved",
        "orchestration-pause": "orchestration-paused",
        "orchestration-resume": "orchestration-resumed",
        "orchestration-tick": "orchestration-ticked",
        "import": "predictions-imported",
        "retry-invalid": "recovery-submitted",
        "recovery-refresh": "recovery-status-refreshed",
        "recovery-resume": "recovery-submitted",
        "recovery-collect": "recovery-collected",
    }[operation]
    return RedirectResponse(
        f"/ai-annotation/{campaign_id}?notice={notice}", status_code=303
    )


@router.post("/{campaign_id}/submit")
async def campaign_submit(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    confirmation: Annotated[str, Form()],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "submit", confirmation)


@router.post("/{campaign_id}/refresh")
async def campaign_refresh(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "refresh")


@router.post("/{campaign_id}/collect")
async def campaign_collect(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "collect")


@router.post("/{campaign_id}/validate")
async def campaign_validate(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "validate")


@router.post("/{campaign_id}/repair-structural")
async def campaign_repair_structural(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "repair-structural")


@router.post("/{campaign_id}/resolve-provider-refusals")
async def campaign_resolve_provider_refusals(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(
        request, settings, principal, campaign_id, "resolve-provider-refusals"
    )


@router.post("/{campaign_id}/plan-chunks")
async def campaign_plan_chunks(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    enqueued_token_ceiling: Annotated[int, Form()],
    safety_factor: Annotated[float, Form()],
    max_chunk_records: Annotated[int, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        plan_campaign_chunks(
            settings,
            principal,
            campaign_id,
            enqueued_token_ceiling=enqueued_token_ceiling,
            safety_factor=safety_factor,
            max_chunk_records=max_chunk_records,
        )
    except (CampaignError, LookupError, PermissionError, ValueError) as error:
        raise _http_error(error) from error
    return RedirectResponse(
        f"/ai-annotation/{campaign_id}?notice=chunks-planned", status_code=303
    )


@router.post("/{campaign_id}/authorize-full")
async def campaign_authorize_full(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    confirmation: Annotated[str, Form()],
    spend_ceiling_usd: Annotated[Decimal, Form()],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        authorize_full_production(
            settings,
            principal,
            campaign_id,
            confirmation=confirmation,
            spend_ceiling_usd=spend_ceiling_usd,
        )
    except (CampaignError, LookupError, PermissionError, ValueError) as error:
        raise _http_error(error) from error
    return RedirectResponse(
        f"/ai-annotation/{campaign_id}?notice=full-authorized", status_code=303
    )


@router.post("/{campaign_id}/orchestration-pause")
async def campaign_orchestration_pause(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "orchestration-pause")


@router.post("/{campaign_id}/orchestration-resume")
async def campaign_orchestration_resume(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "orchestration-resume")


@router.post("/{campaign_id}/orchestration-tick")
async def campaign_orchestration_tick(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "orchestration-tick")


@router.post("/{campaign_id}/refresh-batches")
async def campaign_refresh_batches(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    await enforce_csrf(request, principal, settings)
    try:
        refresh_campaign_batch_status(settings, principal, campaign_id)
    except (CampaignError, LookupError, PermissionError, ValueError) as error:
        raise _http_error(error) from error
    return RedirectResponse(
        f"/ai-annotation/{campaign_id}?notice=status-refreshed", status_code=303
    )


@router.post("/{campaign_id}/import")
async def campaign_import(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "import")


@router.post("/{campaign_id}/retry-invalid")
async def campaign_retry_invalid(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
    confirmation: Annotated[str, Form()],
) -> RedirectResponse:
    return await _action(
        request, settings, principal, campaign_id, "retry-invalid", confirmation
    )


@router.post("/{campaign_id}/recovery-refresh")
async def campaign_recovery_refresh(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(
        request, settings, principal, campaign_id, "recovery-refresh"
    )


@router.post("/{campaign_id}/recovery-resume")
async def campaign_recovery_resume(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(request, settings, principal, campaign_id, "recovery-resume")


@router.post("/{campaign_id}/recovery-collect")
async def campaign_recovery_collect(
    request: Request,
    campaign_id: str,
    settings: Annotated[WebSettings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_admin)],
) -> RedirectResponse:
    return await _action(
        request, settings, principal, campaign_id, "recovery-collect"
    )
