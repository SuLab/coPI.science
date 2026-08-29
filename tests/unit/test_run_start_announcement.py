"""_announce_run_start: fresh runs announce to the configured channels, once,
with the hub's voice, and nothing about the announcement can break a startup.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.run_marker import RUN_START_MARKER_PREFIX
from src.agent.simulation import SimulationEngine
from src.models import SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _engine(monkeypatch, tmp_path, *, with_hub=True, channels="general,assessments-summary"):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    # Stub the ONE setting the method reads. Never touch the real
    # (lru_cached) get_settings from a test: clearing the cache leaks a
    # test-built Settings into every later test in the session.
    from types import SimpleNamespace
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: SimpleNamespace(
            run_start_announce_channels=channels,
            # Required since SimulationEngine.__init__ eagerly constructs the
            # reply-lane semaphore (self._reply_sem) — see the same note in
            # tests/unit/test_cohort_isolation.py's _settings().
            reply_lane_max_in_flight=1,
        ),
    )

    agents, clients = [], {}
    if with_hub:
        hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
        agents.append(hub)
        clients["blackbird"] = FakeSlackClient(agent_id="blackbird")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    agents.append(lab)
    clients["wang"] = FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(agents=agents, slack_clients=clients, fresh_start=True)
    eng._channel_id_map.update({
        "general": "C-GENERAL", "assessments-summary": "C-SUMMARY",
    })
    return eng, clients


async def test_posts_the_marker_to_every_configured_channel(monkeypatch, tmp_path):
    eng, clients = _engine(monkeypatch, tmp_path)

    await eng._announce_run_start()

    hub = clients["blackbird"]
    for ch_id in ("C-GENERAL", "C-SUMMARY"):
        texts = hub.posted_messages.get(ch_id, [])
        assert len(texts) == 1
        assert texts[0].startswith(RUN_START_MARKER_PREFIX)
    assert not clients["wang"].posted  # the hub speaks, not a lab


async def test_falls_back_to_another_client_without_a_hub(monkeypatch, tmp_path, caplog):
    eng, clients = _engine(monkeypatch, tmp_path, with_hub=False)

    await eng._announce_run_start()

    assert len(clients["wang"].posted_messages.get("C-GENERAL", [])) == 1
    assert any("falling back" in r.getMessage().lower() for r in caplog.records)


async def test_unresolvable_and_local_channels_are_skipped(monkeypatch, tmp_path, caplog):
    eng, clients = _engine(
        monkeypatch, tmp_path, channels="general,assessments-summary,no-such-channel",
    )
    eng._channel_id_map["assessments-summary"] = "local:assessments-summary"

    await eng._announce_run_start()

    hub = clients["blackbird"]
    assert len(hub.posted_messages.get("C-GENERAL", [])) == 1
    assert "local:assessments-summary" not in hub.posted_messages
    assert not any(k.startswith("local:") for k in hub.posted_messages)


async def test_a_refused_post_is_tolerated(monkeypatch, tmp_path):
    eng, clients = _engine(monkeypatch, tmp_path)
    monkeypatch.setattr(clients["blackbird"], "post_message", lambda *a, **k: None)

    await eng._announce_run_start()  # must not raise


async def test_empty_setting_disables_the_announcement(monkeypatch, tmp_path):
    eng, clients = _engine(monkeypatch, tmp_path, channels="")

    await eng._announce_run_start()

    assert not clients["blackbird"].posted


async def test_no_connected_client_is_a_quiet_skip(monkeypatch, tmp_path):
    eng, clients = _engine(monkeypatch, tmp_path)
    for c in clients.values():
        monkeypatch.setattr(type(c), "is_connected", property(lambda self: False))

    await eng._announce_run_start()  # must not raise; nothing posted

    assert not clients["blackbird"].posted


async def test_the_run_row_records_the_announcement(monkeypatch, tmp_path, engine):
    eng, clients = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun(status="running", config={"seed": True})
        db.add(run)
        await db.commit()
        run_id = run.id
    eng.session_factory = factory
    eng.simulation_run_id = run_id

    await eng._announce_run_start()

    try:
        async with factory() as db:
            row = (await db.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one()
        rec = row.config["run_start_announcement"]
        assert rec["posted"].keys() == {"general", "assessments-summary"}
        assert rec["failed"] == []
        assert rec["text"].startswith(RUN_START_MARKER_PREFIX)
        assert row.config["seed"] is True  # reassignment preserved existing keys
    finally:
        # The factory commits for real (this is the method's own session
        # path, not the rolled-back db_session fixture); don't leave a run
        # row in the shared session DB — main.py's resume path orders by
        # started_at DESC, so a leaked row could poison a future test.
        async with factory() as db:
            leftover = (await db.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if leftover is not None:
                await db.delete(leftover)
                await db.commit()


async def test_start_announces_only_fresh_runs_after_validation(monkeypatch, tmp_path):
    """Positional pin: announce fires between _validate_star_topology and
    _run_main_loop, and only when fresh_start=True."""
    calls: list[str] = []

    def _rec(name, result=None):
        async def _async(*a, **k):
            calls.append(name)
            return result
        def _sync(*a, **k):
            calls.append(name)
            return result
        return _async, _sync

    for fresh, expected in ((True, 1), (False, 0)):
        calls.clear()
        eng, _ = _engine(monkeypatch, tmp_path)
        eng._fresh_start = fresh
        for name in (
            "_persist_seeded_channels", "_sync_private_channels_from_db",
            "_rebuild_state_from_db", "_restore_slack_state",
            "_rebuild_agent_state", "_rehydrate_assessed_threads",
            "_recompute_allowed_sender_ids", "_record_topology_snapshot",
            "_announce_run_start", "_run_main_loop",
        ):
            monkeypatch.setattr(eng, name, _rec(name)[0])
        for name in (
            "_ensure_seeded_channels", "_ensure_assessments_summary_channel",
            "_rewind_cursors_for_private_channels", "refresh_lab_directories",
        ):
            monkeypatch.setattr(eng, name, _rec(name)[1])
        monkeypatch.setattr(eng, "_validate_star_topology", _rec("_validate", [])[1])

        try:
            await eng.start()
        finally:
            # start() installs a process-global LLM-log callback (:849,
            # src/services/llm.py) pointing at this dead test engine —
            # clear it so no later test's LLM fake calls into it.
            from src.agent.simulation import set_call_log_callback
            set_call_log_callback(None)

        assert calls.count("_announce_run_start") == expected
        if fresh:
            assert calls.index("_validate") < calls.index("_announce_run_start")
            assert calls.index("_announce_run_start") < calls.index("_run_main_loop")
