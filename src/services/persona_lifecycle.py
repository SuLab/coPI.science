"""The persona file across an agent's lifecycle (spec 2026-10-05 §6.4, D31; §4.3).

An agent's creation, request, link, rename and activation each publish the PI's persona
after the change commits: a ``lifecycle_export`` revision of the rendered text (when it
differs from the file), a commit, then ``profile_publish.write_persona_files``. A file
already at a new or relinked agent's slug was not written for its owner, so it is moved to
``ORPHANED_DIR`` first. Nothing here runs inside the caller's transaction: every function
expects the caller to have committed, and ends its own transaction before returning."""
from __future__ import annotations

import logging
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry
from src.services import profile_export
from src.services.pi_companies import move_companies_file
from src.services.profile_publish import (
    reexport_persona,
    render_persona_from_db,
    write_persona_files,
)

logger = logging.getLogger(__name__)

#: Where a persona file no agent stands behind goes: out of the directory the engine reads,
#: kept for audit. Gitignored with the rest of profiles/ (``.gitignore``: ``profiles/**/*.md``).
ORPHANED_DIR = Path("profiles/private/orphaned")
#: The slug rule persona files share with ``user_deletion`` and ``pi_companies``.
_SAFE_AGENT_ID = re.compile(r"[a-z0-9_-]{1,50}")
_SUMMARY_HEADING = "## Research Summary"


def persona_has_research_summary(text: str | None) -> bool:
    """Whether persona ``text`` has a line exactly ``## Research Summary`` followed by a
    non-blank line before the next ``## `` heading. ``render_profile_markdown`` omits the
    heading for an empty summary, so a file without it has no summary to serve."""
    if not text:
        return False
    lines = text.splitlines()
    try:
        start = lines.index(_SUMMARY_HEADING)
    except ValueError:
        return False
    for line in lines[start + 1:]:
        if line.startswith("## "):
            return False
        if line.strip():
            return True
    return False


def archive_persona_file(agent_id: str) -> Path | None:
    """Move ``profile_export.PROFILES_DIR/<agent_id>.md`` to
    ``ORPHANED_DIR/<agent_id>.<UTC %Y%m%dT%H%M%S%fZ>.md`` and return the new path. None when
    there is no such file, the slug fails ``_SAFE_AGENT_ID`` (logged at ERROR), or the move
    failed (logged at ERROR, the file left in place). Never raises."""
    if not _SAFE_AGENT_ID.fullmatch(agent_id or ""):
        logger.error("Persona file of unsafe agent id %r not archived", agent_id)
        return None
    source = profile_export.PROFILES_DIR / f"{agent_id}.md"
    try:
        if not source.is_file():
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = ORPHANED_DIR / f"{agent_id}.{stamp}.md"
        ORPHANED_DIR.mkdir(parents=True, exist_ok=True)
        # profiles/public and profiles/private share the bind mount on the host, but
        # shutil.move also covers a cross-device move.
        shutil.move(str(source), str(target))
    except OSError as exc:
        logger.error("Persona file %s not archived: %s", source, exc)
        return None
    logger.info("Archived persona file %s to %s", source, target)
    return target


async def export_after_lifecycle(
    db: AsyncSession, user_id: uuid.UUID, *, event: str, actor_id: uuid.UUID | None,
    replace_leftover: bool = False,
) -> Path | None:
    """Publish the PI's persona after a committed lifecycle change (spec §6.4, D31).

    With ``replace_leftover`` (agent created, requested, linked or renamed) the file now at
    the agent's slug is archived first (``archive_persona_file``), even when nothing is
    exported. Then ``reexport_persona`` records a ``lifecycle_export`` revision with
    ``event`` as its change summary (without ``replace_leftover``, nothing is recorded or
    written when the file already equals the render), ``db`` is committed, and
    ``write_persona_files`` writes the persona and companies files.

    Returns the persona path written; None when the PI has no agent or no profile, the file
    already matched, or the write failed (logged; ``agents.persona_export_failed_at`` is
    set by the writer). Requires that ``db`` holds no open transaction with the persona
    lock (commit first); commits ``db``. Database errors propagate."""
    if replace_leftover:
        slug = await db.scalar(
            select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user_id)
        )
        if slug:
            archive_persona_file(slug)
    rendered = await reexport_persona(
        db, user_id, mechanism="lifecycle_export", changed_by_user_id=actor_id,
        change_summary=event, skip_if_file_matches=not replace_leftover,
    )
    await db.commit()  # records the revision, and ends the transaction holding the persona lock
    if rendered is None:
        return None
    return await write_persona_files(db, user_id)


async def rename_persona_files(
    db: AsyncSession, user_id: uuid.UUID, *, old_agent_id: str, actor_id: uuid.UUID | None,
) -> Path | None:
    """After a committed rename of the PI's agent: ``export_after_lifecycle`` under the new
    slug with ``replace_leftover=True`` (event ``"Agent renamed from <old_agent_id>"``);
    then the old slug's persona file is removed when the new one was written, archived when
    there is no profile to export, and kept (logged at ERROR) when the write failed; last,
    ``pi_companies.move_companies_file``. Returns the new persona path or None. Commits
    ``db``; never raises for a filesystem error."""
    path = await export_after_lifecycle(
        db, user_id, event=f"Agent renamed from {old_agent_id}", actor_id=actor_id,
        replace_leftover=True,
    )
    old = profile_export.PROFILES_DIR / f"{old_agent_id}.md"
    if path is not None:
        if path.name != old.name:
            try:
                old.unlink(missing_ok=True)
            except OSError as exc:
                logger.error("Persona file of renamed agent %s not removed: %s", old_agent_id, exc)
    elif await render_persona_from_db(db, user_id) is None:
        archive_persona_file(old_agent_id)  # no profile: nothing stands behind the old file
    else:
        logger.error(
            "Persona file of renamed agent %s kept: the export under the new id failed",
            old_agent_id,
        )
    await db.commit()  # ends render_persona_from_db's read transaction
    await move_companies_file(db, user_id=user_id, old_agent_id=old_agent_id)
    return path
