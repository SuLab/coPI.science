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
    eng._record_consult("t1", "chemistry")
    eng._record_consult("t1", "legal")
    eng._record_consult("t2", "scientific")
    assert eng._specialist_consults["t1"] == {"chemistry", "legal"}
    assert eng._specialist_consults["t2"] == {"scientific"}


def test_recording_the_same_domain_twice_is_idempotent():
    eng = _engine(_hub())
    eng._record_consult("t1", "chemistry")
    eng._record_consult("t1", "chemistry")
    assert eng._specialist_consults["t1"] == {"chemistry"}


def test_consults_for_an_unknown_thread_read_as_empty():
    """The floor reads this for a thread it may never have seen — after a
    restart, for instance. It must not KeyError inside _persist_assessment."""
    eng = _engine(_hub())
    assert eng._consulted_domains("never-seen") == frozenset()


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
        eng._record_consult("t1", d)
    missing = eng._specialist_floor_gap(
        {"recommendation": recommendation}, "t1"
    )
    assert missing == expected_missing


def test_chemical_matter_pulls_chemistry_into_the_floor():
    eng = _engine(_hub())
    for d in ("scientific", "talent"):
        eng._record_consult("t1", d)
    verdict = {
        "recommendation": "advance",
        "company_or_project": "A small molecule SCAP inhibitor",
    }
    assert eng._specialist_floor_gap(verdict, "t1") == {"chemistry"}


def test_a_fully_consulted_verdict_has_no_gap():
    eng = _engine(_hub())
    for d in ("scientific", "talent", "chemistry", "clinical"):
        eng._record_consult("t1", d)
    verdict = {
        "recommendation": "advance",
        "company_or_project": "A small molecule inhibitor",
        "rationale": "for the treatment of a rare disease",
    }
    assert eng._specialist_floor_gap(verdict, "t1") == set()


def test_the_floor_fails_open_for_a_thread_we_never_saw():
    """Post-restart. An empty map must not block every assessment."""
    eng = _engine(_hub())
    verdict = {"recommendation": "advance"}
    assert eng._specialist_floor_gap(verdict, "unseen-thread") == set()


def test_fail_open_applies_only_to_a_thread_with_NO_consults():
    """A thread with one consult is a thread we DID see, so the rest are owed.
    Otherwise a hub could consult one cheap specialist and buy an exemption."""
    eng = _engine(_hub())
    eng._record_consult("t1", "budget")
    assert eng._specialist_floor_gap({"recommendation": "advance"}, "t1") == {
        "scientific", "talent",
    }
