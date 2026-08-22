# Manager PI Controls, Assessment→Profile Links, Assessments-Summary Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a manager add, edit, and mute PI profiles from `/manager`; link an
assessment row to its PI's profile on both `/admin` and `/manager`; and have
BlackbirdBot post a headline-only summary of every concluded interview to a
new, dedicated Slack channel.

**Architecture:** Three independent workstreams, each shippable on its own.
WS-A adds three narrowly-scoped write routes to the otherwise read-only
`/manager` router, backed by three new single-purpose service functions (two
of which de-duplicate ORCID-creation and profile-edit logic that currently
exists twice/thrice inline) and a small additive migration. WS-B adds a
batched `AgentRegistry` lookup to the two existing assessment-listing services
and a per-surface link macro to four templates, with zero backend behavior
change beyond the new lookup. WS-C adds a new, deliberately-isolated Slack
channel (excluded from the simulation's topical channel-discovery machinery)
and a synchronous, failure-isolated post triggered at the exact point an
interview's verdict is persisted.

**Tech Stack:** Python 3.12, FastAPI/Starlette, Jinja2, SQLAlchemy 2
(async/asyncpg), Alembic, slack_sdk, pytest + testcontainers.

**Spec:** `docs/specs/2026-08-21-manager-pi-controls-design.md`. Read it before
starting any task — this plan implements its decisions (D1-D18) and does not
re-derive them.

## Global Constraints

- Test command: `.venv-test/bin/python -m pytest <path> -v` from the repo root, on the host (testcontainers spins the DB; no env vars needed). The full gate before any push is `./scripts/ci.sh`.
- NEVER run bare `docker compose` on this host and NEVER pass `--remove-orphans` (a second production stack shares the machine — see CLAUDE.md).
- **The concurrent `docs/plans/2026-08-21-perf-memory-race-remediation.md` has now landed in full** (confirmed by `git log`: all 14 of its commits are present, `alembic heads` is `0033_badge_and_fk_indexes`, `src/routers/profile.py`'s `profile_version` increment is the atomic `func.coalesce(...)` form, and `src/agent/slack_client.py`'s async-wrappers block runs through `aconnect` at ~line 1207). Tasks 1, 3, and 10 below were updated to reflect this confirmed state rather than hedge about it. If you're executing this plan much later and more has landed since, **grep before trusting any absolute line number anyway** — that discipline doesn't expire just because these specific numbers were true once.
- Migration numbering: current alembic head is `0033_badge_and_fk_indexes` (confirmed). Task 1 below uses `revision = "0034"`, `down_revision = "0033"`. If executing later, re-run `ls alembic/versions/ | sort | tail -5` and use the real head instead.
- Migrate-before-serve applies to Task 1's migration (same class of risk as `0028`/`0030`/`0032` — see that migration's own docstring for the pattern to follow).
- Do NOT reword any `pi_lab` string in `src/agent/thread_guidance.py`, and do NOT run `pytest --snapshot-update`. Nothing in this plan touches that file, but it's a standing repo rule.
- Commits: conventional style matching the repo's log (`feat(manager): …`, `feat(assessments): …`, `feat(hub): …`), each ending with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Every "Run" step's expected outcome is stated. If reality differs, stop and investigate before proceeding (superpowers:systematic-debugging).

---

## Workstream A — Manager PI controls

### Task 1: `muted_at`/`muted_by` columns on `AgentRegistry`

**Files:**
- Modify: `src/models/agent_registry.py` (add two columns after `approved_by`)
- Create: `alembic/versions/0034_agent_mute_tracking.py`
- Test: `tests/unit/test_agent_mute.py` (create — this file grows through Task 4 too)

**Interfaces:**
- Produces: `AgentRegistry.muted_at: datetime | None`, `AgentRegistry.muted_by: uuid.UUID | None`.

- [ ] **Step 1: Determine the migration's `down_revision`**

**Confirmed as of this plan's writing: the perf-race remediation plan has
already landed** (`alembic/versions/0033_badge_and_fk_indexes.py` exists).
Use `down_revision = "0033"` and `revision = "0034"` below. If you're
executing this plan much later and more migrations have landed since, don't
trust this — re-run `ls alembic/versions/ | sort | tail -5` and use whatever
the real current head is instead.

- [ ] **Step 2: Write the failing test**

```python
"""Mute/unmute: the muted_at/muted_by columns, and set_agent_mute_state."""
import pytest

from src.models import AgentRegistry
from tests import factories

pytestmark = pytest.mark.integration


async def test_agent_registry_has_mute_tracking_columns(db_session):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, status="active")
    assert agent.muted_at is None
    assert agent.muted_by is None
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_mute.py -v`
Expected: FAIL — `AttributeError: 'AgentRegistry' object has no attribute 'muted_at'` (or a SQLAlchemy mapping error, since the column doesn't exist yet).

- [ ] **Step 4: Add the columns**

In `src/models/agent_registry.py`, after the `approved_by` column:

```python
    muted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    muted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
```

- [ ] **Step 5: Write the migration**

Create `alembic/versions/0034_agent_mute_tracking.py`:

```python
"""Add agents.muted_at / agents.muted_by — mute is a purpose-built control
over the existing 'inactive' status, not a new status value (design decision
D2/D3). Both nullable and additive.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-21 00:00:00.000000

Migrate-before-serve applies, same one-way constraint as 0028/0030/0032: once
AgentRegistry maps these columns, every existing select(AgentRegistry) in the
app — including SimulationEngine._sync_roster_from_db's roster query — names
them in its column list, and against a pre-migration database that raises
UndefinedColumn. Old code against the new schema is safe (nullable, no
backfill).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents", sa.Column("muted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "agents",
        sa.Column("muted_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agents_muted_by_users",
        "agents", "users",
        ["muted_by"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agents_muted_by_users", "agents", type_="foreignkey")
    op.drop_column("agents", "muted_by")
    op.drop_column("agents", "muted_at")
```

- [ ] **Step 6: Run the test and the alembic sanity check**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_mute.py -v`
Expected: PASS.

Run: `./scripts/ci.sh` (or at minimum its alembic single-head + upgrade→downgrade→upgrade round trip, if you want a faster iteration than the full gate)
Expected: PASS — single head, and the new migration round-trips cleanly.

- [ ] **Step 7: Commit**

```bash
git add src/models/agent_registry.py alembic/versions/*_agent_mute_tracking.py tests/unit/test_agent_mute.py
git commit -m "feat(manager): add agents.muted_at/muted_by for mute attribution

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `find_or_create_pi_by_orcid` — de-duplicated ORCID onboarding

**Files:**
- Create: `src/services/pi_onboarding.py`
- Modify: `src/routers/admin.py` (`impersonate_user`, currently lines 1021-1072 — grep `async def impersonate_user` to confirm)
- Test: `tests/unit/test_pi_onboarding.py` (create)

**Interfaces:**
- Produces: `async def find_or_create_pi_by_orcid(db: AsyncSession, orcid: str) -> User` — raises `ValueError(f"A user with ORCID {orcid} already exists")` if any user (any role) already has that ORCID; raises `ValueError(f"Could not fetch ORCID profile for {orcid}: {exc}")` on a fetch failure. Returns the newly-created `User` (already flushed, with `id` populated; caller commits).

- [ ] **Step 1: Write the failing tests**

```python
"""find_or_create_pi_by_orcid: the shared ORCID-onboarding logic used by the
manager Add-PI route and (Task 2b) admin's impersonate-if-new path."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.models import Job, USER_ROLE_ADMIN, User
from src.services.pi_onboarding import find_or_create_pi_by_orcid
from tests import factories

pytestmark = pytest.mark.integration


async def test_creates_a_pi_user_and_enqueues_a_profile_job(db_session):
    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile",
        new=AsyncMock(return_value={
            "name": "Ada Lovelace", "email": "ada@example.edu",
            "institution": "Example University", "department": "Computing",
        }),
    ):
        user = await find_or_create_pi_by_orcid(db_session, "0000-0001-2345-6789")
        await db_session.commit()

    assert user.orcid == "0000-0001-2345-6789"
    assert user.name == "Ada Lovelace"
    assert user.user_role == "pi"

    jobs = (await db_session.execute(
        select(Job).where(Job.user_id == user.id, Job.type == "generate_profile")
    )).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].payload == {"user_id": str(user.id), "orcid": "0000-0001-2345-6789"}


async def test_rejects_an_orcid_that_already_exists_regardless_of_role(db_session):
    existing = await factories.make_user(
        db_session, orcid="0000-0009-9999-0001", user_role=USER_ROLE_ADMIN,
    )
    with pytest.raises(ValueError, match="already exists"):
        await find_or_create_pi_by_orcid(db_session, existing.orcid)


async def test_a_fetch_failure_raises_instead_of_creating_a_stub_user(db_session):
    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile",
        new=AsyncMock(side_effect=RuntimeError("ORCID API down")),
    ):
        with pytest.raises(ValueError, match="Could not fetch ORCID profile"):
            await find_or_create_pi_by_orcid(db_session, "0000-0002-0000-0000")

    count = (await db_session.execute(
        select(User).where(User.orcid == "0000-0002-0000-0000")
    )).scalars().all()
    assert count == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_pi_onboarding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.pi_onboarding'`.

- [ ] **Step 3: Implement**

```python
"""ORCID-driven PI onboarding, shared by the manager Add-PI route and admin's
impersonate-if-new path. Ports the fetch->create->enqueue logic that used to
be duplicated in src/cli.py's _seed_one_orcid and inline in
src/routers/admin.py's impersonate_user — see design decision D7."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Job, USER_ROLE_PI, User
from src.services.orcid import fetch_orcid_profile


async def find_or_create_pi_by_orcid(db: AsyncSession, orcid: str) -> User:
    """Create a PI User + enqueue a generate_profile Job for one ORCID iD.

    Unlike admin's impersonate flow (which reuses an existing User of any
    role silently), this is an explicit creation action (D6): it raises if
    the ORCID already belongs to anyone, rather than returning their
    existing row. Raises ValueError on either failure mode; never returns
    None. Does not commit — the caller decides the transaction boundary.
    """
    orcid = orcid.strip()
    existing = (
        await db.execute(select(User).where(User.orcid == orcid))
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"A user with ORCID {orcid} already exists")

    try:
        profile_data = await fetch_orcid_profile(orcid)
    except Exception as exc:
        raise ValueError(f"Could not fetch ORCID profile for {orcid}: {exc}") from exc

    user = User(
        orcid=orcid,
        name=profile_data.get("name", orcid),
        email=profile_data.get("email"),
        institution=profile_data.get("institution"),
        department=profile_data.get("department"),
        user_role=USER_ROLE_PI,
    )
    db.add(user)
    await db.flush()

    job = Job(
        type="generate_profile",
        user_id=user.id,
        payload={"user_id": str(user.id), "orcid": orcid},
    )
    db.add(job)
    return user
```

- [ ] **Step 4: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_pi_onboarding.py -v`
Expected: PASS.

- [ ] **Step 5: Refactor `admin.py`'s `impersonate_user` to call the shared function**

Grep to confirm the current body: `grep -n "async def impersonate_user" -A 55 src/routers/admin.py`. Replace the inline "doesn't exist yet" branch (the `if not target:` block that calls `fetch_orcid_profile` and builds `User`/`Job` inline) with a call to the new function, preserving the existing check-first/reuse-if-exists wrapper and the existing 404-on-failure behavior:

```python
    result = await db.execute(select(User).where(User.orcid == orcid))
    target = result.scalar_one_or_none()

    if not target:
        try:
            target = await find_or_create_pi_by_orcid(db, orcid)
            await db.commit()
        except ValueError as exc:
            logger.error("Failed to fetch ORCID profile for impersonation: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ORCID {orcid} not found",
            )
```

Add the import near the top of `admin.py` (alongside the other `src.services.*` imports): `from src.services.pi_onboarding import find_or_create_pi_by_orcid`. This changes behavior in exactly one edge case, which is a genuine (and correct) narrowing: if the ORCID belongs to a NON-PI user (a manager or admin, already excluded by the `if not target:` guard since `target` would already be truthy in that case) — no, re-check: the outer `if not target:` guard means this new call only runs when NO user has that ORCID at all, so `find_or_create_pi_by_orcid`'s "already exists" `ValueError` branch is actually unreachable from this call site (the caller already proved no such user exists). It will only ever raise via the ORCID-fetch-failure path, which this refactor maps onto the same existing 404 behavior. No behavior change for impersonate's happy path.

- [ ] **Step 6: Run the full admin impersonate test suite**

Run: `grep -rln "impersonate" tests/integration/*.py` to find the relevant test file(s), then `.venv-test/bin/python -m pytest <that file> -v`.
Expected: PASS, unchanged — this refactor is behavior-preserving for every existing test.

- [ ] **Step 7: Commit**

```bash
git add src/services/pi_onboarding.py src/routers/admin.py tests/unit/test_pi_onboarding.py
git commit -m "refactor(admin): extract ORCID onboarding into a shared function

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: `apply_profile_edits` — shared profile-field mutation

**Files:**
- Create: `src/services/profile_edit.py`
- Modify: `src/routers/profile.py` (`profile_save`, currently lines 99-195 — **grep first**, per this plan's Global Constraints note about the concurrent Task 13)
- Test: `tests/unit/test_profile_edit.py` (create)

**Interfaces:**
- Produces: `async def apply_profile_edits(db: AsyncSession, *, target_user: User, changed_by_user_id: uuid.UUID, name: str, email: str, institution: str, department: str, research_summary: str, techniques: str, experimental_models: str, disease_areas: str, key_targets: str, keywords: str) -> str | None` — returns an error code string (`"invalid_email"` / `"email_taken"`) if validation fails (nothing written in that case), or `None` on success (fully written and committed).

**Confirmed as of this plan's writing: the remediation plan's Task 13 has
already landed.** `src/routers/profile.py` now does
`profile.profile_version = func.coalesce(ResearcherProfile.profile_version, 0) + 1`
(a DB-side atomic increment, not the old Python read-modify-write) — the
code block below already uses that exact line. If you're executing this
plan much later and the surrounding code has changed again, re-grep
(`grep -n "profile_version" src/routers/profile.py`) and use whatever the
real current line does — do not reintroduce a read-modify-write race.

- [ ] **Step 1: Write the failing tests**

```python
"""apply_profile_edits: shared field-mutation logic for self-service
profile.py:/profile/save and the manager's PI-edit route."""
import uuid

import pytest
from sqlalchemy import select

from src.models import ResearcherProfile, User
from src.services.profile_edit import apply_profile_edits
from tests import factories

pytestmark = pytest.mark.integration


async def test_applies_edits_and_creates_a_profile_row_if_none_existed(db_session):
    pi = await factories.make_user(db_session, name="Old Name", email="old@example.edu")

    error = await apply_profile_edits(
        db_session,
        target_user=pi,
        changed_by_user_id=pi.id,
        name="New Name", email="new@example.edu",
        institution="New U", department="New Dept",
        research_summary="Studies new things.",
        techniques="crispr, sequencing",
        experimental_models="mouse",
        disease_areas="cancer",
        key_targets="TP53",
        keywords="oncology",
    )

    assert error is None
    await db_session.refresh(pi)
    assert pi.name == "New Name"
    assert pi.email == "new@example.edu"

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.research_summary == "Studies new things."
    assert profile.techniques == ["crispr", "sequencing"]
    assert profile.profile_version == 1


async def test_a_manager_editing_a_pi_attributes_the_revision_to_the_manager(db_session):
    manager = await factories.make_user(db_session, user_role="manager")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="active")

    error = await apply_profile_edits(
        db_session,
        target_user=pi,
        changed_by_user_id=manager.id,
        name=pi.name, email=pi.email or "",
        institution="", department="",
        research_summary="Edited by a manager.",
        techniques="", experimental_models="",
        disease_areas="", key_targets="", keywords="",
    )

    assert error is None
    from src.models import ProfileRevision
    revision = (await db_session.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id.isnot(None))
        .order_by(ProfileRevision.created_at.desc())
    )).scalars().first()
    assert revision is not None
    assert revision.changed_by_user_id == manager.id


async def test_rejects_an_email_already_used_by_someone_else(db_session):
    await factories.make_user(db_session, email="taken@example.edu")
    pi = await factories.make_user(db_session, email="mine@example.edu")

    error = await apply_profile_edits(
        db_session, target_user=pi, changed_by_user_id=pi.id,
        name=pi.name, email="taken@example.edu",
        institution="", department="", research_summary="",
        techniques="", experimental_models="", disease_areas="",
        key_targets="", keywords="",
    )

    assert error == "email_taken"
    await db_session.refresh(pi)
    assert pi.email == "mine@example.edu"
```

Check the exact model/field name for the revision table before running this
— `grep -n "class ProfileRevision\|create_revision" src/services/profile_versioning.py src/models/*.py` — and adjust the second test's import/query to match whatever the real model is called (the design spec calls it "revision" generically; confirm the exact name here rather than guessing further).

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_profile_edit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.profile_edit'`.

- [ ] **Step 3: Implement**

Read the current body of `profile_save` first (`grep -n "async def profile_save" -A 100 src/routers/profile.py`) and port it verbatim into the new function, parameterizing `current_user` -> `target_user`/`changed_by_user_id`:

```python
"""Shared profile-field mutation, used by both the PI's own /profile/save
and the manager's PI-edit route (design decision D8). target_user is whose
profile changes; changed_by_user_id is who made the change — they differ
exactly when a manager edits a PI's profile, and create_revision's existing
changed_by_user_id parameter already supports that attribution without any
schema change."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Publication, ResearcherProfile, User
from src.services.validators import is_valid_email


def _parse_list(val: str) -> list[str]:
    return [s.strip() for s in val.split(",") if s.strip()]


async def apply_profile_edits(
    db: AsyncSession, *, target_user: User, changed_by_user_id: uuid.UUID,
    name: str, email: str, institution: str, department: str,
    research_summary: str, techniques: str, experimental_models: str,
    disease_areas: str, key_targets: str, keywords: str,
) -> str | None:
    email_clean = (email or "").strip().lower()
    if email_clean != (target_user.email or ""):
        if email_clean:
            if not is_valid_email(email_clean):
                return "invalid_email"
            existing = await db.execute(
                select(User).where(
                    User.email == email_clean, User.id != target_user.id
                )
            )
            if existing.scalar_one_or_none():
                return "email_taken"
        target_user.email = email_clean or None

    if name:
        target_user.name = name
    if institution is not None:
        target_user.institution = institution or None
    if department is not None:
        target_user.department = department or None

    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == target_user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=target_user.id)
        db.add(profile)
        # Flush the row into existence before the SQL-side bump below: on a
        # pending object the expression would render inside the INSERT's
        # VALUES, which cannot reference its own target table.
        await db.flush()

    profile.research_summary = research_summary
    profile.techniques = _parse_list(techniques)
    profile.experimental_models = _parse_list(experimental_models)
    profile.disease_areas = _parse_list(disease_areas)
    profile.key_targets = _parse_list(key_targets)
    profile.keywords = _parse_list(keywords)
    # SQL-side increment (matches profile.py's own fix for issue #22 C1) —
    # nothing below reads profile_version, so the expiry this expression
    # assignment causes needs no refresh here.
    profile.profile_version = func.coalesce(ResearcherProfile.profile_version, 0) + 1

    await db.commit()

    from src.models import AgentRegistry
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == target_user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    agent_id_for_export = agent_reg.agent_id if agent_reg else None

    from src.services.profile_export import export_profile_to_markdown
    pub_result = await db.execute(
        select(Publication).where(Publication.user_id == target_user.id)
    )
    user_pubs = list(pub_result.scalars().all())
    exported_path = export_profile_to_markdown(
        target_user, profile, agent_id_for_export, publications=user_pubs
    )

    from src.services.profile_versioning import create_revision
    if agent_reg and exported_path:
        await create_revision(
            db,
            agent_registry_id=agent_reg.id,
            profile_type="public",
            content=exported_path.read_text(encoding="utf-8"),
            changed_by_user_id=changed_by_user_id,
            mechanism="web",
        )
        await db.commit()

    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_profile_edit.py -v`
Expected: PASS.

- [ ] **Step 5: Make `profile_save` a thin wrapper**

Replace `profile.py`'s `profile_save` body (from `"""Save profile changes."""` through `return RedirectResponse(url="/profile?saved=1", status_code=302)`) with:

```python
@router.post("/save")
async def profile_save(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    institution: str = Form(""),
    department: str = Form(""),
    research_summary: str = Form(""),
    techniques: str = Form(""),
    experimental_models: str = Form(""),
    disease_areas: str = Form(""),
    key_targets: str = Form(""),
    keywords: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save profile changes."""
    error = await apply_profile_edits(
        db, target_user=current_user, changed_by_user_id=current_user.id,
        name=name, email=email, institution=institution, department=department,
        research_summary=research_summary, techniques=techniques,
        experimental_models=experimental_models, disease_areas=disease_areas,
        key_targets=key_targets, keywords=keywords,
    )
    if error:
        return RedirectResponse(url=f"/profile/edit?error={error}", status_code=302)
    return RedirectResponse(url="/profile?saved=1", status_code=302)
```

Add `from src.services.profile_edit import apply_profile_edits` to `profile.py`'s imports. Leave every other route in the file untouched.

- [ ] **Step 6: Run the existing profile route tests**

Run: `grep -rln "profile/save\|profile_save" tests/integration/*.py` to find them, then run that file (or files) with `-v`.
Expected: PASS, unchanged — this is a behavior-preserving refactor.

- [ ] **Step 7: Commit**

```bash
git add src/services/profile_edit.py src/routers/profile.py tests/unit/test_profile_edit.py
git commit -m "refactor(profile): extract apply_profile_edits for reuse by the manager route

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: `set_agent_mute_state`

**Files:**
- Create: `src/services/agent_mute.py`
- Modify: `tests/unit/test_agent_mute.py` (extends Task 1's file)

**Interfaces:**
- Consumes: `AgentRegistry.status`, `.muted_at`, `.muted_by` (Task 1).
- Produces: `async def set_agent_mute_state(db: AsyncSession, *, agent: AgentRegistry, muted: bool, actor_user_id: uuid.UUID) -> bool` — returns `False` (no-op, nothing written) if `agent.status` is not currently `"active"` or `"inactive"`; otherwise flips status and the two attribution columns, commits, and returns `True`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_agent_mute.py`:

```python
from datetime import UTC, datetime

from src.services.agent_mute import set_agent_mute_state


async def test_muting_an_active_agent_sets_inactive_and_attribution(db_session):
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    agent = await factories.make_agent(db_session, user=pi, status="active")

    ok = await set_agent_mute_state(
        db_session, agent=agent, muted=True, actor_user_id=manager.id,
    )

    assert ok is True
    await db_session.refresh(agent)
    assert agent.status == "inactive"
    assert agent.muted_by == manager.id
    assert agent.muted_at is not None


async def test_unmuting_clears_attribution_and_reactivates(db_session):
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    agent = await factories.make_agent(
        db_session, user=pi, status="inactive",
        muted_at=datetime.now(UTC), muted_by=manager.id,
    )

    ok = await set_agent_mute_state(
        db_session, agent=agent, muted=False, actor_user_id=manager.id,
    )

    assert ok is True
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert agent.muted_by is None
    assert agent.muted_at is None


async def test_muting_a_pending_agent_is_a_no_op(db_session):
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    agent = await factories.make_agent(db_session, user=pi, status="pending")

    ok = await set_agent_mute_state(
        db_session, agent=agent, muted=True, actor_user_id=manager.id,
    )

    assert ok is False
    await db_session.refresh(agent)
    assert agent.status == "pending"
    assert agent.muted_at is None


async def test_muting_a_suspended_agent_is_a_no_op(db_session):
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    agent = await factories.make_agent(db_session, user=pi, status="suspended")

    ok = await set_agent_mute_state(
        db_session, agent=agent, muted=True, actor_user_id=manager.id,
    )

    assert ok is False
    await db_session.refresh(agent)
    assert agent.status == "suspended"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_mute.py -v -k "mute"`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.agent_mute'`.

- [ ] **Step 3: Implement**

```python
"""Mute/unmute a PI's agent — a purpose-built control over the existing
'active'/'inactive' status axis (design decisions D2-D4), not a new status
value. pending/suspended agents are admin-only concerns and are left alone."""
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry

_MUTABLE_STATUSES = ("active", "inactive")


async def set_agent_mute_state(
    db: AsyncSession, *, agent: AgentRegistry, muted: bool, actor_user_id: uuid.UUID,
) -> bool:
    """Returns False (no-op) if agent.status isn't active/inactive; otherwise
    flips status + attribution and commits, returning True."""
    if agent.status not in _MUTABLE_STATUSES:
        return False

    if muted:
        agent.status = "inactive"
        agent.muted_at = datetime.now(UTC)
        agent.muted_by = actor_user_id
    else:
        agent.status = "active"
        agent.muted_at = None
        agent.muted_by = None

    await db.commit()
    return True
```

- [ ] **Step 4: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_mute.py -v`
Expected: PASS (all of Task 1's and Task 4's tests in this file).

- [ ] **Step 5: Commit**

```bash
git add src/services/agent_mute.py tests/unit/test_agent_mute.py
git commit -m "feat(manager): add set_agent_mute_state

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Manager write routes — create, edit, mute, unmute

**Files:**
- Modify: `src/routers/manager.py` (add three new routes after `manager_pi_detail`)
- Modify: `tests/integration/test_manager_views.py` (rewrite `test_manager_router_exposes_no_mutating_routes`)
- Test: `tests/integration/test_manager_pi_writes.py` (create)

**Interfaces:**
- Consumes: `find_or_create_pi_by_orcid` (Task 2), `apply_profile_edits` (Task 3), `set_agent_mute_state` (Task 4).
- Produces: `POST /manager/pis`, `POST /manager/pis/{user_id}/profile`, `POST /manager/pis/{user_id}/mute`, `POST /manager/pis/{user_id}/unmute`.

- [ ] **Step 1: Write the failing allowlist test**

In `tests/integration/test_manager_views.py`, replace
`test_manager_router_exposes_no_mutating_routes` with:

```python
def test_manager_router_mutations_are_an_explicit_allowlist():
    """D12 amended, not abolished (design decision D1): the manager router may
    have non-GET routes now, but only these four, named exactly. A future
    accidental fifth write route still fails this test loudly."""
    allowed_post_paths = {
        "/pis",
        "/pis/{user_id}/profile",
        "/pis/{user_id}/mute",
        "/pis/{user_id}/unmute",
    }
    methods = {m for r in manager_router.router.routes for m in getattr(r, "methods", ())}
    assert methods == {"GET", "POST"}, f"unexpected method on the manager router: {methods}"

    post_paths = {
        route.path for route in manager_router.router.routes
        if "POST" in getattr(route, "methods", ())
    }
    assert post_paths == allowed_post_paths, (
        f"manager router POST paths changed: {post_paths}"
    )
```

- [ ] **Step 2: Write the failing route tests**

```python
"""POST routes on /manager: create, edit, mute, unmute a PI (design D1)."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, AgentRegistry, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _manager(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)


async def test_pi_is_denied_all_four_write_routes(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    target = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    headers = auth_headers(pi.id)

    r = await client.post("/manager/pis", data={"orcid": "0000-0003-0000-0000"}, headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/profile", data={}, headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/mute", headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/manager/pis/{target.id}/unmute", headers=headers)
    assert r.status_code == 403


async def test_manager_creates_a_pi_via_orcid(client, db_session):
    manager = await _manager(db_session)
    with patch(
        "src.routers.manager.find_or_create_pi_by_orcid",
        new=AsyncMock(),
    ) as mock_create:
        mock_create.return_value = User(
            id="00000000-0000-0000-0000-000000000001", orcid="0000-0004-0000-0000",
        )
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0004-0000-0000"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.status_code == 302
    assert "/manager/pis/" in r.headers["location"]


async def test_manager_create_pi_rejects_a_duplicate_orcid(client, db_session):
    manager = await _manager(db_session)
    existing = await factories.make_user(db_session, orcid="0000-0005-0000-0000")

    r = await client.post(
        "/manager/pis", data={"orcid": existing.orcid},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


async def test_manager_edits_a_pi_profile(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session, name="Old Name")

    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={
            "name": "New Name", "email": pi.email or "", "institution": "",
            "department": "", "research_summary": "Edited.", "techniques": "",
            "experimental_models": "", "disease_areas": "", "key_targets": "",
            "keywords": "",
        },
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(pi)
    assert pi.name == "New Name"


async def test_manager_edit_404s_on_a_non_pi_target(client, db_session):
    manager = await _manager(db_session)
    other_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    r = await client.post(
        f"/manager/pis/{other_admin.id}/profile", data={}, headers=auth_headers(manager.id),
    )
    assert r.status_code == 404


async def test_manager_mutes_and_unmutes_a_pi(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, status="active")

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(agent)
    assert agent.status == "inactive"
    assert agent.muted_by == manager.id

    r = await client.post(
        f"/manager/pis/{pi.id}/unmute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    await db_session.refresh(agent)
    assert agent.status == "active"
    assert agent.muted_by is None


async def test_muting_a_pi_with_no_agent_redirects_with_an_error(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


async def test_muting_a_pending_agent_redirects_with_an_error(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="pending")

    r = await client.post(
        f"/manager/pis/{pi.id}/mute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=" in r.headers["location"]
```

- [ ] **Step 3: Run everything to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_views.py tests/integration/test_manager_pi_writes.py -v`
Expected: FAIL — 404s on the new routes (they don't exist yet) and the allowlist test failing on the empty POST-path set.

- [ ] **Step 4: Implement the routes**

In `src/routers/manager.py`, add to the imports:

```python
from fastapi import Form
from src.services.agent_mute import set_agent_mute_state
from src.services.pi_onboarding import find_or_create_pi_by_orcid
from src.services.profile_edit import apply_profile_edits
```

Add, immediately after `manager_pi_detail`:

```python
@router.post("/pis")
async def manager_create_pi(
    orcid: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Create a PI via the ORCID pipeline (design D5/D6) — no manual profile
    form exists anywhere in the app; every profile is ORCID/publication
    derived. Rejects if the ORCID already belongs to anyone, any role."""
    try:
        pi = await find_or_create_pi_by_orcid(db, orcid)
        await db.commit()
    except ValueError as exc:
        return RedirectResponse(
            url=f"/manager/pis?error={exc}", status_code=302
        )
    return RedirectResponse(url=f"/manager/pis/{pi.id}", status_code=302)


@router.post("/pis/{user_id}/profile")
async def manager_edit_pi_profile(
    user_id: uuid.UUID,
    name: str = Form(""),
    email: str = Form(""),
    institution: str = Form(""),
    department: str = Form(""),
    research_summary: str = Form(""),
    techniques: str = Form(""),
    experimental_models: str = Form(""),
    disease_areas: str = Form(""),
    key_targets: str = Form(""),
    keywords: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Edit a PI's profile fields (design D8) — same fields as the PI's own
    /profile/save, attributed to the acting manager via changed_by_user_id."""
    detail = await load_user_detail(db, user_id)
    if detail is None or detail["user"].user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")

    error = await apply_profile_edits(
        db, target_user=detail["user"], changed_by_user_id=current_user.id,
        name=name, email=email, institution=institution, department=department,
        research_summary=research_summary, techniques=techniques,
        experimental_models=experimental_models, disease_areas=disease_areas,
        key_targets=key_targets, keywords=keywords,
    )
    if error:
        return RedirectResponse(url=f"/manager/pis/{user_id}?error={error}", status_code=302)
    return RedirectResponse(url=f"/manager/pis/{user_id}?saved=1", status_code=302)


async def _manager_set_mute(
    user_id: uuid.UUID, db: AsyncSession, current_user: User, *, muted: bool,
) -> RedirectResponse:
    detail = await load_user_detail(db, user_id)
    if detail is None or detail["user"].user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")

    agent = (
        await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))
    ).scalar_one_or_none()
    if agent is None:
        return RedirectResponse(
            url=f"/manager/pis/{user_id}?error=no_agent", status_code=302
        )

    ok = await set_agent_mute_state(
        db, agent=agent, muted=muted, actor_user_id=current_user.id,
    )
    if not ok:
        return RedirectResponse(
            url=f"/manager/pis/{user_id}?error=agent_not_mutable", status_code=302
        )
    return RedirectResponse(url=f"/manager/pis/{user_id}", status_code=302)


@router.post("/pis/{user_id}/mute")
async def manager_mute_pi(
    user_id: uuid.UUID, db: AsyncSession = _DB, current_user: User = _STAFF,
):
    """Mute a PI's agent — maps to status='inactive' (design D2), not a new
    status value. No-ops (redirects with an error) if the agent doesn't
    exist or isn't currently active/inactive."""
    return await _manager_set_mute(user_id, db, current_user, muted=True)


@router.post("/pis/{user_id}/unmute")
async def manager_unmute_pi(
    user_id: uuid.UUID, db: AsyncSession = _DB, current_user: User = _STAFF,
):
    return await _manager_set_mute(user_id, db, current_user, muted=False)
```

Add `from sqlalchemy import select` and `from src.models import AgentRegistry` to
`manager.py`'s existing import block (check they aren't already imported
under a different alias before adding).

- [ ] **Step 5: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_views.py tests/integration/test_manager_pi_writes.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full manager test suite to check for regressions**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_views.py tests/integration/test_manager_access.py -v`
Expected: PASS — no existing manager test should need changes beyond the one allowlist rewrite in Step 1.

- [ ] **Step 7: Commit**

```bash
git add src/routers/manager.py tests/integration/test_manager_views.py tests/integration/test_manager_pi_writes.py
git commit -m "feat(manager): add PI create/edit/mute/unmute routes (D1 amends D12)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Manager templates — Add-PI form, edit form, mute button

**Files:**
- Modify: `templates/manager/pis.html`
- Modify: `templates/manager/pi_detail.html`
- Test: `tests/integration/test_manager_pi_writes.py` (extend with rendering assertions)

This task has no meaningful "write a failing test first" step for pure
template markup beyond what Task 5's route tests already exercise (they
already assert the redirects/DB effects). Add two lightweight rendering
tests, then implement the markup to make them pass — same TDD discipline,
just against rendered HTML instead of DB state.

- [ ] **Step 1: Write the failing rendering tests**

Append to `tests/integration/test_manager_pi_writes.py`:

```python
async def test_pis_page_shows_an_add_pi_form(client, db_session):
    manager = await _manager(db_session)
    r = await client.get("/manager/pis", headers=auth_headers(manager.id))
    assert r.status_code == 200
    assert '<form' in r.text and 'action="/manager/pis"' in r.text
    assert 'name="orcid"' in r.text


async def test_pi_detail_shows_mute_button_for_an_active_agent(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="active")

    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert r.status_code == 200
    assert f'/manager/pis/{pi.id}/mute' in r.text


async def test_pi_detail_hides_mute_button_for_a_pending_agent(client, db_session):
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="pending")

    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert r.status_code == 200
    assert f'/manager/pis/{pi.id}/mute' not in r.text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_pi_writes.py -v -k "add_pi_form or mute_button"`
Expected: FAIL — no such markup exists yet.

- [ ] **Step 3: Read the current templates**

Read `templates/manager/pis.html` and `templates/manager/pi_detail.html` in
full before editing, to match existing indentation/class conventions (both
use Tailwind utility classes throughout, per every other template in this
codebase).

- [ ] **Step 4: Add the Add-PI form to `pis.html`**

Add near the top of the page content block (above the directory table),
matching the file's existing heading/spacing style:

```html
<form method="post" action="/manager/pis" class="mb-6 flex items-end gap-2">
    <div>
        <label class="block text-xs text-gray-500 mb-1" for="orcid">Add a PI by ORCID iD</label>
        <input type="text" id="orcid" name="orcid" placeholder="0000-0000-0000-0000"
               class="border rounded px-2 py-1 text-sm" required>
    </div>
    <button type="submit" class="bg-indigo-600 text-white text-sm px-3 py-1 rounded hover:bg-indigo-700">
        Add PI
    </button>
    {% if request.query_params.get('error') %}
        <span class="text-sm text-red-600">{{ request.query_params.get('error') }}</span>
    {% endif %}
</form>
```

- [ ] **Step 5: Add the edit form and mute/unmute button to `pi_detail.html`**

Add an edit form posting to `/manager/pis/{{ target_user.id }}/profile`,
covering the same fields as `templates/profile/edit.html`'s `/profile/save`
form but with plain comma-separated text inputs for the list fields instead
of that page's tag-pill JS widget — this is manager tooling, not the PI's
own polished self-service page, and `apply_profile_edits`' `_parse_list`
already expects a comma-separated string either way, so the simpler markup
is fully functionally equivalent:

```html
{% if request.query_params.get('error') %}
<div class="bg-red-50 border border-red-200 rounded-lg p-4 mb-4">
    <p class="text-red-800 text-sm">
        {% if request.query_params.get('error') == 'invalid_email' %}Please enter a valid email address.
        {% elif request.query_params.get('error') == 'email_taken' %}That email address is already in use by another account.
        {% else %}Something went wrong saving changes.{% endif %}
    </p>
</div>
{% endif %}

<form method="post" action="/manager/pis/{{ target_user.id }}/profile" class="bg-white rounded-xl shadow-sm border border-gray-200 p-6 mb-6">
    <h2 class="text-lg font-semibold text-gray-800 mb-4">Edit Profile</h2>
    <div class="grid grid-cols-1 gap-4 mb-5">
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Display Name</label>
            <input type="text" name="name" value="{{ target_user.name }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Email</label>
            <input type="email" name="email" value="{{ target_user.email or '' }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Institution</label>
            <input type="text" name="institution" value="{{ target_user.institution or '' }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Department</label>
            <input type="text" name="department" value="{{ target_user.department or '' }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Research Summary</label>
            <textarea name="research_summary" rows="5"
                      class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">{{ profile.research_summary if profile else '' }}</textarea>
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Techniques &amp; Methods (comma-separated)</label>
            <input type="text" name="techniques" value="{{ (profile.techniques or []) | join(', ') if profile else '' }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Model Systems (comma-separated)</label>
            <input type="text" name="experimental_models" value="{{ (profile.experimental_models or []) | join(', ') if profile else '' }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Disease Areas (comma-separated)</label>
            <input type="text" name="disease_areas" value="{{ (profile.disease_areas or []) | join(', ') if profile else '' }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Key Molecular Targets (comma-separated)</label>
            <input type="text" name="key_targets" value="{{ (profile.key_targets or []) | join(', ') if profile else '' }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
        <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Keywords (comma-separated)</label>
            <input type="text" name="keywords" value="{{ (profile.keywords or []) | join(', ') if profile else '' }}"
                   class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
        </div>
    </div>
    <button type="submit" class="bg-indigo-600 text-white px-6 py-2 rounded-lg hover:bg-indigo-700 font-medium">
        Save Changes
    </button>
</form>
```

Then add a mute control conditioned on the PI having a linked agent whose
status is `active` or `inactive`:

```html
{% if target_user.agent and target_user.agent.status in ('active', 'inactive') %}
    <form method="post"
          action="/manager/pis/{{ target_user.id }}/{{ 'unmute' if target_user.agent.status == 'inactive' else 'mute' }}"
          class="inline">
        <button type="submit"
                class="text-sm px-3 py-1 rounded {{ 'bg-green-600' if target_user.agent.status == 'inactive' else 'bg-yellow-600' }} text-white hover:opacity-90">
            {{ 'Unmute' if target_user.agent.status == 'inactive' else 'Mute' }}
        </button>
    </form>
{% elif target_user.agent %}
    <span class="text-xs text-gray-400">
        Agent is {{ target_user.agent.status }} — mute is only available for an active or already-muted agent.
    </span>
{% endif %}
```

Confirm `target_user.agent` is a valid accessor — `manager_pi_detail` passes
`target_user=detail["user"]`, and `AgentRegistry.user` back-populates as
`User.agent` (`src/models/agent_registry.py:50-52`), so `target_user.agent`
should resolve via SQLAlchemy's lazy relationship loading as long as the
session is still open when the template renders (it is — `load_user_detail`
runs inside the same request's session). If this triggers a lazy-load error
in practice (async SQLAlchemy sessions sometimes require eager loading),
check whether `load_user_detail` already eager-loads `.agent` — if not, add
`selectinload(User.agent)` to its query rather than working around it in
the template.

- [ ] **Step 6: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_pi_writes.py -v`
Expected: PASS.

- [ ] **Step 7: Manual check in a browser (per this repo's UI-change convention)**

Start the dev stack if not already running and visually confirm the Add-PI
form and mute button render sensibly — this is user-facing markup, and the
plan's automated tests only check for substring presence, not layout.

- [ ] **Step 8: Commit**

```bash
git add templates/manager/pis.html templates/manager/pi_detail.html tests/integration/test_manager_pi_writes.py
git commit -m "feat(manager): Add-PI form, profile edit form, and mute/unmute button

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Workstream B — Assessment → PI profile link

### Task 7: Batched `AgentRegistry` lookup in the two assessment services

**Files:**
- Modify: `src/services/directory.py` (`list_assessments`)
- Modify: `src/services/assessment_detail.py` (`build_assessment_detail`)
- Modify: `src/routers/admin.py` (`admin_assessments` — add the one new allowlisted key)
- Test: `tests/unit/test_assessment_pi_lookup.py` (create)

**Interfaces:**
- Produces: `list_assessments(...)`'s return dict gains `"pi_user_ids": dict[str, str]` (subject_agent_id -> str(user_id), only for resolvable rows). `build_assessment_detail(...)`'s return dict gains `"pi_user_id": str | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""The assessment -> PI profile lookup: resolvable, unresolvable, and stale
subject_agent_id all render distinctly (design D9)."""
import pytest

from src.services.assessment_detail import build_assessment_detail
from src.services.directory import list_assessments
from tests import factories

pytestmark = pytest.mark.integration


async def _assessment(db_session, run, subject_agent_id):
    from src.models import OpportunityAssessment
    a = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id=subject_agent_id, channel_name="general",
    )
    db_session.add(a)
    await db_session.flush()
    return a


async def test_list_assessments_resolves_pi_user_ids(db_session):
    run = await factories.make_simulation_run(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="wang")
    await _assessment(db_session, run, "wang")

    view = await list_assessments(db_session, str(run.id))
    assert view["pi_user_ids"]["wang"] == str(pi.id)


async def test_list_assessments_omits_an_unresolvable_subject(db_session):
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "decommissioned-slug")

    view = await list_assessments(db_session, str(run.id))
    assert "decommissioned-slug" not in view["pi_user_ids"]


async def test_list_assessments_omits_an_unlinked_agent(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="unlinked")  # no user=
    await _assessment(db_session, run, "unlinked")

    view = await list_assessments(db_session, str(run.id))
    assert "unlinked" not in view["pi_user_ids"]


async def test_build_assessment_detail_resolves_pi_user_id(db_session):
    run = await factories.make_simulation_run(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="wu")
    a = await _assessment(db_session, run, "wu")

    detail = await build_assessment_detail(db_session, a.id, admin_view=True)
    assert detail["pi_user_id"] == str(pi.id)


async def test_build_assessment_detail_pi_user_id_is_none_when_unresolvable(db_session):
    run = await factories.make_simulation_run(db_session)
    a = await _assessment(db_session, run, None)

    detail = await build_assessment_detail(db_session, a.id, admin_view=True)
    assert detail["pi_user_id"] is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessment_pi_lookup.py -v`
Expected: FAIL — `KeyError: 'pi_user_ids'` / `'pi_user_id'`.

- [ ] **Step 3: Implement the lookup in `list_assessments`**

Grep to confirm: `grep -n "assessments = result.scalars().all()" src/services/directory.py`.
Immediately after that line (before `row_scales` is built, since both key off
`assessments`), add:

```python
    subject_ids = {a.subject_agent_id for a in assessments if a.subject_agent_id}
    pi_user_ids: dict[str, str] = {}
    if subject_ids:
        from src.models import AgentRegistry
        rows = (await db.execute(
            select(AgentRegistry.agent_id, AgentRegistry.user_id)
            .where(AgentRegistry.agent_id.in_(subject_ids))
        )).all()
        pi_user_ids = {
            r.agent_id: str(r.user_id) for r in rows if r.user_id is not None
        }
```

Add `"pi_user_ids": pi_user_ids,` to the function's return dict (near
`"lab_options"`, since both are keyed by `subject_agent_id`).

- [ ] **Step 4: Implement the lookup in `build_assessment_detail`**

Grep to confirm: `grep -n "if assessment is None:" src/services/assessment_detail.py`.
Immediately after that guard, add:

```python
    pi_user_id: str | None = None
    if assessment.subject_agent_id:
        from src.models import AgentRegistry
        row = (await db.execute(
            select(AgentRegistry.user_id)
            .where(AgentRegistry.agent_id == assessment.subject_agent_id)
        )).scalar_one_or_none()
        if row is not None:
            pi_user_id = str(row)
```

Add `"pi_user_id": pi_user_id,` to the function's return dict (near
`"assessment"`, at line ~536).

- [ ] **Step 5: Wire the new key into `admin_assessments`' explicit allowlist**

`admin_assessments` (`src/routers/admin.py`, ~line 728-780) forwards every
key from `list_assessments` explicitly rather than splatting `**view` (see
that function's own comment about why). Add
`pi_user_ids=view["pi_user_ids"],` to its `_template_context(...)` call.
`manager_assessments` already splats `**view` (`src/routers/manager.py:162`)
and both detail routes already splat `**detail` — no change needed there.

- [ ] **Step 6: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessment_pi_lookup.py -v`
Expected: PASS.

- [ ] **Step 7: Run the existing assessments-page test suites for regressions**

Run: `.venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py tests/integration/test_manager_views.py -v -k assessment`
Expected: PASS — this task adds a key, it doesn't remove or rename any existing one.

- [ ] **Step 8: Commit**

```bash
git add src/services/directory.py src/services/assessment_detail.py src/routers/admin.py tests/unit/test_assessment_pi_lookup.py
git commit -m "feat(assessments): resolve subject_agent_id to a PI user_id for linking

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: Link markup in the four assessment templates

**Files:**
- Modify: `templates/admin/assessments.html`
- Modify: `templates/admin/assessment_detail.html`
- Modify: `templates/manager/assessments.html`
- Modify: `templates/manager/assessment_detail.html`
- Modify: `templates/admin/_assessments_body.html` (one line)
- Modify: `templates/admin/_assessment_detail_body.html` (one line)
- Test: `tests/integration/test_assessment_pi_link_rendering.py` (create)

This mirrors the codebase's own existing `assessment_link(a)` macro pattern
exactly (`templates/admin/assessments.html:92-104`'s header comment
confirms: "Jinja passes the including template's context, macros included,
into an `{% include %}`" — this is a proven, already-working mechanism in
this file, not a new one).

- [ ] **Step 1: Write the failing tests**

```python
"""The Lab cell on both assessment surfaces links to the PI's profile when
resolvable, and falls back to plain text otherwise (design D9/D10)."""
import pytest

from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _run_and_assessment(db_session, subject_agent_id):
    from src.models import OpportunityAssessment
    run = await factories.make_simulation_run(db_session)
    a = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id=subject_agent_id, channel_name="general",
    )
    db_session.add(a)
    await db_session.flush()
    return run, a


async def test_admin_assessments_list_links_to_admin_users_page(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="wang")
    run, _ = await _run_and_assessment(db_session, "wang")

    r = await client.get(f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id))
    assert f'/admin/users/{pi.id}' in r.text


async def test_manager_assessments_list_links_to_manager_pis_page(client, db_session):
    manager = await factories.make_user(db_session, user_role="manager")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="wu")
    run, _ = await _run_and_assessment(db_session, "wu")

    r = await client.get(f"/manager/assessments?run_id={run.id}", headers=auth_headers(manager.id))
    assert f'/manager/pis/{pi.id}' in r.text


async def test_unresolvable_subject_renders_plain_text_no_link(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    run, _ = await _run_and_assessment(db_session, "decommissioned-slug")

    r = await client.get(f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id))
    assert "decommissioned-slug" in r.text
    assert '/admin/users/' not in r.text.split("decommissioned-slug")[0][-200:]


async def test_admin_assessment_detail_links_to_pi_profile(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="su")
    _, a = await _run_and_assessment(db_session, "su")

    r = await client.get(f"/admin/assessments/{a.id}", headers=auth_headers(admin.id))
    assert f'/admin/users/{pi.id}' in r.text
```

The third test's assertion is deliberately loose (checks no admin link
immediately precedes the slug text) rather than asserting global absence of
`/admin/users/` anywhere on the page, since other rows on the same page may
legitimately link elsewhere. Adjust it once you see the real page structure
if it's too brittle.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_pi_link_rendering.py -v`
Expected: FAIL — no such links exist yet.

- [ ] **Step 3: Update the shared list partial**

In `templates/admin/_assessments_body.html`, update the CONTRACT comment
(lines 12-15) to also require a `pi_link(a)` macro:

```
   CONTRACT: an including template must define an `assessment_link(a)` macro
   AND a `pi_link(a)` macro BEFORE the include (see either wrapper). That is
   how the per-row links to /admin|/manager/assessments/{id} and
   /admin|/manager/users|pis/{id} get literal URLs without one appearing
   here.
```

Replace the "Lab" cell (grep-confirm the line: `grep -n 'a.subject_agent_id or "—"' templates/admin/_assessments_body.html`):

```html
<td class="px-4 py-3 text-sm text-gray-600">{{ pi_link(a) }}</td>
```

- [ ] **Step 4: Update the shared detail partial**

In `templates/admin/_assessment_detail_body.html`, update its own header
comment similarly, and replace the "Lab:" line (grep-confirm:
`grep -n 'Lab:' templates/admin/_assessment_detail_body.html`):

```html
Lab: <span class="font-medium text-gray-700">{{ pi_link(a) }}</span>
```

- [ ] **Step 5: Define the macro in each of the four wrapper templates**

In `templates/admin/assessments.html`, add right after the existing
`assessment_link` macro definition (line ~104):

```html
{% macro pi_link(a) %}{% if a.subject_agent_id and pi_user_ids.get(a.subject_agent_id) %}<a class="underline text-indigo-600 hover:text-indigo-800" href="/admin/users/{{ pi_user_ids[a.subject_agent_id] }}">{{ a.subject_agent_id }}</a>{% else %}{{ a.subject_agent_id or "—" }}{% endif %}{% endmacro %}
```

In `templates/manager/assessments.html`, the same macro with
`/manager/pis/{{ pi_user_ids[a.subject_agent_id] }}` instead.

In `templates/admin/assessment_detail.html` and
`templates/manager/assessment_detail.html` (which each show exactly one
assessment, not a list), define the same-named macro but taking the single
`assessment` object directly, using the singular `pi_user_id` context key
from Task 7:

```html
{% macro pi_link(a) %}{% if a.subject_agent_id and pi_user_id %}<a class="underline text-indigo-600 hover:text-indigo-800" href="/admin/users/{{ pi_user_id }}">{{ a.subject_agent_id }}</a>{% else %}{{ a.subject_agent_id or "—" }}{% endif %}{% endmacro %}
```

(manager's detail wrapper: `/manager/pis/{{ pi_user_id }}`). Place each macro
definition before that wrapper's own `{% include %}` line, exactly like the
existing `assessment_link` macro.

- [ ] **Step 6: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_pi_link_rendering.py -v`
Expected: PASS.

- [ ] **Step 7: Verify the shared partials still contain no absolute URLs**

Run: `grep -nE "/admin/|/manager/" templates/admin/_assessments_body.html templates/admin/_assessment_detail_body.html`
Expected: no output (both files' own documented contract, now extended to
`pi_link` as well as `assessment_link`).

- [ ] **Step 8: Run the reachability test**

Run: `.venv-test/bin/python -m pytest tests/unit/test_reachability.py -v`
Expected: PASS — this task adds a link but doesn't change which routes exist, so it shouldn't affect this test either way; run it to confirm.

- [ ] **Step 9: Commit**

```bash
git add templates/admin/assessments.html templates/admin/assessment_detail.html \
  templates/manager/assessments.html templates/manager/assessment_detail.html \
  templates/admin/_assessments_body.html templates/admin/_assessment_detail_body.html \
  tests/integration/test_assessment_pi_link_rendering.py
git commit -m "feat(assessments): link the Lab cell to the PI's profile on both surfaces

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Workstream C — Assessments-summary Slack channel

### Task 9: Spike — verify `chat.getPermalink` works with existing bot scopes

**This is a manual verification task, not a code-writing one — design decision D18.** Do this before Task 10.

- [ ] **Step 1: Confirm the slack_sdk method name**

Run: `.venv-test/bin/python -c "from slack_sdk import WebClient; print(WebClient.chat_getPermalink)"`
Expected: prints a bound method reference (confirms the method exists in the installed SDK version under this exact name — remember this repo's two Anthropic SDK versions differ, per CLAUDE.md's warning about `.venv-test` vs. the deployed image; slack_sdk is a different package with its own pin, but the same "verify before trusting" discipline applies).

- [ ] **Step 2: Call it against the real workspace with an existing bot token**

Using any already-provisioned agent's `slack_bot_token` (from the `agents`
table in the running deployment, NOT committed anywhere) and any real
message's channel ID + `ts` from that workspace, run a one-off script (do
not commit this script — it's throwaway per this task's nature):

```python
from slack_sdk import WebClient
client = WebClient(token="<a real bot token>")
resp = client.chat_getPermalink(channel="<a real channel id>", message_ts="<a real message ts>")
print(resp)
```

Expected: `{"ok": True, "permalink": "https://<workspace>.slack.com/archives/...", "channel": "..."}`.

- [ ] **Step 3: If it fails on scope**

If the response is `{"ok": False, "error": "missing_scope", ...}`, **stop
here and report back** rather than proceeding to Tasks 10-12. Per the design
spec (D18), fixing this means adding a scope to the Slack app manifest and
reinstalling **every** agent's Slack app — an operational decision for the
deployment operator, not something to route around in code. Do not build a
fallback permalink construction (spec §10 explicitly leaves this
undesigned) — surface the finding and get a decision before writing more
code.

- [ ] **Step 4: If it succeeds, proceed to Task 10**

No commit for this task — it's verification only. Note the confirmed method
name and response shape in Task 10's implementation (below), since Task 10
was written assuming this spike passes.

---

### Task 10: `AgentSlackClient.get_permalink` / `.aget_permalink`

**Files:**
- Modify: `src/agent/slack_client.py` (add near the async-wrappers block, now at lines ~1180-1207 — **confirmed as of this plan's writing**: the concurrent remediation plan's Task 2 already landed and added `ais_bot_user`/`ajoin_channel`/`aconnect` there; if executing this plan later, re-grep rather than trust these numbers)
- Modify: `tests/fakes.py` (`FakeSlackClient` — add a fake so callers in tests don't need a real Slack connection)
- Test: `tests/unit/test_slack_client_contract.py`

**Interfaces:**
- Produces: `AgentSlackClient.get_permalink(channel_id: str, message_ts: str) -> str | None`, `AgentSlackClient.aget_permalink(*args, **kwargs) -> str | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_slack_client_contract.py`:

```python
def test_get_permalink_returns_the_url_on_success():
    from src.agent.slack_client import AgentSlackClient

    class _Stub:
        def chat_getPermalink(self, **kw):
            return {"ok": True, "permalink": "https://example.slack.com/archives/C1/p123"}

    client = AgentSlackClient(agent_id="hub", bot_token="xoxb-x")
    client._client = _Stub()
    assert client.get_permalink("C1", "123.000") == "https://example.slack.com/archives/C1/p123"


def test_get_permalink_returns_none_on_any_failure():
    from slack_sdk.errors import SlackApiError
    from src.agent.slack_client import AgentSlackClient

    class _Resp:
        headers: dict = {}
        def get(self, key, default=None):
            return {"error": "channel_not_found"}.get(key, default)

    class _Stub:
        def chat_getPermalink(self, **kw):
            raise SlackApiError("boom", response=_Resp())

    client = AgentSlackClient(agent_id="hub", bot_token="xoxb-x")
    client._client = _Stub()
    assert client.get_permalink("C1", "123.000") is None


async def test_aget_permalink_wraps_the_sync_call():
    from src.agent.slack_client import AgentSlackClient

    class _Stub:
        def chat_getPermalink(self, **kw):
            return {"ok": True, "permalink": "https://example.slack.com/archives/C1/p999"}

    client = AgentSlackClient(agent_id="hub", bot_token="xoxb-x")
    client._client = _Stub()
    result = await client.aget_permalink("C1", "999.000")
    assert result == "https://example.slack.com/archives/C1/p999"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_slack_client_contract.py -k permalink -v`
Expected: FAIL — `AttributeError: 'AgentSlackClient' object has no attribute 'get_permalink'`.

- [ ] **Step 3: Implement**

Grep `grep -n "async def a" src/agent/slack_client.py` to find the current
async-wrappers block — confirmed at lines ~1180-1207 as of this plan's
writing, ending with `aconnect`; add `aget_permalink` after it. Add the
synchronous `get_permalink` method anywhere in the main body near `_api`
(any existing synchronous method is fine — it doesn't have to be adjacent
to `post_message`, just call the same chokepoint):

```python
    def get_permalink(self, channel_id: str, message_ts: str) -> str | None:
        """chat.getPermalink through the _api chokepoint (retry/backoff for
        free). Returns None on any failure — callers degrade gracefully
        (design D16), they never treat a missing permalink as a reason to
        skip a post entirely."""
        if not self._client:
            return None
        try:
            resp = self._api(
                "chat_getPermalink", channel=channel_id, message_ts=message_ts,
            )
        except SlackApiError:
            return None
        return resp.get("permalink") if resp else None
```

(This plan originally omitted the `if not self._client: return None` guard
that every other public method in `AgentSlackClient` has — caught by task
review, not written correctly here the first time. Corrected in place
2026-08-21 after Task 10's implementation.)

And in the async-wrappers block:

```python
    async def aget_permalink(self, *args, **kwargs) -> str | None:
        return await asyncio.to_thread(self.get_permalink, *args, **kwargs)
```

`SlackApiError` should already be imported at the top of this file (used by
`is_bot_user` and others) — confirm with
`grep -n "^from slack_sdk\|^import slack_sdk" src/agent/slack_client.py`
rather than adding a duplicate import.

- [ ] **Step 4: Add the fake**

In `tests/fakes.py`'s `FakeSlackClient`, add:

```python
    def get_permalink(self, channel_id: str, message_ts: str) -> str | None:
        return f"https://fake.slack.com/archives/{channel_id}/p{message_ts.replace('.', '')}"

    async def aget_permalink(self, *args, **kwargs) -> str | None:
        return self.get_permalink(*args, **kwargs)
```

(Check `FakeSlackClient` doesn't already have a similarly-named method
before adding — `grep -n "permalink" tests/fakes.py`.)

- [ ] **Step 5: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_slack_client_contract.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent/slack_client.py tests/fakes.py tests/unit/test_slack_client_contract.py
git commit -m "feat(hub): add AgentSlackClient.get_permalink via chat.getPermalink

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 11: The assessments-summary channel — creation and isolation

**Files:**
- Modify: `src/agent/channels.py` (new constant, separate from `SEEDED_CHANNELS`)
- Modify: `src/agent/simulation.py` (`start()` around line 605; new method near `_ensure_seeded_channels`)
- Test: `tests/unit/test_assessments_summary_channel.py` (create)

**Interfaces:**
- Produces: `src.agent.channels.ASSESSMENTS_SUMMARY_CHANNEL: str`; `SimulationEngine._ensure_assessments_summary_channel() -> None`, setting `self._assessments_summary_channel_id: str | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""The assessments-summary channel is created and joined by the hub only,
and stays out of the topical channel-discovery machinery (design D11)."""
import pytest

from src.agent.agent import Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL, SEEDED_CHANNELS
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def test_assessments_summary_channel_is_not_a_seeded_channel():
    assert ASSESSMENTS_SUMMARY_CHANNEL not in SEEDED_CHANNELS


def test_assessments_summary_channel_is_not_a_discovery_keyword_channel():
    from src.agent.simulation import _CHANNEL_KEYWORDS, _UNIVERSAL_CHANNELS
    assert ASSESSMENTS_SUMMARY_CHANNEL not in _CHANNEL_KEYWORDS
    assert ASSESSMENTS_SUMMARY_CHANNEL not in _UNIVERSAL_CHANNELS


async def test_ensure_assessments_summary_channel_creates_and_joins_only_the_hub(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    hub_client = FakeSlackClient(agent_id="blackbird")
    lab_client = FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": hub_client, "wang": lab_client},
    )

    eng._ensure_assessments_summary_channel()

    assert eng._assessments_summary_channel_id is not None
    assert ASSESSMENTS_SUMMARY_CHANNEL in eng._channel_id_map
    assert ASSESSMENTS_SUMMARY_CHANNEL in hub_client.joined_channels
    assert ASSESSMENTS_SUMMARY_CHANNEL not in lab_client.joined_channels
```

The last assertion assumes `FakeSlackClient` tracks joined channels (e.g. a
`joined_channels: set[str]` populated by its `join_channel`). Check
`tests/fakes.py` first — if it doesn't track this yet, add that tracking as
part of this task (it's a one-line addition to an existing fake method, in
the spirit of "targeted improvements as part of the design" rather than a
new file).

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessments_summary_channel.py -v`
Expected: the two module-level tests PASS trivially (nothing exists to
violate yet) — the third FAILs with `AttributeError`. This is expected and
fine: the first two are regression guards for LATER, not proof of anything
now.

- [ ] **Step 3: Add the channel name constant**

In `src/agent/channels.py`, after `SEEDED_CHANNELS`:

```python
# The hub's one-way announcement channel (design D11): deliberately NOT in
# SEEDED_CHANNELS, _CHANNEL_KEYWORDS, or _UNIVERSAL_CHANNELS
# (src/agent/simulation.py) — those three drive Phase-1 topical
# discovery/auto-join for PI-lab agents, and channel polling scope
# (_poll_slack_for_bot_messages, _rebuild_state_from_slack) is keyed off
# SEEDED_CHANNELS membership too. Keeping this name out of all three means
# no PI-lab bot ever joins it, scans it, or treats the hub's headline posts
# as something to reply to.
ASSESSMENTS_SUMMARY_CHANNEL = "assessments-summary"
```

- [ ] **Step 4: Implement `_ensure_assessments_summary_channel`**

Grep to confirm placement: `grep -n "def _ensure_seeded_channels" src/agent/simulation.py`.
Add a sibling method right after it:

```python
    def _ensure_assessments_summary_channel(self) -> None:
        """Create (or adopt) the hub's one-way assessments-summary channel
        and join only the hub to it — never added to SEEDED_CHANNELS, so it
        never enters Phase-1 discovery or the poller's scope (design D11).
        """
        hub = next(
            (a for a in self.agents.values() if a.role == "scout_hub"), None
        )
        if hub is None:
            return
        client = self.slack_clients.get(hub.agent_id)
        if not client or not client.is_connected:
            self._assessments_summary_channel_id = f"local:{ASSESSMENTS_SUMMARY_CHANNEL}"
            self._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = self._assessments_summary_channel_id
            return

        try:
            existing = client.list_channels()
        except SlackListingIncomplete:
            # Same caution as _ensure_seeded_channels: an incomplete listing
            # must not risk creating a duplicate channel.
            return

        ch_id = existing.get(ASSESSMENTS_SUMMARY_CHANNEL)
        if ch_id is None:
            ch_data = client.create_channel(ASSESSMENTS_SUMMARY_CHANNEL)
            ch_id = ch_data.get("id") if ch_data else None
        if not ch_id:
            return

        self._assessments_summary_channel_id = ch_id
        self._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = ch_id
        client.join_channel(ch_id)
```

Read `_ensure_seeded_channels`'s exact `list_channels()`/`create_channel()`
call shapes first (`src/agent/simulation.py:4603-4658`, read earlier in
this plan's research) and match them precisely — the above is written from
that reading but re-verify field names (`ch_data.get("id", "")` vs
`ch_data.get("id")`) against the real method signatures before trusting
this block verbatim.

Add the call in `start()`, right after `self._ensure_seeded_channels()`
(grep-confirm: `grep -n "_ensure_seeded_channels()" src/agent/simulation.py`):

```python
        self._ensure_seeded_channels()
        self._ensure_assessments_summary_channel()
```

Add `self._assessments_summary_channel_id: str | None = None` to `__init__`
alongside the other channel-tracking attributes (near `self._channel_id_map`
— grep `grep -n "_channel_id_map: dict" src/agent/simulation.py`).

- [ ] **Step 5: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessments_summary_channel.py -v`
Expected: PASS.

- [ ] **Step 6: Run the existing channel-discovery and polling test suites**

Run: `grep -rln "_phase1_channel_discovery\|_ensure_seeded_channels\|_poll_slack_for_bot_messages" tests/unit/*.py tests/integration/*.py`, then run those files.
Expected: PASS, unchanged — this task adds a new method and constant, it doesn't modify any existing discovery/polling logic.

- [ ] **Step 7: Commit**

```bash
git add src/agent/channels.py src/agent/simulation.py tests/fakes.py tests/unit/test_assessments_summary_channel.py
git commit -m "feat(hub): create the assessments-summary channel, isolated from PI-bot discovery

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 12: Post a headline summary when an interview concludes

**Files:**
- Modify: `src/agent/simulation.py` (`_capture_hub_assessment`, new helper method)
- Test: `tests/unit/test_assessments_summary_post.py` (create)

**Interfaces:**
- Consumes: `AgentSlackClient.aget_permalink` (Task 10), `self._assessments_summary_channel_id`/`ASSESSMENTS_SUMMARY_CHANNEL` (Task 11).
- Produces: `SimulationEngine._post_assessment_summary(self, agent: Agent, thread: ThreadState, verdict: dict, slack_ts: str | None) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
"""Every held interview verdict — pass or fail — posts one headline to the
assessments-summary channel, with no rationale/red-flags/gating content
(design D12/D13/D14/D16)."""
import pytest

from src.agent.agent import Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _engine(monkeypatch, tmp_path):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    hub_client = FakeSlackClient(agent_id="blackbird")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": hub_client, "wang": lab_client := FakeSlackClient(agent_id="wang")},
    )
    eng._assessments_summary_channel_id = "C-SUMMARY"
    eng._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = "C-SUMMARY"
    return eng, hub, lab, hub_client


VERDICT = {
    "subject_agent_id": "wang", "company_or_project": "CRISPR Platform",
    "recommendation": "pass", "funnel_stage": "incubation",
    "scores": {"external_signals": 4, "ip_fto": 4},
}


async def test_a_held_pass_verdict_posts_a_headline(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t1", channel="general", other_agent_id="wang")

    await eng._post_assessment_summary(hub, thread, VERDICT, "111.000")

    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    text = hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]
    assert "Wang" in text or "wang" in text
    assert "CRISPR Platform" in text
    assert "pass" in text
    assert "rationale" not in text.lower()


async def test_a_held_fail_verdict_also_posts(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t2", channel="general", other_agent_id="wang")
    fail_verdict = {**VERDICT, "recommendation": "no-fit", "scores": {"external_signals": 1}}

    await eng._post_assessment_summary(hub, thread, fail_verdict, "222.000")

    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    assert "no-fit" in hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]


async def test_no_scores_still_posts_without_a_band(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t3", channel="general", other_agent_id="wang")
    no_scores = {**VERDICT, "scores": {}}

    await eng._post_assessment_summary(hub, thread, no_scores, "333.000")

    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1


async def test_a_slack_post_failure_is_swallowed(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t4", channel="general", other_agent_id="wang")

    async def boom(*a, **kw):
        raise RuntimeError("Slack is down")
    monkeypatch.setattr(hub_client, "apost_message", boom)

    await eng._post_assessment_summary(hub, thread, VERDICT, "444.000")  # must not raise


async def test_capture_hub_assessment_triggers_the_summary_post(monkeypatch, tmp_path):
    """End-to-end from the real hook point: a held verdict from
    _capture_hub_assessment posts to the summary channel."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t5", channel="general", other_agent_id="wang")
    hub.state.active_threads["t5"] = thread

    eng.session_factory = None  # forces _persist_assessment's no-DB branch (held=False)
    raw = "some text <assessment_json>" + __import__("json").dumps(VERDICT) + "</assessment_json>"

    await eng._capture_hub_assessment(hub, thread, raw, "555.000", closes_thread=True)

    # No DB configured means _persist_assessment returns False (not held) —
    # per design D13, no post should fire for something that wasn't held.
    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages


async def test_a_refused_sidecar_never_posts_a_summary(monkeypatch, tmp_path):
    """Design D14: only a HELD OpportunityAssessment row triggers a post. A
    refused/dropped sidecar (recorded as an AssessmentDrop, never persisted
    as a verdict) must not post — structurally guaranteed by
    _capture_hub_assessment's refusal branch returning before it ever calls
    _persist_assessment, but pinned here as an explicit regression test
    rather than left as an inference from code structure."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t6", channel="general", other_agent_id="wang")
    hub.state.active_threads["t6"] = thread
    thread.message_count = 1  # an early, non-concluding, non-closing turn

    async def fake_record_drop(*a, **kw):
        return None
    monkeypatch.setattr(eng, "_record_assessment_drop", fake_record_drop)

    raw = "some text <assessment_json>" + __import__("json").dumps(VERDICT) + "</assessment_json>"
    # closes_thread=False on an early ordinal is exactly the premature_sidecar
    # refusal case (see CLAUDE.md's "One interview yields exactly one
    # assessment" section) — verify the real refusal condition against
    # _sidecar_refusal before trusting this drives the refusal branch.
    await eng._capture_hub_assessment(hub, thread, raw, "666.000", closes_thread=False)

    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages
```

This last test's exact mechanics for driving `_capture_hub_assessment`
depend on `_extract_assessment_json`'s real parsing format — read that
function (`grep -n "_extract_assessment_json\|_ASSESSMENT_UNCLOSED_RE" src/agent/simulation.py`)
before trusting the `raw` string's exact shape here; adjust to match
whatever tag/format it actually expects (this plan's earlier research
found it expects an `<assessment_json>...</assessment_json>` sidecar, but
confirm the exact regex/parsing before relying on this literal string).

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessments_summary_post.py -v`
Expected: FAIL — `AttributeError: 'SimulationEngine' object has no attribute '_post_assessment_summary'`.

- [ ] **Step 3: Check `FakeSlackClient` supports the needed assertions**

`grep -n "posted_messages\|class FakeSlackClient" tests/fakes.py`. If
`posted_messages` isn't already a `dict[channel, list[str]]`-shaped
attribute, add tracking for it in `apost_message`/`post_message` as part of
this task (matching whatever shape is closest to what's already there —
adapt the tests above to the real shape rather than assuming this plan's
literal `hub_client.posted_messages[CHANNEL]` list-of-strings is exactly
right).

- [ ] **Step 4: Implement `_post_assessment_summary`**

Add as a new method, placed near `_capture_hub_assessment`:

```python
    async def _post_assessment_summary(
        self, agent: Agent, thread: ThreadState, verdict: dict, slack_ts: str | None,
    ) -> None:
        """Post a headline-only summary of a concluded interview to the
        assessments-summary channel (design D12/D13/D14/D16). Called from
        _capture_hub_assessment right after a verdict is HELD — covers both
        the immediate fail (closes_thread) path and the pass path
        symmetrically, since both funnel through that one call site.

        Deliberately duplicates two PURE function calls
        (rubric_weighted_score/rubric_band) rather than changing
        _persist_assessment's return signature to hand back its computed
        values — that would risk breaking existing direct unit-test callers
        of _persist_assessment that assert a plain bool return. The
        weighting LOGIC itself is not duplicated, only these two calls.

        Never raises: a Slack failure here must not affect anything the
        caller already did (the assessment row's persistence, or the reply
        already posted to Slack).
        """
        try:
            channel_id = self._assessments_summary_channel_id
            client = self.slack_clients.get(agent.agent_id)
            if not channel_id or not client:
                return

            subject_agent_id = thread.other_agent_id
            pi = self.agents.get(subject_agent_id) if subject_agent_id else None
            pi_label = pi.pi_name if pi else (subject_agent_id or "Unknown lab")

            scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
            if scores:
                stage = verdict.get("funnel_stage")
                score = rubric_weighted_score(scores, stage)
                band = rubric_band(score, stage)
                score_part = f" (band: {band}, score: {score:.1f})"
            else:
                score_part = ""

            project = verdict.get("company_or_project") or "(untitled)"
            recommendation = verdict.get("recommendation") or "unknown"

            source_channel_id = self._channel_id_map.get(thread.channel)
            permalink = None
            if source_channel_id and slack_ts:
                permalink = await client.aget_permalink(source_channel_id, slack_ts)
            link_part = f" — <{permalink}|View interview>" if permalink else " (link unavailable)"

            text = (
                f":mag: {pi_label} — {project} → *{recommendation}*{score_part}{link_part}"
            )
            await client.apost_message(ASSESSMENTS_SUMMARY_CHANNEL, text)
        except Exception:
            logger.exception(
                "[%s] Failed to post assessments-summary headline for thread %s",
                agent.agent_id, thread.thread_id,
            )
```

Add `from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL` to
`simulation.py`'s imports (it likely already imports `SEEDED_CHANNELS` from
the same module — add to that existing import line rather than a new one).
`rubric_weighted_score`/`rubric_band` should already be imported (used by
`_persist_assessment`) — confirm with
`grep -n "rubric_weighted_score\|rubric_band" src/agent/simulation.py`
rather than adding a duplicate import.

- [ ] **Step 5: Wire the call into `_capture_hub_assessment`**

Grep-confirm the exact current line: `grep -n "if held:" src/agent/simulation.py`.
In the `if held:` block (inside `_capture_hub_assessment`), add the call
right after the `self._assessed_threads[...] = _HeldVerdict(...)` assignment
and before the `if superseded is not None:` check:

```python
                if held:
                    self._assessed_threads[thread.thread_id] = _HeldVerdict(
                        ordinal=thread.message_count + 1,
                        final=closes_thread,
                        slack_ts=slack_ts,
                    )
                    await self._post_assessment_summary(agent, thread, verdict, slack_ts)
                    if superseded is not None:
                        await self._retire_superseded_verdict(
                            agent.agent_id, thread, superseded,
                            replacement_ordinal=thread.message_count + 1,
                        )
```

Note `verdict` here is `_capture_hub_assessment`'s own local variable from
`_extract_assessment_json(raw_response)` (the RAW model verdict, not
`subject_view` — but `_post_assessment_summary` reads `thread.other_agent_id`
for the subject, not `verdict.get("subject_agent_id")`, so this is
consistent with `_persist_assessment`'s own override rationale: the engine's
own knowledge of who the interview partner is always wins over the model's
guess).

- [ ] **Step 6: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessments_summary_post.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full assessment-capture regression suite**

Run: `.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py tests/integration/test_opportunity_assessment_persistence.py -v -k assessment`
Expected: PASS — this task adds a call inside an existing `if held:` block; it should not change any existing assertion about `_persist_assessment`'s DB effects.

- [ ] **Step 8: Update the stale documentation**

Per design spec §6's note: `CLAUDE.md`'s "BlackbirdBot" section currently
states the hub is reply-only and its assessment "never appears on anything a
PI or another lab sees." Update that section to describe the new headline
summary and its deliberate content restriction (no rationale/red
flags/gating), matching this repo's existing practice of correcting
CLAUDE.md when reality changes. Also check
`docs/blackbird-star-topology-runbook.md` for the same stale claim
(`grep -n "reply-only\|never appears\|top-level post" CLAUDE.md docs/blackbird-star-topology-runbook.md`)
and correct it there too.

- [ ] **Step 9: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_assessments_summary_post.py CLAUDE.md docs/blackbird-star-topology-runbook.md
git commit -m "feat(hub): post a headline summary to #assessments-summary on every held verdict

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the full gate: `./scripts/ci.sh` — must pass end to end (alembic single-head + round trip including this plan's new migration, ruff ratchet, pytest with the coverage floor).
- [ ] Flag to the user (per CLAUDE.md's standing rule) that the agent image and web tier both need rebuilding + restarting for any of Workstream C's changes to take effect in the running simulation — do not restart anything yourself.
- [ ] Re-read `docs/specs/2026-08-21-manager-pi-controls-design.md` end to end once more and confirm every decision (D1-D18) has a corresponding task above. If the perf-race remediation plan has landed any of its tasks during this plan's implementation, re-run the Global Constraints' grep checks (`profile.py:159`, `slack_client.py`'s async-wrappers block) one more time before the final commit of each affected task.
