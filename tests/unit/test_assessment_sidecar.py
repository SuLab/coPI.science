from src.agent.simulation import _extract_assessment_json

_RESPONSE = """
Here is my reasoning about the Wang DBT opportunity.

```json
{"action": "new_post", "target_post_id": null, "channel": "general",
 "post_type": "opportunity_assessment", "tagged_agent": null}
```

<slack_message>
:mag: *Opportunity Assessment — Wang Lab (JHU)*
Recommendation: route-to-incubation. [Speculative]
</slack_message>

<assessment_json>
{
  "company_or_project": "DBT / BCAA-autophagy axis",
  "subject_agent_id": "wang",
  "funnel_stage": "incubation",
  "gating": {"baltimore_commitment": "unconfirmed", "life_sciences_domain": "met",
             "credible_tech_source": "met", "fto_achievable": "not_met"},
  "scores": {"differentiation": 4, "market_unmet_need": 4, "team": 4,
             "external_signals": 1, "ip_fto": 2, "platform": 3,
             "dev_regulatory_feasibility": 3, "workplan_capital_efficiency": 3,
             "exit_thesis": 2},
  "weighted_score": 0,
  "red_flags": ["No external validation yet"],
  "recommendation": "route-to-incubation",
  "rationale": "Differentiated metabolic angle; needs mammalian in vivo.",
  "suggested_derisking_milestones": ["TDP-43 mouse rescue"],
  "confidence": "Speculative"
}
</assessment_json>
"""


def test_extracts_the_sidecar_verdict():
    verdict = _extract_assessment_json(_RESPONSE)
    assert verdict["funnel_stage"] == "incubation"
    assert verdict["subject_agent_id"] == "wang"
    # Tri-state string, never a bare boolean (F11) — "the PI declined"
    # (not_met) and "we never asked" (unconfirmed) are different facts.
    assert verdict["gating"]["baltimore_commitment"] == "unconfirmed"
    assert verdict["gating"]["fto_achievable"] == "not_met"
    assert verdict["scores"]["differentiation"] == 4
    assert verdict["recommendation"] == "route-to-incubation"


def test_action_json_still_wins_the_action_parse():
    """The sidecar is bare JSON precisely so the LAST ```json``` fence stays the
    action. If this breaks, every scout_hub post silently becomes a no-op."""
    from src.agent.simulation import SimulationEngine

    data, body = SimulationEngine._parse_phase5_response(None, _RESPONSE)
    assert data["action"] == "new_post"
    assert data["post_type"] == "opportunity_assessment"
    assert ":mag:" in body
    assert "assessment_json" not in body
    assert "funnel_stage" not in body


def test_missing_sidecar_returns_none():
    assert _extract_assessment_json("<slack_message>hi</slack_message>") is None


def test_malformed_sidecar_returns_none_and_does_not_raise():
    assert _extract_assessment_json(
        "<assessment_json>{not json,,,}</assessment_json>"
    ) is None


def test_last_sidecar_wins_when_the_model_revises():
    text = (
        "<assessment_json>{\"funnel_stage\": \"seed\"}</assessment_json>"
        "<assessment_json>{\"funnel_stage\": \"incubation\"}</assessment_json>"
    )
    assert _extract_assessment_json(text)["funnel_stage"] == "incubation"


def test_earlier_valid_verdict_survives_a_later_malformed_revision():
    # Finding A4: last-wins is right when the revision itself parses. It must
    # NOT mean "the newest block, or nothing" — a model emitting a good
    # verdict and then a broken revision must not lose the good one.
    text = (
        '<assessment_json>{"funnel_stage": "incubation"}</assessment_json>'
        "<assessment_json>{not json,,,}</assessment_json>"
    )
    verdict = _extract_assessment_json(text)
    assert verdict is not None
    assert verdict["funnel_stage"] == "incubation"


def test_earlier_valid_verdict_survives_a_later_non_object_revision():
    # Same principle, but the later block is syntactically valid JSON — just
    # the wrong shape (an array instead of an object). Still not usable, so
    # the earlier good verdict must still win.
    text = (
        '<assessment_json>{"funnel_stage": "incubation"}</assessment_json>'
        '<assessment_json>[1, 2, 3]</assessment_json>'
    )
    verdict = _extract_assessment_json(text)
    assert verdict is not None
    assert verdict["funnel_stage"] == "incubation"


def test_all_blocks_unusable_still_returns_none_and_does_not_raise():
    text = (
        "<assessment_json>{not json,,,}</assessment_json>"
        '<assessment_json>[1, 2, 3]</assessment_json>'
    )
    assert _extract_assessment_json(text) is None


_RESPONSE_FENCED_SIDECAR = """
Here is my reasoning about the Wang DBT opportunity.

```json
{"action": "new_post", "target_post_id": null, "channel": "general",
 "post_type": "opportunity_assessment", "tagged_agent": null}
```

<slack_message>
:mag: *Opportunity Assessment — Wang Lab (JHU)*
Recommendation: route-to-incubation. [Speculative]
</slack_message>

<assessment_json>
```json
{
  "company_or_project": "DBT / BCAA-autophagy axis",
  "subject_agent_id": "wang",
  "funnel_stage": "incubation",
  "gating": {"baltimore_commitment": "unconfirmed", "life_sciences_domain": "met",
             "credible_tech_source": "met", "fto_achievable": "not_met"},
  "scores": {"differentiation": 4},
  "weighted_score": 0,
  "recommendation": "route-to-incubation",
  "confidence": "Speculative"
}
```
</assessment_json>
"""


def test_fenced_sidecar_does_not_hijack_the_action_parse():
    """F1 regression: the model routinely wraps the <assessment_json> sidecar
    in a ```json``` fence despite being told not to. Because the sidecar is
    emitted LAST, a fenced sidecar becomes the LAST fenced block in the raw
    response — exactly what _parse_phase5_response used to take as the
    action. Before the fix, this made ``action_data`` the verdict dict
    itself: ``action`` fell back to "new_post" (now removed too, see below),
    ``channel`` fell back to "general", and post_type came back empty, so
    persistence never fired for a named-PI assessment that posted into the
    workspace's broadest channel."""
    from src.agent.simulation import SimulationEngine

    data, body = SimulationEngine._parse_phase5_response(None, _RESPONSE_FENCED_SIDECAR)

    # The real action wins, not the verdict dict.
    assert data["action"] == "new_post"
    assert data["channel"] == "general"
    assert data["post_type"] == "opportunity_assessment"
    assert "funnel_stage" not in data
    assert "gating" not in data

    # The slack_message body is unaffected and carries no sidecar leakage.
    assert ":mag:" in body
    assert "assessment_json" not in body

    # The verdict itself is still separately extractable via the sidecar
    # extractor (fence-tolerant on the way in — _ASSESSMENT_RE matches on the
    # tags, not the fence — even though the prompt asks for no fence).
    verdict = _extract_assessment_json(_RESPONSE_FENCED_SIDECAR)
    assert verdict is not None
    assert verdict["subject_agent_id"] == "wang"
    assert verdict["funnel_stage"] == "incubation"


# --- the sidecar has to fit in the call that emits it ------------------------

import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_phase4_reply_budget_fits_the_assessment_sidecar(monkeypatch):
    """The CONCLUDE reply carries the `<assessment_json>` sidecar LAST, so a
    truncated response loses the closing tag, `_extract_assessment_json` returns
    None, and the verdict is gone — the reply is already posted and there is no
    retry path for the artifact.

    This codebase already sized this exact artifact once: `_phase5_new_post`
    carries a note that 1000 tokens "truncated the verdict first while leaving
    the Slack post looking complete" and sits at 2500 for it. When the sidecar
    moved into the Phase-4 CONCLUDE reply (Option A), it landed in a call still
    budgeted at 1500 — below the figure the same file documents as necessary.
    Production logs for this phase show 11 `stop_reason=max_tokens` retries and
    one response still truncated after the 2x retry.
    """
    from src.agent.agent import Agent
    from src.agent.simulation import SimulationEngine
    from src.agent.state import ThreadState
    from tests.fakes import FakeSlackClient

    PHASE5_BUDGET = 2500

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang",
        message_count=11, has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    engine = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )

    seen: dict = {}

    async def _capture(**kwargs):
        seen.update(kwargs)
        return "<slack_message>A concluding reply.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _capture)

    await engine._reply_to_thread(hub, thread)

    assert seen, "generate_with_tools was never called"
    assert seen["max_tokens"] >= PHASE5_BUDGET, (
        f"Phase 4 carries the assessment sidecar but is budgeted at "
        f"{seen['max_tokens']} tokens, below the {PHASE5_BUDGET} this codebase "
        f"documents as necessary for the same artifact"
    )
