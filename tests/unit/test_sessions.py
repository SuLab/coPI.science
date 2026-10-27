"""src/agent/sessions.py: one session per start of a system, its limits, its wind-down and how
the run ends (the independent-controls design §4)."""

from datetime import UTC, datetime, timedelta

import pytest

from src.agent.sessions import (
    ENDED,
    FAILURE_STOP,
    INTERVIEWS,
    LIMIT_COUNT,
    LIMIT_TIME,
    PROJECTS,
    REASON_LABELS,
    RUNNING,
    STARTED,
    STOP_EVERYTHING,
    STOP_NOW,
    WIND_DOWN,
    WINDING_DOWN,
    Limits,
    Sessions,
    describe_records,
    systems_from_start_payload,
)

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)


def test_start_twice_or_unknown_raises():
    s = Sessions()
    s.start(INTERVIEWS, Limits(), T0)
    with pytest.raises(ValueError, match="already running"):
        s.start(INTERVIEWS, Limits(), T0)
    with pytest.raises(ValueError, match="unknown system"):
        s.start("grants", Limits(), T0)
    assert s.accepting(INTERVIEWS) and s.active(INTERVIEWS) and not s.active(PROJECTS)


def test_a_system_can_start_again_after_it_ended():
    s = Sessions()
    s.start(PROJECTS, Limits(), T0)
    s.end(PROJECTS, STOP_NOW, T0 + MIN)
    again = s.start(PROJECTS, Limits(count=2), T0 + 2 * MIN)
    assert s.current(PROJECTS) is again and again.state == RUNNING and again.count == 0


def test_add_count_winds_down_at_the_cap():
    s = Sessions()
    s.start(INTERVIEWS, Limits(count=2), T0)
    s.add_count(INTERVIEWS, T0)
    assert s.accepting(INTERVIEWS)
    s.add_count(INTERVIEWS, T0 + MIN)
    session = s.current(INTERVIEWS)
    assert session.state == WINDING_DOWN and session.reason == LIMIT_COUNT
    assert session.wound_down_at == T0 + MIN and not s.accepting(INTERVIEWS)


def test_a_zero_count_never_winds_down():
    s = Sessions()
    s.start(INTERVIEWS, Limits(), T0)
    s.add_count(INTERVIEWS, T0, n=500)
    s.observe(INTERVIEWS, open_work=0, now=T0 + 1000 * MIN)
    assert s.accepting(INTERVIEWS) and s.current(INTERVIEWS).count == 500


def test_runtime_winds_down():
    s = Sessions()
    s.start(INTERVIEWS, Limits(runtime_minutes=30), T0)
    s.observe(INTERVIEWS, open_work=2, now=T0 + 29 * MIN)
    assert s.accepting(INTERVIEWS)
    s.observe(INTERVIEWS, open_work=2, now=T0 + 30 * MIN)
    assert s.winding_down(INTERVIEWS) and s.current(INTERVIEWS).reason == LIMIT_TIME


def test_three_failures_wind_projects_down_and_interviews_ignore_failures():
    s = Sessions()
    s.start(PROJECTS, Limits(), T0)
    s.start(INTERVIEWS, Limits(), T0)
    s.observe(PROJECTS, open_work=1, now=T0, failures_in_a_row=2)
    assert s.accepting(PROJECTS)
    s.observe(PROJECTS, open_work=1, now=T0, failures_in_a_row=3)
    assert s.winding_down(PROJECTS) and s.current(PROJECTS).reason == FAILURE_STOP
    s.observe(INTERVIEWS, open_work=1, now=T0, failures_in_a_row=9)
    assert s.accepting(INTERVIEWS)


def test_the_count_limit_is_checked_before_the_runtime_and_the_failures():
    s = Sessions()
    s.start(PROJECTS, Limits(count=2, runtime_minutes=1), T0)
    s.observe(PROJECTS, open_work=1, now=T0 + 5 * MIN, count=2, failures_in_a_row=3)
    assert s.current(PROJECTS).reason == LIMIT_COUNT


def test_interviews_end_after_three_quiet_observations_and_a_busy_one_resets():
    s = Sessions()
    s.start(INTERVIEWS, Limits(), T0)
    assert s.wind_down(INTERVIEWS, WIND_DOWN, T0)
    s.observe(INTERVIEWS, open_work=0, now=T0)
    s.observe(INTERVIEWS, open_work=0, now=T0)
    s.observe(INTERVIEWS, open_work=1, now=T0)
    s.observe(INTERVIEWS, open_work=0, now=T0)
    s.observe(INTERVIEWS, open_work=0, now=T0)
    assert s.winding_down(INTERVIEWS)
    s.observe(INTERVIEWS, open_work=0, now=T0 + MIN)
    session = s.current(INTERVIEWS)
    assert session.state == ENDED and session.reason == WIND_DOWN
    assert session.ended_at == T0 + MIN and not s.any_active()


def test_projects_end_on_the_first_quiet_observation():
    s = Sessions()
    s.start(PROJECTS, Limits(count=1), T0)
    s.observe(PROJECTS, open_work=0, now=T0 + MIN, count=1)
    session = s.current(PROJECTS)
    assert session.state == ENDED and session.reason == LIMIT_COUNT


def test_hand_wind_down_and_stop_now():
    s = Sessions()
    assert not s.wind_down(PROJECTS, WIND_DOWN, T0), "nothing to wind down"
    assert not s.end(PROJECTS, STOP_NOW, T0), "nothing to stop"
    s.start(PROJECTS, Limits(), T0)
    assert s.wind_down(PROJECTS, WIND_DOWN, T0)
    assert not s.wind_down(PROJECTS, WIND_DOWN, T0), "already winding down"
    assert s.end(PROJECTS, STOP_NOW, T0 + MIN)
    assert s.current(PROJECTS).reason == STOP_NOW and not s.end(PROJECTS, STOP_NOW, T0)


def test_stop_everything_ends_every_active_session():
    s = Sessions()
    s.start(INTERVIEWS, Limits(), T0)
    s.start(PROJECTS, Limits(), T0)
    s.wind_down(PROJECTS, WIND_DOWN, T0)
    s.stop_everything(T0 + MIN)
    assert [(r["state"], r["reason"]) for r in s.records()] == [
        (ENDED, STOP_EVERYTHING), (ENDED, STOP_EVERYTHING),
    ]


def test_run_end_reason_follows_the_last_session_and_a_carried_hold():
    s = Sessions()
    s.start(INTERVIEWS, Limits(), T0, initial=True)
    s.start(PROJECTS, Limits(count=2), T0, initial=True)
    s.end(INTERVIEWS, STOP_NOW, T0 + MIN)
    s.observe(PROJECTS, open_work=0, now=T0 + 2 * MIN, count=2)
    assert not s.any_active() and s.run_end_reason() == "target_drained"

    timed = Sessions()
    timed.start(INTERVIEWS, Limits(runtime_minutes=1), T0)
    timed.observe(INTERVIEWS, open_work=0, now=T0 + 2 * MIN)
    for _ in range(2):
        timed.observe(INTERVIEWS, open_work=0, now=T0 + 3 * MIN)
    assert timed.run_end_reason() == "time_limit"

    failing = Sessions()
    failing.start(PROJECTS, Limits(), T0)
    failing.observe(PROJECTS, open_work=0, now=T0, failures_in_a_row=3)
    assert failing.run_end_reason() == "failure_stop"

    by_hand = Sessions()
    by_hand.start(PROJECTS, Limits(count=5), T0)
    by_hand.end(PROJECTS, STOP_NOW, T0)
    assert by_hand.run_end_reason() == "operator"
    assert Sessions().run_end_reason() == "operator"
    assert Sessions(hold_carried=True).run_end_reason() == "held_over"


def test_events_pop_once_and_carry_initial_and_cleared_hold():
    s = Sessions(hold_carried=True)
    s.start(PROJECTS, Limits(), T0, initial=True)
    assert s.hold_carried, "a projects start leaves the interview hold carried"
    s.start(INTERVIEWS, Limits(count=1), T0 + MIN)
    s.add_count(INTERVIEWS, T0 + MIN)
    s.end(INTERVIEWS, STOP_NOW, T0 + 2 * MIN)
    events = s.pop_events()
    assert [(e.system, e.kind, e.reason, e.initial, e.cleared_hold) for e in events] == [
        (PROJECTS, STARTED, None, True, False),
        (INTERVIEWS, STARTED, None, False, True),
        (INTERVIEWS, WINDING_DOWN, LIMIT_COUNT, False, False),
        (INTERVIEWS, ENDED, STOP_NOW, False, False),
    ]
    assert s.pop_events() == [] and not s.hold_carried


def test_records_append_to_prior_and_normalise_unfinished_ones():
    prior = [
        {"system": INTERVIEWS, "state": ENDED, "reason": LIMIT_TIME},
        {"system": PROJECTS, "state": RUNNING, "reason": None},
        "not a record",
    ]
    s = Sessions(prior_records=prior)
    s.start(PROJECTS, Limits(count=3, runtime_minutes=60), T0, initial=True)
    records = s.records()
    assert [(r["system"], r["state"], r["reason"]) for r in records] == [
        (INTERVIEWS, ENDED, LIMIT_TIME),
        (PROJECTS, ENDED, STOP_EVERYTHING),
        (PROJECTS, RUNNING, None),
    ]
    assert records[2]["started_at"] == T0.isoformat()
    assert records[2]["limits"] == {"count": 3, "runtime_minutes": 60}
    assert prior[1]["state"] == RUNNING, "the caller's record is not mutated"
    assert set(s.detail()) == {PROJECTS} and s.detail()[PROJECTS]["initial"] is True


def test_dirty_follows_changes_and_mark_clean():
    s = Sessions()
    assert not s.dirty
    s.start(PROJECTS, Limits(), T0)
    assert s.dirty
    s.mark_clean()
    s.observe(PROJECTS, open_work=0, now=T0)
    assert not s.dirty, "an observation that changes nothing"
    s.observe(PROJECTS, open_work=2, now=T0, waiting_on=["gray", "hu", "gray"])
    assert s.dirty and s.current(PROJECTS).waiting_on == ("gray", "hu")


def test_legacy_is_interviews_only_and_initial():
    s = Sessions.legacy(count=4, runtime_minutes=60, now=T0)
    session = s.current(INTERVIEWS)
    assert session.limits == Limits(count=4, runtime_minutes=60) and session.initial
    assert not s.active(PROJECTS)


@pytest.mark.parametrize("data,expected", [
    ({"count": 3, "runtime_minutes": 90}, Limits(3, 90)),
    ({"count": -1, "runtime_minutes": True}, Limits()),
    ({"count": "5", "runtime_minutes": 2.5}, Limits()),
    ({}, Limits()),
    (None, Limits()),
    ([1, 2], Limits()),
])
def test_limits_from_mapping_rejects_garbage(data, expected):
    assert Limits.from_mapping(data) == expected


def test_start_payload_new_and_legacy_shapes():
    assert systems_from_start_payload({"fresh": True, "systems": {
        "interviews": {"count": 20}, "projects": {"count": 10, "runtime_minutes": 0},
        "grants": {"count": 1},
    }}) == {INTERVIEWS: Limits(count=20), PROJECTS: Limits(count=10)}
    assert systems_from_start_payload({"fresh": True, "max_runtime": 30, "max_proposals": 5}) == {
        INTERVIEWS: Limits(count=5, runtime_minutes=30),
    }
    assert systems_from_start_payload({"fresh": False}) == {INTERVIEWS: Limits()}
    assert systems_from_start_payload({"systems": ["interviews"]}) == {}
    assert systems_from_start_payload({"systems": {}}) == {}
    assert systems_from_start_payload(None) == {}


def test_describe_records_rows():
    s = Sessions()
    s.start(INTERVIEWS, Limits(count=1, runtime_minutes=240), T0)
    s.start(PROJECTS, Limits(count=10), T0)
    s.add_count(INTERVIEWS, T0 + MIN)
    s.end(PROJECTS, STOP_NOW, T0 + 2 * MIN)
    rows = describe_records([*s.records(), {"system": PROJECTS, "limits": None}, 7])
    assert rows == [
        {"system": "Interviews", "started_at": T0, "ended_at": None,
         "limits": "1 proposal, 240 minutes", "outcome": "winding down"},
        {"system": "Projects", "started_at": T0, "ended_at": T0 + 2 * MIN,
         "limits": "10 finished projects", "outcome": "stopped by hand"},
        {"system": "Projects", "started_at": None, "ended_at": None,
         "limits": "no limit", "outcome": "running"},
    ]


def test_describe_records_of_a_stopped_run_ends_what_its_process_never_ended():
    """A record still running in a run that no longer runs reads as the run stopped, as a
    resume records it; the same record of a running run stays running."""
    s = Sessions()
    s.start(PROJECTS, Limits(count=2), T0)
    (record,) = s.records()
    (stopped,) = describe_records([record], run_running=False)
    assert stopped["outcome"] == REASON_LABELS[STOP_EVERYTHING]
    assert stopped["ended_at"] is None
    (live,) = describe_records([record])
    assert live["outcome"] == "running"
