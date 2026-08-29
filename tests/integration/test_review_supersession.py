"""Task 8: before the supersession DELETE removes a superseded assessment row,
its human-review rows must be re-pointed to the row that replaces it.

``_retire_superseded_verdict`` already deletes a provisional
``opportunity_assessments`` row the moment a later reply in the same
interview supersedes it (see ``tests/integration/test_hub_assessment_capture_
gate.py`` for the full "one interview, one verdict" story). Three of the four
review tables CASCADE off ``opportunity_assessments.id``
(``AssessmentReview``, ``AssessmentReviewEvent``, ``AssessmentReviewAssignment``)
and the fourth (``PromptChangeSuggestion``) is SET NULL — so, unrepointed, a
human's review of a verdict that turned out to be provisional is silently
destroyed (the first two) or orphaned (the third) the moment a later reply in
the SAME interview supersedes it, minutes later, mid-run. This module proves
the re-point that now runs, in the same transaction as the delete, before it.

Driven directly through ``_persist_assessment``/``_retire_superseded_verdict``
(the unbound-method-on-a-stub idiom ``test_opportunity_assessment_
persistence.py`` already uses) rather than through ``_capture_hub_assessment``
end to end: the ordinal/closes_thread wiring that decides WHETHER a verdict
supersedes another is already exercised by ``test_hub_assessment_capture_
gate.py``, so this module only needs a valid ``(agent_id, thread, superseded)``
triple to drive the retire step itself, and needs to construct scenarios (a
pre-existing assignment conflict on the REPLACEMENT row, before the retire
that would otherwise re-point another one onto it) that are not reachable
through the higher-level call in a single natural turn.
"""

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.simulation import SimulationEngine, _HeldVerdict
from src.agent.state import ThreadState
from src.models import (
    AssessmentDrop,
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    OpportunityAssessment,
    PromptChangeSuggestion,
    SimulationRun,
    User,
)
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _reset_review_tables(engine):
    """Start every test in this module with these tables empty.

    Every test here commits for real against the shared session-scoped
    ``engine`` (the same reason ``test_opportunity_assessment_persistence.
    py``'s ``_reset_assessment_tables`` exists) — several assertions below
    query a review table with no filter at all, which is the whole point:
    "exactly one assignment row survives" is only a meaningful claim if the
    table started empty.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(sa_delete(AssessmentReviewAssignment))
        await db.execute(sa_delete(AssessmentReviewEvent))
        await db.execute(sa_delete(AssessmentReview))
        await db.execute(sa_delete(PromptChangeSuggestion))
        await db.execute(sa_delete(AssessmentDrop))
        await db.execute(sa_delete(OpportunityAssessment))
        await db.execute(sa_delete(SimulationRun))
        await db.commit()
    yield


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


def _stub(factory, run_id):
    return SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )


def _thread(thread_id: str = "t1") -> ThreadState:
    return ThreadState(thread_id=thread_id, channel="general", other_agent_id="wang")


async def _no_seed(*_args, **_kwargs):
    """Stubs out ``_seed_consults_from_db`` on the engine under test.

    That method conditionally opens its OWN session (only when the verdict's
    recommendation/band owes a panel and nothing is yet recorded — see its
    docstring), which would otherwise be an extra, hard-to-predict
    ``session_factory()`` call ahead of the write this module's buffered-
    replacement test needs to fail on a specific, single call. The verdict
    fixture below uses a plain "advance" recommendation for the same reason
    test_persist_assessment_failure_is_buffered_and_a_later_flush_persists_it
    stubs this out: the claim under test is about the retry/re-point
    machinery, not the specialist floor.
    """
    return None


async def _make_user(factory) -> User:
    async with factory() as db:
        user = await factories.make_user(db)
        await db.commit()
        return user


def _verdict(score: int) -> dict:
    return {
        "subject_agent_id": "wang",
        "recommendation": "advance",
        "scores": {"differentiation": score},
    }


async def _attach_review_rows(factory, assessment_id, assignee_id) -> dict:
    """One row of each of the four review-table kinds, attached to
    ``assessment_id``. Returns each row's own id so the test can look each up
    by primary key afterward rather than trusting an ``assessment_id`` filter
    that is exactly what is under test."""
    async with factory() as db:
        review = AssessmentReview(
            assessment_id=assessment_id, reviewer_name="Dr. Reviewer",
            score=4, feedback_mode="learn",
        )
        event = AssessmentReviewEvent(
            assessment_id=assessment_id, action="approved", actor_name="Dr. Reviewer",
        )
        assignment = AssessmentReviewAssignment(
            assessment_id=assessment_id, assignee_user_id=assignee_id,
            assignee_name="Assignee", assigned_by_name="Boss",
        )
        suggestion = PromptChangeSuggestion(
            assessment_id=assessment_id, subject_label="s",
            feedback_snapshot=[], target="scout_hub", prompt_files=[],
            suggestion="tighten the rubric wording", transcript_available=False,
        )
        db.add_all([review, event, assignment, suggestion])
        await db.flush()
        ids = {
            "review": review.id, "event": event.id,
            "assignment": assignment.id, "suggestion": suggestion.id,
        }
        await db.commit()
        return ids


async def _cleanup(factory, run_id, *, user_ids=()):
    async with factory() as db:
        # PromptChangeSuggestion.assessment_id is SET NULL, not CASCADE — a
        # row that outlives the assessment it was attached to is not cleaned
        # up by deleting the run below, so it is swept explicitly here.
        await db.execute(sa_delete(PromptChangeSuggestion))
        stale = (await db.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await db.delete(stale)  # cascades the assessment(s) and their reviews
        if user_ids:
            await db.execute(sa_delete(User).where(User.id.in_(user_ids)))
        await db.commit()


class _FailOnceFactory:
    """Wraps a real session factory and raises exactly once, on its very next
    call after being armed — the same "fails the first attempt, then behaves"
    shape as ``flaky_factory`` in
    ``test_persist_assessment_failure_is_buffered_and_a_later_flush_persists_
    it``, but arming is explicit here so the ONE call this module means to
    fail (the replacement verdict's own write) is unambiguous regardless of
    how many session-factory calls preceded it.
    """

    def __init__(self, real_factory):
        self._real = real_factory
        self.armed = False

    def __call__(self):
        if self.armed:
            self.armed = False
            raise RuntimeError("pool checkout timed out")
        return self._real()


@pytest.mark.asyncio
async def test_supersession_re_points_review_rows_to_the_replacement(engine):
    """The mission pin: a human's review of a verdict that turns out to be
    provisional must survive that verdict being superseded — on ALL FOUR
    review-table kinds, including PromptChangeSuggestion (SET NULL, not
    CASCADE, and therefore silently orphaned rather than destroyed if the
    re-point is skipped)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    stub = _stub(factory, run_id)
    stub._seed_consults_from_db = _no_seed
    thread = _thread()
    assignee = await _make_user(factory)
    try:
        held_a, a_id = await SimulationEngine._persist_assessment(
            stub, "blackbird", "general", _verdict(3), slack_ts="1.1", thread=thread,
        )
        assert held_a is True and a_id is not None

        ids = await _attach_review_rows(factory, a_id, assignee.id)

        superseded = _HeldVerdict(ordinal=1, final=False, slack_ts="1.1", announced=False)
        held_b, b_id = await SimulationEngine._persist_assessment(
            stub, "blackbird", "general", _verdict(4), slack_ts="2.2", thread=thread,
        )
        assert held_b is True and b_id is not None

        await SimulationEngine._retire_superseded_verdict(
            stub, "blackbird", thread, superseded,
            replacement_ordinal=2, replacement_id=b_id,
        )

        async with factory() as check:
            assert await check.get(OpportunityAssessment, a_id) is None, "A is gone"
            assert await check.get(OpportunityAssessment, b_id) is not None

            review = await check.get(AssessmentReview, ids["review"])
            assert review is not None, "CASCADE deleted it — the re-point did not run"
            assert review.assessment_id == b_id

            event = await check.get(AssessmentReviewEvent, ids["event"])
            assert event is not None
            assert event.assessment_id == b_id

            assignment = await check.get(AssessmentReviewAssignment, ids["assignment"])
            assert assignment is not None
            assert assignment.assessment_id == b_id

            suggestion = await check.get(PromptChangeSuggestion, ids["suggestion"])
            assert suggestion is not None
            assert suggestion.assessment_id == b_id, (
                "NULL here means the delete's SET NULL fired instead of the "
                "re-point — i.e. the re-point silently failed (or never ran)"
            )

            drops = (await check.execute(
                select(AssessmentDrop).where(AssessmentDrop.simulation_run_id == run_id)
            )).scalars().all()
            assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
    finally:
        await _cleanup(factory, run_id, user_ids=[assignee.id])


@pytest.mark.asyncio
async def test_re_point_skips_conflicting_assignments(engine):
    """The same assignee already has a row on BOTH the superseded verdict and
    its replacement (a staff member assigned before the interview concluded a
    second time). The unique constraint on (assessment_id, assignee_user_id)
    means a naive re-point would violate it outright; the brief's SQL instead
    filters the moved rows to assignees not already present on the
    replacement, so the conflicting old row is left in place — and is then
    swept away for free when the DELETE removes its now-orphaned parent."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    stub = _stub(factory, run_id)
    stub._seed_consults_from_db = _no_seed
    thread = _thread()
    assignee = await _make_user(factory)
    try:
        held_a, a_id = await SimulationEngine._persist_assessment(
            stub, "blackbird", "general", _verdict(3), slack_ts="1.1", thread=thread,
        )
        held_b, b_id = await SimulationEngine._persist_assessment(
            stub, "blackbird", "general", _verdict(4), slack_ts="2.2", thread=thread,
        )
        assert a_id is not None and b_id is not None

        async with factory() as db:
            db.add(AssessmentReviewAssignment(
                assessment_id=a_id, assignee_user_id=assignee.id,
                assignee_name="Assignee", assigned_by_name="Boss A",
            ))
            b_assignment = AssessmentReviewAssignment(
                assessment_id=b_id, assignee_user_id=assignee.id,
                assignee_name="Assignee", assigned_by_name="Boss B",
            )
            db.add(b_assignment)
            await db.flush()
            b_assignment_id = b_assignment.id
            await db.commit()

        superseded = _HeldVerdict(ordinal=1, final=False, slack_ts="1.1", announced=False)
        await SimulationEngine._retire_superseded_verdict(
            stub, "blackbird", thread, superseded,
            replacement_ordinal=2, replacement_id=b_id,
        )

        async with factory() as check:
            rows = (await check.execute(select(AssessmentReviewAssignment))).scalars().all()
            assert len(rows) == 1, "the pre-existing conflict must not duplicate"
            assert rows[0].id == b_assignment_id, (
                "the surviving row must be B's ORIGINAL assignment — a naive "
                "re-point either violates the unique constraint or clobbers "
                "this row with A's copy"
            )
            assert rows[0].assigned_by_name == "Boss B"
    finally:
        await _cleanup(factory, run_id, user_ids=[assignee.id])


@pytest.mark.asyncio
async def test_re_point_tolerates_a_buffered_replacement(engine):
    """The replacement verdict's own write fails on its first attempt (a pool
    timeout, say) and is queued on ``_pending_assessments`` instead of
    committed — ``_persist_assessment`` returns ``(True, None)``.
    ``_retire_superseded_verdict`` must still retire A cleanly: nothing to
    re-point onto (there is no replacement row in the database yet), so the
    re-point is skipped and A's review row goes with it via CASCADE — and
    nothing may raise, the same best-effort contract as every other write on
    this path."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    stub = _stub(factory, run_id)
    stub._seed_consults_from_db = _no_seed
    thread = _thread()
    failing_factory = _FailOnceFactory(factory)
    stub.session_factory = failing_factory
    try:
        held_a, a_id = await SimulationEngine._persist_assessment(
            stub, "blackbird", "general", _verdict(3), slack_ts="1.1", thread=thread,
        )
        assert held_a is True and a_id is not None

        async with factory() as db:
            db.add(AssessmentReview(
                assessment_id=a_id, reviewer_name="Dr. Reviewer",
                score=4, feedback_mode="learn",
            ))
            await db.commit()

        failing_factory.armed = True
        superseded = _HeldVerdict(ordinal=1, final=False, slack_ts="1.1", announced=False)
        held_b, b_id = await SimulationEngine._persist_assessment(
            stub, "blackbird", "general", _verdict(4), slack_ts="2.2", thread=thread,
        )
        assert held_b is True and b_id is None, "buffered, not committed"
        assert len(stub._pending_assessments) == 1
        assert failing_factory.armed is False, "the single arm must be consumed"

        await SimulationEngine._retire_superseded_verdict(
            stub, "blackbird", thread, superseded,
            replacement_ordinal=2, replacement_id=b_id,
        )

        async with factory() as check:
            assert await check.get(OpportunityAssessment, a_id) is None
            reviews = (await check.execute(select(AssessmentReview))).scalars().all()
            assert reviews == [], "A's review row must CASCADE away, not survive orphaned"
            drops = (await check.execute(
                select(AssessmentDrop).where(AssessmentDrop.simulation_run_id == run_id)
            )).scalars().all()
            assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
    finally:
        stub.session_factory = factory
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_persist_returns_false_none_with_no_db():
    """No database configured at all (see ``SimulationEngine.__init__``):
    ``_persist_assessment`` must answer ``(False, None)``, not merely a
    falsy first element — ``_capture_hub_assessment``'s call site unpacks
    both, and a caller that only checked truthiness would treat ANY tuple,
    including this one, as HELD."""
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=None, simulation_run_id=None,
    )
    held, replacement_id = await SimulationEngine._persist_assessment(
        stub, "blackbird", "general", {"scores": {"differentiation": 5}},
    )
    assert held is False
    assert replacement_id is None
