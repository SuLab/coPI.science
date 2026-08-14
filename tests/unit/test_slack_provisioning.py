"""Slack app provisioning: the manifest we submit, token rotation, and secret hygiene.

`test_admin_provisioning.py` has three tests covering the happy path of the admin
service. Nothing covered the manifest contents, `rotate_config_token`, `exchange_code`,
or the caching that keeps a single-use refresh token from being burned on every click.

The manifest test is the load-bearing one: a scope missing from `BOT_SCOPES` produces a
bot that provisions cleanly, connects cleanly, and then fails one specific API call at
runtime — and fixing it needs a manifest change *and* a manual reinstall of every bot.
"""

import time

import httpx
import pytest
from sqlalchemy import select

from src.models import AppSetting
from src.services.admin_provisioning import _config_token
from src.services.slack_provisioning import BOT_SCOPES, create_app, exchange_code

pytestmark = pytest.mark.integration


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --- the manifest -------------------------------------------------------------------

# Every Slack API method the codebase calls, and the bot scope it needs. Derived by
# grepping src/ for `client.<method>` — see the Surface Inventory in
# .notes/slack-integration-test-plan.md.
METHOD_SCOPES = {
    "auth.test": None,                                  # no scope required
    "chat.postMessage": "chat:write",
    "chat.delete": "chat:write",
    "conversations.list (public)": "channels:read",
    "conversations.list (private)": "groups:read",
    "conversations.create (public)": "channels:manage",
    "conversations.create (private)": "groups:write",
    "conversations.join": "channels:join",
    "conversations.invite (private)": "groups:write",
    "conversations.history (public)": "channels:history",
    "conversations.history (private)": "groups:history",
    "conversations.replies (public)": "channels:history",
    "conversations.open": "im:write",
    "conversations.history (dm)": "im:history",
    "users.info": "users:read",
    "users.lookupByEmail": "users:read.email",
}


def test_manifest_requests_every_scope_the_client_actually_needs():
    """The invariant that keeps provisioning honest.

    AgentSlackClient exposes create_private_channel() and invite_to_channel(), and
    private-channel migration calls both. Both need `groups:write`. A scope absent here
    is invisible until the one call that needs it fails at runtime with missing_scope,
    on a bot that otherwise looks perfectly healthy.
    """
    needed = {s for s in METHOD_SCOPES.values() if s}
    missing = sorted(needed - set(BOT_SCOPES))
    assert not missing, (
        f"BOT_SCOPES is missing {missing}. Methods that need them: "
        + ", ".join(m for m, s in METHOD_SCOPES.items() if s in missing)
    )


def test_method_scope_table_is_not_trivially_satisfiable():
    """Control for the table above: it must name scopes that are genuinely required,
    not an empty set. An empty table would make the invariant vacuous."""
    assert len({s for s in METHOD_SCOPES.values() if s}) >= 8


def test_create_app_manifest_shape(monkeypatch):
    captured = {}

    def _post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        captured["auth"] = kw.get("headers", {}).get("Authorization")
        return _Resp({"ok": True, "app_id": "A1",
                      "credentials": {"client_id": "cid", "client_secret": "csec"},
                      "oauth_authorize_url": "https://slack.com/oauth/v2/authorize?x=1"})

    monkeypatch.setattr(httpx, "post", _post)
    out = create_app("xoxe.xoxp-token", "su", "SuBot", "PI Su",
                     "https://example.test/admin/agents/slack/callback")

    assert captured["url"].endswith("/apps.manifest.create")
    assert captured["auth"] == "Bearer xoxe.xoxp-token"
    m = captured["json"]["manifest"]
    assert m["display_information"]["name"] == "SuBot"
    assert m["features"]["bot_user"]["display_name"] == "SuBot"
    assert m["oauth_config"]["redirect_urls"] == [
        "https://example.test/admin/agents/slack/callback"]
    assert set(m["oauth_config"]["scopes"]["bot"]) == set(BOT_SCOPES)
    # Socket Mode off is what makes the polling design correct; org deploy off keeps
    # the app single-workspace.
    assert m["settings"]["socket_mode_enabled"] is False
    assert m["settings"]["org_deploy_enabled"] is False
    assert out == {
        "agent_id": "su", "bot_name": "SuBot", "pi_name": "PI Su", "app_id": "A1",
        "client_id": "cid", "client_secret": "csec",
        "oauth_url": "https://slack.com/oauth/v2/authorize?x=1",
    }


def test_create_app_retries_only_on_rate_limit(monkeypatch):
    """Control included: a non-rate-limit error must raise on the FIRST call, so a
    create_app that retried everything would not pass both halves."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    calls = []

    def _post_ratelimited(url, **kw):
        calls.append(url)
        if len(calls) < 3:
            return _Resp({"ok": False, "error": "ratelimited", "retry_after": 1})
        return _Resp({"ok": True, "app_id": "A1",
                      "credentials": {"client_id": "c", "client_secret": "s"},
                      "oauth_authorize_url": "u"})

    monkeypatch.setattr(httpx, "post", _post_ratelimited)
    assert create_app("t", "su", "SuBot", "PI", "https://x/cb")["app_id"] == "A1"
    assert len(calls) == 3

    calls.clear()
    monkeypatch.setattr(httpx, "post",
                        lambda url, **kw: calls.append(url) or
                        _Resp({"ok": False, "error": "invalid_manifest"}))
    with pytest.raises(RuntimeError, match="invalid_manifest"):
        create_app("t", "su", "SuBot", "PI", "https://x/cb")
    assert len(calls) == 1


# --- secret hygiene -------------------------------------------------------------------


def test_exchange_code_never_echoes_the_token(monkeypatch):
    """SEC-9. This error string reaches the server log and a user-facing
    ?slack_error= redirect, so any fragment of the value is a leak."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"ok": True, "access_token": "xoxp-WRONGTYPE-abcdefghijklmnop"}))
    with pytest.raises(RuntimeError) as ei:
        exchange_code("cid", "csec", "code", "https://example.test/cb")
    msg = str(ei.value)
    for fragment in ("xoxp-WRONGTYPE", "abcdefghijklmnop", "WRONGTYPE"):
        assert fragment not in msg, f"the token leaked into the error: {msg!r}"
    assert msg.strip(), "control leg failed: the message is empty, which is unhelpful"


def test_exchange_code_returns_a_bot_token(monkeypatch):
    """Control for the test above: the happy path must actually work, or 'never echoes
    the token' is satisfied by a function that always raises."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"ok": True, "access_token": "xoxb-good-token"}))
    assert exchange_code("cid", "csec", "code", "https://x/cb") == "xoxb-good-token"


def test_exchange_code_surfaces_a_slack_error(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"ok": False, "error": "invalid_code"}))
    with pytest.raises(RuntimeError, match="invalid_code"):
        exchange_code("cid", "csec", "code", "https://x/cb")


# --- config-token rotation --------------------------------------------------------


async def test_rotation_persists_the_whole_triple(db_session, monkeypatch):
    """SEC-10. The refresh token just spent is dead; if only some of the three KV rows
    land, app-config access is lost with no way back to the old pair."""
    monkeypatch.setattr(
        "src.services.slack_provisioning.rotate_config_token",
        lambda refresh: ("xoxe.xoxp-NEW", "xoxe-1-NEWREFRESH", int(time.time()) + 43200),
    )
    monkeypatch.setattr("src.services.admin_provisioning.get_settings",
                        lambda: _settings_with(refresh="xoxe-1-SEED"))
    tok = await _config_token(db_session)
    assert tok == "xoxe.xoxp-NEW"
    rows = {r.key: r.value for r in
            (await db_session.execute(select(AppSetting))).scalars().all()}
    assert rows["slack_config_token"] == "xoxe.xoxp-NEW"
    assert rows["slack_config_refresh_token"] == "xoxe-1-NEWREFRESH"
    assert int(rows["slack_config_token_exp"]) > time.time()


async def test_a_cached_token_is_reused_and_does_not_rotate(db_session, monkeypatch):
    """Control for the test above, and the property that makes provisioning usable at
    all: rotation must be RARE. A _config_token that rotated on every call would satisfy
    the atomicity test while burning a single-use refresh token on every admin click —
    and a crash between Slack's rotate and our commit would strand the new pair.
    """
    calls = []
    monkeypatch.setattr(
        "src.services.slack_provisioning.rotate_config_token",
        lambda r: (calls.append(r) or
                   ("xoxe.xoxp-N", "xoxe-1-N", int(time.time()) + 43200)),
    )
    monkeypatch.setattr("src.services.admin_provisioning.get_settings",
                        lambda: _settings_with(refresh="xoxe-1-SEED"))
    first = await _config_token(db_session)
    second = await _config_token(db_session)
    assert first == second == "xoxe.xoxp-N"
    assert len(calls) == 1, f"rotated {len(calls)} times across two calls"


async def test_an_expiring_token_is_rotated_before_it_dies(db_session, monkeypatch):
    """The cache must not hand out a token that expires mid-request."""
    db_session.add(AppSetting(key="slack_config_token", value="xoxe.xoxp-OLD"))
    db_session.add(AppSetting(key="slack_config_refresh_token", value="xoxe-1-OLD"))
    db_session.add(AppSetting(key="slack_config_token_exp",
                              value=str(int(time.time()) + 5)))   # inside the margin
    await db_session.flush()
    monkeypatch.setattr(
        "src.services.slack_provisioning.rotate_config_token",
        lambda r: ("xoxe.xoxp-FRESH", "xoxe-1-FRESH", int(time.time()) + 43200))
    monkeypatch.setattr("src.services.admin_provisioning.get_settings",
                        lambda: _settings_with(refresh=""))
    assert await _config_token(db_session) == "xoxe.xoxp-FRESH"


async def test_a_valid_cached_token_is_returned_untouched(db_session, monkeypatch):
    """Control for the expiry test: a token with plenty of life left must NOT rotate."""
    db_session.add(AppSetting(key="slack_config_token", value="xoxe.xoxp-STILLGOOD"))
    db_session.add(AppSetting(key="slack_config_token_exp",
                              value=str(int(time.time()) + 43200)))
    await db_session.flush()

    def _boom(_r):
        raise AssertionError("rotate must not be called for a healthy cached token")

    monkeypatch.setattr("src.services.slack_provisioning.rotate_config_token", _boom)
    monkeypatch.setattr("src.services.admin_provisioning.get_settings",
                        lambda: _settings_with(refresh="xoxe-1-SEED"))
    assert await _config_token(db_session) == "xoxe.xoxp-STILLGOOD"


def _settings_with(*, refresh: str = "", token: str = ""):
    import types
    return types.SimpleNamespace(
        slack_config_refresh_token=refresh, slack_config_token=token,
        base_url="http://localhost:8001",
    )
