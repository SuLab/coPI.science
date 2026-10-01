import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_default_is_dry_run(monkeypatch, capsys):
    import scripts.slack_test_teardown as t

    calls = []
    monkeypatch.setenv("SLACK_TEST_BOT_TOKEN_SU", "xoxb-1")
    monkeypatch.delenv("SLACK_CONFIG_TOKEN", raising=False)
    monkeypatch.setattr(t, "call", lambda method, token, payload=None, form=False: (
        calls.append(method),
        {"ok": True, "team_id": "T1", "team": "test", "url": "https://test.slack.com/",
         "channels": [{"name": "t-x", "id": "C1"}], "response_metadata": {}})[1])
    monkeypatch.setattr(sys, "argv", ["x", "--workspace", "T1"])
    t.main()
    assert "conversations.archive" not in calls
    assert "dry run" in capsys.readouterr().out.lower()


def test_apply_archives_in_the_asserted_workspace(monkeypatch):
    import scripts.slack_test_teardown as t

    calls = []
    monkeypatch.setenv("SLACK_TEST_BOT_TOKEN_SU", "xoxb-1")
    monkeypatch.delenv("SLACK_CONFIG_TOKEN", raising=False)
    monkeypatch.setattr(t, "call", lambda method, token, payload=None, form=False: (
        calls.append(method),
        {"ok": True, "team_id": "T1", "team": "test", "url": "https://test.slack.com/",
         "channels": [{"name": "t-x", "id": "C1"}], "response_metadata": {}})[1])
    monkeypatch.setattr(sys, "argv", ["x", "--workspace", "T1", "--apply"])
    t.main()
    assert "conversations.archive" in calls


def test_workspace_mismatch_aborts():
    import scripts.slack_test_teardown as t

    with pytest.raises(SystemExit):
        t.assert_workspace({"team_id": "T1", "team": "test", "url": "https://test.slack.com/"}, "T9")


def test_workspace_substring_is_refused():
    import scripts.slack_test_teardown as t

    auth = {"team_id": "T1", "team": "test-prod", "url": "https://test-prod.slack.com/"}
    with pytest.raises(SystemExit):
        t.assert_workspace(auth, "test")
    t.assert_workspace(auth, "test-prod")
