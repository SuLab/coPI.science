"""On a RESUME, the hub recovers lab pitches that got no reply because the
previous process stopped in the tick they were posted."""
import time

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine, set_call_log_callback
from src.agent.state import ThreadState
from src.agent.transport import NullTransport
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _engine(monkeypatch, tmp_path, *, fresh_start=False, slack_off=False):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    make = (lambda aid: NullTransport(aid)) if slack_off else (lambda aid: FakeSlackClient(agent_id=aid))
    eng = SimulationEngine(
        agents=[hub, lab], slack_clients={"blackbird": make("blackbird"), "wang": make("wang")},
        fresh_start=fresh_start, slack_enabled=not slack_off,
    )
    eng._channel_id_map["general"] = "local:general" if slack_off else "C_general"
    return eng, hub, lab


def _pitch(eng, ts, *, sender="wang", thread_ts=None, content="an orphaned pitch"):
    eng.message_log.append(LogEntry(
        ts=ts, channel="general", sender_agent_id=sender,
        sender_name="WangBot" if sender == "wang" else "BlackbirdBot",
        content=content, thread_ts=thread_ts, posted_at=float(ts), is_bot=True,
    ))


async def test_a_resume_activates_a_pitch_from_the_stop_tick(monkeypatch, tmp_path):
    eng, hub, _lab = _engine(monkeypatch, tmp_path)
    now = time.time()
    _pitch(eng, f"{now - 30:.6f}")
    hub.state.last_seen_cursor = now  # the rebuilt cursor is already past the pitch

    await eng._recover_reply_less_pitches()

    assert "general" in hub.state.subscribed_channels
    [(thread_id, thread)] = hub.state.active_threads.items()
    assert thread.has_pending_reply is True and thread.other_agent_id == "wang"
    assert hub.state.last_seen_cursor == now, "the cursor is never moved back"
    assert (hub, thread) in eng._pending_reply_pairs(), "owed before the hub's first turn"


async def test_an_already_answered_active_thread_gets_no_reply_on_tick_1(monkeypatch, tmp_path):
    eng, hub, _lab = _engine(monkeypatch, tmp_path)
    now = time.time()
    root = f"{now - 60:.6f}"
    _pitch(eng, root)
    _pitch(eng, f"{now - 50:.6f}", sender="blackbird", thread_ts=root, content="hub answered")
    hub.state.active_threads[root] = ThreadState(
        thread_id=root, channel="general", other_agent_id="wang", message_count=2,
        has_pending_reply=False,
    )
    hub.state.last_seen_cursor = now

    await eng._recover_reply_less_pitches()

    assert hub.state.active_threads[root].has_pending_reply is False
    assert eng._pending_reply_pairs() == []


async def test_a_pitch_the_cohort_gate_excludes_is_not_activated(monkeypatch, tmp_path):
    eng, hub, _lab = _engine(monkeypatch, tmp_path)
    _pitch(eng, f"{time.time() - 30:.6f}")
    hub.allowed_sender_ids = {"someone-else"}

    await eng._recover_reply_less_pitches()

    assert hub.state.active_threads == {}


async def test_a_failure_never_aborts_startup(monkeypatch, tmp_path):
    eng, hub, _lab = _engine(monkeypatch, tmp_path)

    def boom(agent, since):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(eng, "_auto_activate_lab_posts", boom)
    await eng._recover_reply_less_pitches()  # must not raise


def _record_start(monkeypatch, eng, calls):
    async def _rec_async(name):
        calls.append(name)

    for name in (
        "_persist_seeded_channels", "_sync_private_channels_from_db", "_rebuild_state_from_db",
        "_restore_slack_state", "_rebuild_agent_state", "_rehydrate_assessed_threads",
        "_recompute_allowed_sender_ids", "_record_topology_snapshot", "_announce_run_start",
        "_run_main_loop",
    ):
        monkeypatch.setattr(eng, name, lambda *a, _n=name, **k: _rec_async(_n), raising=False)
    for name in (
        "_ensure_seeded_channels", "_ensure_assessments_summary_channel",
        "_rewind_cursors_for_private_channels", "refresh_lab_directories",
    ):
        monkeypatch.setattr(eng, name, lambda *a, _n=name, **k: calls.append(_n), raising=False)
    monkeypatch.setattr(eng, "_validate_star_topology", lambda: calls.append("_validate") or [])


@pytest.mark.parametrize("fresh", [False, True])
async def test_start_runs_the_one_shot_only_on_a_resume_after_gate_and_validation(
    monkeypatch, tmp_path, fresh,
):
    eng, hub, _lab = _engine(monkeypatch, tmp_path, fresh_start=fresh)
    calls: list[str] = []
    _record_start(monkeypatch, eng, calls)
    real = eng._recover_reply_less_pitches

    async def recover():
        calls.append("_recover_reply_less_pitches")
        await real()

    monkeypatch.setattr(eng, "_recover_reply_less_pitches", recover)
    try:
        await eng.start()
    finally:
        set_call_log_callback(None)

    if fresh:
        assert "_recover_reply_less_pitches" not in calls
        assert hub.state.subscribed_channels == set(), "a fresh hub subscribes at its first post turn"
        assert eng.slack_clients["blackbird"].joined_channels == set(), "no Phase-1 join at start"
    else:
        i = calls.index("_recover_reply_less_pitches")
        assert calls.index("_recompute_allowed_sender_ids") < i
        assert calls.index("_validate") < i
        assert i < calls.index("_run_main_loop")


async def test_slack_off_start_completes_with_the_one_shot(monkeypatch, tmp_path):
    """With NullTransport clients the one-shot's Phase-1 join is a no-op."""
    eng, hub, _lab = _engine(monkeypatch, tmp_path, slack_off=True)
    calls: list[str] = []
    _record_start(monkeypatch, eng, calls)
    _pitch(eng, f"{time.time() - 30:.6f}")
    try:
        await eng.start()
    finally:
        set_call_log_callback(None)
    assert "_run_main_loop" in calls
    assert len(hub.state.active_threads) == 1
