"""A round's ``consult_specialist`` blocks run together. Nothing else does.

Run 8b64a0e0: 81% of tool rounds carried two or more blocks and
``generate_with_tools`` executed them in a plain ``for`` loop, so the hub waited
25-40 s per consult, in series, for calls the API had already decided to make in
parallel. Measured cost **2,344 s — 21.2% of the run**, the single largest perf
item in the audit.

The narrowness is the design, not timidity. Each of the four other tool paths
breaks under concurrency for its own reason:

- ``ThreadState.abstracts_other`` / ``full_text`` are check-then-increment
  (``tools.py:278`` / ``:292``), so two concurrent fetches both read the same
  count and both pass a per-thread cap of one;
- ``agent.record_api_call`` mutates a deque per call (safe only because it is
  synchronous — an ``await`` added inside it would silently become a race);
- ``_record_specialist_consult`` must land before ``_post_panel_note``, an
  ordering that only holds inside one consult's own coroutine;
- ``search_prior_art`` already loses 10 of 125 searches to self-inflicted 429s,
  every one on the 3rd POST of its own un-paced tier ladder. Parallelism
  multiplies that.

So the consults are gathered and every other block stays exactly as serial as it
was — including relative to the consults, which is what these tests pin.
"""

import asyncio
import time

import pytest

from src.services import llm
from tests.fakes import FakeAnthropic, multi_tool_use_response, text_response

pytestmark = pytest.mark.asyncio

LOG_META = {"agent_id": "blackbird", "phase": "thread_reply"}

# Deliberately smaller than the plan's 1 s: two other subagents run pytest on
# the same 2-vCPU host, and the assertions that matter are the interval-overlap
# ones, which do not depend on the scale at all. 0.3 s keeps a 2x margin in both
# directions (3 blocks serial = 0.9 s; concurrent ~= 0.3 s).
_BLOCK_SECONDS = 0.3

TOOLS = [
    {"name": "consult_specialist", "input_schema": {}},
    {"name": "search_prior_art", "input_schema": {}},
    {"name": "fetch_abstract", "input_schema": {}},
]


def _install(monkeypatch, fake):
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)


class _Timeline:
    """A tool executor that records when each block entered and left."""

    def __init__(self, seconds: float = _BLOCK_SECONDS) -> None:
        self.seconds = seconds
        self.spans: list[tuple[str, float, float]] = []

    async def __call__(self, name: str, tool_input: dict) -> str:
        enter = time.monotonic()
        await asyncio.sleep(self.seconds)
        self.spans.append((name, enter, time.monotonic()))
        return f"{name} result"

    def spans_for(self, name: str) -> list[tuple[str, float, float]]:
        return [s for s in self.spans if s[0] == name]


def _overlaps(a: tuple[str, float, float], b: tuple[str, float, float]) -> bool:
    return a[1] < b[2] and b[1] < a[2]


def _tool_results(row: dict) -> list[dict]:
    """The tool_result blocks of the last user turn in the logged conversation."""
    user_turns = [
        m for m in row["messages"]
        if m.get("role") == "user" and isinstance(m.get("content"), list)
    ]
    return user_turns[-1]["content"]


@pytest.fixture
def logged():
    rows: list[dict] = []
    llm.set_call_log_callback(rows.append)
    yield rows
    llm.set_call_log_callback(None)


# ---------------------------------------------------------------------------
# the win
# ---------------------------------------------------------------------------


async def test_consult_blocks_run_concurrently_and_others_do_not(monkeypatch, logged):
    """Three consults in one round overlap; a mixed round's other blocks never do."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                multi_tool_use_response(
                    ("consult_specialist", {"domain": "chemistry"}),
                    ("consult_specialist", {"domain": "clinical"}),
                    ("consult_specialist", {"domain": "commercial"}),
                ),
                text_response("the reply"),
            ]
        ),
    )
    timeline = _Timeline()

    started = time.monotonic()
    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS,
        tool_executor=timeline,
        log_meta=LOG_META,
    )
    wall = time.monotonic() - started

    consults = timeline.spans_for("consult_specialist")
    assert len(consults) == 3
    for i in range(3):
        for j in range(i + 1, 3):
            assert _overlaps(consults[i], consults[j]), (
                f"consults {i} and {j} did not overlap: {consults}"
            )
    assert wall < _BLOCK_SECONDS * 2, (
        f"3 x {_BLOCK_SECONDS}s of consults took {wall:.2f}s — still serial"
    )


async def test_a_mixed_rounds_non_consult_blocks_never_overlap_anything(
    monkeypatch, logged
):
    """``search_prior_art`` is the reason this is worded as strongly as it is: its
    429s come from its OWN un-paced tier ladder, so it must not run alongside a
    consult either, not merely alongside another search."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                multi_tool_use_response(
                    ("search_prior_art", {"q": "a"}),
                    ("consult_specialist", {"domain": "chemistry"}),
                    ("consult_specialist", {"domain": "clinical"}),
                    ("fetch_abstract", {"pmid": "1"}),
                    ("search_prior_art", {"q": "b"}),
                ),
                text_response("the reply"),
            ]
        ),
    )
    timeline = _Timeline()

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS,
        tool_executor=timeline,
        log_meta=LOG_META,
    )

    consults = timeline.spans_for("consult_specialist")
    assert len(consults) == 2
    assert _overlaps(*consults)

    others = [s for s in timeline.spans if s[0] != "consult_specialist"]
    assert len(others) == 3
    for i, one in enumerate(others):
        for two in timeline.spans:
            if one is two:
                continue
            assert not _overlaps(one, two), (
                f"{one[0]} (index {i}) overlapped {two[0]}: {timeline.spans}"
            )


# ---------------------------------------------------------------------------
# the contract with the API: one result per block, in order, correctly paired
# ---------------------------------------------------------------------------


async def test_tool_results_keep_block_order_and_tool_use_id_pairing(
    monkeypatch, logged
):
    """The API matches results to calls by ``tool_use_id``, and a mismatched or
    missing pair is a 400 that kills the whole turn. Gathering must not be
    observable in the request at all."""
    round_msg = multi_tool_use_response(
        ("search_prior_art", {"q": "a"}),
        ("consult_specialist", {"domain": "chemistry"}),
        ("consult_specialist", {"domain": "clinical"}),
        ("fetch_abstract", {"pmid": "1"}),
    )
    _install(monkeypatch, FakeAnthropic([round_msg, text_response("the reply")]))

    async def _executor(name, tool_input):
        return f"{name}:{sorted(tool_input.values())}"

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS,
        tool_executor=_executor,
        log_meta=LOG_META,
    )

    results = _tool_results(logged[0])
    blocks = [b for b in round_msg.content if b.type == "tool_use"]
    assert [r["tool_use_id"] for r in results] == [b.id for b in blocks]
    assert all(r["type"] == "tool_result" for r in results)
    # And each result carries the output of ITS OWN block, not a neighbour's.
    assert [r["content"] for r in results] == [
        f"{b.name}:{sorted(b.input.values())}" for b in blocks
    ]


async def test_a_single_consult_round_is_unchanged(monkeypatch, logged):
    """The majority-of-rounds case must not acquire a gather it cannot use."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                multi_tool_use_response(("consult_specialist", {"domain": "chemistry"})),
                text_response("the reply"),
            ]
        ),
    )

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS,
        tool_executor=_Timeline(seconds=0.0),
        log_meta=LOG_META,
    )

    assert out == "the reply"
    assert [r["tool_use_id"] for r in _tool_results(logged[0])] == ["toolu_1"]


async def test_a_raising_consult_still_fails_the_turn(monkeypatch):
    """Unchanged semantics. ``execute_tool`` catches everything and returns an
    error STRING, so this never happens in production — but if it ever does, a
    gather must not swallow it into a silently short result list, which the API
    would reject as a missing tool_result."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                multi_tool_use_response(
                    ("consult_specialist", {"domain": "chemistry"}),
                    ("consult_specialist", {"domain": "clinical"}),
                ),
                text_response("never reached"),
            ]
        ),
    )

    async def _boom(name, tool_input):
        raise RuntimeError("specialist exploded")

    with pytest.raises(RuntimeError, match="specialist exploded"):
        await llm.generate_with_tools(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS,
            tool_executor=_boom,
            log_meta=LOG_META,
        )
