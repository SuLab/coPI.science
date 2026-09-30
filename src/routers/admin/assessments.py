"""Admin opportunity-assessment pages."""

import uuid

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_admin_user
from src.models import User
from src.routers.admin._common import _ADMIN, _DB, _template_context, router, templates
from src.services.assessment_detail import build_assessment_detail
from src.services.directory import list_assessments


@router.get("/assessments", response_class=HTMLResponse)
async def admin_assessments(
    request: Request,
    run_id: str | None = None,
    sort: str | None = None,
    lab: str | None = None,
    review: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """BlackbirdBot's screening verdicts against the Blackbird investment rubric.

    See ``src.services.directory.list_assessments`` for the run-scoping,
    truncation and sort/filter semantics — including why an unrecognized
    ``sort``, ``lab`` or ``review`` renders the default view instead of an
    error. ``review`` is the reviewed/unreviewed sub-tab (spec 2026-09-21 §4).
    """
    view = await list_assessments(db, run_id, sort=sort, lab=lab, review=review)

    return templates.TemplateResponse(
        request,
        "admin/assessments.html",
        _template_context(
            request,
            current_user,
            active_admin="assessments",
            assessments=view["assessments"],
            # The band thresholds and the decline label the legend states, from
            # the rubric document rather than as template literals — the page
            # and the scorer must never be able to disagree about where the
            # "advance" line sits.
            banding=view["banding"],
            rubric_version=view["rubric_version"],
            runs=view["runs"],
            runs_by_id=view["runs_by_id"],
            selected_run_id=view["selected_run_id"],
            show_all_runs=view["show_all_runs"],
            # The sort/lab controls' own state. Forwarded explicitly because
            # this handler allowlists every key it passes (unlike
            # manager_assessments' `**view` splat) — a key added to
            # list_assessments and not added here simply never reaches the
            # page, and Jinja's Undefined would render the control as if no
            # filter were applied.
            sort=view["sort"],
            sort_options=view["sort_options"],
            lab_filter=view["lab_filter"],
            lab_options=view["lab_options"],
            # The review sub-tab and its three counts. Forwarded EXPLICITLY for
            # the reason the comment above gives: omitted here they would be
            # silently-falsy Jinja `Undefined` on this surface and correct on
            # the manager one, which splats the whole view.
            review=view["review"],
            review_counts=view["review_counts"],
            pi_user_ids=view["pi_user_ids"],
            total_count=view["total_count"],
            assessments_limit=view["assessments_limit"],
            drop_counts=view["drop_counts"],
            drops_total=view["drops_total"],
            incomplete_panel_count=view["incomplete_panel_count"],
            dimension_stats=view["dimension_stats"],
            band_counts=view["band_counts"],
            assessment_counts_by_run=view["assessment_counts_by_run"],
            off_rubric_count=view["off_rubric_count"],
        ),
    )



@router.get("/assessments/{assessment_id}", response_class=HTMLResponse)
async def admin_assessment_detail(
    assessment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """One verdict in full, plus the interview that produced it.

    ``admin_view=True`` is what unlocks the two admin-only strands of this page
    — the hub's tool activity (parsed out of ``llm_call_logs``) and each
    specialist's verbatim opinion. /manager/assessments/{id} renders the same
    shared body with it False; see ``src.services.assessment_detail``.
    """
    detail = await build_assessment_detail(
        db, assessment_id, admin_view=True, viewer_is_staff=current_user.is_staff
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return templates.TemplateResponse(
        request,
        "admin/assessment_detail.html",
        _template_context(request, current_user, active_admin="assessments", **detail),
    )
