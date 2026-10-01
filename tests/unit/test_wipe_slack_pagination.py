import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class FakeClient:
    def __init__(self):
        self.deleted = []
        self.history = {
            None: {"messages": [{"ts": "1", "user": "U"}, {"ts": "2", "user": "X", "reply_count": 2}],
                   "response_metadata": {"next_cursor": "p2"}},
            "p2": {"messages": [{"ts": "3", "user": "U", "reply_count": 1}], "response_metadata": {}},
        }
        self.replies = {"2": [{"ts": "2", "user": "X"}, {"ts": "2.1", "user": "U"}, {"ts": "2.2", "user": "X"}],
                        "3": [{"ts": "3", "user": "U"}, {"ts": "3.1", "user": "U"}]}

    def auth_test(self):
        return {"user_id": "U"}

    def conversations_history(self, channel, limit, cursor=None):
        return self.history[cursor]

    def conversations_replies(self, channel, ts, limit=200, cursor=None):
        return {"messages": self.replies[ts], "response_metadata": {}}

    def conversations_list(self, types, limit, cursor=None):
        pages = {None: {"channels": [{"id": "C1", "name": "a"}], "response_metadata": {"next_cursor": "n"}},
                 "n": {"channels": [{"id": "C2", "name": "b"}], "response_metadata": {}}}
        return pages[cursor]

    def chat_delete(self, channel, ts):
        self.deleted.append(ts)
        for page in self.history.values():
            page["messages"] = [m for m in page["messages"] if m["ts"] != ts]
        for ts_root, msgs in self.replies.items():
            self.replies[ts_root] = [m for m in msgs if m["ts"] != ts]


def test_dry_run_count_equals_delete_count(monkeypatch):
    import scripts.wipe_slack as w

    fake = FakeClient()
    monkeypatch.setattr(w, "WebClient", lambda token: fake)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    counted = w._count_for_bot("a", "xoxb-1", ["C1"], {"C1": "a"})
    deleted = w._wipe_for_bot("a", "xoxb-1", ["C1"], {"C1": "a"})
    assert counted == deleted == 4
    assert sorted(fake.deleted) == ["1", "2.1", "3", "3.1"]


def test_channel_list_is_paginated():
    import scripts.wipe_slack as w

    assert [c["id"] for c in w.list_channels(FakeClient())] == ["C1", "C2"]


def test_workspace_match_is_exact_not_substring():
    import scripts.wipe_slack as w

    info = {"team_id": "T0123", "team": "Test Lab", "url": "https://test-prod.slack.com/"}
    assert w.workspace_matches(info, "T0123")
    assert w.workspace_matches(info, "test lab")
    assert w.workspace_matches(info, "test-prod")
    assert not w.workspace_matches(info, "test")
    assert not w.workspace_matches(info, "T01")
    assert not w.workspace_matches(info, "  ")
