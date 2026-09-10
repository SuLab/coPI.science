"""Read-side aggregates for the control panel (src/services/simulation_stats.py).

Every test hand-computes its expected numbers rather than asserting "some
value came back" — the whole point of this module is arithmetic a human can
check. Three cross-cutting requirements are folded into the relevant tests
rather than each getting a dedicated pass: unpriced-model surfacing, the
pre-0036 floor flag (NULL cache columns), and run-scoping (a second run's
rows never leaking into the first's numbers).
"""

import statistics
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.agent.roles import prompt_set_stamp
from src.models import (
    AssessmentDrop,
    OpportunityAssessment,
    SpecialistConsult,
    ThreadDecision,
)
from src.services import simulation_stats as stats
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.build_info import get_build_info
from tests import factories

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# run_overview
# ---------------------------------------------------------------------------


async def test_run_overview_reads_run_fields_and_process_stamps(db_session):
    started = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    ended = datetime(2026, 1, 1, 0, 2, 0, tzinfo=UTC)  # 120s later
    announcement = {"at": "2026-01-01T00:00:00+00:00", "text": "hi", "posted": {"c": "1.1"}, "failed": []}
    run = await factories.make_simulation_run(
        db_session,
        status="completed",
        started_at=started,
        ended_at=ended,
        total_messages=7,
        total_api_calls=13,
        config={"max_runtime": 60, "run_start_announcement": announcement},
    )

    overview = await stats.run_overview(db_session, run.id)

    assert overview.run_id == run.id
    assert overview.status == "completed"
    assert overview.total_messages == 7
    assert overview.total_api_calls == 13
    assert overview.elapsed_seconds == 120.0
    assert overview.planned_seconds == 3600
    assert overview.run_start_announcement == announcement

    assert overview.rubric_version == RUBRIC_VERSION
    assert overview.rubric_content_hash == RUBRIC_CONTENT_HASH
    assert overview.hub_prompt_stamp == prompt_set_stamp("scout_hub")
    assert overview.pi_prompt_stamp == prompt_set_stamp("pi_lab")
    assert overview.build_info == get_build_info()


async def test_run_overview_planned_seconds_none_when_indefinite_or_absent(db_session):
    run_indefinite = await factories.make_simulation_run(db_session, config={"max_runtime": 0})
    run_absent = await factories.make_simulation_run(db_session, config={})

    assert (await stats.run_overview(db_session, run_indefinite.id)).planned_seconds is None
    assert (await stats.run_overview(db_session, run_absent.id)).planned_seconds is None
    assert (await stats.run_overview(db_session, run_absent.id)).run_start_announcement is None


async def test_run_overview_raises_for_a_run_that_does_not_exist(db_session):
    with pytest.raises(ValueError):
        await stats.run_overview(db_session, uuid.uuid4())


# ---------------------------------------------------------------------------
# cost_summary
# ---------------------------------------------------------------------------


async def test_cost_summary_hand_computed_total_and_floor_flag(db_session):
    run = await factories.make_simulation_run(db_session)
    # Task 8's seeded case: 5.00 + 2.50 + 0.25 + 1.25 = 9.00
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=100_000,
        cache_read_input_tokens=500_000, cache_creation_input_tokens=200_000,
    )
    # A pre-0036 row: NULL cache columns -> floor flag, and coalesced to 0 for cost.
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-sonnet-5",
        input_tokens=100, output_tokens=50,
        cache_read_input_tokens=None, cache_creation_input_tokens=None,
    )
    await db_session.commit()

    summary = await stats.cost_summary(db_session, run.id)

    assert summary.is_floor is True
    assert summary.unpriced_models == []
    by_model = {m.model: m for m in summary.by_model}
    assert by_model["claude-opus-5"].cost == Decimal("9.00")
    assert by_model["claude-sonnet-5"].cost == Decimal("0.0007")
    assert by_model["claude-sonnet-5"].cache_read_tokens == 0  # NULL coalesced to 0
    assert summary.total == Decimal("9.00") + Decimal("0.0007")
    assert summary.total_input_tokens == 1_000_100
    assert summary.total_output_tokens == 100_050
    assert summary.total_cache_read_tokens == 500_000
    assert summary.total_cache_creation_tokens == 200_000


async def test_cost_summary_surfaces_unpriced_model_without_polluting_total(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-opus-5",
        input_tokens=1000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # cost = 1000 * 5 / 1e6 = 0.005
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-unknown-99",
        input_tokens=100, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    await db_session.commit()

    summary = await stats.cost_summary(db_session, run.id)

    assert summary.unpriced_models == ["claude-unknown-99"]
    assert summary.total == Decimal("0.005")
    by_model = {m.model: m for m in summary.by_model}
    assert by_model["claude-unknown-99"].cost is None
    assert summary.is_floor is False


async def test_cost_summary_by_agent_and_by_phase(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="agentA", phase="decide", model="claude-sonnet-5",
        input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # (1000*2 + 100*10)/1e6 = 0.003
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="agentB", phase="respond", model="claude-sonnet-5",
        input_tokens=2000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # (2000*2 + 200*10)/1e6 = 0.006
    await db_session.commit()

    summary = await stats.cost_summary(db_session, run.id)

    by_agent = {a.agent_id: a for a in summary.by_agent}
    assert by_agent["agentA"].cost == Decimal("0.003")
    assert by_agent["agentA"].call_count == 1
    assert by_agent["agentB"].cost == Decimal("0.006")

    by_phase = {p.phase: p for p in summary.by_phase}
    assert by_phase["decide"].cost == Decimal("0.003")
    assert by_phase["respond"].cost == Decimal("0.006")


async def test_cost_summary_empty_run_is_zeroed(db_session):
    run = await factories.make_simulation_run(db_session)
    summary = await stats.cost_summary(db_session, run.id)
    assert summary.by_model == []
    assert summary.total == Decimal(0)
    assert summary.unpriced_models == []
    assert summary.is_floor is False
    assert summary.by_agent == []
    assert summary.by_phase == []
    assert summary.total_input_tokens == 0


async def test_cost_summary_is_run_scoped(db_session):
    run1 = await factories.make_simulation_run(db_session)
    run2 = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run1, model="claude-opus-5",
        input_tokens=100, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # 0.0005
    await factories.make_llm_call_log(
        db_session, run=run2, model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # 5.00 — would dominate run1's total if scoping leaked
    await db_session.commit()

    summary1 = await stats.cost_summary(db_session, run1.id)
    assert summary1.total == Decimal("0.0005")


# ---------------------------------------------------------------------------
# hourly_activity
# ---------------------------------------------------------------------------


async def test_hourly_activity_buckets_by_hour(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-sonnet-5",
        input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 10, 30, 0, tzinfo=UTC),
    )  # cost 0.003
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-sonnet-5",
        input_tokens=2000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 12, 15, 0, tzinfo=UTC),
    )  # cost 0.006
    await db_session.commit()

    buckets = await stats.hourly_activity(db_session, run.id)

    assert len(buckets) == 2
    assert buckets[0].hour < buckets[1].hour
    assert buckets[0].calls == 1
    assert buckets[0].cost == Decimal("0.003")
    assert buckets[0].input_tokens == 1000
    assert buckets[1].cost == Decimal("0.006")


async def test_hourly_activity_empty_run(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await stats.hourly_activity(db_session, run.id) == []


# ---------------------------------------------------------------------------
# funnel
# ---------------------------------------------------------------------------


async def test_funnel_hand_computed(db_session):
    run = await factories.make_simulation_run(db_session)

    # T1: terminal (closed), not yet announced -> headline owed. Verified panel.
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id="T1", recommendation="advance", summary_posted_at=None,
        panel_owed=True, panel_incomplete=False, missing_domains=None,
    ))
    # T2: terminal (closed), announced. Panel exempted (not_owed).
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id="T2", recommendation="pass",
        summary_posted_at=datetime(2026, 1, 1, tzinfo=UTC),
        panel_owed=False, panel_incomplete=False, missing_domains=None,
    ))
    # T3: still open (no ThreadDecision) -> provisional, not "owed" a headline. Unrecorded panel.
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id="T3", recommendation="conditional", summary_posted_at=None,
    ))
    # No thread_id at all (pre-0036 style) -> provisional, excluded from interviews_opened. Gapped panel.
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id=None, recommendation="advance", summary_posted_at=None,
        panel_incomplete=True, missing_domains=["chemistry"],
    ))
    db_session.add_all([
        ThreadDecision(simulation_run_id=run.id, thread_id="T1", channel="c",
                       agent_a="blackbird", agent_b="labbot", outcome="no_proposal"),
        ThreadDecision(simulation_run_id=run.id, thread_id="T2", channel="c",
                       agent_a="blackbird", agent_b="labbot", outcome="no_proposal"),
        # T4 closed with no verdict at all (e.g. a lab's own ⏸️ before the hub's verdict).
        ThreadDecision(simulation_run_id=run.id, thread_id="T4", channel="c",
                       agent_a="blackbird", agent_b="labbot", outcome="no_proposal"),
    ])
    db_session.add_all([
        AssessmentDrop(simulation_run_id=run.id, agent_id="blackbird", reason="missing_sidecar"),
        AssessmentDrop(simulation_run_id=run.id, agent_id="blackbird", reason="missing_sidecar"),
        AssessmentDrop(simulation_run_id=run.id, agent_id="blackbird", reason="empty_reply"),
    ])
    await db_session.commit()

    result = await stats.funnel(db_session, run.id)

    assert result.verdicts_stored == 4
    assert result.terminal == 2       # T1, T2
    assert result.provisional == 2    # T3, and the thread_id-less row
    assert result.announced == 1      # T2
    assert result.headlines_owed == 1  # T1: terminal, not announced
    assert result.interviews_opened == 4  # T1, T2, T3, T4
    assert result.drops_by_reason == {"missing_sidecar": 2, "empty_reply": 1}
    assert result.unvetted_panel_count == 2  # T3 (unrecorded), thread_id-less row (gap)

    # Run-scoping: a second run's rows must not move any of the above.
    run2 = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run2.id, agent_id="blackbird", channel_name="general",
        thread_id="OTHER", recommendation="advance", summary_posted_at=None,
    ))
    db_session.add(AssessmentDrop(simulation_run_id=run2.id, agent_id="blackbird", reason="empty_reply"))
    await db_session.commit()

    result_again = await stats.funnel(db_session, run.id)
    assert result_again == result

    result2 = await stats.funnel(db_session, run2.id)
    assert result2.verdicts_stored == 1
    assert result2.drops_by_reason == {"empty_reply": 1}


async def test_funnel_empty_run(db_session):
    run = await factories.make_simulation_run(db_session)
    result = await stats.funnel(db_session, run.id)
    assert result.verdicts_stored == 0
    assert result.terminal == 0
    assert result.provisional == 0
    assert result.announced == 0
    assert result.headlines_owed == 0
    assert result.interviews_opened == 0
    assert result.drops_by_reason == {}
    assert result.unvetted_panel_count == 0


# ---------------------------------------------------------------------------
# specialist_mix / consult_fanout
# ---------------------------------------------------------------------------


def _consult(run_id, *, domain, signal, thread_id=None):
    return SpecialistConsult(
        simulation_run_id=run_id, agent_id="blackbird", domain=domain,
        question="Q?", verdict_signal=signal, confidence="moderate",
        raw_opinion="opinion", thread_id=thread_id,
    )


async def test_specialist_mix_folds_historical_labels_and_counts_by_domain(db_session):
    run = await factories.make_simulation_run(db_session)
    db_session.add_all([
        _consult(run.id, domain="chemistry", signal="blocking", thread_id="IT1"),
        _consult(run.id, domain="chemistry", signal="gap", thread_id="IT1"),
        _consult(run.id, domain="chemistry", signal="gap", thread_id="IT2"),
        _consult(run.id, domain="chemistry", signal="adequate", thread_id="IT2"),
        _consult(run.id, domain="chemistry", signal="caution", thread_id="IT2"),  # historical
        _consult(run.id, domain="legal", signal="clear", thread_id="IT3"),  # historical
        _consult(run.id, domain="legal", signal="adequate", thread_id=None),
    ])
    await db_session.commit()

    mix = {m.domain: m for m in await stats.specialist_mix(db_session, run.id)}

    assert mix["chemistry"].blocking == 1
    assert mix["chemistry"].gap == 2
    assert mix["chemistry"].adequate == 1
    assert mix["chemistry"].historical == 1
    assert mix["chemistry"].total == 5
    assert mix["legal"].adequate == 1
    assert mix["legal"].historical == 1
    assert mix["legal"].total == 2

    fanout = await stats.consult_fanout(db_session, run.id)
    counts = {b.consult_count: b.interview_count for b in fanout}
    assert counts == {2: 1, 3: 1, 1: 1}  # IT1=2, IT2=3, IT3=1 (thread_id=None excluded)

    # Run-scoping.
    run2 = await factories.make_simulation_run(db_session)
    db_session.add(_consult(run2.id, domain="chemistry", signal="blocking", thread_id="OTHER"))
    await db_session.commit()
    mix_again = {m.domain: m for m in await stats.specialist_mix(db_session, run.id)}
    assert mix_again["chemistry"].total == 5


async def test_specialist_mix_and_fanout_empty_run(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await stats.specialist_mix(db_session, run.id) == []
    assert await stats.consult_fanout(db_session, run.id) == []


# ---------------------------------------------------------------------------
# per_agent
# ---------------------------------------------------------------------------


async def test_per_agent_joins_registry_and_aggregates(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="labbot", role="pi_lab", status="active")
    await factories.make_agent(
        db_session, agent_id="mutedbot", role="pi_lab", status="suspended",
        muted_at=datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC),
    )

    await factories.make_llm_call_log(
        db_session, run=run, agent_id="labbot", model="claude-sonnet-5",
        input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
    )  # cost 0.003
    await factories.make_agent_message(
        db_session, run=run, agent_id="labbot",
        created_at=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
    )
    await factories.make_agent_message(
        db_session, run=run, agent_id="labbot",
        created_at=datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC),
    )

    await factories.make_agent_message(
        db_session, run=run, agent_id="mutedbot",
        created_at=datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC),
    )

    # An agent with no `agents` row at all.
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="ghostbot", model="claude-opus-5",
        input_tokens=10, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 10, 30, 0, tzinfo=UTC),
    )  # cost 0.0003

    await db_session.commit()

    rows = {r.agent_id: r for r in await stats.per_agent(db_session, run.id)}
    assert set(rows) == {"labbot", "mutedbot", "ghostbot"}

    lab = rows["labbot"]
    assert lab.role == "pi_lab"
    assert lab.registry_status == "active"
    assert lab.muted is False
    assert lab.message_count == 2
    assert lab.call_count == 1
    assert lab.cost == Decimal("0.003")
    assert lab.last_activity == datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC)

    muted = rows["mutedbot"]
    assert muted.muted is True
    assert muted.registry_status == "suspended"
    assert muted.message_count == 1
    assert muted.call_count == 0
    assert muted.cost == Decimal(0)

    ghost = rows["ghostbot"]
    assert ghost.role is None
    assert ghost.registry_status is None
    assert ghost.muted is False
    assert ghost.call_count == 1
    assert ghost.cost == Decimal("0.0003")


async def test_per_agent_empty_run(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await stats.per_agent(db_session, run.id) == []


# ---------------------------------------------------------------------------
# stop_reason_taxonomy / latency_percentiles
# ---------------------------------------------------------------------------


async def test_stop_reason_taxonomy_buckets(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, phase="thread_reply",
        call_stats=[
            {"seq": 1, "kind": "round", "stop_reason": "end_turn", "latency_ms": 100.0},
            {"seq": 2, "kind": "final", "stop_reason": "tool_use", "latency_ms": 200.0},
        ],
    )
    await factories.make_llm_call_log(
        db_session, run=run, phase="thread_reply",
        call_stats=[{"seq": 1, "kind": "final", "stop_reason": "max_tokens", "latency_ms": 300.0}],
    )
    await factories.make_llm_call_log(
        db_session, run=run, phase="consult_chemistry",
        call_stats=[{"seq": 1, "kind": "final", "stop_reason": "refusal", "latency_ms": 50.0}],
    )
    await factories.make_llm_call_log(
        db_session, run=run, phase="decide",
        call_stats=[{"seq": 1, "kind": "final", "stop_reason": "weird_custom", "latency_ms": 10.0}],
    )
    # A pre-0032 row with no call_stats at all: must not raise, must not count.
    await factories.make_llm_call_log(db_session, run=run, phase="memory", call_stats=None)
    await db_session.commit()

    taxonomy = await stats.stop_reason_taxonomy(db_session, run.id)
    assert taxonomy == {"normal": 2, "truncated": 1, "refused": 1, "weird_custom": 1}


async def test_latency_percentiles_hand_computed_per_phase_and_overall(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, phase="thread_reply",
        call_stats=[
            {"stop_reason": "end_turn", "latency_ms": 100.0},
            {"stop_reason": "tool_use", "latency_ms": 200.0},
            {"stop_reason": "end_turn", "latency_ms": 300.0},
        ],
    )
    await factories.make_llm_call_log(
        db_session, run=run, phase="consult_chemistry",
        call_stats=[{"stop_reason": "refusal", "latency_ms": 50.0}],
    )
    await db_session.commit()

    pcts = await stats.latency_percentiles(db_session, run.id)

    thread_reply_values = [100.0, 200.0, 300.0]
    cuts = statistics.quantiles(thread_reply_values, n=100)
    assert pcts["thread_reply"].n == 3
    assert pcts["thread_reply"].p50 == cuts[49]
    assert pcts["thread_reply"].p95 == cuts[94]
    assert pcts["thread_reply"].p99 == cuts[98]

    # n=1 for this phase: guarded, no raise, all-None percentile set.
    assert pcts["consult_chemistry"].n == 1
    assert pcts["consult_chemistry"].p50 is None
    assert pcts["consult_chemistry"].p95 is None
    assert pcts["consult_chemistry"].p99 is None

    overall_values = [100.0, 200.0, 300.0, 50.0]
    overall_cuts = statistics.quantiles(overall_values, n=100)
    assert pcts["overall"].n == 4
    assert pcts["overall"].p50 == overall_cuts[49]


async def test_latency_percentiles_below_two_samples_never_raises(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, phase="decide",
        call_stats=[{"stop_reason": "end_turn", "latency_ms": 42.0}],
    )
    await db_session.commit()

    pcts = await stats.latency_percentiles(db_session, run.id)  # must not raise StatisticsError
    assert pcts["overall"] == stats.LatencyPcts(n=1, p50=None, p95=None, p99=None)


async def test_stop_reason_and_latency_empty_run(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await stats.stop_reason_taxonomy(db_session, run.id) == {}
    pcts = await stats.latency_percentiles(db_session, run.id)
    assert pcts == {"overall": stats.LatencyPcts(n=0, p50=None, p95=None, p99=None)}


# ---------------------------------------------------------------------------
# interview_timeline
# ---------------------------------------------------------------------------


async def test_interview_timeline_hand_computed(db_session):
    run = await factories.make_simulation_run(db_session)
    a1 = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id="T1", subject_agent_id="labbot", recommendation="advance",
        summary_posted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    a2 = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id="T2", subject_agent_id="labbot2", recommendation=None,
        summary_posted_at=None,
    )
    # No thread_id at all -> excluded from the timeline entirely.
    a3 = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id=None, recommendation="pass",
    )
    db_session.add_all([a1, a2, a3])
    await db_session.flush()

    await factories.make_agent_message(
        db_session, run=run, thread_ts="T1", posted_at=100.0,
    )
    await factories.make_agent_message(
        db_session, run=run, thread_ts="T1", posted_at=300.0,
    )
    await db_session.commit()

    spans = {s.thread_id: s for s in await stats.interview_timeline(db_session, run.id)}
    assert set(spans) == {"T1", "T2"}

    t1 = spans["T1"]
    assert t1.assessment_id == a1.id
    assert t1.subject_agent_id == "labbot"
    assert t1.first_message_at == 100.0
    assert t1.last_message_at == 300.0
    assert t1.outcome == "advance"
    assert t1.announced is True

    t2 = spans["T2"]
    assert t2.first_message_at is None
    assert t2.last_message_at is None
    assert t2.outcome == "in flight"  # recommendation is None
    assert t2.announced is False


async def test_interview_timeline_empty_run(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await stats.interview_timeline(db_session, run.id) == []


# ---------------------------------------------------------------------------
# hub_lab_burn
# ---------------------------------------------------------------------------


async def test_hub_lab_burn_hand_computed(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-sonnet-5",
        input_tokens=1000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="labbot", model="claude-sonnet-5",
        input_tokens=500, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 10, 15, 0, tzinfo=UTC),
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="labbot", model="claude-sonnet-5",
        input_tokens=300, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC),
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-sonnet-5",
        input_tokens=700, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    await db_session.commit()

    points = await stats.hub_lab_burn(db_session, run.id, "blackbird")
    assert len(points) == 3

    p10 = points[0]
    assert p10.hub_tokens == 1000
    assert p10.lab_tokens == 500
    assert p10.ratio == 2.0

    p11 = points[1]
    assert p11.hub_tokens == 0
    assert p11.lab_tokens == 300
    assert p11.ratio == 0.0

    p12 = points[2]
    assert p12.hub_tokens == 700
    assert p12.lab_tokens == 0
    assert p12.ratio is None


async def test_hub_lab_burn_empty_run(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await stats.hub_lab_burn(db_session, run.id, "blackbird") == []


# ---------------------------------------------------------------------------
# cost_per_interview
# ---------------------------------------------------------------------------


async def test_cost_per_interview_hand_computed_with_unattributed_bucket(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, thread_ts="T1", model="claude-sonnet-5",
        input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # 0.003
    await factories.make_llm_call_log(
        db_session, run=run, thread_ts="T1", model="claude-opus-5",
        input_tokens=10, output_tokens=10, cache_read_input_tokens=None, cache_creation_input_tokens=None,
    )  # 0.0003, NULL cache -> floor for T1
    await factories.make_llm_call_log(
        db_session, run=run, thread_ts="T2", model="claude-unknown-1",
        input_tokens=100, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # unpriced -> 0
    await factories.make_llm_call_log(
        db_session, run=run, thread_ts=None, model="claude-sonnet-5",
        input_tokens=50, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # unattributed, 0.00015
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id="T1", recommendation="advance",
    ))
    await db_session.commit()

    results = {r.thread_ts: r for r in await stats.cost_per_interview(db_session, run.id)}

    assert set(results) == {"T1", "T2", None}
    t1 = results["T1"]
    assert t1.cost == Decimal("0.0033")
    assert t1.is_floor is True
    assert t1.outcome == "advance"

    t2 = results["T2"]
    assert t2.cost == Decimal(0)
    assert t2.is_floor is False
    assert t2.outcome is None

    unattributed = results[None]
    assert unattributed.cost == Decimal("0.00015")
    assert unattributed.is_floor is False
    assert unattributed.outcome is None


async def test_cost_per_interview_empty_run_still_has_unattributed_bucket(db_session):
    run = await factories.make_simulation_run(db_session)
    results = await stats.cost_per_interview(db_session, run.id)
    assert len(results) == 1
    assert results[0].thread_ts is None
    assert results[0].cost == Decimal(0)
    assert results[0].is_floor is False


async def test_cost_per_interview_is_run_scoped(db_session):
    run1 = await factories.make_simulation_run(db_session)
    run2 = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run1, thread_ts="T1", model="claude-sonnet-5",
        input_tokens=10, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    await factories.make_llm_call_log(
        db_session, run=run2, thread_ts="T1", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    await db_session.commit()

    results1 = {r.thread_ts: r for r in await stats.cost_per_interview(db_session, run1.id)}
    assert results1["T1"].cost == Decimal("0.00002")


# ---------------------------------------------------------------------------
# cost_by_stage / cost_by_specialist / cost_by_call_kind  (F1)
# ---------------------------------------------------------------------------


async def test_cost_by_stage_joins_the_role_and_buckets_a_null_phase(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="blackbird", role="scout_hub")
    await factories.make_agent(db_session, agent_id="labbot", role="pi_lab")
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0)
    # claude-opus-5: $5/MTok in, $25/MTok out.  claude-sonnet-5: $2 / $10.
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        thread_phase="decide", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, **common,
    )  # 1_000_000 * 5 / 1e6 = $5.00
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        thread_phase=None, model="claude-opus-5",
        input_tokens=200_000, output_tokens=0, **common,
    )  # 200_000 * 5 / 1e6 = $1.00
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="labbot", phase="thread_reply",
        thread_phase="explore", model="claude-sonnet-5",
        input_tokens=1_000_000, output_tokens=100_000, **common,
    )  # 1_000_000 * 2 / 1e6 + 100_000 * 10 / 1e6 = 2.00 + 1.00 = $3.00
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="ghost", phase="thread_reply",
        thread_phase="conclude", model="claude-sonnet-5",
        input_tokens=500_000, output_tokens=0, **common,
    )  # 500_000 * 2 / 1e6 = $1.00, and `ghost` has no AgentRegistry row.
    await db_session.commit()

    rows = await stats.cost_by_stage(db_session, run.id)
    by = {(r.role, r.thread_phase): r for r in rows}

    assert set(by) == {
        ("scout_hub", "decide"), ("scout_hub", "unclassified"),
        ("pi_lab", "explore"), ("unknown", "conclude"),
    }
    assert by[("scout_hub", "decide")].cost == Decimal("5.00")
    assert by[("scout_hub", "decide")].call_count == 1
    assert by[("scout_hub", "unclassified")].cost == Decimal("1.00")
    assert by[("scout_hub", "unclassified")].call_count == 1
    assert by[("pi_lab", "explore")].cost == Decimal("3.00")
    assert by[("unknown", "conclude")].cost == Decimal("1.00")


async def test_cost_by_stage_is_run_scoped_and_zeroes_an_unpriced_model(db_session):
    run1 = await factories.make_simulation_run(db_session)
    run2 = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="blackbird", role="scout_hub")
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0)
    await factories.make_llm_call_log(
        db_session, run=run1, agent_id="blackbird", thread_phase="decide",
        model="claude-made-up", input_tokens=1_000_000, output_tokens=1_000_000, **common,
    )
    await factories.make_llm_call_log(
        db_session, run=run2, agent_id="blackbird", thread_phase="decide",
        model="claude-opus-5", input_tokens=1_000_000, output_tokens=0, **common,
    )
    await db_session.commit()

    rows = await stats.cost_by_stage(db_session, run1.id)

    assert len(rows) == 1
    assert rows[0].role == "scout_hub"
    assert rows[0].thread_phase == "decide"
    assert rows[0].cost == Decimal(0)  # unpriced contributes nothing
    assert rows[0].call_count == 1


async def test_cost_by_specialist_splits_by_domain_and_labels_unmatched(db_session):
    run = await factories.make_simulation_run(db_session)
    db_session.add(_consult(run.id, domain="chemistry", signal="blocking", thread_id="T1"))
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="consult_chemistry",
        thread_ts="T1", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, **common,
    )  # $5.00, matched to the blocking consult above
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="consult_legal",
        thread_ts="T9", model="claude-sonnet-5",
        input_tokens=1_000_000, output_tokens=0, **common,
    )  # $2.00, no consult row names (T9, legal)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        thread_ts="T1", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, **common,
    )  # not a consult call at all — must not appear
    await db_session.commit()

    rows = await stats.cost_by_specialist(db_session, run.id)
    by = {(r.domain, r.verdict_signal): r for r in rows}

    assert set(by) == {("chemistry", "blocking"), ("legal", "unmatched")}
    assert by[("chemistry", "blocking")].cost == Decimal("5.00")
    assert by[("chemistry", "blocking")].call_count == 1
    assert by[("legal", "unmatched")].cost == Decimal("2.00")
    assert by[("legal", "unmatched")].call_count == 1


async def test_cost_by_call_kind_prices_each_call_stats_element(db_session):
    run = await factories.make_simulation_run(db_session)
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=200_000, **common,
        call_stats=[
            {"seq": 0, "kind": "round", "input_tokens": 1_000_000,
             "output_tokens": 0, "thinking_tokens": 0},
            {"seq": 1, "kind": "final", "input_tokens": 0,
             "output_tokens": 100_000, "thinking_tokens": 100_000},
        ],
    )
    # round: 1_000_000 * 5 / 1e6  = $5.00
    # final:   100_000 * 25 / 1e6 = $2.50 — `thinking_tokens` is a
    #   DECOMPOSITION of `output_tokens` (src/services/llm.py:112), so adding
    #   the two would bill those 100k thinking tokens twice.
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-sonnet-5",
        input_tokens=0, output_tokens=0, **common,
        call_stats=[
            {"seq": 0, "kind": "round", "input_tokens": 500_000,
             "output_tokens": 0, "thinking_tokens": 0},
            # sonnet-5 cache: read $0.20/MTok, 5m write $2.50/MTok.
            {"seq": 1, "kind": "retry", "input_tokens": 0, "output_tokens": 0,
             "thinking_tokens": 0, "cache_read_input_tokens": 1_000_000,
             "cache_creation_input_tokens": 1_000_000},
        ],
    )
    # round: 500_000 * 2 / 1e6 = $1.00
    # retry: 1_000_000 * 0.20 / 1e6 + 1_000_000 * 2.50 / 1e6 = 0.20 + 2.50 = $2.70
    await db_session.commit()

    rows = await stats.cost_by_call_kind(db_session, run.id)
    by = {r.kind: r for r in rows}

    assert set(by) == {"round", "final", "retry"}
    assert by["round"].cost == Decimal("6.00")   # 5.00 + 1.00
    assert by["round"].call_count == 2
    assert by["final"].cost == Decimal("2.50")
    assert by["final"].call_count == 1
    assert by["retry"].cost == Decimal("2.70")
    assert by["retry"].call_count == 1
    assert all(r.is_floor is False for r in rows)


async def test_cost_by_call_kind_is_a_floor_when_a_row_has_no_call_stats(db_session):
    run = await factories.make_simulation_run(db_session)
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, **common,
        call_stats=[
            {"seq": 0, "kind": "round", "input_tokens": 1_000_000,
             "output_tokens": 0, "thinking_tokens": 0},
        ],
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, **common, call_stats=None,
    )
    await db_session.commit()

    rows = await stats.cost_by_call_kind(db_session, run.id)

    assert len(rows) == 1
    assert rows[0].kind == "round"
    assert rows[0].cost == Decimal("5.00")  # the NULL-call_stats row is invisible here
    assert rows[0].is_floor is True


async def test_cost_by_call_kind_survives_the_legacy_jsonb_null_scalar(db_session):
    """`call_stats` has TWO encodings of "absent" and only one is SQL NULL.

    Migration 0031 documents the other: the JSONB scalar `null`, which
    `jsonb_array_elements` RAISES on rather than skipping — so this row would
    500 the whole Live tab if the query did not filter on `jsonb_typeof`. It
    is also a floor cause, exactly like a SQL NULL.
    """
    run = await factories.make_simulation_run(db_session)
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, **common,
        call_stats=[
            {"seq": 0, "kind": "round", "input_tokens": 1_000_000,
             "output_tokens": 0, "thinking_tokens": 0},
        ],
    )
    scalar_null_row = await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, **common, call_stats=None,
    )
    await db_session.execute(
        text("UPDATE llm_call_logs SET call_stats = 'null'::jsonb WHERE id = :id"),
        {"id": scalar_null_row.id},
    )
    await db_session.commit()

    rows = await stats.cost_by_call_kind(db_session, run.id)

    assert len(rows) == 1
    assert rows[0].kind == "round"
    assert rows[0].cost == Decimal("5.00")
    assert rows[0].is_floor is True


async def test_the_three_f1_aggregates_tolerate_an_empty_run(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await stats.cost_by_stage(db_session, run.id) == []
    assert await stats.cost_by_specialist(db_session, run.id) == []
    assert await stats.cost_by_call_kind(db_session, run.id) == []
