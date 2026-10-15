"""Canonical shared workspace router."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from src.dependencies import get_review_user
from src.web.help_intro import load_help_intro
from src.web.urls import page_url

from . import (
    activity,
    assessments,
    discussions,
    pi_bots,
    pi_directory,
    pi_evidence,
    pi_profile,
    prompt_suggestions,
    staff,
)

# load_help_intro sets the first-sign-in prompt's state for every workspace page; it never
# refuses a request, so each route's own gate still decides access.
router = APIRouter(dependencies=[Depends(load_help_intro)])
_REVIEW = Depends(get_review_user)


@router.get("", name="workspace_root")
async def workspace_root(request: Request, current_user=_REVIEW):
    return RedirectResponse(
        page_url(request, "workspace_pis" if current_user.is_staff else "workspace_assessments"),
        status_code=302,
    )


for feature in (
    pi_directory,
    pi_profile,
    pi_evidence,
    pi_bots,
    assessments,
    activity,
    discussions,
    prompt_suggestions,
    staff,
):
    router.include_router(feature.router)
