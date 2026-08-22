"""Unit tests for scripts/backfill_dropped_verdicts.py (Task 4, workstream F).

Pure-function tests only — no database. The script's four defects (F1.1-F1.4,
see .superpowers/sdd/2026-08-22-correctness-remediation/task-4-brief.md) are
all exercised through the module's testable helpers:

  * ``_build_assessment_row``       — F1.1 (contract key), F1.2 (panel claim),
                                       F1.3 (per-field guards)
  * ``_existing_assessment_for``    — F1.3 (interview-keyed idempotency)
  * ``_fallback_llm_log_query``     — F1.4 (narrow SELECT)
  * ``_recover_from_llm_logs``      — F1.4 (subject match)

None of these touch a session or an engine, so a plain in-memory
``AssessmentDrop``/``OpportunityAssessment`` (never added to any session) is
all any test needs.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect as sa_inspect

from scripts.backfill_dropped_verdicts import (
    _build_assessment_row,
    _existing_assessment_for,
    _fallback_llm_log_query,
    _recover_from_llm_logs,
)
from src.models import AssessmentDrop, OpportunityAssessment

RUN_ID = uuid.uuid4()
OTHER_RUN_ID = uuid.uuid4()
RUBRIC_VERSION = "2.0.0"
RUBRIC_HASH = "e3ef75f84c48"
T0 = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def _drop(**overrides) -> AssessmentDrop:
    defaults = dict(
        id=uuid.uuid4(),
        simulation_run_id=RUN_ID,
        agent_id="blackbird",
        subject_agent_id="markham",
        thread_id="T-markham-1",
        reason="premature_sidecar",
        detail=None,
        raw_verdict=None,
        created_at=T0,
    )
    defaults.update(overrides)
    return AssessmentDrop(**defaults)


def _verdict(**overrides) -> dict:
    defaults = dict(
        subject_agent_id="markham",
        channel_name="lab-markham",
        company_or_project="Acme Biotech",
        funnel_stage="incubation",
        recommendation="advance",
        confidence="medium",
        gating={},
        scores={},
        red_flags=[],
        suggested_derisking_milestones=[],
        rationale="Solid preliminary data.",
    )
    defaults.update(overrides)
    return defaults


def _existing_row(**overrides) -> OpportunityAssessment:
    defaults = dict(
        id=uuid.uuid4(),
        simulation_run_id=RUN_ID,
        agent_id="blackbird",
        subject_agent_id="markham",
        channel_name="lab-markham",
        thread_id=None,
    )
    defaults.update(overrides)
    return OpportunityAssessment(**defaults)


# ---------------------------------------------------------------------------
# F1.1 — read the sidecar contract key
# ---------------------------------------------------------------------------


def test_the_backfill_reads_the_contract_milestone_key():
    verdict = _verdict(
        suggested_derisking_milestones=["File composition-of-matter", "Run tox screen"],
    )
    # A real sidecar never carries the wrong key at all, but a stray
    # `derisking_milestones` key must not be mistaken for the real one either.
    verdict["derisking_milestones"] = None
    drop = _drop()

    row = _build_assessment_row(verdict, drop, RUBRIC_VERSION, RUBRIC_HASH)

    assert row.derisking_milestones == [
        "File composition-of-matter", "Run tox screen",
    ]


# ---------------------------------------------------------------------------
# F1.2 — stop claiming a verified panel
# ---------------------------------------------------------------------------


def test_a_backfilled_row_does_not_claim_a_verified_panel():
    drop = _drop(subject_agent_id="weeraratna", thread_id="T-weeraratna-1")
    verdict = _verdict(subject_agent_id="weeraratna")

    row = _build_assessment_row(verdict, drop, RUBRIC_VERSION, RUBRIC_HASH)

    # NULL means VERIFIED complete (a claim no backfilled row can support);
    # [] is the documented UNVERIFIED state.
    assert row.missing_domains == []
    # panel_owed must be *explicitly* None (a deliberate "we don't know"),
    # not merely left unset — `"panel_owed" in sa_inspect(row).dict` is only
    # true when the constructor actually assigned it, so this catches the
    # difference between "assigned None on purpose" and "never touched"
    # (which read back identically through plain attribute access).
    assert "panel_owed" in sa_inspect(row).dict
    assert row.panel_owed is None
    # False here must mean "we looked and found no gap" everywhere else on
    # this table; a backfilled row must not borrow that meaning for "nobody
    # looked".
    assert row.panel_incomplete is False


# ---------------------------------------------------------------------------
# F1.3 — reuse the engine's guards
# ---------------------------------------------------------------------------


def test_an_overlong_recommendation_is_clipped_not_fatal():
    drop = _drop()
    verdict = _verdict(recommendation="advance-with-conditions-" * 20)

    row = _build_assessment_row(verdict, drop, RUBRIC_VERSION, RUBRIC_HASH)

    assert row.recommendation is not None
    assert len(row.recommendation) <= 30
    # raw_verdict keeps the untruncated original regardless.
    assert row.raw_verdict["recommendation"] == verdict["recommendation"]


def test_gating_is_normalised_on_a_backfilled_row():
    drop = _drop()
    verdict = _verdict(
        gating={
            "ip_freedom_to_operate": "met",
            "regulatory_pathway": True,  # legacy boolean — must be dropped, not coerced
            "market_validated": "bogus-value",  # not one of the three states — dropped
        },
    )

    row = _build_assessment_row(verdict, drop, RUBRIC_VERSION, RUBRIC_HASH)

    assert row.gating == {"ip_freedom_to_operate": "met"}
    # raw_verdict keeps the original verbatim, boolean and all.
    assert row.raw_verdict["gating"]["regulatory_pathway"] is True


def test_a_non_string_rationale_does_not_lose_the_row():
    drop = _drop()
    structured_rationale = {"summary": "looks promising", "concerns": ["IP"]}
    verdict = _verdict(rationale=structured_rationale)

    row = _build_assessment_row(verdict, drop, RUBRIC_VERSION, RUBRIC_HASH)

    assert row.rationale is None
    # Nothing is lost: raw_verdict keeps the dict exactly as emitted.
    assert row.raw_verdict["rationale"] == structured_rationale


def test_a_second_interview_with_the_same_pi_is_not_skipped():
    # An earlier interview with this PI already has a (thread-keyed) row.
    first_interview_row = _existing_row(
        subject_agent_id="markham", thread_id="T-markham-1",
    )
    # A second, DIFFERENT interview with the same PI is what we're deciding
    # whether to skip.
    second_interview_drop = _drop(
        subject_agent_id="markham", thread_id="T-markham-2",
    )

    result = _existing_assessment_for(
        [first_interview_row], RUN_ID, second_interview_drop
    )

    assert result is None


def test_a_rerun_does_not_duplicate_the_legacy_null_thread_rows():
    """Not one of the brief's named tests, but the scenario it explicitly

    calls out: the two rows this script already wrote in production have
    thread_id=NULL (the pre-fix code never set it), so a purely
    thread-keyed idempotency check would fail to find them and duplicate
    them on a second run.
    """
    legacy_row = _existing_row(
        subject_agent_id="markham", thread_id=None,
    )
    rerun_drop = _drop(subject_agent_id="markham", thread_id="T-markham-1")

    result = _existing_assessment_for([legacy_row], RUN_ID, rerun_drop)

    assert result is legacy_row


def test_existing_assessment_lookup_is_scoped_to_the_run():
    other_run_row = _existing_row(simulation_run_id=OTHER_RUN_ID, thread_id=None)
    drop = _drop(subject_agent_id="markham", thread_id=None)

    result = _existing_assessment_for([other_run_row], RUN_ID, drop)

    assert result is None


# ---------------------------------------------------------------------------
# F1.4 — narrow the llm_call_logs fallback
# ---------------------------------------------------------------------------


def _sidecar_response(verdict: dict) -> str:
    import json
    return (
        "Some preamble text.\n\n<assessment_json>\n"
        + json.dumps(verdict)
        + "\n</assessment_json>"
    )


def test_the_fallback_never_recovers_another_pis_verdict():
    drop = _drop(subject_agent_id="markham", created_at=T0)
    # The only candidate row in range belongs to a DIFFERENT PI, timestamped
    # just before the drop — exactly the interleaving the brief describes.
    other_pi_verdict = _verdict(subject_agent_id="weeraratna")
    rows = [
        (uuid.uuid4(), T0 - timedelta(milliseconds=200), _sidecar_response(other_pi_verdict)),
    ]

    recovered, source = _recover_from_llm_logs(rows, drop)

    assert recovered is None
    assert source is None


def test_the_fallback_recovers_the_matching_subjects_verdict():
    drop = _drop(subject_agent_id="markham", created_at=T0)
    other_pi_verdict = _verdict(subject_agent_id="weeraratna")
    own_verdict = _verdict(subject_agent_id="markham", recommendation="route-to-incubation")
    rows = [
        # Most recent first, as the real query returns.
        (uuid.uuid4(), T0 - timedelta(milliseconds=50), _sidecar_response(other_pi_verdict)),
        (uuid.uuid4(), T0 - timedelta(milliseconds=300), _sidecar_response(own_verdict)),
    ]

    recovered, source = _recover_from_llm_logs(rows, drop)

    assert recovered is not None
    assert recovered["subject_agent_id"] == "markham"
    assert recovered["recommendation"] == "route-to-incubation"
    assert source is not None and "llm_call_logs" in source


def test_the_fallback_does_not_select_whole_rows():
    drop = _drop()

    stmt = _fallback_llm_log_query(RUN_ID, drop)

    selected_names = {col.name for col in stmt.selected_columns}
    assert selected_names == {"id", "created_at", "response_text"}
