"""Which verdicts owe a specialist panel at all.

The floor used to key on one field only — the **model-written**
`recommendation` — with two consequences measured in run 8b64a0e0
(docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md, the "new defect
found behind H8"):

1. **Band and recommendation disagree, often.** 3 of the 4 `conditional` bands
   in the stored corpus carry `recommendation='pass'` — 75% of non-`pass`
   bands. The band is COMPUTED by `weighted_score`; the recommendation is
   whatever the model wrote. So a verdict that scores into `conditional` while
   saying `pass` exempted itself from the panel, and did so on the one field
   the hub controls.
2. **`route-to-incubation` — Blackbird's own positive outcome, the grant the
   programme exists to award — was exempt**, on the stated rationale that "a
   decline costs Blackbird nothing". That rationale is true of a decline and
   false of a grant, so the exemption was applied to exactly the wrong half of
   the funnel. 8 of the run's emitted verdicts were `route-to-incubation`.

The gate is fail-CLOSED on anything it cannot read: only an explicit `pass`,
uncontradicted by the computed band, buys the exemption. An unreadable
recommendation must not.

`required_domains_for` is the pure half; `SimulationEngine._specialist_floor_gap`
is the caller. That paragraph used to say the caller "still returns early on its
own `_PANEL_REQUIRED_FOR` test", so everything here was "live in the module and
dead in the engine". Both halves are false now: A1.7 wired the band through the
floor, A2.1 wired it through `_seed_consults_from_db` — the last site testing the
recommendation alone — and the `_PANEL_REQUIRED_FOR` alias on the engine has been
deleted for want of readers. `panel_is_owed` is the single gate, and what is
pinned here is live everywhere.
"""

from src.agent.specialists import (
    PANEL_EXEMPT_RECOMMENDATIONS,
    PANEL_REQUIRED_FOR,
    panel_is_owed,
    required_domains_for,
)

_ALWAYS = {"scientific", "talent"}


def _verdict(**over):
    v = {
        "recommendation": "pass",
        "subject_agent_id": "gill",
        "rationale": "A clean but unremarkable idea.",
        "company_or_project": "",
        "funnel_stage": "incubation",
        "red_flags": [],
        "suggested_derisking_milestones": [],
    }
    v.update(over)
    return v


# --- the gate ---------------------------------------------------------------

def test_a_pass_with_no_band_is_still_exempt():
    """The commonest case in production by far — 25 of 29 stored v2 verdicts —
    and the change must not turn it into a panel obligation."""
    assert panel_is_owed("pass") is False
    assert required_domains_for(_verdict()) == frozenset()


def test_a_conditional_band_with_a_pass_recommendation_still_owes_a_panel():
    """The 3-of-4 case. Fails before the band participates in the gate."""
    assert panel_is_owed("pass", "conditional") is True
    required = required_domains_for(_verdict(), band="conditional")
    assert _ALWAYS <= required


def test_an_advance_band_with_a_pass_recommendation_still_owes_a_panel():
    assert panel_is_owed("pass", "advance") is True


def test_route_to_incubation_owes_a_panel():
    """Blackbird's own positive outcome. Nothing about a grant is cheap."""
    assert panel_is_owed("route-to-incubation") is True
    assert _ALWAYS <= required_domains_for(_verdict(recommendation="route-to-incubation"))


def test_advance_and_conditional_recommendations_are_unchanged():
    for recommendation in sorted(PANEL_REQUIRED_FOR):
        assert panel_is_owed(recommendation) is True
        assert _ALWAYS <= required_domains_for(_verdict(recommendation=recommendation))


def test_a_pass_band_does_not_rescue_an_advance_recommendation():
    """Either field can pull a verdict INTO the panel; neither can pull it out.
    A hub that writes `advance` has said the thing the floor exists to check,
    whatever the arithmetic came to."""
    assert panel_is_owed("advance", "pass") is True


def test_an_unreadable_recommendation_owes_a_panel():
    """Fail-closed. The old test was `recommendation not in {advance,
    conditional}` -> exempt, so every off-contract or missing value bought an
    exemption. `panel_incomplete=True` on a garbage verdict is a flag on a row;
    an unreviewed grant is not.
    """
    for recommendation in (None, "", "maybe", "advance-ish", 7, ["advance"]):
        assert panel_is_owed(recommendation) is True


def test_the_only_exempt_recommendation_is_a_decline():
    assert PANEL_EXEMPT_RECOMMENDATIONS == frozenset({"pass"})


def test_the_gate_is_case_and_whitespace_tolerant():
    """Both fields arrive from a model through free text. `_haystack` already
    lowercases for cue matching; the gate must not be the one place where
    `"Pass"` means something different."""
    assert panel_is_owed(" Pass ") is False
    assert panel_is_owed("pass", " Conditional ") is True


def test_the_band_set_is_the_band_names_the_rubric_can_produce():
    """`band()` returns exactly advance/conditional/pass, so the gate's band
    side needs no vocabulary of its own."""
    from src.services.blackbird_rubric import band

    produced = {
        band(score)
        for score in (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
    }
    assert PANEL_REQUIRED_FOR <= produced
    assert produced - PANEL_REQUIRED_FOR == {"pass"}, (
        "the band vocabulary grew a fourth value; the gate needs to say what it means"
    )


# --- the derived obligations, once the gate is passed ------------------------

def test_the_exempt_path_derives_nothing_at_all():
    """An exempt verdict must not merely be ignored by the caller — it must
    derive an EMPTY obligation set, so a future caller that forgets the gate
    still cannot invent a panel for a decline."""
    assert required_domains_for(
        _verdict(rationale="A small-molecule inhibitor for a rare epilepsy.")
    ) == frozenset()


def test_a_band_owed_verdict_still_derives_its_cue_driven_domains():
    """The gate decides WHETHER; the content decides WHICH. A `pass`
    recommendation banding `conditional` owes the same domains an `advance`
    would."""
    verdict = _verdict(rationale="A small-molecule inhibitor for a rare epilepsy.")
    assert required_domains_for(verdict, band="conditional") >= (
        _ALWAYS | {"chemistry", "clinical"}
    )


def test_the_band_keyword_is_optional_and_defaults_to_todays_behaviour():
    """Every pre-existing caller passes no band. `advance` with no band must
    behave exactly as it did before the keyword existed."""
    verdict = _verdict(recommendation="advance", rationale="A reusable platform.")
    assert required_domains_for(verdict) == required_domains_for(verdict, band=None)
    assert "technologic" in required_domains_for(verdict)


def test_the_gate_never_raises_on_junk():
    """Same contract as `required_domains_for`: this runs inside
    `_persist_assessment`'s try block, where an exception is logged as "Failed
    to persist assessment" AFTER the row has already been committed."""
    for verdict in (None, [], "string", 7, {"recommendation": {"nested": True}}):
        assert isinstance(required_domains_for(verdict), frozenset)
    for band in (None, 7, [], {"a": 1}):
        assert isinstance(panel_is_owed("pass", band), bool)
