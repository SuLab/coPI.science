"""Prompt caching: two breakpoints, a 5-minute TTL, and the ordering it rests on.

Run 8b64a0e0, finding M6 — **revised upward as cost, refuted as performance.**
Measured with the real tokenizer, the re-sent system prompt is **5.18 M tokens =
40.6% of all input** (not the 4.66 M / 36.5% the first pass reported), and the
hub's stable prefix is **90.4% of its own prompt**. But OLS over 810 calls gives
``latency ~= 3.4 + 13.66 * output_k`` with input tokens having no measurable
effect (R^2 = 0.53, coefficient indistinguishable from zero). So this buys ~40%
of input spend and ~0 wall time: it is a cost fix, and filing it as a perf fix
would have made the perf numbers wrong.

**5-minute TTL, not 1-hour.** Median hub turn inter-arrival is 58 s, so the
common case is well inside the default TTL; the extra hits a 1-hour entry would
catch do not pay for doubling the write premium (1.25x -> 2x).

**The prefix is only cacheable because of an ordering accident that is now an
invariant.** ``Agent._compose_system_prompt`` renders ``## Your Working Memory``
LAST, so everything a turn changes sits at the end of the prompt. Move it up —
or interpolate anything per-turn ahead of it — and the hit rate silently goes to
zero with no error and no failing test. That is what
``test_the_cached_prefix_is_identical_across_differing_working_memory`` exists to
prevent.
"""

import pytest

from src.agent import agent as agent_module
from src.agent.agent import Agent
from src.services import llm
from tests.fakes import FakeAnthropic, multi_tool_use_response, text_response

pytestmark = pytest.mark.asyncio

LOG_META = {"agent_id": "blackbird", "phase": "thread_reply"}
EPHEMERAL = {"type": "ephemeral"}


def _install(monkeypatch, fake):
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)


async def _noop_executor(name, tool_input):
    return "tool output"


def _breakpoints(request: dict) -> int:
    """How many cache_control markers one outbound request carries."""
    total = 0
    for block in request.get("system") or []:
        if isinstance(block, dict) and block.get("cache_control"):
            total += 1
    for message in request.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("cache_control"):
                total += 1
    return total


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A hermetic Agent: no real profiles, no real memory on disk."""
    monkeypatch.setattr(agent_module, "PROFILES_DIR", tmp_path)
    for sub in ("public", "private", "memory"):
        (tmp_path / sub).mkdir()
    return Agent(agent_id="blackbird", bot_name="BlackbirdBot", pi_name="Blackbird")


# ---------------------------------------------------------------------------
# the ordering invariant the whole thing rests on
# ---------------------------------------------------------------------------


async def test_the_cached_prefix_is_identical_across_differing_working_memory(agent):
    """Two prompts that differ ONLY in working memory must share a byte-identical
    cacheable prefix. If a future edit moves ``## Your Working Memory`` earlier,
    or renders anything per-turn ahead of it, this fails loudly instead of the
    hit rate quietly becoming zero."""
    agent._public_working_memory = "Talked to Su about repurposing. Nothing pending."
    first = agent.build_thread_reply_system_prompt()

    agent._public_working_memory = (
        "Interviewed Pearce; owed a follow-up on the ICC/CV study. "
        "Markham declined. Weeraratna pitched senescence."
    )
    second = agent.build_thread_reply_system_prompt()

    assert first != second, "the fixture must actually vary the volatile half"

    head_first, *tail_first = llm._cacheable_system(first)
    head_second, *tail_second = llm._cacheable_system(second)

    assert head_first["text"] == head_second["text"]
    assert head_first["cache_control"] == EPHEMERAL
    assert first.startswith(head_first["text"])
    assert second.startswith(head_second["text"])
    # The volatile half carries no marker of its own — marking it would write a
    # distinct cache entry per turn and read none of them.
    assert len(tail_first) == len(tail_second) == 1
    assert "cache_control" not in tail_first[0]
    assert tail_first[0]["text"] != tail_second[0]["text"]
    # And splitting loses nothing: the blocks reassemble to the original prompt.
    assert head_first["text"] + tail_first[0]["text"] == first


async def test_the_split_puts_the_working_memory_header_in_the_volatile_half(agent):
    """The boundary is the header line itself, not somewhere inside the prefix —
    otherwise the header's own bytes would be cached while its body was not."""
    agent._public_working_memory = "notes"
    prompt = agent.build_thread_reply_system_prompt()

    head, tail = llm._cacheable_system(prompt)

    assert "## Your Working Memory" not in head["text"]
    assert tail["text"].lstrip("\n").startswith("## Your Working Memory")


# ---------------------------------------------------------------------------
# what actually goes out on the wire
# ---------------------------------------------------------------------------


async def test_a_prompt_with_no_working_memory_section_is_cached_whole(monkeypatch):
    """A specialist persona, a profile-synthesis prompt, a phase-1 decision: all
    static per call site, so the whole system prompt is the stable prefix."""
    fake = FakeAnthropic([text_response("answer")])
    _install(monkeypatch, fake)

    await llm.generate_agent_response(
        "You are the chemistry specialist.", [{"role": "user", "content": "hi"}],
        log_meta=LOG_META,
    )

    assert fake.calls[0]["system"] == [
        {
            "type": "text",
            "text": "You are the chemistry specialist.",
            "cache_control": EPHEMERAL,
        }
    ]


async def test_the_ttl_is_the_default_five_minutes(monkeypatch):
    """Explicit, because ``ttl`` is the one field where the cheaper choice is the
    one you get by leaving it out: 1h doubles the write premium (1.25x -> 2x) and
    the hub's 58 s median inter-arrival never needs it."""
    fake = FakeAnthropic([text_response("answer")])
    _install(monkeypatch, fake)

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], log_meta=LOG_META
    )

    (block,) = fake.calls[0]["system"]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in block["cache_control"]


async def test_an_empty_system_prompt_is_left_exactly_as_it_was(monkeypatch):
    """The API rejects an empty text block, so "" must stay a bare string rather
    than becoming ``[{"type": "text", "text": ""}]``."""
    fake = FakeAnthropic([text_response("answer")])
    _install(monkeypatch, fake)

    await llm.generate_agent_response("", [{"role": "user", "content": "hi"}])

    assert fake.calls[0]["system"] == ""


async def test_the_last_tool_result_of_the_round_carries_the_second_breakpoint(
    monkeypatch
):
    """One marker per round, on the last tool_result: the tool outputs are the
    bulk of what a multi-round turn re-sends, and the next round's request has
    them as its prefix."""
    fake = FakeAnthropic(
        [
            multi_tool_use_response(
                ("consult_specialist", {"domain": "chemistry"}),
                ("consult_specialist", {"domain": "clinical"}),
            ),
            text_response("the reply"),
        ]
    )
    _install(monkeypatch, fake)

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        log_meta=LOG_META,
    )

    results = fake.calls[-1]["messages"][-1]["content"]
    assert len(results) == 2
    assert "cache_control" not in results[0]
    assert results[1]["cache_control"] == EPHEMERAL


async def test_the_breakpoint_rolls_forward_and_never_exceeds_four(monkeypatch):
    """Max 4 ``cache_control`` blocks per request, hard API limit. A 5-round turn
    would carry 6 if each round kept its own, so the marker MOVES rather than
    accumulating — and moving it costs nothing, because a marker designates a
    breakpoint and is not part of the content being matched."""
    fake = FakeAnthropic(
        [multi_tool_use_response(("consult_specialist", {"n": i})) for i in range(5)]
        + [text_response("the reply")]
    )
    _install(monkeypatch, fake)

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tool_rounds=5,
        log_meta=LOG_META,
    )

    assert len(fake.calls) == 6
    for i, request in enumerate(fake.calls):
        assert _breakpoints(request) <= 4, f"call {i} carried too many breakpoints"
    # The last request: one on the system prefix, one on the newest tool_result.
    assert _breakpoints(fake.calls[-1]) == 2
    marked = [
        block
        for message in fake.calls[-1]["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("cache_control")
    ]
    assert len(marked) == 1
    assert marked[0]["tool_use_id"] == "toolu_1"
    assert marked[0] is fake.calls[-1]["messages"][-1]["content"][-1]
