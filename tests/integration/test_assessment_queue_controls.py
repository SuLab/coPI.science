"""The triage queue's sort/lab controls, and the detail page's way out to the log.

The service tests (``tests/unit/test_directory_assessment_sorting.py``) prove the
SQL. These prove the wiring, which is a different failure: the query parameters
have to be declared on BOTH handlers, the chosen values have to reach BOTH
wrappers, and the three selects have to sit in ONE form — otherwise changing the
sort silently resets the run to "current", which on a `--fresh`-wiped instance
means the rows a reader was looking at vanish.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    OpportunityAssessment,
)
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
    assert "specialist panel" not in html
