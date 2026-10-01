"""RB-06 / PS-10 / PS-12: one transactional sender, called after commit; the welcome
email goes out once. Nothing here reaches SES: the sender is patched at its USE site,
or `_ses_client` is faked."""
import pytest

from src.services import email as email_svc
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_allowlist_and_thread(monkeypatch):
    sent = []

    class FakeSes:
        def send_email(self, **kw):
            sent.append(("send_email", kw["Destination"]["ToAddresses"]))

        def send_raw_email(self, **kw):
            sent.append(("send_raw_email", kw["Destinations"]))

    monkeypatch.setattr(email_svc, "_ses_client", lambda region: FakeSes())
    monkeypatch.setattr(email_svc, "is_allowed_recipient", lambda to: to == "ok@x.edu")
    assert await email_svc.send_transactional_email(email_svc.build_welcome("ok@x.edu", "Ok")) is True
    assert await email_svc.send_transactional_email(email_svc.build_welcome("no@x.edu", "No")) is False
    assert await email_svc.send_transactional_email(
        email_svc.build_delegate_invitation("ok@x.edu", "PI", "Bot", "https://x/invite/t")) is True
    assert sent == [("send_raw_email", ["ok@x.edu"]), ("send_email", ["ok@x.edu"])]


async def test_welcome_is_sent_once_on_concurrent_completion(client, db_session, monkeypatch):
    user = await factories.make_user(db_session, onboarding_complete=False, email="w@x.edu")
    await factories.make_profile(db_session, user=user, profile_version=1)
    sent = []

    async def fake_send(message, *, force=False):
        sent.append(message.kind)
        return True

    monkeypatch.setattr("src.routers.onboarding.send_transactional_email", fake_send)
    form = {"email": "w@x.edu", "research_summary": "s", "techniques": "", "experimental_models": "",
            "disease_areas": "", "key_targets": "", "keywords": "", "profile_version": "1"}
    await client.post("/onboarding/save-profile", data=form, headers=auth_headers(user.id), follow_redirects=False)
    form["profile_version"] = "2"
    await client.post("/onboarding/save-profile", data=form, headers=auth_headers(user.id), follow_redirects=False)
    assert sent == ["welcome"]


async def test_send_happens_after_commit(client, db_session, monkeypatch):
    """The invite row is committed before the email leaves."""
    pi = await factories.make_user(db_session, email="pi@x.edu")
    agent = await factories.make_agent(db_session, user=pi, status="active")
    order = []
    orig_commit = db_session.commit

    async def spy_commit():
        order.append("commit")
        await orig_commit()

    async def fake_send(message, *, force=False):
        order.append("send")
        return True

    monkeypatch.setattr(db_session, "commit", spy_commit)
    monkeypatch.setattr("src.routers.agent_page.send_transactional_email", fake_send)
    await client.post(f"/agent/{agent.agent_id}/delegates/invite", data={"emails": "new@x.edu"},
                      headers=auth_headers(pi.id), follow_redirects=False)
    assert "send" in order
    assert order.index("send") > order.index("commit")
