"""Read-only admin page of the authorship-claim check's rows (F31; the 2026-10-15
publication-handling design §11 R6.17). GET only: no route here writes, and a read records
nothing. The guard is ``get_admin_user``, as on every /admin page, so managers, reviewers,
PIs and an admin impersonating one of them are refused."""

import uuid

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AuthorshipClaimCheck, SimulationRun, User
from src.models.authorship_claim_check import MODES, OUTCOMES
from src.routers.admin._common import _ADMIN, _DB, _template_context, router, templates
from src.services.directory import MAX_PAGE

_PAGE = Query(1, ge=1, le=MAX_PAGE)
CLAIM_CHECKS_PAGE_SIZE = 100
RUN_OPTIONS_LIMIT = 50
AGENT_FILTER_MAX_CHARS = 50


def _run_filter(run: str | None) -> uuid.UUID | None:
    if not run:
        return None
    try:
        return uuid.UUID(run)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown run filter") from None


def _view_reasons(reasons: object) -> list[tuple[str, str]]:
    """``(code, text)`` pairs of the stored JSON array; anything malformed is dropped."""
    if not isinstance(reasons, list):
        return []
    return [
        (r["code"], r["text"]) for r in reasons
        if isinstance(r, dict) and isinstance(r.get("code"), str) and isinstance(r.get("text"), str)
    ]


@router.get("/authorship-checks", response_class=HTMLResponse, name="admin_authorship_checks")
async def admin_authorship_checks(
    request: Request, run: str | None = None, agent: str | None = None,
    outcome: str | None = None, mode: str | None = None, page: int = _PAGE,
    db: AsyncSession = _DB, current_user: User = _ADMIN,
) -> HTMLResponse:
    """The claim-check rows, newest first, filtered by run, agent, outcome and mode. An
    empty filter means none; an invalid one is a 400."""
    run_id = _run_filter(run)
    if agent and len(agent) > AGENT_FILTER_MAX_CHARS:
        raise HTTPException(status_code=400, detail="Unknown agent filter")
    if outcome and outcome not in OUTCOMES:
        raise HTTPException(status_code=400, detail="Unknown outcome filter")
    if mode and mode not in MODES:
        raise HTTPException(status_code=400, detail="Unknown mode filter")
    c = AuthorshipClaimCheck
    base = []
    if run_id:
        base.append(c.simulation_run_id == run_id)
    if agent:
        base.append(c.agent_id == agent)
    if mode:
        base.append(c.mode == mode)
    filters = [*base, c.outcome == outcome] if outcome else base

    total = (await db.execute(select(func.count()).select_from(c).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(c).where(*filters).order_by(c.created_at.desc(), c.id.desc())
        .limit(CLAIM_CHECKS_PAGE_SIZE).offset((page - 1) * CLAIM_CHECKS_PAGE_SIZE)
    )).scalars().all()
    counts = dict((await db.execute(
        select(c.outcome, func.count()).where(*base).group_by(c.outcome)
    )).all())
    run_options = (await db.execute(
        select(SimulationRun.id, SimulationRun.started_at)
        .where(exists().where(c.simulation_run_id == SimulationRun.id))
        .order_by(SimulationRun.started_at.desc()).limit(RUN_OPTIONS_LIMIT)
    )).all()
    agent_options = (await db.execute(
        select(c.agent_id).distinct().order_by(c.agent_id)
    )).scalars().all()

    view_rows = [
        {
            "created_at": r.created_at, "run_id": r.simulation_run_id, "agent_id": r.agent_id,
            "phase": r.phase, "mode": r.mode, "outcome": r.outcome,
            "reasons": _view_reasons(r.reasons), "excerpt": r.excerpt,
        }
        for r in rows
    ]
    filter_params = {k: v for k, v in (("run", run), ("agent", agent), ("outcome", outcome),
                                         ("mode", mode)) if v}
    return templates.TemplateResponse(
        request,
        "admin/authorship_checks.html",
        _template_context(
            request, current_user, active_admin="authorship-checks",
            rows=view_rows, counts=counts, total=total, page=page,
            page_count=max(1, -(-total // CLAIM_CHECKS_PAGE_SIZE)),
            run_options=run_options, agent_options=agent_options,
            outcomes=OUTCOMES, modes=MODES,
            run_filter=run or "", agent_filter=agent or "",
            outcome_filter=outcome or "", mode_filter=mode or "",
            filter_params=filter_params,
        ),
    )
