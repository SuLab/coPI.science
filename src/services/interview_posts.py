"""The posts of one interview, as the Interviews page and the trajectory generator see them.

Imported by the worker (``src/services/trajectory_generation.py``), so it depends only on
``src.models`` and ``src.services.runs``; it must never pull in
``src.services.blackbird_rubric`` or ``src.services.rubric_revisions``, which parse a TOML
document under ``prompts/`` at import time (see ``src/services/interview_transcript.py``).

An **interview** is a root lab post (``phase == "new_post"``, ``thread_ts IS NULL``) with
at least one ``thread_reply`` from a different ``agent_id`` in its thread. A split root
keeps its continuation chunks in its own thread (``src/agent/slack_client.py``), so
counting only replies from another author keeps a long unanswered pitch out. Measured
2026-10-19: every pitch on record answered by another author was answered by the hub
(``agents.role = 'scout_hub'``) and by no other agent.

A **post** is one turn: Slack splits a long message into several rows and nothing links
them, so consecutive non-panel rows of one thread by the same ``agent_id`` less than
``MERGE_WINDOW_SECONDS`` apart are one post. Measured over every thread on record
(2026-10-19): all 74 consecutive same-author pairs were under 2 s apart, and of 957
different-author pairs one was 2-10 s apart and the rest 10 s or more.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models import AgentMessage, SimulationRun, ThreadDecision

MERGE_WINDOW_SECONDS = 2.0

#: Characters of each chunk kept when posts are loaded without their full text; the page
#: shows only a first sentence, which sits well inside this.
CLIP_CHARS = 600

_POST_PHASES = ("new_post", "thread_reply")


@dataclass(frozen=True)
class Post:
    #: ``agent_messages.id`` of the post's first chunk: the key of
    #: ``InterviewTrajectory.lines``.
    first_row_id: uuid.UUID
    agent_id: str | None
    #: ``agent_messages.sender_name`` of the first chunk (e.g. "BlackbirdBot").
    speaker: str
    #: ``"pitch"`` for the root post, ``"reply"`` for every other post.
    kind: str
    #: ``agent_messages.posted_at`` of the first chunk (epoch seconds).
    posted_at: float
    #: The chunks' content joined with a blank line (clipped when loaded without full text).
    text: str


def merge_chunks(rows: Sequence[Any]) -> list[Post]:
    """One thread's non-panel rows, ordered by ``(posted_at, created_at)``, as posts.

    A row joins the previous post when it has the same ``agent_id`` and its ``posted_at``
    is less than ``MERGE_WINDOW_SECONDS`` after the previous row's; chunks join with
    ``"\\n\\n"``. Rows carry ``id``, ``agent_id``, ``sender_name``, ``phase``,
    ``posted_at`` and ``content`` (ORM rows or SQL rows alike).
    """
    firsts: list[Any] = []
    chunks: list[list[str]] = []
    last_posted_at = 0.0
    for row in rows:
        if (
            firsts
            and row.agent_id == firsts[-1].agent_id
            and row.posted_at - last_posted_at < MERGE_WINDOW_SECONDS
        ):
            chunks[-1].append(row.content)
        else:
            firsts.append(row)
            chunks.append([row.content])
        last_posted_at = row.posted_at
    return [
        Post(
            first_row_id=first.id,
            agent_id=first.agent_id,
            speaker=first.sender_name,
            kind="pitch" if first.phase == "new_post" else "reply",
            posted_at=first.posted_at,
            text="\n\n".join(parts),
        )
        for first, parts in zip(firsts, chunks, strict=True)
    ]


def posts_hash(posts: Sequence[Post]) -> str:
    """``sha256("\\n".join(str(p.first_row_id)))`` as its first 12 hex characters."""
    joined = "\n".join(str(p.first_row_id) for p in posts)
    return hashlib.sha256(joined.encode()).hexdigest()[:12]


def _is_root(m: Any) -> Any:
    return and_(m.phase == "new_post", m.thread_ts.is_(None), m.message_ts.is_not(None))


def _answered(root: Any) -> Any:
    """EXISTS: a ``thread_reply`` in the root's thread from a different author."""
    reply = aliased(AgentMessage)
    return exists().where(
        reply.simulation_run_id == root.simulation_run_id,
        reply.thread_ts == root.message_ts,
        reply.phase == "thread_reply",
        reply.agent_id.is_distinct_from(root.agent_id),
    )


async def run_interview_roots(db: AsyncSession, run_id: uuid.UUID) -> list[Any]:
    """The run's interview root rows, newest first (see the module docstring).

    Rows carry ``id``, ``agent_id``, ``thread_id`` (the root's ``message_ts``),
    ``channel_name``, ``posted_at`` and ``created_at``.
    """
    root = aliased(AgentMessage)
    stmt = (
        select(
            root.id,
            root.agent_id,
            root.message_ts.label("thread_id"),
            root.channel_name,
            root.posted_at,
            root.created_at,
        )
        .where(root.simulation_run_id == run_id, _is_root(root), _answered(root))
        .order_by(root.posted_at.desc(), root.created_at.desc(), root.id)
    )
    return list((await db.execute(stmt)).all())


async def interview_and_root_counts(
    db: AsyncSession,
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """``(interviews, root posts)`` per run id, one query each, for the run bar and the
    unanswered-pitch count."""
    root = aliased(AgentMessage)
    base = select(root.simulation_run_id, func.count()).where(_is_root(root))
    roots = dict((await db.execute(base.group_by(root.simulation_run_id))).all())
    answered = dict(
        (
            await db.execute(base.where(_answered(root)).group_by(root.simulation_run_id))
        ).all()
    )
    return answered, roots


async def load_posts(
    db: AsyncSession, run_id: uuid.UUID, thread_id: str, *, full_text: bool
) -> list[Post]:
    """One interview's posts in order; ``text`` is ``left(content, 600)`` per chunk
    unless ``full_text``."""
    content = AgentMessage.content if full_text else func.left(AgentMessage.content, CLIP_CHARS)
    stmt = (
        select(
            AgentMessage.id,
            AgentMessage.agent_id,
            AgentMessage.sender_name,
            AgentMessage.phase,
            AgentMessage.posted_at,
            content.label("content"),
        )
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.phase.in_(_POST_PHASES),
            or_(
                AgentMessage.thread_ts == thread_id,
                and_(AgentMessage.message_ts == thread_id, AgentMessage.thread_ts.is_(None)),
            ),
        )
        .order_by(AgentMessage.posted_at, AgentMessage.created_at, AgentMessage.id)
    )
    return merge_chunks((await db.execute(stmt)).all())


async def done_thread_ids(
    db: AsyncSession, run: SimulationRun, live_id: uuid.UUID | None
) -> set[str]:
    """The run's interview thread ids that are done: every one with a
    ``ThreadDecision``, plus every interview of a run that is not ``live_id`` and has
    ``finalized_at`` or ``ended_at`` set (a run that stopped with interviews still open
    writes no decision for them)."""
    threads = {str(r.thread_id) for r in await run_interview_roots(db, run.id)}
    if run.id != live_id and (run.finalized_at is not None or run.ended_at is not None):
        return threads
    decided = (
        await db.execute(
            select(ThreadDecision.thread_id).where(ThreadDecision.simulation_run_id == run.id)
        )
    ).scalars()
    return threads & set(decided)
