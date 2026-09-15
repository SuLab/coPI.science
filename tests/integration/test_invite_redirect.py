"""A delegate invite opened by a signed-out visitor lands on the login explainer.

The redirect used to go to ``/login/start``, which 302s straight out to
orcid.org: someone who followed an invitation from their inbox met an external
consent screen before anything had told them what CoPI is. The token is stashed
in the session either way, so the extra hop is free — ``auth_callback`` pops it
and resumes at ``/invite/<token>``.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.models import DelegateInvitation
from tests import factories

pytestmark = pytest.mark.integration


async def _make_pending_invite(db) -> DelegateInvitation:
    pi = await factories.make_user(db)
    agent = await factories.make_agent(db, user=pi)
    invite = DelegateInvitation(
        agent_registry_id=agent.id,
        invited_by_user_id=pi.id,
        email="delegate@example.edu",
        token=uuid.uuid4().hex,
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(invite)
    await db.commit()
    return invite


async def test_signed_out_invite_redirects_to_the_login_explainer(client, db_session):
    invite = await _make_pending_invite(db_session)

    r = await client.get(f"/invite/{invite.token}", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/login", "the invite jumps straight out to ORCID"


async def test_signed_out_invite_still_stashes_the_token(client, db_session):
    """The extra hop must not cost the token — without it the visitor signs in
    and lands on their profile with the invitation silently dropped."""
    invite = await _make_pending_invite(db_session)

    r = await client.get(f"/invite/{invite.token}", follow_redirects=False)

    assert "set-cookie" in r.headers, "no session written, so the token is gone"
    assert "session=" in r.headers["set-cookie"]
