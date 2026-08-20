"""The per-assessment detail page, on both the admin and the manager surface.

What these tests are for, beyond "it renders": the page is the first place in
the app where the LLM drill-down (tool calls, verbatim specialist opinions) and
a manager-visible surface meet. The split is a recorded policy decision — a
manager sees a consult's domain, signal, confidence, concerns and
questions_to_ask, and never the raw opinion, the raw verdict, or the hub's tool
log. A template edit that widened that is invisible without an assertion on the
manager response body, so there is one, keyed on fixture literals that exist
nowhere else on the page.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    LlmCallLog,
    OpportunityAssessment,
    SpecialistConsult,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

CHANNEL = "detail-page-channel"
HUB = "blackbird"
SUBJECT = "wang"

# Fixture literals. Each appears exactly once in the seeded data, so a
# substring assertion on the rendered page cannot be satisfied by chrome,
# legend prose or another row.
RAW_OPINION_MARKER = "RAW-OPINION-VERBATIM-TEXT"
CONCERN_MARKER = "CONCERN-NO-ISOGENIC-CONTROL"
QUESTION_MARKER = "QUESTION-WHICH-CONTROL"
HUB_QUESTION_MARKER = "HUB-ASKED-ABOUT-COUNTER-SCREEN"
TOOL_RESULT_MARKER = "TOOL-RESULT-USPTO-NO-HITS"
TOOL_QUERY_MARKER = "TOOL-QUERY-TFEB-MELANOMA"
RAW_VERDICT_MARKER = "RAW-VERDICT-SENTINEL"
RATIONALE_MARKER = "RATIONALE-DIFFERENTIATED-METABOLIC-ANGLE"
REPLY_TEXT = (
    "That is a clarifying answer, and it sharpens where the risk sits: not in "
    "the proteins but in what the counter-screen can logically report. "
    "Before I close this out I want one number from you."
)


async def _seed(db_session, *, with_messages: bool = True, with_consult: bool = True):
    """One assessment, its interview thread, one recorded consult, and one
    logged hub turn whose response_text matches the posted reply."""
    run = await factories.make_simulation_run(db_session)
    now = time.time()
    root_ts = f"{now - 600:.6f}"
    reply_ts = f"{now:.6f}"

    if with_messages:
        await factories.make_agent_message(
            db_session,
            run=run,
            agent_id=SUBJECT,
            channel_name=CHANNEL,
            message_ts=root_ts,
            phase="new_post",
            content="We have a selective inhibitor of the BCAA-autophagy axis.",
            posted_at=now - 600,
        )
        await factories.make_agent_message(
            db_session,
            run=run,
            agent_id=HUB,
            channel_name=CHANNEL,
            message_ts=reply_ts,
            thread_ts=root_ts,
            phase="thread_reply",
            content=REPLY_TEXT,
            posted_at=now,
        )

    if with_consult:
        db_session.add(
            SpecialistConsult(
                simulation_run_id=run.id,
                agent_id=HUB,
                subject_agent_id=SUBJECT,
                thread_id=root_ts,
                channel_name=CHANNEL,
                domain="scientific",
                question=HUB_QUESTION_MARKER,
                verdict_signal="caution",
                confidence="moderate",
                concerns=[CONCERN_MARKER],
                questions_to_ask=[QUESTION_MARKER],
                raw_opinion=RAW_OPINION_MARKER,
            )
        )

    db_session.add(
        LlmCallLog(
            simulation_run_id=run.id,
            agent_id=HUB,
            phase="thread_reply",
            channel=CHANNEL,
            model="claude-opus-test",
            system_prompt="SYSTEM-PROMPT-MUST-NOT-RENDER",
            messages_json=[
                {"role": "user", "content": "# Phase 4: Scouting Interview Reply"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "", "signature": "SIG"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "search_prior_art",
                            "input": {"query": TOOL_QUERY_MARKER},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": TOOL_RESULT_MARKER,
                        }
                    ],
                },
            ],
            response_text=f"<slack_message>\n{REPLY_TEXT}\n</slack_message>",
        )
    )

    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        slack_ts=reply_ts if with_messages else f"{now + 999:.6f}",
        company_or_project="Detail Page Fixture Co",
        funnel_stage="incubation",
        recommendation="conditional",
        confidence="Moderate",
        weighted_score=3.20,
        band="conditional",
        gating={"life_sciences_domain": "met", "fto_achievable": "unconfirmed"},
        scores={"differentiation": 4, "ip_fto": 2},
        red_flags=["RED-FLAG-NO-EXTERNAL-VALIDATION"],
        derisking_milestones=["MILESTONE-MOUSE-RESCUE"],
        rationale=RATIONALE_MARKER,
        raw_verdict={"weighted_score": 9.99, "sentinel": RAW_VERDICT_MARKER},
        panel_incomplete=True,
        missing_domains=["chemistry"],
    )
    db_session.add(assessment)
    await db_session.flush()
    return run, assessment


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="detail-admin@example.org"
    )


@pytest.fixture
async def manager(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="detail-manager@example.org"
    )


# ---------------------------------------------------------------------------
# Admin surface
# ---------------------------------------------------------------------------


async def test_admin_detail_page_renders_the_whole_verdict(client, db_session, admin):
    _, assessment = await _seed(db_session)
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text

    assert "Detail Page Fixture Co" in html
    assert "[Moderate]" in html and "[[Moderate]]" not in html
    assert SUBJECT in html and "incubation" in html
    assert RATIONALE_MARKER in html
    assert "RED-FLAG-NO-EXTERNAL-VALIDATION" in html
    assert "MILESTONE-MOUSE-RESCUE" in html
    # All thirteen dimensions, scored or not: an unscored one counts as zero in
    # the weighted score, so it has to be visibly distinguishable from a low one.
    assert 'class="score-differentiation' in html
    assert 'class="score-external_signals' in html
    assert "not scored" in html
    # The panel gap, named.
    assert "Specialist panel incomplete" in html
    assert "chemistry" in html
    # Back to the list.
    assert 'href="/admin/assessments"' in html


async def test_admin_detail_page_shows_the_panel_and_the_tool_activity(
    client, db_session, admin
):
    """The two strands that only exist on this page: the recorded consult, and
    the hub's tool log correlated to the reply it produced."""
    _, assessment = await _seed(db_session)
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text

    # Panel card, from specialist_consults.
    assert HUB_QUESTION_MARKER in html
    assert CONCERN_MARKER in html
    assert QUESTION_MARKER in html
    assert RAW_OPINION_MARKER in html  # admin-only, behind a <details>

    # Tool chip, parsed out of llm_call_logs.messages_json at read time.
    assert "search_prior_art" in html
    assert TOOL_QUERY_MARKER in html
    assert TOOL_RESULT_MARKER in html
    # ...attributed to the message it produced, not left in the unplaced group.
    assert "Unplaced turns" not in html
    # The thinking block's signature and the system prompt are never rendered.
    assert "SYSTEM-PROMPT-MUST-NOT-RENDER" not in html


async def test_admin_detail_page_shows_the_raw_verdict(client, db_session, admin):
    _, assessment = await _seed(db_session)
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert RAW_VERDICT_MARKER in html
    # The model's own weighted_score is in the raw block but is NOT the number
    # the page presents: 3.20 is computed, 9.99 is what the model claimed.
    assert "3.20" in html


async def test_admin_detail_page_files_an_uncorrelated_turn_as_unplaced(
    client, db_session, admin
):
    """A turn whose logged reply does not match any stored message is still
    evidence of what the hub did. It must appear, not vanish."""
    run, assessment = await _seed(db_session)
    db_session.add(
        LlmCallLog(
            simulation_run_id=run.id,
            agent_id=HUB,
            phase="thread_reply",
            channel=CHANNEL,
            model="claude-opus-test",
            system_prompt="sys",
            messages_json=[
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_9",
                            "name": "retrieve_abstract",
                            "input": {"pmid": "UNPLACED-PMID-30593499"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_9",
                            "content": "UNPLACED-TOOL-RESULT",
                        }
                    ],
                },
            ],
            response_text="<slack_message>A reply that was never stored.</slack_message>",
        )
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert "Unplaced turns" in html
    assert "UNPLACED-PMID-30593499" in html
    assert "UNPLACED-TOOL-RESULT" in html


async def test_admin_detail_page_tool_scan_keeps_the_newest_turns(
    client, db_session, admin, monkeypatch
):
    """Regression: the log-scan query used to be order_by(created_at).limit(N),
    which keeps the EARLIEST N rows in the window -- backwards, since the banner
    says "most recent" and the concluding turn (whose consults matter most) is
    always the newest. With the query fixed to newest-N (order desc, reversed for
    display), a turn older than the newest N must be dropped, not one of the
    newest."""
    from src.services import assessment_detail as assessment_detail_module

    monkeypatch.setattr(assessment_detail_module, "LOG_SCAN_LIMIT", 2)

    run, assessment = await _seed(db_session)
    backdated_now = time.time()
    oldest_marker = "TOOL-QUERY-OLDEST-MUST-BE-DROPPED"
    middle_marker = "TOOL-QUERY-MIDDLE-MUST-BE-DROPPED"
    newest_marker = "TOOL-QUERY-NEWEST-MUST-SURVIVE"
    # All three predate _seed's own logged turn (created_at defaults to real
    # now()), so with LOG_SCAN_LIMIT=2 only the newest of these three plus
    # _seed's turn fit -- oldest and middle must both be dropped.
    for i, marker in enumerate([oldest_marker, middle_marker, newest_marker]):
        db_session.add(
            LlmCallLog(
                simulation_run_id=run.id,
                agent_id=HUB,
                phase="thread_reply",
                channel=CHANNEL,
                model="claude-opus-test",
                system_prompt="sys",
                messages_json=[
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"toolu_scan_{i}",
                                "name": "search_prior_art",
                                "input": {"query": marker},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"toolu_scan_{i}",
                                "content": f"RESULT-{marker}",
                            }
                        ],
                    },
                ],
                response_text=f"<slack_message>Never stored reply {i}.</slack_message>",
                created_at=datetime.fromtimestamp(
                    backdated_now - 450 + i * 150, tz=UTC
                ),
            )
        )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert newest_marker in html
    assert middle_marker not in html
    assert oldest_marker not in html


async def test_admin_detail_page_survives_a_wiped_transcript(client, db_session, admin):
    """`--fresh` wipes agent_messages and never wipes opportunity_assessments,
    so a verdict legitimately outlives its own thread. The verdict must still
    render; only the timeline degrades."""
    _, assessment = await _seed(db_session, with_messages=False, with_consult=False)
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    assert "Interview messages unavailable" in resp.text
    assert "Detail Page Fixture Co" in resp.text
    assert RATIONALE_MARKER in resp.text


async def test_admin_detail_page_404s_on_an_unknown_id(client, db_session, admin):
    resp = await client.get(
        f"/admin/assessments/{uuid.uuid4()}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 404


async def test_admin_detail_page_requires_admin(client, db_session):
    _, assessment = await _seed(db_session)
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, email="detail-pi@example.org"
    )
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(pi.id)
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Manager surface
# ---------------------------------------------------------------------------


async def test_manager_detail_page_shows_the_verdict_and_the_panel_substance(
    client, db_session, manager
):
    _, assessment = await _seed(db_session)
    resp = await client.get(
        f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Detail Page Fixture Co" in html
    assert RATIONALE_MARKER in html
    assert 'class="score-differentiation' in html
    # A consult's signal and its structured lists are verdict substance.
    assert "caution" in html
    assert HUB_QUESTION_MARKER in html
    assert CONCERN_MARKER in html
    assert QUESTION_MARKER in html
    assert 'href="/manager/assessments"' in html


async def test_manager_detail_page_withholds_the_llm_drill_down(
    client, db_session, manager
):
    """D10 / plan decision 2, asserted on the wire: no verbatim opinion, no raw
    verdict, no tool chips. The service omits these from a manager render's
    context, so this also fails if only the template guard were removed."""
    _, assessment = await _seed(db_session)
    html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
        )
    ).text
    assert RAW_OPINION_MARKER not in html
    assert RAW_VERDICT_MARKER not in html
    assert TOOL_RESULT_MARKER not in html
    assert TOOL_QUERY_MARKER not in html
    assert "search_prior_art" not in html
    assert "Raw verdict JSON" not in html
    assert "/admin/" not in html


async def test_manager_detail_page_404s_on_an_unknown_id(client, db_session, manager):
    resp = await client.get(
        f"/manager/assessments/{uuid.uuid4()}", headers=auth_headers(manager.id)
    )
    assert resp.status_code == 404


async def test_pi_is_denied_the_manager_detail_page(client, db_session):
    _, assessment = await _seed(db_session)
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, email="detail-pi2@example.org"
    )
    resp = await client.get(
        f"/manager/assessments/{assessment.id}",
        headers=auth_headers(pi.id),
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# The list pages link here
# ---------------------------------------------------------------------------


async def test_both_assessment_lists_link_to_the_detail_page(
    client, db_session, admin, manager
):
    """A page nothing links to is a page nobody visits, and each surface must
    link to ITS OWN detail route — a manager control pointing at /admin 403s on
    click (F6)."""
    run, assessment = await _seed(db_session)
    admin_html = (
        await client.get(
            f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert f'href="/admin/assessments/{assessment.id}"' in admin_html

    manager_html = (
        await client.get(
            f"/manager/assessments?run_id={run.id}", headers=auth_headers(manager.id)
        )
    ).text
    assert f'href="/manager/assessments/{assessment.id}"' in manager_html
    assert "/admin/" not in manager_html


async def test_the_assessments_legend_states_the_rubric_thresholds(
    client, db_session, admin
):
    """The legend used to hard-code "≥4.0 / 3.0–3.9 / <3.0". Those numbers now
    come from prompts/rubric/blackbird-rubric.toml via BANDING, so a
    recalibration cannot leave the page confidently stating the old bar."""
    from src.services.blackbird_rubric import BANDING, RUBRIC_VERSION

    run, _ = await _seed(db_session)
    html = (
        await client.get(
            f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert f"&ge;{BANDING['advance_min']} advance" in html
    assert str(BANDING["pass_label"]) in html
    assert RUBRIC_VERSION in html


# ---------------------------------------------------------------------------
# Panel indicators elsewhere
# ---------------------------------------------------------------------------


async def test_discussions_pages_show_the_per_thread_panel_indicator(
    client, db_session, admin, manager
):
    """`panel_by_thread` reaches the shared threads body through both routers.
    The join is thread_id (specialist_consults) == message_ts (the root post);
    getting it wrong renders nothing at all, which no other test would notice.
    """
    run = await factories.make_simulation_run(db_session)
    root_ts = f"{time.time():.6f}"
    await factories.make_agent_message(
        db_session,
        run=run,
        agent_id=SUBJECT,
        channel_name=CHANNEL,
        message_ts=root_ts,
        phase="new_post",
        content="Root post for the panel indicator.",
        posted_at=time.time(),
    )
    db_session.add(
        SpecialistConsult(
            simulation_run_id=run.id,
            agent_id=HUB,
            subject_agent_id=SUBJECT,
            thread_id=root_ts,
            channel_name=CHANNEL,
            domain="chemistry",
            question="Is there a path to a development candidate?",
            verdict_signal="blocking",
            confidence="high",
            raw_opinion="not shown on this page",
        )
    )
    await db_session.flush()

    for base, user in (("/admin", admin), ("/manager", manager)):
        html = (
            await client.get(
                f"{base}/discussions?run_id={run.id}", headers=auth_headers(user.id)
            )
        ).text
        assert 'class="thread-panel' in html, f"no panel indicator on {base}/discussions"
        assert "chemistry" in html
        # The signal drives the colour, and blocking must not read as neutral.
        assert "text-red-600" in html


async def test_a_thread_with_no_consults_renders_unchanged(client, db_session, admin):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session,
        run=run,
        agent_id=SUBJECT,
        channel_name=CHANNEL,
        message_ts=f"{time.time():.6f}",
        phase="new_post",
        content="Root post with no panel.",
        posted_at=time.time(),
    )
    await db_session.flush()
    html = (
        await client.get(
            f"/admin/discussions?run_id={run.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert f"#{CHANNEL}" in html
    assert "thread-panel" not in html


async def test_llm_calls_page_badges_a_consult_signal(client, db_session, admin):
    """The signal was previously only visible by opening the row and reading the
    JSON, on the one page where a blocking specialist matters most."""
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session,
        run=run,
        agent_id=HUB,
        phase="consult_chemistry",
        response_text='{"verdict_signal": "blocking", "confidence": "high"}',
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id=HUB, phase="thread_reply", response_text="hello"
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id)
        )
    ).text
    assert 'class="consult-signal' in html
    assert "blocking" in html
    # Exactly one badge: the thread_reply row must not get one.
    assert html.count('class="consult-signal') == 1
