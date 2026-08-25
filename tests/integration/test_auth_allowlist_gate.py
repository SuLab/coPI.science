"""The allowlist promotes PENDING users only (audit F6, decision D5)."""
import base64
import json

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import AccessAllowlist, User
from src.routers import auth as auth_module
from tests import factories

pytestmark = pytest.mark.asyncio


def _session_cookie(payload: dict) -> dict:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps(payload).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


def _fake_oauth(orcid: str):
    class _Client:
        async def fetch_token(self, url, code, grant_type):
            return {"orcid": orcid, "name": "Test User"}

    return _Client()


async def _callback(client, monkeypatch, orcid: str):
    monkeypatch.setattr(auth_module, "_get_oauth_client", lambda: _fake_oauth(orcid))

    async def _fake_profile(orcid_id):
        return {"orcid": orcid_id, "name": "Test User"}

    monkeypatch.setattr(auth_module, "fetch_orcid_profile", _fake_profile)
    return await client.get(
        "/auth/callback?code=c&state=s",
        headers=_session_cookie({"oauth_state": "s"}),
    )


async def test_denied_user_is_not_resurrected_by_allowlist(
    client, db_session, monkeypatch
):
    user = await factories.make_user(db_session, access_status="denied")
    db_session.add(AccessAllowlist(orcid=user.orcid))
    await db_session.flush()

    r = await _callback(client, monkeypatch, user.orcid)

    assert r.status_code == 302
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.access_status == "denied"


async def test_pending_user_is_still_promoted_by_allowlist(
    client, db_session, monkeypatch
):
    user = await factories.make_user(db_session, access_status="pending")
    db_session.add(AccessAllowlist(orcid=user.orcid))
    await db_session.flush()

    r = await _callback(client, monkeypatch, user.orcid)

    assert r.status_code == 302
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.access_status == "allowed"
