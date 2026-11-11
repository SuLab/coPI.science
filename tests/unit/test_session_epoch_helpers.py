"""Pure helpers behind session revocation and impersonation."""

from types import SimpleNamespace

import pytest

from src.dependencies import IMPERSONATE_COOKIE, account_of, impersonating
from src.services.session_epoch import current_epoch, epoch_in_session


def test_current_epoch_reads_null_as_zero():
    assert current_epoch(SimpleNamespace(session_epoch=None)) == 0
    assert current_epoch(SimpleNamespace(session_epoch=3)) == 3


def test_epoch_in_session_reads_a_missing_key_as_zero_and_an_int_as_itself():
    assert epoch_in_session({}) == 0
    assert epoch_in_session({"epoch": 5}) == 5


@pytest.mark.parametrize("value", ["0", True, None, 0.0])
def test_epoch_in_session_rejects_a_non_integer(value):
    assert epoch_in_session({"epoch": value}) is None


def _request(**cookies):
    return SimpleNamespace(cookies=cookies)


def test_impersonating_needs_an_admin_and_a_non_empty_cookie():
    admin = SimpleNamespace(is_admin=True)
    member = SimpleNamespace(is_admin=False)
    assert impersonating(_request(**{IMPERSONATE_COOKIE: "x"}), admin)
    assert impersonating(_request(**{IMPERSONATE_COOKIE: "not-a-uuid"}), admin)
    assert not impersonating(_request(), admin)
    assert not impersonating(_request(**{IMPERSONATE_COOKIE: ""}), admin)
    assert not impersonating(_request(**{IMPERSONATE_COOKIE: "x"}), member)


def test_account_of_returns_the_real_admin_when_impersonated():
    admin = SimpleNamespace(name="admin")
    target = SimpleNamespace(_is_impersonated=True, _real_admin=admin)
    assert account_of(target) is admin
    plain = SimpleNamespace()
    assert account_of(plain) is plain
