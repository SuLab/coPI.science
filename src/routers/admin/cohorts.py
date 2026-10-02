"""Admin cohort routes."""

import re
import uuid
from typing import Any
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.config import get_settings
from src.database import get_db
from src.dependencies import get_admin_user
from src.models import (
    COHORT_ACTION_AGENT_ADDED,
    COHORT_ACTION_AGENT_REMOVED,
    COHORT_ACTION_CREATED,
    COHORT_ACTION_DELETED,
    COHORT_ACTION_TOPOLOGY_SNAPSHOT,
    AgentRegistry,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
    User,
)
from src.routers.admin._common import _ADMIN, _DB, _template_context, router, templates
from src.services.cohort_gate_inputs import load_gate_inputs
from src.services.cohorts import compute_gates, record_cohort_audit_event, summarise_gates
from src.web.flash import flash

# ---------------------------------------------------------------------------
# Cohorts — admin-managed groups gating which agents interact during simulation.
#
# The gate is an agent-BEHAVIOUR filter, never access control: it changes what an
# agent acts on, never what a human can read. Nothing in this section may be reused
# to scope a PI-facing view. See specs/cohort-system-v2.md §6.2.
#
# Enforcement only happens in the running simulation, and only when
# settings.cohort_isolation_enabled is True. Membership edits are picked up live on
# the engine's roster-sync cadence (~30s) — no restart. Filtering is forward-only:
# adding an agent to a cohort does not reveal the backlog it missed while excluded,
# because the agent's cursor has already advanced past it (v2 §6.3).
# ---------------------------------------------------------------------------

# Cohort name: lowercase alphanumeric + hyphens, max 48 chars (slug style).
_COHORT_NAME_RE = re.compile(r"^[a-z0-9-]{1,48}$")


# Starlette's request.form() defaults to max_fields=1000. The topology matrix
# posts one marker per rendered row and column plus one value per ticked cell,
# so the payload is agents + cohorts + ticked — but "ticked" alone will pass
# 1,000 on a large enough roster, so the limit is raised rather than relied on.
_TOPOLOGY_MAX_FIELDS = 50_000



async def _cohort_gate_context(db: AsyncSession) -> dict[str, Any]:
    """Preview of the gate the engine will compute from the current topology.

    Uses the same ``compute_gates`` the engine uses, so the preview cannot drift from
    the behaviour. The roster is the engine's roster query (active, and pi_lab rows
    linked to a user), so an inactive agent shows as absent rather than as
    unrestricted. See v2 §12.
    """
    settings = get_settings()
    inputs = await load_gate_inputs(db)

    gates, preflight_error = compute_gates(
        membership_rows=inputs.membership_rows,
        agent_ids=inputs.agent_ids,
        isolation_enabled=settings.cohort_isolation_enabled,
        policy=settings.cohort_default_policy,
        cohort_count=inputs.cohort_count,
        has_db=True,
    )

    # Most recent topology snapshot written by a running engine — the only way this
    # process can see the engine's in-memory counters (v2 §9 req. 4 / §13.1).
    snapshot = (await db.execute(
        select(CohortAuditEvent)
        .where(CohortAuditEvent.action == COHORT_ACTION_TOPOLOGY_SNAPSHOT)
        .order_by(CohortAuditEvent.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    return {
        "isolation_enabled": settings.cohort_isolation_enabled,
        "default_policy": settings.cohort_default_policy,
        "preflight_error": preflight_error,
        "preview": {
            aid: (None if g is None else sorted(g)) for aid, g in gates.items()
        },
        "summary": summarise_gates(gates),
        "bot_names": inputs.bot_names,
        "snapshot": snapshot,
    }



@router.get("/cohorts", response_class=HTMLResponse)
async def admin_cohorts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """List all cohorts with member counts, plus the live gate preview."""
    result = await db.execute(
        select(Cohort).options(selectinload(Cohort.memberships)).order_by(Cohort.name)
    )
    cohorts = result.scalars().unique().all()

    # Creator names for display
    creator_map: dict[str, str] = {}
    creator_ids = {c.created_by for c in cohorts if c.created_by}
    if creator_ids:
        u_result = await db.execute(select(User).where(User.id.in_(creator_ids)))
        for u in u_result.scalars().all():
            creator_map[str(u.id)] = u.name

    # Dry-run the star-spoke maintainer so the page can offer the wire-up
    # button exactly when a pi_lab is missing its hub-and-spoke cohort (the
    # state that fails _validate_star_topology at run startup).
    from src.services.star_topology import ensure_star_spokes

    try:
        spoke_plan = await ensure_star_spokes(db, apply=False)
        spokes_missing = len(
            set(spoke_plan.created_cohorts)
            | {name for name, _ in spoke_plan.added_members}
        )
    except ValueError:
        spokes_missing = None  # no single scout_hub; the button would refuse too

    return templates.TemplateResponse(
        request,
        "admin/cohorts.html",
        _template_context(
            request,
            current_user,
            active_admin="cohorts",
            cohorts=cohorts,
            creator_map=creator_map,
            spokes_missing=spokes_missing,
            notice=request.query_params.get("notice"),
            gate=await _cohort_gate_context(db),
        ),
    )



@router.post("/cohorts/create")
async def admin_cohort_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Create a new cohort."""
    name = name.strip().lower()
    if not _COHORT_NAME_RE.match(name):
        flash(request, "Invalid name (lowercase letters, numbers, hyphens; max 48)", "error")
        return RedirectResponse(url="/admin/cohorts", status_code=302)
    existing = await db.execute(select(Cohort).where(Cohort.name == name))
    if existing.scalar_one_or_none():
        flash(request, "A cohort with that name already exists", "error")
        return RedirectResponse(url="/admin/cohorts", status_code=302)
    cohort = Cohort(
        name=name,
        description=description.strip() or None,
        created_by=current_user.id,
    )
    db.add(cohort)
    await db.flush()
    await record_cohort_audit_event(
        db,
        action=COHORT_ACTION_CREATED,
        cohort_id=cohort.id,
        cohort_name=cohort.name,
        actor=current_user,
    )
    await db.commit()
    return RedirectResponse(url=f"/admin/cohorts/{cohort.id}", status_code=302)



@router.get("/cohorts/topology", response_class=HTMLResponse)
async def admin_cohort_topology(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Agent x cohort matrix — edit the whole topology in one pass.

    Granular control: every (agent, cohort) pair is a checkbox, so an admin can move
    several agents across several cohorts in one save instead of walking the
    per-cohort add/remove forms. The resulting per-agent gate is shown alongside,
    computed with the engine's own logic. See v2 §12.

    Registered before /cohorts/{cohort_id} so "topology" is not swallowed as a UUID
    path parameter.
    """
    cohorts = (await db.execute(
        select(Cohort).order_by(Cohort.name)
    )).scalars().all()
    agents = (await db.execute(
        select(AgentRegistry).order_by(AgentRegistry.bot_name)
    )).scalars().all()
    rows = (await db.execute(
        select(CohortMembership.cohort_id, CohortMembership.agent_id)
    )).all()
    membership_set = {f"{c}:{a}" for c, a in rows}

    return templates.TemplateResponse(
        request,
        "admin/cohort_topology.html",
        _template_context(
            request,
            current_user,
            active_admin="cohorts",
            cohorts=cohorts,
            agents=agents,
            membership_set=membership_set,
            notice=request.query_params.get("notice"),
            gate=await _cohort_gate_context(db),
        ),
    )



@router.post("/cohorts/topology")
async def admin_cohort_topology_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Apply a whole-matrix edit as a diff against what the form SHOWED (C-07).

    The form posts one ``cell`` per box ticked now (``{cohort_id}:{agent_id}``), one
    ``was_checked`` per box it rendered ticked, one ``present_agent`` per rendered row and one
    ``present_cohort`` per rendered column; the rendered cell set is the cross product of the
    markers, which is what the template renders (an unconditional nested loop). A cell is
    added only when ticked now and not rendered ticked, and removed only when rendered ticked
    and unticked now. Diffing against the current table instead let a second tab, rendered
    before another tab's save, delete every membership that save added. A form from before
    this field existed posts no ``was_checked`` and can therefore only add. Markers instead
    of one hidden input per cell keep the payload at agents + cohorts + memberships fields:
    60x56 posted 3,528 fields and hit Starlette's ``max_fields=1000``.

    Diffing against ``rendered`` rather than against the whole table means a stale
    or partial form can never delete memberships for a cohort or agent it did not
    display — the usual checkbox-matrix data-loss bug. Unknown cohort/agent ids are
    ignored, never written. Every add and remove is audited individually.

    ``present_cohort``/``present_agent`` are filtered down to ids that still exist
    *before* the cross product is built, not after: the product of two
    attacker-controlled lists is multiplicative, so crossing them first and
    validating each resulting cell afterward (the naive approach) lets a payload
    well within ``_TOPOLOGY_MAX_FIELDS`` build a cross product many orders of
    magnitude larger than either list — e.g. 25,000 garbage ids on each side is
    50,000 form fields (under the cap) but a 625-million-entry ``rendered`` set.
    Filtering first bounds the product by the real ``Cohort``/``AgentRegistry`` row
    counts instead. ``ticked`` and ``was_checked`` are filtered the same way for the
    same reason, and so that a ticked cell naming an id that no longer exists is silently ignored
    (as it always was) rather than tripping the "malformed submission" guard below,
    which is reserved for a cell that names two otherwise-valid ids but was never
    part of the rendered cross product at all.
    """
    form = await request.form(max_fields=_TOPOLOGY_MAX_FIELDS)
    ticked = {v for v in form.getlist("cell") if isinstance(v, str)}
    present_agents = {v for v in form.getlist("present_agent") if isinstance(v, str)}
    present_cohorts = {v for v in form.getlist("present_cohort") if isinstance(v, str)}
    was_checked = {v for v in form.getlist("was_checked") if isinstance(v, str)}
    # Checked on the raw, unfiltered marker sets: a genuinely empty submission (no
    # rows or no columns rendered at all) is an error, but a submission naming only
    # since-deleted rows/columns is not — that is just every cell turning out inert,
    # handled below by the (empty) diff loop, not by this guard.
    if not present_agents or not present_cohorts:
        flash(request, "Nothing to save", "error")
        return RedirectResponse(url="/admin/cohorts/topology", status_code=302)

    cohorts_by_id = {
        str(c.id): c for c in (await db.execute(select(Cohort))).scalars().all()
    }
    valid_agents = {
        r[0] for r in (await db.execute(select(AgentRegistry.agent_id))).all()
    }

    def _known_cell(cell: str) -> bool:
        cid, _, aid = cell.partition(":")
        return bool(cid) and bool(aid) and cid in cohorts_by_id and aid in valid_agents

    # Filter BEFORE crossing: bounds the cross product by the current table sizes
    # rather than by the (attacker-controlled) lengths of the submitted lists.
    present_cohorts &= cohorts_by_id.keys()
    present_agents &= valid_agents
    ticked = {t for t in ticked if _known_cell(t)}
    was_checked = {t for t in was_checked if _known_cell(t)}

    rendered = {f"{cid}:{aid}" for cid in present_cohorts for aid in present_agents}
    if (ticked | was_checked) - rendered:
        flash(request, "Malformed submission", "error")
        return RedirectResponse(url="/admin/cohorts/topology", status_code=302)

    existing = {
        (str(cid), aid): mid
        for mid, cid, aid in (await db.execute(
            select(CohortMembership.id, CohortMembership.cohort_id,
                   CohortMembership.agent_id)
        )).all()
    }

    added = removed = 0
    for cell in sorted(rendered):
        cid, _, aid = cell.partition(":")
        if not cid or not aid or cid not in cohorts_by_id or aid not in valid_agents:
            continue  # stale form referencing something that no longer exists
        want = cell in ticked
        was = cell in was_checked
        have = (cid, aid) in existing
        if want and not was and not have:
            db.add(CohortMembership(
                cohort_id=uuid.UUID(cid), agent_id=aid, added_by=current_user.id,
            ))
            await record_cohort_audit_event(
                db,
                action=COHORT_ACTION_AGENT_ADDED,
                cohort_id=uuid.UUID(cid),
                cohort_name=cohorts_by_id[cid].name,
                agent_id=aid,
                actor=current_user,
            )
            added += 1
        elif was and not want and have:
            await db.execute(
                sa_delete(CohortMembership).where(
                    CohortMembership.id == existing[(cid, aid)]
                )
            )
            await record_cohort_audit_event(
                db,
                action=COHORT_ACTION_AGENT_REMOVED,
                cohort_id=uuid.UUID(cid),
                cohort_name=cohorts_by_id[cid].name,
                agent_id=aid,
                actor=current_user,
            )
            removed += 1

    if added or removed:
        await db.commit()
    return RedirectResponse(
        url=f"/admin/cohorts/topology?notice={added}+added,+{removed}+removed",
        status_code=302,
    )



@router.post("/cohorts/ensure-star-spokes")
async def admin_ensure_star_spokes(
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """One click: wire every pi_lab into the hub-and-spoke topology.

    Same service the CLI (scripts/ensure_star_spokes.py) drives, with the
    click attributed to the acting admin in cohort_audit_events. Registered
    before /cohorts/{cohort_id} so the literal path is matched, not parsed as
    a UUID. Additive only — anomalies (lab-to-lab contamination, overlong
    slugs) are surfaced in the banner, never "fixed" by deletion.
    """

    from src.services.star_topology import ensure_star_spokes

    try:
        report = await ensure_star_spokes(db, apply=True, actor=current_user)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url="/admin/cohorts", status_code=302)
    await db.commit()
    notice = (
        f"Star spokes: {len(report.created_cohorts)} cohort(s) created, "
        f"{len(report.added_members)} membership(s) added, "
        f"{len(report.complete)} already complete"
    )
    url = f"/admin/cohorts?notice={quote(notice)}"
    if report.anomalies:
        # MAX_FLASH_CHARS (src/web/flash.py) bounds the joined text.
        flash(request, "; ".join(report.anomalies), "error")
    return RedirectResponse(url=url, status_code=302)



@router.get("/cohorts/{cohort_id}", response_class=HTMLResponse)
async def admin_cohort_detail(
    cohort_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Cohort detail: members, add-agent picker, agent->cohort map, audit log."""
    result = await db.execute(
        select(Cohort).options(selectinload(Cohort.memberships)).where(Cohort.id == cohort_id)
    )
    cohort = result.scalar_one_or_none()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")

    # All agents, for the add-agent picker and status display.
    agents_result = await db.execute(
        select(AgentRegistry).order_by(AgentRegistry.bot_name)
    )
    all_agents = agents_result.scalars().all()
    agent_by_id = {a.agent_id: a for a in all_agents}

    member_ids = {m.agent_id for m in cohort.memberships}
    available_agents = [a for a in all_agents if a.agent_id not in member_ids]

    # Adder names for the members table.
    adder_map: dict[str, str] = {}
    adder_ids = {m.added_by for m in cohort.memberships if m.added_by}
    if adder_ids:
        u_result = await db.execute(select(User).where(User.id.in_(adder_ids)))
        for u in u_result.scalars().all():
            adder_map[str(u.id)] = u.name

    # Read-only agent -> cohorts map (all memberships across all cohorts).
    all_memberships = (await db.execute(
        select(CohortMembership.agent_id, Cohort.name)
        .join(Cohort, CohortMembership.cohort_id == Cohort.id)
    )).all()
    agent_cohort_map: dict[str, list[str]] = {}
    for aid, cname in all_memberships:
        agent_cohort_map.setdefault(aid, []).append(cname)

    # Audit log for this cohort. Matched on cohort_id, which outlives the cohort
    # row; a recreated cohort with the same name gets a new id and so a fresh
    # trail, which is the honest reading.
    audit_events = (await db.execute(
        select(CohortAuditEvent)
        .where(CohortAuditEvent.cohort_id == cohort_id)
        .order_by(CohortAuditEvent.created_at.desc())
        .limit(200)
    )).scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/cohort_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="cohorts",
            cohort=cohort,
            agent_by_id=agent_by_id,
            available_agents=available_agents,
            adder_map=adder_map,
            all_agents=all_agents,
            agent_cohort_map=agent_cohort_map,
            audit_events=audit_events,
            notice=request.query_params.get("notice"),
            gate=await _cohort_gate_context(db),
        ),
    )



@router.post("/cohorts/{cohort_id}/delete")
async def admin_cohort_delete(
    cohort_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete a cohort. Refused while it still has members.

    A server-side guard, not just a disabled button: deleting a populated cohort
    cascades its memberships away, silently reshaping the interaction topology of a
    running simulation. Remove the members first so each removal is an audited,
    individually reversible step. See v2 §12.

    A cohort id that does not exist is a 404, matching every other route in this
    module whose path-addressed row is missing (and ``admin_cohort_detail`` for
    this very id). It used to be a bare redirect to the list, which said nothing
    at all — a double-submitted delete looked like it had done the work.

    The cohort row is locked FOR UPDATE, so a delete and an add serialize (RA-12).
    """
    result = await db.execute(
        select(Cohort).options(selectinload(Cohort.memberships)).where(Cohort.id == cohort_id)
        .with_for_update(of=Cohort)
    )
    cohort = result.scalar_one_or_none()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")
    if cohort.memberships:
        flash(
            request,
            f"Remove all {len(cohort.memberships)} members before deleting this cohort",
            "error",
        )
        return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)
    name = cohort.name
    await record_cohort_audit_event(
        db,
        action=COHORT_ACTION_DELETED,
        cohort_id=cohort_id,
        cohort_name=name,
        actor=current_user,
    )
    await db.delete(cohort)
    await db.commit()
    return RedirectResponse(
        url=f"/admin/cohorts?notice=Deleted+cohort+{name}", status_code=302
    )



@router.post("/cohorts/{cohort_id}/add-agent")
async def admin_cohort_add_agent(
    cohort_id: uuid.UUID,
    request: Request,
    agent_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Add an agent to the cohort.

    The cohort row is locked FOR UPDATE, so a delete and an add serialize (RA-12).
    """
    result = await db.execute(select(Cohort).where(Cohort.id == cohort_id).with_for_update())
    cohort = result.scalar_one_or_none()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")

    agent_id = agent_id.strip().lower()
    # Validate the agent exists in the registry.
    agent_exists = await db.execute(
        select(AgentRegistry.id).where(AgentRegistry.agent_id == agent_id)
    )
    if not agent_exists.scalar_one_or_none():
        flash(request, "Unknown agent", "error")
        return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)
    # Reject duplicate membership.
    dup = await db.execute(
        select(CohortMembership.id).where(
            CohortMembership.cohort_id == cohort_id,
            CohortMembership.agent_id == agent_id,
        )
    )
    if dup.scalar_one_or_none():
        flash(request, "Agent is already a member", "error")
        return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)
    db.add(CohortMembership(
        cohort_id=cohort_id,
        agent_id=agent_id,
        added_by=current_user.id,
    ))
    await record_cohort_audit_event(
        db,
        action=COHORT_ACTION_AGENT_ADDED,
        cohort_id=cohort_id,
        cohort_name=cohort.name,
        agent_id=agent_id,
        actor=current_user,
    )
    await db.commit()
    return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)



@router.post("/cohorts/{cohort_id}/remove-agent")
async def admin_cohort_remove_agent(
    cohort_id: uuid.UUID,
    agent_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Remove an agent from the cohort.

    An unknown cohort id is a 404, as everywhere else in this module: the old
    behaviour redirected to ``/admin/cohorts/{cohort_id}``, a detail page that
    then 404s itself — so the user paid for two requests to be told nothing.
    Removing an agent that is not a member is a different case and stays a quiet
    redirect back to the (real) detail page: a stale Remove button is a race the
    admin cannot act on, and the page it returns to already shows the truth.
    """
    cohort = (await db.execute(
        select(Cohort).where(Cohort.id == cohort_id)
    )).scalar_one_or_none()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")
    result = await db.execute(
        select(CohortMembership).where(
            CohortMembership.cohort_id == cohort_id,
            CohortMembership.agent_id == agent_id.strip().lower(),
        )
    )
    membership = result.scalar_one_or_none()
    if membership:
        await record_cohort_audit_event(
            db,
            action=COHORT_ACTION_AGENT_REMOVED,
            cohort_id=cohort_id,
            cohort_name=cohort.name,
            agent_id=membership.agent_id,
            actor=current_user,
        )
        await db.delete(membership)
        await db.commit()
    return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)
