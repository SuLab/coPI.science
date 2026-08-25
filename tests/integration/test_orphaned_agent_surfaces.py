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
