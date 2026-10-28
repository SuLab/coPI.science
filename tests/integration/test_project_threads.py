"""A project thread in the reply lane (spec §5.8, D55).

The thread under a lab's reply to one of BlackbirdBot's projects (root logged with phase
``project_reply``) gets project prompts and tools; BlackbirdBot's close records the project's
outcome and closes the thread as ``project_closed``; nothing an interview writes (assessment,
drop, decline, headline, panel note) happens there. Driven through the real
``_reply_to_thread`` on a live engine, as ``test_hub_assessment_capture_gate._drive_reply``.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.engine import reply_lane
from src.agent.message_log import LogEntry
from src.agent.sessions import PROJECTS
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import SpecialistConsult
from src.models.hub_project import (
    REASON_CLOSE_UNREADABLE,
    STATUS_NOT_FINISHED,
    STATUS_POSTED,
    STATUS_READY,
    STATUS_REFINING,
    HubProject,
)
from tests.fakes import FakeSlackClient
from tests.integration.test_hub_assessment_capture_gate import (
    _assessments,
    _decisions,
    _delete_run,
    _drops,
    _new_run,
    _verdict,
)
from tests.integration.test_hub_declines_engine import (
    _declines,
    _headlines,
    _wire_summary_channel,
)

pytestmark = pytest.mark.integration

ROOT = "1700000000.000000"
CHANNEL = "general"
TITLE = "Lipid panel response in a mouse model"
PLAN = {
    "indication": "Atherosclerosis",
    "target": "LOX-1",
    "model": "ApoE-/- mice",
    "positive_control": "Atorvastatin arm",
    "negative_control": "Vehicle",
    "standard_of_care": "Statins",
    "readout": "Plasma lipidomics",
    "design": "Two arms, ten mice each",
    "time_and_budget": "9 months, $200K",
    "lab_tools": ["LC-MS lipidomics"],
}
CHOICE = {
    "title": TITLE,
    "summary": "Test whether the lab's lipidomics workflow reads out drug response.",
    "plan": {**PLAN, "positive_control": "to settle with the lab"},
    "anchors": [{"section": "Model Systems", "item": "ApoE-/- mice"}],
    "sources": [{"key": "pmid:31500698", "kind": "pubmed", "title": "A LOX-1 paper",
                 "domain": "scientific", "shows": "LOX-1 in plaques"}],
    "why_chosen": "It uses the lab's own model.",
}


def _close_block(position: str = "agree", plan: dict | None = None) -> str:
    body = {"position": position, "plan": plan or PLAN, "gaps": [], "disagreements": []}
    return f"<project_json>{json.dumps(body)}</project_json>"


def _history(count: int, *, lab_latest: str = "We have used that model.",
             split_at: int | None = None) -> list[tuple[str, str]]:
    """``count`` log entries, the root first: the lab and the hub alternate from the
    root; ``split_at`` repeats the lab at that index (a reply split into two chunks).
    The lab's last entry carries ``lab_latest``."""
    senders: list[str] = []
    for i in range(count):
        if split_at is not None and i == split_at:
            senders.append("gordy")
            continue
        previous = senders[-1] if senders else None
        senders.append("gordy" if previous in (None, "blackbird") else "blackbird")
    last_lab = max(i for i, s in enumerate(senders) if s == "gordy")
    entries = []
    for i, sender in enumerate(senders):
        if i == 0:
            text = f":test_tube: *Blackbird project:* {TITLE}\nWe would use our ApoE-/- mice."
        elif i == last_lab:
            text = lab_latest
        else:
            text = f"project message {i} from {sender}"
        entries.append((sender, text))
    return entries


def _chunked(history: list[tuple[str | None, str]], chunks: int) -> list[tuple[str | None, str]]:
    """``history`` with every entry after the root logged as ``chunks`` chunks, as a reply
    over Slack's limit is; every chunk after the first ends " (part k)"."""
    out = [history[0]]
    for sender, text in history[1:]:
        out.append((sender, text))
        out.extend((sender, f"{text} (part {k})") for k in range(2, chunks + 1))
    return out


async def _projects(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(HubProject).where(HubProject.simulation_run_id == run_id)
        )).scalars().all()


async def _consults(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(SpecialistConsult).where(SpecialistConsult.simulation_run_id == run_id)
        )).scalars().all()


async def _drive_project(
    engine, monkeypatch, raw_response, *, history, as_lab: bool = False,
    with_project: bool = True,
    project_status: str = STATUS_REFINING,
    project_reply_ts: str | None = ROOT,
    configure: Callable[[SimulationEngine], None] | None = None,
    generate: Callable | None = None,
):
    """One turn of the hub (or, ``as_lab``, the lab) in a project thread whose log holds
    ``history``, whose ``None`` senders are the lab's PI (a human message). Returns
    ``(sim, agent, thread, calls, factory, run_id)``; ``calls`` holds the kwargs of every
    ``generate_with_tools`` call."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("gordy", "GordyBot", "Gordy Lab")
    for agent in (hub, lab):
        # No disk reads: the live mount may hold real profiles and memory.
        agent._public_profile = "# Gordy Lab\n\n## Research Summary\nLipidomics.\n"
        agent._public_working_memory = ""
    sim = SimulationEngine(
        agents=[hub, lab],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "gordy": FakeSlackClient(agent_id="gordy"),
        },
        session_factory=factory, simulation_run_id=run_id,
    )
    if configure is not None:
        configure(sim)
    for i, (sender, text) in enumerate(history):
        # Sub-second steps after ROOT: FakeSlackClient numbers its posts 1700000001.000000
        # upwards, and a seeded entry on one of those ts would swallow the turn's own post.
        ts = ROOT if i == 0 else f"1700000000.{i:06d}"
        names = {"gordy": "GordyBot", "blackbird": "BlackbirdBot", None: "Gordy (PI)"}
        sim.message_log.append(LogEntry(
            ts=ts, channel=CHANNEL, sender_agent_id=sender, sender_name=names[sender],
            is_bot=sender is not None,
            content=text, thread_ts=None if i == 0 else ROOT, posted_at=float(ts),
            slack_ts=ts, slack_channel_id="C_GENERAL",
            phase="project_reply" if i == 0 else None,
        ))
    if with_project:
        async with factory() as db:
            db.add(HubProject(
                simulation_run_id=run_id, agent_id="gordy", slot=1, status=project_status,
                requested_run_seconds=0.0, choice=CHOICE, title=TITLE,
                posting_text=f":test_tube: *Blackbird project for the Gordy lab*\n{TITLE}",
                posting_ts="1690000000.000000", reply_ts=project_reply_ts,
                reply_claimed_at=None if project_reply_ts else datetime.now(UTC),
            ))
            await db.commit()
    agent = lab if as_lab else hub
    other = "blackbird" if as_lab else "gordy"
    thread = ThreadState(
        thread_id=ROOT, channel=CHANNEL, other_agent_id=other,
        message_count=len(history), has_pending_reply=True,
    )
    agent.state.active_threads[ROOT] = thread
    calls: list[dict] = []

    async def _fake_generate_with_tools(**kwargs):
        calls.append(kwargs)
        if generate is not None:
            return await generate(**kwargs)
        return raw_response

    monkeypatch.setattr("src.agent.engine.deps.generate_with_tools", _fake_generate_with_tools)

    async def _fast_memory_update(*args, **kwargs):
        return None

    monkeypatch.setattr(sim, "_update_agent_memory", _fast_memory_update)
    await sim._reply_to_thread(agent, thread)
    return sim, agent, thread, calls, factory, run_id


def _thread_posts(sim, agent_id="blackbird"):
    return [p for p in sim.slack_clients[agent_id].posted if p.get("thread_ts") == ROOT]


@pytest.mark.asyncio
async def test_project_turn_uses_project_prompts_and_tools(engine, monkeypatch):
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, "<slack_message>Which positive control?</slack_message>",
        history=_history(5),
    )
    try:
        [call] = calls
        assert "BlackbirdBot in a project thread" in call["system_prompt"]
        tool_names = {t["name"] for t in call["tools"]}
        assert {"search_pubmed", "search_trials", "consult_specialist"} <= tool_names
        meta = call["log_meta"]
        assert meta["phase"] == "project_thread"
        assert meta["thread_phase"] == "project_refine"
        assert meta["message_ordinal"] == 6
        assert meta["thread_ts"] == ROOT
        assert "<project_brief>" in call["messages"][0]["content"]
        assert [p["text"] for p in _thread_posts(sim)] == ["Which positive control?"]
        assert thread.status == "active" and thread.has_pending_reply is False
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_REFINING
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_no_assessment_drop_decline_or_headline_in_a_project_thread(engine, monkeypatch):
    raw = (
        "<slack_message>⏸️ Not a fit for the hub right now.</slack_message>\n"
        f"<assessment_json>{json.dumps(_verdict())}</assessment_json>"
    )
    sim, _agent, thread, _calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=_history(11), configure=_wire_summary_channel,
    )
    try:
        assert await _assessments(factory, run_id) == []
        assert await _drops(factory, run_id) == []
        assert await _declines(factory, run_id) == []
        assert _headlines(sim.slack_clients["blackbird"]) == []
        # Ordinal 12 is the close band: the reply closes the project whatever it holds.
        assert thread.status == "closed"
        assert [(d.thread_id, d.outcome) for d in await _decisions(factory, run_id)] == [
            (ROOT, "project_closed")
        ]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_close_at_the_last_turn_records_the_outcome_and_closes(engine, monkeypatch):
    raw = (
        "<slack_message>✅ We agree. Final plan: two arms, ten mice each.</slack_message>\n"
        + _close_block()
    )
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=_history(11, lab_latest="✅ Agreed, we can run it."),
    )
    try:
        assert calls[0]["log_meta"]["thread_phase"] == "project_close"
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_READY
        assert project.final["plan"]["readout"] == "Plasma lipidomics"
        assert thread.status == "closed"
        [decision] = await _decisions(factory, run_id)
        assert decision.outcome == "project_closed"
        [post] = _thread_posts(sim)
        assert "project_json" not in post["text"]
        assert post["text"].startswith("✅")
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_split_post_counts_once(engine, monkeypatch):
    # A project thread counts posts, not log entries: the lab's post in two chunks is one.
    history = _history(10, lab_latest="✅ Agreed.", split_at=5)
    assert [s for s, _ in history][4:6] == ["gordy", "gordy"], "one lab post in two chunks"
    raw = "<slack_message>✅ Final plan as agreed.</slack_message>\n" + _close_block()
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=history,
    )
    try:
        meta = calls[0]["log_meta"]
        assert meta["message_ordinal"] == 10
        assert meta["thread_phase"] == "project_refine"
        assert thread.status == "closed"
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_READY
        [decision] = await _decisions(factory, run_id)
        assert decision.outcome == "project_closed"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_posts_split_three_ways_still_reach_the_close_at_12(engine, monkeypatch):
    history = _chunked(_history(11, lab_latest="✅ Agreed, we can run it."), 3)
    assert len(history) == 31
    raw = "<slack_message>✅ We agree. Final plan below.</slack_message>\n" + _close_block()
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=history,
    )
    try:
        meta = calls[0]["log_meta"]
        assert meta["message_ordinal"] == 12
        assert meta["thread_phase"] == "project_close"
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_READY
        assert [(d.thread_id, d.outcome) for d in await _decisions(factory, run_id)] == [
            (ROOT, "project_closed")
        ]
        assert thread.status == "closed"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_hub_turn_past_the_cap_closes_instead_of_timing_out(engine, monkeypatch):
    raw = "<slack_message>✅ We agree. Final plan below.</slack_message>\n" + _close_block()
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=_history(13, lab_latest="✅ Agreed."),
    )
    try:
        meta = calls[0]["log_meta"]
        assert meta["message_ordinal"] == 14
        assert meta["thread_phase"] == "project_close"
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_READY
        [decision] = await _decisions(factory, run_id)
        assert decision.outcome == "project_closed"
        assert thread.status == "closed"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_human_message_counts_and_the_hub_still_closes(engine, monkeypatch):
    history = _history(11, lab_latest="✅ Agreed.")
    history.insert(6, (None, "Looks good from here."))
    assert len(history) == 12
    raw = "<slack_message>✅ We agree. Final plan below.</slack_message>\n" + _close_block()
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=history,
    )
    try:
        meta = calls[0]["log_meta"]
        assert meta["message_ordinal"] == 13
        assert meta["thread_phase"] == "project_close"
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_READY
        assert thread.status == "closed"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_lab_turn_at_the_cap_still_times_out(engine, monkeypatch):
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, "<slack_message>One more thing.</slack_message>",
        history=_history(12), as_lab=True,
    )
    try:
        assert calls == []
        assert thread.status == "closed"
        [decision] = await _decisions(factory, run_id)
        assert decision.outcome == "timeout"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_an_early_close_one_brace_short_is_recorded(engine, monkeypatch):
    # Run 1abf809c's shape: `plan` never closed, so `gaps` and `disagreements` sit inside it.
    block = ('<project_json>{"position": "agree", "plan": {' + json.dumps(PLAN)[1:-1]
             + ', "gaps": [], "disagreements": []}</project_json>')
    raw = "<slack_message>✅ Then we are done: final plan below.</slack_message>\n" + block
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=_history(9, lab_latest="✅ This plan works for us."),
    )
    try:
        assert thread.status == "closed"
        [decision] = await _decisions(factory, run_id)
        assert decision.outcome == "project_closed"
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_READY
        assert project.final["plan"] == PLAN
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_early_close_after_lab_agreement(engine, monkeypatch):
    raw = "<slack_message>✅ Then we are done: final plan below.</slack_message>\n" + _close_block()
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=_history(5, lab_latest="✅ This plan works for us."),
    )
    try:
        assert calls[0]["log_meta"]["message_ordinal"] == 6
        assert "The lab's latest reply starts with ✅" in calls[0]["messages"][0]["content"]
        assert thread.status == "closed"
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_READY
        [decision] = await _decisions(factory, run_id)
        assert decision.outcome == "project_closed"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_early_sidecar_without_lab_agreement_is_not_a_close(engine, monkeypatch):
    raw = "<slack_message>✅ I think this is settled.</slack_message>\n" + _close_block()
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw,
        history=_history(5, lab_latest="⚠️ The positive control is still missing."),
    )
    try:
        assert "The lab's latest reply starts with ✅" not in calls[0]["messages"][0]["content"]
        assert thread.status == "active"
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_REFINING
        assert project.final is None
        assert await _decisions(factory, run_id) == []
        [post] = _thread_posts(sim)
        assert "project_json" not in post["text"]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_unreadable_close_ends_not_finished(engine, monkeypatch):
    raw = "<slack_message>✅ Final plan.</slack_message>\n<project_json>not json</project_json>"
    sim, _agent, thread, _calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=_history(11, lab_latest="✅ Agreed."),
    )
    try:
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_NOT_FINISHED
        assert project.reason_code == REASON_CLOSE_UNREADABLE
        assert thread.status == "closed"
        [decision] = await _decisions(factory, run_id)
        assert decision.outcome == "project_closed"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("as_lab", [False, True])
async def test_pause_marker_does_not_close_a_project_thread(engine, monkeypatch, as_lab):
    # The hub answers the lab's latest message (ordinal 8); the lab answers the hub's (9).
    history = _history(8) if as_lab else _history(7)
    sim, agent, thread, _calls, factory, run_id = await _drive_project(
        engine, monkeypatch, "<slack_message>⏸️ We cannot take this further.</slack_message>",
        history=history, as_lab=as_lab, configure=_wire_summary_channel,
    )
    try:
        assert thread.status == "active"
        assert await _decisions(factory, run_id) == []
        assert await _declines(factory, run_id) == []
        assert await _drops(factory, run_id) == []
        [project] = await _projects(factory, run_id)
        assert project.status == STATUS_REFINING
        assert len(_thread_posts(sim, agent.agent_id)) == 1
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_panel_notes_not_posted_in_project_threads(engine, monkeypatch):
    seen_kinds: list[str] = []

    async def _fake_execute_tool(name, tool_input, agent_id, thread, **kwargs):
        seen_kinds.append(kwargs["thread_kind"])
        await kwargs["on_consult_record"](
            domain="scientific", question="Is a statin arm the right positive control?",
            context_excerpt="the plan", verdict_signal="gap", confidence="medium",
            concerns=["dose"], questions_to_ask=[], raw_opinion="Use atorvastatin.",
        )
        return "opinion"

    monkeypatch.setattr(reply_lane, "execute_tool", _fake_execute_tool)
    notes: list[dict] = []

    async def _generate(**kwargs):
        await kwargs["tool_executor"]("consult_specialist", {"domain": "scientific"})
        return "<slack_message>The specialist suggests atorvastatin.</slack_message>"

    def configure(sim):
        async def _spy(*args, **kwargs):
            notes.append(kwargs)

        monkeypatch.setattr(sim.panel, "_post_panel_note", _spy)

    sim, _agent, _thread, _calls, factory, run_id = await _drive_project(
        engine, monkeypatch, None, history=_history(5), configure=configure, generate=_generate,
    )
    try:
        assert seen_kinds == ["project"]
        [consult] = await _consults(factory, run_id)
        assert consult.thread_id == ROOT and consult.subject_agent_id == "gordy"
        assert notes == []
        assert [p["text"] for p in _thread_posts(sim)] == [
            "The specialist suggests atorvastatin."
        ]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_abandon_in_a_project_thread_records_no_drop(engine, monkeypatch):
    import anthropic
    import httpx

    def _bad_request():
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        return anthropic.BadRequestError(
            message="400", response=httpx.Response(400, request=req), body=None,
        )

    async def _generate(**kwargs):
        raise _bad_request()

    sim, agent, thread, _calls, factory, run_id = await _drive_project(
        engine, monkeypatch, None, history=_history(5), generate=_generate,
    )
    try:
        assert thread.status == "active", "one failure only backs off"
        # Another call succeeds in between, so the second 400 on this thread abandons it.
        sim.ctx.circuit.record_success()
        await sim._reply_to_thread(agent, thread)
        assert thread.status == "closed"
        assert [d.outcome for d in await _decisions(factory, run_id)] == ["abandoned"]
        assert await _drops(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_missing_project_row_closes_the_thread(engine, monkeypatch):
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, "<slack_message>Hello.</slack_message>",
        history=_history(5), with_project=False,
    )
    try:
        assert calls == []
        assert _thread_posts(sim) == []
        assert thread.status == "closed"
        assert [d.outcome for d in await _decisions(factory, run_id)] == ["abandoned"]
        assert await _drops(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_finished_project_thread_closes_without_a_model_call(engine, monkeypatch):
    # A thread reopened after its project ended (a resume's reply-less-pitch scan, or a reply
    # linked after the tick ended the project) must not be refined, or held open.
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, "<slack_message>Hello.</slack_message>",
        history=_history(5), project_status=STATUS_NOT_FINISHED,
    )
    try:
        assert calls == []
        assert _thread_posts(sim) == []
        assert thread.status == "closed"
        assert [d.outcome for d in await _decisions(factory, run_id)] == ["abandoned"]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_unresolved_reply_claim_skips_the_turn(engine, monkeypatch):
    # The lab's reply went out but its link was never written (a crash, then failed reads):
    # the thread must wait for _resolve_project_claims, not be closed as abandoned.
    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, "<slack_message>Hello.</slack_message>",
        history=_history(5), project_status=STATUS_POSTED, project_reply_ts=None,
    )
    try:
        assert calls == []
        assert thread.status == "active"
        assert thread.has_pending_reply is True
        assert await _decisions(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_unreadable_project_skips_the_turn(engine, monkeypatch):
    async def _unreadable(thread_id):
        return None

    def configure(sim):
        monkeypatch.setattr(sim.projects, "_project_thread_context", _unreadable)

    sim, _agent, thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, "<slack_message>Hello.</slack_message>",
        history=_history(5), configure=configure,
    )
    try:
        assert calls == []
        assert _thread_posts(sim) == []
        assert thread.has_pending_reply is True
        assert thread.status == "active"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_project_thread_recognised_with_projects_not_running(engine, monkeypatch):
    raw = (
        "<slack_message>⏸️ Not a fit.</slack_message>\n"
        f"<assessment_json>{json.dumps(_verdict())}</assessment_json>"
    )
    running: list[bool] = []

    def configure(sim):
        running.append(sim.ctx.sessions.active(PROJECTS))

    sim, _agent, _thread, calls, factory, run_id = await _drive_project(
        engine, monkeypatch, raw, history=_history(7), configure=configure,
    )
    try:
        assert running == [False], "no Projects session runs in this engine"
        [call] = calls
        assert "BlackbirdBot in a project thread" in call["system_prompt"]
        assert call["log_meta"]["phase"] == "project_thread"
        assert await _assessments(factory, run_id) == []
        assert await _drops(factory, run_id) == []
        assert await _declines(factory, run_id) == []
        assert await _decisions(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)
