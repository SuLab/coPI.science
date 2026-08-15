"""Cohort gate against the REAL Anthropic API. Skipped unless a key is present.

Everything else in the cohort suite scripts the LLM. This module used to spend
real tokens to prove two claims that a fake cannot check:

1. A real model, given a Phase 2 prompt built under an active gate, cannot select or
   reason about a post the gate removed. Removal-cycle task 7 deleted Phase 2
   outright (`_phase2_scan_filter`/`build_phase2_scan_prompt`/`build_scan_system_
   prompt`/`interesting_posts`), so the four tests that proved this claim
   (`test_real_model_would_have_acted_on_the_post_the_gate_removes`,
   `test_real_scan_response_parses_under_an_active_gate`,
   `test_real_model_acts_on_an_uncohorted_peer_under_open_policy`,
   `test_real_model_cannot_reach_across_a_hub`) were deleted with it — there is no
   longer a prompt for them to build. The underlying read-path invariant they
   rested on is NOT left unpinned:
     - `tests/unit/test_cohort_isolation.py::TestGatedReads::
       test_top_level_posts_filtered` deterministically pins that
       `MessageLog.get_new_top_level_posts(allowed_sender_ids=...)` — the exact
       gated read Phase 2 used to consume — excludes non-cohort posts from its
       returned set, with no LLM involved.
     - `tests/integration/test_cohort_engine_live.py::
       test_hub_auto_activation_does_not_activate_from_a_non_cohort_post` re-pins
       that same read at the one surviving production call site (the scout_hub
       auto-activation branch of `_phase3_activate_threads`), against a real
       engine and a real database.
   A real-model version of "does Opus/Sonnet actually decline to act on gated-out
   content" would need a new vehicle built on Phase 5's response shape
   (`{"action": "new_post"|"skip", "post_type": ...}` plus a `<slack_message>`
   tag, vs. Phase 2's `{"selected_post_ids": [...]}`) — real design work, not a
   mechanical retarget, and deliberately not attempted here under this removal
   task; flagged for a follow-up if real-model verification of this claim is
   wanted again.
2. A real model asked to start a conversation will name a partner, and the outbound
   strip must remove a cross-cohort mention from genuine model prose rather than
   from a hand-written string. This claim is independent of Phase 2 — hand-written
   system/user prompts, no phase-2 builder call — and its test survives below.

Cost control:
- ``max_tokens`` is capped hard.
- Sonnet, not Opus.
- One call, one test.

Run it with:

    docker compose exec -e ANTHROPIC_API_KEY=sk-ant-... \\
      -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_test \\
      app python -m pytest tests/integration/test_cohort_real_llm.py -v -m real_llm

Without a key every test skips, so the default suite stays free and offline.
"""

import os

import pytest

from src.agent.agent import Agent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_llm,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="no ANTHROPIC_API_KEY — real-API tests are opt-in and cost money",
    ),
]

MAX_TOKENS = 300


def _agent(agent_id="su", bot="SuBot"):
    return Agent(agent_id=agent_id, bot_name=bot, pi_name=f"PI {agent_id}")


async def _call(system_prompt, messages, model=None):
    """One real API call. Sonnet, with a hard token cap."""
    from src.config import get_settings
    from src.services import llm

    settings = get_settings()
    return await llm.generate_agent_response(
        system_prompt=system_prompt,
        messages=messages,
        model=model or settings.llm_agent_model_sonnet,
        max_tokens=MAX_TOKENS,
        log_meta={"agent_id": "su", "phase": "real_llm_audit"},
    )


async def test_real_model_prose_gets_its_cross_cohort_mention_stripped(monkeypatch):
    """Ask a real model to write a post that tags a specific bot, then run the real
    outbound strip over its actual prose."""
    import types

    import src.agent.simulation as sim
    from src.agent.simulation import SimulationEngine
    from src.agent.transport import NullTransport

    settings_ns = types.SimpleNamespace(
        cohort_isolation_enabled=True, cohort_default_policy="isolated",
        turn_delay_seconds=0.0,
    )
    monkeypatch.setattr(sim, "get_settings", lambda: settings_ns)

    ids = ("su", "wiseman", "cravatt")
    eng = SimulationEngine(
        agents=[_agent(a, f"{a.capitalize()}Bot") for a in ids],
        slack_clients={a: NullTransport(a) for a in ids},
        budget_cap=0, session_factory=None, slack_enabled=False,
    )
    eng._bot_name_to_id = {f"{a}bot": a for a in ids}
    su = eng.agents["su"]
    su.allowed_sender_ids = {"su", "wiseman"}   # cravatt is outside the cohort

    response = await _call(
        "You are SuBot, a lab's research agent in a Slack channel. Reply with one "
        "short paragraph only, no preamble.",
        [{"role": "user", "content":
          "Write a two-sentence Slack message proposing a collaboration. You must "
          "mention both @WisemanBot and @CravattBot by name with the @ prefix."}],
    )
    assert response, "the real API returned nothing"

    cleaned, _ = eng._strip_disallowed_tags(response, su)
    assert cleaned is not None
    if "@CravattBot" in response:
        assert "CravattBot" not in cleaned, (
            f"a cross-cohort mention survived the strip.\nmodel wrote: {response!r}\n"
            f"after strip: {cleaned!r}"
        )
        assert eng._cohort_tags_stripped.get("su", 0) >= 1
    else:
        pytest.skip(
            "the model did not produce an @CravattBot mention, so there was nothing "
            f"to strip. Model output: {response!r}"
        )
    if "@WisemanBot" in response:
        assert "@WisemanBot" in cleaned, "a cohort-mate mention must survive"
