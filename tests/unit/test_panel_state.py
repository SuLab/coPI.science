""""No panel was owed" is not "the panel was verified complete" — and neither is
"we have no idea whether a floor ever ran".

`panel_state` (then `_panel_state`) had three values and `missing_domains` has
three states, so the
mapping looked total. It was not. NULL means "no gap recorded", and a verdict
reaches that state two very different ways:

* an `advance` or `conditional` verdict WAS held to the panel floor, the floor
  checked `required_domains_for` against the consults, and found nothing owed
  and unconsulted. A real verification.
* a verdict that was never held to the floor at all — the floor returns an empty
  set before looking at anything. Nothing was checked.

Both stored NULL and both rendered the green "Nothing the verdict's own content
owed a specialist was left unconsulted". For the second kind that sentence is
false on its own terms: production run 60c53424's pearce route-to-incubation
verdict has `required_domains_for` naming `clinical`, and clinical was never
consulted on that thread. The janak `pass` in the same run is starker — five
content-implied domains, zero consults, same green box.

**The first fix was itself wrong, and this module records why.** It split that
NULL by asking `panel_is_owed(recommendation, band)` AT RENDER TIME. That reads
the row through TODAY's rules, and the rule has moved twice this month: every row
written by the older, recommendation-only floor stored
`panel_incomplete=False, missing_domains=NULL` meaning "no panel was owed", and
the band-aware reader read the same NULL back as "the floor ran and found no
gap". 12 production rows rendered the green box for a panel nobody ever
evaluated, at least five of them with a demonstrable gap.

So the STORED COLUMN is now the sole authority for green.
`opportunity_assessments.panel_owed` records what the floor decided AT WRITE TIME
(see that column's comment: True = a panel was owed so the floor evaluated this
verdict, False = none was owed, NULL = the row predates 0036 and nobody knows),
and `panel_state` replays it instead of re-deriving it — the same discipline
`display_scale_for(assessment.rubric_version, …)` already applies to scoring.
`panel_is_owed` is deliberately absent from the read path; putting it back ahead
of the column test re-arms this exact bug the next time the predicate widens.

The floor's own exemption rules are NOT changed here. Only the claim the page
makes about a row it cannot see the floor's decision for.
"""

import inspect

from src.models import OpportunityAssessment
from src.services.assessment_detail import panel_state


def _assessment(**kwargs) -> OpportunityAssessment:
    """An unsaved row. `panel_state` is a pure read of three attributes, so it
    needs no session and no database.

    `panel_owed` defaults to None — the pre-0036 truth, and the honest default
    for a hand-rolled row: roughly 45 test constructions across this suite build
    an `OpportunityAssessment` without ever mentioning the column, and the safe
    landing spot for all of them is the state that is never green.
    """
    defaults = {
        "recommendation": "conditional",
        "panel_incomplete": False,
        "missing_domains": None,
        "panel_owed": None,
    }
    return OpportunityAssessment(**{**defaults, **kwargs})


def test_a_verified_advance_verdict_is_still_verified():
    """INVERTED 2026-08-22. The old assertion was the bug.

    This used to assert that an `advance`/`conditional` row with
    `panel_incomplete=False, missing_domains=NULL` is "verified", on the
    reasoning that those recommendations are held to the floor so an empty gap
    must be a real finding. That reasoning re-derives the floor's decision from
    today's predicate, and the row in front of it may well have been written by
    a floor that asked a different question. With no `panel_owed` on the row
    there is no evidence any floor ran, and inventing one is precisely the
    verification this column exists to stop asserting.
    """
    assert panel_state(_assessment(recommendation="advance")) == "unrecorded"
    assert panel_state(_assessment(recommendation="conditional")) == "unrecorded"


def test_a_floor_checked_verdict_with_no_gap_is_verified():
    """The one state that has earned the green box: the floor recorded that it
    was owed a panel (`panel_owed=True`), so it evaluated this verdict, and it
    recorded no gap. An empty gap on such a row is a real finding."""
    assert panel_state(_assessment(panel_owed=True)) == "verified"
    assert panel_state(
        _assessment(recommendation="advance", panel_owed=True)
    ) == "verified"


def test_an_owed_verdict_with_no_panel_record_is_not_called_verified():
    """The 12 production rows, stated as a rule. A verdict whose recommendation
    and band would be owed a panel under TODAY's floor, but whose row carries no
    record of any floor having run, is `unrecorded` — never `verified`."""
    row = _assessment(recommendation="advance", band="advance", panel_owed=None)
    assert panel_state(row) == "unrecorded"
    assert panel_state(row) != "verified"


def test_a_demonstrated_gap_is_still_a_gap():
    row = _assessment(panel_incomplete=True, missing_domains=["chemistry"])
    assert panel_state(row) == "gap"


def test_a_demonstrated_gap_outranks_a_recorded_panel_owed():
    """`panel_owed=True` is the precondition for green, not a licence for it:
    a row that also carries a gap is a gap, whatever the floor was owed."""
    row = _assessment(
        panel_incomplete=True, missing_domains=["chemistry"], panel_owed=True
    )
    assert panel_state(row) == "gap"


def test_an_unverifiable_floor_is_still_unverified():
    """`[]` — the floor could not be checked at all. Must not be swallowed by the
    new state: "could not check" and "nothing to check" are different findings."""
    assert panel_state(_assessment(missing_domains=[])) == "unverified"
    assert panel_state(_assessment(missing_domains=[], panel_owed=True)) == "unverified"


def test_a_pass_verdict_reports_that_no_panel_was_owed():
    """A `pass` the floor exempted, and RECORDED exempting: `panel_owed=False`
    is the floor's own answer, so the page may repeat it. Note what changed —
    the page no longer infers this from the recommendation, it reads it."""
    assert panel_state(
        _assessment(recommendation="pass", panel_owed=False)
    ) == "not_owed"


def test_a_route_to_incubation_verdict_now_owes_a_panel():
    """INVERTED 2026-08-22 (twice, and this is the second time).

    `route-to-incubation` used to be exempt from the floor, then became owed one,
    and this test moved with it — asserting "verified" for a row with no gap
    recorded. That is the production defect exactly: run 60c53424's pearce
    `route-to-incubation` row was written by the OLD floor, which exempted it and
    stored NULL for "no panel owed", and the new band-aware reader relabelled the
    same NULL as a completed audit. The page cannot know which floor wrote a row
    unless the row says so, and a pre-0036 row does not.
    """
    assert panel_state(
        _assessment(recommendation="route-to-incubation")
    ) == "unrecorded"


def test_an_exempt_verdict_with_a_real_gap_still_reports_the_gap():
    """`not_owed` must never outrank a recorded gap. The floor cannot produce one
    for an exempt recommendation today, but a stored row is the authority on what
    was found — reading a flagged row as "no panel owed" would silently unflag
    every historical row the floor did refuse."""
    row = _assessment(
        recommendation="pass", panel_incomplete=True, missing_domains=["talent"],
        panel_owed=False,
    )
    assert panel_state(row) == "gap"


def test_an_exempt_verdict_that_could_not_be_checked_reports_unverified():
    """`[]` also outranks `not_owed`: `_floor_unverifiable_reason` returns None for
    an exempt recommendation, so an exempt row should never carry `[]` — but if
    one does, "we could not look" is the more honest of the two."""
    row = _assessment(recommendation="pass", missing_domains=[], panel_owed=False)
    assert panel_state(row) == "unverified"


def test_a_missing_recommendation_is_owed_a_panel_rather_than_exempted():
    """INVERTED 2026-08-22.

    `recommendation` is nullable and degrades to None when the model omitted it
    or it was clipped. The old assertion was "verified", reached by asking
    `panel_is_owed(None)` — which fails CLOSED and answers True — and then
    treating an owed-but-unrecorded panel as a completed one. Failing closed is
    right for the FLOOR, which is deciding whether to convene a panel; it is
    exactly wrong for the PAGE, which is deciding whether to claim one happened.
    An unreadable recommendation on a row with no floor record is the least
    verified row on the surface, not the most.
    """
    assert panel_state(_assessment(recommendation=None)) == "unrecorded"


def test_the_read_path_never_re_derives_the_floor_s_decision():
    """The drift alarm, and the reason this module exists twice over.

    `panel_is_owed` answers "would a panel be owed under TODAY's rules", which is
    a different question from "was one owed when this row was written" — and the
    predicate has widened twice in 2026-08 alone, silently relabelling every
    older row each time. The column is the answer; re-deriving it here is the
    bug, so its absence is asserted structurally rather than left to reviewer
    memory.
    """
    # The docstring NAMES the predicate (it has to — it is the whole account of
    # why the predicate is gone), so the body is what is checked, not the prose.
    body = inspect.getsource(panel_state).replace(panel_state.__doc__ or "", "")
    assert "panel_is_owed" not in body
    assert "PANEL_REQUIRED_FOR" not in body
    assert "panel_owed" in body
    # And the module must not carry the import either: an unused import here is
    # an invitation to re-introduce the call.
    # Tightened after review: `"panel_is_owed," not in module_source` let a SOLE
    # `from src.agent.specialists import panel_is_owed` — no trailing comma —
    # through. The body assertion above still catches any USE, so this is
    # belt-and-braces, but a belt with a hole in it is worse than no belt: it
    # reads as coverage. Checked against the module's real symbol table instead
    # of its text, which no import spelling can dodge.
    module = inspect.getmodule(panel_state)
    assert not hasattr(module, "panel_is_owed")
    assert not hasattr(module, "PANEL_REQUIRED_FOR")
    assert not hasattr(module, "PANEL_EXEMPT_RECOMMENDATIONS")
