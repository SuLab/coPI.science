"""Unit tests for scripts/backfill_dropped_verdicts.py (Task 4, workstream F).

Pure-function tests only — no database. The script's defects (F1.1-F1.4, plus
fix round 1's FIX 1-FIX 7; see
.superpowers/sdd/2026-08-22-correctness-remediation/task-4-brief.md and the
fix-round-1 coordinator messages) are all exercised through the module's
testable helpers:

  * ``_build_assessment_row``       — F1.1, F1.2, F1.3, FIX 3, FIX 4, FIX 5
  * ``_existing_assessment_for``    — F1.3, FIX 6
  * ``_fallback_llm_log_query``     — F1.4, FIX 2(a)
  * ``_recover_from_llm_logs``      — F1.4, FIX 1, FIX 2(b)/(c)
  * ``_subject_matches``            — FIX 1
  * ``_derive_rubric_stamp``        — FIX 5

None of these touch a session or an engine, so a plain in-memory
``AssessmentDrop``/``OpportunityAssessment`` (never added to any session) is
all any test needs.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect as sa_inspect

from scripts.backfill_dropped_verdicts import (
    _build_assessment_row,
    _derive_rubric_stamp,
    _existing_assessment_for,
    _fallback_llm_log_query,
    _recover_from_llm_logs,
    _subject_matches,
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


def _sidecar_response(verdict: dict) -> str:
    return (
        "Some preamble text.\n\n<assessment_json>\n"
        + json.dumps(verdict)
        + "\n</assessment_json>"
    )


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
    # FIX 4: exact bound, not `<= 30` — a wrong bound (e.g. _bounded_str(...,
    # 20)) would still satisfy `<= 30` and pass silently.
    assert len(row.recommendation) == 30
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


def test_company_or_project_guard_drops_a_non_string_value():
    """FIX 3: `_str_or_none` on `company_or_project` had no direct test —
    removing it left all 11 original tests green."""
    drop = _drop()
    structured = {"structured": True, "hq": "La Jolla"}
    verdict = _verdict(company_or_project=structured)

    row = _build_assessment_row(verdict, drop, RUBRIC_VERSION, RUBRIC_HASH)

    assert row.company_or_project is None
    assert row.raw_verdict["company_or_project"] == structured


@pytest.mark.parametrize(
    "field, max_len",
    [
        ("funnel_stage", 20),
        ("confidence", 20),
        ("channel_name", 100),
    ],
)
def test_bounded_varchar_columns_are_clipped_not_fatal(field, max_len):
    """FIX 3: `_bounded_str` on funnel_stage/confidence/channel_name had no
    direct test — removing any one of the three left all 11 original tests
    green."""
    drop = _drop()
    verdict = _verdict(**{field: "x" * (max_len + 50)})

    row = _build_assessment_row(verdict, drop, RUBRIC_VERSION, RUBRIC_HASH)

    value = getattr(row, field)
    assert value is not None
    assert len(value) == max_len


def test_the_rubric_stamp_is_bounded_not_fatal():
    """FIX 5: an operator-supplied --rubric-version/--rubric-hash is exactly
    the same StringDataRightTruncation risk as an over-long LLM field."""
    drop = _drop()
    verdict = _verdict()

    row = _build_assessment_row(verdict, drop, "v" * 50, "h" * 50)

    assert row.rubric_version is not None
    assert len(row.rubric_version) == 20
    assert row.rubric_content_hash is not None
    assert len(row.rubric_content_hash) == 20


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


def test_a_null_thread_drop_still_finds_an_existing_threaded_row():
    """FIX 6: the asymmetric case the reviewer flagged — a drop with no
    thread_id of its own must still be recognised as a duplicate of an
    existing row that DOES have one, for the same subject. Unreachable via
    `_persist_assessment` today (it never writes thread_id), but this
    function must not silently write a duplicate the moment that changes —
    or the moment two of THIS SCRIPT's own drops for one subject differ in
    whether their own thread_id could be identified.
    """
    existing_row = _existing_row(subject_agent_id="markham", thread_id="T-markham-1")
    drop_without_thread = _drop(subject_agent_id="markham", thread_id=None)

    result = _existing_assessment_for([existing_row], RUN_ID, drop_without_thread)

    assert result is existing_row


# ---------------------------------------------------------------------------
# FIX 1 — subject matching must tolerate the model's bot-name guess
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate_subject, drop_subject, expected",
    [
        # Real production pairs (raw_verdict->>'subject_agent_id' vs the
        # stored, authoritative subject_agent_id), measured directly across
        # all 63 opportunity_assessments rows.
        ("dangbot", "dang", True),
        ("kriegerbot", "krieger", True),
        ("leebot", "lee", True),
        ("DANGBOT", "dang", True),  # case-folded
        ("ThompsonBot", "thompson", True),  # the one that matters: thompson's
        # only recoverable candidate names the bot, not the agent.
        ("markham", "markham", True),  # plain exact match still works
        # The one real pair that MUST be refused: a last-name collision
        # (first-initial-prefixed agents), not a naming variant of one PI.
        ("epearce", "pearce", False),
        ("pearce", "epearce", False),
        ("weeraratna", "markham", False),  # a different PI outright
        (None, "pearce", False),
        ("pearce", None, False),
        (123, "pearce", False),  # non-string sidecar value
    ],
)
def test_subject_matches_bot_name_and_case_but_not_collisions(
    candidate_subject, drop_subject, expected
):
    assert _subject_matches(candidate_subject, drop_subject) is expected


def test_the_fallback_accepts_the_models_bot_name_guess():
    drop = _drop(subject_agent_id="thompson", created_at=T0)
    own_verdict = _verdict(subject_agent_id="ThompsonBot")
    rows = [(uuid.uuid4(), T0 - timedelta(milliseconds=250), _sidecar_response(own_verdict))]

    recovered, source = _recover_from_llm_logs(rows, drop)

    assert recovered is not None
    assert recovered["subject_agent_id"] == "ThompsonBot"
    assert source is not None


def test_the_fallback_refuses_a_last_name_collision():
    drop = _drop(subject_agent_id="pearce", created_at=T0)
    wrong_agent_verdict = _verdict(subject_agent_id="epearce")
    rows = [
        (uuid.uuid4(), T0 - timedelta(milliseconds=250), _sidecar_response(wrong_agent_verdict)),
    ]

    recovered, source = _recover_from_llm_logs(rows, drop)

    assert recovered is None
    assert source is None


# ---------------------------------------------------------------------------
# F1.4 / FIX 2 — narrow the llm_call_logs fallback, bound and pin the walk
# ---------------------------------------------------------------------------


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


def test_the_walk_prefers_a_farther_correct_subject_over_a_nearer_wrong_one():
    """Mirrors production's `pienta` shape (fix round 1 addendum): two
    candidates inside the 60s lookback window, the NEARER one belonging to a
    different PI (`huganir`) whose interview happened to interleave closer in
    time. The 60s cap alone would still hand pienta huganir's verdict; only
    the subject check (cooperating with the cap) catches it.
    """
    drop = _drop(subject_agent_id="pienta", created_at=T0)
    nearer_wrong_pi = (
        uuid.uuid4(), T0 - timedelta(seconds=5),
        _sidecar_response(_verdict(subject_agent_id="huganir")),
    )
    farther_correct_pi = (
        uuid.uuid4(), T0 - timedelta(seconds=30),
        _sidecar_response(_verdict(subject_agent_id="pienta")),
    )
    rows = [nearer_wrong_pi, farther_correct_pi]  # most-recent-first, as the real query returns

    recovered, source = _recover_from_llm_logs(rows, drop)

    assert recovered is not None
    assert recovered["subject_agent_id"] == "pienta"
    assert source is not None and "30.0s before drop" in source


def test_the_fallback_walk_is_bounded_by_max_lookback_seconds():
    """FIX 2: mirrors production's `hart` shape — the nearest candidate is
    the one that failed to parse (why the drop exists at all), and the only
    same-subject candidate left is a 272.6s-old SUPERSEDED sidecar. Without
    a cap the walk falls through to it and would recover a stale verdict as
    if it were current.
    """
    drop = _drop(subject_agent_id="hart", created_at=T0)
    near_but_unparseable = (
        uuid.uuid4(), T0 - timedelta(milliseconds=300),
        '<assessment_json>{"subject_agent_id": "hart", "truncated": tr',  # no closing tag
    )
    stale_same_subject = (
        uuid.uuid4(), T0 - timedelta(seconds=272.6),
        _sidecar_response(_verdict(subject_agent_id="hart")),
    )
    rows = [near_but_unparseable, stale_same_subject]  # most-recent-first

    recovered, source = _recover_from_llm_logs(rows, drop, max_lookback_seconds=60.0)

    assert recovered is None
    assert source is None


def test_a_wider_lookback_would_have_recovered_the_stale_row():
    """Sanity check on the test above: confirms the CAP, not something else
    (e.g. a typo in the sidecar), is what excludes the stale candidate."""
    drop = _drop(subject_agent_id="hart", created_at=T0)
    stale_same_subject = (
        uuid.uuid4(), T0 - timedelta(seconds=272.6),
        _sidecar_response(_verdict(subject_agent_id="hart")),
    )

    recovered, source = _recover_from_llm_logs(
        [stale_same_subject], drop, max_lookback_seconds=300.0
    )

    assert recovered is not None
    assert recovered["subject_agent_id"] == "hart"


def test_the_fallback_does_not_select_whole_rows():
    drop = _drop()

    stmt = _fallback_llm_log_query(RUN_ID, drop)

    selected_names = {col.name for col in stmt.selected_columns}
    assert selected_names == {"id", "created_at", "response_text"}


def test_the_fallback_query_orders_most_recent_first():
    """FIX 2(a): pins `.desc()` — flipping to `.asc()` makes the walk return
    the OLDEST same-subject sidecar in the run instead of the nearest one,
    and every other test in this file hand-orders its `rows` fixture, so
    nothing else would catch that flip.
    """
    drop = _drop()

    stmt = _fallback_llm_log_query(RUN_ID, drop)

    assert "DESC" in str(stmt)


# ---------------------------------------------------------------------------
# FIX 5 — derive the rubric stamp from the run itself
# ---------------------------------------------------------------------------


def test_rubric_stamp_is_derived_from_the_runs_own_rows():
    existing = [
        _existing_row(
            subject_agent_id="a", thread_id="T1",
            rubric_version="2.0.0", rubric_content_hash="abc123",
        ),
        _existing_row(
            subject_agent_id="b", thread_id="T2",
            rubric_version="2.0.0", rubric_content_hash="abc123",
        ),
    ]

    version, content_hash = _derive_rubric_stamp(existing)

    assert (version, content_hash) == ("2.0.0", "abc123")


def test_rubric_stamp_is_null_when_the_run_wrote_no_stamp():
    existing = [
        _existing_row(
            subject_agent_id="a", thread_id="T1",
            rubric_version=None, rubric_content_hash=None,
        ),
    ]

    version, content_hash = _derive_rubric_stamp(existing)

    assert (version, content_hash) == (None, None)


def test_rubric_stamp_derivation_refuses_when_the_run_disagrees_with_itself():
    existing = [
        _existing_row(
            subject_agent_id="a", thread_id="T1",
            rubric_version="2.0.0", rubric_content_hash="abc123",
        ),
        _existing_row(
            subject_agent_id="b", thread_id="T2",
            rubric_version="2.1.0", rubric_content_hash="def456",
        ),
    ]

    with pytest.raises(ValueError):
        _derive_rubric_stamp(existing)


def test_rubric_stamp_derivation_ignores_no_run_rows():
    version, content_hash = _derive_rubric_stamp([])

    assert (version, content_hash) == (None, None)
