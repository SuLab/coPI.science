"""revoke_token: the deletion teardown's Slack half (audit F2 / decision D3)."""
import pytest
from slack_sdk.errors import SlackApiError

from src.services import slack_web


class _FakeClient:
    def __init__(self, result=None, error_code=None):
        self._result = result
        self._error_code = error_code

    def auth_revoke(self):
        if self._error_code:
            raise SlackApiError(
                message="boom", response={"ok": False, "error": self._error_code}
            )
        return self._result


def test_revoke_token_true_on_success(monkeypatch):
    monkeypatch.setattr(
        slack_web, "_client", lambda token: _FakeClient(result={"ok": True, "revoked": True})
    )
    assert slack_web.revoke_token("xoxb-live") is True


def test_revoke_token_true_when_already_dead(monkeypatch):
    for code in ("token_revoked", "invalid_auth", "account_inactive"):
        monkeypatch.setattr(
            slack_web, "_client", lambda token, c=code: _FakeClient(error_code=c)
        )
        assert slack_web.revoke_token("xoxb-dead") is True


def test_revoke_token_raises_on_other_errors(monkeypatch):
    # no_permission is in slack_web._TERMINAL but NOT in revoke_token's
    # already-dead trio, so _call raises immediately — no retry backoff, so
    # this test stays fast (a non-terminal code would time.sleep ~3.5s).
    monkeypatch.setattr(
        slack_web, "_client", lambda token: _FakeClient(error_code="no_permission")
    )
    with pytest.raises(SlackApiError):
        slack_web.revoke_token("xoxb-live")
