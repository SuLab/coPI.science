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
