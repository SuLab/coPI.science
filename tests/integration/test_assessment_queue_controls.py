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

    html = (
        await client.get(
            f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
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

    html = (
        await client.get(
            f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
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
    rendering a non-verified panel as unremarkable is a named failure mode."""
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
