"""`_auto_activate_lab_posts`: the hub's Phase-3 auto-activation, extracted so
the resume one-shot applies exactly the same gates."""
from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.visibility import VISIBILITY_COLLAB_PRIVATE
from tests.fakes import FakeSlackClient


def _engine(monkeypatch, tmp_path):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    labs = [Agent("wang", "WangBot", "Wang"), Agent("gordy", "GordyBot", "Gordy")]
    eng = SimulationEngine(
        agents=[hub, *labs],
        slack_clients={a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in [hub, *labs]},
    )
    hub.state.subscribed_channels = {"general", "private-room"}
    eng._channel_visibility["private-room"] = VISIBILITY_COLLAB_PRIVATE
    return eng, hub


def _post(eng, ts, sender, *, channel="general", is_bot=True):
    eng.message_log.append(LogEntry(
        ts=ts, channel=channel, sender_agent_id=sender, sender_name=f"{sender.title()}Bot",
        content=f"pitch {ts}", thread_ts=None, posted_at=float(ts), is_bot=is_bot,
    ))


def test_every_phase3_gate_applies_and_the_count_is_returned(monkeypatch, tmp_path):
    eng, hub = _engine(monkeypatch, tmp_path)
    _post(eng, "100.000001", "wang")                       # activated
    _post(eng, "100.000002", "wang", is_bot=False)         # human row: skipped
    _post(eng, "100.000003", "wang", channel="genomics")   # not subscribed: skipped
    _post(eng, "100.000004", "wang", channel="private-room")  # collab_private: skipped
    _post(eng, "100.000005", "wang")                       # closed: skipped
    eng._closed_thread_ids.add("100.000005")
    _post(eng, "100.000006", "gordy")                      # outside the gate: skipped
    hub.allowed_sender_ids = {"wang"}
    _post(eng, "50.000000", "wang")                        # older than `since`: skipped

    assert eng._auto_activate_lab_posts(hub, since=99.0) == 1
    assert list(hub.state.active_threads) == ["100.000001"]
    assert hub.state.active_threads["100.000001"].has_pending_reply is True
    assert eng._auto_activate_lab_posts(hub, since=99.0) == 0, "already active: skipped"


def test_phase3_still_auto_activates_through_the_extracted_method(monkeypatch, tmp_path):
    eng, hub = _engine(monkeypatch, tmp_path)
    calls = []
    real = eng._auto_activate_lab_posts
    monkeypatch.setattr(
        eng, "_auto_activate_lab_posts",
        lambda agent, since: calls.append((agent.agent_id, since)) or real(agent, since),
    )
    hub.state.last_seen_cursor = 7.5
    eng._phase3_activate_threads(hub)
    assert calls == [("blackbird", 7.5)]
