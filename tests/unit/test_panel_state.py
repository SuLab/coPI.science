""""No panel was owed" is not "the panel was verified complete".

`_panel_state` had three values and `missing_domains` has three states, so the
mapping looked total. It was not. NULL means "no gap recorded", and a verdict
reaches that state two very different ways:

* an `advance` or `conditional` verdict WAS held to the panel floor, the floor
  checked `required_domains_for` against the consults, and found nothing owed
  and unconsulted. A real verification.
* a `pass` or `route-to-incubation` verdict was never held to the floor at all —
  `_PANEL_REQUIRED_FOR` covers only advance/conditional, so `_specialist_floor_gap`
  returns an empty set before looking at anything. Nothing was checked.

Both stored NULL and both rendered the green "Nothing the verdict's own content
owed a specialist was left unconsulted". For the second kind that sentence is
false on its own terms: production run 60c53424's pearce route-to-incubation
verdict has `required_domains_for` naming `clinical`, and clinical was never
consulted on that thread. The janak `pass` in the same run is starker — five
content-implied domains, zero consults, same green box.

The exemption itself is deliberate and documented
(`prompts/roles/scout_hub/phase4-thread-reply.md:83`, "`pass` and
`route-to-incubation` verdicts require no panel at all") and is NOT changed here.
Only the claim the page makes about it is.
"""

from src.agent.specialists import PANEL_REQUIRED_FOR
from src.models import OpportunityAssessment
from src.services.assessment_detail import _panel_state


def _assessment(**kwargs) -> OpportunityAssessment:
    """An unsaved row. `_panel_state` is a pure read of three attributes, so it
    needs no session and no database."""
    defaults = {
        "recommendation": "conditional",
        "panel_incomplete": False,
        "missing_domains": None,
    }
    return OpportunityAssessment(**{**defaults, **kwargs})


def test_a_verified_advance_verdict_is_still_verified():
    """The floor ran and found no gap. Unchanged — this is the only state that
    has earned the green "no gap recorded" claim."""
    assert _panel_state(_assessment(recommendation="advance")) == "verified"
    assert _panel_state(_assessment(recommendation="conditional")) == "verified"


def test_a_demonstrated_gap_is_still_a_gap():
    row = _assessment(panel_incomplete=True, missing_domains=["chemistry"])
    assert _panel_state(row) == "gap"


def test_an_unverifiable_floor_is_still_unverified():
    """`[]` — the floor could not be checked at all. Must not be swallowed by the
    new state: "could not check" and "nothing to check" are different findings."""
    assert _panel_state(_assessment(missing_domains=[])) == "unverified"


def test_a_pass_verdict_reports_that_no_panel_was_owed():
    """A `pass` is exempt from the floor, so nothing about its panel was ever
    evaluated. Claiming a verification here is the failure."""
    assert _panel_state(_assessment(recommendation="pass")) == "not_owed"


def test_a_route_to_incubation_verdict_now_owes_a_panel():
    """The production case, inverted 2026-08-22.

    `route-to-incubation` commits Blackbird to incubating an idea, and it used to
    be EXEMPT from the floor — exempted alongside `pass` on the reasoning that "a
    decline costs Blackbird nothing". It is not a decline: it is the grant
    Blackbird exists to award, which made it the one positive verdict class
    nobody reviewed. It is now owed a panel, and the page must say so or it will
    describe a floor the engine no longer runs.
    """
    assert _panel_state(_assessment(recommendation="route-to-incubation")) == "verified"


def test_an_exempt_verdict_with_a_real_gap_still_reports_the_gap():
    """`not_owed` must never outrank a recorded gap. The floor cannot produce one
    for an exempt recommendation today, but a stored row is the authority on what
    was found — reading a flagged row as "no panel owed" would silently unflag
    every historical row the floor did refuse."""
    row = _assessment(
        recommendation="pass", panel_incomplete=True, missing_domains=["talent"]
    )
    assert _panel_state(row) == "gap"


def test_an_exempt_verdict_that_could_not_be_checked_reports_unverified():
    """`[]` also outranks `not_owed`: `_floor_unverifiable_reason` returns None for
    an exempt recommendation, so an exempt row should never carry `[]` — but if
    one does, "we could not look" is the more honest of the two."""
    row = _assessment(recommendation="pass", missing_domains=[])
    assert _panel_state(row) == "unverified"


def test_a_missing_recommendation_is_owed_a_panel_rather_than_exempted():
    """`recommendation` is nullable and degrades to None when the model omitted
    it or it was clipped.

    This used to render `not_owed`, which reads as "the floor had no business
    here" — but an unreadable recommendation is precisely the case where nobody
    can say that. `panel_is_owed` now fails CLOSED, so an unknown recommendation
    is owed a panel: a wrongly-owed panel costs a flag on a row, a wrongly-exempt
    one costs the review of a funding decision.
    """
    assert _panel_state(_assessment(recommendation=None)) == "verified"


def test_the_exempt_set_is_the_engine_s_own():
    """The display state and the floor must read one definition. If they drift,
    the page starts describing a floor the engine does not run."""
    assert PANEL_REQUIRED_FOR == frozenset({"advance", "conditional"})
