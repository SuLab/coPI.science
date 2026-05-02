"""Public-facing routes: landing page, waitlist, access-pending."""

import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import User, WaitlistSignup

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Institution mapping for the Cabo collaboration graph. Mirrors the comment
# groupings in src/agent/simulation.py PILOT_LABS.
_SCRIPPS = {
    "su", "wiseman", "grotjahn", "ward", "briney", "forli", "lairson",
    "badran", "kern", "lasker", "lippi", "maillie", "millar", "miller",
    "mravic", "paulson", "pwu", "seiple", "williamson", "wilson",
    "young",  # Calibr-Skaggs is part of Scripps
}
_UCSF = {
    "sali", "larabell", "zaro", "roe", "santi", "wells", "echeverria",
    "fraser", "craik", "stroud", "minor", "manglik", "susa", "capra",
}
_OTHER_INST = {
    "kim": "Stanford",
    "azumaya": "Genentech",
    "nomura": "UC Berkeley",
}

# Cohort cutover for the Cabo retreat graph: matches commit 0ef4741
# ("Reshape PILOT_LABS for Cabo retreat"). All proposals to date share a
# single simulation_run_id, so date is the only way to isolate the new cohort.
CABO_COHORT_START = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _institution_for(agent_id: str) -> str:
    if agent_id in _SCRIPPS:
        return "Scripps"
    if agent_id in _UCSF:
        return "UCSF"
    return _OTHER_INST.get(agent_id, "Other")


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Public landing page. Logged-in users redirect to their profile."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/profile", status_code=302)
    return templates.TemplateResponse(request, "landing.html", {"request": request})


@router.post("/waitlist", response_class=HTMLResponse)
async def waitlist_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    institution: str = Form(""),
    note: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Accept a waitlist signup. Upserts on email."""
    email_clean = (email or "").strip().lower()
    if not EMAIL_RE.match(email_clean):
        return templates.TemplateResponse(
            request,
            "landing.html",
            {
                "request": request,
                "waitlist_error": "Please enter a valid email address.",
                "form_values": {
                    "email": email,
                    "name": name,
                    "institution": institution,
                    "note": note,
                },
            },
            status_code=400,
        )

    result = await db.execute(
        select(WaitlistSignup).where(WaitlistSignup.email == email_clean)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.name = name.strip() or existing.name
        existing.institution = institution.strip() or existing.institution
        existing.note = note.strip() or existing.note
    else:
        db.add(
            WaitlistSignup(
                email=email_clean,
                name=name.strip() or None,
                institution=institution.strip() or None,
                note=note.strip() or None,
            )
        )
    await db.commit()
    logger.info("Waitlist signup: %s", email_clean)

    return templates.TemplateResponse(
        request,
        "landing.html",
        {"request": request, "waitlist_success": True},
    )


@router.get("/access-pending", response_class=HTMLResponse)
async def access_pending(request: Request):
    """Shown after ORCID login when the user is not yet approved."""
    pending_info = request.session.get("pending_access") or {}
    return templates.TemplateResponse(
        request,
        "access_pending.html",
        {
            "request": request,
            "pending_info": pending_info,
        },
    )


@router.post("/access-pending/email", response_class=HTMLResponse)
async def access_pending_email(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Capture an email for a pending-access user who didn't share one via ORCID."""
    pending_info = request.session.get("pending_access") or {}
    user_id = pending_info.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=302)

    email_clean = (email or "").strip().lower()
    if not EMAIL_RE.match(email_clean):
        return templates.TemplateResponse(
            request,
            "access_pending.html",
            {
                "request": request,
                "pending_info": pending_info,
                "email_error": "Please enter a valid email address.",
            },
            status_code=400,
        )

    import uuid as _uuid

    result = await db.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user and not user.email:
        user.email = email_clean
        await db.commit()
        pending_info["email"] = email_clean
        request.session["pending_access"] = pending_info

    return templates.TemplateResponse(
        request,
        "access_pending.html",
        {
            "request": request,
            "pending_info": pending_info,
            "email_saved": True,
        },
    )


@router.get("/cabo-graph", response_class=HTMLResponse)
async def cabo_graph(request: Request, db: AsyncSession = Depends(get_db)):
    """PI collaboration network for the Cabo retreat: nodes = active PIs, edges = joint proposals."""
    nodes_result = await db.execute(
        text(
            "SELECT agent_id, pi_name, bot_name FROM agents "
            "WHERE status='active' ORDER BY pi_name"
        )
    )
    active_rows = nodes_result.fetchall()
    active_ids = {row.agent_id for row in active_rows}

    edges_result = await db.execute(
        text(
            """
            WITH pairs AS (
                SELECT
                    LEAST(agent_a, agent_b)    AS a,
                    GREATEST(agent_a, agent_b) AS b,
                    thread_id,
                    decided_at,
                    summary_text
                FROM thread_decisions
                WHERE outcome = 'proposal'
                  AND decided_at >= :cohort_start
            )
            SELECT
                a, b,
                COUNT(DISTINCT thread_id) AS n,
                MAX(decided_at)           AS last_at,
                (ARRAY_AGG(summary_text ORDER BY decided_at DESC)
                    FILTER (WHERE summary_text IS NOT NULL))[1] AS latest_summary
            FROM pairs
            GROUP BY a, b
            """
        ),
        {"cohort_start": CABO_COHORT_START},
    )

    # Compute degree (unique collaborators) and total proposals per node from edges.
    degree: dict[str, int] = {row.agent_id: 0 for row in active_rows}
    total_proposals: dict[str, int] = {row.agent_id: 0 for row in active_rows}
    links: list[dict] = []
    for r in edges_result:
        if r.a not in active_ids or r.b not in active_ids:
            continue
        links.append(
            {
                "source": r.a,
                "target": r.b,
                "weight": int(r.n),
                "summary": (r.latest_summary or "")[:200],
            }
        )
        degree[r.a] += 1
        degree[r.b] += 1
        total_proposals[r.a] += int(r.n)
        total_proposals[r.b] += int(r.n)

    nodes = [
        {
            "id": row.agent_id,
            "pi": row.pi_name,
            "bot": row.bot_name,
            "institution": _institution_for(row.agent_id),
            "degree": degree[row.agent_id],
            "proposals": total_proposals[row.agent_id],
        }
        for row in active_rows
    ]

    return templates.TemplateResponse(
        request,
        "cabo_graph.html",
        {
            "request": request,
            "graph_json": json.dumps({"nodes": nodes, "links": links}),
            "node_count": len(nodes),
            "edge_count": len(links),
        },
    )
