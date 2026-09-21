"""The triage queue's sort/lab controls, and the detail page's way out to the log.

The service tests (``tests/unit/test_directory_assessment_sorting.py``) prove the
SQL. These prove the wiring, which is a different failure: the query parameters
have to be declared on BOTH handlers, the chosen values have to reach BOTH
wrappers, and the three selects have to sit in ONE form — otherwise changing the
sort silently resets the run to "current", which on a `--fresh`-wiped instance
means the rows a reader was looking at vanish.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    OpportunityAssessment,
)
from src.services.assessment_reviews import review_columns_for
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="queue-admin@example.org"
    )


@pytest.fixture
async def manager(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="queue-manager@example.org"
    )


async def _seed(db_session):
    """Two labs, and a score order that is the REVERSE of the recency order —
    so `sort=recent` cannot be confused with the default by looking at the page.
    """
    run = await factories.make_simulation_run(db_session)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wang", channel_name="general",
            company_or_project="Queue Top Scorer", recommendation="advance",
            weighted_score=4.6, band="advance",
            created_at=now - timedelta(minutes=90),
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            company_or_project="Queue Recent Decline", recommendation="pass",
            weighted_score=1.2, band="pass",
            created_at=now - timedelta(minutes=5),
        )
    )
    await db_session.flush()
    return run


def _order(html: str, first: str, second: str) -> bool:
    return html.index(first) < html.index(second)


@pytest.mark.parametrize(
    ("base", "role"),
    [("/admin", USER_ROLE_ADMIN), ("/manager", USER_ROLE_MANAGER)],
)
async def test_both_surfaces_offer_the_sort_and_lab_controls(
    client, db_session, base, role
):
    staff = await factories.make_user(
        db_session, user_role=role, email=f"controls{base.strip('/')}@example.org"
    )
    run = await _seed(db_session)

    html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
        )
    ).text

    assert 'name="sort"' in html
    assert 'name="lab"' in html
    # Labels come from the service, so this also pins that they arrived.
    assert "Score (triage)" in html
    assert "Most recent" in html
    assert "All labs" in html
    # Both labs offered, as the bare agent id the table's Lab column shows.
    assert '<option value="wang"' in html
    assert '<option value="gordy"' in html
    # One form, so each select carries the others. Asserted structurally: the
    # run select and both new selects must be inside the SAME <form>.
    form = html.split(f'action="{base}/assessments"', 1)[1].split("</form>", 1)[0]
    assert 'name="run_id"' in form
    assert 'name="sort"' in form
    assert 'name="lab"' in form


@pytest.mark.parametrize("base", ["/admin", "/manager"])
async def test_sort_recent_reorders_the_table_on_both_surfaces(
    client, db_session, base, admin, manager
):
    staff = admin if base == "/admin" else manager
    run = await _seed(db_session)

    default_html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
        )
    ).text
    recent_html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}&sort=recent",
            headers=auth_headers(staff.id),
        )
    ).text

    assert _order(default_html, "Queue Top Scorer", "Queue Recent Decline")
    assert _order(recent_html, "Queue Recent Decline", "Queue Top Scorer")
    # The control shows the state it is actually in.
    assert '<option value="recent" selected' in recent_html
    assert '<option value="score" selected' in default_html


@pytest.mark.parametrize("base", ["/admin", "/manager"])
async def test_the_lab_filter_narrows_the_table(client, db_session, base, admin, manager):
    staff = admin if base == "/admin" else manager
    run = await _seed(db_session)

    html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}&lab=gordy",
            headers=auth_headers(staff.id),
        )
    ).text

    assert "Queue Recent Decline" in html
    assert "Queue Top Scorer" not in html
    assert '<option value="gordy" selected' in html
    # Still reachable: the option for the lab that was filtered OUT is present.
    assert '<option value="wang"' in html


@pytest.mark.parametrize("base", ["/admin", "/manager"])
async def test_the_run_selection_survives_a_sort_change(
    client, db_session, base, admin, manager
):
    """The trap this closes: with the sort in its own form, submitting it drops
    run_id, the page falls back to the CURRENT run, and on a --fresh instance
    the rows the reader had selected disappear with no message."""
    staff = admin if base == "/admin" else manager
    old_run = await _seed(db_session)
    newer_run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=newer_run.id, agent_id="blackbird",
            subject_agent_id="fu", channel_name="general",
            company_or_project="Queue Current Run Row",
        )
    )
    await db_session.flush()

    html = (
        await client.get(
            f"{base}/assessments?run_id={old_run.id}&sort=lab",
            headers=auth_headers(staff.id),
        )
    ).text

    assert "Queue Top Scorer" in html
    assert "Queue Current Run Row" not in html
    assert f'<option value="{old_run.id}" selected' in html
    assert '<option value="lab" selected' in html


async def test_an_unknown_sort_or_lab_still_renders_the_queue(client, db_session, admin):
    run = await _seed(db_session)
    resp = await client.get(
        f"/admin/assessments?run_id={run.id}&sort=by-vibes&lab=ghostlab",
        headers=auth_headers(admin.id),
    )
    assert resp.status_code == 200
    assert "Queue Top Scorer" in resp.text
    assert "Queue Recent Decline" in resp.text
    assert '<option value="score" selected' in resp.text


async def test_the_manager_controls_never_point_into_admin(client, db_session, manager):
    run = await _seed(db_session)
    html = (
        await client.get(
            f"/manager/assessments?run_id={run.id}&sort=lab&lab=wang",
            headers=auth_headers(manager.id),
        )
    ).text
    assert "/admin/" not in html


# ---------------------------------------------------------------------------
# The detail page's link out to the raw calls (admin only)
# ---------------------------------------------------------------------------


async def test_the_admin_detail_page_links_to_this_interviews_llm_calls(
    client, db_session, admin
):
    """A prefilter is the whole point: /admin/activity/{run}/llm-calls unfiltered
    is every call of the run, which is where the previous "go read the log"
    answer died."""
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id="wang", channel_name="scout-wang",
        company_or_project="Linked Interview Co",
    )
    db_session.add(assessment)
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text

    assert (
        f'href="/admin/activity/{run.id}/llm-calls'
        "?agent=blackbird&amp;channel=scout-wang\"" in html
    )
    assert "LLM calls for this interview" in html


async def test_the_manager_detail_page_has_no_llm_calls_link(
    client, db_session, manager
):
    """D10: the LLM drill-down is not a manager surface, and a control that
    403s on click is worse than no control."""
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id="wang", channel_name="scout-wang",
        company_or_project="Linked Interview Co",
    )
    db_session.add(assessment)
    await db_session.flush()

    html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
        )
    ).text

    assert "llm-calls" not in html
    assert "LLM calls for this interview" not in html
    assert "/admin/" not in html


# ---------------------------------------------------------------------------
# The panel badge in the table, on both surfaces
#
# The detail page's five-state panel box is one click away, and until now the
# table gave a reader no reason to take that click: the badge gated on
# `a.panel_incomplete` alone, so a verdict the floor could not check and a
# verdict that records nothing about a floor at all looked exactly like a
# verified one. Twelve production rows sat in that blind spot while the detail
# page called them verified.
#
# Both partials below are included by the manager templates too, so every one of
# these runs on /admin and /manager.
# ---------------------------------------------------------------------------


async def _seed_panel_states(db_session):
    """One row per panel state that has a badge, plus the two that must not."""
    run = await factories.make_simulation_run(db_session)
    rows = {
        "Gap Co": dict(
            panel_incomplete=True, missing_domains=["chemistry"], panel_owed=True
        ),
        "Unverified Co": dict(
            panel_incomplete=False, missing_domains=[], panel_owed=True
        ),
        "Unrecorded Co": dict(
            panel_incomplete=False, missing_domains=None, panel_owed=None
        ),
        "Verified Co": dict(
            panel_incomplete=False, missing_domains=None, panel_owed=True
        ),
        "Not Owed Co": dict(
            panel_incomplete=False, missing_domains=None, panel_owed=False
        ),
    }
    for index, (project, panel) in enumerate(rows.items()):
        db_session.add(
            OpportunityAssessment(
                simulation_run_id=run.id, agent_id="blackbird",
                subject_agent_id="gordy", channel_name="general",
                company_or_project=project, recommendation="conditional",
                weighted_score=4.0 - index * 0.1, band="advance",
                **panel,
            )
        )
    await db_session.flush()
    return run


@pytest.mark.parametrize("base", ["/admin", "/manager"])
async def test_the_list_page_flags_an_unrecorded_panel(
    client, db_session, base, admin, manager
):
    """A row that does not record whether a panel was owed must be visibly
    distinct in the table — it is the state 12 production rows are in, and it is
    never a verification.

    The two states that DO have an answer (`verified`, `not_owed`) stay
    unbadged: badging everything is the same as badging nothing.
    """
    staff = admin if base == "/admin" else manager
    run = await _seed_panel_states(db_session)

    html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
        )
    ).text

    assert "panel not recorded" in html
    assert "panel unverified" in html
    assert "&#9873; panel" in html or "⚑ panel" in html
    # Exactly three badges — the two answered states must not have picked one up.
    assert html.count("panel not recorded") == 1
    assert html.count("panel unverified") == 1
    # ...and neither answered state fell through to the terminal fallback. This
    # is the other half of the unknown-state test below: that one proves the
    # fallback fires, this one proves it does not fire on a state the page can
    # actually read.
    assert "panel state unknown" not in html
    # And the run-level warning counts all three, not just the demonstrated gap.
    # Whitespace-normalized: the banner's number and its noun are on separate
    # template lines, so the rendered HTML carries a newline plus indentation
    # between them.
    flat = " ".join(html.split())
    assert "3 verdicts" in flat
    # The BOLD headline has to name all three classes it counts, not two of them
    # with the third relegated to the unstyled sentence underneath. Today every
    # one of the 64 historical rows is in that third class — `panel_owed IS
    # NULL`, deliberately never backfilled — so a headline reading "incomplete or
    # unverified" describes 64 rows as something zero of them are known to be.
    headline = flat.split('<span class="font-semibold">', 1)[1].split("</span>", 1)[0]
    assert "3 verdicts" in headline
    assert "unrecorded" in headline, (
        "the third class must be named where the reader actually reads"
    )
    assert "incomplete" in headline and "unverified" in headline


@pytest.mark.parametrize("base", ["/admin", "/manager"])
async def test_a_verified_panel_gets_no_badge_and_no_warning(
    client, db_session, base, admin, manager
):
    """The control: a run whose only verdict is a floor-checked, gap-free one
    shows no banner and no badge. Without this, a badge rendered unconditionally
    would satisfy every assertion above."""
    staff = admin if base == "/admin" else manager
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            company_or_project="Only Verified Co", recommendation="conditional",
            weighted_score=4.0, band="advance",
            panel_incomplete=False, missing_domains=None, panel_owed=True,
        )
    )
    await db_session.flush()

    html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
        )
    ).text

    assert "Only Verified Co" in html
    assert "panel not recorded" not in html
    assert "panel unverified" not in html
    assert "panel state unknown" not in html
    assert "specialist panel" not in html


@pytest.mark.parametrize("base", ["/admin", "/manager"])
async def test_an_unknown_panel_state_is_never_left_unbadged(
    client, db_session, base, admin, manager, monkeypatch
):
    """The list page enumerated its three badged states POSITIVELY, so a sixth
    `panel_state` — a typo, a state added to `panel_state` and not here, a
    future finding — fell off the end with no badge at all, which on this table
    is exactly how `verified` renders.

    The detail page's terminal `{% else %}` is deliberately the amber "not
    recorded" box for this reason
    (`test_an_unknown_panel_state_never_renders_green`); the two surfaces
    defaulted in opposite directions. The fallback must be the claim that costs
    nothing to make wrongly.

    Forced through the real render path rather than read out of the template
    source: what matters is the HTML a reader sees. The row is seeded in the
    shape that renders `verified` — `test_a_verified_panel_gets_no_badge_and_no_
    warning` above is the control proving that shape carries no badge — so any
    badge here comes from the fallback and nothing else.
    """
    staff = admin if base == "/admin" else manager
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            company_or_project="Off Contract Co", recommendation="conditional",
            weighted_score=4.0, band="advance",
            panel_incomplete=False, missing_domains=None, panel_owed=True,
        )
    )
    await db_session.flush()
    monkeypatch.setattr(
        "src.services.directory.panel_state",
        lambda _assessment: "a_state_that_does_not_exist",
    )

    html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
        )
    ).text

    assert "Off Contract Co" in html
    assert html.count("panel state unknown") == 1, (
        "an unhandled panel state must not read like a verified one"
    )
    # And it must not have been quietly filed under one of the three answers
    # the page does know how to make.
    assert "panel not recorded" not in html
    assert "panel unverified" not in html
    assert "&#9873; panel" not in html and "⚑ panel" not in html


# ---------------------------------------------------------------------------
# Task 7: the "Assigned"/"Reviewed by" columns and the approval-status chip
#
# review_columns_for (src/services/assessment_reviews.py) is the batched read
# behind both. It is deliberately three IN-clause queries plus a Python fold,
# never a DISTINCT ON: the status chip needs only the LATEST status event per
# assessment, while "reviewed by" needs EVERY actor who ever touched the row
# (a feedback author or a status-event actor), and one DISTINCT ON query
# returns a single row per assessment and would silently drop every earlier
# actor.
# ---------------------------------------------------------------------------


async def _seed_reviewed_row(db_session, run, *, project="Reviewed Co"):
    """One row touched by all three review tables, with EXPLICIT created_at
    values — ties are real inside one test transaction (Postgres's `now()`
    is transaction-start time), so the chronological order this pins would be
    a coin flip without them.

    Assigned: Alice A. Then feedback by Bob B, then approved by Cara C, then
    disapproved by Dana D — the disapprove is both the last event (so it is
    the status chip) and the last actor (so it is the last name folded into
    "reviewed by").
    """
    alice = await factories.make_user(db_session, name="Alice A")
    bob = await factories.make_user(db_session, name="Bob B")
    cara = await factories.make_user(db_session, name="Cara C")
    dana = await factories.make_user(db_session, name="Dana D")
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id="wang", channel_name="general",
        company_or_project=project, recommendation="advance",
        weighted_score=4.0, band="advance",
    )
    db_session.add(assessment)
    await db_session.flush()

    t0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    db_session.add(AssessmentReviewAssignment(
        assessment_id=assessment.id, assignee_user_id=alice.id, assignee_name="Alice A",
        assigned_by_user_id=alice.id, assigned_by_name="Alice A", created_at=t0,
    ))
    db_session.add(AssessmentReview(
        assessment_id=assessment.id, reviewer_user_id=bob.id, reviewer_name="Bob B",
        score=4, comment="Looks promising", feedback_mode="learn",
        created_at=t0 + timedelta(minutes=1),
    ))
    db_session.add(AssessmentReviewEvent(
        assessment_id=assessment.id, action="approved",
        actor_user_id=cara.id, actor_name="Cara C",
        created_at=t0 + timedelta(minutes=2),
    ))
    db_session.add(AssessmentReviewEvent(
        assessment_id=assessment.id, action="disapproved",
        actor_user_id=dana.id, actor_name="Dana D",
        created_at=t0 + timedelta(minutes=3),
    ))
    await db_session.flush()
    return assessment


#: The exact opening substring the template's card `<div>` must carry
#: (trailing space included) — see the header comment of
#: templates/admin/_assessments_body.html and the card list section of
#: task-9-brief.md.
_CARD_OPEN = 'class="assessment-card '


def _row_slice(html: str, marker: str) -> str:
    """The inner markup of the ``assessment-card`` ``<div>`` that CONTAINS
    marker — bounded by that div's own matching close tag, found by walking
    tag depth, not by any sentinel that happens to follow it.

    Two prior forms of this helper both over-returned instead of failing:

    * Until 2026-09-09 this split on ``"</tr>"``, a leftover from the table
      layout. The card list has no ``</tr>`` at all, so ``str.split`` on an
      absent separator returned the WHOLE REMAINING PAGE, and every caller
      silently became page-scoped rather than row-scoped without a single
      test failing.
    * The 2026-09-09 fix for that bounded on the NEXT ``class="assessment-card
      "`` occurrence instead — which does not exist when the marked row is
      the LAST card on a page, so the same over-return happened again, just
      narrower: only the last card leaked into whatever page chrome follows
      it (footer, scripts). Two of the three real call sites hit this exact
      case (fix round 1, 2026-09-09) and none of them failed, only because
      the trailing markup happened not to contain any asserted string.

    A sentinel is the wrong shape of fix twice in a row for the same reason:
    it assumes something about what comes AFTER the card. This version
    instead locates the last ``assessment-card`` opening tag before the
    marker, then walks ``<div`` / ``</div`` depth from there to that div's own
    matching close — bounded by the card's own structure, so it cannot leak
    into anything that follows regardless of what that happens to be.

    ``html.find`` below picks the FIRST occurrence of ``marker`` — silently,
    for a duplicate marker, the same shape of bug the two forms above were
    retired for: a caller asserting against the wrong card's slice without
    any failure naming why. The count check below closes that.
    """
    assert html.count(marker) == 1, (
        f"marker {marker!r} appears {html.count(marker)} times in html; "
        "_row_slice would silently scope to the FIRST occurrence"
    )
    marker_pos = html.find(marker)
    if marker_pos == -1:
        raise AssertionError(f"marker {marker!r} not found in html")

    card_class_pos = html.rfind(_CARD_OPEN, 0, marker_pos)
    if card_class_pos == -1:
        raise AssertionError(
            f"no {_CARD_OPEN!r} div found before marker {marker!r}"
        )
    div_start = html.rfind("<div", 0, card_class_pos)
    tag_end = html.find(">", card_class_pos)
    if div_start == -1 or tag_end == -1:
        raise AssertionError(f"malformed assessment-card div near marker {marker!r}")
    pos = tag_end + 1

    depth = 1  # the card's own <div>, already open
    end = None
    for tag in re.finditer(r"</div>|<div\b", html[pos:]):
        depth += 1 if tag.group() == "<div" else -1
        if depth == 0:
            end = pos + tag.start()
            break
    if end is None:
        raise AssertionError(f"unbalanced assessment-card div for marker {marker!r}")

    if not (pos <= marker_pos < end):
        raise AssertionError(
            f"marker {marker!r} (at {marker_pos}) is not inside the "
            f"assessment-card div that precedes it (card spans [{pos}, {end}))"
        )
    return html[pos:end]


def test_row_slice_stops_at_the_next_card():
    """Guard on the guard. `_row_slice` is what makes four column assertions
    ROW-scoped. If its boundary string stops matching the markup it does not
    fail — it returns the whole page, and those four stop testing anything."""
    html = (
        '<div class="assessment-card p-5">A-MARKER Alice</div>'
        '<div class="assessment-card p-5">B-MARKER Bob</div>'
    )
    sliced = _row_slice(html, "A-MARKER")
    assert "Alice" in sliced
    assert "Bob" not in sliced


def test_row_slice_stops_at_the_end_of_the_last_card():
    """Second guard case, added in fix round 1 (2026-09-09). The 2026-09-09
    `</tr>`-fix bounded on the NEXT `class="assessment-card ` occurrence —
    which does not exist when the marked row is the LAST card on the page, so
    `str.split` on an absent separator again returned everything from the
    marker to end-of-document (footer, scripts, all of it). Same failure
    mode as the original `</tr>` bug, just narrower: it only bit the
    last-card case. Two real call sites hit it (`test_list_pages_show_
    reviewer_columns`'s "Untouched Co", which sorts last by score, and
    `test_reviewer_role.py`'s single-assessment fixture, which is trivially
    last) — nothing failed only because the page's own footer happens not to
    contain any of the strings those tests assert against."""
    html = (
        '<div class="assessment-card p-5">A-MARKER Alice</div>'
        '<footer>Approved</footer>'
    )
    sliced = _row_slice(html, "A-MARKER")
    assert "Alice" in sliced
    assert "Approved" not in sliced


@pytest.mark.parametrize(
    ("base", "role"),
    [("/admin", USER_ROLE_ADMIN), ("/manager", USER_ROLE_MANAGER)],
)
async def test_list_pages_show_reviewer_columns(client, db_session, base, role):
    staff = await factories.make_user(
        db_session, user_role=role, email=f"review-cols{base.strip('/')}@example.org"
    )
    run = await factories.make_simulation_run(db_session)
    await _seed_reviewed_row(db_session, run, project="Reviewed Co")
    untouched = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id="gordy", channel_name="general",
        company_or_project="Untouched Co", recommendation="pass",
        weighted_score=1.0, band="pass",
    )
    db_session.add(untouched)
    await db_session.flush()

    # `&review=all`: the default tab is Unreviewed (2026-09-21) and this
    # fixture carries feedback, so it lives on the Reviewed tab now.
    html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}&review=all",
            headers=auth_headers(staff.id),
        )
    ).text

    assert "Reviewed Co" in html
    assert "Assigned" in html and "Reviewed by" in html

    reviewed_row = _row_slice(html, "Reviewed Co")
    assert "Alice A" in reviewed_row
    assert "Bob B, Cara C, Dana D" in reviewed_row
    assert "Disapproved" in reviewed_row

    # An untouched row gets neither names nor a chip.
    untouched_row = _row_slice(html, "Untouched Co")
    assert "Alice A" not in untouched_row
    assert "Bob B" not in untouched_row
    assert "Approved" not in untouched_row
    assert "Disapproved" not in untouched_row
    assert untouched_row.count("—") >= 2, "the two new columns must render — when untouched"


async def test_review_columns_for_empty_ids_hits_the_db_zero_times():
    """The early return IS the contract: a db stub that raises on any
    `execute` call proves nothing was queried, since there is no
    before_cursor_execute listener in this suite to count against instead."""

    class _ExplodingDB:
        async def execute(self, *args, **kwargs):
            raise AssertionError("no query should run for an empty id list")

    assert await review_columns_for(_ExplodingDB(), []) == {}


async def test_review_columns_parity_with_single_id_calls(db_session):
    """5 seeded assessments, each touched by at least one review row (so it
    is a key in both the batched and the single-id result) — the batched
    call must equal the union of calling review_columns_for one id at a
    time."""
    run = await factories.make_simulation_run(db_session)
    assessments = []
    for i in range(5):
        a = OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wang", channel_name="general",
            company_or_project=f"Parity Co {i}",
        )
        db_session.add(a)
        await db_session.flush()
        assessments.append(a)

    base_t = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    for i, a in enumerate(assessments):
        reviewer = await factories.make_user(db_session, name=f"Parity Reviewer {i}")
        db_session.add(AssessmentReview(
            assessment_id=a.id, reviewer_user_id=reviewer.id, reviewer_name=reviewer.name,
            score=3, comment="note", feedback_mode="log_only",
            created_at=base_t + timedelta(minutes=i),
        ))
        actor = await factories.make_user(db_session, name=f"Parity Actor {i}")
        db_session.add(AssessmentReviewEvent(
            assessment_id=a.id,
            action="approved" if i % 2 == 0 else "disapproved",
            actor_user_id=actor.id, actor_name=actor.name,
            created_at=base_t + timedelta(minutes=i, seconds=30),
        ))
    await db_session.flush()

    ids = [a.id for a in assessments]
    batched = await review_columns_for(db_session, ids)
    singles = {
        a.id: (await review_columns_for(db_session, [a.id]))[a.id] for a in assessments
    }

    assert batched == singles
    assert set(batched) == set(ids)


async def test_no_detail_prose_in_new_columns(client, db_session, admin):
    """The new columns show names and a chip, never comment text — the same
    scan-only discipline test_admin_assessments_page_renders_no_inline_detail_rows
    (tests/integration/test_opportunity_assessment_persistence.py) pins for
    rationale/red-flags/milestones."""
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id="wang", channel_name="general",
        company_or_project="Prose Guard Co",
    )
    db_session.add(assessment)
    await db_session.flush()
    reviewer = await factories.make_user(db_session, name="Prose Reviewer")
    db_session.add(AssessmentReview(
        assessment_id=assessment.id, reviewer_user_id=reviewer.id,
        reviewer_name="Prose Reviewer", score=5,
        comment="TOP-SECRET-REVIEW-COMMENT-TEXT", feedback_mode="learn",
    ))
    await db_session.flush()

    # `&review=all`: this fixture carries feedback, so since 2026-09-21 it sits
    # on the Reviewed tab rather than the default Unreviewed one.
    html = (
        await client.get(
            f"/admin/assessments?run_id={run.id}&review=all",
            headers=auth_headers(admin.id),
        )
    ).text

    assert "Prose Reviewer" in html
    assert "TOP-SECRET-REVIEW-COMMENT-TEXT" not in html


async def test_the_card_leads_with_the_headline_and_keeps_the_short_label(
    client, db_session, admin
):
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general",
        company_or_project="SHORT-LABEL-MARKER",
        headline="HEADLINE-MARKER: a blood test for immunotherapy response.",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "assessment-card-headline" in html
    assert "HEADLINE-MARKER" in html
    assert "SHORT-LABEL-MARKER" in html


async def test_a_row_with_no_headline_falls_back_to_the_short_label(
    client, db_session, admin
):
    """A3. All 12 rows in production today have headline IS NULL and are never
    backfilled, so the fallback is the COMMON case, not an edge one. It must
    render exactly what today's page renders, with no empty-state artefact."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="ONLY-LABEL-MARKER",
        headline=None, key_points=None,
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "ONLY-LABEL-MARKER" in html
    # No bullet block, and no subtitle line — the fallback shows the label
    # ONCE, as the heading, exactly as the old Project cell did.
    assert "assessment-card-points" not in html
    assert "assessment-card-label" not in html


async def test_the_card_keeps_gating_panel_flags_and_rubric_on_its_face(
    client, db_session, admin
):
    """N9. Dropping the panel badge in particular would be a real loss:
    rendering a non-verified panel as unremarkable is a named failure mode.

    UPDATED 2026-09-14 (Task C2, D4): the gating string is still ON THE PAGE
    but no longer on the card FACE — it moved, verbatim, into each card's
    collapsed "Rubric scores & gating" disclosure, at operator request. This
    test is deliberately page-scoped and so still passes unchanged; the
    sibling test below is what pins WHERE the string now lives, and the flag
    count, panel badge and rubric stamp asserted here are all still face
    content.
    """
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Face check",
        gating={"life_sciences_domain": "met", "credible_science": "unconfirmed"},
        red_flags=["one", "two"],
        rubric_version="3.4.0",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "life sciences domain" in html
    assert "2 flags" in html
    assert "3.4.0" in html
    assert "panel not recorded" in html   # panel_owed IS NULL -> 'unrecorded'


async def test_the_manager_surface_renders_the_same_cards(client, db_session):
    from src.models import USER_ROLE_MANAGER

    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Manager card",
        headline="MANAGER-HEADLINE-MARKER",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/manager/assessments?run_id={run.id}", headers=auth_headers(manager.id)
    )).text
    assert "assessment-card" in html
    assert "MANAGER-HEADLINE-MARKER" in html


# ---------------------------------------------------------------------------
# Task C (2026-09-14): the card's two collapsed disclosures — "Rubric scores &
# gating" (C2) and "Quick scoring" (C3) — and the two-column narrative (C1).
# ---------------------------------------------------------------------------


def _details_open_tag(html: str, klass: str) -> str:
    """The literal ``<details ...>`` open tag carrying `klass`.

    Found by walking BACK to the `<details` that owns the class attribute, not
    by slicing a fixed number of characters before it: the preceding markup
    contains its own ``>`` characters, so a fixed-width window would be cut
    short and the "no `open` attribute" assertion would pass vacuously.
    """
    at = html.index(f'class="{klass} ')
    start = html.rfind("<details", 0, at)
    assert start != -1, f"{klass} is not on a <details> element"
    return html[start : html.index(">", at) + 1]


def _details_slice(html: str, klass: str) -> str:
    """The inner markup of the FIRST ``<details class="<klass> ...">`` on the
    page, bounded by its own ``</details>``.

    Bounded on the element's own close tag rather than on a sentinel, for the
    same reason ``_row_slice`` walks div depth: a sentinel assumes something
    about what follows. Neither disclosure nests a second ``<details>``, so a
    depth walk buys nothing here — but if one ever does, this helper must
    grow one.
    """
    assert f'class="{klass} ' in html, f"no <details class={klass!r} ...> on the page"
    start = html.index(f'class="{klass} ')
    assert "<details" in html[:start], f"{klass} is not on a <details> element"
    open_tag_end = html.index(">", start)
    end = html.index("</details>", open_tag_end)
    assert "<details" not in html[open_tag_end:end], (
        f"{klass} now nests another <details>; this helper needs a depth walk"
    )
    return html[open_tag_end + 1 : end]


async def _seed_narrative_row(db_session, *, project: str, **overrides):
    """One row with every narrative field the card can render."""
    run = overrides.pop("run", None) or await factories.make_simulation_run(db_session)
    kwargs = dict(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project=project,
        recommendation="conditional", weighted_score=3.05, band="conditional",
    )
    kwargs.update(overrides)
    assessment = OpportunityAssessment(**kwargs)
    db_session.add(assessment)
    await db_session.flush()
    return run, assessment


async def test_quick_scoring_is_expanded_and_labelled(client, db_session, admin):
    """OPEN by default. The 2026-09-09 card list made this disclosure closed on
    the grounds that a permanently-expanded form per card is what made the old
    table unreadable; the operator reversed that call on 2026-09-21 — the
    review form is the point of the queue. Still a <details>, so a reader can
    collapse it and the print handler still works on it."""
    run, _ = await _seed_narrative_row(db_session, project="Quickscore Co")

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert "assessment-card-quickscore" in html
    assert "Quick scoring" in html
    assert " open" in _details_open_tag(html, "assessment-card-quickscore"), (
        "the quick-scoring disclosure must start open"
    )


@pytest.mark.parametrize(
    ("base", "role", "surface"),
    [
        ("/admin", USER_ROLE_ADMIN, "admin-list"),
        ("/manager", USER_ROLE_MANAGER, "manager-list"),
    ],
)
async def test_quick_scoring_posts_to_literal_review_paths_on_both_surfaces(
    client, db_session, base, role, surface
):
    """The action paths are LITERAL `/reviews/...` on both surfaces (the header
    comment's recorded exception): that router is not split by surface, and the
    `surface` hidden input — a bare token, contract C5/C6 — is what
    `_assessments_redirect` uses to send the writer back to the list they were
    reading. A path-shaped discriminator would put `/admin/` on the manager
    page and fail test_manager_assessments_never_links_into_admin.
    """
    staff = await factories.make_user(
        db_session, user_role=role, email=f"qs{base.strip('/')}@example.org"
    )
    run, assessment = await _seed_narrative_row(db_session, project="Paths Co")

    html = (await client.get(
        f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
    )).text

    assert f'action="/reviews/assessments/{assessment.id}/feedback"' in html
    assert f'action="/reviews/assessments/{assessment.id}/status"' not in html
    assert 'method="post"' in html
    # ONE form now (the status form was removed 2026-09-21 on operator
    # request); the four hidden inputs appear once each, not twice.
    quickscore = _details_slice(html, "assessment-card-quickscore")
    assert quickscore.count(f'name="surface" value="{surface}"') == 1
    assert quickscore.count('name="run_id"') == 1
    assert quickscore.count('name="sort"') == 1
    assert quickscore.count('name="lab"') == 1
    # The FIFTH input (2026-09-21). Counted inside the disclosure slice on
    # purpose: the wrapper's own GET form emits a byte-identical
    # `name="review"` input, so a page-wide assertion would pass with this
    # one deleted and a reviewer scoring from the Reviewed tab would be
    # silently returned to Unreviewed.
    assert quickscore.count('name="review"') == 1
    # No status control of any kind survives on the card.
    assert 'name="action"' not in quickscore
    assert "Approved" not in quickscore and "Disapproved" not in quickscore


async def test_quick_scoring_offers_no_rubric_dimension_fields(
    client, db_session, admin
):
    """Per-dimension scoring stays on the detail page. Posting no `dim_*` field
    at all is the already-supported path (`_parse_dimension_scores` -> `{}` ->
    SQL NULL), so this needs no route change — but a `dim_*` field appearing
    here later would silently change what a quick score records."""
    run, _ = await _seed_narrative_row(db_session, project="No Dims Co")

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert 'name="dim_' not in html


async def test_quick_scoring_offers_no_edit_delete_or_assign_controls(
    client, db_session, admin
):
    """Edit is excluded because `edit_feedback` REPLACES `dimension_scores`
    from the posted form: a dimension-free edit posted from a card would wipe
    scores a reviewer entered on the detail page. Delete is admin-only and
    assign/unassign is staff-only, and neither belongs on a card a reviewer
    also sees."""
    run, _ = await _seed_narrative_row(db_session, project="No Edit Co")

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert "/reviews/feedback/" not in html
    assert "/assign" not in html
    assert "/unassign" not in html


async def test_quick_scoring_select_ids_are_unique_and_labelled(
    client, db_session, admin
):
    """The list-page twin of the detail page's labelling gate. PAGE-WIDE, which
    is only possible because Task D gave the three filter selects an `id` and
    their labels a `for` — before that the page violated the rule on
    pre-existing markup. Two seeded rows, so a per-row id collision (an id
    built from anything but `a.id`) fails here rather than in a screen
    reader."""
    run, _ = await _seed_narrative_row(db_session, project="Unique Ids One")
    await _seed_narrative_row(db_session, project="Unique Ids Two", run=run)

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    select_ids = re.findall(r'<select[^>]*\bid="([^"]+)"', html)
    assert len(select_ids) == len(set(select_ids)), (
        f"duplicate select ids: {select_ids}"
    )
    assert len(re.findall(r"<select\b", html)) == len(select_ids), (
        "every <select> on the page needs an id (Task D fixes the filter row)"
    )
    for sid in select_ids:
        assert f'for="{sid}"' in html, f"no <label for={sid!r}>"


async def test_quick_scoring_renders_for_a_reviewer_on_the_manager_surface(
    client, db_session
):
    """A reviewer's whole surface is the manager list plus /reviews, so the
    quick-scoring form has to be there — it is the only write they can reach
    from the queue."""
    reviewer = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, email="qs-reviewer@example.org"
    )
    run, assessment = await _seed_narrative_row(db_session, project="Reviewer Co")

    html = (await client.get(
        f"/manager/assessments?run_id={run.id}", headers=auth_headers(reviewer.id)
    )).text

    assert "assessment-card-quickscore" in html
    assert f'action="/reviews/assessments/{assessment.id}/feedback"' in html
    assert 'value="manager-list"' in html
    assert "/admin/" not in html


async def test_the_card_scores_disclosure_is_closed_by_default_and_holds_the_gating(
    client, db_session, admin
):
    """C2/D4. The gating glyphs moved off the card face into this disclosure,
    VERBATIM — same three glyphs, same titles, same `gating-row gating-*`
    classes. The sibling of
    test_the_card_keeps_gating_panel_flags_and_rubric_on_its_face, which is
    page-scoped and therefore cannot tell the two placements apart."""
    run, _ = await _seed_narrative_row(
        db_session, project="Scores Disclosure Co",
        gating={"life_sciences_domain": "met", "credible_science": "unconfirmed"},
        scores={"differentiation_unmet_need": 4},
        rubric_version="3.4.0",
    )

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert "assessment-card-scores" in html
    assert " open" not in _details_open_tag(html, "assessment-card-scores"), (
        "the scores disclosure must start closed"
    )

    scores = _details_slice(html, "assessment-card-scores")
    assert "Rubric scores" in html
    assert "life sciences domain" in scores
    assert "credible science" in scores
    assert "gating-row gating-met" in scores
    assert "gating-row gating-unconfirmed" in scores
    assert "&#9989;" in scores and "&#10067;" in scores
    # The face keeps what the face kept (finding R3) — these are OUTSIDE it.
    assert "panel not recorded" not in scores
    assert "Reviewed by" not in scores


async def test_the_card_scores_use_the_rows_own_revision(client, db_session, admin):
    """Contract C4: the rows are built from the ROW'S OWN stamped revision, not
    from today's document. A 1.0.0 row's `differentiation` is
    "Commercialization potential / differentiation" at 15% weight; the live
    3.4.0 document has no such key and names nothing that way. Re-deriving
    against the live rubric is the failure this pins — the same class of bug
    `panel_state` had before `panel_owed` became a stored column."""
    run, _ = await _seed_narrative_row(
        db_session, project="Own Revision Co",
        scores={"differentiation": 4},
        rubric_version="1.0.0",
    )

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    scores = _details_slice(html, "assessment-card-scores")
    assert "Commercialization potential / differentiation" in scores
    assert "15%" in scores
    assert "Differentiation &amp; unmet need" not in scores


async def test_the_list_page_renders_the_pitch_and_key_points_side_by_side(
    client, db_session, admin
):
    """C1: pitch LEFT, key points RIGHT, mirroring the detail page's brief so
    the two surfaces cannot order the same two fields two ways."""
    run, _ = await _seed_narrative_row(
        db_session, project="Side By Side Co",
        elevator_pitch="PITCH-MARKER: one tube of blood, two-week answer.",
        key_points={"significance": ["POINTS-MARKER: decision-window fit"]},
    )

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert "assessment-card-pitch" in html
    assert "assessment-card-points" in html
    assert html.index("assessment-card-pitch") < html.index("assessment-card-points")
    assert "PITCH-MARKER" in html
    assert "POINTS-MARKER" in html


async def test_a_row_with_no_pitch_or_points_renders_neither_box(
    client, db_session, admin
):
    """Every box is FULLY conditional. Every production row today has
    `elevator_pitch IS NULL` and `key_points IS NULL`, so an unconditional
    grid wrapper or tinted box fails on the entire current corpus, not on an
    edge case. The mapping-of-empty-lists case is the list-page twin of
    test_a_mapping_of_only_empty_lists_renders_no_key_points_column: it is a
    storable shape (`normalize_key_points`) and must render no box, which is
    why the gate is a CONTENT check and not `is mapping`."""
    run, _ = await _seed_narrative_row(
        db_session, project="Bare Card Co", elevator_pitch=None, key_points=None,
    )
    await _seed_narrative_row(
        db_session, project="Empty Points Co", run=run,
        elevator_pitch=None,
        key_points={"significance": [], "innovation": []},
    )

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert "Bare Card Co" in html
    assert "Empty Points Co" in html
    # ROW-scoped, not page-wide: the wrapper's own stylesheet carries a
    # `.assessment-card-pitch .assessment-prose { max-width: none; }` rule
    # (templates/admin/assessments.html), so the class NAME is on every render
    # whether or not any card emits the box. The claim being tested is about
    # the card's markup, so it is asserted inside the card.
    for marker in ("Bare Card Co", "Empty Points Co"):
        card = _row_slice(html, marker)
        assert "assessment-card-pitch" not in card, marker
        assert "assessment-card-points" not in card, marker
        # The score-rationale box is UNCONDITIONAL as of 2026-09-21 (D9): it
        # now carries the score itself, so a row that omitted it would show no
        # score anywhere.
        assert "assessment-card-score-rationale" in card, marker


async def test_the_list_page_stays_under_a_size_ceiling(client, db_session, admin):
    """Finding Q6. Everything Task C adds to a card — the pitch, the key-point
    groups, the score rationale, six dimension bars and two disclosures with a
    full review form in one of them — is per ROW, and the page renders up to
    `assessments_limit` rows. A ceiling makes the next per-card addition
    declare its cost instead of discovering it on a 500-row run.

    MEASURED 2026-09-14 against the REAL served page (not a standalone render
    of the body), with this exact fixture shape — 50 rows, ~560-char pitch,
    five key-point groups x 2 bullets, ~310-char score rationale, six
    dimension rows:

    | what | bytes | per card |
    |---|---|---|
    | 50 rows, every narrative field populated | **728,695** | 14.2 KiB |
    | 50 rows, no narrative fields at all (today's production shape) | 503,955 | 9.8 KiB |
    | 500 rows (`ASSESSMENTS_LIMIT`, the "All Runs" worst case), populated | 7,130,306 | 13.9 KiB |
    | 500 rows, no narrative fields | 4,882,416 | 9.5 KiB |

    For scale: before this change a card was ~3.0 KiB, so the 500-row worst
    case was ~1.5 MB. The first cut of this markup measured 8,669,804 bytes at
    500 rows; whitespace control in the two disclosures (see the comment on the
    dimension-row loop in templates/admin/_assessments_body.html) took it to
    the 7,130,306 above with byte-identical rendered output. The DEFAULT view is
    run-scoped and holds 6-20 rows, i.e. ~280 KB; the multi-megabyte figure is
    reachable only via "All Runs".

    RE-MEASURED 2026-09-21, same fixture, after the queue change (score+band
    folded into an unconditional collapsed box, quick review `open`, the
    detail link moved into the pitch box, the citation rewrite, the review
    tab strip):

    | what | bytes | per card |
    |---|---|---|
    | 50 rows, every narrative field populated | **685,309** | 13.4 KiB |

    That is 43,386 bytes BELOW the 2026-09-14 figure, not above it: the
    top-right score block was deleted outright, and this fixture populates
    `score_rationale` on every row, so making its box unconditional costs
    nothing here. A run of rows with `score_rationale IS NULL` pays ~180
    bytes each for the box's "no score rationale" line instead — still less
    than the block that was removed.

    CEILING is the populated 50-row measurement plus ~20%. If a change pushes
    past it, raise it in the same commit to the newly MEASURED number and
    record the measurement here — no speculative headroom. It is deliberately
    NOT lowered to match a shrink: the ceiling exists to make the next
    addition declare its cost, and re-baselining downward every time would
    turn it into a tripwire that fires on noise.
    """
    CEILING = 875_000

    run = await factories.make_simulation_run(db_session)
    pitch = (
        "A blood test that reports checkpoint-inhibitor response from one tube "
        "of blood, inside the two-week decision window an oncologist has. "
    ) * 4
    why = "Scored on the retrospective cohort's strength and the IP position's weakness. " * 4
    points = {
        key: [f"{key} point one for triage", f"{key} point two for triage"]
        for key in (
            "significance", "innovation", "clinical_actionability",
            "key_questions", "commercial_potential",
        )
    }
    for i in range(50):
        db_session.add(OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wang", channel_name="general",
            company_or_project=f"Weight Co {i}",
            headline=f"Headline {i}: a blood test for immunotherapy response",
            recommendation="conditional", weighted_score=3.05, band="conditional",
            confidence="High", prose_format="markdown",
            elevator_pitch=pitch, score_rationale=why, key_points=points,
            gating={
                "life_sciences_domain": "met",
                "credible_science": "unconfirmed",
                "translational_potential": "met",
            },
            red_flags=["flag one", "flag two"],
            scores={
                "differentiation_unmet_need": 4, "scientific_credibility": 4,
                "translational_path": 3, "fundable_experiment": 3,
                "venture_potential": 2, "team_executability": 4,
            },
            rubric_version="3.4.0",
        ))
    await db_session.flush()

    resp = await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    assert "Weight Co 0" in resp.text
    assert len(resp.text) < CEILING, (
        f"the 50-row admin list page is {len(resp.text)} bytes, over the "
        f"{CEILING}-byte ceiling — re-measure and raise it deliberately, or "
        "move something off the card"
    )


# --- cited-paper links (Task 2 / spec §7) -----------------------------------

CITED_DOI = "https://doi.org/10.7554/eLife.94488"


async def test_the_card_pitch_renders_a_doi_as_a_cited_paper_link(
    client, db_session, admin
):
    """Spec §7. A markdown row's pitch reaches the browser as `data-markdown`,
    so the assertion is on the MARKDOWN we hand the client, not on an anchor:
    marked builds the anchor at render time."""
    run, _ = await _seed_narrative_row(
        db_session,
        project="Cited Co",
        prose_format="markdown",
        elevator_pitch=f"Built on the lab's eLife 2025 paper ({CITED_DOI}), which reports.",
    )

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Cited Co")

    assert "cited paper" in card
    assert f"&lt;{CITED_DOI}&gt;" in card, "destination must be angle-bracketed"
    assert f">{CITED_DOI}<" not in card, "the raw URL must not be visible text"


async def test_a_plain_text_card_pitch_renders_a_real_anchor(
    client, db_session, admin
):
    """A `prose_format IS NULL` row never reaches marked, so the anchor has to
    be built server-side."""
    run, _ = await _seed_narrative_row(
        db_session,
        project="Plain Cited Co",
        prose_format=None,
        elevator_pitch=f"See {CITED_DOI} for the paper.",
    )

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Plain Cited Co")

    assert f'<a class="citation-link" href="{CITED_DOI}"' in card
    assert ">cited paper</a>" in card
    assert "target=" not in card.split("citation-link")[1][:200]


async def test_the_manager_surface_renders_citation_links_too(client, db_session):
    """Both routers own their own Jinja2Templates instance and their own
    globals; registering the helpers on one leaves the other raising
    UndefinedError. This is the test that catches a single registration."""
    manager = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="cite-mgr@example.org"
    )
    run, _ = await _seed_narrative_row(
        db_session,
        project="Manager Cited Co",
        prose_format=None,
        elevator_pitch=f"See {CITED_DOI} for the paper.",
    )

    resp = await client.get(
        f"/manager/assessments?run_id={run.id}", headers=auth_headers(manager.id)
    )
    assert resp.status_code == 200
    assert ">cited paper</a>" in _row_slice(resp.text, "Manager Cited Co")


# --- the detail control (Task 6 / spec §6) ----------------------------------


async def test_the_detail_button_sits_in_the_pitch_box(client, db_session, admin):
    """Spec §6/D7. One control per card, at the foot of "In one minute".

    The fixture carries key points as well as a pitch so the slice below has a
    right-hand box to stop at; with a pitch alone the grid renders one column
    and there is no `assessment-card-points` marker to bound on.
    """
    run, _ = await _seed_narrative_row(
        db_session, project="Pitch Button Co",
        elevator_pitch="One minute of prose about the idea.",
        key_points=["a point the reviewer can read"],
    )
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Pitch Button Co")

    assert card.count("assessment-open-link") == 1
    pitch_box = card[card.index("assessment-card-pitch") :]
    pitch_box = pitch_box[: pitch_box.index("assessment-card-points")]
    assert "assessment-open-link" in pitch_box


async def test_a_card_with_no_pitch_still_has_exactly_one_detail_button(
    client, db_session, admin
):
    """12 of 22 production rows have no pitch, so the fallback is the common
    case, not an edge one."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="No Pitch Button Co",
        recommendation="pass", weighted_score=1.2, band="pass",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "No Pitch Button Co")

    assert card.count("assessment-open-link") == 1
    assert "/admin/assessments/" in card


# --- score and band, one click down (Task 7 / spec §8) ----------------------


async def test_the_score_and_band_live_inside_the_collapsed_why_this_score_box(
    client, db_session, admin
):
    """Spec §8/D8. The card face keeps the recommendation chip only."""
    run, _ = await _seed_narrative_row(
        db_session, project="Score Box Co", score_rationale="Because of the cohort.",
    )
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Score Box Co")

    assert " open" not in _details_open_tag(html, "assessment-card-score-rationale")
    box = _details_slice(html, "assessment-card-score-rationale")
    assert "3.05" in box
    assert "band-label" in box
    # Nothing outside the box carries the number or the band word.
    face = card[: card.index("assessment-card-score-rationale")]
    assert "3.05" not in face
    assert "band-label" not in face
    assert "conditional" in face, "the recommendation chip stays on the face"
    # ...and the markup AFTER the box too: checking only what precedes it
    # would miss a score re-added to the footer row or either disclosure.
    outside = card.replace(_details_slice(html, "assessment-card-score-rationale"), "")
    assert outside.count("3.05") == 0
    assert outside.count("band-label") == 0


async def test_a_row_with_no_score_rationale_still_shows_its_score_in_the_box(
    client, db_session, admin
):
    """D9: 12 of 22 production rows are pre-0048 and have score_rationale NULL.
    The box is unconditional so none of them loses its score."""
    run, _ = await _seed_narrative_row(
        db_session, project="No Rationale Co", score_rationale=None,
    )
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    box = _details_slice(html, "assessment-card-score-rationale")
    assert "3.05" in box
    assert "no score rationale" in box.lower()


async def test_a_row_with_no_weighted_score_renders_the_em_dash_in_the_box(
    client, db_session, admin
):
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="No Score Co",
        recommendation="pass",
    ))
    await db_session.flush()
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    box = _details_slice(html, "assessment-card-score-rationale")
    assert "&mdash;" in box or "—" in box


# --- the review sub-tabs (Task 8 / spec §4) ---------------------------------


@pytest.mark.parametrize(
    ("base", "role"),
    [("/admin", USER_ROLE_ADMIN), ("/manager", USER_ROLE_MANAGER)],
)
async def test_both_surfaces_offer_the_review_tabs(client, db_session, base, role):
    staff = await factories.make_user(
        db_session, user_role=role, email=f"tabs-{role}@example.org"
    )
    run, _ = await _seed_narrative_row(db_session, project="Tab Co")

    html = (await client.get(
        f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
    )).text

    assert f'href="{base}/assessments?' in html
    assert "review=reviewed" in html and "review=unreviewed" in html and "review=all" in html
    assert 'aria-current="page"' in html


async def test_the_review_tab_survives_a_sort_change(client, db_session, admin):
    """The run/sort/lab controls are ONE GET form with no hidden inputs, so a
    param that is not a form field is dropped the moment a select changes."""
    run, _ = await _seed_narrative_row(db_session, project="Sticky Tab Co")
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}&review=all", headers=auth_headers(admin.id)
    )).text
    assert '<input type="hidden" name="review" value="all">' in html


async def test_the_empty_state_names_the_tab_not_the_run(client, db_session, admin):
    """A run whose only row is reviewed, seen from the Unreviewed tab: "no
    assessments stored for this run" is false, and the run menu on the same
    screen says so."""
    run, assessment = await _seed_narrative_row(db_session, project="All Reviewed Co")
    reviewer = await factories.make_user(db_session, email="empty-state-rev@example.org")
    db_session.add(AssessmentReview(
        assessment_id=assessment.id, reviewer_user_id=reviewer.id,
        reviewer_name="R", score=4, comment="ok", feedback_mode="log_only",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}&review=unreviewed",
        headers=auth_headers(admin.id),
    )).text
    assert "No assessments stored for" not in html
    # The NEW copy, not just the word "unreviewed" — the tab strip prints
    # that on every render, so the old assertion could not fail.
    assert "Nothing is owed here" in html


async def test_the_admin_surface_forwards_the_review_keys(client, db_session, admin):
    """admin_assessments allowlists every context key; a key added to the
    service and not to that list renders as silently-falsy Undefined here and
    nowhere else."""
    run, _ = await _seed_narrative_row(db_session, project="Allowlist Co")
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "review=all" in html  # the All tab link renders its count
    assert "(1)" in html or "(0)" in html


# ---------------------------------------------------------------------------
# 2026-09-21 audit: acceptance criteria the first pass left unproven.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "role"), [("/admin", USER_ROLE_ADMIN), ("/manager", USER_ROLE_MANAGER)]
)
async def test_each_review_tab_renders_only_its_own_rows(
    client, db_session, base, role
):
    """Spec §4 acceptance 1, at the HTTP level and on BOTH surfaces. The
    service test proves the SQL; this proves the parameter is declared on both
    handlers and reaches the query — the `reviewed` branch had no surface
    coverage at all."""
    staff = await factories.make_user(
        db_session, user_role=role, email=f"tabs-rows-{role}@example.org"
    )
    run, reviewed = await _seed_narrative_row(db_session, project="Has Feedback Co")
    await _seed_narrative_row(db_session, project="Awaits Review Co", run=run)
    author = await factories.make_user(
        db_session, email=f"tab-author-{role}@example.org"
    )
    db_session.add(AssessmentReview(
        assessment_id=reviewed.id, reviewer_user_id=author.id,
        reviewer_name="Tab Author", score=4, comment="ok", feedback_mode="log_only",
    ))
    await db_session.flush()

    async def _get(review: str) -> str:
        return (await client.get(
            f"{base}/assessments?run_id={run.id}&review={review}",
            headers=auth_headers(staff.id),
        )).text

    reviewed_html = await _get("reviewed")
    assert "Has Feedback Co" in reviewed_html
    assert "Awaits Review Co" not in reviewed_html

    unreviewed_html = await _get("unreviewed")
    assert "Awaits Review Co" in unreviewed_html
    assert "Has Feedback Co" not in unreviewed_html

    all_html = await _get("all")
    assert "Has Feedback Co" in all_html and "Awaits Review Co" in all_html


async def test_a_run_with_no_assessments_at_all_says_so_on_the_default_tab(
    client, db_session, admin
):
    """The state of every fresh run and every new deployment, seen on the tab
    every reader lands on. Ordering the tab branches ahead of the
    nothing-at-all branch made it read "all 0 have review feedback. Nothing is
    owed here" — false, and reassuring about a run that has produced nothing.
    """
    run = await factories.make_simulation_run(db_session)
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "No assessments stored for" in html
    assert "Nothing is owed here" not in html


async def test_the_card_pitch_renders_two_dois_as_two_links(
    client, db_session, admin
):
    """The real production shape, proved through the template rather than
    only in the helper's unit tests."""
    run, _ = await _seed_narrative_row(
        db_session,
        project="Two Cites Co",
        prose_format="markdown",
        elevator_pitch=(
            f"Built on the eLife 2025 paper ({CITED_DOI}), with modelling on "
            "bioRxiv (https://doi.org/10.64898/2026.06.29.735215)."
        ),
    )
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Two Cites Co")
    assert card.count("cited paper") == 2
    assert f">{CITED_DOI}<" not in card


@pytest.mark.parametrize(
    ("base", "role"), [("/admin", USER_ROLE_ADMIN), ("/manager", USER_ROLE_MANAGER)]
)
async def test_quick_scoring_is_expanded_on_both_surfaces(
    client, db_session, base, role
):
    """The disclosure lives in the shared body, but only a both-surface
    assertion proves both wrappers render it open."""
    staff = await factories.make_user(
        db_session, user_role=role, email=f"qs-open-{role}@example.org"
    )
    run, _ = await _seed_narrative_row(db_session, project="Open Both Co")

    html = (await client.get(
        f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
    )).text
    assert " open" in _details_open_tag(html, "assessment-card-quickscore")


async def test_the_dimension_panel_names_the_tab_it_describes(
    client, db_session, admin
):
    """`dimension_stats` and `band_counts` are built from the post-filter row
    list, so one reviewer writing one comment moves a row out of the default
    tab and the band line silently drops it. Unmarked, that reads as a data
    change rather than a filter."""
    run, _ = await _seed_narrative_row(
        db_session, project="Panel Scope Co",
        scores={"venture_potential": 3, "differentiation_unmet_need": 4},
    )

    default_tab = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "(unreviewed only)" in default_tab

    all_tab = (await client.get(
        f"/admin/assessments?run_id={run.id}&review=all", headers=auth_headers(admin.id)
    )).text
    assert " only)" not in all_tab
