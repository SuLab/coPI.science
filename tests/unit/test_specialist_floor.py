"""Consults are recorded during the phase-4 interview and read during the
separate phase-5 assessment turn. That seam — two LLM calls apart — is what the
whole enforcement floor rests on.

See docs/specs/2026-08-07-nine-evaluator-panel-design.md §4.
"""
import logging

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


def test_recording_a_consult_with_no_thread_is_keyed_by_pi_alone():
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


def test_a_consult_with_no_thread_round_trips_under_the_pis_none_slot():
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
    thread_one.floor_armed = True
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


# --- "no gap" vs "no way to tell" --------------------------------------------
#
# `_specialist_floor_gap` returns an empty set in both cases and cannot tell
# them apart by design (its signature is pinned by every test above).
# `_floor_verifiable` is the sibling that answers the other half, and
# `_persist_assessment` needs both to write the column's three states.


def test_a_complete_panel_is_verifiable():
    eng = _engine(_hub())
    thread = _activated_thread(eng, "t1", other_agent_id="gill")
    for domain in ("scientific", "talent"):
        eng._record_consult("gill", domain, thread_id="t1")
    thread.floor_armed = True

    verdict = {
        "recommendation": "advance", "subject_agent_id": "gill",
        "rationale": "No cues here.",
    }
    assert eng._specialist_floor_gap(verdict, thread=thread) == set()
    assert eng._floor_verifiable(verdict, thread=thread) is True


def test_a_real_gap_is_verifiable_by_definition():
    """A named gap can only come from the path that could check. If this ever
    returned False the row would carry both a gap AND the unverified sentinel,
    which is incoherent."""
    eng = _engine(_hub())
    eng._record_consult("pearce", "scientific")
    verdict = {"recommendation": "advance", "subject_agent_id": "gill"}
    assert eng._specialist_floor_gap(verdict) == {"scientific", "talent"}
    assert eng._floor_verifiable(verdict) is True


def test_an_unarmed_floor_is_not_verifiable():
    """The post-restart case: no consult recorded for anyone, so an absent
    record for this PI proves nothing either way."""
    eng = _engine(_hub())
    verdict = {"recommendation": "advance", "subject_agent_id": "gill"}
    assert eng._specialist_floor_gap(verdict) == set()
    assert eng._floor_verifiable(verdict) is False


def test_an_unarmed_thread_is_not_verifiable_even_when_the_map_is_not_empty():
    """`floor_armed` is the authority for a thread, not the live map — the
    same read `_specialist_floor_gap` makes, so the two cannot disagree about
    one verdict."""
    eng = _engine(_hub())
    thread = _activated_thread(eng, "t1", other_agent_id="gill")
    assert thread.floor_armed is False
    eng._record_consult("someone-else", "scientific")  # lands after activation

    verdict = {"recommendation": "advance", "subject_agent_id": "gill"}
    assert eng._specialist_floor_gap(verdict, thread=thread) == set()
    assert eng._floor_verifiable(verdict, thread=thread) is False


def test_a_verdict_with_no_subject_is_not_verifiable():
    eng = _engine(_hub())
    eng._record_consult("gill", "scientific")          # armed
    verdict = {"recommendation": "advance"}
    assert eng._specialist_floor_gap(verdict) == set()
    assert eng._floor_verifiable(verdict) is False


def test_a_verdict_that_owes_no_panel_is_verifiable():
    """A `pass` is exempt from the panel entirely, so nothing about it failed
    to verify — otherwise the unverified sentinel would land on the commonest
    verdict there is and mean nothing.

    `route-to-incubation` used to sit in this list, exempted on the reasoning
    that "a decline costs Blackbird nothing". It is not a decline: it is the
    incubation grant Blackbird exists to award, so it was the one verdict class
    that most needed a panel and the only positive one that never got it. It now
    owes a panel — see the test below.
    """
    eng = _engine(_hub())
    assert eng._specialist_consults == {}              # unarmed, as after a restart
    verdict = {"recommendation": "pass", "subject_agent_id": "gill"}
    assert eng._specialist_floor_gap(verdict) == set()
    assert eng._floor_verifiable(verdict) is True


def test_an_unreadable_recommendation_owes_a_panel():
    """`panel_is_owed` fails CLOSED, and this pins the direction.

    The old test was "not in {advance, conditional} ⇒ exempt", so a missing,
    empty or off-contract recommendation bought an exemption silently. The
    asymmetry is cheap in one direction only: a wrongly-owed panel costs a
    `panel_incomplete` flag on a row, a wrongly-exempt one costs the review of a
    funding decision.
    """
    eng = _engine(_hub())
    eng._specialist_consults = {"gill": {"scientific"}}   # armed
    for recommendation in (None, "", "definitely-maybe"):
        verdict = {"recommendation": recommendation, "subject_agent_id": "gill"}
        assert eng._specialist_floor_gap(verdict), recommendation


def test_route_to_incubation_owes_a_panel():
    """Blackbird's own positive outcome is held to the floor like any other."""
    eng = _engine(_hub())
    eng._specialist_consults = {"gill": {"scientific"}}   # armed, one domain done
    verdict = {"recommendation": "route-to-incubation", "subject_agent_id": "gill"}

    assert eng._specialist_floor_gap(verdict), (
        "route-to-incubation must owe the panel it used to be exempt from"
    )


def test_a_conditional_band_with_a_pass_recommendation_still_owes_a_panel():
    """The floor keys on the COMPUTED band as well as the written recommendation.

    Keying on the model's `recommendation` alone let a verdict that scores into
    `conditional` exempt itself by writing `pass` — 3 of the 4 conditional bands
    in the v2 corpus do exactly that, so the band that is supposed to trigger
    diligence was the one buying an exemption from review.
    """
    eng = _engine(_hub())
    eng._specialist_consults = {"gill": {"scientific"}}
    # Scores chosen to band above the conditional line (all 4s -> 4.0).
    verdict = {
        "recommendation": "pass",
        "subject_agent_id": "gill",
        "funnel_stage": "incubation",
        "scores": {k: 4 for k in (
            "differentiation_unmet_need", "scientific_credibility",
            "translational_path", "fundable_experiment", "venture_potential",
            "team_executability",
        )},
    }
    _, band = eng._computed_score_and_band(verdict)
    assert band in ("advance", "conditional"), band

    assert eng._specialist_floor_gap(verdict), (
        "a verdict scoring into a diligence band must owe a panel even when the "
        "model wrote 'pass'"
    )


# --- signal-mix report & domain-flatness warning (Task 5, 2026-08-28) ------
#
# The clear-rate monitor (Task 9) is RETIRED. It asserted that a low `clear`
# share meant the panel could not discriminate; a 48-consult positive control
# falsified that (blocking 87.5% -> 0% across a quality ladder, p = 5.1e-07),
# and its floor sat ABOVE the rate a correct panel produces on this
# population. `signal_mix_report` replaces it with an unconditional INFO
# report of the run's mix at n>=50 (same sample floor, renamed reasoning) —
# there is no replacement threshold, because the optimal operating point for a
# screening instrument is a likelihood ratio, not a fixed floor on the output
# rate. `domain_flatness_warning` is the only piece that still stays quiet for
# a healthy (varied) domain, and it is worded as a prompt to measure, never a
# verdict — see docs/audits/2026-08-27-consult-persona-calibration/.
#
# These pin the tally, the sample floor, and the unconditional-vs-quiet split
# directly, rather than relying on the branch merely executing (as the
# pre-existing `total == 0` coverage in test_simulation_logic.py's
# TestGracefulShutdown did).

# Substrings of the messages `specialists.signal_mix_report` and
# `specialists.domain_flatness_warning` build, kept distinct so a mix-report
# assertion cannot be satisfied by a flatness line or vice versa. The retired
# alarm's own marker went through exactly this failure once already — it used
# to be "NOT ONE returned", from when the alarm was a ZERO test, and once it
# became a rate test an assertion looking for that old string passed
# *vacuously* on the "does not warn" cases, because the new warning fired and
# the test could not see it. Anchor on the invariant part of each message, not
# on the tally.
_MIX_REPORT_MARKER = "signal mix over"
_FLATNESS_WARNING_MARKER = "one-sided domain"


def test_note_consult_records_the_domain_and_tallies_the_signal():
    """`_note_consult` is a thin wrapper: it must do all three things
    `_record_consult` and the two tallies each do alone, not just one of them."""
    eng = _engine(_hub())
    eng._note_consult("wang", "legal", "caution", thread_id="t1")

    assert eng._consulted_domains("wang", "t1") == frozenset({"legal"}), (
        "_note_consult must still satisfy the specialist floor, exactly like "
        "_record_consult"
    )
    assert eng._consult_signal_counts == {"caution": 1}
    assert eng._consult_signal_counts_by_domain == {"legal": {"caution": 1}}

    # A second call, same signal, different domain: the floor gains a domain,
    # both tallies accumulate rather than overwrite.
    eng._note_consult("wang", "scientific", "caution", thread_id="t1")
    assert eng._consulted_domains("wang", "t1") == frozenset({"legal", "scientific"})
    assert eng._consult_signal_counts == {"caution": 2}
    assert eng._consult_signal_counts_by_domain == {
        "legal": {"caution": 1},
        "scientific": {"caution": 1},
    }


@pytest.mark.asyncio
async def test_stop_reports_the_signal_mix_at_fifty_or_more_consults(caplog):
    """Premise of the retired alarm dies here: "never clears" is no longer a
    warning at all. `stop()` now REPORTS the mix, at INFO, once the sample
    floor is met — unconditionally, on whatever the mix turns out to be. This
    exact tally (no `clear` at all) used to be the alarm's own trigger case;
    it is now simply reported like any other."""
    eng = _engine(_hub())
    eng._consult_signal_counts = {"caution": 30, "blocking": 20}  # 50 total, no clear

    with caplog.at_level(logging.INFO, logger="src.agent.simulation"):
        await eng.stop()

    assert _MIX_REPORT_MARKER in caplog.text
    assert "50" in caplog.text, "the operator needs the denominator"


@pytest.mark.asyncio
async def test_stop_does_not_warn_of_flatness_for_a_domain_with_real_variance(caplog):
    """This IS the retired concept, relocated rather than deleted:
    `clear_rate_warning` used to stay silent for a panel with real variance.
    The mix report now fires regardless (see above), so the only thing that
    still stays quiet for a healthy panel is the per-domain flatness warning
    — and it must stay quiet for a domain that is genuinely discriminating
    (33% blocking here), not just for one that has cleared something."""
    eng = _engine(_hub())
    eng._consult_signal_counts = {"caution": 25, "blocking": 20, "clear": 5}
    eng._consult_signal_counts_by_domain = {
        "chemistry": {"caution": 55, "blocking": 27},
    }

    with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
        await eng.stop()

    assert _FLATNESS_WARNING_MARKER not in caplog.text


@pytest.mark.asyncio
async def test_stop_warns_of_flatness_for_a_genuinely_one_sided_domain(caplog):
    """The positive-path counterpart to the test above, driven through the
    real `stop()` wiring rather than only through the pure function
    (`test_specialists.py::test_a_domain_stuck_on_one_label_is_named` covers
    that). Without this, a bug in the for-loop itself — wrong attribute name,
    wrong logger method, an exception swallowed before the loop runs — would
    be invisible to this suite, since every other engine-level test here
    asserts the warning's ABSENCE.

    `domain_flatness_warning` logs via `logger.warning`
    (`simulation.py:1221`), so WARNING is the correct capture level here —
    deliberately the opposite choice from the mix-report tests above, which
    log at INFO. The two must not be interchanged.
    """
    eng = _engine(_hub())
    eng._consult_signal_counts_by_domain = {
        "legal": {"caution": 87, "blocking": 4},  # 91 total, 95.6% modal
    }

    with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
        await eng.stop()

    assert _FLATNESS_WARNING_MARKER in caplog.text
    assert "legal" in caplog.text


@pytest.mark.asyncio
async def test_the_mix_report_states_the_clear_share_however_small(caplog):
    """The regression the retired alarm was rebuilt for is moot now: there is
    no threshold left to silence. Under the old zero test, ONE `clear` bought
    silence no matter how many consults surrounded it — run 8b64a0e0 supplied
    exactly that, 1 clear in 168 consults (0.6%), the only `clear` in the
    database across every run ever. The mix report does not need an escape
    hatch for that case because it never goes quiet on account of the mix: it
    STATES the clear share, however small, alongside every other label.
    """
    eng = _engine(_hub())
    eng._consult_signal_counts = {"caution": 29, "blocking": 20, "clear": 1}

    with caplog.at_level(logging.INFO, logger="src.agent.simulation"):
        await eng.stop()

    assert _MIX_REPORT_MARKER in caplog.text
    assert "clear 1" in caplog.text
    assert "2.0%" in caplog.text


@pytest.mark.asyncio
async def test_stop_does_not_warn_below_the_threshold(caplog):
    """The mix report logs at INFO (`signal_mix_report` -> `logger.info`), so
    the capture level here must be INFO, not WARNING: under a WARNING-level
    capture `Logger.isEnabledFor(INFO)` is False and the assertion below would
    pass unconditionally regardless of whether the n>=50 floor exists at all —
    a fix-round finding (2026-08-28) against an earlier version of this test
    that used WARNING here and could not fail no matter what the floor did."""
    eng = _engine(_hub())
    eng._consult_signal_counts = {"caution": 49}  # one short of 50, still no clear

    with caplog.at_level(logging.INFO, logger="src.agent.simulation"):
        await eng.stop()

    assert _MIX_REPORT_MARKER not in caplog.text


@pytest.mark.asyncio
async def test_stop_is_safe_with_an_empty_tally(caplog):
    """No consults happened at all this run (e.g. a run with no interviews) —
    stop() must still complete cleanly and must not warn.

    Captured at INFO deliberately, not WARNING: INFO is the lower of the two
    levels either function could log at (`signal_mix_report` -> INFO,
    `domain_flatness_warning` -> WARNING), and `Logger.isEnabledFor` treats a
    WARNING record as enabled under an INFO-level capture too, so one context
    manager here catches a regression in either guard rather than only one.
    """
    eng = _engine(_hub())
    assert eng._consult_signal_counts == {}
    assert eng._consult_signal_counts_by_domain == {}

    with caplog.at_level(logging.INFO, logger="src.agent.simulation"):
        await eng.stop()

    assert _MIX_REPORT_MARKER not in caplog.text
    assert _FLATNESS_WARNING_MARKER not in caplog.text
