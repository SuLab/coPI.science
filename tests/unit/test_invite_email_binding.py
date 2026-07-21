"""SEC-6: delegate invite acceptance must be bound to the invited email."""

import uuid
from datetime import UTC, datetime, timedelta

from src.models.delegate import DelegateInvitation
from src.models.user import User
from src.routers.invite import _invite_matches_user


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
