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

* **in-transaction** — suspend the linked agent, purge revisions/DMs/tenure
  key (and optionally the allowlist row), delete the user, COMMIT.
* **post-commit** — filesystem cleanup and Slack token revocation. External
  effects that cannot roll back run only once the DB state is durable; a
  failure lands on the report and in the log, never raises. A half-failed
  cleanup still leaves a *suspended* agent, which is the state that actually
  stops activity.

``status='suspended'`` and not ``'inactive'`` (D2): suspended is the
admin-only parked state, and ``set_agent_mute_state`` refuses to touch it —
so a manager unmute cannot resurrect a deleted PI's agent.
"""

import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AccessAllowlist,
    AgentRegistry,
    AppSetting,
    PiDmMessage,
    ProfileRevision,
    User,
)
from src.services.jhu_rules import TENURE_KEY_PREFIX
from src.services.slack_web import revoke_token_async

logger = logging.getLogger(__name__)

# Kept in sync by comment-reference, not import: profile_export.PROFILES_DIR
# (src/services/profile_export.py) writes _PUBLIC_DIR/{agent_id}.md, and the
# engine's memory writer (src/agent/agent.py, PROFILES_DIR / "memory") owns
# _MEMORY_DIR. Module-level so tests can monkeypatch them to a tmp_path.
_PUBLIC_DIR = Path("profiles/public")
_MEMORY_DIR = Path("profiles/memory")

# agent_id slugs are minted by our own code (lowercase last names, optional
# initial prefix / numeric suffix), but this function deletes files, so it
# refuses anything that could escape the two directories above.
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


def _agent_paths(agent_id: str) -> list[Path]:
    if not _SAFE_AGENT_ID.fullmatch(agent_id):
        raise ValueError(f"refusing file cleanup for unsafe agent_id {agent_id!r}")
    paths = [
        _PUBLIC_DIR / f"{agent_id}.md",
        _MEMORY_DIR / f"{agent_id}.md",  # legacy pre-partition memory file
        _MEMORY_DIR / agent_id,  # partitioned memory directory
    ]
    # --fresh archives whole memory trees under archive/<stamp>/; a deleted
    # PI's synthesized memory must be purged from those snapshots too.
    archive_root = _MEMORY_DIR / "archive"
    if archive_root.is_dir():
        paths.extend(sorted(archive_root.glob(f"*/{agent_id}")))
        paths.extend(sorted(archive_root.glob(f"*/{agent_id}.md")))
    return paths


def _delete_agent_files(agent_id: str, report: DeletionReport) -> None:
    for path in _agent_paths(agent_id):
        try:
            if path.is_dir():
                shutil.rmtree(path)
                report.files_deleted.append(str(path))
            elif path.exists():
                path.unlink()
                report.files_deleted.append(str(path))
        except OSError as exc:
            report.errors.append(f"file cleanup {path}: {exc}")
            logger.error("Deletion teardown: could not remove %s: %s", path, exc)


async def delete_user_account(
    db: AsyncSession,
    user: User,
    *,
    remove_from_allowlist: bool = False,
) -> DeletionReport:
    """Tear down and delete ``user``. Commits. See module docstring."""
    report = DeletionReport(user_id=user.id, orcid=user.orcid)

    agent = (
        await db.execute(
            select(AgentRegistry).where(AgentRegistry.user_id == user.id)
        )
    ).scalar_one_or_none()
    token: str | None = None

    if agent is not None:
        report.agent_id = agent.agent_id
        token = agent.slack_bot_token
        if agent.status != "suspended":
            agent.status = "suspended"
            report.agent_suspended = True
        res = await db.execute(
            sa_delete(ProfileRevision).where(
                ProfileRevision.agent_registry_id == agent.id
            )
        )
        report.revisions_deleted = res.rowcount or 0
        res = await db.execute(
            sa_delete(PiDmMessage).where(PiDmMessage.agent_id == agent.agent_id)
        )
        report.dms_deleted += res.rowcount or 0

    # Local-transport DMs are keyed by the string "local:<users.id>", the one
    # place a users.id value outlives its row (audit F4) — purge these even
    # when the agent row was unlinked before this teardown existed.
    res = await db.execute(
        sa_delete(PiDmMessage).where(PiDmMessage.pi_user_id == f"local:{user.id}")
    )
    report.dms_deleted += res.rowcount or 0

    res = await db.execute(
        sa_delete(AppSetting).where(
            AppSetting.key == f"{TENURE_KEY_PREFIX}{user.id}"
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

    # ---- post-commit, best-effort: external effects that cannot roll back ----
    if report.agent_id is not None:
        try:
            _delete_agent_files(report.agent_id, report)
        except ValueError as exc:
            report.errors.append(str(exc))
            logger.error("Deletion teardown: %s", exc)

    if token:
        try:
            report.slack_token_revoked = await revoke_token_async(token)
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
