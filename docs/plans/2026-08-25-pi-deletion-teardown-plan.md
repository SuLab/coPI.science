# PI-Deletion Teardown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make deleting a PI actually remove them — stop their agent, revoke its Slack token, purge everything the confirmation page promises to purge, close the guard gaps (last admin, impersonation, allowlist resurrection), and harden every surface that currently 500s or corrupts against an orphaned agent.

**Architecture:** All deletion policy moves into one service, `src/services/user_deletion.py::delete_user_account`, with a DB phase (suspend agent, purge, delete user, commit) and a post-commit best-effort phase (filesystem cleanup, Slack token revocation). Both delete routes call it. Independent hardening lands in the roster query (one shared helper for both call sites), the delegate-facing agent pages, the auth allowlist gate, and the worker's job lifecycle.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async, Postgres (testcontainers in tests), slack_sdk via the `src/services/slack_web.py` boundary, pytest.

**Spec:** `docs/audits/2026-08-25-pi-deletion/README.md` (findings F1–F11, decisions D1–D9). Read it first — every task below cites the finding it fixes.

## Global Constraints

- Run tests on the **host**: `.venv-test/bin/python -m pytest tests/... -v`. Never `pip install` into `.venv-test` from an sshfs client. Full gate before the final push: `./scripts/ci.sh`.
- `ruff check tests/` must report zero findings; `src/` has a ratcheted ceiling — do not add new violations.
- **No schema migration in this plan (D9).** If you think you need DDL, stop — you've misread a task.
- Never touch `docker-compose.prod.yml` (uncommitted, load-bearing working-tree edit). Never run `pytest --snapshot-update`.
- `slack_sdk` may be imported in exactly two modules (`tests/unit/test_slack_boundary.py` pins this) — Slack work goes in `src/services/slack_web.py`, nowhere else.
- Do not add manager routes (`test_manager_router_mutations_are_an_explicit_allowlist` fails on a fifth mutation).
- Commit style: conventional (`fix(...)`, `feat(...)`, `test(...)`, `docs(...)`), each ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Forged-session test helper used throughout (copy into each new test file that needs it — several existing files carry their own copy by convention):

```python
import base64
import json

from itsdangerous import TimestampSigner

from src.config import get_settings


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}
```

---

### Task 1: `revoke_token` in the Slack boundary

**Files:**
- Modify: `src/services/slack_web.py` (add two functions + `__all__` entries)
- Test: `tests/unit/test_slack_revoke.py` (create)

**Interfaces:**
- Produces: `revoke_token(token: str) -> bool` and `revoke_token_async(token: str) -> bool` — True means the token is dead afterwards, including when it already was (`token_revoked`/`invalid_auth`/`account_inactive`). Raises `SlackApiError` on other failures (caller decides how to degrade).

- [ ] **Step 1: Write the failing tests**

```python
"""revoke_token: the deletion teardown's Slack half (audit F2 / decision D3)."""
import pytest
from slack_sdk.errors import SlackApiError

from src.services import slack_web


class _FakeClient:
    def __init__(self, result=None, error_code=None):
        self._result = result
        self._error_code = error_code

    def auth_revoke(self):
        if self._error_code:
            raise SlackApiError(
                message="boom", response={"ok": False, "error": self._error_code}
            )
        return self._result


def test_revoke_token_true_on_success(monkeypatch):
    monkeypatch.setattr(
        slack_web, "_client", lambda token: _FakeClient(result={"ok": True, "revoked": True})
    )
    assert slack_web.revoke_token("xoxb-live") is True


def test_revoke_token_true_when_already_dead(monkeypatch):
    for code in ("token_revoked", "invalid_auth", "account_inactive"):
        monkeypatch.setattr(
            slack_web, "_client", lambda token, c=code: _FakeClient(error_code=c)
        )
        assert slack_web.revoke_token("xoxb-dead") is True


def test_revoke_token_raises_on_other_errors(monkeypatch):
    # no_permission is in slack_web._TERMINAL but NOT in revoke_token's
    # already-dead trio, so _call raises immediately — no retry backoff, so
    # this test stays fast (a non-terminal code would time.sleep ~3.5s).
    monkeypatch.setattr(
        slack_web, "_client", lambda token: _FakeClient(error_code="no_permission")
    )
    with pytest.raises(SlackApiError):
        slack_web.revoke_token("xoxb-live")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_slack_revoke.py -v`
Expected: FAIL with `AttributeError: module 'src.services.slack_web' has no attribute 'revoke_token'`

- [ ] **Step 3: Implement**

Add to `src/services/slack_web.py`, after `post_message` (sync section) and after `post_message_async` (async section), and add both names to `__all__`:

```python
def revoke_token(token: str) -> bool:
    """Revoke a bot token (auth.revoke). True when the token is dead
    afterwards — including when it already was: a token that is
    ``token_revoked``/``invalid_auth``/``account_inactive`` cannot post, which
    is the outcome revocation exists to guarantee. Exists for the account-
    deletion teardown (docs/audits/2026-08-25-pi-deletion, D3); the app stays
    installed, only this token dies.
    """
    try:
        result = _call(_client(token), "auth_revoke")
    except SlackApiError as exc:
        if _error_code(exc) in {"token_revoked", "invalid_auth", "account_inactive"}:
            return True
        raise
    return bool(result.get("revoked"))
```

```python
async def revoke_token_async(token: str) -> bool:
    """``revoke_token`` off the event loop."""
    return await asyncio.to_thread(revoke_token, token)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_slack_revoke.py tests/unit/test_slack_boundary.py -v`
Expected: PASS (boundary test still green — no new slack_sdk import site)

- [ ] **Step 5: Commit**

```bash
git add src/services/slack_web.py tests/unit/test_slack_revoke.py
git commit -m "feat(slack): revoke_token in the slack_web boundary for account-deletion teardown"
```

---

### Task 2: the teardown service — DB phase

**Files:**
- Create: `src/services/user_deletion.py`
- Test: `tests/integration/test_user_deletion_service.py` (create)

**Interfaces:**
- Consumes: `revoke_token_async` from Task 1; `TENURE_KEY_PREFIX` from `src.services.jhu_rules`.
- Produces: `delete_user_account(db: AsyncSession, user: User, *, remove_from_allowlist: bool = False) -> DeletionReport` — commits the DB phase itself, then runs post-commit side effects (Task 3), never raising for side-effect failures. `DeletionReport` dataclass with fields `user_id: uuid.UUID`, `orcid: str`, `agent_id: str | None`, `agent_suspended: bool`, `revisions_deleted: int`, `dms_deleted: int`, `tenure_key_deleted: bool`, `allowlist_removed: bool`, `files_deleted: list[str]`, `slack_token_revoked: bool | None`, `errors: list[str]`, and a `summary() -> str` method for log lines.

- [ ] **Step 1: Write the failing tests**

```python
"""delete_user_account, DB phase (audit F1/F4/F5, decisions D1/D2/D4/D5)."""
import pytest
from sqlalchemy import select

from src.models import (
    AccessAllowlist,
    AgentRegistry,
    AppSetting,
    PiDmMessage,
    ProfileRevision,
    User,
)
from src.services.jhu_rules import TENURE_KEY_PREFIX
from src.services.user_deletion import delete_user_account
from tests import factories

pytestmark = pytest.mark.asyncio


async def _seed_pi_with_agent(db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user, status="active")
    db_session.add(
        ProfileRevision(
            agent_registry_id=agent.id,
            profile_type="public",
            content="full profile snapshot",
            mechanism="pipeline",
        )
    )
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        PiDmMessage(
            simulation_run_id=run.id,
            agent_id=agent.agent_id,
            pi_user_id=f"local:{user.id}",
            direction="inbound",
            content="my unpublished idea",
            ts="1.0",
            posted_at=1.0,
        )
    )
    db_session.add(
        AppSetting(key=f"{TENURE_KEY_PREFIX}{user.id}", value='{"year": 2019}')
    )
    await db_session.flush()
    return user, agent


async def test_teardown_suspends_agent_and_purges(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")
    user, agent = await _seed_pi_with_agent(db_session)
    user_id, agent_pk, agent_slug = user.id, agent.id, agent.agent_id

    report = await delete_user_account(db_session, user)

    assert (await db_session.get(User, user_id)) is None
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    assert refreshed.status == "suspended"
    assert refreshed.user_id is None
    assert (
        await db_session.scalar(
            select(ProfileRevision).where(
                ProfileRevision.agent_registry_id == agent_pk
            )
        )
    ) is None
    assert (
        await db_session.scalar(
            select(PiDmMessage).where(PiDmMessage.agent_id == agent_slug)
        )
    ) is None
    assert (
        await db_session.scalar(
            select(AppSetting).where(
                AppSetting.key == f"{TENURE_KEY_PREFIX}{user_id}"
            )
        )
    ) is None
    assert report.agent_id == agent_slug
    assert report.agent_suspended is True
    assert report.revisions_deleted == 1
    assert report.dms_deleted == 1
    assert report.tenure_key_deleted is True


async def test_teardown_without_agent_still_purges_user_keyed_rows(db_session):
    user = await factories.make_user(db_session)
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        PiDmMessage(
            simulation_run_id=run.id,
            agent_id="ghost",
            pi_user_id=f"local:{user.id}",
            direction="inbound",
            content="x",
            ts="1.0",
            posted_at=1.0,
        )
    )
    await db_session.flush()
    user_id = user.id

    report = await delete_user_account(db_session, user)

    assert (await db_session.get(User, user_id)) is None
    assert report.agent_id is None
    assert report.dms_deleted == 1


async def test_allowlist_removed_only_when_asked(db_session):
    u1 = await factories.make_user(db_session)
    u2 = await factories.make_user(db_session)
    db_session.add(AccessAllowlist(orcid=u1.orcid))
    db_session.add(AccessAllowlist(orcid=u2.orcid))
    await db_session.flush()
    o1, o2 = u1.orcid, u2.orcid

    await delete_user_account(db_session, u1)  # default: keep
    await delete_user_account(db_session, u2, remove_from_allowlist=True)

    kept = await db_session.scalar(
        select(AccessAllowlist).where(AccessAllowlist.orcid == o1)
    )
    gone = await db_session.scalar(
        select(AccessAllowlist).where(AccessAllowlist.orcid == o2)
    )
    assert kept is not None
    assert gone is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_user_deletion_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.services.user_deletion'`

- [ ] **Step 3: Implement the service (DB phase; post-commit phase stubs run but find nothing to do without an agent/token)**

Create `src/services/user_deletion.py`:

```python
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
    return [
        _PUBLIC_DIR / f"{agent_id}.md",
        _MEMORY_DIR / f"{agent_id}.md",  # legacy pre-partition memory file
        _MEMORY_DIR / agent_id,  # partitioned memory directory
    ]


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
            # purpose — the user delete above must not wait on Slack.
            await db.execute(
                sa_update(AgentRegistry)
                .where(AgentRegistry.agent_id == report.agent_id)
                .values(slack_bot_token=None)
            )
            await db.commit()

    logger.info("Account %s deleted: %s", report.user_id, report.summary())
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_user_deletion_service.py -v`
Expected: PASS (no token seeded, so the Slack path is not exercised yet — that's Task 3)

- [ ] **Step 5: Commit**

```bash
git add src/services/user_deletion.py tests/integration/test_user_deletion_service.py
git commit -m "feat(deletion): user_deletion service — suspend agent, purge revisions/DMs/tenure key on account delete"
```

---

### Task 3: the teardown service — post-commit phase (files + Slack)

**Files:**
- Modify: `src/services/user_deletion.py` (already written in Task 2 — this task *tests* the post-commit behaviors and fixes anything the tests flush out)
- Test: `tests/integration/test_user_deletion_service.py` (extend)

**Interfaces:**
- Consumes: `delete_user_account`, `DeletionReport` from Task 2.

- [ ] **Step 1: Write the failing tests (append to the Task 2 file)**

```python
async def test_files_are_deleted_post_commit(db_session, monkeypatch, tmp_path):
    pub = tmp_path / "pub"
    mem = tmp_path / "mem"
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", pub)
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", mem)
    user, agent = await _seed_pi_with_agent(db_session)
    slug = agent.agent_id
    pub.mkdir()
    (pub / f"{slug}.md").write_text("profile")
    (mem / slug / "private").mkdir(parents=True)
    (mem / f"{slug}.md").write_text("legacy memory")
    (mem / slug / "public.md").write_text("memory")
    (mem / slug / "private" / "C1.md").write_text("private memory")

    report = await delete_user_account(db_session, user)

    assert not (pub / f"{slug}.md").exists()
    assert not (mem / f"{slug}.md").exists()
    assert not (mem / slug).exists()
    assert len(report.files_deleted) == 3
    assert report.errors == []


async def test_unsafe_agent_id_skips_files_but_reports(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")
    user = await factories.make_user(db_session)
    await factories.make_agent(
        db_session, user=user, agent_id="../escape", status="active"
    )

    report = await delete_user_account(db_session, user)

    assert report.files_deleted == []
    assert any("unsafe agent_id" in e for e in report.errors)


async def test_token_revoked_and_cleared_on_success(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")
    calls = []

    async def _fake_revoke(token):
        calls.append(token)
        return True

    monkeypatch.setattr(
        "src.services.user_deletion.revoke_token_async", _fake_revoke
    )
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(
        db_session, user=user, status="active", slack_bot_token="xoxb-live"
    )
    agent_pk = agent.id

    report = await delete_user_account(db_session, user)

    assert calls == ["xoxb-live"]
    assert report.slack_token_revoked is True
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    assert refreshed.slack_bot_token is None


async def test_token_kept_when_revocation_fails(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")

    async def _fake_revoke(token):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(
        "src.services.user_deletion.revoke_token_async", _fake_revoke
    )
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(
        db_session, user=user, status="active", slack_bot_token="xoxb-live"
    )
    agent_pk = agent.id

    report = await delete_user_account(db_session, user)

    assert report.slack_token_revoked is False
    assert any("slack revoke" in e for e in report.errors)
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    # Failure keeps the token recorded so an operator can revoke it manually.
    assert refreshed.slack_bot_token == "xoxb-live"
    # The agent is still suspended — the state that actually stops activity.
    assert refreshed.status == "suspended"
```

- [ ] **Step 2: Run tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_user_deletion_service.py -v`
Expected: the four new tests exercise code written in Task 2 — some may already pass; any failure is a real defect in the Task 2 implementation (most likely candidates: rowcount handling, the unsafe-slug ValueError path, ordering of the post-commit steps). Fix the service until all pass. If everything passes first try, verify each test can fail by temporarily breaking the code path it covers (e.g. comment out the `_delete_agent_files` call), watching the test fail, then restoring.

- [ ] **Step 3: Commit**

```bash
git add src/services/user_deletion.py tests/integration/test_user_deletion_service.py
git commit -m "test(deletion): pin post-commit teardown — file cleanup, slug safety, token revocation semantics"
```

---

### Task 4: self-service route — guards, service call, honest copy

**Files:**
- Modify: `src/routers/profile.py:159-189`
- Modify: `templates/profile/delete_account.html`
- Test: `tests/integration/test_account_deletion_routes.py` (create)

**Interfaces:**
- Consumes: `delete_user_account` (Task 2), `USER_ROLE_ADMIN` from `src.models.user`.
- Produces: unchanged URLs; new redirect `?error=last_admin`; existing `?error=1` (wrong confirm word) is preserved byte-for-byte — `tests/integration/test_onboarding_flow.py:828-846` pins it.

- [ ] **Step 1: Write the failing tests**

```python
"""Route-level deletion guards (audit F7/F8, decision D6).

The test DB is savepoint-isolated per test (tests/conftest.py::db_session), so
each test starts from an empty users table and nothing here leaks across tests.
"""
import base64
import json
from types import SimpleNamespace

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import User
from src.services import user_deletion
from tests import factories

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def teardown_dirs(tmp_path, monkeypatch):
    """Redirect the teardown's file cleanup away from the repo's profiles/."""
    pub = tmp_path / "public"
    mem = tmp_path / "memory"
    monkeypatch.setattr(user_deletion, "_PUBLIC_DIR", pub)
    monkeypatch.setattr(user_deletion, "_MEMORY_DIR", mem)
    return SimpleNamespace(public=pub, memory=mem)


def _auth(user_id) -> dict:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


def _auth_as(admin_id, impersonate_id) -> dict:
    """Admin session plus the copi-impersonate cookie.

    Byte-identical to tests/integration/test_onboarding_flow.py:73 — the
    impersonate cookie is a PLAIN unsigned UUID (src/dependencies.py reads it
    with uuid.UUID(cookie_value), no signer).
    """
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(admin_id)}).encode())
    return {
        "Cookie": (
            f"copi-session={signer.sign(data).decode()}; "
            f"copi-impersonate={impersonate_id}"
        )
    }


async def test_impersonated_delete_is_refused(client, db_session):
    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")
    r = await client.post(
        "/profile/delete-account",
        headers=_auth_as(admin.id, pi.id),
        data={"confirm": "delete"},
    )
    assert r.status_code == 403
    assert (await db_session.get(User, pi.id)) is not None


async def test_last_admin_cannot_self_delete(client, db_session):
    # db_session is savepoint-isolated, but SOME suites commit real rows
    # through their own sessionmaker (test_worker.py, test_role_live_flip.py),
    # and a reused TEST_DATABASE_URL scratch DB can hold debris from a crashed
    # run. Demote any pre-existing loginable admins HERE (rolls back with the
    # savepoint) so the guard under test is what decides the outcome.
    debris = (
        await db_session.execute(
            select(User).where(
                User.user_role == "admin", User.access_status == "allowed"
            )
        )
    ).scalars().all()
    for u in debris:
        u.access_status = "denied"
    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    await db_session.flush()

    r = await client.post(
        "/profile/delete-account",
        headers=_auth(admin.id),
        data={"confirm": "delete"},
    )
    assert r.status_code == 302
    assert "error=last_admin" in r.headers["location"]
    assert (await db_session.get(User, admin.id)) is not None


async def test_admin_with_peers_can_self_delete(client, db_session):
    a1 = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    a2 = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    r = await client.post(
        "/profile/delete-account", headers=_auth(a1.id), data={"confirm": "delete"}
    )
    assert r.status_code == 302
    assert "deleted=1" in r.headers["location"]
    assert (await db_session.get(User, a1.id)) is None
    assert (await db_session.get(User, a2.id)) is not None


async def test_pi_delete_routes_through_teardown(client, db_session):
    """The route calls the service: a linked agent ends the request suspended."""
    from src.models import AgentRegistry

    pi = await factories.make_user(db_session, access_status="allowed")
    agent = await factories.make_agent(db_session, user=pi, status="active")
    agent_pk = agent.id
    r = await client.post(
        "/profile/delete-account", headers=_auth(pi.id), data={"confirm": "delete"}
    )
    assert r.status_code == 302
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    assert refreshed.status == "suspended"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_account_deletion_routes.py -v`
Expected: FAIL — impersonated delete currently succeeds (302, PI row gone), last-admin delete currently succeeds, agent stays `active`.

- [ ] **Step 3: Implement the route**

Replace `delete_account` in `src/routers/profile.py` (and extend the imports: `from fastapi import HTTPException`, `from sqlalchemy import func, select` — `select` is already imported — plus `from src.models.user import USER_ROLE_ADMIN` and `from src.services.user_deletion import delete_user_account`):

```python
@router.post("/delete-account")
async def delete_account(
    request: Request,
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete the user's own account after confirmation.

    Refuses impersonated sessions: an admin viewing an account through
    impersonation is on a support path, and account deletion is the one
    action there that cannot be undone (2026-08-22 audit §2.13; deletion
    audit F8). Admins delete accounts through /admin/users/{id}/delete,
    which logs the actor.
    """
    if getattr(current_user, "_is_impersonated", False):
        raise HTTPException(
            status_code=403,
            detail="Account deletion is disabled while impersonating.",
        )

    if confirm.lower() != "delete":
        return RedirectResponse(url="/profile/delete-account?error=1", status_code=302)

    # The same "at least one admin can still log in" invariant the role
    # route defends (src/routers/admin.py) — deletion is the other door out
    # of adminhood, and it had no guard (deletion audit F7).
    if current_user.user_role == USER_ROLE_ADMIN:
        admin_count = await db.scalar(
            select(func.count(User.id)).where(
                User.user_role == USER_ROLE_ADMIN,
                User.access_status == "allowed",
            )
        )
        if (admin_count or 0) <= 1:
            return RedirectResponse(
                url="/profile/delete-account?error=last_admin", status_code=302
            )

    await delete_user_account(db, current_user)

    request.session.clear()
    response = RedirectResponse(url="/login?deleted=1", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response
```

- [ ] **Step 4: Make the confirmation page honest (audit F4, decision D4)**

In `templates/profile/delete_account.html`, replace the warning block (lines 8-17) with:

```html
        <div class="bg-red-50 border border-red-200 rounded-lg p-4 mb-6 text-sm text-red-700">
            <p class="font-medium mb-2">This action is permanent and cannot be undone.</p>
            <p>The following will be permanently deleted:</p>
            <ul class="list-disc list-inside mt-2 space-y-1">
                <li>Your research profile, all of its stored revisions, and the copy your agent reads</li>
                <li>All publications imported from ORCID</li>
                <li>Your direct messages with your agent</li>
                <li>All pending profile updates</li>
            </ul>
            <p class="mt-2">Your lab agent will be shut down and its Slack access revoked.</p>
            <p class="mt-2">What remains: messages your agent already posted in the shared
            workspace and completed assessments stay part of the simulation record,
            and may still show your name.</p>
        </div>
        {% if request.query_params.get('error') == 'last_admin' %}
        <div class="bg-yellow-50 border border-yellow-200 rounded-lg p-3 mb-4 text-sm text-yellow-800">
            You are the last administrator who can log in. Appoint another admin
            before deleting this account.
        </div>
        {% elif request.query_params.get('error') %}
        <div class="bg-yellow-50 border border-yellow-200 rounded-lg p-3 mb-4 text-sm text-yellow-800">
            Type the word <strong>delete</strong> exactly to confirm.
        </div>
        {% endif %}
```

- [ ] **Step 5: Update the impersonation sweep's CONTROL branch**

`tests/integration/test_onboarding_flow.py::test_no_logged_in_user_can_read_or_write_another_users_data` runs every `ENDPOINTS` entry twice: once with a non-admin's impersonate cookie (must be inert — that half still passes: the attacker self-deletes exactly as before), and once as a real admin CONTROL asserting the cookie **reaches the victim** (`control_after != control_before`, around line 1420). The new guard makes the admin's `POST /profile/delete-account` a 403 — which is *itself* proof the cookie was honored: only an impersonated session trips the guard; an ignored cookie would have self-deleted the admin with a 302. Encode that in the control branch's non-GET arm:

```python
    else:
        if ep.method == "POST" and ep.path == "/profile/delete-account":
            # The deletion impersonation guard (deletion audit F8) refuses
            # this one mutation outright. The 403 is still positive proof the
            # cookie was honoured: the guard fires on _is_impersonated, which
            # only get_current_user's impersonation path sets — an inert
            # cookie would have self-deleted the admin with a 302.
            assert r2.status_code == 403, (
                "the delete-account impersonation guard did not fire for an admin"
            )
            assert control_after == control_before, (
                "the refused delete still changed the victim's data"
            )
        else:
            assert control_after != control_before, (
                "the copi-impersonate cookie is inert even for an admin, so the "
                "negative assertions above are not testing anything"
            )
```

- [ ] **Step 6: Run the new tests plus the existing pins**

Run: `.venv-test/bin/python -m pytest tests/integration/test_account_deletion_routes.py tests/integration/test_onboarding_flow.py -v`
Expected: PASS — the deletion tests at lines 820-852 pin the confirm-word behavior this task must not change, and the sweep passes with the Step 5 carve-out. If the sweep fails anywhere else, stop and read the failure; do not widen the carve-out beyond this one endpoint.

- [ ] **Step 7: Commit**

```bash
git add src/routers/profile.py templates/profile/delete_account.html tests/integration/test_account_deletion_routes.py tests/integration/test_onboarding_flow.py
git commit -m "fix(deletion): self-delete goes through teardown; refuse impersonated and last-admin deletes; honest confirm page"
```

---

### Task 5: admin route — service call + allowlist checkbox

**Files:**
- Modify: `src/routers/admin.py:180-199`
- Modify: `templates/admin/user_detail.html` (Danger Zone block, around lines 147-160)
- Test: `tests/integration/test_account_deletion_routes.py` (extend)

**Interfaces:**
- Consumes: `delete_user_account` (Task 2).
- Produces: `POST /admin/users/{user_id}/delete` gains an optional form field `remove_from_allowlist` (any non-empty value = true; absent/empty = false).

- [ ] **Step 1: Write the failing tests (append to the Task 4 file)**

```python
async def test_admin_delete_removes_allowlist_when_asked(client, db_session):
    from src.models import AccessAllowlist

    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")
    db_session.add(AccessAllowlist(orcid=pi.orcid))
    await db_session.flush()
    orcid = pi.orcid

    r = await client.post(
        f"/admin/users/{pi.id}/delete",
        headers=_auth(admin.id),
        data={"remove_from_allowlist": "1"},
    )
    assert r.status_code == 302
    assert (
        await db_session.scalar(
            select(AccessAllowlist).where(AccessAllowlist.orcid == orcid)
        )
    ) is None


async def test_admin_delete_keeps_allowlist_by_default(client, db_session):
    from src.models import AccessAllowlist

    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")
    db_session.add(AccessAllowlist(orcid=pi.orcid))
    await db_session.flush()
    orcid = pi.orcid

    r = await client.post(
        f"/admin/users/{pi.id}/delete", headers=_auth(admin.id), data={}
    )
    assert r.status_code == 302
    assert (
        await db_session.scalar(
            select(AccessAllowlist).where(AccessAllowlist.orcid == orcid)
        )
    ) is not None


async def test_admin_delete_suspends_linked_agent(client, db_session):
    from src.models import AgentRegistry

    admin = await factories.make_user(
        db_session, user_role="admin", access_status="allowed"
    )
    pi = await factories.make_user(db_session, access_status="allowed")
    agent = await factories.make_agent(db_session, user=pi, status="active")
    agent_pk = agent.id

    r = await client.post(
        f"/admin/users/{pi.id}/delete", headers=_auth(admin.id), data={}
    )
    assert r.status_code == 302
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    assert refreshed.status == "suspended"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_account_deletion_routes.py -v`
Expected: the three new tests FAIL (agent stays active; allowlist field is ignored — the current route has no such parameter)

- [ ] **Step 3: Implement**

Replace `admin_delete_user` in `src/routers/admin.py` (add `from src.services.user_deletion import delete_user_account` to imports):

```python
@router.post("/users/{user_id}/delete")
async def admin_delete_user(
    user_id: uuid.UUID,
    request: Request,
    remove_from_allowlist: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete a user account (admin only) — through the full teardown."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    name = user.name
    report = await delete_user_account(
        db, user, remove_from_allowlist=bool(remove_from_allowlist)
    )
    logger.info(
        "Admin %s deleted user %s (%s): %s",
        current_user.name, name, user_id, report.summary(),
    )
    return RedirectResponse(url="/admin/users", status_code=302)
```

- [ ] **Step 4: Update the Danger Zone**

In `templates/admin/user_detail.html`, replace the Danger Zone block with:

```html
    <!-- Danger zone -->
    <div class="border border-red-200 rounded-xl p-6 mb-6">
        <h2 class="font-semibold text-red-700 mb-2">Danger Zone</h2>
        <p class="text-sm text-gray-600 mb-2">
            Permanently deletes this user's profile (including all stored
            revisions and the on-disk copy), publications, jobs, and agent DMs.
            A linked lab agent is suspended and its Slack token revoked.
            Messages the agent already posted and completed assessments remain
            in the simulation record.
        </p>
        <form method="POST" action="/admin/users/{{ target_user.id }}/delete"
              onsubmit="return confirm('Delete {{ target_user.name }}? This cannot be undone.')">
            <label class="flex items-center gap-2 text-sm text-gray-700 mb-4">
                <input type="checkbox" name="remove_from_allowlist" value="1" checked>
                Also remove this ORCID from the access allowlist
                (otherwise they can sign straight back in)
            </label>
            <button type="submit"
                    class="px-4 py-2 bg-red-600 text-white text-sm font-medium rounded-lg hover:bg-red-700">
                Delete User
            </button>
        </form>
    </div>
```

- [ ] **Step 5: Run tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_account_deletion_routes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/routers/admin.py templates/admin/user_detail.html tests/integration/test_account_deletion_routes.py
git commit -m "fix(deletion): admin delete goes through teardown, with an allowlist-removal checkbox"
```

---

### Task 6: the allowlist must not overrule a denial

**Files:**
- Modify: `src/routers/auth.py:247-249`
- Test: `tests/integration/test_auth_allowlist_gate.py` (create)

**Interfaces:** none new — a one-line predicate change.

- [ ] **Step 1: Write the failing test**

```python
"""The allowlist promotes PENDING users only (audit F6, decision D5)."""
import base64
import json

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import AccessAllowlist, User
from src.routers import auth as auth_module
from tests import factories

pytestmark = pytest.mark.asyncio


def _session_cookie(payload: dict) -> dict:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps(payload).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


def _fake_oauth(orcid: str):
    class _Client:
        async def fetch_token(self, url, code, grant_type):
            return {"orcid": orcid, "name": "Test User"}

    return _Client()


async def _callback(client, monkeypatch, orcid: str):
    monkeypatch.setattr(auth_module, "_get_oauth_client", lambda: _fake_oauth(orcid))

    async def _fake_profile(orcid_id):
        return {"orcid": orcid_id, "name": "Test User"}

    monkeypatch.setattr(auth_module, "fetch_orcid_profile", _fake_profile)
    return await client.get(
        "/auth/callback?code=c&state=s",
        headers=_session_cookie({"oauth_state": "s"}),
    )


async def test_denied_user_is_not_resurrected_by_allowlist(
    client, db_session, monkeypatch
):
    user = await factories.make_user(db_session, access_status="denied")
    db_session.add(AccessAllowlist(orcid=user.orcid))
    await db_session.flush()

    r = await _callback(client, monkeypatch, user.orcid)

    assert r.status_code == 302
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.access_status == "denied"


async def test_pending_user_is_still_promoted_by_allowlist(
    client, db_session, monkeypatch
):
    user = await factories.make_user(db_session, access_status="pending")
    db_session.add(AccessAllowlist(orcid=user.orcid))
    await db_session.flush()

    r = await _callback(client, monkeypatch, user.orcid)

    assert r.status_code == 302
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.access_status == "allowed"
```

- [ ] **Step 2: Run tests to verify the denied case fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_auth_allowlist_gate.py -v`
Expected: `test_denied_user_is_not_resurrected_by_allowlist` FAILS (status flips to "allowed"); the pending test passes.

- [ ] **Step 3: Implement**

In `src/routers/auth.py`, change line 248:

```python
        # Allowlist can promote an existing PENDING user to allowed.
        # 'denied' is an explicit admin decision and must not be overruled
        # by a seed list (deletion audit 2026-08-25, F6).
        if is_allowlisted and user.access_status == "pending":
```

- [ ] **Step 4: Run tests to verify both pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_auth_allowlist_gate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/routers/auth.py tests/integration/test_auth_allowlist_gate.py
git commit -m "fix(auth): the allowlist promotes pending users only — a denial survives re-login"
```

---

### Task 7: orphaned-agent surfaces stop 500ing

**Files:**
- Modify: `src/routers/agent_page.py` — five routes: `review_proposal` (:433), `reopen_proposal` (:513), `view_public_profile` (:855), `edit_public_profile` (:888), `save_public_profile` (:918)
- Test: `tests/integration/test_orphaned_agent_surfaces.py` (create)

**Interfaces:** none new. Behavior: GET pages on an orphaned agent redirect to `/agent`; write POSTs return 409.

- [ ] **Step 1: Write the failing tests**

```python
"""Delegate-facing routes on an agent whose PI was deleted (audit F9).

Legacy orphans (user_id NULL, still active) predate the teardown service;
these routes must degrade, not 500.
"""
import base64
import json

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from tests import factories

pytestmark = pytest.mark.asyncio


def _auth(user_id) -> dict:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


async def _orphan_with_delegate(db_session):
    from src.models import AgentDelegate

    delegate_user = await factories.make_user(db_session, access_status="allowed")
    agent = await factories.make_agent(db_session, status="active")  # user_id NULL
    db_session.add(
        AgentDelegate(agent_registry_id=agent.id, user_id=delegate_user.id)
    )
    await db_session.flush()
    return delegate_user, agent


async def test_public_profile_redirects_not_500(client, db_session):
    delegate, agent = await _orphan_with_delegate(db_session)
    r = await client.get(
        f"/agent/{agent.agent_id}/public-profile", headers=_auth(delegate.id)
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/agent"


async def test_public_profile_edit_redirects_not_500(client, db_session):
    delegate, agent = await _orphan_with_delegate(db_session)
    r = await client.get(
        f"/agent/{agent.agent_id}/public-profile/edit", headers=_auth(delegate.id)
    )
    assert r.status_code == 302


async def test_public_profile_save_refuses_not_500(client, db_session):
    delegate, agent = await _orphan_with_delegate(db_session)
    r = await client.post(
        f"/agent/{agent.agent_id}/public-profile/save",
        headers=_auth(delegate.id),
        data={"research_summary": "x"},
    )
    assert r.status_code == 409


async def test_review_refuses_not_500(client, db_session):
    # No ThreadDecision is seeded on purpose: the orphan guard sits directly
    # after get_agent_with_access, BEFORE the proposal lookup, so the 409 must
    # fire without ever touching the proposal. A 404 here means the guard is
    # in the wrong place.
    import uuid as _uuid

    delegate, agent = await _orphan_with_delegate(db_session)
    r = await client.post(
        f"/agent/{agent.agent_id}/proposals/{_uuid.uuid4()}/review",
        headers=_auth(delegate.id),
        data={"rating": "4"},
    )
    assert r.status_code == 409


async def test_reopen_refuses_not_500(client, db_session):
    import uuid as _uuid

    delegate, agent = await _orphan_with_delegate(db_session)
    r = await client.post(
        f"/agent/{agent.agent_id}/proposals/{_uuid.uuid4()}/reopen",
        headers=_auth(delegate.id),
        data={"guidance": "please reconsider"},
    )
    assert r.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_orphaned_agent_surfaces.py -v`
Expected: FAIL with 500s (`NoResultFound` / NOT NULL violations), not the asserted 302/409s

- [ ] **Step 3: Implement the guards**

In the two GET routes, `view_public_profile` and `edit_public_profile`, immediately after the existing `if agent.status != "active": return RedirectResponse(...)` line:

```python
    if agent.user_id is None:
        # Orphaned agent — its PI account was deleted before the teardown
        # service existed (deletion audit F9). There is no PI profile here.
        return RedirectResponse(url="/agent", status_code=302)
```

In the three POST routes — `save_public_profile` (immediately after its status check), and `review_proposal` / `reopen_proposal` (immediately after the `get_agent_with_access(...)` call, before the agent-status check and before the ThreadDecision lookup; the tests above assert that ordering by posting a nonexistent proposal id) — writes refuse with a 409, matching the Interfaces line and the tests:

```python
    if agent.user_id is None:
        # Orphaned agent (deletion audit F9): save would build
        # ResearcherProfile(user_id=None) and review/reopen would build
        # ProposalReview(user_id=None) — both NOT NULL columns. Refuse loudly;
        # replaying a POST as a redirect would just hide the state.
        raise HTTPException(
            status_code=409,
            detail="This lab is no longer linked to a PI account",
        )
```

- [ ] **Step 4: Run the new tests plus the proposal-review suite**

Run: `.venv-test/bin/python -m pytest tests/integration/test_orphaned_agent_surfaces.py tests/integration/test_proposal_review.py -v`
Expected: PASS (linked-agent behavior unchanged)

- [ ] **Step 5: Commit**

```bash
git add src/routers/agent_page.py tests/integration/test_orphaned_agent_surfaces.py
git commit -m "fix(agent-page): degrade cleanly on orphaned agents instead of 500ing"
```

---

### Task 8: one roster criterion, orphan-free

**Files:**
- Create: `src/agent/roster_query.py`
- Modify: `src/agent/simulation.py:7157-7165` (`_sync_roster_from_db`), `src/agent/main.py:160-176` (startup roster load)
- Test: `tests/unit/test_roster_query.py` (create)

**Interfaces:**
- Produces: `active_roster_select()` returning a SQLAlchemy `Select` of exactly the five columns both call sites read: `agent_id, bot_name, pi_name, slack_bot_token, role`, ordered by `agent_id`.

- [ ] **Step 1: Write the failing tests**

```python
"""The single roster criterion (audit F1, decision D7): active, and for
pi_lab rows, linked to a user. Hub/specialist roles carry no user by design."""
import pytest

from src.agent.roster_query import active_roster_select
from tests import factories

pytestmark = pytest.mark.asyncio


async def test_orphaned_pi_lab_is_excluded(db_session):
    user = await factories.make_user(db_session)
    linked = await factories.make_agent(db_session, user=user, status="active")
    orphan = await factories.make_agent(db_session, status="active")  # user_id NULL
    hub = await factories.make_agent(db_session, status="active", role="scout_hub")
    suspended = await factories.make_agent(
        db_session, user=await factories.make_user(db_session), status="suspended"
    )

    rows = (await db_session.execute(active_roster_select())).all()
    ids = {r.agent_id for r in rows}

    assert linked.agent_id in ids
    assert hub.agent_id in ids  # NULL user is fine for non-pi_lab roles
    assert orphan.agent_id not in ids
    assert suspended.agent_id not in ids


async def test_select_carries_the_five_roster_columns(db_session):
    user = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=user, status="active")
    row = (await db_session.execute(active_roster_select())).first()
    for col in ("agent_id", "bot_name", "pi_name", "slack_bot_token", "role"):
        assert hasattr(row, col)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roster_query.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.agent.roster_query'`

- [ ] **Step 3: Implement**

Create `src/agent/roster_query.py`:

```python
"""The single definition of "which agents belong on a live roster".

Two consumers — engine startup (src/agent/main.py) and the ~30s live sync
(SimulationEngine._sync_roster_from_db) — must agree, or an agent evicted by
one is resurrected by the other. pi_lab rows with user_id IS NULL are
excluded: that is the activation gate's invariant
(src/services/agent_activation.py — "no profile to stand behind this lab"),
enforced here for rows orphaned by a user deletion that predates the teardown
service (docs/audits/2026-08-25-pi-deletion, F1/D7). Hub and specialist roles
carry no user by design and are exempt.
"""
from sqlalchemy import or_, select
from sqlalchemy.sql import Select

from src.models import AgentRegistry


def active_roster_select() -> Select:
    return (
        select(
            AgentRegistry.agent_id,
            AgentRegistry.bot_name,
            AgentRegistry.pi_name,
            AgentRegistry.slack_bot_token,
            AgentRegistry.role,
        )
        .where(
            AgentRegistry.status == "active",
            or_(
                AgentRegistry.role != "pi_lab",
                AgentRegistry.user_id.isnot(None),
            ),
        )
        .order_by(AgentRegistry.agent_id)
    )
```

In `src/agent/simulation.py:_sync_roster_from_db`, replace the inline `sa_select(...).where(AgentRegistry.status == "active")` statement with:

```python
            from src.agent.roster_query import active_roster_select

            async with self.session_factory() as db:
                rows = (await db.execute(active_roster_select())).all()
```

(keep the surrounding imports it still needs — `AgentSlackClient`, `env_token`, `is_valid_token`; drop `sa_select`/`AgentRegistry` if now unused in that scope).

In `src/agent/main.py`, replace the `_stmt` construction:

```python
            if all_agents:
                _stmt = _select(
                    _AR.agent_id, _AR.bot_name, _AR.pi_name,
                    _AR.slack_bot_token, _AR.role,
                ).order_by(_AR.agent_id)
            else:
                from src.agent.roster_query import active_roster_select
                _stmt = active_roster_select()
            _rows = (await _db.execute(_stmt)).all()
```

(`--all-agents` keeps loading everything including orphans — it is the explicit debug override.)

Also in `src/agent/main.py`, both log lines that describe the filter currently say `"status='active'"` and would now lie. Update the two literals (the `"No agents in roster (filter=%s)"` error and the `"Roster: %d agents (%s)"` banner) to:

```python
"all statuses (--all-agents)" if all_agents else "status='active', pi_lab linked to a user"
```

Two operational consequences to note in the commit message or PR description, both intended:

- **`POST /admin/agents/{agent_id}/link` with an empty user becomes an eviction lever**: unlinking an *active* pi_lab agent (`src/routers/admin.py:1063` sets `user_id = None`) now removes it from the roster within ~30s. That is the activation-gate invariant applied consistently; it is called out in the deploy notes and the Task 10 CLAUDE.md section so nobody discovers it in production.

- [ ] **Step 4: Run the new tests plus the existing roster-sync suite**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roster_query.py tests/unit/test_roster_sync.py -v`
Expected: PASS. `test_roster_sync.py` fakes the DB result rows, so it exercises the diffing logic, not the WHERE clause — it must keep passing unchanged; the new file is what pins the criterion.

- [ ] **Step 5: Commit**

```bash
git add src/agent/roster_query.py src/agent/simulation.py src/agent/main.py tests/unit/test_roster_query.py
git commit -m "fix(roster): one shared roster criterion; pi_lab agents without a user never load"
```

---

### Task 9: worker survives a mid-flight deletion

**Files:**
- Modify: `src/worker/main.py:80-111` (`process_job`)
- Modify: `src/services/profile_pipeline.py` (just before the markdown export, after `agent_id = agent_reg.agent_id if agent_reg else None`)
- Modify: `tests/integration/test_worker.py` — two characterization tests pin the *defective* except-branch behavior this task fixes, and both self-describe as stale-once-fixed (their own assertion messages say so). Updating them is part of this task, not an accident discovered at the final gate.
- Test: `tests/unit/test_worker_deletion_races.py` (create)

**Interfaces:** none new — `process_job(job_id, job_type, job_attempts, job_max_attempts, session_factory)` keeps its signature.

- [ ] **Step 1: Write the failing tests**

```python
"""process_job vs. a concurrent account deletion (audit F10, decision D8).

These tests CANNOT use the savepoint-isolated ``db_session`` fixture:
``process_job`` opens its own sessions/connections, which can never see rows
that exist only inside another connection's uncommitted savepoint
(tests/conftest.py::db_session, ``join_transaction_mode="create_savepoint"``).
So they commit real rows through the same factory the worker uses, and clean
up in ``finally`` — deleting the seeded user cascades the jobs row, so one
DELETE is the whole cleanup.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import Job
from src.worker import main as worker_main
from tests import factories

pytestmark = pytest.mark.asyncio


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_committed_job(session_factory):
    """A real, committed user + generate_profile job. Returns (user_id, job_id)."""
    async with session_factory() as s:
        user = await factories.make_user(s)
        job = Job(type="generate_profile", user_id=user.id, payload={})
        s.add(job)
        await s.flush()
        user_id, job_id = user.id, job.id
        await s.commit()
    return user_id, job_id


async def _delete_user(session_factory, user_id):
    async with session_factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await s.commit()


async def test_vanished_job_row_is_skipped_quietly(session_factory):
    user_id, job_id = await _seed_committed_job(session_factory)
    # The user delete cascades the jobs row — the exact race window between
    # claim_job's commit and process_job's re-fetch.
    await _delete_user(session_factory, user_id)

    # Must return, not raise (NoResultFound used to escape to the loop logger).
    await worker_main.process_job(job_id, "generate_profile", 1, 3, session_factory)


async def test_failure_after_deletion_does_not_raise(session_factory, monkeypatch):
    user_id, job_id = await _seed_committed_job(session_factory)

    async def _delete_user_then_fail(job, db):
        await _delete_user(session_factory, user_id)
        raise RuntimeError("pipeline blew up mid-flight")

    monkeypatch.setattr(
        worker_main, "execute_generate_profile", _delete_user_then_fail
    )
    # Used to raise StaleDataError from inside the except handler.
    await worker_main.process_job(job_id, "generate_profile", 1, 3, session_factory)


async def test_normal_failure_still_marks_retry(session_factory, monkeypatch):
    user_id, job_id = await _seed_committed_job(session_factory)
    try:
        async def _fail(job, db):
            raise RuntimeError("ordinary failure")

        monkeypatch.setattr(worker_main, "execute_generate_profile", _fail)
        await worker_main.process_job(
            job_id, "generate_profile", 1, 3, session_factory
        )

        async with session_factory() as check:
            row = await check.get(Job, job_id)
            assert row.status == "pending"  # attempts < max_attempts: retried
            assert "ordinary failure" in (row.last_error or "")
    finally:
        await _delete_user(session_factory, user_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_worker_deletion_races.py -v`
Expected: first test FAILS with `NoResultFound`; second FAILS with `StaleDataError` (or `InFailedSQLTransactionError`); third may pass (regression pin).

- [ ] **Step 3: Implement**

Replace `process_job` in `src/worker/main.py`:

```python
async def process_job(job_id: uuid.UUID, job_type: str, job_attempts: int, job_max_attempts: int, session_factory: async_sessionmaker) -> None:
    """Process a single job. Handles errors and updates job status.

    The jobs row can vanish at any await: jobs.user_id is ON DELETE CASCADE,
    so an account deletion mid-run takes the row with it (deletion audit F10).
    Both the initial re-fetch and the failure bookkeeping tolerate that — the
    account is gone, so there is no state anyone still needs updated.
    """
    async with session_factory() as db:
        # Re-fetch the job in this session so SQLAlchemy tracks changes
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            logger.info(
                "Job %s no longer exists (user deleted between claim and "
                "processing); skipping", job_id,
            )
            return

        try:
            if job.type == "generate_profile":
                await execute_generate_profile(job, db)
            elif job.type == "monthly_refresh":
                await execute_monthly_refresh(job, db)
            else:
                raise ValueError(f"Unknown job type: {job.type}")

            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Job %s completed", job.id)

        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
            # The pipeline may have left the transaction aborted (e.g. an FK
            # violation after a concurrent user delete) — clear it before the
            # bookkeeping writes, then re-check the row still exists.
            await db.rollback()
            result = await db.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one_or_none()
            if job is None:
                logger.info(
                    "Job %s row is gone (user deleted mid-run); "
                    "dropping the result", job_id,
                )
                return
            job.last_error = str(exc)[:2000]

            if job.attempts >= job.max_attempts:
                job.status = "dead"
                logger.warning("Job %s marked as dead after %d attempts", job_id, job.attempts)
            else:
                job.status = "pending"  # Will be retried

            job.completed_at = datetime.now(timezone.utc)
            try:
                await db.commit()
            except Exception:
                # Lost a second race on the same delete; nothing left to save.
                await db.rollback()
                logger.info("Job %s vanished during failure bookkeeping", job_id)
```

In `src/services/profile_pipeline.py`, insert immediately after `agent_id = agent_reg.agent_id if agent_reg else None` (before the export block):

```python
    # A concurrent account deletion can commit while this pipeline is between
    # flushes (the worker holds no lock on the users row). Re-check before the
    # export below: it is a plain filesystem write outside any transaction, so
    # without this it would recreate profiles/public/{agent_id}.md for an
    # account that no longer exists (deletion audit 2026-08-25, F10).
    if await db.scalar(select(User.id).where(User.id == user.id)) is None:
        raise ValueError(
            f"User {user.id} was deleted mid-pipeline; aborting before export"
        )
```

- [ ] **Step 4: Update the two characterization tests that pin the old defect**

In `tests/integration/test_worker.py`:

(a) `test_a_database_error_in_the_pipeline_orphans_the_job_in_processing` (~line 760) pins that a DB error escapes `process_job` and strands the job in `processing` with `last_error IS NULL` — its own assertion message says "if the error handler now survives a database error this test should assert 'pending' and the bug report is stale". That day is now. Rename it `test_a_database_error_in_the_pipeline_is_recorded_and_retried`, drive it through `_one_round` instead of `_one_round_expecting_escape`, and flip the three assertions:

```python
    claimed = await _one_round(wk.factory)
    assert claimed is not None and claimed.id == jid

    state = await wk.job_state(jid)
    assert state.status == "pending", (
        f"the job is {state.status!r}; a database error must be recorded and "
        "retried like any other failure (deletion audit F10 / D8)"
    )
    assert "duplicate key" in ((await wk.job(jid)).last_error or ""), (
        "the failure reason never reached the row"
    )
```

Rewrite its docstring to describe the fixed behavior (rollback-then-refetch in the except branch), keep its CONTROL half unchanged (it already asserts the fixed shape), and **delete `_one_round_expecting_escape`** (~line 818) — nothing else uses it, and a dead helper that asserts a defect is a trap.

(b) `test_a_crash_after_partial_work_leaves_a_retryable_job` (~line 709) pins `leaked == 1` — the old handler committed the pipeline's partial writes alongside the failure record, and its message says the docstring becomes "out of date" the day that's fixed. Flip it:

```python
    assert leaked == 0, (
        "the pipeline's partial writes were committed alongside the failure "
        "record — the except branch must roll back before its bookkeeping"
    )
```

and update the docstring's NOTE paragraph to say the except branch now rolls back before writing `last_error`, so partial work is discarded and the retry starts clean.

- [ ] **Step 5: Run the new tests plus every suite this touches**

Run: `.venv-test/bin/python -m pytest tests/unit/test_worker_deletion_races.py tests/integration/test_worker.py tests/unit/test_job_progress_durability.py tests/characterization/test_profile_pipeline_gm.py -v`
Expected: PASS (the characterization run confirms the pipeline insert didn't disturb the golden-master flow — its fake users exist, so the new check is a no-op there). Never `--snapshot-update` on a mismatch; a mismatch means the insertion point is wrong.

- [ ] **Step 6: Commit**

```bash
git add src/worker/main.py src/services/profile_pipeline.py tests/unit/test_worker_deletion_races.py tests/integration/test_worker.py
git commit -m "fix(worker): tolerate account deletion mid-job — quiet skip, rollback-then-refetch, pre-export existence check"
```

---

### Task 10: documentation + deploy notes

**Files:**
- Modify: `CLAUDE.md` (new section after "Account Types (PI / manager / admin)")
- Test: none (docs) — but run `tests/unit/test_claude_md_disclosure_sync.py` to confirm the CLAUDE.md edit didn't trip the drift alarm.

- [ ] **Step 1: Add the CLAUDE.md section**

```markdown
## Deleting a PI

**All deletion goes through `src/services/user_deletion.py::delete_user_account`**
(both `POST /profile/delete-account` and `POST /admin/users/{id}/delete`).
Never `db.delete(user)` directly — before 2026-08-25 that was the whole
process, and it left the deleted PI's agent RUNNING: `agents.user_id` is SET
NULL, the roster sync loads by status alone, and the agent reads its persona
from `profiles/public/{agent_id}.md`, not from the users table. See
`docs/audits/2026-08-25-pi-deletion/README.md`.

What the teardown does: suspends the linked agent (`status='suspended'` — the
one state a manager unmute cannot undo), purges `profile_revisions` (full
profile snapshots), the agent's `pi_dm_messages`, the
`jhu_tenure_start:{user_id}` app_settings key, the on-disk
`profiles/public/{agent_id}.md` and `profiles/memory/{agent_id}` artifacts,
and revokes the Slack bot token (post-commit, best-effort — a failed
revocation is logged loudly and leaves the token in the DB column for manual
revocation; the agent is suspended either way). The agent ROW is kept: it is
the record behind old messages and assessments, and its `agent_id` slug stays
reserved. Deliberately retained: `agent_messages`, `llm_call_logs`,
assessments, and everything already posted to Slack — both confirmation pages
say so.

Guards: an impersonating admin cannot trigger the self-service delete (403);
the last loginable admin cannot self-delete; the admin form has a
default-checked "also remove from the access allowlist" checkbox — without it
a deleted allowlisted ORCID can sign straight back in as `allowed`. Related:
the allowlist promotes only `pending` users at login; a `denied` user stays
denied (`src/routers/auth.py`).

The roster criterion lives in `src/agent/roster_query.py` and excludes
`pi_lab` rows with `user_id IS NULL` (hub/specialists are exempt). Both the
startup load and `_sync_roster_from_db` use it; `--all-agents` bypasses it.
One consequence to know before touching **/admin/agents → Link**: UNLINKING an
active `pi_lab` agent (submitting the link form with an empty user) now evicts
it from the running roster within ~30s — the same invariant, applied live.
```

- [ ] **Step 2: Run the drift alarm + docs-adjacent tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_claude_md_disclosure_sync.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/audits/2026-08-25-pi-deletion/README.md docs/plans/2026-08-25-pi-deletion-teardown-plan.md
git commit -m "docs(deletion): deletion audit, teardown semantics in CLAUDE.md, implementation plan"
```

---

### Task 11: full gate

- [ ] **Step 1: Run the whole suite**

Run: `./scripts/ci.sh`
Expected: alembic sanity green (no new migrations — the round trip is unchanged), ruff green, full pytest green at or above the branch-coverage floor.

- [ ] **Step 2: Fix anything it surfaces, then final commit if needed**

---

## Deploy notes (operator checklist — not part of the code tasks)

1. **Preflight — count existing orphans before the roster change ships:**
   ```bash
   docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -t -A -c \
     "SELECT agent_id, status FROM agents WHERE role='pi_lab' AND user_id IS NULL AND status='active';"
   ```
   Any row listed will be **evicted from the roster** on the first sync after
   the new agent image starts. If one should keep running, relink it first
   (`/admin/agents/{id}/link`). If none should, no action. Note the ongoing
   coupling this creates: unlinking an active pi_lab agent through that same
   admin form now evicts it live (~30s) — intended, but tell the operators.
2. No migration to apply (`alembic heads` unchanged at 0037).
3. Rebuild **all three** images — `src/services`, `src/routers`, `src/worker`
   and `src/agent` all changed: `$DC build blackbird-app worker && $DC
   --profile agent build agent`.
4. The agent-run container loads code at startup: **flag to the owner that the
   running simulation needs a stop/start** (per the CLAUDE.md restart
   procedure, `docker stop -t 420`, save logs, rebuild, restart) to pick up
   the roster filter.
5. After deploy, verify the 0036 FK is live (the deletion path depends on it):
   ```sql
   SELECT confdeltype FROM pg_constraint
   WHERE conname='private_channel_members_user_id_fkey';  -- must be 'c'
   ```

---

## Adversarial-review record (2026-08-25)

This plan was red-teamed twice before being handed to an executor; the fixes
are already folded into the tasks above. Kept here so nobody re-litigates.

**Round 1 (author's own pass):** the worker-race tests originally seeded
through the savepoint-isolated `db_session`, whose rows are invisible to the
worker's own connections — rewritten to commit real rows with cleanup (Task
9); the forged `copi-impersonate` cookie was signed, but the real cookie is a
plain UUID — helper replaced with the repo's proven one (Task 4); the orphan
409 tests depended on an unverified factory signature — replaced with
nonexistent-proposal POSTs that also pin guard ordering (Task 7).

**Round 2 (independent reviewer, verified against code):** two blockers —
the impersonation sweep's CONTROL half required the admin-impersonated delete
to *succeed* (now carved out: the 403 is itself proof the cookie was honored;
Task 4 step 5), and `tests/integration/test_worker.py` pinned the old
except-branch defect in two self-describing characterization tests (now
updated in Task 9 step 4, with the dead `_one_round_expecting_escape` helper
removed). One major — Task 7 said 302 in one place and 409 in another for
`save_public_profile`; resolved as 409 for all writes. Minors — lint-clean
test imports; the roster banner literals in `src/agent/main.py` now name the
real filter; the admin Link form's new eviction behavior is documented (Task
8/10 + deploy note 1); the last-admin test defends against committed debris
on reused scratch DBs; the Task 1 error-path test uses a terminal error code
so it doesn't sleep through retry backoff. Reviewer confirmed: every audit
finding F1–F11 and decision D1–D9 maps to a task, nothing in the plan is
untraceable to a finding, `WebClient.auth_revoke` exists in the installed
SDK, all factory/fixture/model usages check out, and no third roster call
site exists.
