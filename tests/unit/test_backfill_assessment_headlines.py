"""Unit tests for scripts/backfill_assessment_headlines.py (Task 7).

Pure-function test only — no database, no Slack. ``select_rows_needing_headline``
is the whole judgement the repair script makes about which rows are owed a
headline, and it is exercised directly against ``types.SimpleNamespace`` stand-ins
for ``OpportunityAssessment`` rows, exactly as
``tests/unit/test_backfill_dropped_verdicts.py`` exercises
``scripts/backfill_dropped_verdicts.py``'s own pure helpers — see that file for
the house pattern this one follows (including proof that
``from scripts.x import y`` works under this repo's pytest config with no
``scripts/__init__.py``).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from scripts.backfill_assessment_headlines import select_rows_needing_headline


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
