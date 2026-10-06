"""Publish a PI's persona in two steps (RB-09; spec 2026-10-05 §4.3).

1. ``export_and_record`` runs inside the caller's transaction: it renders the persona once
   and, with a ``mechanism``, records a ``public`` revision of exactly that text. It writes
   no file. The caller commits.
2. ``write_persona_files`` runs after that commit: under the per-PI advisory lock
   (``advisory_locks.lock_agent_persona``, key ``agent:<user_id>``) it re-selects the agent
   by ``user_id``, re-renders from the database and writes the persona file and the PI's
   staff companies file (``export_companies_file``, spec 2026-10-02 §7.2). Request paths
   call it directly; a worker job queues it on ``JobContext.after_commit`` through
   ``schedule_persona_write``, so it runs after the handler's commit.

Re-rendering under the lock, rather than writing the text step 1 rendered, is what keeps a
late writer from regressing the file: whichever writer is granted the lock last renders
what the database holds by then. ``user_deletion.delete_user_account`` takes the same lock
before it commits, so a write in flight finishes first and one that starts later finds no
agent for the user and writes nothing.

The caller passes the publications to step 1, so each path keeps its own query (FA3-V3);
step 2 always uses ``scoped_publications_for_export``. A post-commit write failure is
logged at ERROR and swallowed (``agents.persona_export_failed_at`` arrives with migration
0062)."""
from __future__ import annotations

import functools
import logging
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, ResearcherProfile, User
from src.services import profile_export
from src.services.advisory_locks import lock_agent_persona
from src.services.grant_sections import GrantSections, load_grant_sections
from src.services.job_queue import AfterCommit
from src.services.pi_companies import export_companies_file
from src.services.profile_export import export_profile_to_markdown, render_profile_markdown
from src.services.profile_versioning import create_revision
from src.services.tenure_scope import TenureScopedPublications, scoped_publications_for_export

logger = logging.getLogger(__name__)


def persona_file_text(agent_id: str) -> str | None:
    """The persona file's current text, or None when it is missing or unreadable."""
    try:
        return (profile_export.PROFILES_DIR / f"{agent_id}.md").read_text(encoding="utf-8")
    except OSError:
        return None


async def _load_owner(
    db: AsyncSession, user_id: uuid.UUID,
) -> tuple[User, ResearcherProfile, AgentRegistry] | None:
    """The PI's user, profile and agent as the database holds them now (identity-map
    objects are refreshed), or None when any of the three is missing."""
    fresh = {"populate_existing": True}
    agent = (await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user_id).execution_options(**fresh)
    )).scalar_one_or_none()
    if agent is None:
        return None
    user = (await db.execute(
        select(User).where(User.id == user_id).execution_options(**fresh)
    )).scalar_one_or_none()
    profile = (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
        .execution_options(**fresh)
    )).scalar_one_or_none()
    if user is None or profile is None:
        return None
    return user, profile, agent


async def export_and_record(
    db: AsyncSession, *, user: User, profile: ResearcherProfile, agent: AgentRegistry | None,
    publications: TenureScopedPublications | None, grants: GrantSections, mechanism: str | None,
    changed_by_user_id: uuid.UUID | None = None, change_summary: str | None = None,
    skip_if_file_matches: bool = False,
) -> str | None:
    """Render the persona once and, with ``mechanism``, record a ``public`` revision of that
    exact text, in the caller's transaction. Writes no file: after committing, the caller
    runs ``write_persona_files`` (a worker queues it with ``schedule_persona_write``).

    Returns the rendered text; None when there is no agent, the user row is gone (a
    deletion won the lock), or ``skip_if_file_matches`` and the file already holds the
    text (nothing is recorded then). Takes the per-PI lock, held until the caller's
    transaction ends. Flushes, never commits."""
    if agent is None:
        return None
    # Row lock BEFORE the advisory lock: delete_user_account's cascade locks this row and
    # then waits for the advisory lock, so the other order deadlocks. After a committed
    # deletion the user re-check below returns None.
    await db.execute(
        select(AgentRegistry.id).where(AgentRegistry.user_id == user.id)
        .with_for_update(key_share=True)
    )
    await lock_agent_persona(db, user.id)
    if await db.scalar(select(User.id).where(User.id == user.id)) is None:
        return None
    text = render_profile_markdown(user, profile, publications=publications, grants=grants)
    if skip_if_file_matches and persona_file_text(agent.agent_id) == text:
        return None
    if mechanism is not None:
        await create_revision(
            db, agent_registry_id=agent.id, profile_type="public", content=text,
            changed_by_user_id=changed_by_user_id, mechanism=mechanism,
            change_summary=change_summary,
        )
    return text


async def reexport_persona(
    db: AsyncSession, user_id: uuid.UUID, *, mechanism: str | None,
    changed_by_user_id: uuid.UUID | None = None, change_summary: str | None = None,
    skip_if_file_matches: bool = False,
) -> str | None:
    """``export_and_record`` over the user, profile, agent, publications and grant sections
    loaded from the database: for callers that hold no profile objects (vetoes, pins,
    enrich_grants, the daily sweep, repair scripts). None when the agent, user or profile
    is missing, or as ``export_and_record``."""
    owner = await _load_owner(db, user_id)
    if owner is None:
        return None
    user, profile, agent = owner
    publications = await scoped_publications_for_export(db, user_id, agent.agent_id)
    grants = await load_grant_sections(db, user_id)
    return await export_and_record(
        db, user=user, profile=profile, agent=agent, publications=publications, grants=grants,
        mechanism=mechanism, changed_by_user_id=changed_by_user_id,
        change_summary=change_summary, skip_if_file_matches=skip_if_file_matches,
    )


async def render_persona_from_db(
    db: AsyncSession, user_id: uuid.UUID,
) -> tuple[AgentRegistry, str] | None:
    """The agent and the persona text a write would produce now (no lock, no write, no
    revision): the comparison the repair uses. None when the agent, user or profile is
    missing."""
    owner = await _load_owner(db, user_id)
    if owner is None:
        return None
    user, profile, agent = owner
    publications = await scoped_publications_for_export(db, user_id, agent.agent_id)
    grants = await load_grant_sections(db, user_id)
    return agent, render_profile_markdown(user, profile, publications=publications, grants=grants)


async def write_persona_files(
    db: AsyncSession, user_id: uuid.UUID, *, commit: bool = True,
) -> Path | None:
    """The post-commit persona write (spec 2026-10-05 §4.3). Takes the per-PI lock,
    re-selects the agent by ``user_id`` (none: deleted or unlinked, nothing is written),
    re-renders from the database and writes the persona and the companies file.

    Returns the persona path, or None when nothing was written or the write failed.
    ``commit=True`` commits to release the lock; any error is logged at ERROR, the session
    rolled back and None returned. ``commit=False`` runs inside the caller's open
    transaction (the lock is held until the caller's transaction ends) and lets errors
    propagate."""
    try:
        await lock_agent_persona(db, user_id)
        owner = await _load_owner(db, user_id)
        path = None
        if owner is not None:
            user, profile, agent = owner
            publications = await scoped_publications_for_export(db, user_id, agent.agent_id)
            grants = await load_grant_sections(db, user_id)
            path = export_profile_to_markdown(
                user, profile, agent.agent_id, publications=publications, grants=grants
            )
            if path is None:
                logger.error(
                    "Persona write failed for user %s (agent %s)", user_id, agent.agent_id
                )
            await export_companies_file(db, user_id)
        if commit:
            await db.commit()
        return path
    except Exception:
        if not commit:
            raise
        logger.exception("Persona write failed after commit for user %s", user_id)
        await db.rollback()
        return None


async def schedule_persona_write(
    db: AsyncSession, user_id: uuid.UUID, after_commit: list[AfterCommit] | None,
) -> None:
    """Queue the post-commit write on a worker's ``JobContext.after_commit`` (it then runs
    on the handler's session after its commit); with None (a direct caller with no commit
    hook: tests) write now, inside the caller's transaction, letting errors propagate."""
    if after_commit is None:
        await write_persona_files(db, user_id, commit=False)
    else:
        after_commit.append(functools.partial(write_persona_files, user_id=user_id))
