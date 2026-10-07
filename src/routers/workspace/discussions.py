"""Staff discussion view and unchanged effective-admin historical export."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user
from src.models import ProposalReview, User
from src.services.directory import MAX_PAGE, build_discussions_view
from src.services.export_markdown import render_export_markdown
from src.services.thread_panel import panel_cards_by_thread
from src.web.page_context import workspace_context
from src.web.templating import make_templates

_AGENT_FILTER = Query(default=[])
router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)
_PAGE = Query(1, ge=1, le=MAX_PAGE)


@router.get("/discussions", response_class=HTMLResponse, name="workspace_discussions")
async def workspace_discussions(
    request: Request,
    run_id: str | None = None,
    channel_filter: str | None = None,
    status_filter: str | None = None,
    agent_filter: list[str] = _AGENT_FILTER,
    export: str = "",
    page: int = _PAGE,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Discussion summary: threads grouped by status."""
    if export and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    view = await build_discussions_view(
        db,
        run_id=run_id,
        channel_filter=channel_filter,
        status_filter=status_filter,
        agent_filter=agent_filter,
        # The export lists every thread; the HTML page shows one page.
        page=None if export else page,
    )

    # No simulation runs exist at all: render the normal HTML page and return
    # here, BEFORE the `if export:` branch below. This ordering is
    # deliberate and matches the pre-extraction code — an export request
    # must never swallow the no-runs page (e.g. GET
    # /admin/discussions?export=true with zero SimulationRun rows previously
    # returned the HTML page, not a "No proposals found" text attachment).
    if view["selected_run_id"] is None:
        return templates.TemplateResponse(
            request,
            "workspace/discussions.html",
            workspace_context(
                request,
                current_user,
                section="discussions",
                runs=view["runs"],
                selected_run_id=view["selected_run_id"],
                threads=view["threads"],
                counts=view["counts"],
                channels=view["channels"],
                agents=view["agents"],
                channel_filter=view["channel_filter"],
                status_filter=view["status_filter"],
                agent_filter=view["agent_filter"],
                page=view["page"],
                page_count=view["page_count"],
                thread_total=view["thread_total"],
                # No run selected means no threads, so no panel to summarize —
                # but the keys must still be present: the shared threads body
                # reads them on every render. Nothing was read, so nothing was
                # capped; `panel_row_limit` is only ever printed inside the
                # truncation notice, which this cannot reach.
                panel_by_thread={},
                panel_truncated=False,
                panel_row_limit=0,
                admin_view=current_user.is_admin,
            ),
        )

    if export:
        proposals = []
        for t in view["threads"]:
            d = t.get("decision")
            if not d or not d.summary_text:
                continue
            proposals.append(
                {
                    "decision_id": d.id,
                    "channel": t["channel_name"],
                    "agent_a": d.agent_a,
                    "agent_b": d.agent_b,
                    "outcome": d.outcome,
                    "date": d.decided_at.strftime("%Y-%m-%d %H:%M UTC"),
                    "summary": d.summary_text.strip(),
                    "summary_html": render_export_markdown(d.summary_text.strip()),
                    "reviews": [],
                }
            )

        # Historical PI reviews of legacy proposals. The review routes are
        # retired and the discussions page no longer shows them, so the export
        # is the only place these rows stay readable. One query for the whole
        # export.
        by_decision = {p["decision_id"]: p for p in proposals}
        if by_decision:
            review_rows = (
                (
                    await db.execute(
                        select(ProposalReview)
                        .where(ProposalReview.thread_decision_id.in_(list(by_decision)))
                        .order_by(ProposalReview.reviewed_at)
                    )
                )
                .scalars()
                .all()
            )
            for rev in review_rows:
                by_decision[rev.thread_decision_id]["reviews"].append(
                    {
                        "agent_id": rev.agent_id,
                        "rating": rev.rating,
                        "comment": rev.comment,
                        "date": rev.reviewed_at.strftime("%Y-%m-%d %H:%M UTC"),
                    }
                )

        if export == "html":
            return templates.TemplateResponse(
                request,
                "discussions/export.html",
                {"request": request, "proposals": proposals},
                headers={"Content-Disposition": "attachment; filename=proposals.html"},
            )

        # Default: plain text
        from fastapi.responses import PlainTextResponse

        lines = []
        for p in proposals:
            lines.append(f"{'=' * 72}")
            lines.append(f"Channel: #{p['channel']}")
            lines.append(f"Agents: {p['agent_a'].capitalize()}Bot + {p['agent_b'].capitalize()}Bot")
            lines.append(f"Outcome: {p['outcome']}")
            lines.append(f"Date: {p['date']}")
            lines.append("")
            lines.append(p["summary"])
            lines.append("")
            if p["reviews"]:
                lines.append("PI reviews:")
                for r in p["reviews"]:
                    comment = f' — "{r["comment"]}"' if r["comment"] else ""
                    lines.append(
                        f"- {r['agent_id'].capitalize()}Bot's PI: {r['rating']}/4{comment} ({r['date']})"
                    )
                lines.append("")
        if not lines:
            lines.append("No proposals found with current filters.")
        return PlainTextResponse(
            "\n".join(lines),
            headers={"Content-Disposition": "attachment; filename=proposals.txt"},
        )

    # What the panel was asked, and what it said, per thread. Keyed on
    # thread_id, which is the ROOT message's ts — the same value the threads
    # list calls `message_ts` (src/services/directory.py::build_discussions_view).
    #
    # ONE query serves both the compact per-row indicator and the cards in the
    # expanded row: the cards carry domain and verdict_signal, which is all the
    # indicator reads. It is scoped to the threads this render is actually
    # showing, so the filters above narrow the consult read too.
    panel = await panel_cards_by_thread(
        db,
        view["selected_run_id"],
        [t["message_ts"] for t in view["threads"]],
        admin_view=current_user.is_admin,
    )

    return templates.TemplateResponse(
        request,
        "workspace/discussions.html",
        workspace_context(
            request,
            current_user,
            section="discussions",
            runs=view["runs"],
            selected_run_id=view["selected_run_id"],
            threads=view["threads"],
            counts=view["counts"],
            channels=view["channels"],
            agents=view["agents"],
            channel_filter=view["channel_filter"],
            status_filter=view["status_filter"],
            agent_filter=view["agent_filter"],
            page=view["page"],
            page_count=view["page_count"],
            thread_total=view["thread_total"],
            all_runs_refused=view["all_runs_refused"],
            panel_by_thread=panel.by_thread,
            # A capped panel read must not look like an unconsulted page.
            panel_truncated=panel.truncated,
            panel_row_limit=panel.limit,
            # Unlocks the verbatim specialist reply inside each panel card.
            # Effective managers render the same shared body without admin diagnostics;
            # the value is also withheld server-side (see
            # src/services/thread_panel.py).
            admin_view=current_user.is_admin,
        ),
    )
