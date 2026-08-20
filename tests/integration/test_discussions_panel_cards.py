"""The specialist panel, expanded, on the discussions pages.

The per-thread indicator (T6) says a panel happened. These tests are about the
case the indicator cannot cover and the assessment detail page cannot reach: an
interview that ended with NO assessment — a timeout, a decline, a conversation
that just stopped. There is no assessment row to open, so before this the
panel's work on those threads was invisible everywhere in the app, and the
expander did not even exist for them (it was gated on `t.decision`).

The manager assertions are the other half: a manager sees the panel's substance
and never the specialist's verbatim reply (Ruling R4). That is enforced in
``src/services/thread_panel.py`` — the value is not SELECTed for a manager
render — so the marker's absence below is not merely a template guard.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    SpecialistConsult,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

# Deliberately does NOT contain the substring "panel-card": the channel name is
# printed in the channel filter's <option> list on every render, so a name that
# collided with the card's own class would make
# `assert "panel-card" not in html` pass against a page that has cards on it.
CHANNEL = "panel-scope-thread"
HUB = "blackbird"
SUBJECT = "wang"

# Each appears exactly once in the seeded data, so a substring assertion cannot
# be satisfied by page chrome, the filter form or another thread's row.
QUESTION_MARKER = "PANEL-ASKED-ABOUT-COUNTER-SCREEN"
CONCERN_MARKER = "PANEL-CONCERN-NO-ISOGENIC-CONTROL"
ASK_MARKER = "PANEL-ASK-WHICH-CONTROL"
RAW_OPINION_MARKER = "PANEL-RAW-OPINION-VERBATIM"
LATER_QUESTION = "Is there a path to a development candidate?"

# `created_at` is set explicitly on both consults. Postgres' `now()` is
# transaction-scoped, so rows written in one test transaction share it to the
# microsecond — and then "oldest first" and "newest first" are the same order,
# which is exactly what the cap and the display-order assertions below have to
# tell apart.
CONSULT_EARLIER = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)
CONSULT_LATER = datetime(2026, 8, 20, 11, 30, tzinfo=UTC)


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="panel-admin@example.org"
    )


@pytest.fixture
async def manager(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="panel-manager@example.org"
    )


async def _thread_with_a_panel_and_no_decision(db_session):
    """A root post, two consults, and deliberately NO ThreadDecision: this is
    the shape the old template rendered with no expander at all."""
    run = await factories.make_simulation_run(db_session)
    root_ts = f"{time.time():.6f}"
    await factories.make_agent_message(
        db_session,
        run=run,
        agent_id=SUBJECT,
        channel_name=CHANNEL,
        message_ts=root_ts,
        thread_ts=None,
        phase="new_post",
        content="An interview that never reached a verdict.",
        posted_at=time.time(),
    )
    db_session.add(
        SpecialistConsult(
            simulation_run_id=run.id,
            agent_id=HUB,
            subject_agent_id=SUBJECT,
            thread_id=root_ts,
            channel_name=CHANNEL,
            domain="scientific",
            question=QUESTION_MARKER,
            verdict_signal="blocking",
            confidence="high",
            concerns=[CONCERN_MARKER],
            questions_to_ask=[ASK_MARKER],
            raw_opinion=RAW_OPINION_MARKER,
            created_at=CONSULT_EARLIER,
        )
    )
    db_session.add(
        SpecialistConsult(
            simulation_run_id=run.id,
            agent_id=HUB,
            subject_agent_id=SUBJECT,
            thread_id=root_ts,
            channel_name=CHANNEL,
            domain="chemistry",
            question=LATER_QUESTION,
            verdict_signal="clear",
            confidence="moderate",
            raw_opinion="a second opinion",
            created_at=CONSULT_LATER,
        )
    )
    await db_session.flush()
    return run, root_ts


async def test_admin_discussions_shows_the_panel_for_a_thread_with_no_decision(
    client, db_session, admin
):
    run, _ = await _thread_with_a_panel_and_no_decision(db_session)

    html = (
        await client.get(
            f"/admin/discussions?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text

    # The card itself, not just the compact indicator.
    assert 'class="panel-card' in html
    assert html.count('class="panel-card') == 2, "both consults get a card"
    assert QUESTION_MARKER in html
    assert CONCERN_MARKER in html
    assert ASK_MARKER in html
    assert "confidence: high" in html
    # Signal colours match the assessment detail page's cards.
    assert "bg-red-100 text-red-700" in html
    assert "bg-green-100 text-green-700" in html
    # The expander exists for a thread with no decision at all — this row used
    # to render with no detail <tr> and no cursor.
    assert 'id="detail-1"' in html
    assert "cursor-pointer" in html
    # Chronological within the thread, even though the query reads newest-first
    # so that the row cap sheds the OLDEST consults: "asked in this order" is
    # the whole reading of a two-card panel.
    assert html.index(QUESTION_MARKER) < html.index(LATER_QUESTION)
    # Nothing was capped, so no notice (control for the cap tests below).
    assert 'class="panel-truncated' not in html


async def test_admin_discussions_shows_the_verbatim_specialist_reply(
    client, db_session, admin
):
    run, _ = await _thread_with_a_panel_and_no_decision(db_session)
    html = (
        await client.get(
            f"/admin/discussions?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert RAW_OPINION_MARKER in html
    assert 'class="raw-opinion' in html


async def test_manager_discussions_shows_the_same_cards_without_the_raw_reply(
    client, db_session, manager
):
    """Seeded identically to the admin tests above — the ONLY difference between
    the two responses must be the verbatim reply."""
    run, _ = await _thread_with_a_panel_and_no_decision(db_session)

    html = (
        await client.get(
            f"/manager/discussions?run_id={run.id}", headers=auth_headers(manager.id)
        )
    ).text

    assert html.count('class="panel-card') == 2
    assert QUESTION_MARKER in html
    assert CONCERN_MARKER in html
    assert ASK_MARKER in html
    assert "confidence: high" in html

    assert RAW_OPINION_MARKER not in html, "Ruling R4: managers never see raw_opinion"
    assert "raw-opinion" not in html
    assert "Specialist's reply, verbatim" not in html
    assert "/admin/" not in html


async def test_a_thread_with_a_decision_keeps_its_summary_and_gains_the_panel(
    client, db_session, admin
):
    """The panel section is added to the existing detail row, not instead of it:
    a proposal summary and a panel must both be readable in one expansion."""
    run, root_ts = await _thread_with_a_panel_and_no_decision(db_session)
    await factories.make_thread_decision(
        db_session,
        run=run,
        thread_id=root_ts,
        channel=CHANNEL,
        agent_a=SUBJECT,
        agent_b=HUB,
        outcome="no_proposal",
        summary_text="A **decision** summary that must survive.",
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/discussions?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text

    assert 'data-markdown="A **decision** summary that must survive."' in html
    assert 'class="panel-card' in html
    assert QUESTION_MARKER in html


async def test_a_thread_with_no_consults_gets_no_panel_section(
    client, db_session, admin
):
    """Non-vacuity control for every assertion above, and the regression guard
    for the un-consulted majority of threads."""
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session,
        run=run,
        agent_id=SUBJECT,
        channel_name=CHANNEL,
        message_ts=f"{time.time():.6f}",
        thread_ts=None,
        phase="new_post",
        content="A thread nobody consulted about.",
        posted_at=time.time(),
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/discussions?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text

    assert f"#{CHANNEL}" in html
    assert 'class="panel-card' not in html
    assert "Specialist panel" not in html
    # No decision and no panel means nothing to expand — and therefore no
    # onclick pointing at an element that does not exist.
    assert 'id="detail-1"' not in html


async def test_the_panel_read_is_scoped_to_the_threads_on_the_page(
    client, db_session, admin
):
    """The consult query is keyed on the page's own thread ids. A filtered page
    must not carry the excluded threads' consults — for an admin that means
    their verbatim opinions in the page source."""
    run, _ = await _thread_with_a_panel_and_no_decision(db_session)
    other_ts = f"{time.time() + 10:.6f}"
    await factories.make_agent_message(
        db_session,
        run=run,
        agent_id=SUBJECT,
        channel_name="other-channel",
        message_ts=other_ts,
        thread_ts=None,
        phase="new_post",
        content="A different interview.",
        posted_at=time.time() + 10,
    )
    db_session.add(
        SpecialistConsult(
            simulation_run_id=run.id,
            agent_id=HUB,
            subject_agent_id="gordy",
            thread_id=other_ts,
            channel_name="other-channel",
            domain="legal",
            question="EXCLUDED-THREAD-QUESTION",
            verdict_signal="caution",
            confidence="low",
            raw_opinion="EXCLUDED-THREAD-RAW-OPINION",
        )
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/discussions?run_id={run.id}&channel_filter={CHANNEL}",
            headers=auth_headers(admin.id),
        )
    ).text

    assert QUESTION_MARKER in html
    assert "EXCLUDED-THREAD-QUESTION" not in html
    assert "EXCLUDED-THREAD-RAW-OPINION" not in html


async def test_the_row_cap_keeps_the_newest_consults_and_says_so(
    client, db_session, admin, monkeypatch
):
    """Two properties of the one bound on this page whose effect is invisible.

    Reading oldest-first under the cap dropped the NEWEST consults, which is
    backwards: the live interviews are the ones a reader is looking for, and
    since the compact per-row indicator feeds from this same query those rows
    lost their indicator too — rendering as "no specialist was ever consulted
    here", a false negative with nothing on the page to suggest it.

    So: the cap sheds the oldest, and hitting it is stated. The notice matters
    precisely because the cards it silences cannot be missed any other way.
    """
    from src.services import thread_panel

    monkeypatch.setattr(thread_panel, "CONSULT_ROW_LIMIT", 1)
    run, _ = await _thread_with_a_panel_and_no_decision(db_session)

    html = (
        await client.get(
            f"/admin/discussions?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text

    # The newest consult is the one that survives the cap.
    assert LATER_QUESTION in html
    assert QUESTION_MARKER not in html
    assert html.count('class="panel-card') == 1

    # ...and the page says it stopped reading, rather than looking complete.
    assert 'class="panel-truncated' in html
    assert "Specialist panel read was capped" in html
    # The cap's actual value, singular-correct. (Not "at 1 …": the template
    # wraps the line between "at" and the number.)
    assert "1 consult for this page" in html


async def test_an_exact_fit_is_not_reported_as_truncated(
    client, db_session, admin, monkeypatch
):
    """A cap that cries wolf gets ignored. Overflow is detected by fetching one
    row PAST the cap, so a page whose consults exactly fill it is not flagged —
    `len(rows) == limit` would have been."""
    from src.services import thread_panel

    monkeypatch.setattr(thread_panel, "CONSULT_ROW_LIMIT", 2)
    run, _ = await _thread_with_a_panel_and_no_decision(db_session)

    html = (
        await client.get(
            f"/admin/discussions?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text

    assert html.count('class="panel-card') == 2
    assert 'class="panel-truncated' not in html
    assert "was capped" not in html


async def test_the_truncation_notice_reaches_the_manager_page_too(
    client, db_session, manager, monkeypatch
):
    """The manager render is the same shared body and the same capped read, so
    a manager must not be the one reader who cannot tell a short panel from a
    truncated one."""
    from src.services import thread_panel

    monkeypatch.setattr(thread_panel, "CONSULT_ROW_LIMIT", 1)
    run, _ = await _thread_with_a_panel_and_no_decision(db_session)

    html = (
        await client.get(
            f"/manager/discussions?run_id={run.id}", headers=auth_headers(manager.id)
        )
    ).text

    assert 'class="panel-truncated' in html
    assert LATER_QUESTION in html
    assert RAW_OPINION_MARKER not in html


async def test_a_pi_still_cannot_reach_either_discussions_page(client, db_session):
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, email="panel-pi@example.org"
    )
    for path in ("/admin/discussions", "/manager/discussions"):
        resp = await client.get(
            path, headers=auth_headers(pi.id), follow_redirects=False
        )
        assert resp.status_code == 403, path
