"""Consults are recorded during the phase-4 interview and read during the
separate phase-5 assessment turn. That seam — two LLM calls apart — is what the
whole enforcement floor rests on.

See docs/specs/2026-08-07-nine-evaluator-panel-design.md §4.
"""
import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine


def _engine(*agents):
    return SimulationEngine(agents=list(agents), slack_clients={})


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def _activated_thread(eng, thread_id, *, other_agent_id, channel="general"):
    """Drive a REAL activation site — the hub auto-activation loop inside
    `_phase3_activate_threads` — rather than hand-building a `ThreadState`, so
    this test exercises the exact code path that must snapshot `floor_armed`.

    Asserts the invariant every activation site owes: `floor_armed` reflects
    `bool(engine._specialist_consults)` at the MOMENT of activation, not
    whatever the map happens to hold later.
    """
    hub = eng.agents["blackbird"]
    hub.state.subscribed_channels.add(channel)
    expected_armed = bool(eng._specialist_consults)
    eng.message_log.append(
        LogEntry(
            ts=thread_id,
            channel=channel,
            sender_agent_id=other_agent_id,
            sender_name=f"{other_agent_id}Bot",
            content="An update from the lab.",
            posted_at=1.0,
            is_bot=True,
        )
    )
    eng._phase3_activate_threads(hub)
    thread = hub.state.active_threads[thread_id]
    assert thread.floor_armed is expected_armed, (
        "floor_armed must be snapshotted from the live map at activation time"
    )
    return thread


def test_the_consult_map_starts_empty():
    eng = _engine(_hub())
    assert eng._specialist_consults == {}


def test_recording_a_consult_is_keyed_by_thread():
    eng = _engine(_hub())
    eng._record_consult("gill", "chemistry")
    eng._record_consult("gill", "legal")
    eng._record_consult("pearce", "scientific")
    # No thread_id given -> keyed under the None-keyed slot for each PI.
    assert eng._specialist_consults[("gill", None)] == {"chemistry", "legal"}
    assert eng._specialist_consults[("pearce", None)] == {"scientific"}


def test_recording_the_same_domain_twice_is_idempotent():
    eng = _engine(_hub())
    eng._record_consult("gill", "chemistry")
    eng._record_consult("gill", "chemistry")
    assert eng._specialist_consults[("gill", None)] == {"chemistry"}


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


# --- the join key: the PI half -----------------------------------------------
#
# The interview thread knows the PI as `other_agent_id`; the verdict names the
# same PI as `subject_agent_id`. That identity is still half of the join key
# today. (The other half is the thread — see
# test_a_second_interview_does_not_inherit_the_first_ones_consults and
# test_the_floor_reads_the_consults_of_this_interview_only below, and
# `_specialist_floor_gap`'s docstring: keying on the PI alone let a PI's
# second interview inherit the first interview's consults and skip its own
# panel.) None of the tests in this section pass a `thread_id`, so they all
# read and write the same `(pi, None)` slot and still exercise the PI-name
# half of the join on its own.


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


# --- snapshotting the fail-open decision at activation ------------------------
#
# `_specialist_floor_gap`'s "map empty overall => fail open" read used to be
# LIVE at persist time, long after an interview began. Under concurrency, a
# DIFFERENT interview's first-ever consult flips the global map from empty to
# non-empty mid-flight, retroactively arming the floor for an in-flight verdict
# that began under fail-open — refusing it after the concluding reply is
# already in Slack, with no later turn to recover it. The fix snapshots the
# decision onto `ThreadState.floor_armed` at activation and consults that
# snapshot instead of the live global.


def test_fail_open_is_decided_at_activation_not_at_persist_time():
    """Another thread's first consult must not retroactively arm the floor for
    an interview that started while the map was empty."""
    eng = _engine(_hub())
    thread = _activated_thread(eng, "t1", other_agent_id="wang")
    assert thread.floor_armed is False   # map was empty when this began

    # A DIFFERENT interview consults someone. The global map is no longer empty.
    eng._record_consult("someone-else", "scientific")

    verdict = {"recommendation": "advance", "subject_agent_id": "wang"}
    assert eng._specialist_floor_gap(verdict, thread=thread) == set(), (
        "this interview began under fail-open and must stay there"
    )


def test_floor_armed_true_when_activated_after_another_consult_exists():
    """The complementary case: an interview that starts AFTER the map is
    already non-empty is armed from the start, and the floor bites normally."""
    eng = _engine(_hub())
    eng._record_consult("someone-else", "scientific")

    thread = _activated_thread(eng, "t2", other_agent_id="wang")
    assert thread.floor_armed is True

    verdict = {"recommendation": "advance", "subject_agent_id": "wang"}
    assert eng._specialist_floor_gap(verdict, thread=thread) == {"scientific", "talent"}


def test_specialist_floor_gap_thread_none_falls_back_to_live_global():
    """Direct callers (existing tests, and any caller with no thread) keep the
    old process-global behavior unchanged."""
    eng = _engine(_hub())
    verdict = {"recommendation": "advance", "subject_agent_id": "gill"}
    # Empty map overall -> fails open, exactly as before this change.
    assert eng._specialist_floor_gap(verdict) == set()
    assert eng._specialist_floor_gap(verdict, thread=None) == set()


def test_a_second_interview_does_not_inherit_the_first_ones_consults():
    """One PI, two interviews: the second must convene its own panel.

    `huganir` was assessed 4 times in run 1787010946 and `hart` 4 times. Under
    PI-only keying every assessment after the first rode on the first
    interview's consults.
    """
    eng = _engine(_hub())
    eng._record_consult("huganir", "chemistry", thread_id="t1")

    assert eng._consulted_domains("huganir", "t1") == frozenset({"chemistry"})
    assert eng._consulted_domains("huganir", "t2") == frozenset(), (
        "a different interview with the same PI starts with no panel"
    )


def test_the_floor_reads_the_consults_of_this_interview_only():
    eng = _engine(_hub())
    thread_one = _activated_thread(eng, "t1", other_agent_id="huganir")
    for domain in ("scientific", "talent"):
        eng._record_consult("huganir", domain, thread_id="t1")
    thread_two = _activated_thread(eng, "t2", other_agent_id="huganir")
    thread_two.floor_armed = True

    verdict = {
        "subject_agent_id": "huganir",
        "recommendation": "advance",
        "rationale": "No cues here.",
    }
    assert eng._specialist_floor_gap(verdict, thread=thread_one) == set()
    assert eng._specialist_floor_gap(verdict, thread=thread_two) == {
        "scientific", "talent",
    }


def test_a_consult_without_a_thread_is_still_recorded():
    """Direct callers and pre-existing tests pass no thread; they must keep
    working, keyed under a None interview."""
    eng = _engine(_hub())
    eng._record_consult("huganir", "scientific")
    assert eng._consulted_domains("huganir") == frozenset({"scientific"})
