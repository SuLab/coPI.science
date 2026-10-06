"""Publish a PI's persona in two steps (RB-09; spec 2026-10-05 §4.3).

1. ``export_and_record`` runs inside the caller's transaction: it takes the persona writer
   locks (``lock_persona_writer``), renders the persona once and, with a ``mechanism``,
   records a ``public`` revision of exactly that text. It writes no file. The caller
   commits.
2. ``write_persona_files`` runs after that commit, in a session of its own: under the
   per-PI advisory lock (``advisory_locks.lock_agent_persona``, key ``agent:<user_id>``) it
   re-selects the agent by ``user_id``, re-renders from the database and writes the persona
   file and the PI's staff companies file (``export_companies_file``, spec 2026-10-02
   §7.2). Request paths call it directly; a worker job queues it on
   ``JobContext.after_commit`` through ``schedule_persona_write``, so it runs after the
   handler's commit.

Re-rendering under the lock, rather than writing the text step 1 rendered, is what keeps a
late writer from regressing the file: whichever writer is granted the lock last renders
what the database holds by then. ``user_deletion.delete_user_account`` takes the same lock
before it deletes anything, so a write in flight finishes first and one that starts later
finds no agent for the user and writes nothing.

Lock order, one for every transaction that writes a PI's profile-related rows (the user
row, the profile, tenure keys, ``pi_grants``, ``pi_orcid_fundings``, revisions, ...): the
PI's ``agents`` row, then the per-PI advisory lock, then those rows. Writers take the first
two through ``lock_persona_writer`` (the row FOR KEY SHARE) before their first write;
deletion takes the row FOR UPDATE and then the advisory lock before it deletes anything.
FOR UPDATE conflicts with FOR KEY SHARE, so deletion and a writer queue on the ``agents``
row (or, for a PI without one, on the advisory lock) before either holds a child-row lock
the other needs, and neither can wait on the other while holding something it waits for.
A writer that writes child rows first and takes the locks later can still deadlock with a
deletion; Postgres then aborts one of the two transactions.

The caller passes the publications to step 1, so each path keeps its own query (FA3-V3);
step 2 always uses ``scoped_publications_for_export``. A post-commit write failure is
logged at ERROR and swallowed (``agents.persona_export_failed_at`` arrives with migration
0062)."""
from __future__ import annotations

import functools
import logging
import uuid
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from src.database import get_session_factory
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

#: ``Session.info`` key: this session's open transaction holds a per-PI persona lock (set
#: by ``lock_persona_writer`` and the in-transaction write, cleared when the session's
#: outermost transaction ends). ``write_persona_files(commit=True)`` refuses to run then.
_PERSONA_LOCK_HELD = "persona_lock_held"


@event.listens_for(Session, "after_transaction_end")
def _forget_persona_lock(session: Session, transaction: SessionTransaction) -> None:
    """The transaction-scoped advisory lock is released with the outermost transaction."""
    if transaction.parent is None:
        session.info.pop(_PERSONA_LOCK_HELD, None)


def persona_file_text(agent_id: str) -> str | None:
    """The persona file's current text, or None when it is missing or unreadable. Read
    without newline translation (``newline=""``), as the file was written, so a ``\\r\\n``
    in a profile field compares equal to a fresh render."""
    try:
        with open(
            profile_export.PROFILES_DIR / f"{agent_id}.md", encoding="utf-8", newline=""
        ) as fh:
            return fh.read()
    except OSError:
        return None


async def lock_persona_writer(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Take the persona writer locks for ``user_id`` in the caller's transaction, held
    until it ends: the PI's ``agents`` row FOR KEY SHARE, then the per-PI advisory lock.

    Row lock BEFORE the advisory lock: ``delete_user_account`` takes that row FOR UPDATE
    and then the advisory lock, so the other order deadlocks. A transaction that writes the
    PI's profile-related rows must call this before its first such write (module
    docstring, "Lock order"). Call it before loading what the persona renders, so a writer
    that waited reads what the previous holder committed. Repeating it in one transaction
    is harmless (the advisory lock is re-entrant). Marks ``db`` as holding the lock until
    its transaction ends (``write_persona_files``)."""
    await db.execute(
        select(AgentRegistry.id).where(AgentRegistry.user_id == user_id)
        .with_for_update(key_share=True)
    )
    await lock_agent_persona(db, user_id)
    db.info[_PERSONA_LOCK_HELD] = True


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
    text (nothing is recorded then). Takes ``lock_persona_writer``, held until the
    caller's transaction ends; a caller that loads the objects it passes should take it
    first (as ``reexport_persona`` does). Flushes, never commits."""
    if agent is None:
        return None
    await lock_persona_writer(db, user.id)
    # After a committed deletion the user row is gone.
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
    enrich_grants, the daily sweep, repair scripts). Takes ``lock_persona_writer`` before
    loading anything, so a call that waited on another writer renders what it committed.
    None when the agent, user or profile is missing, or as ``export_and_record``."""
    await lock_persona_writer(db, user_id)
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


def _writer_session(db: AsyncSession) -> AsyncSession:
    """A new session for the post-commit write on ``db``'s bind: its own connection for an
    engine bind; a savepoint on the same connection when ``db`` is bound to one (tests).
    The session factory when ``db`` has no single bind."""
    bind = db.bind
    if bind is None:
        return get_session_factory()()
    if isinstance(bind, AsyncConnection):
        return AsyncSession(
            bind=bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
    return AsyncSession(bind=bind, expire_on_commit=False)


async def _write_files(db: AsyncSession, user_id: uuid.UUID) -> Path | None:
    """Lock, re-select, re-render and write, in ``db``'s transaction; errors propagate."""
    await lock_agent_persona(db, user_id)
    db.info[_PERSONA_LOCK_HELD] = True
    owner = await _load_owner(db, user_id)
    if owner is None:
        return None
    user, profile, agent = owner
    publications = await scoped_publications_for_export(db, user_id, agent.agent_id)
    grants = await load_grant_sections(db, user_id)
    path = export_profile_to_markdown(
        user, profile, agent.agent_id, publications=publications, grants=grants
    )
    if path is None:
        logger.error("Persona write failed for user %s (agent %s)", user_id, agent.agent_id)
    await export_companies_file(db, user_id)
    return path


async def write_persona_files(
    db: AsyncSession, user_id: uuid.UUID, *, commit: bool = True,
) -> Path | None:
    """The post-commit persona write (spec 2026-10-05 §4.3). Takes the per-PI lock,
    re-selects the agent by ``user_id`` (none: deleted or unlinked, nothing is written),
    re-renders from the database and writes the persona and the companies file.

    Returns the persona path, or None when nothing was written or the write failed.
    ``commit=True`` runs in a new session on ``db``'s bind and commits it to release the
    lock; ``db`` itself is neither committed nor rolled back, so the caller's loaded
    objects stay usable. Any error is logged at ERROR, the new session rolled back and None
    returned. ``commit=False`` runs inside ``db``'s open transaction (the lock is held until
    the caller's transaction ends) and lets errors propagate.

    ``commit=True`` raises ``RuntimeError`` when ``db``'s open transaction holds the per-PI
    lock (it called ``lock_persona_writer``, e.g. through ``export_and_record``, and has not
    committed or rolled back) and the write would run on another connection: that
    connection would wait for the caller's own lock forever. The caller must commit first.
    A ``db`` bound to a single connection (tests) is exempt, as its writer session shares
    the connection and so the lock. A read-only transaction opened after the commit holds
    no lock and passes."""
    if not commit:
        return await _write_files(db, user_id)
    if (
        db.in_transaction() and db.info.get(_PERSONA_LOCK_HELD)
        and not isinstance(db.bind, AsyncConnection)
    ):
        raise RuntimeError(
            f"write_persona_files(commit=True) for user {user_id} while the caller's "
            "transaction holds the persona lock: commit it first"
        )
    session = _writer_session(db)
    try:
        path = await _write_files(session, user_id)
        await session.commit()
        return path
    except Exception:
        logger.exception("Persona write failed after commit for user %s", user_id)
        await session.rollback()
        return None
    finally:
        await session.close()


async def schedule_persona_write(
    db: AsyncSession, user_id: uuid.UUID, after_commit: list[AfterCommit] | None,
) -> None:
    """Queue the post-commit write on a worker's ``JobContext.after_commit`` (it then runs
    after the handler's commit, in a session on the handler's bind); with None (a direct
    caller with no commit hook: tests) write now, inside the caller's transaction, letting
    errors propagate."""
    if after_commit is None:
        await write_persona_files(db, user_id, commit=False)
    else:
        after_commit.append(functools.partial(write_persona_files, user_id=user_id))
