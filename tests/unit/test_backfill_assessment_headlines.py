"""Unit tests for scripts/backfill_assessment_headlines.py (Task 7, fix
round 1).

Pure-function tests only — no database. ``select_rows_needing_headline`` is
exercised against ``types.SimpleNamespace`` stand-ins for
``OpportunityAssessment`` rows, exactly as
``tests/unit/test_backfill_dropped_verdicts.py`` exercises
``scripts/backfill_dropped_verdicts.py``'s own pure helpers — see that file
for the house pattern this one follows (including proof that
``from scripts.x import y`` works under this repo's pytest config with no
``scripts/__init__.py``). ``apply_headline_repairs``/``exit_code_for`` are
also dependency-free aside from a caller-supplied Slack client, so the
safety-critical post/stamp behaviours (fix round 1's Fix 3) are exercised
here too, against ``tests/fakes.py::FakeSlackClient`` — no live database, no
real Slack workspace.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from scripts.backfill_assessment_headlines import (
    FAILED,
    POSTED,
    STAMPED,
    WOULD_POST,
    WOULD_STAMP,
    apply_headline_repairs,
    exit_code_for,
    select_rows_needing_headline,
)
from tests.fakes import FakeSlackClient


def test_the_repair_script_skips_rows_that_do_not_need_a_headline():
    owed = SimpleNamespace(
        id="a", summary_posted_at=None, rubric_content_hash="42aec0479ac6",
        thread_id="t1",
    )
    already = SimpleNamespace(
        id="b", summary_posted_at=datetime.now(UTC),
        rubric_content_hash="42aec0479ac6", thread_id="t2",
    )
    drifted = SimpleNamespace(
        id="c", summary_posted_at=None, rubric_content_hash="0000deadbeef",
        thread_id="t3",
    )

    to_post, skipped = select_rows_needing_headline(
        [owed, already, drifted],
        live_rubric_hash="42aec0479ac6", allow_rubric_drift=False,
    )
    assert [r.id for r in to_post] == ["a"]
    reasons = {r.id: why for r, why in skipped}
    assert "already" in reasons["b"]
    assert "rubric" in reasons["c"]

    to_post, _ = select_rows_needing_headline(
        [owed, already, drifted],
        live_rubric_hash="42aec0479ac6", allow_rubric_drift=True,
    )
    assert [r.id for r in to_post] == ["a", "c"]


def test_a_null_rubric_hash_is_not_treated_as_drift():
    """A row with no stamp at all predates the rubric-stamp column entirely
    (or predates the rubric regime that started stamping it) — that is a
    different, unknowable fact from "this row was stamped against an OLD
    rubric", and must not be skipped as drift. It is posted like any other
    owed row, and the caller (not this pure selector) is responsible for
    deciding whether an unstamped verdict's rendered band/score should be
    trusted at all.
    """
    unstamped = SimpleNamespace(
        id="d", summary_posted_at=None, rubric_content_hash=None, thread_id="t4",
    )

    to_post, skipped = select_rows_needing_headline(
        [unstamped], live_rubric_hash="42aec0479ac6", allow_rubric_drift=False,
    )

    assert [r.id for r in to_post] == ["d"]
    assert skipped == []


def test_a_row_missing_the_rubric_content_hash_attribute_is_not_drift():
    """The selector must work on ``getattr(row, "rubric_content_hash", None)``
    — a bare object with no such attribute at all reads the same as an
    explicit ``None``, not as drift."""
    unstamped = SimpleNamespace(id="e", summary_posted_at=None, thread_id="t5")

    to_post, skipped = select_rows_needing_headline(
        [unstamped], live_rubric_hash="42aec0479ac6", allow_rubric_drift=False,
    )

    assert [r.id for r in to_post] == ["e"]
    assert skipped == []


# ---------------------------------------------------------------------------
# Fix round 1, Fix 2 — --stamp-only must never be gated on rubric drift
# ---------------------------------------------------------------------------


def test_a_drifted_row_can_still_be_stamped():
    """The exact production situation that forced this fix: run 61ccad6d's
    five already-in-Slack rows are stamped rubric 3.2.0 against a live
    3.4.0 document. A ``--stamp-only`` pass must be able to record all five
    without ``--allow-rubric-drift`` — stamping renders and posts nothing, so
    there is no number that could be misreported by any rubric revision.
    """
    drifted = SimpleNamespace(
        id="f", summary_posted_at=None, rubric_content_hash="42aec0479ac6",
        thread_id="t6",
    )

    # The posting path (default) still skips it as drift...
    to_post, skipped = select_rows_needing_headline(
        [drifted], live_rubric_hash="b7b0a1d6a4a5", allow_rubric_drift=False,
    )
    assert to_post == []
    assert "rubric" in skipped[0][1]

    # ...but the stamp-only path does not, even with allow_rubric_drift left
    # False.
    to_post, skipped = select_rows_needing_headline(
        [drifted], live_rubric_hash="b7b0a1d6a4a5", allow_rubric_drift=False,
        for_stamp_only=True,
    )
    assert [r.id for r in to_post] == ["f"]
    assert skipped == []


def test_stamp_only_still_skips_an_already_announced_row():
    """The one skip that DOES still apply under --stamp-only: re-stamping a
    row that is already announced would not itself post a duplicate, but it
    would overwrite a real `summary_posted_at` with a fresh one, destroying
    the original record of when the headline actually reached Slack."""
    already = SimpleNamespace(
        id="g", summary_posted_at=datetime.now(UTC),
        rubric_content_hash="42aec0479ac6", thread_id="t7",
    )

    to_post, skipped = select_rows_needing_headline(
        [already], live_rubric_hash="b7b0a1d6a4a5", allow_rubric_drift=False,
        for_stamp_only=True,
    )

    assert to_post == []
    assert "already" in skipped[0][1]


# ---------------------------------------------------------------------------
# Fix round 1, Fix 3 — the safety-critical post/stamp behaviours
# ---------------------------------------------------------------------------


def _row(**overrides):
    defaults = dict(
        id="row-1", agent_id="blackbird", subject_agent_id="markham",
        summary_posted_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_a_successful_post_stamps_summary_posted_at():
    row = _row()
    client = FakeSlackClient(agent_id="blackbird")

    results = apply_headline_repairs(
        [(row, "the headline text")],
        client_for=lambda agent_id: client,
        apply=True, stamp_only=False,
    )

    assert [outcome for _, _, outcome in results] == [POSTED]
    assert row.summary_posted_at is not None
    assert len(client.posted) == 1


def test_a_post_that_raises_leaves_summary_posted_at_null():
    row = _row()
    client = FakeSlackClient(agent_id="blackbird")

    def boom(*a, **kw):
        raise RuntimeError("Slack is down")
    client.post_message = boom

    results = apply_headline_repairs(
        [(row, "the headline text")],
        client_for=lambda agent_id: client,
        apply=True, stamp_only=False,
    )

    assert [outcome for _, _, outcome in results] == [FAILED]
    assert row.summary_posted_at is None


def test_a_post_that_returns_a_falsy_result_leaves_summary_posted_at_null():
    row = _row()
    client = FakeSlackClient(agent_id="blackbird")
    client.post_message = lambda *a, **kw: None

    results = apply_headline_repairs(
        [(row, "the headline text")],
        client_for=lambda agent_id: client,
        apply=True, stamp_only=False,
    )

    assert [outcome for _, _, outcome in results] == [FAILED]
    assert row.summary_posted_at is None


def test_stamp_only_apply_stamps_without_touching_slack():
    row = _row()
    client = FakeSlackClient(agent_id="blackbird")

    results = apply_headline_repairs(
        [(row, "the headline text")],
        client_for=lambda agent_id: client,
        apply=True, stamp_only=True,
    )

    assert [outcome for _, _, outcome in results] == [STAMPED]
    assert row.summary_posted_at is not None
    assert client.posted == []


def test_dry_run_posts_nothing_and_writes_nothing():
    posted_row = _row(id="row-1")
    stamp_row = _row(id="row-2")
    client = FakeSlackClient(agent_id="blackbird")

    posted_results = apply_headline_repairs(
        [(posted_row, "text")], client_for=lambda agent_id: client,
        apply=False, stamp_only=False,
    )
    stamp_results = apply_headline_repairs(
        [(stamp_row, "text")], client_for=lambda agent_id: client,
        apply=False, stamp_only=True,
    )

    assert [outcome for _, _, outcome in posted_results] == [WOULD_POST]
    assert [outcome for _, _, outcome in stamp_results] == [WOULD_STAMP]
    assert posted_row.summary_posted_at is None
    assert stamp_row.summary_posted_at is None
    assert client.posted == []


def test_a_missing_client_is_a_failure_not_a_crash():
    row = _row()

    results = apply_headline_repairs(
        [(row, "text")], client_for=lambda agent_id: None,
        apply=True, stamp_only=False,
    )

    assert [outcome for _, _, outcome in results] == [FAILED]
    assert row.summary_posted_at is None


def test_exit_code_is_nonzero_when_any_intended_post_failed():
    """A mixed batch — one row posts fine, a DIFFERENT row's client has no
    usable token — is exactly the scenario the exit code must react to: a
    partial failure must not look like a clean run."""
    ok_row = _row(id="ok", agent_id="blackbird")
    bad_row = _row(id="bad", agent_id="no-token-for-this-one")
    ok_client = FakeSlackClient(agent_id="blackbird")

    def client_for(agent_id):
        return ok_client if agent_id == "blackbird" else None

    results = apply_headline_repairs(
        [(ok_row, "text"), (bad_row, "text")],
        client_for=client_for, apply=True, stamp_only=False,
    )

    outcomes = {row.id: outcome for row, _, outcome in results}
    assert outcomes == {"ok": POSTED, "bad": FAILED}
    assert exit_code_for(results) == 1


def test_exit_code_is_zero_when_nothing_failed():
    row = _row()
    client = FakeSlackClient(agent_id="blackbird")

    results = apply_headline_repairs(
        [(row, "text")], client_for=lambda agent_id: client,
        apply=True, stamp_only=False,
    )

    assert exit_code_for(results) == 0


def test_exit_code_is_nonzero_for_an_actual_failure():
    row = _row()

    results = apply_headline_repairs(
        [(row, "text")], client_for=lambda agent_id: None,
        apply=True, stamp_only=False,
    )

    assert exit_code_for(results) == 1
