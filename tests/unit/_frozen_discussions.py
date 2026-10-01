"""Verbatim copy of ``build_discussions_view`` before the Task 119 split.

Frozen as the oracle for tests/unit/test_discussions_split.py. Do not edit.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentMessage, ThreadDecision
from src.services.runs import runs_ordered


async def build_discussions_view(
    db: AsyncSession,
    *,
    run_id: str | None,
    channel_filter: str | None,
    status_filter: str | None,
    agent_filter: list[str],
) -> dict[str, Any]:
    """Discussion summary: threads grouped by status.

    Stops before the router's ``if export:`` branch — export is admin-only,
    returns a different response type (``PlainTextResponse`` / an export
    template) entirely, and consumes ``threads`` from this function's return
    value. The manager router will never pass an export parameter.

    Both the "no simulation runs exist at all" early-return and the normal
    return below yield the same 9 keys (including ``agents`` and
    ``agent_filter``, which the early return used to omit) so a template can
    rely on their presence rather than on Jinja2's lenient ``Undefined``.
    """
    # Pick which simulation run to show
    runs = await runs_ordered(db)

    show_all_runs = run_id == "all"
    selected_run_id = "all" if show_all_runs else None
    if not show_all_runs and run_id:
        try:
            selected_run_id = uuid.UUID(run_id)
        except ValueError:
            pass
    if not selected_run_id and runs:
        selected_run_id = runs[0].id

    if not selected_run_id:
        return {
            "runs": runs,
            "selected_run_id": None,
            "threads": [],
            "counts": {},
            "channels": [],
            "agents": [],
            "channel_filter": channel_filter,
            "status_filter": status_filter,
            "agent_filter": [],
        }

    # Get all root posts (new_post phase, no thread_ts)
    roots_query = select(AgentMessage).where(
        AgentMessage.phase == "new_post",
        AgentMessage.thread_ts.is_(None),
    )
    if not show_all_runs:
        roots_query = roots_query.where(AgentMessage.simulation_run_id == selected_run_id)
    roots_result = await db.execute(roots_query.order_by(AgentMessage.created_at)
    )
    root_posts = roots_result.scalars().all()

    # Get reply counts and replier agent IDs per thread
    reply_query = select(
        AgentMessage.thread_ts,
        func.count(AgentMessage.id).label("reply_count"),
    ).where(AgentMessage.phase == "thread_reply")
    if not show_all_runs:
        reply_query = reply_query.where(AgentMessage.simulation_run_id == selected_run_id)
    reply_counts_result = await db.execute(reply_query.group_by(AgentMessage.thread_ts))
    reply_count_map = {r.thread_ts: r.reply_count for r in reply_counts_result}

    # Get distinct replier agent IDs per thread
    replier_query = select(AgentMessage.thread_ts, AgentMessage.agent_id).where(
        AgentMessage.phase == "thread_reply",
    )
    if not show_all_runs:
        replier_query = replier_query.where(AgentMessage.simulation_run_id == selected_run_id)
    repliers_result = await db.execute(replier_query.distinct())
    replier_map: dict[str, set[str]] = {}
    for r in repliers_result:
        replier_map.setdefault(r.thread_ts, set()).add(r.agent_id)

    # Get thread decisions
    decisions_query = select(ThreadDecision)
    if not show_all_runs:
        decisions_query = decisions_query.where(ThreadDecision.simulation_run_id == selected_run_id)
    decisions_result = await db.execute(decisions_query.order_by(ThreadDecision.decided_at))
    all_decisions = decisions_result.scalars().all()

    # Build a map: thread_id -> final outcome (last decision wins)
    decision_map: dict[str, ThreadDecision] = {}
    for d in all_decisions:
        decision_map[d.thread_id] = d

    # Build thread list
    threads = []
    available_channels = set()
    for post in root_posts:
        ts = post.message_ts
        available_channels.add(post.channel_name)
        reply_count = reply_count_map.get(ts, 0)
        repliers = replier_map.get(ts, set())
        decision = decision_map.get(ts)

        # Find the other agent (replier who isn't the poster)
        other_agents = repliers - {post.agent_id}
        replier = next(iter(other_agents), None) if other_agents else None

        if decision:
            if decision.outcome == "proposal":
                thread_status = "proposal"
            elif decision.outcome == "no_proposal":
                thread_status = "no_proposal"
            elif decision.outcome == "timeout":
                thread_status = "timeout"
            else:
                thread_status = decision.outcome
        elif reply_count > 0:
            thread_status = "active"
        else:
            thread_status = "no_replies"

        threads.append({
            "message_ts": ts,
            "channel_name": post.channel_name,
            "agent_id": post.agent_id,
            "created_at": post.created_at,
            "reply_count": reply_count,
            "replier": replier,
            "status": thread_status,
            "decision": decision,
        })

    # Add orphaned decisions (thread_decisions with no matching root post in
    # agent_messages). Iterating decision_map, not all_decisions, gives each
    # orphan its LAST decision, the same rule the root-post loop applies; a
    # thread with several decisions appears once, under its final outcome.
    known_thread_ids = {t["message_ts"] for t in threads}
    for thread_id, td in decision_map.items():
        if thread_id not in known_thread_ids:
            other_agents = replier_map.get(td.thread_id, set())
            poster_id = td.agent_a
            replier = td.agent_b if td.agent_a == poster_id else td.agent_a
            threads.append({
                "message_ts": td.thread_id,
                "channel_name": td.channel,
                "agent_id": poster_id,
                "created_at": td.decided_at,
                "reply_count": reply_count_map.get(td.thread_id, 0),
                "replier": replier,
                "status": td.outcome,
                "decision": td,
            })
            known_thread_ids.add(td.thread_id)
            available_channels.add(td.channel)

    # Count by status over the whole run, before any filter: the summary cards
    # link to `?run_id=...&status_filter=...` with no channel or agent filter,
    # so each card's number must be what its link lists, and "Total threads"
    # (the sum) is the same under every filter. It counts threads, not root
    # posts: an orphaned decision (no root post) is listed, carded and
    # exported as a thread, so it is counted as one too.
    counts: dict[str, int] = {}
    for t in threads:
        s = t["status"]
        counts[s] = counts.get(s, 0) + 1

    # Collect available agents from threads.
    #
    # Every add is None-guarded, including the poster's. `agent_id` is nullable
    # on agent_messages and really is NULL in production: the retired Slack reconcile
    # (`_rebuild_state_from_slack`, removed 2026-09-29) recorded a real Slack message whose sender maps to no known bot as
    # `is_bot=True, agent_id=NULL` (measured: 7 rows, all from one raw Slack user
    # id). This set is sorted() below, so a single None took the whole page down
    # with "'<' not supported between instances of 'NoneType' and 'str'". The
    # replier and decision adds were already guarded; the poster's was not.
    available_agents = set()
    for t in threads:
        for candidate in (
            t["agent_id"],
            t.get("replier"),
            t["decision"].agent_a if t.get("decision") else None,
            t["decision"].agent_b if t.get("decision") else None,
        ):
            if candidate:
                available_agents.add(candidate)

    # Apply filters
    if channel_filter:
        threads = [t for t in threads if t["channel_name"] == channel_filter]
    if status_filter:
        threads = [t for t in threads if t["status"] == status_filter]
    if agent_filter:
        agent_set = set(agent_filter)
        threads = [
            t for t in threads
            if t["agent_id"] in agent_set
            or (t.get("replier") and t["replier"] in agent_set)
            or (t.get("decision") and (
                t["decision"].agent_a in agent_set or t["decision"].agent_b in agent_set
            ))
        ]

    return {
        "runs": runs,
        "selected_run_id": selected_run_id,
        "threads": threads,
        "counts": counts,
        "channels": sorted(available_channels),
        "agents": sorted(available_agents),
        "channel_filter": channel_filter,
        "status_filter": status_filter,
        "agent_filter": agent_filter or [],
    }
