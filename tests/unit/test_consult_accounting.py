"""A specialist consult is a real, billed Opus call and must be booked as one.

`Agent.record_api_call`'s own docstring states the invariant: "Every call site
must use this rather than bumping ``api_call_count`` directly — a site that bumps
only the counter is invisible to the rate limiter". `_execute_consult_specialist`
called `generate_agent_response` without booking anything at all, so up to eight
consults per concluding reply were invisible to both the sliding-window limiter
and `SimulationRun.total_api_calls`.

That matters most exactly where the spend is highest: the hub is the only role
with `consult_specialist`, it is the agent the limiter is meant to pace, and the
mandatory-consult rules mean a strong verdict pulls in more consults, not fewer.
"""

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.agent.tools import _execute_consult_specialist
from tests.fakes import FakeAnthropic, FakeSlackClient

_OPINION = """VERDICT SIGNAL: proceed
CONFIDENCE: moderate

The mechanism is plausible and the chemistry path is not obviously blocked.
"""


def _hub_engine():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang",
        message_count=5, has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    engine = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    return engine, hub, thread


@pytest.mark.asyncio
async def test_a_consult_is_booked_against_the_rate_limiter(monkeypatch):
    engine, hub, thread = _hub_engine()

    async def _fake_opinion(**kwargs):
        return _OPINION

    async def _fake_reply(**kwargs):
        # One tool round: the hub consults chemistry, then answers.
        await kwargs["tool_executor"](
            "consult_specialist",
            {"domain": "chemistry", "question": "q", "context": "c"},
        )
        return "<slack_message>Thanks — one more question.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)
    monkeypatch.setattr("src.agent.tools.generate_agent_response", _fake_opinion)

    await engine._reply_to_thread(hub, thread)

    # The reply itself, plus the consult it made. Booking only the reply is what
    # let the hub outrun its own allowance.
    assert hub.api_call_count == 2, (
        f"expected the reply + 1 consult to be booked, got {hub.api_call_count}"
    )
    # And the consult must still be recorded for the specialist floor, keyed
    # under this interview's own thread — not the PI alone (see task 6:
    # a PI's second interview must not inherit a first interview's consults).
    assert engine._consulted_domains("wang", thread.thread_id) == frozenset({"chemistry"})


@pytest.mark.asyncio
async def test_the_on_consult_closure_forwards_the_signal_into_the_run_tally(monkeypatch):
    """The `on_consult` closure built inside `_reply_to_thread` (Task 9) takes
    TWO arguments now — domain and the parsed verdict_signal — and must land
    both in `_note_consult`: the domain into the per-interview floor map, and
    the signal into the per-run `_consult_signal_counts` tally the clear-rate
    monitor in `stop()` reads. A one-argument closure (the pre-Task-9 shape)
    would TypeError the moment `execute_tool` calls it with two arguments."""
    engine, hub, thread = _hub_engine()

    async def _fake_opinion(**kwargs):
        return (
            '{"verdict_signal": "clear", "concerns": [], '
            '"questions_to_ask": [], "confidence": "high"}'
        )

    async def _fake_reply(**kwargs):
        await kwargs["tool_executor"](
            "consult_specialist",
            {"domain": "legal", "question": "q", "context": "c"},
        )
        return "<slack_message>Thanks — that clears it up.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)
    monkeypatch.setattr("src.agent.tools.generate_agent_response", _fake_opinion)

    await engine._reply_to_thread(hub, thread)

    assert engine._consulted_domains("wang", thread.thread_id) == frozenset({"legal"})
    assert engine._consult_signal_counts == {"clear": 1}


@pytest.mark.asyncio
async def test_a_consult_appends_to_the_sliding_window_ledger(monkeypatch):
    """Fix round 1 (Ruling R5): booking a consult against api_call_count is
    not enough — it must also land in call_times, or the limiter's coverage
    silently narrows to just the two reserved call sites and a hub that fires
    consults all day never looks throttled for them."""
    engine, hub, thread = _hub_engine()

    async def _fake_opinion(**kwargs):
        return _OPINION

    async def _fake_reply(**kwargs):
        await kwargs["tool_executor"](
            "consult_specialist",
            {"domain": "chemistry", "question": "q", "context": "c"},
        )
        return "<slack_message>Thanks — one more question.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)
    monkeypatch.setattr("src.agent.tools.generate_agent_response", _fake_opinion)

    await engine._reply_to_thread(hub, thread)

    # try_reserve appends once for the reply itself; record_api_call's
    # default (already_reserved=False) must append a second time for the
    # consult, which was never separately reserved.
    assert len(hub.state.call_times) == 2, (
        f"expected reply + consult both in the ledger, got {len(hub.state.call_times)}"
    )


@pytest.mark.asyncio
async def test_a_truncation_retry_appends_to_the_sliding_window_ledger(monkeypatch):
    """The on_retry hook passed into generate_with_tools is agent.record_api_call
    — a second real billed call for a turn that already reserved once. It must
    still land in call_times, or a heavily-retried agent looks artificially
    under its allowance."""
    engine, hub, thread = _hub_engine()

    async def _fake_reply(**kwargs):
        # Simulate generate_with_tools detecting a max_tokens truncation and
        # firing its second, billed call.
        kwargs["on_retry"]()
        return "<slack_message>Concluding.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)

    await engine._reply_to_thread(hub, thread)

    assert hub.api_call_count == 2, "expected the reply + the retry to be booked"
    assert len(hub.state.call_times) == 2, (
        f"expected reply + retry both in the ledger, got {len(hub.state.call_times)}"
    )


@pytest.mark.asyncio
async def test_every_consult_in_a_turn_is_booked(monkeypatch):
    engine, hub, thread = _hub_engine()

    async def _fake_opinion(**kwargs):
        return _OPINION

    async def _fake_reply(**kwargs):
        for domain in ("scientific", "talent", "chemistry", "clinical"):
            await kwargs["tool_executor"](
                "consult_specialist",
                {"domain": domain, "question": "q", "context": "c"},
            )
        return "<slack_message>Concluding.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)
    monkeypatch.setattr("src.agent.tools.generate_agent_response", _fake_opinion)

    await engine._reply_to_thread(hub, thread)

    assert hub.api_call_count == 5, (
        f"expected the reply + 4 consults, got {hub.api_call_count}"
    )


@pytest.mark.asyncio
async def test_a_failed_consult_is_not_booked(monkeypatch):
    """An unknown domain never reaches the API, so it must not be charged —
    the same reasoning that keeps it from satisfying the specialist floor."""
    engine, hub, thread = _hub_engine()

    async def _fake_reply(**kwargs):
        await kwargs["tool_executor"](
            "consult_specialist",
            {"domain": "astrology", "question": "q", "context": "c"},
        )
        return "<slack_message>Never mind.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)

    await engine._reply_to_thread(hub, thread)

    assert hub.api_call_count == 1, "an unknown domain made no API call to charge"
    assert engine._consulted_domains("wang", thread.thread_id) == frozenset()


@pytest.mark.asyncio
async def test_an_empty_specialist_reply_is_billed_but_not_counted(monkeypatch):
    """The two callbacks must disagree: the call happened and is billed, but
    it produced no opinion and must not satisfy the floor."""
    consulted, billed = [], []

    async def _empty(*args, **kwargs):
        return "   "

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _empty)

    result = await _execute_consult_specialist(
        "chemistry", "Is the series tractable?", "The PI said little.",
        agent_id="blackbird",
        on_consult=lambda domain, signal: consulted.append(domain),
        on_api_call=lambda: billed.append(1),
    )

    assert billed, "a call that was issued is billed whatever it returned"
    assert consulted == [], "an empty reply must not satisfy the floor"
    assert "empty response" in result


# --- refusal-truncated consults ---------------------------------------------
#
# `stop_reason` was compared against "max_tokens" at nine sites in llm.py and
# branched on NOWHERE else in src/. So a `refusal` — the SDK's own word for a
# reply the model stopped mid-sentence — was indistinguishable from a complete
# answer at every call site. Measured over run 8b64a0e0: 47 refusals (21
# recorded + 26 that wrote no row at all), 3 of them specialist consults, all 3
# credited to the panel while contributing zero concerns and zero questions,
# all 3 published into the PI's own thread as "⚠️ caution". markham's verdict
# rested on that panel.
#
# See docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md, H4/H5.


@pytest.mark.asyncio
async def test_a_refusal_truncated_consult_is_recorded_but_not_credited(monkeypatch):
    """Three callbacks, three different questions, and they must disagree here.

    - `on_api_call`  — "was this billed?"            yes, it was.
    - `on_consult_record` — "what did we get?"       write it; a truncated
      opinion is the only evidence the attempt happened at all, and it is what
      a backfill or an audit reads.
    - `on_consult`   — "does this satisfy the floor?" NO. A specialist that was
      cut off mid-array has not cleared, cautioned or blocked anything.
    """
    consulted, billed, recorded = [], [], []

    async def _refused(**kwargs):
        # The pinned contract from src/services/llm.py: invoked exactly once,
        # with the FINAL call's stop_reason, before the text is returned.
        on_stop_reason = kwargs.get("on_stop_reason")
        if on_stop_reason is not None:
            on_stop_reason("refusal")
        return '{"verdict_signal": "blocking", "concerns": ["The dose-response is not'

    async def _record(**fields):
        recorded.append(fields)

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _refused)

    result = await _execute_consult_specialist(
        "chemistry", "Is the series tractable?", "The PI said a great deal.",
        agent_id="blackbird",
        on_consult=lambda domain, signal: consulted.append(domain),
        on_consult_record=_record,
        on_api_call=lambda: billed.append(1),
    )

    assert billed, "the call was issued and is billed"
    assert consulted == [], "a truncated consult must not satisfy the floor"
    assert len(recorded) == 1, "the durable record is the whole point of C1.3"
    assert recorded[0]["domain"] == "chemistry"
    assert recorded[0]["raw_opinion"].endswith("The dose-response is not")
    # The model is told, in the string it reads, that this one did not land —
    # otherwise it consults once, believes the domain is covered, and concludes.
    assert "truncated" in result.lower()


@pytest.mark.asyncio
async def test_a_max_tokens_truncated_consult_is_not_credited(monkeypatch):
    """`refusal` was never the only way a consult gets cut off.

    The local test here was `stop_reasons[-1] == "refusal"`, so a consult that
    hit the 4000-token ceiling, retried, and STILL truncated reported
    `max_tokens` and was credited to the specialist floor as a complete opinion
    — a partial reply satisfying the domain the verdict is checked against.
    `src.services.llm.is_truncated_stop` is the single definition of "the text
    in hand is incomplete", and this call site must use it rather than its own
    one-value copy.
    """
    consulted, recorded = [], []

    async def _still_truncated(**kwargs):
        on_stop_reason = kwargs.get("on_stop_reason")
        if on_stop_reason is not None:
            # The retry ran, doubled max_tokens and truncated again: llm.py
            # returns the retry's text and reports the RETRY's stop_reason.
            on_stop_reason("max_tokens")
        return '{"verdict_signal": "clear", "confidence": "high", "concerns": ['

    async def _record(**fields):
        recorded.append(fields)

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _still_truncated)

    result = await _execute_consult_specialist(
        "chemistry", "Is the series tractable?", "The PI said a great deal.",
        agent_id="blackbird",
        on_consult=lambda domain, signal: consulted.append(domain),
        on_consult_record=_record,
    )

    assert consulted == [], "a max_tokens-truncated consult must not satisfy the floor"
    assert len(recorded) == 1, "it still happened, so it is still recorded"
    assert "truncated" in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("refusal", True), ("max_tokens", True), ("end_turn", False), (None, False)],
)
async def test_a_truncated_consult_is_marked_on_the_record(
    monkeypatch, stop_reason, expected
):
    """The stored row must be able to say it was cut off.

    Without this the `specialist_consults` row of a truncated consult is
    byte-indistinguishable from a complete one, so `_seed_consults_from_db`
    rehydrates it after a restart as a domain that counts — the in-process
    refusal above is undone by the next `docker stop`. `specialist_consults
    .truncated` (migration 0036) is where this lands; NULL there means "written
    before the column existed", which is why the value is always sent, `False`
    included.
    """
    recorded = []

    async def _reply(**kwargs):
        on_stop_reason = kwargs.get("on_stop_reason")
        if on_stop_reason is not None and stop_reason is not None:
            on_stop_reason(stop_reason)
        return '{"verdict_signal": "clear", "confidence": "high"}'

    async def _record(**fields):
        recorded.append(fields)

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _reply)

    await _execute_consult_specialist(
        "chemistry", "Is the series tractable?", "The PI said a great deal.",
        agent_id="blackbird",
        on_consult_record=_record,
    )

    assert len(recorded) == 1
    assert recorded[0]["truncated"] is expected


@pytest.mark.asyncio
async def test_a_complete_consult_that_stopped_on_end_turn_is_still_credited(monkeypatch):
    """The other side of the branch. `end_turn` is the normal terminator and
    must not be caught up in this."""
    consulted = []

    async def _complete(**kwargs):
        on_stop_reason = kwargs.get("on_stop_reason")
        if on_stop_reason is not None:
            on_stop_reason("end_turn")
        return '{"verdict_signal": "clear", "confidence": "high"}'

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _complete)

    await _execute_consult_specialist(
        "chemistry", "Is the series tractable?", "The PI said a great deal.",
        agent_id="blackbird",
        on_consult=lambda domain, signal: consulted.append(domain),
    )
    assert consulted == ["chemistry"]


@pytest.mark.asyncio
async def test_a_consult_whose_stop_reason_never_arrives_is_credited(monkeypatch):
    """`on_stop_reason` is additive and best-effort on the llm.py side: it is
    wrapped so it can never raise into the caller, which also means it can fail
    to fire. A missing stop_reason must degrade to today's behaviour — crediting
    a consult that returned a usable opinion — and not to a silent floor
    failure on every consult in the system."""
    consulted = []

    async def _no_signal(**kwargs):
        return '{"verdict_signal": "clear", "confidence": "high"}'

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _no_signal)

    await _execute_consult_specialist(
        "chemistry", "Is the series tractable?", "The PI said a great deal.",
        agent_id="blackbird",
        on_consult=lambda domain, signal: consulted.append(domain),
    )
    assert consulted == ["chemistry"]


# --- label placement --------------------------------------------------------
#
# Task 6: the label ("<Title> — signal: X") must follow the opinion body, not
# precede it, in the string the hub reads. A verdict word already in context
# anchors the hub's own subsequent reasoning: anchoring on a score already in
# context reaches Cohen's d = 0.71 and is NOT removable by instruction
# (arXiv:2608.25869), while generating evidence before rating is worth +6 to
# +11 accuracy points (arXiv:2305.17926).


@pytest.mark.asyncio
async def test_the_hub_reads_the_opinion_before_it_reads_the_label(monkeypatch):
    """Pin placement on a marker unique to the LABEL ("— signal:"), not on the
    bare verdict word "blocking": the specialist's own raw JSON already names
    its ``verdict_signal`` near the top of ``opinion.raw`` (schema order, not
    this function's doing), so a bare "blocking" shows up early in the string
    either way this function assembles it. What actually moves between the
    old and new implementation is the em-dash label itself — this is why the
    assertion below compares `unscalable` (a body word) against `"— signal:"`
    (the label's own marker), not against `blocking`."""
    fake = FakeAnthropic([
        '{"verdict_signal": "blocking", "concerns": ["the route is unscalable"],'
        ' "questions_to_ask": [], "confidence": "high"}'
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await _execute_consult_specialist(
        "chemistry", "is the route scalable?", "PI: we have one gram.",
        agent_id="blackbird",
    )

    assert out.index("unscalable") < out.index("— signal:"), (
        "the label must come after the body"
    )
    assert "read: parsed" in out
