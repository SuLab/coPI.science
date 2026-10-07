"""Legacy manager URLs: guarded GET redirects and same-callable POST aliases.

Canonical implementation lives in src.routers.workspace. The widest router
gate is review access; every staff-only read and all writes have their own gate.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_review_user, get_staff_user
from src.models import OpportunityAssessment, PromptChangeSuggestion, SimulationRun, User
from src.routers.workspace.compatibility import redirect, require_resource
from src.routers.workspace.pi_bots import (
    workspace_activate_agent,
    workspace_mute_pi,
    workspace_provision_slack,
    workspace_unmute_pi,
)
from src.routers.workspace.pi_directory import (
    workspace_create_pi,
    workspace_verify_pi_email,
)
from src.routers.workspace.pi_evidence import (
    workspace_add_company,
    workspace_confirm_company,
    workspace_confirm_no_reporter_profile,
    workspace_delete_company,
    workspace_discover_companies,
    workspace_pin_grant_identity,
    workspace_reject_company,
    workspace_unpin_grant_identity,
    workspace_veto_grant,
    workspace_veto_industry_evidence,
    workspace_veto_orcid_funding,
)
from src.routers.workspace.pi_profile import (
    workspace_accept_candidate,
    workspace_accept_draft,
    workspace_discard_draft,
    workspace_edit_pi_profile,
    workspace_exclude_publication,
    workspace_keep_publication,
    workspace_reexport_persona,
    workspace_regenerate_profile,
    workspace_reject_candidate,
    workspace_restore_publication,
    workspace_retry_profile,
)
from src.services import directory
from src.web.urls import legacy_query, page_url

router=APIRouter(dependencies=[Depends(get_review_user)])
_DB=Depends(get_db)
_REVIEW=Depends(get_review_user)
_STAFF=Depends(get_staff_user)
_PAGE=Query(1,ge=1,le=directory.MAX_PAGE)
# Compatibility reads validate before redirecting; writes use canonical callables.

manager_create_pi = workspace_create_pi
manager_edit_pi_profile = workspace_edit_pi_profile
manager_mute_pi = workspace_mute_pi
manager_unmute_pi = workspace_unmute_pi
manager_verify_pi_email = workspace_verify_pi_email
manager_veto_grant = workspace_veto_grant
manager_pin_grant_identity = workspace_pin_grant_identity
manager_confirm_no_reporter_profile = workspace_confirm_no_reporter_profile
manager_unpin_grant_identity = workspace_unpin_grant_identity
manager_veto_orcid_funding = workspace_veto_orcid_funding
manager_veto_industry_evidence = workspace_veto_industry_evidence
manager_provision_slack = workspace_provision_slack
manager_activate_agent = workspace_activate_agent
manager_retry_profile = workspace_retry_profile
manager_accept_candidate = workspace_accept_candidate
manager_reject_candidate = workspace_reject_candidate
manager_keep_publication = workspace_keep_publication
manager_exclude_publication = workspace_exclude_publication
manager_restore_publication = workspace_restore_publication
manager_accept_draft = workspace_accept_draft
manager_discard_draft = workspace_discard_draft
manager_regenerate_profile = workspace_regenerate_profile
manager_reexport_persona = workspace_reexport_persona
manager_add_company = workspace_add_company
manager_discover_companies = workspace_discover_companies
manager_delete_company = workspace_delete_company
manager_confirm_company = workspace_confirm_company
manager_reject_company = workspace_reject_company


@router.get("", response_class=HTMLResponse, name="legacy_manager_root")
async def manager_root(request: Request):
    return RedirectResponse(url=page_url(request, "workspace_root"), status_code=302)

@router.get("/pis", response_class=HTMLResponse, name="legacy_manager_pis")
async def manager_pis(request: Request, page: int = _PAGE, current_user: User = _REVIEW):
    return RedirectResponse(
        url=page_url(request, "workspace_pis", query=legacy_query(request, "pis", staff=current_user.is_staff)),
        status_code=302,
    )

@router.get("/pis/{user_id}", response_class=HTMLResponse, name="legacy_manager_pi_detail")
async def manager_pi_detail(user_id: uuid.UUID, request: Request, db: AsyncSession = _DB, current_user: User = _REVIEW):
    target = await directory.load_pi_target(db, user_id)
    if target is None or target["user"].user_role != "pi":
        raise HTTPException(status_code=404, detail="PI not found")
    return RedirectResponse(url=page_url(request, "workspace_pi_detail", user_id=user_id, query=legacy_query(request, "pi_detail", staff=current_user.is_staff)), status_code=302)

for _path, _endpoint, _name in (
    ("/pis", manager_create_pi, "legacy_manager_create_pi"),
    ("/pis/{user_id}/profile", manager_edit_pi_profile, "legacy_manager_edit_pi_profile"),
    ("/pis/{user_id}/mute", manager_mute_pi, "legacy_manager_mute_pi"),
    ("/pis/{user_id}/unmute", manager_unmute_pi, "legacy_manager_unmute_pi"),
    ("/pis/{user_id}/verify-email", manager_verify_pi_email, "legacy_manager_verify_pi_email"),
    ("/pis/{user_id}/grants/{grant_id}/veto", manager_veto_grant, "legacy_manager_veto_grant"),
    ("/pis/{user_id}/grant-identity/pin", manager_pin_grant_identity, "legacy_manager_pin_grant_identity"),
    ("/pis/{user_id}/grant-identity/none", manager_confirm_no_reporter_profile, "legacy_manager_confirm_no_reporter_profile"),
    ("/pis/{user_id}/grant-identity/unpin", manager_unpin_grant_identity, "legacy_manager_unpin_grant_identity"),
    ("/pis/{user_id}/orcid-fundings/{funding_id}/veto", manager_veto_orcid_funding, "legacy_manager_veto_orcid_funding"),
    ("/pis/{user_id}/industry/{evidence_id}/veto", manager_veto_industry_evidence, "legacy_manager_veto_industry_evidence"),
    ("/pis/{user_id}/slack/provision", manager_provision_slack, "legacy_manager_provision_slack"),
    ("/pis/{user_id}/activate", manager_activate_agent, "legacy_manager_activate_agent"),
    ("/pis/{user_id}/profile/retry", manager_retry_profile, "legacy_manager_retry_profile"),
    ("/pis/{user_id}/candidates/{candidate_id}/accept", manager_accept_candidate, "legacy_manager_accept_candidate"),
    ("/pis/{user_id}/candidates/{candidate_id}/reject", manager_reject_candidate, "legacy_manager_reject_candidate"),
    ("/pis/{user_id}/publications/{publication_id}/keep", manager_keep_publication, "legacy_manager_keep_publication"),
    ("/pis/{user_id}/publications/{publication_id}/exclude", manager_exclude_publication, "legacy_manager_exclude_publication"),
    ("/pis/{user_id}/publications/{publication_id}/restore", manager_restore_publication, "legacy_manager_restore_publication"),
    ("/pis/{user_id}/draft/accept", manager_accept_draft, "legacy_manager_accept_draft"),
    ("/pis/{user_id}/draft/discard", manager_discard_draft, "legacy_manager_discard_draft"),
    ("/pis/{user_id}/regenerate", manager_regenerate_profile, "legacy_manager_regenerate_profile"),
    ("/pis/{user_id}/persona/reexport", manager_reexport_persona, "legacy_manager_reexport_persona"),
    ("/pis/{user_id}/companies", manager_add_company, "legacy_manager_add_company"),
    ("/pis/{user_id}/companies/discover", manager_discover_companies, "legacy_manager_discover_companies"),
    ("/pis/{user_id}/companies/{company_id}/delete", manager_delete_company, "legacy_manager_delete_company"),
    ("/pis/{user_id}/companies/{company_id}/confirm", manager_confirm_company, "legacy_manager_confirm_company"),
    ("/pis/{user_id}/companies/{company_id}/reject", manager_reject_company, "legacy_manager_reject_company"),
):
    router.add_api_route(_path, _endpoint, methods=["POST"], name=_name)


@router.get('/assessments', name='legacy_manager_assessments')
async def manager_assessments(request: Request, current_user: User = _REVIEW):
    return redirect(request, current_user, 'assessments', 'workspace_assessments', manager=True)

@router.get('/assessments/{assessment_id}', name='legacy_manager_assessment_detail')
async def manager_assessment_detail(assessment_id: uuid.UUID, request: Request, db: AsyncSession = _DB, current_user: User = _REVIEW):
    await require_resource(db, OpportunityAssessment, assessment_id, 'Assessment not found')
    return redirect(request, current_user, 'assessment_detail', 'workspace_assessment_detail', assessment_id=assessment_id, manager=True)

@router.get('/slack-bots', name='legacy_manager_slack_bots')
async def manager_slack_bots(request: Request, current_user: User = _STAFF):
    return redirect(request, current_user, 'slack_bots', 'workspace_slack_bots', manager=True)

@router.get('/discussions', name='legacy_manager_discussions')
async def manager_discussions(request: Request, page: int = _PAGE, current_user: User = _STAFF):
    return redirect(request, current_user, 'discussions', 'workspace_discussions', manager=True)

@router.get('/activity', name='legacy_manager_activity')
async def manager_activity(request: Request, current_user: User = _STAFF):
    return redirect(request, current_user, 'activity', 'workspace_activity', manager=True)

@router.get('/activity/{run_id}', name='legacy_manager_activity_detail')
async def manager_activity_detail(run_id: uuid.UUID, request: Request, page: int = _PAGE, db: AsyncSession = _DB, current_user: User = _STAFF):
    await require_resource(db, SimulationRun, run_id, 'Run not found')
    return redirect(request, current_user, 'activity_detail', 'workspace_activity_detail', run_id=run_id, manager=True)

@router.get('/prompt-suggestions', name='legacy_manager_prompt_suggestions')
async def manager_prompt_suggestions(request: Request, current_user: User = _STAFF):
    return redirect(request, current_user, 'prompt_suggestions', 'workspace_prompt_suggestions', manager=True)

@router.get('/prompt-suggestions/{suggestion_id}', name='legacy_manager_prompt_suggestion_detail')
async def manager_prompt_suggestion_detail(suggestion_id: uuid.UUID, request: Request, db: AsyncSession = _DB, current_user: User = _STAFF):
    await require_resource(db, PromptChangeSuggestion, suggestion_id, 'Suggestion not found')
    return redirect(request, current_user, 'prompt_suggestion_detail', 'workspace_prompt_suggestion_detail', suggestion_id=suggestion_id, manager=True)
