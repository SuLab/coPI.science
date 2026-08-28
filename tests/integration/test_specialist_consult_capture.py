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
4. Nothing in SLACK said a panel had been convened at all — §7 below. The
   durable row fixed the audit trail; a human watching an interview still saw
   the hub go quiet for 30-40 seconds per consult and then produce a verdict
   shaped by opinions nobody in the workspace could see.

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
from src.agent.message_log import PHASE_PANEL_NOTE, MessageLog
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.config import get_settings
from src.models import (
    AgentMessage,
    LlmCallLog,
    OpportunityAssessment,
    SimulationRun,
    SpecialistConsult,
)
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
    fail_the_note=False, stop_reason=None,
):
    """Run one mid-interview hub turn whose single tool call is a consult.

    ``message_count=5`` keeps this an ordinary interview turn (not the CONCLUDE
    turn), so no assessment machinery runs and the only thing under test is the
    consult path. ``opinion`` may be an ``Exception`` instance, which the faked
    specialist call raises instead of answering.

    ``fail_the_record`` replaces the engine's writer with one that raises — the
    only way to prove ``_execute_consult_specialist``'s own guard, since the
    real writer never raises.

    ``fail_the_note`` breaks the panel-note post and ONLY the panel-note post
    (keyed on the phase, so the turn's real reply still goes out through the
    real code path) — the same reason ``fail_the_record`` exists: the note post
    is best-effort and its guard has to be driven to be proven.

    ``stop_reason``, when given, is fired through the real ``on_stop_reason``
    callback that ``_execute_consult_specialist`` passes down — the only way to
    drive the truncation branch, since it is decided from that callback and not
    from the text. ``None`` (the default) fires nothing, which is what every
    other case here has always done.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)

    agent = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel=channel, other_agent_id="wang",
        message_count=5, has_pending_reply=True,
    )
    agent.state.active_threads["t1"] = thread
    client = FakeSlackClient(agent_id="blackbird")
    sim = SimulationEngine(
        agents=[agent], slack_clients={"blackbird": client},
        session_factory=factory, simulation_run_id=run_id,
    )
    # What `SimulationEngine.run` does during setup: the log's persist callback
    # is how an appended message ever becomes an `agent_messages` row. Registered
    # here so a test can drive `_flush_persisted` and read the real rows back
    # (nothing reaches the DB until it does, so this is inert for the tests that
    # never flush).
    sim.message_log.set_persist_callback(sim._enqueue_persist)
    if fail_the_record:
        async def _boom(*args, **kwargs):
            raise RuntimeError("the writer itself blew up")

        monkeypatch.setattr(sim, "_record_specialist_consult", _boom)

    if fail_the_note:
        real_post = sim._post_message

        async def _boom_on_notes(*args, **kwargs):
            if kwargs.get("phase") == PHASE_PANEL_NOTE:
                raise RuntimeError("Slack said no")
            return await real_post(*args, **kwargs)

        monkeypatch.setattr(sim, "_post_message", _boom_on_notes)

    # Bypass real prompt construction (profile files on disk) — this tests what
    # happens once the model calls a tool, not prompt building.
    monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))

    log_metas: list[dict] = []
    results: list[str] = []

    async def _fake_opinion(**kwargs):
        log_metas.append(kwargs.get("log_meta"))
        if isinstance(opinion, Exception):
            raise opinion
        if stop_reason is not None:
            on_stop = kwargs.get("on_stop_reason")
            if on_stop is not None:
                on_stop(stop_reason)
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
        results=results, log_metas=log_metas, client=client,
    )


async def _message_rows(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(AgentMessage)
            .where(AgentMessage.simulation_run_id == run_id)
            .order_by(AgentMessage.posted_at)
        )).scalars().all()


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


async def _insert_consults(
    factory, run_id, domains, *, subject="gordy", thread_id="t1", truncated=False,
):
    """Rows exactly as ``_record_specialist_consult`` writes them — the state a
    restarted process finds waiting for it.

    ``truncated`` defaults to ``False`` (a complete consult, the ordinary case).
    ``None`` is the third state, "written before migration 0036".
    """
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
                truncated=truncated,
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


# --- 1b. …and the row says whether the reply was CUT OFF ---------------------
#
# The column (migration 0036) exists so that a truncated consult stays refused
# across a restart: `tools.py` declines to credit it in-process, but the row it
# writes used to be byte-indistinguishable from a complete one, so
# `_seed_consults_from_db` rehydrated it as a domain that counts and the refusal
# was undone by the next `docker stop`.
#
# These are DB-backed on purpose. The unit tests
# (tests/unit/test_consult_accounting.py) pin the kwarg at the
# `on_consult_record` boundary, which proves the tool computes and sends the
# flag — and proves nothing about the last hop, engine -> `SpecialistConsult(...)`
# -> committed row. Deleting `truncated=truncated` from that constructor left
# the entire suite green; these are the tests that turn red.

_TRUNCATED_OPINION = (
    '{"verdict_signal": "blocking", "concerns": ["The Baltimore animal-model'
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_a_truncated_consult_is_marked_truncated_on_the_committed_row(
    engine, monkeypatch, stop_reason,
):
    """Both truncation stops, all the way to the column.

    `refusal` is the classifier cutting the reply and `max_tokens` is the ceiling
    doing it; the text in hand is equally partial, so the row must say so either
    way (`src.services.llm.is_truncated_stop`).
    """
    turn = await _drive_a_consult(
        engine, monkeypatch, opinion=_TRUNCATED_OPINION, stop_reason=stop_reason,
    )
    try:
        rows = await _consult_rows(turn.factory, turn.run_id)
        assert len(rows) == 1, "a truncated consult is still the only evidence it happened"
        assert rows[0].truncated is True
        assert rows[0].raw_opinion == _TRUNCATED_OPINION
        # The in-memory half of the same refusal, so the two cannot disagree:
        # the row exists AND the domain does not count.
        assert turn.sim._consulted_domains("wang", "t1") == frozenset()
        assert "truncated" in turn.results[0].lower()
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", None])
async def test_a_complete_consult_is_not_marked_truncated_on_the_committed_row(
    engine, monkeypatch, stop_reason,
):
    """The negative, and it is not "not True" — it is `False`.

    NULL in that column is a THIRD state, "written before 0036", which
    `_seed_consults_from_db` must keep reading as "not truncated" for the rows
    already in production. A live write that leaves it NULL would be
    indistinguishable from those, so the ordinary consult has to assert the flag
    positively. `stop_reason=None` covers the other real path: `on_stop_reason`
    is best-effort on the llm.py side and can legitimately never fire.
    """
    turn = await _drive_a_consult(engine, monkeypatch, stop_reason=stop_reason)
    try:
        rows = await _consult_rows(turn.factory, turn.run_id)
        assert len(rows) == 1
        assert rows[0].truncated is False
        assert turn.sim._consulted_domains("wang", "t1") == frozenset({"legal"})
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
    """A verdict that owes no panel has nothing for a read-back to inform. One
    session is opened for this call: the write.

    The scores are overridden as well as the recommendation, and that is the
    point. `_VERDICT`'s straight 3s band `conditional` on the investment scale,
    and the floor now keys on the COMPUTED band as well as the written
    recommendation — so a `pass` sitting on a conditional-banding score sheet
    DOES owe a panel. Exempting this call needs both halves to be unowed.
    """
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
            "blackbird", "general",
            {**_VERDICT, "recommendation": "pass",
             "scores": {k: 2 for k in RUBRIC_WEIGHTS}},
            slack_ts="1.1", subject_agent_id_fallback="gordy", thread=thread,
        )
        assert calls["n"] == 1, "no panel owed, so no read-back round trip"
        rows = await _assessment_rows(real_factory, run_id)
        assert len(rows) == 1
        assert rows[0].missing_domains is None, "no panel owed is not a failure to verify"
    finally:
        await _delete_run(real_factory, run_id)


# --- 7. the panel note in the interview thread -------------------------------
#
# The durable row above answers "what did the panel say?" for staff, after the
# fact. It says nothing IN SLACK, where the interview is actually happening.
# These tests pin the note that does, and — more importantly — pin that it
# changes nothing any agent reads or does. The exclusions are the feature; the
# post is the easy half.

_EXPECTED_NOTE = (
    '🧪 Panel · legal — ⛔ blocking — asked: '
    '"Is the mouse line encumbered by a third-party research-tool licence?"'
)


def _notes(client):
    return [p for p in client.posted if p["text"].startswith("🧪 Panel")]


@pytest.mark.asyncio
async def test_a_successful_consult_posts_one_thin_panel_note_in_the_thread(
    engine, monkeypatch,
):
    """The mission pin: one successful consult, one note, in the interview
    thread, under the hub's own identity, carrying the domain, the signal and
    the question — and NOTHING else the specialist said."""
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        notes = _notes(turn.client)
        assert len(notes) == 1
        note = notes[0]
        assert note["text"] == _EXPECTED_NOTE
        assert note["channel"] == "general"
        assert note["thread_ts"] == "t1", "in the interview thread, not the channel"

        # Chronologically honest: the note lands from inside the turn's tool
        # rounds, so it precedes the reply the consult informed.
        assert [p["text"] for p in turn.client.posted] == [
            _EXPECTED_NOTE, "Thanks — one more question.",
        ]

        # Signal-level ONLY. An interview thread is visible to every lab in the
        # workspace; the opinion paraphrases the PI's confidential statements
        # and the internal rubric.
        for withheld in (
            "Baltimore",                       # a concern
            "Who owns the mouse line?",        # a question_to_ask
            "high",                            # the confidence
            "verdict_signal",                  # any part of the raw opinion
        ):
            assert withheld not in note["text"], withheld
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_the_note_row_is_stamped_panel_note_not_thread_reply(
    engine, monkeypatch,
):
    """`phase` is the whole exclusion mechanism — in the DB (the staff pages'
    reply counts already key on 'thread_reply') and in memory (every
    agent-facing MessageLog read). If this column is wrong, nothing else in
    this section holds."""
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        await turn.sim._flush_persisted()
        rows = await _message_rows(turn.factory, turn.run_id)
        by_phase = {r.phase: r for r in rows}
        assert sorted(by_phase) == ["panel_note", "thread_reply"]
        note = by_phase[PHASE_PANEL_NOTE]
        assert note.content == _EXPECTED_NOTE
        assert note.thread_ts == "t1"
        assert note.channel_name == "general"
        assert note.agent_id == "blackbird", "posted as the hub, not anonymously"
        assert note.is_bot is True
        # The reply is untouched by any of this.
        assert by_phase["thread_reply"].content == "Thanks — one more question."
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_no_agent_can_see_the_note_it_just_posted(engine, monkeypatch):
    """The load-bearing half. A note must not enter a thread history, must not
    count toward the interview's turn budget, and must not read to the OTHER
    party as "the hub replied to you" — which is what would pull a lab bot into
    the reply lane mid-way through the hub's own turn."""
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        log = turn.sim.message_log
        # Both messages really are in the log — this is an exclusion at the
        # read, not a decision not to record.
        assert len(log) == 2

        history = log.get_thread_history("t1")
        assert [e.content for e in history] == ["Thanks — one more question."]
        assert log.get_thread_message_count("t1") == 1

        # The note came FIRST (from inside the tool rounds) and the reply
        # second, which is the point of posting at consult time — but it also
        # means the reply alone would satisfy every "is there something new
        # here" question asked of this log. So take the note the engine really
        # built and ask a log holding ONLY it: the PI's lab bot must not be told
        # the hub has spoken to it. (`test_a_panel_note_is_not_a_reply_trigger`
        # in tests/unit/test_simulation_logic.py drives the same claim through
        # `_pending_reply_pairs`, where it actually decides a turn.)
        note_entry = next(e for e in log._entries if e.phase == PHASE_PANEL_NOTE)
        reply_entry = next(e for e in log._entries if e.phase is None)
        assert note_entry.posted_at < reply_entry.posted_at
        assert log.has_new_reply_from_other("t1", "wang", 0.0) is True, (
            "the hub's actual reply still counts"
        )
        note_only = MessageLog()
        note_only.load_entry(note_entry)
        assert note_only.has_new_reply_from_other("t1", "wang", 0.0) is False
        assert note_only.get_thread_history("t1") == []
        assert note_only.get_thread_message_count("t1") == 0

        # The note is not one of the hub's own messages either.
        assert turn.agent.message_count == 1, "the reply, and only the reply"
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_the_flag_off_posts_no_note_and_costs_nothing_else(
    engine, monkeypatch,
):
    """`panel_notes_in_thread` is read at post time, so an operator turns notes
    off with a `.env` edit and a container recreate — no rebuild. Everything
    else about the consult is unaffected."""
    monkeypatch.setattr(get_settings(), "panel_notes_in_thread", False)
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        assert _notes(turn.client) == []
        assert [p["text"] for p in turn.client.posted] == ["Thanks — one more question."]
        await turn.sim._flush_persisted()
        rows = await _message_rows(turn.factory, turn.run_id)
        assert [r.phase for r in rows] == ["thread_reply"]
        # The consult itself is untouched: recorded, credited, answered.
        assert len(await _consult_rows(turn.factory, turn.run_id)) == 1
        assert turn.sim._consulted_domains("wang", "t1") == frozenset({"legal"})
        assert turn.results[0].startswith("Legal Specialist — signal: blocking")
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_a_consult_that_did_not_happen_posts_no_note(engine, monkeypatch):
    """Same contract as the durable row: the note fires on the SUCCESS path
    only. A note for an unreachable specialist would tell the workspace a panel
    was convened when it was not."""
    for kwargs in (
        {"opinion": "   "},                            # billed, but said nothing
        {"opinion": RuntimeError("upstream 529")},      # never answered
        {"domain": "astrology"},                        # refused before the call
    ):
        turn = await _drive_a_consult(engine, monkeypatch, **kwargs)
        try:
            assert _notes(turn.client) == [], kwargs
            assert await _consult_rows(turn.factory, turn.run_id) == [], kwargs
        finally:
            await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_a_failed_note_post_costs_the_note_and_nothing_else(
    engine, monkeypatch, caplog,
):
    """Best-effort, in exactly the sense the durable write is: the consult
    stands, the opinion reaches the model verbatim, the row is written, the
    floor is credited and the turn's reply still posts. Only the trace is
    missing, and it says so at ERROR."""
    turn = await _drive_a_consult(engine, monkeypatch, fail_the_note=True)
    try:
        assert _notes(turn.client) == []
        assert [p["text"] for p in turn.client.posted] == ["Thanks — one more question."]
        # The opinion is unchanged — `execute_tool`'s outer handler would
        # otherwise have replaced it with "Error executing consult_specialist".
        assert turn.results[0].startswith("Legal Specialist — signal: blocking")
        assert "Error executing" not in turn.results[0]
        assert len(await _consult_rows(turn.factory, turn.run_id)) == 1
        assert turn.sim._consulted_domains("wang", "t1") == frozenset({"legal"})
        assert "Failed to post the legal panel note" in caplog.text
        # NOT the durable writer's message: the record succeeded.
        assert "NOT durably" not in caplog.text
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_a_restart_does_not_resurrect_a_note_as_conversation(engine):
    """The exclusions are only as durable as `LogEntry.phase` surviving the
    round trip through `agent_messages`. Rebuild a fresh engine from the rows a
    previous process left and the note must still be invisible."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    # The note is LAST on purpose: it models the process dying after a consult
    # in a turn whose reply never landed, which is the one arrangement where the
    # note is the newest thing in the thread and could therefore, on its own,
    # look like a reply owed an answer.
    async with factory() as db:
        for ts, phase, content in (
            ("100.1", "new_post", "Here is our idea."),
            ("100.2", "thread_reply", "Tell me about the mouse line."),
            ("100.3", PHASE_PANEL_NOTE, _EXPECTED_NOTE),
        ):
            db.add(AgentMessage(
                simulation_run_id=run_id,
                agent_id="wang" if phase == "new_post" else "blackbird",
                channel_id="local:general", channel_name="general",
                message_ts=ts, message_length=len(content),
                thread_ts=None if phase == "new_post" else "100.1",
                phase=phase, content=content,
                sender_name="WangBot" if phase == "new_post" else "BlackbirdBot",
                is_bot=True, posted_at=float(ts),
            ))
        await db.commit()

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim = SimulationEngine(
        agents=[hub], slack_clients={},
        session_factory=factory, simulation_run_id=run_id,
    )
    try:
        await sim._rebuild_state_from_db()
        log = sim.message_log
        assert len(log) == 3, "all three rows are loaded"
        assert [e.content for e in log.get_thread_history("100.1")] == [
            "Here is our idea.", "Tell me about the mouse line.",
        ]
        assert log.get_thread_message_count("100.1") == 2
        assert log.has_new_reply_from_other("100.1", "wang", 100.15) is True, (
            "the hub's real reply, at 100.2"
        )
        assert log.has_new_reply_from_other("100.1", "wang", 100.25) is False, (
            "past the reply the only thing left in the thread is the note at "
            "100.3 — and a restored note is still not a reply"
        )
        # And the same thread hydrated on demand (the reopen path) agrees.
        sim.message_log = MessageLog()
        await sim._hydrate_thread_from_db("100.1")
        assert sim.message_log.get_thread_message_count("100.1") == 2
    finally:
        await _delete_run(factory, run_id)


# --- 7b. clip-rate drift bookkeeping (specialists.clip_rate_warning) --------
#
# `_post_panel_note` is also where the note's question gets clipped to
# `PANEL_NOTE_QUESTION_CHARS`, so it is where the engine has to notice when
# that calibration has decayed. These drive `_post_panel_note` directly, on
# the same `sim`/`agent`/`thread` a real consult already exercised above, to
# pile up enough posts to cross `clip_rate_warning`'s sample floor without
# re-running the whole tool-call machinery per note.

_LONG_QUESTION = "Is the animal model encumbered by a third-party licence? " * 20


@pytest.mark.asyncio
async def test_the_clip_counters_increment_on_every_successful_note(
    engine, monkeypatch,
):
    """The counters `clip_rate_warning` is judged against are driven from
    what `_post_panel_note` actually posted — not re-derived — so they can
    never disagree with the note in the thread."""
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        assert turn.sim._panel_notes_posted == 1, "the fixture's own consult"
        assert turn.sim._panel_notes_clipped == 0, "the fixture question is short"

        await turn.sim._post_panel_note(
            "blackbird", channel="general", thread_ts="t1", domain="legal",
            question=_LONG_QUESTION, verdict_signal="blocking",
        )
        assert turn.sim._panel_notes_posted == 2
        assert turn.sim._panel_notes_clipped == 1

        # A note that never posts (no channel) must not count either way.
        await turn.sim._post_panel_note(
            "blackbird", channel=None, thread_ts="t1", domain="legal",
            question=_LONG_QUESTION, verdict_signal="blocking",
        )
        assert turn.sim._panel_notes_posted == 2
        assert turn.sim._panel_notes_clipped == 1
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_the_clip_rate_warning_logs_exactly_once_past_the_threshold(
    engine, monkeypatch, caplog,
):
    """`clip_rate_warning` stays quiet below its 20-note floor and its 10%
    ceiling (see tests/unit/test_specialists.py); this crosses both through
    the engine's own counters and checks the log fires exactly once even
    though further clipped notes keep the rate crossed."""
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        # 1 short (unclipped) note already posted by the fixture. 18 more
        # short notes clear the sample floor (19 total) while keeping the
        # rate at 0%.
        for _ in range(18):
            await turn.sim._post_panel_note(
                "blackbird", channel="general", thread_ts="t1", domain="legal",
                question=_QUESTION, verdict_signal="blocking",
            )
        assert turn.sim._panel_notes_posted == 19
        assert turn.sim._panel_note_clip_warned is False

        caplog.clear()
        # 5 clipped notes: the 3rd (22 total, 3 clipped, 13.6%) is the first
        # to clear the 10% ceiling; the 4th and 5th keep it crossed.
        for _ in range(5):
            await turn.sim._post_panel_note(
                "blackbird", channel="general", thread_ts="t1", domain="legal",
                question=_LONG_QUESTION, verdict_signal="blocking",
            )
        assert turn.sim._panel_notes_posted == 24
        assert turn.sim._panel_notes_clipped == 5

        decay_warnings = [
            r for r in caplog.records if "calibration has decayed" in r.getMessage()
        ]
        assert len(decay_warnings) == 1, (
            "logs once per run even though the rate stays past the ceiling"
        )
        assert "22" in decay_warnings[0].getMessage(), "the first crossing's own tally"
        assert turn.sim._panel_note_clip_warned is True
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_the_consult_seed_runs_for_a_band_owed_verdict(engine):
    """The seed has to ask the SAME question the floor asks, or it starves
    exactly the verdicts the floor holds to the panel.

    `_seed_consults_from_db` opened with `recommendation not in
    _PANEL_REQUIRED_FOR` (an engine alias since deleted for want of readers) —
    the recommendation-only rule the floor abandoned. A
    verdict the model wrote `pass` on but that COMPUTES into the `conditional`
    band is owed a panel, so `_specialist_floor_gap` runs for it; the seed
    skipped it, so after a restart the map stayed empty, the floor found nothing
    recorded, and the row was stamped `panel_incomplete=true` naming domains that
    ARE in `specialist_consults`. Reproduced in production: four named domains,
    three of them recorded as consulted on that very thread.

    Straight 3s band `conditional` on the investment scale, so this fixture is
    owed a panel by its band alone.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _insert_consults(factory, run_id, _REQUIRED)
    sim = _restart_engine(factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general",
            {**_VERDICT, "recommendation": "pass"},
            slack_ts="1.1", subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_incomplete is False, (
            "every required domain is recorded for this very thread; flagging a "
            "gap here accuses the hub of skipping a panel it convened"
        )
        assert rows[0].missing_domains is None
        assert sim._consulted_domains("gordy", "t1") == frozenset(_REQUIRED)
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_truncated_consult_does_not_satisfy_the_floor_after_a_restart(engine):
    """The refusal has to survive a `docker stop`.

    `src/agent/tools.py` declines to credit a truncated opinion in memory — the
    text is a half-sentence and nothing read it — but it still writes the row,
    because that row is the only evidence the attempt happened. Rehydration then
    read every row as a consult, so the next restart quietly undid the refusal
    and an unread specialist became a consulted one.

    Four required domains: three complete, one truncated. Only the truncated
    domain may come back as a gap.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _insert_consults(factory, run_id, ("scientific", "talent", "chemistry"))
    await _insert_consults(factory, run_id, ("technologic",), truncated=True)
    sim = _restart_engine(factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT,
            slack_ts="1.1", subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_incomplete is True
        assert rows[0].missing_domains == ["technologic"], (
            "the truncated consult must not count; the three complete ones must"
        )
        assert sim._consulted_domains("gordy", "t1") == frozenset(
            {"scientific", "talent", "chemistry"}
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_pre_migration_consult_still_satisfies_the_floor(engine):
    """The other side of the same filter, and the reason it is `IS NOT TRUE`
    rather than `= False`.

    Every `specialist_consults` row written before migration 0036 carries NULL
    in that column — "not stated" — and those consults really did happen and
    really were read. A `truncated = False` filter would silently invalidate all
    of them on no evidence whatever, turning a live production panel record into
    a run of `panel_incomplete` accusations.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _insert_consults(factory, run_id, _REQUIRED, truncated=None)
    sim = _restart_engine(factory, run_id)
    thread = _restored_thread()
    try:
        await sim._persist_assessment(
            "blackbird", "general", _VERDICT,
            slack_ts="1.1", subject_agent_id_fallback="gordy", thread=thread,
        )
        rows = await _assessment_rows(factory, run_id)
        assert len(rows) == 1
        assert rows[0].panel_incomplete is False
        assert rows[0].missing_domains is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_a_truncated_consult_posts_no_caution_note(
    engine, monkeypatch, stop_reason,
):
    """The note is a claim, and a truncated consult has nothing to claim.

    `_post_panel_note`'s signature ends in `**_withheld`, which is deliberate —
    it means a field added to the consult-record contract is withheld from Slack
    by DEFAULT. It also meant the new `truncated` flag was silently absorbed, so
    the note kept publishing the DEFAULTED `⚠️ caution` signal — a parse failure
    rendered as a specialist's opinion — into the PI's own interview thread,
    where every lab in the workspace can read it.

    The durable row is still written (it is the only evidence the attempt
    happened); only the workspace-visible claim is skipped.
    """
    turn = await _drive_a_consult(
        engine, monkeypatch, opinion=_TRUNCATED_OPINION, stop_reason=stop_reason,
    )
    try:
        assert _notes(turn.client) == [], (
            "a truncated opinion must not be published as a panel signal"
        )
        rows = await _consult_rows(turn.factory, turn.run_id)
        assert len(rows) == 1 and rows[0].truncated is True, (
            "the durable record still happens — only the note is skipped"
        )
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_a_complete_consult_still_posts_its_note(engine, monkeypatch):
    """The control for the test above: skipping every note would satisfy it."""
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        assert _notes(turn.client) != []
    finally:
        await _delete_run(turn.factory, turn.run_id)


# --- 7c. `read_state` generalises the truncation cancellation ----------------
#
# A note is a workspace-visible statement in the PI's own interview thread. It
# must not assert a verdict that was DEFAULTED rather than read — the same
# reason `truncated` already cancels it (§7 above), just not the only reason
# any more: a reply can arrive COMPLETE and still fail to parse, and that is
# just as unread as a truncation. `_post_panel_note` is driven directly here
# (as the clip-rate tests in §7b already do), reusing the `sim`/`agent`/client
# a real consult set up, because the claim under test is about the parameter
# itself, not about wiring a fresh engine.


@pytest.mark.parametrize(
    "read_state, expect_note",
    [("parsed", True), ("defaulted", False), ("truncated", False)],
)
@pytest.mark.asyncio
async def test_only_a_read_opinion_reaches_the_thread(
    engine, monkeypatch, read_state, expect_note,
):
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        turn.client.posted.clear()  # isolate this call from the fixture's own note
        await turn.sim._post_panel_note(
            "blackbird", channel="general", thread_ts="t1",
            domain="chemistry", question="is the route scalable?",
            verdict_signal="caution", read_state=read_state,
        )
        assert bool(_notes(turn.client)) is expect_note
    finally:
        await _delete_run(turn.factory, turn.run_id)


@pytest.mark.asyncio
async def test_a_missing_read_state_still_posts(engine, monkeypatch):
    """``None`` means "this caller predates the field", not "unread". Failing
    closed here would silently stop every note the moment a caller was missed
    rather than updated — exactly the failure mode `**_withheld` elsewhere
    exists to avoid."""
    turn = await _drive_a_consult(engine, monkeypatch)
    try:
        turn.client.posted.clear()
        await turn.sim._post_panel_note(
            "blackbird", channel="general", thread_ts="t1",
            domain="chemistry", question="q", verdict_signal="caution",
        )
        assert _notes(turn.client)
    finally:
        await _delete_run(turn.factory, turn.run_id)
