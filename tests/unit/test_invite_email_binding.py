"""SEC-6: delegate invite acceptance must be bound to the invited email."""

import uuid
from datetime import UTC, datetime, timedelta

from src.models.delegate import DelegateInvitation
from src.models.user import USER_ROLE_PI, USER_ROLE_REVIEWER, User
from src.routers.invite import (
    _INVITE_EMAIL_MISMATCH_MSG,
    _INVITE_NOT_PI_MSG,
    _INVITE_UNVERIFIED_MSG,
    _invite_matches_user,
    _invite_refusal,
)


def _inv(email: str) -> DelegateInvitation:
    return DelegateInvitation(
        agent_registry_id=uuid.uuid4(),
        invited_by_user_id=uuid.uuid4(),
        email=email,
        token="tok",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


def _user(email):
    return User(orcid="0000-0000-0000-0000", name="X", email=email)


def test_exact_match_accepts():
    assert _invite_matches_user(_inv("pi@scripps.edu"), _user("pi@scripps.edu")) is True


def test_case_insensitive_match_accepts():
    assert _invite_matches_user(_inv("PI@Scripps.edu"), _user("pi@scripps.EDU")) is True


def test_whitespace_tolerated():
    assert _invite_matches_user(_inv("pi@scripps.edu"), _user("  pi@scripps.edu ")) is True


def test_different_email_rejected():
    assert _invite_matches_user(_inv("pi@scripps.edu"), _user("attacker@evil.com")) is False


def test_missing_user_email_rejected():
    # Fail closed: a user with no email cannot be verified as the invitee.
    assert _invite_matches_user(_inv("pi@scripps.edu"), _user(None)) is False


def test_empty_invitation_email_rejected():
    assert _invite_matches_user(_inv(""), _user("pi@scripps.edu")) is False


def _account(email, *, role=USER_ROLE_PI, verified=True):
    return User(orcid="0000-0000-0000-0001", name="Y", email=email, user_role=role,
                email_verified_at=datetime.now(UTC) if verified else None)


def test_refusal_order_role_then_address_then_verification():
    inv = _inv("pi@scripps.edu")
    assert _invite_refusal(inv, _account("pi@scripps.edu")) is None
    assert _invite_refusal(inv, _account("PI@Scripps.edu")) is None
    assert _invite_refusal(inv, _account("pi@scripps.edu", verified=False)) == _INVITE_UNVERIFIED_MSG
    assert _invite_refusal(inv, _account("x@evil.com", verified=False)) == _INVITE_EMAIL_MISMATCH_MSG
    assert _invite_refusal(inv, _account("pi@scripps.edu", role=USER_ROLE_REVIEWER)) == _INVITE_NOT_PI_MSG
