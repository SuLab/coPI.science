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
    USER_ROLE_REVIEWER,
    VALID_USER_ROLES,
    User,
)

# No pytestmark: this file touches no database. The repo registers only
# integration / characterization / contract / real_llm / live_slack / live_api
# in pyproject.toml — there is no `unit` marker, and most files in tests/unit
# carry none. `asyncio_mode = "auto"` means async tests need no marker either.


def _user(role: str) -> User:
    return User(name="X", orcid="0000-0000-0000-0001", user_role=role)


def test_valid_roles_are_exactly_the_four_account_types():
    assert VALID_USER_ROLES == (
        USER_ROLE_PI, USER_ROLE_MANAGER, USER_ROLE_ADMIN, USER_ROLE_REVIEWER,
    )


@pytest.mark.parametrize(
    "role,expect_admin,expect_manager,expect_staff,expect_reviewer",
    [
        (USER_ROLE_PI, False, False, False, False),
        (USER_ROLE_MANAGER, False, True, True, False),
        (USER_ROLE_ADMIN, True, False, True, False),
        (USER_ROLE_REVIEWER, False, False, False, True),
    ],
)
def test_predicates_in_python(role, expect_admin, expect_manager, expect_staff, expect_reviewer):
    u = _user(role)
    assert u.is_admin is expect_admin
    assert u.is_manager is expect_manager
    assert u.is_staff is expect_staff
    assert u.is_reviewer is expect_reviewer


def test_is_staff_excludes_reviewer_in_sql():
    sql = str(select(User).where(User.is_staff).compile(compile_kwargs={"literal_binds": True}))
    assert "'reviewer'" not in sql


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
    @property is invisible to SQL and that query would raise.

    Compile with literal_binds and assert on the actual predicate, not just on
    "user_role" appearing in the SQL: the escalation formulation
    `user_role.in_(("manager", "admin"))` also contains the string "user_role"
    and would pass a bare substring check while silently granting managers
    is_admin. Never weaken this back to `"user_role" in str(select(...))`.
    """
    sql = str(select(User.is_admin).compile(compile_kwargs={"literal_binds": True}))
    assert "users.user_role = 'admin'" in sql
    assert "manager" not in sql


def test_is_staff_compiles_to_a_sql_in_clause():
    """Same rationale as above: assert on the literal values inside the IN
    clause, not just that "user_role" appears, so a narrowed is_staff (e.g.
    admin-only) would fail this test instead of passing it vacuously."""
    sql = str(
        select(User).where(User.is_staff).compile(compile_kwargs={"literal_binds": True})
    )
    assert "'manager'" in sql
    assert "'admin'" in sql


# The default-role case is NOT tested here. A pre-flush `User()` has no
# user_role at all (the mapped default is applied by the INSERT, not the
# constructor), so the only assertion this file could make was
# `is None or == 'pi'` — which accepts both answers and therefore cannot fail.
# The real, DB-backed default is pinned in
# tests/integration/test_manager_access.py::test_default_role_is_pi_in_the_database,
# which flushes and reads the column back.
