"""Account-deletion teardown (docs/audits/2026-08-25-pi-deletion).

Deleting a User used to be ``await db.delete(user)`` and nothing else, which
made FK topology the deletion policy: the linked agent stayed on the live
roster (``agents.user_id`` is SET NULL and the roster sync loads by status
alone), its Slack token stayed valid, the exported profile markdown stayed on
disk feeding the agent's prompts, full profile snapshots stayed readable in
``profile_revisions``, and the per-user tenure key leaked in ``app_settings``.

This module is the one place deletion policy lives. Both delete routes call
``delete_user_account``; nothing else may delete users.

Two phases, deliberately:

* **in-transaction** — lock, suspend the linked agent, purge the user's profile
  revisions (by owner, C6 of the 2026-10-15 publication-handling design), DMs and tenure
  key (and optionally the allowlist row), delete the user, COMMIT. It
  first takes the PI's ``agents`` row FOR UPDATE and then the per-PI persona
  lock (``lock_agent_persona``), the lock order every profile writer follows
  (``profile_publish`` module docstring, spec 2026-10-05 §4.3): a writer holding
  ``lock_persona_writer`` commits before this deletes anything, so its revision
  is purged and its persona file removed below, and a writer that starts later
  finds no user and records or writes nothing.
* **post-commit** — filesystem cleanup (the agent's files and the user's owner-tagged
  archived persona copies; a symbolic link is removed, never followed) and Slack token
  revocation. External
  effects that cannot roll back run only once the DB state is durable; a
  failure lands on the report and in the log, never raises. A half-failed
  cleanup still leaves a *suspended* agent, which is the state that actually
  stops activity.

``status='suspended'`` and not ``'inactive'`` (D2): suspended is the
admin-only parked state, and ``set_agent_mute_state`` refuses to touch it —
so a manager unmute cannot resurrect a deleted PI's agent.
"""

import logging
import os
import re
import shutil
import stat
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import and_, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AccessAllowlist,
    AgentRegistry,
    AppSetting,
    PiDmMessage,
    ProfileRevision,
    Publication,
    ResearcherProfile,
    User,
)
from src.services.advisory_locks import lock_agent_persona
from src.services.jhu_rules import PROVISIONAL_KEY_PREFIX, TENURE_KEY_PREFIX
from src.services.slack_tokens import is_valid_token
from src.services.slack_web import revoke_token_async

logger = logging.getLogger(__name__)

# Kept in sync by comment-reference, not import: profile_export.PROFILES_DIR
# (src/services/profile_export.py) writes _PUBLIC_DIR/{agent_id}.md, the
# engine's memory writer (src/agent/agent.py, PROFILES_DIR / "memory") owns
# _MEMORY_DIR, and pi_companies.COMPANIES_DIR (src/services/pi_companies.py)
# writes _COMPANIES_DIR/{agent_id}.md, the hub's staff company record
# (tests/unit/test_pi_companies.py pins that pair equal), and persona_lifecycle.ORPHANED_DIR
# (src/services/persona_lifecycle.py) archives persona copies under _ORPHANED_DIR
# (tests/unit/test_user_deletion_paths.py pins that pair equal). Module-level so tests
# can monkeypatch them to a tmp_path.
_PUBLIC_DIR = Path("profiles/public")
_MEMORY_DIR = Path("profiles/memory")
_COMPANIES_DIR = Path("profiles/private/companies")
_ORPHANED_DIR = Path("profiles/private/orphaned")

# agent_id slugs are minted by our own code (lowercase last names, optional
# initial prefix / numeric suffix), but this function deletes files, so it
# refuses anything that could escape the directories above.
_SAFE_AGENT_ID = re.compile(r"[a-z0-9_-]{1,50}")


@dataclass
class DeletionReport:
    user_id: uuid.UUID
    orcid: str
    agent_id: str | None = None
    agent_suspended: bool = False
    revisions_deleted: int = 0
    dms_deleted: int = 0
    tenure_key_deleted: bool = False
    allowlist_removed: bool = False
    files_deleted: list[str] = field(default_factory=list)
    slack_token_revoked: bool | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"agent={self.agent_id or '-'} suspended={self.agent_suspended} "
            f"revisions={self.revisions_deleted} dms={self.dms_deleted} "
            f"tenure_key={self.tenure_key_deleted} "
            f"allowlist_removed={self.allowlist_removed} "
            f"files={len(self.files_deleted)} "
            f"slack_revoked={self.slack_token_revoked} "
            f"errors={self.errors or 'none'}"
        )


@dataclass(frozen=True)
class ResearchData:
    """What deleting an account would destroy beyond its staff-side rows (Q5, F18 of the
    2026-10-15 publication-handling design): a researcher profile, any ``publications``
    row of the user (excluded or not), and the ``agents`` row the user owns (any status).
    The Staff page's delete and the self-service delete refuse a non-admin on it."""

    has_profile: bool
    publication_count: int
    agent_id: str | None

    @property
    def has_any(self) -> bool:
        return self.has_profile or self.publication_count > 0 or self.agent_id is not None

    @property
    def items_text(self) -> str:
        """The data in words, in this order and only when present: "a researcher
        profile", "{n} stored publication(s)", "a lab agent". One item stands alone, two
        are joined with " and ", three read "a, b and c"."""
        items: list[str] = []
        if self.has_profile:
            items.append("a researcher profile")
        if self.publication_count:
            n = self.publication_count
            items.append(f"{n} stored publication{'' if n == 1 else 's'}")
        if self.agent_id is not None:
            items.append("a lab agent")
        if len(items) <= 1:
            return "".join(items)
        return f"{', '.join(items[:-1])} and {items[-1]}"


async def research_data_for(
    db: AsyncSession, user_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, ResearchData]:
    """``ResearchData`` for every id in ``user_ids``, from three grouped reads (profile,
    publication count, owned agent). An id with none of them gets
    ``ResearchData(False, 0, None)``; no ids, no query. Reads only."""
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    with_profile = set((await db.scalars(
        select(ResearcherProfile.user_id).where(ResearcherProfile.user_id.in_(ids))
    )).all())
    counts = dict((await db.execute(
        select(Publication.user_id, func.count())
        .where(Publication.user_id.in_(ids))
        .group_by(Publication.user_id)
    )).tuples().all())
    agents = dict((await db.execute(
        select(AgentRegistry.user_id, AgentRegistry.agent_id)
        .where(AgentRegistry.user_id.in_(ids))
    )).tuples().all())
    return {
        uid: ResearchData(uid in with_profile, int(counts.get(uid, 0)), agents.get(uid))
        for uid in ids
    }


def _record_error(report: DeletionReport, message: str) -> None:
    """Report and log a teardown problem that does not stop the teardown."""
    report.errors.append(message)
    logger.error("Deletion teardown: %s", message)


def _remove_entry(path: Path, report: DeletionReport) -> None:
    """Remove ``path`` without following a symbolic link (main SPEC WEB-5; D-D7): a real
    directory goes with ``shutil.rmtree`` (which never follows a link inside the tree and
    refuses one at the top); anything else, a symbolic link included, is unlinked, so a
    link's target is never touched. A missing path is nothing to do. A removal is listed
    in ``files_deleted``; a failure is reported and logged."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        report.errors.append(f"file cleanup {path}: {exc}")
        logger.error("Deletion teardown: could not remove %s: %s", path, exc)
        return
    try:
        if stat.S_ISDIR(mode):
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        report.errors.append(f"file cleanup {path}: {exc}")
        logger.error("Deletion teardown: could not remove %s: %s", path, exc)
        return
    report.files_deleted.append(str(path))


def _archive_stamp_dirs(root: Path, report: DeletionReport) -> list[Path]:
    """The real directories directly under the memory archive ``root``, sorted; none when
    it is missing. A ``root`` that is a symbolic link, and an entry that is one, are
    reported and never followed (``Path.glob`` would follow a symlinked stamp out of
    ``profiles/``)."""
    try:
        mode = os.lstat(root).st_mode
    except FileNotFoundError:
        return []
    except OSError as exc:
        _record_error(report, f"memory archive {root}: {exc}")
        return []
    if stat.S_ISLNK(mode):
        _record_error(report, f"memory archive: {root} is a symbolic link; not followed")
        return []
    if not stat.S_ISDIR(mode):
        return []
    stamps: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_symlink():
                    report.errors.append(
                        f"memory archive: {entry.path} is a symbolic link; not followed")
                    logger.error(
                        "Deletion teardown: archive entry %s is a symbolic link; not followed",
                        entry.path,
                    )
                elif entry.is_dir(follow_symlinks=False):
                    stamps.append(Path(entry.path))
    except OSError as exc:
        _record_error(report, f"memory archive {root}: {exc}")
        return []
    return sorted(stamps)


def _agent_paths(agent_id: str, report: DeletionReport) -> list[Path]:
    if not _SAFE_AGENT_ID.fullmatch(agent_id):
        raise ValueError(f"refusing file cleanup for unsafe agent_id {agent_id!r}")
    paths = [
        _PUBLIC_DIR / f"{agent_id}.md",
        _COMPANIES_DIR / f"{agent_id}.md",  # the hub's staff company record (pi_companies)
        _MEMORY_DIR / f"{agent_id}.md",  # legacy pre-partition memory file
        _MEMORY_DIR / agent_id,  # partitioned memory directory
    ]
    # --fresh archives whole memory trees under archive/<stamp>/; a deleted
    # PI's synthesized memory must be purged from those snapshots too. Only real stamp
    # directories are searched, so no symbolic link is followed (D-D7).
    for stamp_dir in _archive_stamp_dirs(_MEMORY_DIR / "archive", report):
        paths.append(stamp_dir / agent_id)
        paths.append(stamp_dir / f"{agent_id}.md")
    return paths


def _delete_agent_files(agent_id: str, report: DeletionReport) -> None:
    for path in _agent_paths(agent_id, report):
        _remove_entry(path, report)


def _delete_archived_personas(user_id: uuid.UUID, report: DeletionReport) -> None:
    """Remove the archived persona copies tagged with ``user_id`` (C6, Q29) from the top
    level of ``_ORPHANED_DIR``: ``<agent_id>.<stamp>.owner-<user id hex>.md``, whichever
    agent they came from. Untagged copies, archived with no known owner, are kept (F30).
    A symbolic-link directory is not followed, and a matching directory is not removed;
    both are reported. Never raises."""
    name_re = re.compile(rf"[a-z0-9_-]{{1,50}}\.\d{{8}}T\d{{12}}Z\.owner-{user_id.hex}\.md")
    try:
        mode = os.lstat(_ORPHANED_DIR).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        _record_error(report, f"archived personas {_ORPHANED_DIR}: {exc}")
        return
    if stat.S_ISLNK(mode):
        _record_error(
            report, f"archived personas: {_ORPHANED_DIR} is a symbolic link; not followed")
        return
    try:
        with os.scandir(_ORPHANED_DIR) as entries:
            found = sorted(
                (Path(entry.path), entry.is_dir(follow_symlinks=False))
                for entry in entries if name_re.fullmatch(entry.name)
            )
    except OSError as exc:
        _record_error(report, f"archived personas {_ORPHANED_DIR}: {exc}")
        return
    for path, is_dir in found:
        if is_dir:
            _record_error(report, f"archived personas: {path} is a directory; not removed")
            continue
        _remove_entry(path, report)


async def _delete_in_transaction(
    db: AsyncSession, user: User, report: DeletionReport, *, remove_from_allowlist: bool,
) -> str | None:
    """The in-transaction phase, through its COMMIT; fills ``report`` and returns the
    agent's stored Slack token (None without an agent)."""
    # Lock order (profile_publish module docstring): the agents row FOR UPDATE, then the
    # per-PI advisory lock, before any write. FOR UPDATE waits for a writer holding the row
    # FOR KEY SHARE, and blocks new revision inserts (their FK check) until this commits.
    agent = (
        await db.execute(
            select(AgentRegistry).where(AgentRegistry.user_id == user.id)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    await lock_agent_persona(db, report.user_id)
    token: str | None = None

    if agent is not None:
        report.agent_id = agent.agent_id
        token = agent.slack_bot_token
        if agent.status != "suspended":
            agent.status = "suspended"
            report.agent_suspended = True
        res = await db.execute(
            sa_delete(PiDmMessage).where(PiDmMessage.agent_id == agent.agent_id)
        )
        report.dms_deleted += res.rowcount or 0

    # Revisions by owner (C6, Q29): every revision stamped with this user, on any agent
    # (one since linked to someone else included), plus the legacy unstamped revisions
    # (written before migration 0069) of the agent the user owns now. A revision stamped
    # for another owner belongs to that owner and stays; an unstamped revision of an agent
    # the user no longer owns has no known owner and stays too. ON DELETE CASCADE would
    # remove the stamped rows with the users row anyway; deleting them here counts them.
    owned = ProfileRevision.owner_user_id == user.id
    if agent is not None:
        owned = or_(owned, and_(
            ProfileRevision.owner_user_id.is_(None),
            ProfileRevision.agent_registry_id == agent.id,
        ))
    res = await db.execute(sa_delete(ProfileRevision).where(owned))
    report.revisions_deleted = res.rowcount or 0

    # Local-transport DMs are keyed by the string "local:<users.id>", the one
    # place a users.id value outlives its row (audit F4) — purge these even
    # when the agent row was unlinked before this teardown existed.
    res = await db.execute(
        sa_delete(PiDmMessage).where(PiDmMessage.pi_user_id == f"local:{user.id}")
    )
    report.dms_deleted += res.rowcount or 0

    res = await db.execute(
        sa_delete(AppSetting).where(
            AppSetting.key.in_((
                f"{TENURE_KEY_PREFIX}{user.id}", f"{PROVISIONAL_KEY_PREFIX}{user.id}",
            ))
        )
    )
    report.tenure_key_deleted = bool(res.rowcount)

    if remove_from_allowlist:
        res = await db.execute(
            sa_delete(AccessAllowlist).where(AccessAllowlist.orcid == user.orcid)
        )
        report.allowlist_removed = bool(res.rowcount)

    await db.delete(user)
    await db.commit()
    return token


async def delete_user_account(
    db: AsyncSession,
    user: User,
    *,
    remove_from_allowlist: bool = False,
) -> DeletionReport:
    """Tear down and delete ``user``. Commits. See module docstring.

    Never retries: a database error in the in-transaction phase propagates with ``db``'s
    transaction left for the caller to roll back (re-running the phase after a rollback
    would run without the locks the caller took in that transaction, such as the routes'
    ``ensure_admin_remains`` admin lock)."""
    report = DeletionReport(user_id=user.id, orcid=user.orcid)
    token = await _delete_in_transaction(
        db, user, report, remove_from_allowlist=remove_from_allowlist
    )

    # ---- post-commit, best-effort: external effects that cannot roll back ----
    if report.agent_id is not None:
        try:
            _delete_agent_files(report.agent_id, report)
        except ValueError as exc:
            report.errors.append(str(exc))
            logger.error("Deletion teardown: %s", exc)
    # The user's owner-tagged archived persona copies, with or without an agent now (Q29).
    _delete_archived_personas(report.user_id, report)

    stored = (token or "").strip()
    if stored.startswith("xoxb-placeholder"):
        pass  # the recognised no-op seed value: nothing live to revoke
    elif stored and not is_valid_token(stored):
        # Only a bot token is ours to revoke. auth.revoke kills whatever token
        # it is handed, so an ``xoxp-`` value mistakenly stored here would
        # revoke a HUMAN's user token while the real bot token stays live.
        redacted = f"{stored[:5]}…"
        report.errors.append(
            f"slack revoke skipped: stored value {redacted!r} is not a bot token; "
            "left in the DB column for manual review"
        )
        logger.error(
            "Deletion teardown: agent %s stores %r, which is not a bot token; "
            "NOT revoked and left in place — find and revoke the real bot token "
            "manually (auth.revoke) or rotate the app",
            report.agent_id,
            redacted,
        )
    elif stored:
        try:
            report.slack_token_revoked = await revoke_token_async(stored)
        except Exception as exc:  # SlackApiError or transport failure
            report.slack_token_revoked = False
            report.errors.append(f"slack revoke: {exc}")
            logger.error(
                "Deletion teardown: bot token for %s could NOT be revoked and "
                "remains valid — revoke it manually (auth.revoke) or rotate "
                "the app: %s",
                report.agent_id,
                exc,
            )
        if report.slack_token_revoked:
            # The token is dead; stop storing it. Separate tiny transaction on
            # purpose — the user delete above must not wait on Slack. Best-
            # effort like every other post-commit step: a failure here must
            # not raise out of delete_user_account — the token itself is
            # already revoked (dead at Slack), so the only thing lost is
            # that we keep a dead value around locally.
            try:
                await db.execute(
                    sa_update(AgentRegistry)
                    .where(AgentRegistry.agent_id == report.agent_id)
                    .values(slack_bot_token=None)
                )
                await db.commit()
            except Exception as exc:
                await db.rollback()
                report.errors.append(f"token clear: {exc}")
                logger.error(
                    "Deletion teardown: bot token for %s was revoked (dead at "
                    "Slack) but the stored value could NOT be cleared: %s",
                    report.agent_id,
                    exc,
                )

    logger.info("Account %s deleted: %s", report.user_id, report.summary())
    return report
