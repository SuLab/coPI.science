"""The hub never reaches Phase 5 at all (hard role gate, decision 9); a lab
at the active-thread threshold skips Phase 5 outright, before any LLM call.

This file used to pin the opposite: a saturated hub needed a `terminal_only`
exemption to still file its :mag: Opportunity Assessment THROUGH Phase 5's
backpressure gate, because production run 2485863a showed the hub holding 65
open threads against a threshold of 12, taking 30 turns, and reaching Phase 5
exactly zero times while every PI bot reached it routinely — the more
interviews it finished, the more assessments it owed and the less able it was
to file any of them.

The reply-only-hub reconciliation (Option A) removed the underlying premise
instead of patching around it: the hub's assessment is not filed through
Phase 5 anymore at all — it is the `<assessment_json>` sidecar carried inside
the hub's own Phase-4 CONCLUDE reply (see simulation.py's
`_reply_to_thread`/`_capture_hub_assessment`). There is therefore nothing
left for the hub to be exempted FROM: `_phase5_new_post` hard-gates on
`agent.role == "scout_hub"` before doing any work at all (no settings lookup,
no prompt built, no LLM call — see that function's docstring for the
cost/noise trap this also closes), and a saturated LAB (the only role that
still reaches this function) now simply skips at the threshold, exactly like
the daily-post-cap check just above it.
"""
import inspect
import types

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def _lab(agent_id="gill"):
    return Agent(agent_id, f"{agent_id.capitalize()}Bot", f"{agent_id.upper()} PI", role="pi_lab")


def _settings(**over):
    base = dict(
        daily_post_cap=5,
        lab_daily_post_cap=1,
        active_thread_threshold=12,
        phase5_skip_probability=0.0,
        llm_agent_model_opus="test-model",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _no_llm_reached(monkeypatch, agent):
    """Wires ``agent`` so a reached LLM call/prompt build is observable, and
    returns the dict this fills in (``{"prompt": bool, "llm": bool}``)."""
    called = {"prompt": False, "llm": False}

    def _stub_prompt(**kw):
        called["prompt"] = True
        return ("sys", [])

    async def _fake_generate(**kwargs):
        called["llm"] = True
        return '```json\n{"action": "skip"}\n```\n\n<slack_message>skip</slack_message>'

    monkeypatch.setattr(agent, "build_phase5_prompt", _stub_prompt)
    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)
    return called


# --- (b) the hub never enters phase 5, engine-level -------------------------

async def test_scout_hub_never_reaches_the_llm_in_phase_5(monkeypatch):
    """Even a hub with a perfectly ordinary load (well under any threshold,
    nothing blocking it) never builds a Phase-5 prompt or makes a Phase-5 LLM
    call — the hard gate returns before either exists, for every hub, not
    just a saturated one."""
    hub = _hub()
    hub.allowed_sender_ids = None
    client = FakeSlackClient(agent_id="blackbird")
    eng = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings())
    called = _no_llm_reached(monkeypatch, hub)

    await eng._phase5_new_post(hub)

    assert called == {"prompt": False, "llm": False}
    assert client.posted == []


async def test_scout_hub_never_reaches_the_llm_in_phase_5_even_when_saturated(monkeypatch):
    """The exact old production shape (65 open threads against a threshold of
    12) — still never reaches the LLM. Before Option A this was the hub's one
    exemption; now there is nothing to exempt, because there is nothing left
    for the hub to post through this function at all."""
    hub = _hub()
    hub.allowed_sender_ids = None
    for i in range(65):
        tid = f"thread-{i}"
        hub.state.active_threads[tid] = ThreadState(
            thread_id=tid, channel="general", other_agent_id=f"pi{i}",
            message_count=4,
        )
    client = FakeSlackClient(agent_id="blackbird")
    eng = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: _settings(active_thread_threshold=12),
    )
    called = _no_llm_reached(monkeypatch, hub)

    await eng._phase5_new_post(hub)

    assert called == {"prompt": False, "llm": False}
    assert client.posted == []


def test_scout_hub_gate_is_the_first_check_in_phase5_new_post():
    """Source-inspection pin (mirrors test_phase2_guard.py's pattern): the
    hard gate must be visible in `_phase5_new_post`'s own source as the
    check that runs before ``get_settings()`` — i.e. before any other work —
    not merely somewhere in the function."""
    src = inspect.getsource(SimulationEngine._phase5_new_post)
    gate_pos = src.find('agent.role == "scout_hub"')
    settings_pos = src.find("get_settings()")
    assert gate_pos != -1, "no scout_hub role check found in _phase5_new_post"
    assert settings_pos != -1
    assert gate_pos < settings_pos, (
        "the scout_hub gate must run before get_settings() — i.e. before any "
        "other work in the function"
    )


# --- (c) a lab at the active-thread threshold skips phase 5 pre-LLM --------

async def test_a_lab_at_the_active_thread_threshold_skips_phase_5_pre_llm(monkeypatch):
    """A lab (the only role that still reaches this function) holding
    `active_thread_threshold` or more open threads skips Phase 5 outright —
    no prompt built, no LLM call — exactly like the daily-cap check just
    above it. This used to still reach the LLM via the (now-removed)
    terminal-artifact exemption path; a lab never had a terminal type to
    exploit that path with, so this is the behaviour it always should have
    had."""
    lab = _lab()
    lab.allowed_sender_ids = None
    hub = _hub()
    hub.allowed_sender_ids = None
    for i in range(12):
        tid = f"thread-{i}"
        lab.state.active_threads[tid] = ThreadState(
            thread_id=tid, channel="general", other_agent_id="blackbird",
            message_count=4,
        )
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"gill": client, "blackbird": FakeSlackClient(agent_id="blackbird")},
    )
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: _settings(active_thread_threshold=12),
    )
    called = _no_llm_reached(monkeypatch, lab)

    await eng._phase5_new_post(lab)

    assert called == {"prompt": False, "llm": False}
    assert client.posted == []


async def test_a_lab_below_the_threshold_is_unaffected(monkeypatch):
    """Sanity-checks the threshold discriminates correctly: below the
    threshold, Phase 5 proceeds normally and a pitch still posts."""
    lab = _lab()
    lab.allowed_sender_ids = None
    hub = _hub()
    hub.allowed_sender_ids = None
    for i in range(2):
        tid = f"thread-{i}"
        lab.state.active_threads[tid] = ThreadState(
            thread_id=tid, channel="general", other_agent_id="blackbird",
            message_count=4,
        )
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"gill": client, "blackbird": FakeSlackClient(agent_id="blackbird")},
    )
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: _settings(active_thread_threshold=12),
    )
    monkeypatch.setattr(lab, "build_phase5_prompt", lambda **kw: ("sys", []))

    response = (
        '```json\n'
        '{"action": "new_post", "channel": "general", '
        '"post_type": "pitch", "tagged_agent": null}\n'
        '```\n\n'
        '<slack_message>:bulb: A thing.</slack_message>'
    )

    async def _fake_generate(**kwargs):
        return response

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)

    await eng._phase5_new_post(lab)

    assert len(client.posted) == 1
