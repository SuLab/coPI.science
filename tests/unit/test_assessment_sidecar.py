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
