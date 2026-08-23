"""A reply the model did not finish must not be treated as one it did.

``src/services/llm.py`` reports the terminating ``stop_reason`` through
``on_stop_reason`` and deliberately still RETURNS the partial text, because
whether a partial answer may be posted, persisted or credited is a call-site
decision and the three sites differ. Until now only ONE call site consumed it —
the specialist consult — and ``grep -rn on_stop_reason src/agent/simulation.py``
returned nothing at all. The engine's other three sites took a truncated reply
and posted or persisted it as complete.

Run 8b64a0e0 measured what that costs: 4 truncated hub replies posted to Slack
as if finished, and a refusal-truncated working-memory write that replaced a
complete 1,977-character memory with a 1,437-character one — twice, on disk and
in ``profile_revisions``. The comment above the memory guard already predicted
it ("a half-written summary is stored as the working memory ... with nothing in
the logs to say the file is short").

Every test here is parametrized over BOTH truncation stops. ``refusal`` is the
classifier cutting the generation and ``max_tokens`` is the ceiling doing it;
the text in hand is equally partial. A ``refusal``-only test would miss the
commonest case of all, because a reply that truncated, retried and truncated
again reports ``max_tokens``, and so does a fallthrough from the retry path even
when the first pass was refused. ``src.services.llm.is_truncated_stop`` is the
one definition, and these drive it through the real callback rather than
asserting on it directly.

The three sites answer differently ON PURPOSE:

* **memory** refuses the write outright. There is a good memory already on
  disk, and a half-written one is worse than a stale one.
* **thread_reply** posts the partial text WITH a marker. Discarding it would
  turn a mid-word reply into an EMPTY one, which increments
  ``empty_response_count`` and, on a second occurrence, abandons the interview
  with an ``empty_reply`` drop — that is how a real interview died, and the cure
  must not be the disease.
* **new_post** skips. Nothing is owed, nothing is lost, and a half-written
  pitch is not worth a workspace-visible post.
"""

from types import SimpleNamespace

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration

TRUNCATED_STOPS = ["refusal", "max_tokens"]

# A reply cut off mid-sentence: the shape of every real truncation, and the
# reason "is it empty?" was never a sufficient guard.
_HALF_A_MEMORY = (
    "(a) Ideas pitched: the low-input paired metabolomics workflow is with "
    "BlackbirdBot, which asked for a cost-per-sample figure and for"
)
_HALF_A_REPLY = (
    "That is a genuinely differentiated approach. Before I can take a view I "
    "need to understand whether the chromatin readout survives the"
)


def _fire(kwargs, stop_reason):
    """Report `stop_reason` the way llm.py does — through whatever callback the
    call site actually passed.

    Deliberately TOLERANT of a missing `on_stop_reason`, rather than
    `kwargs["on_stop_reason"]`. A KeyError would make every test here fail with
    the same message whether the wiring is absent or merely ignored, and the
    interesting failure is the behavioural one: an unwired call site silently
    posts or persists the partial text, which is exactly what these assert
    against. `stop_reason=None` means "the callback never fired", which llm.py's
    contract permits and which is a real path.
    """
    callback = kwargs.get("on_stop_reason")
    if callback is not None and stop_reason is not None:
        callback(stop_reason)


def _engine(agents):
    clients = {a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in agents}
    return SimulationEngine(agents=agents, slack_clients=clients), clients


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", TRUNCATED_STOPS)
async def test_a_truncated_memory_reply_does_not_overwrite_working_memory(
    monkeypatch, stop_reason,
):
    """The existing guard is only "empty or blank", so a half-sentence sailed
    straight into `update_working_memory_file` and into every later prompt."""
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    sim, _clients = _engine([agent])
    monkeypatch.setattr(
        agent, "build_thread_reply_system_prompt", lambda **kw: "sys"
    )

    written: list[str] = []
    monkeypatch.setattr(
        agent, "update_working_memory_file",
        lambda text, **kw: written.append(text),
    )

    async def _fake_generate(**kwargs):
        _fire(kwargs, stop_reason)
        return _HALF_A_MEMORY

    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", _fake_generate
    )

    await sim._update_agent_memory(agent, "an interview closed")

    assert written == [], (
        "a truncated synthesis must not replace the memory already on disk"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", ""])
async def test_a_complete_memory_reply_still_overwrites_working_memory(
    monkeypatch, stop_reason,
):
    """The control. Refusing every write would satisfy the test above, and the
    empty string is the real shape of "the reply carried no stop_reason at all"
    (`_notify_stop_reason` reports "" rather than None)."""
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    sim, _clients = _engine([agent])
    monkeypatch.setattr(
        agent, "build_thread_reply_system_prompt", lambda **kw: "sys"
    )

    written: list[str] = []
    monkeypatch.setattr(
        agent, "update_working_memory_file",
        lambda text, **kw: written.append(text),
    )

    async def _fake_generate(**kwargs):
        _fire(kwargs, stop_reason)
        return "(a) Ideas pitched: none. (b) No PI feedback. (c) Keep pitching."

    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", _fake_generate
    )

    await sim._update_agent_memory(agent, "an interview closed")

    assert len(written) == 1


# ---------------------------------------------------------------------------
# thread_reply
# ---------------------------------------------------------------------------


async def _drive_truncated_reply(monkeypatch, stop_reason, *, text=_HALF_A_REPLY):
    agent = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim, clients = _engine([agent])
    thread = ThreadState(
        thread_id="t1", channel="single-cell-omics", other_agent_id="gordy",
        message_count=3, has_pending_reply=True,
    )
    agent.state.active_threads["t1"] = thread
    sim.message_log.append(LogEntry(
        ts="t1", channel="single-cell-omics", sender_agent_id="gordy",
        sender_name="GordyBot", content="Here is the idea.", thread_ts=None,
        posted_at=1.0, slack_ts="t1", slack_channel_id="C_OMICS",
    ))
    monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _fake_generate_with_tools(**kwargs):
        _fire(kwargs, stop_reason)
        return f"<slack_message>{text}</slack_message>"

    monkeypatch.setattr(
        "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
    )

    async def _no_memory(*args, **kwargs):
        return None

    monkeypatch.setattr(sim, "_update_agent_memory", _no_memory)

    await sim._reply_to_thread(agent, thread)
    return SimpleNamespace(sim=sim, agent=agent, thread=thread, client=clients["blackbird"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", TRUNCATED_STOPS)
async def test_a_truncated_thread_reply_is_marked_not_silently_posted(
    monkeypatch, stop_reason,
):
    """Marked, and STILL POSTED.

    Discarding the partial text is the tempting fix and it is the wrong one: an
    empty reply increments `empty_response_count`, and a second one abandons the
    interview with an `empty_reply` drop — no verdict, no later turn, nothing.
    The PI is mid-conversation and is owed the words the hub actually produced,
    plus an honest statement that they stop mid-thought.
    """
    turn = await _drive_truncated_reply(monkeypatch, stop_reason)

    assert len(turn.client.posted) == 1, "the reply still goes out"
    posted = turn.client.posted[0]["text"]
    assert _HALF_A_REPLY in posted, (
        "the partial text is kept — dropping it converts this into an EMPTY "
        "reply, which is what abandons interviews"
    )
    assert "cut off" in posted.lower() or "truncat" in posted.lower(), (
        "and the reader is told it is incomplete"
    )
    assert turn.thread.empty_response_count == 0
    assert turn.thread.has_pending_reply is False, "the turn counts as taken"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", None])
async def test_a_complete_thread_reply_carries_no_truncation_marker(
    monkeypatch, stop_reason,
):
    """The control: marking every reply would satisfy the test above.
    `stop_reason=None` fires the callback not at all, which llm.py's contract
    permits."""
    turn = await _drive_truncated_reply(monkeypatch, stop_reason)

    assert len(turn.client.posted) == 1
    posted = turn.client.posted[0]["text"]
    assert posted.strip() == _HALF_A_REPLY, "no marker on a finished reply"


# ---------------------------------------------------------------------------
# new_post
# ---------------------------------------------------------------------------


def _phase5_settings():
    """The settings `_phase5_new_post` reads before it will make a call at all:
    a daily cap, a thread threshold, no random skip, and the rate limiter's two
    window knobs. Same shape as tests/unit/test_llm_log_channel.py's."""
    import types

    return types.SimpleNamespace(
        lab_daily_post_cap=5,
        active_thread_threshold=12,
        phase5_skip_probability=0.0,
        llm_agent_model_opus="test-model",
        llm_calls_per_load_per_window=8,
        llm_rate_window_seconds=600,
    )


async def _drive_truncated_post(monkeypatch, stop_reason, *, response):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    # No cohort gate: `_strip_disallowed_tags` is not what is under test here.
    agent.allowed_sender_ids = None
    # The hub has to be on the roster or `_available_post_types` returns an
    # EMPTY menu ("no post type satisfiable — check cohort/roster") and the turn
    # bails before it ever makes a call — which would make the truncation test
    # below pass for entirely the wrong reason.
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim, clients = _engine([agent, hub])
    monkeypatch.setattr(
        "src.agent.simulation.get_settings", lambda: _phase5_settings()
    )
    monkeypatch.setattr(
        agent, "build_phase5_prompt", lambda **kw: ("sys", [])
    )

    async def _fake_generate(**kwargs):
        _fire(kwargs, stop_reason)
        return response

    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", _fake_generate
    )

    await sim._phase5_new_post(agent)
    return SimpleNamespace(sim=sim, agent=agent, client=clients["wang"])


_A_PITCH = (
    "```json\n"
    '{"action": "new_post", "channel": "deep-dive", '
    '"post_type": "pitch", "tagged_agent": null}\n'
    "```\n\n"
    "<slack_message>A low-input paired metabolomics and chromatin workflow.</slack_message>"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", TRUNCATED_STOPS)
async def test_a_truncated_new_post_is_skipped(monkeypatch, stop_reason):
    """Nothing is owed here — no thread is waiting, no PI is mid-sentence — so
    a pitch the model did not finish is simply not posted."""
    turn = await _drive_truncated_post(monkeypatch, stop_reason, response=_A_PITCH)

    assert turn.client.posted == [], "a truncated pitch must not reach the workspace"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", None])
async def test_a_complete_new_post_still_posts(monkeypatch, stop_reason):
    """The control: skipping every post would satisfy the test above."""
    turn = await _drive_truncated_post(monkeypatch, stop_reason, response=_A_PITCH)

    assert len(turn.client.posted) == 1
    assert "paired metabolomics" in turn.client.posted[0]["text"]
