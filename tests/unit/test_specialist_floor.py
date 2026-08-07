"""Consults are recorded during the phase-4 interview and read during the
separate phase-5 assessment turn. That seam — two LLM calls apart — is what the
whole enforcement floor rests on.

See docs/specs/2026-08-07-nine-evaluator-panel-design.md §4.
"""
import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine(*agents):
    return SimulationEngine(agents=list(agents), slack_clients={})


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def test_the_consult_map_starts_empty():
    eng = _engine(_hub())
    assert eng._specialist_consults == {}


def test_recording_a_consult_is_keyed_by_thread():
    eng = _engine(_hub())
    eng._record_consult("gill", "chemistry")
    eng._record_consult("gill", "legal")
    eng._record_consult("pearce", "scientific")
    assert eng._specialist_consults["gill"] == {"chemistry", "legal"}
    assert eng._specialist_consults["pearce"] == {"scientific"}


def test_recording_the_same_domain_twice_is_idempotent():
    eng = _engine(_hub())
    eng._record_consult("gill", "chemistry")
    eng._record_consult("gill", "chemistry")
    assert eng._specialist_consults["gill"] == {"chemistry"}


def test_consults_for_an_unknown_thread_read_as_empty():
    """The floor reads this for a thread it may never have seen — after a
    restart, for instance. It must not KeyError inside _persist_assessment."""
    eng = _engine(_hub())
    assert eng._consulted_domains("never-seen-pi") == frozenset()


# --- the floor ---------------------------------------------------------------


@pytest.mark.parametrize(
    "recommendation,consulted,expected_missing",
    [
        # pass and route-to-incubation never need a panel
        ("pass", set(), set()),
        ("route-to-incubation", set(), set()),
        # advance always needs scientific + talent. "budget" (never a member
        # of the default-required set) stands in for "consulted is empty"
        # here: an EMPTY consulted set on thread "t1" is indistinguishable
        # from a thread the map never saw at all, which is the fail-open case
        # pinned separately by test_the_floor_fails_open_for_a_thread_we_never_saw
        # — it would swallow this row's arithmetic instead of exercising it.
        # Recording one irrelevant domain marks the thread as "seen" (per
        # test_fail_open_applies_only_to_a_thread_with_NO_consults) without
        # satisfying scientific/talent.
        ("advance", {"budget"}, {"scientific", "talent"}),
        ("advance", {"scientific"}, {"talent"}),
        ("advance", {"scientific", "talent"}, set()),
        # conditional is held to the same bar as advance
        ("conditional", {"budget"}, {"scientific", "talent"}),
        ("conditional", {"scientific", "talent"}, set()),
    ],
)
def test_floor_arithmetic(recommendation, consulted, expected_missing):
    eng = _engine(_hub())
    for d in consulted:
        eng._record_consult("gill", d)
    missing = eng._specialist_floor_gap({"recommendation": recommendation, "subject_agent_id": "gill"})
    assert missing == expected_missing


def test_chemical_matter_pulls_chemistry_into_the_floor():
    eng = _engine(_hub())
    for d in ("scientific", "talent"):
        eng._record_consult("gill", d)
    verdict = {
        "recommendation": "advance",
        "subject_agent_id": "gill",
        "company_or_project": "A small molecule SCAP inhibitor",
    }
    assert eng._specialist_floor_gap(verdict) == {"chemistry"}


def test_a_fully_consulted_verdict_has_no_gap():
    eng = _engine(_hub())
    for d in ("scientific", "talent", "chemistry", "clinical"):
        eng._record_consult("gill", d)
    verdict = {
        "recommendation": "advance",
        # Without a subject the floor fails open, so a test asserting an empty
        # gap would pass whether or not the consults were found. Name the PI so
        # this asserts the real path.
        "subject_agent_id": "gill",
        "company_or_project": "A small molecule inhibitor",
        "rationale": "for the treatment of a rare disease",
    }
    assert eng._specialist_floor_gap(verdict) == set()


def test_the_floor_fails_open_for_a_thread_we_never_saw():
    """Post-restart. An empty map must not block every assessment."""
    eng = _engine(_hub())
    verdict = {"recommendation": "advance"}
    assert eng._specialist_floor_gap(verdict) == set()


def test_fail_open_applies_only_to_a_thread_with_NO_consults():
    """A thread with one consult is a thread we DID see, so the rest are owed.
    Otherwise a hub could consult one cheap specialist and buy an exemption."""
    eng = _engine(_hub())
    eng._record_consult("gill", "budget")
    assert eng._specialist_floor_gap({"recommendation": "advance", "subject_agent_id": "gill"}) == {
        "scientific", "talent",
    }


# --- the join key: PI, not thread --------------------------------------------
#
# An assessment is a NEW TOP-LEVEL POST, never a reply in the interview thread,
# so `_persist_assessment` has no thread_id to offer. Keying the consult record
# on thread_id therefore made the floor read an empty set every single time and
# fail open on every verdict — enforcing nothing, while looking enforced.
#
# The interview thread knows the PI as `other_agent_id`; the verdict names the
# same PI as `subject_agent_id`. The PI is the join key; the thread never was.


def test_consults_are_recorded_against_the_pi_not_the_thread():
    eng = _engine(_hub())
    eng._record_consult("gill", "chemistry")
    assert eng._consulted_domains("gill") == {"chemistry"}


def test_the_floor_finds_consults_via_subject_agent_id():
    """The regression that made the floor inert: the writer keyed on the
    interview thread, the reader had only the verdict, and they never met."""
    eng = _engine(_hub())
    for d in ("scientific", "talent"):
        eng._record_consult("gill", d)

    verdict = {"recommendation": "advance", "subject_agent_id": "gill"}
    assert eng._specialist_floor_gap(verdict) == set()


def test_the_floor_bites_for_a_pi_whose_panel_was_skipped():
    eng = _engine(_hub())
    eng._record_consult("gill", "scientific")          # gill's panel is partial
    eng._record_consult("pearce", "scientific")        # a different PI entirely
    eng._record_consult("pearce", "talent")

    verdict = {"recommendation": "advance", "subject_agent_id": "gill"}
    assert eng._specialist_floor_gap(verdict) == {"talent"}


def test_one_pis_consults_do_not_satisfy_anothers():
    """The hub interviews 55 PIs concurrently. Consulting Chemistry about
    pearce's compound says nothing about gill's."""
    eng = _engine(_hub())
    for d in ("scientific", "talent", "chemistry"):
        eng._record_consult("pearce", d)

    verdict = {
        "recommendation": "advance",
        "subject_agent_id": "gill",
        "company_or_project": "A small molecule inhibitor",
    }
    gap = eng._specialist_floor_gap(verdict)
    assert "chemistry" in gap and "scientific" in gap


def test_a_verdict_with_no_subject_fails_open():
    """No subject_agent_id means nothing to join on. Fail open and say so,
    rather than refusing every unattributed verdict."""
    eng = _engine(_hub())
    eng._record_consult("gill", "scientific")
    assert eng._specialist_floor_gap({"recommendation": "advance"}) == set()
