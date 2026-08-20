"""Durable capture of the specialist panel, and the floor that reads it back.

Three separate holes, all in one seam (design:
docs/plans/2026-08-20-assessments-rca-ux-specialist-visibility.md §3.1-3.2):

1. A consult existed only in engine memory (``_specialist_consults``) and in an
   ``llm_call_logs`` row with ``channel`` NULL. Nothing could show WHO was
   consulted about an interview or what they said, and the log row could not
   even be joined to the discussion it came from.
2. Because the map is in-memory, every verdict written after a restart was
   UNVERIFIABLE — ``missing_domains=[]`` however complete the panel had been.
   Production's normal exit is a SIGKILL, so that was the ordinary case.
3. Nothing recorded WHICH rubric a stored score was computed under.

These tests drive the real wiring: a real ``SimulationEngine`` + ``Agent`` +
``ThreadState`` through ``_reply_to_thread``, with only the two LLM seams faked,
so the assertions exercise the closure, the tool and the writer rather than a
re-description of them.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import LlmCallLog, OpportunityAssessment, SimulationRun, SpecialistConsult
from src.services.blackbird_rubric import (
    RUBRIC_CONTENT_HASH,
    RUBRIC_VERSION,
    RUBRIC_WEIGHTS,
)
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration

_OPINION_JSON = (
    '{"verdict_signal": "blocking", '
    '"concerns": ["The Baltimore animal-model license is third-party."], '
    '"questions_to_ask": ["Who owns the mouse line?"], '
    '"confidence": "high"}'
)

_QUESTION = "Is the mouse line encumbered by a third-party research-tool licence?"
_CONTEXT = "The PI said: 'we get the animals from a collaborator in Baltimore'."


# --- harness -----------------------------------------------------------------


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


async def _drive_a_consult(
    engine, monkeypatch, *, opinion=_OPINION_JSON, domain="legal",
    question=_QUESTION, context=_CONTEXT, channel="general", fail_the_record=False,
):
    """Run one mid-interview hub turn whose single tool call is a consult.

    ``message_count=5`` keeps this an ordinary interview turn (not the CONCLUDE
    turn), so no assessment machinery runs and the only thing under test is the
    consult path. ``opinion`` may be an ``Exception`` instance, which the faked
    specialist call raises instead of answering.

    ``fail_the_record`` replaces the engine's writer with one that raises — the
    only way to prove ``_execute_consult_specialist``'s own guard, since the
    real writer never raises.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)

    agent = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel=channel, other_agent_id="wang",
        message_count=5, has_pending_reply=True,
    )
    agent.state.active_threads["t1"] = thread
    sim = SimulationEngine(
        agents=[agent], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run_id,
    )
    if fail_the_record:
        async def _boom(*args, **kwargs):
            raise RuntimeError("the writer itself blew up")

        monkeypatch.setattr(sim, "_record_specialist_consult", _boom)

    # Bypass real prompt construction (profile files on disk) — this tests what
    # happens once the model calls a tool, not prompt building.
    monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))

    log_metas: list[dict] = []
    results: list[str] = []

    async def _fake_opinion(**kwargs):
        log_metas.append(kwargs.get("log_meta"))
        if isinstance(opinion, Exception):
            raise opinion
        return opinion

    async def _fake_reply(**kwargs):
        results.append(await kwargs["tool_executor"](
            "consult_specialist",
            {"domain": domain, "question": question, "context": context},
        ))
        return "<slack_message>Thanks — one more question.</slack_message>"

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _fake_opinion)
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)

    await sim._reply_to_thread(agent, thread)
    return SimpleNamespace(
        sim=sim, agent=agent, thread=thread, factory=factory, run_id=run_id,
        results=results, log_metas=log_metas,
    )


async def _consult_rows(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(SpecialistConsult)
            .where(SpecialistConsult.simulation_run_id == run_id)
            .order_by(SpecialistConsult.domain)
        )).scalars().all()


async def _assessment_rows(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(OpportunityAssessment)
            .where(OpportunityAssessment.simulation_run_id == run_id)
        )).scalars().all()


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)  # cascades to consults + assessments
            await cleanup.commit()


async def _insert_consults(factory, run_id, domains, *, subject="gordy", thread_id="t1"):
    """Rows exactly as ``_record_specialist_consult`` writes them — the state a
    restarted process finds waiting for it."""
    async with factory() as db:
        for domain in domains:
            db.add(SpecialistConsult(
                simulation_run_id=run_id, agent_id="blackbird",
                subject_agent_id=subject, thread_id=thread_id, channel_name="general",
                domain=domain, question="asked earlier, before the restart",
                context_excerpt="what the PI had said by then",
                verdict_signal="caution", confidence="moderate",
                concerns=["a concern"], questions_to_ask=["a question"],
                raw_opinion="the specialist's full reply",
            ))
        await db.commit()


# --- 1. a successful consult is recorded -------------------------------------


@pytest.mark.asyncio
async def test_a_successful_consult_writes_a_row_carrying_the_whole_exchange(
    engine, monkeypatch,
):
    """The mission pin: one successful consult, one durable row, joined to the
    interview it happened in — agent, subject, thread AND channel — with both
    the ask and the parsed opinion on it."""
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        rows = await _consult_rows(turn.factory, turn.run_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.agent_id == "blackbird"
        assert row.subject_agent_id == "wang"   # the engine's ground truth, not a guess
        assert row.thread_id == "t1"
        assert row.channel_name == "general"
        assert row.domain == "legal"
        assert row.question == _QUESTION
        assert row.context_excerpt == _CONTEXT
        assert row.verdict_signal == "blocking"
        assert row.confidence == "high"
        assert row.concerns == ["The Baltimore animal-model license is third-party."]
        assert row.questions_to_ask == ["Who owns the mouse line?"]
        # The reply exactly as the specialist wrote it, so a later parsing
        # change cannot lose the original.
        assert row.raw_opinion == _OPINION_JSON
        assert row.created_at is not None

        # The in-memory floor and the durable record agree, keyed on the same
        # (PI, interview) pair — the durable row is added to the in-memory
        # credit, never instead of it.
        assert turn.sim._consulted_domains("wang", "t1") == frozenset({"legal"})
        # The model still got its opinion back, unchanged by the write.
        assert turn.results[0].startswith("Legal Specialist — signal: blocking")
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_the_recorded_context_is_capped_not_unbounded(engine, monkeypatch):
    """`context` is model-written text on a Text column that a page reads back.
    It is excerpted; the full prompt stays in llm_call_logs."""
    turn = await _drive_a_consult(engine, monkeypatch, context="x" * 2500)
    try:
        rows = await _consult_rows(turn.factory, turn.run_id)
        assert len(rows) == 1
        assert len(rows[0].context_excerpt) == 2000
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_a_prose_opinion_still_records_with_the_degraded_defaults(
    engine, monkeypatch,
):
    """Prose IS an opinion (``has_usable_content``/``parse_opinion``), so it
    counts for the floor and must therefore also produce a row. What it cannot
    supply — a signal, a confidence — arrives already degraded from
    ``parse_opinion`` ("caution"/"low", never "clear"), and the row stores that
    rather than inventing anything."""
    turn = await _drive_a_consult(
        engine, monkeypatch,
        opinion="The mouse line is almost certainly encumbered. Ask them.",
    )
    try:
        rows = await _consult_rows(turn.factory, turn.run_id)
        assert len(rows) == 1
        assert rows[0].verdict_signal == "caution"
        assert rows[0].confidence == "low"
        assert rows[0].concerns == []
        assert rows[0].raw_opinion.startswith("The mouse line")
    finally:
        await _delete_run(turn.factory, turn.run_id)


# --- 2. a consult that did NOT happen is not recorded ------------------------


@pytest.mark.asyncio
async def test_an_empty_specialist_reply_records_nothing(engine, monkeypatch):
    """A row must mean "this domain counts as consulted". An empty reply is
    billed but does not satisfy the floor (``on_consult`` never fires), so it
    must not leave a row either — otherwise the table would read back an
    unreachable specialist as a convened one."""
    turn = await _drive_a_consult(engine, monkeypatch, opinion="   ")
    try:
        assert await _consult_rows(turn.factory, turn.run_id) == []
        assert turn.sim._consulted_domains("wang", "t1") == frozenset()
        assert "empty response" in turn.results[0]
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_a_failed_specialist_call_records_nothing(engine, monkeypatch):
    turn = await _drive_a_consult(
        engine, monkeypatch, opinion=RuntimeError("upstream 529"),
    )
    try:
        assert await _consult_rows(turn.factory, turn.run_id) == []
        assert turn.sim._consulted_domains("wang", "t1") == frozenset()
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_an_unknown_domain_records_nothing(engine, monkeypatch):
    """Refused before any API call, so there is nothing to record."""
    turn = await _drive_a_consult(engine, monkeypatch, domain="astrology")
    try:
        assert await _consult_rows(turn.factory, turn.run_id) == []
        assert turn.log_metas == [], "no specialist call was ever issued"
    finally:
        await _delete_run(turn.factory, turn.run_id)


# --- 3. the write is best-effort, in both directions -------------------------


@pytest.mark.asyncio
async def test_a_write_failure_costs_the_row_and_nothing_else(engine, monkeypatch, caplog):
    """Same contract as ``_record_assessment_drop``: never raises, ERROR on
    failure. Driven through the real turn so the failure has to travel back
    through the tool, the closure and the reply."""
    turn = await _drive_a_consult(engine, monkeypatch, fail_the_record=True)
    try:
        # The opinion reached the model verbatim. `execute_tool`'s outer handler
        # would otherwise have replaced it with "Error executing
        # consult_specialist: ..." — telling the model its consult failed while
        # the parsed opinion sat right there.
        assert turn.results[0].startswith("Legal Specialist — signal: blocking")
        assert "Error executing" not in turn.results[0]
        # In-memory stays authoritative in-process: a failed write must not
        # un-count a consult that really happened.
        assert turn.sim._consulted_domains("wang", "t1") == frozenset({"legal"})
        assert await _consult_rows(turn.factory, turn.run_id) == []
        assert "NOT durably" in caplog.text
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_the_writer_itself_never_raises_when_the_session_fails(caplog):
    def _boom():
        raise RuntimeError("database is gone")

    sim = SimulationEngine(
        agents=[], slack_clients={}, session_factory=_boom,
        simulation_run_id=uuid.uuid4(),
    )
    await sim._record_specialist_consult(
        "blackbird", subject_agent_id="wang", thread_id="t1", channel_name="general",
        domain="legal", question="q", context_excerpt="c",
        verdict_signal="clear", confidence="high", concerns=[], questions_to_ask=[],
        raw_opinion="o",
    )
    assert "Failed to record the legal consult" in caplog.text


@pytest.mark.asyncio
async def test_a_dbless_engine_records_silently_nothing(caplog):
    """The engine can run with no database at all (see __init__); every
    run-scoped write is a silent no-op in that mode, not an error."""
    sim = SimulationEngine(agents=[], slack_clients={})
    await sim._record_specialist_consult(
        "blackbird", subject_agent_id="wang", thread_id="t1", channel_name="general",
        domain="legal", question="q", context_excerpt=None,
        verdict_signal="clear", confidence="high", concerns=None, questions_to_ask=None,
        raw_opinion="o",
    )
    assert "Failed to record" not in caplog.text


# --- 4. the consult's own LLM log row carries the channel --------------------


@pytest.mark.asyncio
async def test_the_consult_log_row_carries_the_interview_channel(engine, monkeypatch):
    """Every ``consult_*`` row used to land with ``channel`` NULL, so a consult
    could not be joined to the discussion it was made during — the phase name
    gave the domain and nothing gave the thread.

    Asserted end to end on purpose: the key name in ``log_meta`` only matters
    because ``_flush_llm_logs`` reads exactly that key onto
    ``LlmCallLog.channel``, so the test pushes the captured metadata through the
    real writer instead of trusting the dict alone. ``completed_at`` is added
    here the way ``generate_agent_response`` adds it to its own callback
    payload (the faked specialist call never reaches that code).
    """
    turn = await _drive_a_consult(engine, monkeypatch, channel="chemical-biology")
    try:
        meta = turn.log_metas[0]
        assert meta["phase"] == "consult_legal"
        assert meta["agent_id"] == "blackbird"
        assert meta["channel"] == "chemical-biology"

        turn.sim._on_llm_call({**meta, "completed_at": datetime.now(UTC)})
        await turn.sim._flush_llm_logs()
        async with turn.factory() as db:
            logs = (await db.execute(
                select(LlmCallLog).where(LlmCallLog.simulation_run_id == turn.run_id)
            )).scalars().all()
        assert [(row.phase, row.channel) for row in logs] == [
            ("consult_legal", "chemical-biology")
        ]
    finally:
        await _delete_run(turn.factory, turn.run_id)


# --- 5. the rubric stamp ----------------------------------------------------


@pytest.mark.asyncio
async def test_persist_assessment_stamps_the_rubric_version_and_hash(engine):
    """A score is only comparable to another score computed under the same
    rubric, and a content hash catches an edit that shipped without a version
    bump."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    try:
        await sim._persist_assessment(
            "blackbird", "general",
            {"subject_agent_id": "wang", "scores": {"differentiation": 3}},
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].rubric_version == RUBRIC_VERSION
        assert rows[0].rubric_content_hash == RUBRIC_CONTENT_HASH
        # Not vacuously equal to None on both sides.
        assert rows[0].rubric_version and rows[0].rubric_content_hash
    finally:
        await _delete_run(factory, run_id)


# --- 6. the floor reads the table back after a restart ----------------------
#
# `_specialist_consults` dies with the process, so before this every verdict
# written after a restart was stored UNVERIFIED (`missing_domains=[]`) however
# thorough the panel had been. The fallback in `_persist_assessment` seeds the
# map from `specialist_consults` when — and only when — memory holds nothing
# for that (subject, thread).
#
# Every test below constructs the thread with floor_armed=False, exactly as
# `_rebuild_agent_state` does for a restart-restored thread, because that is
# the state the fallback exists for.

# scientific + talent are always required; "peptide" pulls in chemistry and
# "platform" pulls in technologic (src/agent/specialists.py cues). Spelled out
# rather than computed from required_domains_for so the expectation is a fact
# about this verdict, not a restatement of the code under test.
_REQUIRED = ("scientific", "talent", "chemistry", "technologic")

_VERDICT = {
    "subject_agent_id": "gordy",
    "recommendation": "conditional",
    "rationale": "A peptide-based vaccine platform for tuberculosis.",
    "scores": {k: 3 for k in RUBRIC_WEIGHTS},
}


def _restart_engine(factory, run_id):
    """An engine as it is right after a restart: no consults in memory at all."""
    sim = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    assert sim._specialist_consults == {}
    return sim


def _restored_thread():
    return ThreadState(
        thread_id="t1", channel="general", other_agent_id="gordy",
        message_count=11, floor_armed=False,
    )


@pytest.mark.asyncio
async def test_recorded_consults_make_a_post_restart_verdict_verifiable(engine):
    """The whole point: a complete panel recorded before the restart must read
    back as VERIFIED complete (missing_domains NULL), not as the unverified []
    that the same verdict produced before this fallback existed."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _insert_consults(factory, run_id, _REQUIRED)
    sim = _restart_engine(factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT, slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_incomplete is False
        assert rows[0].missing_domains is None, (
            "NULL is the VERIFIED-complete state; [] would mean the floor "
            "could not be checked, which is exactly what the recorded rows fix"
        )
        # The map was seeded for this interview, and the latch moved with it.
        assert sim._consulted_domains("gordy", "t1") == frozenset(_REQUIRED)
        assert thread.floor_armed is True
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_recorded_consults_are_scored_for_the_real_gap_not_waved_through(engine):
    """Seeding must feed the ordinary gap arithmetic, not bypass it. Only one of
    the four required domains was recorded, so the row must name the other
    three — where the pre-fallback behavior was to fail open and record no gap
    at all."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _insert_consults(factory, run_id, ("scientific",))
    sim = _restart_engine(factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT, slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_incomplete is True
        assert rows[0].missing_domains == ["chemistry", "talent", "technologic"]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_with_neither_memory_nor_rows_the_verdict_stays_unverified(engine):
    """Unchanged behavior where there is genuinely nothing to read: the row is
    stored, not flagged, and marked unverified — never NULL, which would be
    indistinguishable from a verified-complete panel."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim = _restart_engine(factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT, slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_incomplete is False
        assert rows[0].missing_domains == []
        assert sim._specialist_consults == {}, "nothing to seed, nothing seeded"
        assert thread.floor_armed is False, "an empty read must not arm the floor"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_another_interviews_rows_do_not_satisfy_this_one(engine):
    """The join is (run, subject, thread) — the same triple the in-memory map
    uses. A PI's OTHER interview is a second idea and owes its own panel
    (`_specialist_floor_gap`'s docstring: huganir was assessed 4 times and only
    the first faced a panel), and another PI's consults were never about this
    one."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _insert_consults(factory, run_id, _REQUIRED, thread_id="t-earlier")
    await _insert_consults(factory, run_id, _REQUIRED, subject="someone-else")
    sim = _restart_engine(factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT, slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].missing_domains == [], "still nothing recorded for THIS interview"
        assert sim._consulted_domains("gordy", "t1") == frozenset()
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_another_runs_rows_do_not_satisfy_this_run(engine):
    """A previous run's panel says nothing about this run's verdict."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    other_run_id = await _new_run(factory)
    run_id = await _new_run(factory)
    await _insert_consults(factory, other_run_id, _REQUIRED)
    sim = _restart_engine(factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT, slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].missing_domains == []
    finally:
        await _delete_run(factory, run_id)
        await _delete_run(factory, other_run_id)


@pytest.mark.asyncio
async def test_in_process_memory_stays_authoritative_when_it_holds_anything(engine):
    """The fallback is for a map with NOTHING for this interview. A process that
    recorded one domain and not the rest must keep reporting that gap — the
    table cannot overrule it, because the same success path writes both and
    memory can never be behind it.

    ``floor_armed=True`` here because this thread is NOT a restart case: the
    process has consulted, so the top-of-turn latch would have armed it.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _insert_consults(factory, run_id, _REQUIRED)
    sim = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    sim._record_consult("gordy", "scientific", "t1")
    thread = _restored_thread()
    thread.floor_armed = True
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT, slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].missing_domains == ["chemistry", "talent", "technologic"], (
            "the four recorded rows must NOT have been merged into a map that "
            "already held this interview's own record"
        )
        assert sim._consulted_domains("gordy", "t1") == frozenset({"scientific"})
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_failed_readback_costs_the_fallback_not_the_verdict(engine, caplog):
    """The fallback runs ahead of the write, inside a method whose whole
    contract is that the Slack post has already gone out. A SELECT that throws
    must degrade to today's unverified state, never take the row with it."""
    real_factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(real_factory)
    await _insert_consults(real_factory, run_id, _REQUIRED)

    calls = {"n": 0}

    def flaky_factory():
        calls["n"] += 1
        if calls["n"] == 1:  # the fallback's own SELECT
            raise RuntimeError("pool checkout timed out")
        return real_factory()

    sim = _restart_engine(flaky_factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT, slack_ts="1.1",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        assert "Failed to read back the consult record" in caplog.text
        assert sim._pending_assessments == [], "the write itself must have succeeded"
        rows = await _assessment_rows(real_factory, run_id)
        assert len(rows) == 1, "the verdict survives a failed read-back"
        assert rows[0].missing_domains == [], "degrades to unverified, not to a gap"
    finally:
        await _delete_run(real_factory, run_id)


@pytest.mark.asyncio
async def test_a_verdict_that_owes_no_panel_does_not_query_the_table(engine):
    """A ``pass`` is not held to the panel, so there is nothing for a read-back
    to inform. One session is opened for this call: the write."""
    real_factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(real_factory)
    calls = {"n": 0}

    def counting_factory():
        calls["n"] += 1
        return real_factory()

    sim = _restart_engine(counting_factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", {**_VERDICT, "recommendation": "pass"},
            slack_ts="1.1", subject_agent_id_fallback="gordy", thread=thread,
        )
        assert calls["n"] == 1, "no panel owed, so no read-back round trip"
        rows = await _assessment_rows(real_factory, run_id)
        assert len(rows) == 1
        assert rows[0].missing_domains is None, "no panel owed is not a failure to verify"
    finally:
        await _delete_run(real_factory, run_id)
