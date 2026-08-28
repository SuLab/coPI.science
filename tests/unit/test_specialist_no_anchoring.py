"""A specialist must never see another specialist's verdict, or any score
that the CODE injected on its own.

Anchoring on a prior score reaches Cohen's d = 0.71 and is NOT removable by
telling the model to ignore it (arXiv:2608.25869). Measured across all 1,192
production consults on 2026-08-28: zero contexts carried a sibling signal or a
numeric score. This test is what keeps that true going forward.

CORRECTION to the plan's original test (task-7-brief.md): the brief's own
fixture passed a `question` containing "the chemistry specialist said
blocking" and a `context` containing "Weighted score so far 2.85", then
asserted the *assembled prompt* contained neither — which cannot pass, since
the brief's own regex (``\\b\\d\\.\\d{1,2}\\b``) matches "2.85" and
`_execute_consult_specialist` passes the caller's `question` and `context`
through VERBATIM (see `tools.py`, around the ``## Question from the hub`` /
``## What the PI has said`` composition): the test manufactures the exact
violation it then asserts against. Forbidding content the CALLER supplied
tests the wrong thing — passing the hub's own words through is how a consult
works at all.

What is actually worth pinning is that the CODE adds no sibling state of its
own on top of whatever the caller passed — no other specialist's verdict, no
computed score, no run-level panel tally. So this drives the consult with
CLEAN inputs (a question and context containing no verdict words and no
numbers) and pins the exact SHAPE of what is sent: the persona file as
`system` with exactly one substitution applied to it — `{stage_bar}`, filled
from the rubric document (rubric 3.3.0) — and exactly one user message whose
content is exactly the two labelled sections built from the caller's own
question and context — nothing else present that could carry sibling state.

The stage-bar substitution is not a loosening of the invariant: the expected
`system` string is still compared for exact equality, just built the way
`tools.py` builds it, and the bar is a function of the DOMAIN alone — the same
text on every `legal` consult in every run and every thread, naming no other
specialist's verdict and no score for the opportunity under review.

Note what this test does NOT check: the persona files themselves legitimately
contain the words "verdict_signal", "blocking", "gap" and "adequate" — as
the ANSWER-FORMAT instructions telling the specialist how to shape its OWN
reply (see e.g. prompts/specialists/legal.md's "Answer format" section). That
is not sibling state, so this test does not scan the persona for those words;
it scans only the assembled user message, and only after proving nothing was
appended to either half.

Also note what this test does NOT guarantee: it does not prove the hub itself
never writes a score or a sibling's verdict into its own `question` or
`context` arguments — that discipline is the hub's (and the prompt's) to
keep, and was measured separately (0 of 1,192 production consults), not
proved here. A caller that passes dirty input will still get it echoed
verbatim, by design.
"""
import re

import pytest

from src.agent.specialists import VERDICT_SIGNALS, persona_path
from src.agent.tools import _execute_consult_specialist
from src.services.blackbird_rubric import render_stage_bar_markdown
from tests.fakes import FakeAnthropic

_SCORE = re.compile(r"\b\d\.\d{1,2}\b")


@pytest.mark.asyncio
async def test_the_prompt_sent_to_a_specialist_carries_no_sibling_state(monkeypatch):
    fake = FakeAnthropic([
        '{"verdict_signal": "gap", "concerns": [], '
        '"questions_to_ask": [], "confidence": "low"}'
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    # Deliberately clean: no verdict word, no sibling domain, no number.
    question = "How ownable is the core IP position for this platform?"
    context = "PI: nothing has been filed yet; we plan to file within six months."

    await _execute_consult_specialist(
        "legal", question, context, agent_id="blackbird",
    )

    assert len(fake.calls) == 1, "exactly one API call for one consult"
    sent = fake.calls[-1]

    # `_acreate` (src/services/llm.py) wraps a plain-string `system` into
    # prompt-caching blocks (`_cacheable_system`) before it ever reaches the
    # API — that is an existing, unrelated transport concern, not sibling
    # state, so unwrap it before comparing. A specialist persona has no
    # "## Your Working Memory" boundary, so it comes back as exactly one
    # block covering the whole text.
    system_sent = sent["system"]
    if isinstance(system_sent, list):
        assert len(system_sent) == 1, (
            "a persona with no working-memory boundary must come back as "
            "ONE cache block — more than one means something else got folded in"
        )
        system_text = system_sent[0]["text"]
    else:
        system_text = system_sent

    # The persona must reach the model with EXACTLY one substitution applied —
    # `{stage_bar}` filled from the rubric document (rubric 3.3.0; see
    # render_stage_bar_markdown and tests/unit/test_stage_bars.py) — and nothing
    # else. Still an exact-equality check, so a sibling verdict or a running
    # tally appended anywhere in the persona still fails it; what changed is
    # that the expected string is now built the way tools.py builds it.
    #
    # A stage bar is not sibling state on any of the three axes this file cares
    # about: it is a function of the DOMAIN alone (identical on every consult of
    # legal, in any run, in any thread), it names no other specialist's verdict,
    # and it carries no computed score for the opportunity under review.
    persona = persona_path("legal").read_text(encoding="utf-8")
    expected_system = persona.replace(
        "{stage_bar}", render_stage_bar_markdown("legal")
    )
    assert expected_system != persona, (
        "prompts/specialists/legal.md lost its {stage_bar} placeholder — this "
        "assertion would then degrade to the pre-3.3.0 byte-for-byte check and "
        "stop proving the substitution happens at all"
    )
    assert system_text == expected_system, (
        "the code must send the persona with only the stage-bar substitution "
        "applied — no sibling state appended"
    )

    # Exactly one message, and its content is EXACTLY the two labelled
    # sections built from the caller's own question and context — proof
    # nothing else (another specialist's opinion, a computed score, a
    # run-level tally) was folded in alongside them.
    assert len(sent["messages"]) == 1, "exactly one message — nothing extra"
    message = sent["messages"][0]
    assert message["role"] == "user"
    expected_body = (
        f"## Question from the hub\n\n{question}\n\n"
        f"## What the PI has said\n\n{context}"
    )
    assert message["content"] == expected_body, (
        "the user message must be exactly the two labelled sections built "
        "from the caller's own question and context — nothing injected"
    )

    # Belt-and-suspenders on the assembled MESSAGE only (not the persona,
    # whose own answer-format instructions legitimately name these words).
    # With clean inputs this is implied by the exact-match assertion above,
    # but states the invariant in the vocabulary the plan cares about.
    assert "verdict_signal" not in message["content"], (
        "the sidecar key must never reach a specialist's context"
    )
    for signal in VERDICT_SIGNALS:
        assert not re.search(
            rf"\b{signal}\b\s*(signal|verdict)", message["content"], re.I
        ), f"a sibling {signal!r} verdict reached the specialist"
    assert not _SCORE.search(message["content"]), (
        "a numeric score reached the specialist"
    )
