# tests/unit/test_pi_surfaces.py
import pytest

from src.models import User
from src.models.user import USER_ROLE_ADMIN, USER_ROLE_PI, VALID_USER_ROLES


@pytest.mark.parametrize("role", VALID_USER_ROLES)
def test_rule_matches_todays_denylist_for_every_role(role):
    u = User(name="x", orcid="0", user_role=role)
    todays = not (u.is_manager or u.is_reviewer)
    assert u.may_use_pi_surfaces == todays
    assert u.may_use_pi_surfaces == (role in (USER_ROLE_PI, USER_ROLE_ADMIN))


def test_unknown_role_fails_closed():
    assert User(name="x", orcid="0", user_role="auditor").may_use_pi_surfaces is False


def test_sites_use_the_property():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    base = (root / "templates/base.html").read_text()
    assert "current_user.user_role == 'pi' or current_user.is_admin" not in base
    assert base.count("current_user.may_use_pi_surfaces") >= 2
    dep = (root / "src/dependencies.py").read_text()
    assert "if not current_user.may_use_pi_surfaces:" in dep
