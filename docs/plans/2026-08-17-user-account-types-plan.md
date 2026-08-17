# User Account Types (PI / manager / admin) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third account type — a global, strictly read-only **manager** — alongside the existing PI and admin types, with its own view surface at `/manager/*`.

**Architecture:** Replace the single `User.is_admin` boolean with a `user_role` enum column, keeping `is_admin` alive as a read-only SQLAlchemy `hybrid_property` so all eight existing read sites work unchanged. `/admin/*` keeps its 34 per-endpoint gates untouched; managers get a **new** `/manager` router with a router-level dependency (deny-by-default). Query logic shared by both routers is extracted into `src/services/directory.py` so it exists once.

**Tech Stack:** FastAPI 0.141.1, SQLAlchemy 2.0.51 (async, `hybrid_property`), Alembic, Jinja2 templates + Tailwind CDN, pytest + pytest-asyncio, testcontainers Postgres.

**Spec:** `docs/specs/2026-08-17-user-account-types-design.md` — read it first. This plan implements it; the `F<n>` and `D<n>` references below are its audit findings and decisions.

## Global Constraints

- **Run the gate with `./scripts/ci.sh`.** There is no server-side CI. It enforces: exactly one Alembic head, no duplicate revision ids, an upgrade→downgrade→upgrade round trip from `MIGRATION_FLOOR=0018` (so a new migration's **downgrade runs on every push**), zero ruff findings in `tests/`, a ruff **ceiling of 231** on `src/` (`SRC_LINT_MAX` — never raise it), and `COV_MIN=60` branch coverage.
- **Run pytest on the host, never in the container:** `.venv-test/bin/python -m pytest tests/ -v`. The image has no `[dev]` extra, so pytest is not installed there.
- **`user_role`, never `role`.** `AgentRegistry.role` and `PrivateChannelMember.role` already exist and mean other things (F3).
- **`is_admin` must stay false for a manager.** The "admin or manager" predicate is `is_staff`. Never widen `is_admin` (F7).
- **`/manager` has zero non-GET routes** (D12).
- **`tests/unit/test_reachability.py` is a hard gate.** Every new template must be rendered by a literal name from `src/` (`test_no_dynamic_template_references`), every link in it must resolve to a real route (`test_template_links_resolve_to_a_real_route`), and every new route must be linked from a reachable template or added to `ROUTE_ALLOWLIST` with a written reason (`test_no_unreachable_routes`). **Consequence: never add a nav link to a route that does not exist yet** — the tabs are therefore added tab-by-tab in Tasks 4, 5 and 6, not all at once.
- **Jinja-expression links do not credit a route.** `_link_credits` is strict: a Jinja expression only fills a `{path_param}` slot. So `href="{{ view_root }}/assessments"` would leave `/manager/assessments` unreferenced and fail the gate. **All manager links must be literal `/manager/...` strings.**
- Commit message style follows the repo: `feat(web): …`, `fix(web): …`, `test(web): …`, `refactor(web): …`.

---

## File Structure

**Created**

| File | Responsibility |
|---|---|
| `alembic/versions/0028_add_user_role.py` | Additive migration: add `user_role`, backfill from `is_admin`, give `is_admin` a server default (F14), add CHECK |
| `src/services/directory.py` | HTTP-free query functions shared by the admin and manager routers |
| `src/routers/manager.py` | The `/manager` read-only router; router-level `get_staff_user` |
| `templates/manager/pis.html` | Manager PI directory (no impersonate widget, no delete button — F6) |
| `templates/manager/pi_detail.html` | Manager PI record (no Danger Zone, no `is_admin` row — F6) |
| `templates/manager/assessments.html` | Wrapper: literal `/manager` links + shared body partial |
| `templates/manager/discussions.html` | Wrapper: literal `/manager` links + shared threads partial |
| `templates/manager/activity.html` | Run list (duplicated outright — only 73 lines) |
| `templates/manager/activity_detail.html` | Wrapper: literal `/manager` links + shared body partial, **no llm-calls link** (D10) |
| `templates/admin/_assessments_body.html` | Link-free bulk of the assessments page, shared |
| `templates/admin/_discussions_threads.html` | Link-free thread list, shared |
| `templates/admin/_run_detail_body.html` | Link-free run-detail body, shared |
| `tests/unit/test_user_roles.py` | Role predicate semantics, incl. the F7 escalation guard. No DB |
| `tests/integration/test_manager_access.py` | `get_staff_user` gating; the shared `auth_headers` helper |
| `tests/integration/test_directory_service.py` | The extracted service's new `roles` filter |
| `tests/integration/test_manager_views.py` | The `/manager` pages, deny-by-default sweep, no-mutation-routes assertion, manager blocked from every `/admin/*` route |
| `tests/integration/test_manager_onboarding.py` | Managers skip PI onboarding and fire no profile job |
| `tests/integration/test_role_appointment.py` | The admin role form and its guards |
| `alembic/versions/0029_drop_is_admin.py` | **Separate later deploy** (Task 10) |

**Modified**

| File | Change |
|---|---|
| `src/models/user.py:24` | Drop the `is_admin` column; add `user_role` + three hybrid predicates + role constants |
| `src/models/__init__.py` | Export the role constants |
| `src/dependencies.py:135` | Append `get_staff_user` |
| `src/main.py:146` | Include the manager router |
| `src/routers/admin.py` | Six handler bodies call `src/services/directory.py`; new `POST /users/{user_id}/role`. **No decorator and no `Depends(get_admin_user)` line is edited** |
| `src/routers/auth.py:295,300` | PI-only onboarding redirect; manager post-login landing |
| `src/routers/onboarding.py:55,75` | Bounce non-PI roles; do not enqueue `generate_profile` for them |
| `src/cli.py:107-165` | `admin:grant`/`admin:revoke` write `user_role`; new `role:set` |
| `templates/base.html:52-109` | Gate PI nav on role; add the Manager link and sub-nav |
| `templates/admin/user_detail.html:36-39` | Role row + role form, replacing the `Admin: Yes/No` row |
| `templates/admin/assessments.html`, `discussions.html`, `activity_detail.html` | Body extracted to partials |
| `tests/factories.py:35` | `is_admin=False` → `user_role=USER_ROLE_PI` |
| `tests/e2e/seed.py:133,137` | Same, including the direct `admin.is_admin = True` assignment |
| 4 test files | The `is_admin=` keyword call sites (enumerated in Task 1, Step 7) |

---

## Task 1: Role column, hybrid predicates, migration 0028

**Files:**
- Modify: `src/models/user.py:24` (remove the `is_admin` column), `src/models/user.py:34-48` (add `user_role`)
- Modify: `src/models/__init__.py`
- Create: `alembic/versions/0028_add_user_role.py`
- Modify: `src/cli.py:124`, `src/cli.py:155`, `src/cli.py:190`
- Modify: `tests/factories.py:35`, `tests/e2e/seed.py:133,137`, and the call sites in Step 7
- Test: `tests/unit/test_user_roles.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `USER_ROLE_PI = "pi"`, `USER_ROLE_MANAGER = "manager"`, `USER_ROLE_ADMIN = "admin"`, `VALID_USER_ROLES: tuple[str, str, str]`, all importable from `src.models`. `User.user_role: str` (column). `User.is_admin`, `User.is_manager`, `User.is_staff` — read-only `hybrid_property` returning `bool` in Python and a SQL boolean in a `select()`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_user_roles.py`:

```python
"""Account-type predicates: pi / manager / admin.

The load-bearing assertion here is that `is_admin` is FALSE for a manager.
Impersonation (src/dependencies.py:74 and the duplicate check at
src/main.py:52) is gated on is_admin and returns a fully substituted User, so
any formulation of is_admin that a manager satisfied would hand managers full
admin. See F7 in the spec.
"""

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    VALID_USER_ROLES,
    User,
)

# No pytestmark: this file touches no database. The repo registers only
# integration / characterization / contract / real_llm / live_slack / live_api
# in pyproject.toml — there is no `unit` marker, and most files in tests/unit
# carry none. `asyncio_mode = "auto"` means async tests need no marker either.


def _user(role: str) -> User:
    return User(name="X", orcid="0000-0000-0000-0001", user_role=role)


def test_valid_roles_are_exactly_the_three_account_types():
    assert VALID_USER_ROLES == (USER_ROLE_PI, USER_ROLE_MANAGER, USER_ROLE_ADMIN)


@pytest.mark.parametrize(
    "role,expect_admin,expect_manager,expect_staff",
    [
        (USER_ROLE_PI, False, False, False),
        (USER_ROLE_MANAGER, False, True, True),
        (USER_ROLE_ADMIN, True, False, True),
    ],
)
def test_predicates_in_python(role, expect_admin, expect_manager, expect_staff):
    u = _user(role)
    assert u.is_admin is expect_admin
    assert u.is_manager is expect_manager
    assert u.is_staff is expect_staff


def test_is_admin_is_false_for_a_manager():
    """The escalation guard (F7). Never relax this to is_staff."""
    assert _user(USER_ROLE_MANAGER).is_admin is False


def test_an_admin_is_staff_but_not_a_manager():
    admin = _user(USER_ROLE_ADMIN)
    assert admin.is_staff is True
    assert admin.is_manager is False


def test_is_admin_is_read_only():
    """Proves the three assignment sites must be rewritten: src/cli.py:124,
    src/cli.py:155, tests/e2e/seed.py:137."""
    with pytest.raises(AttributeError):
        _user(USER_ROLE_PI).is_admin = True


def test_is_admin_compiles_to_sql_over_user_role():
    """Pins src/main.py:52, which runs select(User.is_admin). A plain
    @property is invisible to SQL and that query would raise."""
    assert "user_role" in str(select(User.is_admin))


def test_is_staff_compiles_to_a_sql_in_clause():
    assert "user_role" in str(select(User).where(User.is_staff))


def test_default_role_is_pi():
    u = User(name="X", orcid="0000-0000-0000-0002")
    assert u.user_role is None or u.user_role == USER_ROLE_PI
```

The last test tolerates `None` before flush because `default=` is applied at INSERT time, not construction; the DB-backed assertion lives in Task 2.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_user_roles.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'USER_ROLE_PI' from 'src.models'`.

- [ ] **Step 3: Add the constants, column and predicates**

In `src/models/user.py`, add to the imports:

```python
from sqlalchemy.ext.hybrid import hybrid_property
```

Add above `class User`:

```python
# Account types. One user has exactly one; they are mutually exclusive (D7).
# Named user_role, not role: AgentRegistry.role and PrivateChannelMember.role
# already exist and mean other things (F3).
USER_ROLE_PI = "pi"
USER_ROLE_MANAGER = "manager"
USER_ROLE_ADMIN = "admin"
VALID_USER_ROLES = (USER_ROLE_PI, USER_ROLE_MANAGER, USER_ROLE_ADMIN)
```

**Delete** line 24 (`is_admin: Mapped[bool] = mapped_column(...)`) and add in its place:

```python
    user_role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=USER_ROLE_PI, server_default=USER_ROLE_PI
    )
```

Add these after the relationships and before `__repr__`:

```python
    # is_admin stays readable as a hybrid rather than a plain @property because
    # src/main.py:52 runs `select(User.is_admin)` — SQL, which a @property
    # cannot satisfy (F13). The hybrid compiles to `users.user_role = 'admin'`,
    # so main.py, templates/base.html:69, base.html:93,
    # templates/admin/user_detail.html:38 and tests/integration/test_cli.py:383
    # all keep working with no edit. It is READ-ONLY on purpose: `is_admin =
    # False` on a manager would have no correct answer.
    @hybrid_property
    def is_admin(self) -> bool:
        return self.user_role == USER_ROLE_ADMIN

    @is_admin.inplace.expression
    @classmethod
    def _is_admin_expr(cls):
        return cls.user_role == USER_ROLE_ADMIN

    @hybrid_property
    def is_manager(self) -> bool:
        return self.user_role == USER_ROLE_MANAGER

    @is_manager.inplace.expression
    @classmethod
    def _is_manager_expr(cls):
        return cls.user_role == USER_ROLE_MANAGER

    # The "may see the manager views" predicate. Everything that means
    # "admin OR manager" must name THIS, never a widened is_admin (F7).
    @hybrid_property
    def is_staff(self) -> bool:
        return self.user_role in (USER_ROLE_MANAGER, USER_ROLE_ADMIN)

    @is_staff.inplace.expression
    @classmethod
    def _is_staff_expr(cls):
        return cls.user_role.in_((USER_ROLE_MANAGER, USER_ROLE_ADMIN))
```

In `src/models/__init__.py`, extend the `from src.models.user import User` line to:

```python
from src.models.user import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    VALID_USER_ROLES,
    User,
)
```

and add `"USER_ROLE_PI"`, `"USER_ROLE_MANAGER"`, `"USER_ROLE_ADMIN"`, `"VALID_USER_ROLES"` to `__all__`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv-test/bin/python -m pytest tests/unit/test_user_roles.py -v`
Expected: PASS, all 9 tests.

- [ ] **Step 5: Write migration 0028**

Create `alembic/versions/0028_add_user_role.py`:

```python
"""Add users.user_role (PI / manager / admin account types)

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-17 00:00:00.000000

Additive on purpose. The model stops mapping users.is_admin in the same change,
so this migration is safe to apply BEFORE the new code is running and the old
code keeps working after it — there is no window where live code and applied
schema disagree. The column drop is deferred to 0029, a separate later deploy.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("user_role", sa.String(20), nullable=False, server_default="pi"),
    )
    op.execute("UPDATE users SET user_role = 'admin' WHERE is_admin = true")
    # 0001_initial.py:33 declared is_admin with Alembic's Python-side `default=`,
    # which emits NO DDL DEFAULT — so the column is NOT NULL with nothing to
    # fall back on. The model stops mapping it as of this change, so without
    # this line the next INSERT INTO users omits the column, Postgres rejects
    # the row, and src/routers/auth.py:215 can no longer create a user. See F14.
    op.alter_column("users", "is_admin", server_default=sa.text("false"))
    op.create_check_constraint(
        "ck_users_user_role",
        "users",
        "user_role IN ('pi', 'manager', 'admin')",
    )


def downgrade() -> None:
    # Restore is_admin from the enum before dropping the enum, so the downgrade
    # is data-preserving rather than just structurally reversible.
    op.execute("UPDATE users SET is_admin = (user_role = 'admin')")
    op.drop_constraint("ck_users_user_role", "users", type_="check")
    op.drop_column("users", "user_role")
    op.alter_column("users", "is_admin", server_default=None)
```

- [ ] **Step 6: Round-trip the migration against a throwaway Postgres**

Run exactly what the gate runs, so a failure here is the gate's failure:

```bash
docker rm -f copi-ci-migcheck 2>/dev/null || true
docker run -d --name copi-ci-migcheck \
  -e POSTGRES_USER=copi -e POSTGRES_PASSWORD=copi -e POSTGRES_DB=copi_migcheck \
  -p 127.0.0.1:55432:5432 postgres:15
until docker exec copi-ci-migcheck pg_isready -U copi -q; do sleep 1; done
DSN=postgresql+asyncpg://copi:copi@127.0.0.1:55432/copi_migcheck
DATABASE_URL=$DSN .venv-test/bin/python -m alembic upgrade head
DATABASE_URL=$DSN .venv-test/bin/python -m alembic downgrade 0018
DATABASE_URL=$DSN .venv-test/bin/python -m alembic upgrade head
docker rm -f copi-ci-migcheck
.venv-test/bin/python -m alembic heads   # must print exactly one head: 0028
```

Expected: all three Alembic commands exit 0; `heads` prints one line.

- [ ] **Step 7: Rewrite every `is_admin` write site**

`is_admin` is now read-only, so `User(is_admin=...)` raises `AttributeError` (SQLAlchemy's default constructor `setattr`s any name the class has, and the hybrid has no setter). Do **not** add a compatibility shim to `factories.make_user` that translates `is_admin=` — it would leave the suite speaking two vocabularies and make `make_user(is_admin=False, user_role="manager")` silently contradictory.

Production code:
- `src/cli.py:124` — `user.is_admin = True` → `user.user_role = USER_ROLE_ADMIN`
- `src/cli.py:155` — `user.is_admin = False` → `user.user_role = USER_ROLE_PI`
- `src/cli.py:190` — the `"Yes" if user.is_admin else "No"` table cell → `user.user_role`; rename the column header at `src/cli.py:187` from `"Admin"` to `"Role"`
- Add `from src.models import USER_ROLE_ADMIN, USER_ROLE_PI` to the two command bodies that need it (they already import `User` lazily inside the closures — follow that pattern)

Tests — replace the keyword only; leave every `.is_admin` **read** alone, because the hybrid keeps them working:
- `tests/factories.py:35` — `is_admin=False,` → `user_role=USER_ROLE_PI,` (and import the constant)
- `tests/e2e/seed.py:133` — `is_admin=True,` → `user_role=USER_ROLE_ADMIN,`
- `tests/e2e/seed.py:137` — `admin.is_admin = True` → `admin.user_role = USER_ROLE_ADMIN`
- `tests/characterization/test_auth_and_admin_routes.py` lines 143, 149, 181, 217, 250
- `tests/integration/test_cohort_admin.py` lines 32, 96, 151
- `tests/integration/test_onboarding_flow.py` lines 1312, 1385
- `tests/integration/test_opportunity_assessment_persistence.py` lines 26, 1169
- `tests/integration/test_cli.py` lines 375, 377, 416, 459, 468

In `tests/integration/test_cli.py`, the reader helper at line 382-383 (`return db(...).is_admin`) and the assertions at 387, 390, 392, 395, 397, 403, 404, 425, 434 **stay as they are** — they exercise the hybrid, which is exactly what we want pinned.

Sanity check that nothing was missed:

```bash
grep -rn "is_admin=" src/ tests/ --include=*.py | grep -v __pycache__
```

Expected: no output.

- [ ] **Step 8: Run the full suite**

Run: `.venv-test/bin/python -m pytest tests/ -q`
Expected: PASS. Every previously-passing test still passes; `test_user_roles.py` adds 9.

- [ ] **Step 9: Commit**

```bash
git add src/models/user.py src/models/__init__.py src/cli.py \
        alembic/versions/0028_add_user_role.py \
        tests/unit/test_user_roles.py tests/factories.py tests/e2e/seed.py \
        tests/characterization/test_auth_and_admin_routes.py \
        tests/integration/test_cohort_admin.py \
        tests/integration/test_onboarding_flow.py \
        tests/integration/test_opportunity_assessment_persistence.py \
        tests/integration/test_cli.py
git commit -m "feat(web): user_role enum with is_admin as a read-only hybrid

Adds pi/manager/admin as one column and derives is_admin from it, so
select(User.is_admin) (src/main.py:52) and the four template/CLI read
sites keep working untouched. is_admin is deliberately read-only: on a
manager, 'is_admin = False' has no correct answer.

0028 is additive and gives the now-unmapped is_admin column a server
default, so old code keeps working against the new schema and the drop
can wait for 0029."
```

---

## Task 2: The `get_staff_user` dependency

**Files:**
- Modify: `src/dependencies.py` (append after `get_admin_user`, line 135)
- Test: `tests/integration/test_manager_access.py`

**Interfaces:**
- Consumes: `User.is_staff` from Task 1.
- Produces: `async def get_staff_user(current_user: User = Depends(get_current_user)) -> User` — raises `HTTPException(403)` for a PI, returns the user for a manager or admin.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_manager_access.py`:

```python
"""Role gating: get_staff_user, and the guarantee that a manager cannot reach
/admin or impersonate anyone.
"""

import base64
import json

import pytest
from fastapi import Depends, FastAPI
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.dependencies import get_staff_user
from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User
from tests import factories

pytestmark = pytest.mark.integration


def _session_cookie(user_id) -> str:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return signer.sign(data).decode("utf-8")


def auth_headers(user_id) -> dict:
    """Shared by test_manager_views.py — keep the two in sync."""
    return {"Cookie": f"copi-session={_session_cookie(user_id)}"}


async def test_default_role_is_pi_in_the_database(db_session):
    u = User(name="Fresh", orcid="0000-0000-0000-9001")
    db_session.add(u)
    await db_session.flush()
    assert u.user_role == USER_ROLE_PI


async def test_is_admin_filters_rows_in_the_database(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    rows = (await db_session.execute(select(User).where(User.is_admin))).scalars().all()
    assert [u.user_role for u in rows] == [USER_ROLE_ADMIN]


async def test_is_staff_filters_admin_and_manager_only(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    rows = (await db_session.execute(select(User).where(User.is_staff))).scalars().all()
    assert sorted(u.user_role for u in rows) == [USER_ROLE_ADMIN, USER_ROLE_MANAGER]


@pytest.mark.parametrize(
    "role,expected",
    [(USER_ROLE_PI, 403), (USER_ROLE_MANAGER, 200), (USER_ROLE_ADMIN, 200)],
)
async def test_get_staff_user_gates_by_role(db_session, monkeypatch, role, expected):
    import httpx
    from httpx import ASGITransport

    from src.database import get_db

    user = await factories.make_user(db_session, user_role=role)

    app = FastAPI()

    @app.get("/probe")
    async def probe(u: User = Depends(get_staff_user)):
        return {"role": u.user_role}

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(
        SessionMiddleware,
        secret_key=get_settings().secret_key,
        session_cookie="copi-session",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/probe", headers=auth_headers(user.id))
    assert r.status_code == expected
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_access.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'get_staff_user'`.

- [ ] **Step 3: Add the dependency**

Append to `src/dependencies.py`:

```python
async def get_staff_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency that requires admin OR manager.

    Used ONLY by the /manager router. This is deliberately a separate
    dependency rather than a relaxation of get_admin_user: /admin declares its
    gate on 34 individual handlers (F5), and widening the one they share is how
    a read-only role would quietly acquire write endpoints.

    Note this also 403s an admin who is currently impersonating a PI, because
    get_current_user returns the impersonated user. That is correct.
    """
    if not current_user.is_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Manager access required"
        )
    return current_user
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_access.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add src/dependencies.py tests/integration/test_manager_access.py
git commit -m "feat(web): add get_staff_user (admin or manager) dependency"
```

---

## Task 3: Extract `src/services/directory.py`

Pure refactor. The existing characterization tests are the safety net: `tests/characterization/test_auth_and_admin_routes.py:171-266` pins `/admin/discussions` and `/admin/activity/{run_id}`, `tests/integration/test_opportunity_assessment_persistence.py:26` pins `/admin/assessments`.

**Files:**
- Create: `src/services/directory.py`
- Modify: `src/routers/admin.py` — bodies of `admin_users` (89-142), `admin_user_detail` (167-193), `admin_assessments` (885-936), `admin_discussions` (519-708), `admin_activity` (269-300), `admin_activity_detail` (318-385)
- Test: `tests/integration/test_directory_service.py`

**Interfaces:**
- Consumes: `USER_ROLE_PI` from Task 1.
- Produces, all `async` and all HTTP-free (no `Request`, no `HTTPException`, no `templates`):

```python
async def list_pi_directory(
    db: AsyncSession, *,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    roles: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]
# each dict: {"user", "profile", "profile_status", "pub_count", "agent_status"}
# roles=None means no role filter (today's /admin behaviour, unchanged).

async def load_user_detail(db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any] | None
# {"user", "profile", "publications", "jobs"}; None when the row is absent.

async def list_assessments(db: AsyncSession, run_id: str | None) -> dict[str, Any]
# {"assessments", "runs", "runs_by_id", "selected_run_id", "show_all_runs",
#  "total_count", "assessments_limit", "drop_counts", "drops_total",
#  "rubric_weights"}

async def build_discussions_view(
    db: AsyncSession, *,
    run_id: str | None, channel_filter: str | None,
    status_filter: str | None, agent_filter: list[str],
) -> dict[str, Any]
# {"runs", "selected_run_id", "threads", "counts", "channels", "agents",
#  "channel_filter", "status_filter", "agent_filter"}

async def list_runs_overview(db: AsyncSession) -> dict[str, Any]
# {"runs", "total_runs", "total_messages", "total_channels",
#  "most_active_agent", "most_active_count"}

async def build_run_detail(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any] | None
# {"run", "messages", "channels", "agent_stats", "channel_stats"}; None when absent.
```

- [ ] **Step 1: Write the failing test for the one piece of NEW behaviour**

The moves are covered by existing tests. The `roles=` filter is new, so it gets its own test. Create `tests/integration/test_directory_service.py`:

```python
"""The directory service. The extracted queries are pinned by the existing
characterization tests; what is new here is the `roles` filter, which is how
one function serves both /admin (no filter) and /manager (PIs only, D11).
"""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI
from src.services.directory import list_pi_directory
from tests import factories

pytestmark = pytest.mark.integration


async def test_roles_none_returns_every_account_type(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    rows = await list_pi_directory(db_session)
    assert sorted(r["user"].user_role for r in rows) == [
        USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI,
    ]


async def test_roles_pi_excludes_staff_accounts(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    rows = await list_pi_directory(db_session, roles=(USER_ROLE_PI,))
    assert [r["user"].user_role for r in rows] == [USER_ROLE_PI]


async def test_unclaimed_pi_stubs_are_included(db_session):
    """D11: managers see recruitment coverage, so a seeded-but-never-signed-in
    PI (claimed_at=None, no profile) must still appear."""
    await factories.make_user(
        db_session, user_role=USER_ROLE_PI, claimed_at=None, onboarding_complete=False
    )
    rows = await list_pi_directory(db_session, roles=(USER_ROLE_PI,))
    assert len(rows) == 1
    assert rows[0]["profile_status"] == "no_profile"


async def test_load_user_detail_returns_none_for_a_missing_row(db_session):
    import uuid
    from src.services.directory import load_user_detail
    assert await load_user_detail(db_session, uuid.uuid4()) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_directory_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.directory'`.

- [ ] **Step 3: Create the service module by moving code**

Create `src/services/directory.py` with a module docstring explaining why it exists, and move each body **verbatim** from `src/routers/admin.py`, changing only:

1. The function signature to the one in **Interfaces** above.
2. `return templates.TemplateResponse(request, "...", _template_context(...))` → `return {…}` with exactly the keys listed above.
3. `raise HTTPException(404, ...)` → `return None` (the router raises; the service stays HTTP-free).
4. In `list_pi_directory`, add the role filter to the query:

```python
    query = select(User).options(
        selectinload(User.profile), selectinload(User.jobs), selectinload(User.agent)
    )
    if roles is not None:
        query = query.where(User.user_role.in_(roles))
```

5. In `build_discussions_view`, **stop before** the `if export:` branch at `admin.py:710`. Export stays in `admin.py` and consumes `view["threads"]` — it is admin-only, and the manager route never accepts the parameter.
6. `list_assessments` also returns `"rubric_weights": RUBRIC_WEIGHTS` and `"assessments_limit": _ASSESSMENTS_LIMIT`; move `_ASSESSMENTS_LIMIT` (`admin.py:855`) into this module as `ASSESSMENTS_LIMIT` and import it back into `admin.py` if anything else references it.

- [ ] **Step 4: Rewire the six admin handlers**

Each becomes a thin caller. `admin_users` in full, as the pattern to copy:

```python
@router.get("", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Admin users overview."""
    user_data = await list_pi_directory(
        db,
        status_filter=status_filter,
        institution_filter=institution_filter,
        claimed_filter=claimed_filter,
    )
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _template_context(
            request,
            current_user,
            active_admin="users",
            user_data=user_data,
            status_filter=status_filter,
            institution_filter=institution_filter,
            claimed_filter=claimed_filter,
        ),
    )
```

`admin_user_detail` and `admin_activity_detail` regain their 404 at the router level:

```python
    detail = await load_user_detail(db, user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="User not found")
```

**Do not touch any `@router.get`/`@router.post` decorator or any `Depends(get_admin_user)` line.** Verify:

```bash
git diff -U0 src/routers/admin.py | grep -E '^[-+].*(get_admin_user|@router\.)'
```

Expected: no output.

- [ ] **Step 5: Run the refactor's safety net**

```bash
.venv-test/bin/python -m pytest tests/integration/test_directory_service.py \
  tests/characterization/test_auth_and_admin_routes.py \
  tests/integration/test_opportunity_assessment_persistence.py \
  tests/integration/test_cohort_admin.py -q
```

Expected: PASS. If `/admin/discussions` or `/admin/activity/{run_id}` regressed, it fails here — those two pins exist precisely because both pages have 500'd in production before.

- [ ] **Step 6: Run the full suite and the lint ratchet**

```bash
.venv-test/bin/python -m pytest tests/ -q
.venv-test/bin/python -m ruff check src --output-format=concise --quiet | grep -c . || true
```
Expected: suite PASS; the ruff count must be **≤ 231**. Extraction moves lines rather than adding them, so this should fall, not rise.

- [ ] **Step 7: Commit**

```bash
git add src/services/directory.py src/routers/admin.py tests/integration/test_directory_service.py
git commit -m "refactor(web): extract admin read queries into services/directory

Six query bodies move out of the 73KB admin router into HTTP-free
functions so the manager router can call the same code instead of
carrying a second copy of a ~280-line discussions query. No decorator
and no Depends(get_admin_user) line is touched; behaviour is pinned by
the existing characterization tests.

Adds a `roles` filter, which is how one function serves /admin (all
accounts) and /manager (PIs only)."
```

---

## Task 4: Manager router + PI directory

**Files:**
- Create: `src/routers/manager.py`
- Modify: `src/main.py:15` (import), `src/main.py:148` (include)
- Create: `templates/manager/pis.html`, `templates/manager/pi_detail.html`
- Modify: `templates/base.html:69-74` (Manager nav link), `templates/base.html:109` (manager sub-nav block)
- Test: `tests/integration/test_manager_views.py`

**Interfaces:**
- Consumes: `get_staff_user` (Task 2); `list_pi_directory`, `load_user_detail` (Task 3); `USER_ROLE_PI`.
- Produces: `src.routers.manager.router` with `GET /manager`, `GET /manager/pis`, `GET /manager/pis/{user_id}`; and `_template_context(request, current_user, active_manager="", **kwargs)` setting `active_page="manager"`.

> **Reachability sequencing.** The manager sub-nav gets its **PIs tab only** in this task. Adding an Assessments/Discussions/Activity tab now would link to routes that do not exist yet and fail `test_template_links_resolve_to_a_real_route`. Tasks 5 and 6 add their own tabs.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_manager_views.py`:

```python
"""The /manager surface: deny-by-default, read-only, and PI-scoped."""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI
from src.routers import manager as manager_router
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _manager_get_paths() -> list[str]:
    """Every GET path on the manager router, with path params filled by name.

    Enumerated from the router rather than hand-listed so a route added later
    is automatically covered by the sweeps below. This is what keeps
    deny-by-default honest instead of aspirational.
    """
    return sorted(
        r.path for r in manager_router.router.routes if "GET" in getattr(r, "methods", ())
    )


def test_manager_router_exposes_no_mutating_routes():
    """D12. If there is no mutation route there is no mutation risk, and this
    turns that from a promise into a check."""
    methods = {m for r in manager_router.router.routes for m in getattr(r, "methods", ())}
    assert methods == {"GET"}, f"non-GET route on the manager router: {methods}"


async def test_unauthenticated_manager_root_redirects_to_login(client):
    r = await client.get("/manager", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


async def test_pi_is_denied_the_manager_surface(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    for path in ("/manager", "/manager/pis"):
        r = await client.get(path, headers=auth_headers(pi.id), follow_redirects=False)
        assert r.status_code == 403, f"{path} was reachable by a PI"


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_ADMIN])
async def test_staff_can_read_the_pi_directory(client, db_session, role):
    staff = await factories.make_user(db_session, user_role=role)
    await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Dr Target")
    r = await client.get("/manager/pis", headers=auth_headers(staff.id))
    assert r.status_code == 200
    assert "Dr Target" in r.text


async def test_manager_root_redirects_to_the_pi_directory(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get("/manager", headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/pis"


async def test_directory_excludes_staff_accounts(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr Self")
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Sneaky Admin")
    await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Real PI")
    r = await client.get("/manager/pis", headers=auth_headers(mgr.id))
    assert "Real PI" in r.text
    assert "Sneaky Admin" not in r.text
    assert "Mgr Self" not in r.text


async def test_pi_detail_is_readable(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, name="Dr Detail", email="pi@example.edu"
    )
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    assert "Dr Detail" in r.text
    assert "pi@example.edu" in r.text  # D3: contact info is in scope


async def test_pi_detail_404s_for_a_staff_account(client, db_session):
    """Closes UUID enumeration of admin/manager records."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(f"/manager/pis/{admin.id}", headers=auth_headers(mgr.id))
    assert r.status_code == 404


async def test_pi_detail_has_no_delete_or_impersonate_control(client, db_session):
    """F6: the admin templates carry both; the manager templates must not."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "/delete" not in body
    assert "impersonate" not in body.lower()
    assert "Danger Zone" not in body


async def test_manager_is_denied_every_admin_route(client, db_session):
    from src.routers import admin as admin_router

    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    checked = 0
    for route in admin_router.router.routes:
        if "GET" not in getattr(route, "methods", ()) or "{" in route.path:
            continue
        r = await client.get(
            f"/admin{route.path}", headers=auth_headers(mgr.id), follow_redirects=False
        )
        assert r.status_code == 403, f"/admin{route.path} leaked to a manager"
        checked += 1
    assert checked >= 8, "the admin sweep matched too few routes to be meaningful"


async def test_manager_cannot_impersonate(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        "/admin/impersonate", data={"orcid": pi.orcid}, headers=auth_headers(mgr.id)
    )
    assert r.status_code == 403


async def test_a_hand_set_impersonate_cookie_is_ignored_for_a_manager(client, db_session):
    """F7: the cookie is unsigned and client-supplied. get_current_user honours
    it only for is_admin, which a manager never satisfies."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="TheAdmin")
    headers = auth_headers(mgr.id)
    headers["Cookie"] += f"; copi-impersonate={admin.id}"
    r = await client.get("/manager/pis", headers=headers, follow_redirects=False)
    assert r.status_code == 200          # still the manager, not the admin
    r2 = await client.get("/admin/users", headers=headers, follow_redirects=False)
    assert r2.status_code == 403         # did NOT become an admin
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_views.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'manager' from 'src.routers'`.

- [ ] **Step 3: Create the router**

Create `src/routers/manager.py`:

```python
"""Manager dashboard router — global, strictly read-only.

Every route here is GET, and the router carries its gate as a router-level
dependency rather than a per-handler one. /admin declares Depends(get_admin_user)
on 34 separate handlers (F5), which means a route added there without the
declaration is open to any logged-in user. A router-level dependency makes that
mistake impossible for this surface: a new route is gated by construction.

Query logic lives in src/services/directory.py and is shared with /admin.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user
from src.models import USER_ROLE_PI, User
from src.services.directory import list_pi_directory, load_user_detail

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(get_staff_user)])
templates = Jinja2Templates(directory="templates")


def _template_context(
    request: Request, current_user: User, active_manager: str = "", **kwargs
) -> dict:
    ctx = {
        "request": request,
        "current_user": current_user,
        "active_page": "manager",
        "active_manager": active_manager,
    }
    ctx.update(kwargs)
    return ctx


@router.get("", response_class=HTMLResponse)
async def manager_root():
    """Bare-prefix landing. The top nav links here; the sub-nav links the children."""
    return RedirectResponse(url="/manager/pis", status_code=302)


@router.get("/pis", response_class=HTMLResponse)
async def manager_pis(
    request: Request,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_staff_user),
):
    """PI directory. Unclaimed stubs included (D11) so recruitment coverage is
    visible; staff accounts excluded so the admin roster is not enumerable."""
    user_data = await list_pi_directory(
        db,
        status_filter=status_filter,
        institution_filter=institution_filter,
        claimed_filter=claimed_filter,
        roles=(USER_ROLE_PI,),
    )
    return templates.TemplateResponse(
        request,
        "manager/pis.html",
        _template_context(
            request,
            current_user,
            active_manager="pis",
            user_data=user_data,
            status_filter=status_filter,
            claimed_filter=claimed_filter,
        ),
    )


@router.get("/pis/{user_id}", response_class=HTMLResponse)
async def manager_pi_detail(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_staff_user),
):
    """One PI's record. 404s on a non-PI account so a manager cannot read an
    admin's row by guessing or harvesting a UUID."""
    detail = await load_user_detail(db, user_id)
    if detail is None or detail["user"].user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")
    return templates.TemplateResponse(
        request,
        "manager/pi_detail.html",
        _template_context(
            request,
            current_user,
            active_manager="pis",
            target_user=detail["user"],
            profile=detail["profile"],
            publications=detail["publications"],
            jobs=detail["jobs"],
        ),
    )
```

In `src/main.py`, add `manager` to the router import at line 15 and include it after the admin router:

```python
    application.include_router(manager.router, prefix="/manager", tags=["manager"])
```

- [ ] **Step 4: Create the two templates**

`templates/manager/pis.html` — start from `templates/admin/users.html` and **delete the impersonation widget (lines 10-20)**. Keep the filter block, the table, and the row `onclick`, changing every URL to `/manager/...`:

```html
{% extends "base.html" %}
{% block title %}Manager — PIs — CoPI{% endblock %}

{% block content %}
<div class="flex items-center justify-between mb-6">
    <h1 class="text-2xl font-bold text-gray-900">PIs</h1>
    <span class="text-sm text-gray-500">{{ user_data | length }} PIs</span>
</div>
```

then the filter `<div>` from `admin/users.html:23-44` verbatim, then the table from
`admin/users.html:47-134` with line 66's `onclick` changed to
`location.href='/manager/pis/{{ item.user.id }}'`, then the `<script>` from lines
136-145 with `location.href = '/manager/pis?' + params.toString();`.

`templates/manager/pi_detail.html` — start from `templates/admin/user_detail.html` and:
- change the back link (line 8) to `/manager/pis`
- **delete the entire Danger Zone block (lines 121-134)** — F6
- **delete the `Admin` `<div>` (lines 36-39)** — it enumerates the admin roster and is meaningless on a PI-only page
- keep Email, ORCID, Institution, Department, Onboarding Complete, Claimed At, Joined (D3: contact + engagement metadata), the Research Profile block, Publications, and Jobs

- [ ] **Step 5: Add the nav link and the sub-nav (PIs tab only)**

In `templates/base.html`, after the Admin link block (line 74):

```html
                    {% if current_user.is_staff and not impersonation_banner %}
                    <a href="/manager" class="text-gray-600 hover:text-indigo-600 px-3 py-2 rounded-md text-sm font-medium
                        {% if active_page == 'manager' %}text-indigo-600 font-semibold{% endif %}">
                        Manager
                    </a>
                    {% endif %}
```

After the admin sub-nav block (line 109), add the manager sub-nav. **Only the PIs tab** — Tasks 5 and 6 add theirs:

```html
<!-- Manager sub-navigation -->
{% if active_page == 'manager' and current_user and current_user.is_staff %}
<div class="bg-gray-50 border-b border-gray-200">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div class="flex space-x-6 h-10 items-center text-sm">
            <a href="/manager/pis" class="{% if active_manager == 'pis' %}text-indigo-600 font-semibold{% else %}text-gray-500 hover:text-gray-700{% endif %}">PIs</a>
        </div>
    </div>
</div>
{% endif %}
```

- [ ] **Step 6: Run the manager tests and the reachability gate**

```bash
.venv-test/bin/python -m pytest tests/integration/test_manager_views.py \
  tests/unit/test_reachability.py -v
```
Expected: PASS. If `test_no_unreachable_routes` fails naming `/manager` or `/manager/pis`, a nav link is missing or misspelled; if `test_template_links_resolve_to_a_real_route` fails, a copied `/admin/...` URL was left behind in a manager template.

- [ ] **Step 7: Run the full suite**

Run: `.venv-test/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/routers/manager.py src/main.py templates/manager/ templates/base.html \
        tests/integration/test_manager_views.py
git commit -m "feat(web): /manager router and PI directory, deny-by-default

Router-level get_staff_user, so unlike /admin (34 per-handler gates) a
route added here cannot be left un-gated. /manager/pis/{id} 404s on a
non-PI account so the admin roster is not enumerable by UUID, and the
manager templates drop the impersonate widget and Delete User button
that the admin ones carry."
```

---

## Task 5: Manager assessments page

**Files:**
- Create: `templates/admin/_assessments_body.html`, `templates/manager/assessments.html`
- Modify: `templates/admin/assessments.html` (body → include), `templates/base.html` (Assessments tab), `src/routers/manager.py` (route)
- Test: `tests/integration/test_manager_views.py` (append)

**Interfaces:**
- Consumes: `list_assessments` (Task 3), `_template_context` (Task 4).
- Produces: `GET /manager/assessments`, accepting `?run_id=<uuid>|all`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_manager_views.py`:

```python
async def test_manager_can_read_assessments(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get("/manager/assessments", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    assert "Opportunity Assessments" in r.text


async def test_pi_is_denied_assessments(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.get(
        "/manager/assessments", headers=auth_headers(pi.id), follow_redirects=False
    )
    assert r.status_code == 403


async def test_manager_assessments_never_links_into_admin(client, db_session):
    """A live-looking control that 403s on click is worse than no control (F6)."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = (await client.get("/manager/assessments", headers=auth_headers(mgr.id))).text
    assert "/admin/" not in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_views.py -k assessments -v`
Expected: FAIL — 404 from the manager router, not 200.

- [ ] **Step 3: Extract the shared body partial**

`templates/admin/assessments.html` has exactly two absolute links, both in the header: the run-selector `action="/admin/assessments"` (line 8) and `href="/admin/assessments?run_id=all"` (line 36). Everything from the gating legend (line 43) to the end of the block (line 281) is link-free.

Create `templates/admin/_assessments_body.html` containing **lines 43-281 of the current `templates/admin/assessments.html`, moved verbatim** (the gating legend, the drops banner, the four summary cards, and the table). Leading comment:

```html
{# Shared assessments body: gating legend, dropped-verdict banner, summary
   cards and the verdict table. Rendered by both templates/admin/assessments.html
   and templates/manager/assessments.html.

   MUST stay free of absolute /admin/... or /manager/... URLs. Links live in the
   two wrappers, as literal strings, because tests/unit/test_reachability.py's
   _link_credits only accepts a Jinja expression in a {path_param} slot — a
   templated base path would leave the manager routes unreferenced and fail the
   gate. Verify with: grep -n '/admin/\|/manager/' templates/admin/_assessments_body.html #}
```

In `templates/admin/assessments.html`, replace lines 43-281 with:

```html
{% include "admin/_assessments_body.html" %}
```

- [ ] **Step 4: Create the manager wrapper**

`templates/manager/assessments.html` — the header from `admin/assessments.html:1-42` with both URLs pointed at `/manager/assessments`, then the include:

```html
{% extends "base.html" %}
{% block title %}Manager — Assessments — CoPI{% endblock %}

{% block content %}
<div class="flex items-center justify-between mb-2 gap-4">
    <h1 class="text-2xl font-bold text-gray-900">Opportunity Assessments</h1>
    {% if runs %}
    <form method="GET" action="/manager/assessments" class="flex items-center gap-2">
        <label class="text-sm text-gray-500">Run:</label>
        <select name="run_id" onchange="this.form.submit()"
                class="text-sm border-gray-300 rounded-md shadow-sm focus:ring-indigo-500 focus:border-indigo-500">
            {% for run in runs %}
            <option value="{{ run.id }}" {% if not show_all_runs and run.id == selected_run_id %}selected{% endif %}>
                {{ run.started_at.strftime('%b %d %H:%M') }} ({{ run.status }}){% if run.id == runs[0].id %} — current{% endif %}
            </option>
            {% endfor %}
            <option value="all" {% if show_all_runs %}selected{% endif %}>All Runs</option>
        </select>
    </form>
    {% endif %}
</div>
<p class="text-sm text-gray-500 mb-2">
    BlackbirdBot's screening verdicts against the Blackbird investment rubric.
    Weighted score is computed from the nine dimension scores — not taken from the
    model. Bands: &ge;4.0 advance, 3.0&ndash;3.9 conditional, &lt;3.0 pass.
    <strong>Recommendation</strong> is the model's own judgement (it can also say
    <em>route-to-incubation</em>, which the computed band never does); the two
    can legitimately disagree.
</p>
<p class="text-xs text-gray-400 mb-2">
    {% if show_all_runs %}
        Showing every run on record.
    {% else %}
        Showing the current run only — assessments from earlier runs still exist
        and are never deleted; pick a run above or
        <a class="underline" href="/manager/assessments?run_id=all">view all runs</a>.
    {% endif %}
    {% if total_count > assessments_limit %}
        Displaying the top {{ assessments_limit }} of {{ total_count }} matching
        assessments by score; the rest are lower-scoring, not hidden by run.
    {% endif %}
</p>
{% include "admin/_assessments_body.html" %}
{% endblock %}
```

- [ ] **Step 5: Add the route**

In `src/routers/manager.py`, add the import `from src.services.directory import list_assessments` and:

```python
@router.get("/assessments", response_class=HTMLResponse)
async def manager_assessments(
    request: Request,
    run_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_staff_user),
):
    """BlackbirdBot's screening verdicts. Same data and same run-scoping as
    /admin/assessments; read-only, and it has no export path."""
    view = await list_assessments(db, run_id)
    return templates.TemplateResponse(
        request,
        "manager/assessments.html",
        _template_context(request, current_user, active_manager="assessments", **view),
    )
```

- [ ] **Step 6: Add the sub-nav tab**

In the manager sub-nav block in `templates/base.html`, after the PIs link:

```html
            <a href="/manager/assessments" class="{% if active_manager == 'assessments' %}text-indigo-600 font-semibold{% else %}text-gray-500 hover:text-gray-700{% endif %}">Assessments</a>
```

- [ ] **Step 7: Verify the partial carries no absolute links, then run the tests**

```bash
grep -n '/admin/\|/manager/' templates/admin/_assessments_body.html   # expect: no output
.venv-test/bin/python -m pytest tests/integration/test_manager_views.py \
  tests/integration/test_opportunity_assessment_persistence.py \
  tests/unit/test_reachability.py -q
```
Expected: PASS. The assessments persistence test is included because it renders `/admin/assessments`, which now goes through the extracted partial.

- [ ] **Step 8: Commit**

```bash
git add templates/admin/assessments.html templates/admin/_assessments_body.html \
        templates/manager/assessments.html templates/base.html \
        src/routers/manager.py tests/integration/test_manager_views.py
git commit -m "feat(web): manager assessments view over a shared body partial

The ~240-line verdict table is included by both wrappers; the two
absolute links stay in the wrappers as literal strings, because the
reachability gate only credits a route from a literal link."
```

---

## Task 6: Manager discussions and activity

The largest task. Tasks 1-5 already deliver the surface you originally asked for (PI profiles + assessments); this adds the agent-activity half.

**Files:**
- Create: `templates/admin/_discussions_threads.html`, `templates/admin/_run_detail_body.html`, `templates/manager/discussions.html`, `templates/manager/activity.html`, `templates/manager/activity_detail.html`
- Modify: `templates/admin/discussions.html`, `templates/admin/activity_detail.html`, `templates/base.html`, `src/routers/manager.py`
- Test: `tests/integration/test_manager_views.py` (append)

**Interfaces:**
- Consumes: `build_discussions_view`, `list_runs_overview`, `build_run_detail` (Task 3).
- Produces: `GET /manager/discussions`, `GET /manager/activity`, `GET /manager/activity/{run_id}`. **No** `/manager/activity/{run_id}/llm-calls`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_manager_views.py`:

```python
async def test_manager_can_read_discussions_and_activity(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    for path in ("/manager/discussions", "/manager/activity"):
        r = await client.get(path, headers=auth_headers(mgr.id))
        assert r.status_code == 200, path


async def test_manager_has_no_llm_calls_route(client, db_session):
    """D10: those rows carry full system prompts and raw model output."""
    import uuid

    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    run = uuid.uuid4()
    r = await client.get(
        f"/manager/activity/{run}/llm-calls",
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 404
    r2 = await client.get(
        f"/admin/activity/{run}/llm-calls",
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r2.status_code == 403


async def test_manager_activity_detail_404s_on_an_unknown_run(client, db_session):
    import uuid

    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get(
        f"/manager/activity/{uuid.uuid4()}", headers=auth_headers(mgr.id)
    )
    assert r.status_code == 404


async def test_manager_discussions_has_no_export_control(client, db_session):
    """Export stays admin-only; a manager page must not offer it."""
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = (await client.get("/manager/discussions", headers=auth_headers(mgr.id))).text
    assert "export" not in body.lower()
    assert "/admin/" not in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_views.py -k "discussions or activity or llm" -v`
Expected: FAIL — 404s where 200s are expected.

- [ ] **Step 3: Extract the two partials**

Re-derive the split points rather than trusting line numbers — the rule is *split immediately after the last absolute link*:

```bash
grep -n 'href=\|action=\|location\.href\|fetch(' templates/admin/discussions.html
grep -n 'href=\|action=\|location\.href\|fetch(' templates/admin/activity_detail.html
```

- `templates/admin/discussions.html` — its four links sit at lines 27, 57, 68 and 96 (the run selector, the status pills, the agent filter form, and Clear). Move everything **after the last link** — the thread list, currently lines ~100-195 — into `templates/admin/_discussions_threads.html` and `{% include %}` it.
- `templates/admin/activity_detail.html` — its links are line 8 (`/admin/activity`) and line 20 (`/admin/activity/{{ run.id }}/llm-calls`). Move everything after line 20 into `templates/admin/_run_detail_body.html` and `{% include %}` it.

Give both partials the same leading comment as Task 5's, with the filename changed. Then verify:

```bash
grep -n '/admin/\|/manager/' templates/admin/_discussions_threads.html \
                             templates/admin/_run_detail_body.html
```
Expected: no output.

- [ ] **Step 4: Create the three manager templates**

- `templates/manager/discussions.html` — the filter UI from `admin/discussions.html:1-99` with all four URLs rewritten to `/manager/discussions`, **the export control removed**, then `{% include "admin/_discussions_threads.html" %}`.
- `templates/manager/activity.html` — a straight copy of `admin/activity.html` (73 lines; no partial, it is too small to be worth one) with line 47's `onclick` changed to `location.href='/manager/activity/{{ run.id }}'` and the title block changed to `Manager — Activity`.
- `templates/manager/activity_detail.html` — the header from `admin/activity_detail.html:1-24` with the back link pointing at `/manager/activity` and **the llm-calls link deleted** (D10 — it would render live and 403 on click), then `{% include "admin/_run_detail_body.html" %}`.

- [ ] **Step 5: Add the three routes**

In `src/routers/manager.py`, extend the service import to include `build_discussions_view, build_run_detail, list_runs_overview` and add:

```python
@router.get("/discussions", response_class=HTMLResponse)
async def manager_discussions(
    request: Request,
    run_id: str | None = None,
    channel_filter: str | None = None,
    status_filter: str | None = None,
    agent_filter: list[str] = Query(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_staff_user),
):
    """Thread-level view of what each lab's bot did.

    Carries no `export` parameter: the export branch is admin-only, and this
    router is strictly read-only-and-render (D12).

    Per D5 this includes threads from collab_private channels. That is a
    deliberate policy decision recorded in the spec, not an oversight — no
    visibility filter exists anywhere in this code path (F12).
    """
    view = await build_discussions_view(
        db,
        run_id=run_id,
        channel_filter=channel_filter,
        status_filter=status_filter,
        agent_filter=agent_filter,
    )
    return templates.TemplateResponse(
        request,
        "manager/discussions.html",
        _template_context(request, current_user, active_manager="discussions", **view),
    )


@router.get("/activity", response_class=HTMLResponse)
async def manager_activity(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_staff_user),
):
    """Simulation-run overview."""
    view = await list_runs_overview(db)
    return templates.TemplateResponse(
        request,
        "manager/activity.html",
        _template_context(request, current_user, active_manager="activity", **view),
    )


@router.get("/activity/{run_id}", response_class=HTMLResponse)
async def manager_activity_detail(
    run_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_staff_user),
):
    """One run's per-agent and per-channel stats. There is deliberately no
    llm-calls drill-down here (D10)."""
    view = await build_run_detail(db, run_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return templates.TemplateResponse(
        request,
        "manager/activity_detail.html",
        _template_context(request, current_user, active_manager="activity", **view),
    )
```

Add `Query` to the `fastapi` import line.

- [ ] **Step 6: Add the last two sub-nav tabs**

In the manager sub-nav block in `templates/base.html`, after the Assessments link:

```html
            <a href="/manager/discussions" class="{% if active_manager == 'discussions' %}text-indigo-600 font-semibold{% else %}text-gray-500 hover:text-gray-700{% endif %}">Discussions</a>
            <a href="/manager/activity" class="{% if active_manager == 'activity' %}text-indigo-600 font-semibold{% else %}text-gray-500 hover:text-gray-700{% endif %}">Activity</a>
```

- [ ] **Step 7: Run the tests**

```bash
.venv-test/bin/python -m pytest tests/integration/test_manager_views.py \
  tests/characterization/test_auth_and_admin_routes.py \
  tests/unit/test_reachability.py -q
```
Expected: PASS. The characterization file is included because it pins `/admin/discussions` and `/admin/activity/{run_id}`, both of which now render through the new partials.

- [ ] **Step 8: Run the full suite and commit**

```bash
.venv-test/bin/python -m pytest tests/ -q
git add templates/admin/discussions.html templates/admin/activity_detail.html \
        templates/admin/_discussions_threads.html templates/admin/_run_detail_body.html \
        templates/manager/ templates/base.html src/routers/manager.py \
        tests/integration/test_manager_views.py
git commit -m "feat(web): manager discussions and activity views

No llm-calls drill-down and no export control: those rows carry full
system prompts and raw model output, and the manager router has no
non-render path. Per D5 private-channel threads ARE included."
```

---

## Task 7: Manager ≠ PI plumbing

Without this a manager logs in, gets redirected into PI onboarding, and `src/routers/onboarding.py:75` enqueues a `generate_profile` job against someone with no relevant publications (F8).

**Files:**
- Modify: `src/routers/auth.py:295`, `src/routers/auth.py:300`
- Modify: `src/routers/onboarding.py:55`, `src/routers/onboarding.py:75`
- Modify: `templates/base.html:52-68`
- Test: `tests/integration/test_manager_onboarding.py`

**Interfaces:**
- Consumes: `USER_ROLE_PI`, `User.is_staff`.
- Produces: no new symbols; behavioural change only.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_manager_onboarding.py`:

```python
"""A manager is not a PI (D7): no onboarding, no profile pipeline, no PI nav."""

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_MANAGER, USER_ROLE_PI, Job
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_manager_visiting_onboarding_is_bounced_to_the_manager_view(
    client, db_session
):
    mgr = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, onboarding_complete=False
    )
    r = await client.get(
        "/onboarding", headers=auth_headers(mgr.id), follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/pis"


async def test_manager_visiting_onboarding_enqueues_no_profile_job(client, db_session):
    """F8: onboarding.py:75 self-heals by enqueuing generate_profile for any
    allowed user with no profile. A manager must not trip it."""
    mgr = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, onboarding_complete=False
    )
    await client.get("/onboarding", headers=auth_headers(mgr.id), follow_redirects=False)
    count = await db_session.scalar(
        select(func.count(Job.id)).where(
            Job.user_id == mgr.id, Job.type == "generate_profile"
        )
    )
    assert count == 0


async def test_pi_visiting_onboarding_still_gets_the_self_heal(client, db_session):
    """The guard must narrow the self-heal, not delete it."""
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, onboarding_complete=False
    )
    await client.get("/onboarding", headers=auth_headers(pi.id), follow_redirects=False)
    count = await db_session.scalar(
        select(func.count(Job.id)).where(
            Job.user_id == pi.id, Job.type == "generate_profile"
        )
    )
    assert count == 1


async def test_manager_profile_url_bounce_terminates(client, db_session):
    """manager -> /profile -> /onboarding -> /manager/pis, with no loop."""
    mgr = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, onboarding_complete=False
    )
    r = await client.get("/profile", headers=auth_headers(mgr.id), follow_redirects=True)
    assert r.status_code == 200
    assert str(r.url).endswith("/manager/pis")


async def test_manager_nav_hides_the_pi_surfaces(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = (await client.get("/manager/pis", headers=auth_headers(mgr.id))).text
    assert "My Agent" not in body
    assert "My Profile" not in body
    assert "Settings" in body       # email preferences stay available to everyone
    assert "Manager" in body


async def test_pi_nav_is_unchanged(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    body = (await client.get("/profile", headers=auth_headers(pi.id))).text
    assert "My Agent" in body
    assert "My Profile" in body
    assert "Manager" not in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_onboarding.py -v`
Expected: FAIL — the bounce test gets `200` (the onboarding page renders) and the job-count test finds `1`.

- [ ] **Step 3: Narrow the onboarding redirect in `auth.py`**

At `src/routers/auth.py:295`:

```python
    # New PIs go through onboarding first; the saved destination is left in the
    # session and consumed when onboarding completes. Managers and admins are
    # not PIs (D7) and have no research profile to review, so they skip it —
    # without this a manager is sent to a page whose only exit is saving a
    # research profile (onboarding.py:193 is the sole write of
    # onboarding_complete in src/).
    if not user.onboarding_complete and user.user_role == USER_ROLE_PI:
        return RedirectResponse(url="/onboarding", status_code=302)

    # Resume the page the user originally requested, if any.
    next_url = pop_post_login_redirect(request)
    if next_url:
        return RedirectResponse(url=next_url, status_code=302)
    if user.is_manager:
        return RedirectResponse(url="/manager/pis", status_code=302)
    return RedirectResponse(url="/profile", status_code=302)
```

Add `USER_ROLE_PI` to the `src.models` import at line 18.

- [ ] **Step 4: Guard the onboarding page and its self-heal**

At `src/routers/onboarding.py:55`:

```python
    if current_user.onboarding_complete:
        return RedirectResponse(url="/profile", status_code=302)
    # Managers and admins have no research profile to review (D7). Bounce them
    # rather than render a PI page they can never complete.
    if current_user.user_role != USER_ROLE_PI:
        return RedirectResponse(url="/manager/pis", status_code=302)
```

At `src/routers/onboarding.py:75`, extend the self-heal condition:

```python
    if (
        job is None
        and profile is None
        and current_user.access_status == "allowed"
        and current_user.user_role == USER_ROLE_PI
    ):
```

Add `USER_ROLE_PI` to the `src.models` import at line 13.

- [ ] **Step 5: Gate the PI nav**

In `templates/base.html`, wrap the My Profile link (lines 52-55) and the My Agent link (lines 60-68) in:

```html
                    {% if current_user.user_role == 'pi' or current_user.is_admin %}
                    ...
                    {% endif %}
```

Leave the Settings link (lines 56-59) outside the guard — it is email notification preferences, which a manager still needs.

- [ ] **Step 6: Run the tests**

```bash
.venv-test/bin/python -m pytest tests/integration/test_manager_onboarding.py \
  tests/integration/test_onboarding_flow.py \
  tests/characterization/test_auth_and_admin_routes.py -q
```
Expected: PASS. `test_onboarding_flow.py` is included because it exercises the PI path these guards narrow.

- [ ] **Step 7: Run the full suite and commit**

```bash
.venv-test/bin/python -m pytest tests/ -q
git add src/routers/auth.py src/routers/onboarding.py templates/base.html \
        tests/integration/test_manager_onboarding.py
git commit -m "fix(web): keep non-PI accounts out of the PI onboarding pipeline

A manager logging in was redirected to /onboarding, whose self-heal
enqueues generate_profile for any allowed user with no profile, and
whose only exit is saving a research profile. Managers now skip it and
land on /manager/pis; the PI self-heal is narrowed, not removed."
```

---

## Task 8: Role appointment — admin UI and CLI

**Files:**
- Modify: `src/routers/admin.py` (new `POST /users/{user_id}/role` near `admin_delete_user`, line 198)
- Modify: `templates/admin/user_detail.html:36-39`
- Modify: `src/cli.py` (add `role:set`)
- Test: `tests/integration/test_role_appointment.py`

**Interfaces:**
- Consumes: `VALID_USER_ROLES`, `USER_ROLE_ADMIN`.
- Produces: `POST /admin/users/{user_id}/role` taking `user_role: str = Form(...)`, redirecting to `/admin/users/{user_id}`; CLI `role:set --orcid <id> --role <pi|manager|admin>`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_role_appointment.py`:

```python
"""Admin-UI role appointment, and the guards that keep it from locking
everyone out of /admin."""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _role_of(db_session, user_id) -> str:
    return await db_session.scalar(select(User.user_role).where(User.id == user_id))


async def test_admin_can_promote_a_pi_to_manager(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        f"/admin/users/{pi.id}/role",
        data={"user_role": USER_ROLE_MANAGER},
        headers=auth_headers(admin.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _role_of(db_session, pi.id) == USER_ROLE_MANAGER


async def test_a_manager_cannot_appoint_anyone(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        f"/admin/users/{pi.id}/role",
        data={"user_role": USER_ROLE_ADMIN},
        headers=auth_headers(mgr.id),
    )
    assert r.status_code == 403
    assert await _role_of(db_session, pi.id) == USER_ROLE_PI


async def test_an_invalid_role_is_rejected(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(
        f"/admin/users/{pi.id}/role",
        data={"user_role": "superuser"},
        headers=auth_headers(admin.id),
    )
    assert r.status_code == 400
    assert await _role_of(db_session, pi.id) == USER_ROLE_PI


async def test_an_admin_cannot_change_their_own_role(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/users/{admin.id}/role",
        data={"user_role": USER_ROLE_PI},
        headers=auth_headers(admin.id),
    )
    assert r.status_code == 400
    assert await _role_of(db_session, admin.id) == USER_ROLE_ADMIN


async def test_an_admin_can_demote_another_admin_when_two_exist(client, db_session):
    a1 = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    a2 = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/users/{a2.id}/role",
        data={"user_role": USER_ROLE_PI},
        headers=auth_headers(a1.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _role_of(db_session, a2.id) == USER_ROLE_PI


async def test_the_last_admin_guard_fires_when_the_target_is_the_only_admin(db_session):
    """Defense in depth, exercised by calling the handler directly.

    It is UNREACHABLE over HTTP while the self-change guard stands, and that is
    not an accident: demoting the last admin X requires an actor with admin
    rights who is not X, and if X is the last admin no such actor exists. The
    self-change guard is therefore what actually prevents lockout today.

    Do not delete this guard as dead code, and do not "fix" this test into an
    HTTP one — it is the invariant's backstop if the self-change guard is ever
    relaxed, and the CLI (`role:set`, which has no guards by design) is the
    recovery path if lockout happens anyway.
    """
    from fastapi import HTTPException

    from src.routers.admin import admin_set_user_role

    sole_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    with pytest.raises(HTTPException) as exc:
        await admin_set_user_role(
            user_id=sole_admin.id,
            request=None,          # the handler never reads it
            user_role=USER_ROLE_PI,
            db=db_session,
            current_user=actor,
        )
    assert exc.value.status_code == 400
    assert "last remaining admin" in exc.value.detail
    assert await _role_of(db_session, sole_admin.id) == USER_ROLE_ADMIN


async def test_user_detail_shows_the_role_and_no_admin_yes_no_row(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = (
        await client.get(f"/admin/users/{mgr.id}", headers=auth_headers(admin.id))
    ).text
    assert "manager" in body
    assert "Role" in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_role_appointment.py -v`
Expected: FAIL — 405/404, the route does not exist.

- [ ] **Step 3: Add the endpoint**

In `src/routers/admin.py`, after `admin_delete_user` (line 217), add — and add `USER_ROLE_ADMIN`, `VALID_USER_ROLES` to the `src.models` import block:

```python
@router.post("/users/{user_id}/role")
async def admin_set_user_role(
    user_id: uuid.UUID,
    request: Request,
    user_role: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Set a user's account type (admin only).

    Named for users, not agents: POST /agents/{agent_id}/role already exists
    and sets a BOT role (pi_lab / scout_hub), which is a different thing.
    """
    if user_role not in VALID_USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {user_role}")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Mirrors the self-delete guard above: an admin editing their own row is
    # how you lose your own access mid-session.
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    # Demoting the last admin locks every human out of /admin; the only way
    # back is `python -m src.cli admin:grant` from a container shell.
    if user.user_role == USER_ROLE_ADMIN and user_role != USER_ROLE_ADMIN:
        admin_count = await db.scalar(
            select(func.count(User.id)).where(User.user_role == USER_ROLE_ADMIN)
        )
        if (admin_count or 0) <= 1:
            raise HTTPException(
                status_code=400, detail="Cannot demote the last remaining admin"
            )

    previous = user.user_role
    user.user_role = user_role
    await db.commit()
    logger.info(
        "Admin %s changed role of %s (%s) from %s to %s",
        current_user.name, user.name, user_id, previous, user_role,
    )
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=302)
```

- [ ] **Step 4: Replace the `Admin: Yes/No` row with a Role row and form**

In `templates/admin/user_detail.html`, replace lines 36-39 with:

```html
            <div>
                <dt class="text-gray-500">Role</dt>
                <dd class="font-medium">{{ target_user.user_role }}</dd>
            </div>
```

and add a role form after the Account `</div>` (line 53), before the profile block:

```html
    <!-- Account type -->
    <div class="bg-white rounded-xl shadow-sm border border-gray-200 p-6 mb-6">
        <h2 class="font-semibold text-gray-800 mb-2">Account Type</h2>
        <p class="text-sm text-gray-600 mb-4">
            <strong>PI</strong> — own profile and lab agent.
            <strong>Manager</strong> — read-only view of every PI, the assessments
            queue and agent activity; no lab of their own, and cannot impersonate.
            <strong>Admin</strong> — full access.
        </p>
        {% if target_user.id != current_user.id %}
        <form method="POST" action="/admin/users/{{ target_user.id }}/role" class="flex gap-2 items-center">
            <select name="user_role" class="border border-gray-300 rounded px-3 py-2 text-sm">
                {% for role in valid_user_roles %}
                <option value="{{ role }}" {% if target_user.user_role == role %}selected{% endif %}>{{ role }}</option>
                {% endfor %}
            </select>
            <button type="submit"
                    class="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700">
                Save Role
            </button>
        </form>
        {% else %}
        <p class="text-sm text-gray-400">You cannot change your own role.</p>
        {% endif %}
    </div>
```

Pass the choices from `admin_user_detail` by adding `valid_user_roles=VALID_USER_ROLES` to its `_template_context(...)` call.

- [ ] **Step 5: Add the CLI command**

In `src/cli.py`, after `admin_revoke`, add:

```python
@app.command(name="role:set")
def role_set(
    orcid: str = typer.Option(..., "--orcid", help="ORCID ID of the account"),
    role: str = typer.Option(..., "--role", help="pi | manager | admin"),
):
    """Set a user's account type. The escape hatch when no admin can log in."""
    async def _set() -> bool:
        from sqlalchemy import select

        from src.models import VALID_USER_ROLES, User
        if role not in VALID_USER_ROLES:
            console.print(f"[red]Invalid role {role!r}; expected one of {VALID_USER_ROLES}[/red]")
            return False
        engine, factory = await _get_db()
        try:
            async with factory() as db:
                result = await db.execute(select(User).where(User.orcid == orcid))
                user = result.scalar_one_or_none()
                if not user:
                    console.print(f"[red]User with ORCID {orcid} not found[/red]")
                    return False
                user.user_role = role
                await db.commit()
                console.print(f"[green]Set {user.name} ({orcid}) to {role}[/green]")
                return True
        finally:
            await engine.dispose()

    if not _run(_set()):
        raise typer.Exit(1)
```

- [ ] **Step 6: Run the tests**

```bash
.venv-test/bin/python -m pytest tests/integration/test_role_appointment.py \
  tests/integration/test_cli.py tests/unit/test_reachability.py -q
```
Expected: PASS. Reachability is included because `POST /admin/users/{user_id}/role` is a new route and now needs the form that references it.

- [ ] **Step 7: Run the full suite and commit**

```bash
.venv-test/bin/python -m pytest tests/ -q
git add src/routers/admin.py templates/admin/user_detail.html src/cli.py \
        tests/integration/test_role_appointment.py
git commit -m "feat(web): admin UI + CLI for setting a user's account type

Guards: no self-change, no invalid value, and no demoting the last
remaining admin — that one click would otherwise lock every human out
of /admin with only a container shell to recover. The CLI is kept
deliberately as that escape hatch."
```

---

## Task 9: Documentation and the full gate

**Files:**
- Modify: `CLAUDE.md` (new section after "Adding New PIs")
- Modify: `docs/specs/2026-08-17-user-account-types-design.md` (status line)

- [ ] **Step 1: Document the account types in `CLAUDE.md`**

Add after the "Adding New PIs" section:

```markdown
## Account Types (PI / manager / admin)

**`users.user_role` is the single source of truth**, with values `pi`, `manager`,
`admin`. There is no `is_admin` column any more — `User.is_admin` is a read-only
`hybrid_property` over `user_role`, so it still works in both SQL
(`select(User.is_admin)`) and Python, but **cannot be assigned**. Set the role
instead.

- **PI** — the original account: own profile, own lab agent, `/profile` and `/agent`.
- **Manager** — global, strictly **read-only**: `/manager/pis`, `/manager/assessments`,
  `/manager/discussions`, `/manager/activity`. No lab, no PI onboarding, **cannot
  impersonate**, and there is deliberately no LLM-call drill-down and no export.
  Managers *do* see private (`collab_private`) discussion threads — a policy decision,
  recorded in the design doc.
- **Admin** — everything, including `/admin/*` and impersonation.

`is_manager` means exactly `user_role == 'manager'`. The "may see the manager views"
predicate is **`is_staff`** (admin OR manager). **Never widen `is_admin`** — impersonation
(`src/dependencies.py`, and a duplicate check in `src/main.py`) is gated on it and
returns a fully substituted user, so a manager satisfying `is_admin` would be a full
privilege escalation.

Appoint from **/admin/users/{id} → Account Type**. The last admin cannot be demoted
there. If no admin can log in at all, recover from a container shell:

    docker compose -f docker-compose.prod.yml exec blackbird-app \
      python -m src.cli role:set --orcid 0000-0000-0000-0000 --role admin

New managers are provisioned in two steps: they sign in with ORCID (landing on
`/access-pending`), an admin approves them at `/admin/access-requests`, then sets their
role. Between approval and role-setting the account behaves as a PI.
```

- [ ] **Step 2: Flip the spec's status line**

In `docs/specs/2026-08-17-user-account-types-design.md`, change
`**Status:** DESIGNED, not implemented. No code in this document has been written.`
to
`**Status:** IMPLEMENTED (Tasks 1-9) as of <date>. 0029 (the is_admin column drop) is NOT yet applied — see §8 and Task 10 of the plan.`

- [ ] **Step 3: Run the whole gate**

Run: `./scripts/ci.sh`

Expected: `==> CI passed.` Every step must pass, in particular:
- one Alembic head (`0028`) and a clean round trip through `downgrade 0018`
- ruff on `src/` **≤ 231** — if it rose, fix what you added; **do not raise `SRC_LINT_MAX`**
- coverage **≥ 60%**

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/specs/2026-08-17-user-account-types-design.md
git commit -m "docs: account types (pi/manager/admin), appointment and recovery"
```

---

## Task 10: Drop the `is_admin` column — A SEPARATE, LATER DEPLOY

**Do not include this in the same deploy as Tasks 1-9.** `0028` is additive so old code
keeps working against the new schema; this one is destructive, and applying it while any
process still reads `users.is_admin` breaks that process. Ship it only after `/admin`
and `/manager` are confirmed working in production on the Task 1-9 code.

**Files:**
- Create: `alembic/versions/0029_drop_is_admin.py`

- [ ] **Step 1: Confirm the precondition**

```bash
grep -rn "is_admin" src/ --include=*.py | grep -v __pycache__
```
Expected: only the `hybrid_property` definitions in `src/models/user.py`. Any other hit
means something still reads the column — stop and fix that first.

- [ ] **Step 2: Write the migration**

```python
"""Drop the orphaned users.is_admin column

Revision ID: 0029
Revises: 0028
Create Date: <fill in at execution time>

0028 replaced this column with users.user_role and left it in place, unmapped
and defaulted, so that migration could be applied before the new code shipped
without breaking the running container. Nothing reads it now.

DESTRUCTIVE. The downgrade restores the column and its values from user_role,
but a downgrade past 0028 afterwards is the only way back to the old code.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("users", "is_admin")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.execute("UPDATE users SET is_admin = (user_role = 'admin')")
```

- [ ] **Step 3: Round-trip it and run the gate**

Run the Step 6 block from Task 1 (it upgrades to `head`, which is now `0029`), then
`./scripts/ci.sh`.
Expected: one head (`0029`); round trip clean; `CI passed.`

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/0029_drop_is_admin.py
git commit -m "chore(db): drop the orphaned users.is_admin column

Separate from 0028 on purpose: that one had to be applyable before the
new code shipped, which meant leaving this column in place. Deploy this
only after the user_role code is confirmed live."
```

---

## Self-Review

**Spec coverage.** §2 data model → Task 1. §3 auth/routing → Tasks 2, 4, 5, 6. §4 service
extraction → Task 3. §5 manager≠PI → Task 7. §6 appointment UI + CLI + bootstrap → Task 8
(bootstrap documented in Task 9). §7 testing → tests in every task. §8 deploy → Task 9
Step 3 and Task 10. §9 rejected alternatives → recorded as anti-instructions where an
executor might drift (the factory shim in Task 1 Step 7, the templated base path in
Task 5 Step 3). §10 out-of-scope items are deliberately absent: no export endpoint, no
annotations table, no read audit log, no visibility filter.

**Findings coverage.** F1/F2 → Task 1. F3 → the `user_role` name, Global Constraints.
F4 → no auth changes; ORCID reused. F5 → Task 4's router-level dependency, plus Task 3's
`git diff` check that no gate line moved. F6 → Task 4 Step 4 deletions plus the
`test_pi_detail_has_no_delete_or_impersonate_control` test. F7 → Task 1's
`test_is_admin_is_false_for_a_manager` and Task 4's two impersonation tests. F8 → Task 7.
F9 → Task 7 Step 5. F10 → Task 4 Step 5's sub-nav. F11 → not applicable under D2.
F12 → accepted under D5, documented in Task 6 Step 5 and Task 9. F13 → Task 1 Step 3's
hybrid. F14 → Task 1 Step 5's `alter_column`. F15 → Task 9 Step 3.

**Type consistency.** `list_pi_directory`, `load_user_detail`, `list_assessments`,
`build_discussions_view`, `list_runs_overview`, `build_run_detail` are declared once in
Task 3's Interfaces and called with those exact names and keyword arguments in Tasks 4,
5 and 6. `get_staff_user` is defined in Task 2 and consumed in Tasks 4-6. `USER_ROLE_PI`,
`USER_ROLE_MANAGER`, `USER_ROLE_ADMIN`, `VALID_USER_ROLES` are produced in Task 1 and
imported by name thereafter. `auth_headers` is defined once in
`tests/integration/test_manager_access.py` (Task 2) and imported by Tasks 4, 5, 6, 7, 8.
`_template_context(request, current_user, active_manager=..., **kwargs)` is defined in
Task 4 and used unchanged in Tasks 5 and 6, which is why the service functions return
dicts whose keys match the template variables exactly.

**Known sequencing traps, called out where they bite.** The manager sub-nav is built one
tab per task (Tasks 4, 5, 6) because the reachability gate rejects a link to a route that
does not exist. Task 1 must land every `is_admin=` call-site edit in the same commit or
the suite is red. Task 3 must not touch a decorator or a gate line, and has an explicit
`git diff` check for it.
