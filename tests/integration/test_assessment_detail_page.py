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

import re
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

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
        gating={"life_sciences_domain": "met", "translational_potential": "unconfirmed"},
        scores={"differentiation_unmet_need": 4, "venture_potential": 2},
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
    assert SUBJECT in html
    assert RATIONALE_MARKER in html
    assert "RED-FLAG-NO-EXTERNAL-VALIDATION" in html
    # funnel_stage and derisking_milestones are tolerant-passthrough COLUMNS
    # (the fixture sets both) but left the contract in v3.1.0/v3.2.0 and are
    # deliberately not rendered anywhere.
    assert "MILESTONE-MOUSE-RESCUE" not in html
    assert "Stage:" not in html
    # All six dimensions, scored or not: an unscored one counts as zero in
    # the weighted score, so it has to be visibly distinguishable from a low one.
    assert 'class="score-differentiation_unmet_need' in html
    assert 'class="score-scientific_credibility' in html
    assert "not scored" in html
    # The panel gap, named.
    assert "Specialist panel incomplete" in html
    assert "chemistry" in html
    # Back to the list.
    assert 'href="/admin/assessments"' in html


async def test_detail_page_renders_the_recommended_next_experiment(
    client, db_session, admin, manager
):
    """Sidecar item 10 is the line Blackbird staff act on, so it renders as its
    own labelled block on BOTH surfaces — the shared body template — and only
    when the column holds something (rows written before 0037 are NULL).

    Task 10 moved this block directly under the brief and relabelled it
    "The ask" — the heading text changed, the column and its guard did not."""
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        recommendation="advance",
        recommended_next_experiment="NEXT-EXPERIMENT-MARKER-SELECTIVITY-PANEL",
    )
    db_session.add(assessment)
    await db_session.flush()

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200
        assert "NEXT-EXPERIMENT-MARKER-SELECTIVITY-PANEL" in resp.text
        assert "The ask" in resp.text


# Deliberately no apostrophes or double quotes: markupsafe escapes ' -> &#39;
# and " -> &#34; inside the data-markdown attribute, so a naive `in html`
# check against quoted prose would fail even when rendering is correct.
TIMELINE_MARKDOWN_CONTENT = "A **bold** claim about HLA-A*02:01"


async def test_timeline_messages_render_via_data_markdown_on_both_surfaces(
    client, db_session, admin, manager
):
    """Interview messages are LLM/agent-generated prose — the same trust class
    as the discussion proposals that already use the sanitized data-markdown
    pattern (marked + DOMPurify, fail-closed to textContent) — so the timeline
    must render them through that pattern rather than as literal-asterisk
    plain text, on BOTH surfaces."""
    run = await factories.make_simulation_run(db_session)
    now = time.time()
    root_ts = f"{now - 600:.6f}"
    reply_ts = f"{now:.6f}"
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
        content=TIMELINE_MARKDOWN_CONTENT,
        posted_at=now,
    )
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        slack_ts=reply_ts,
        company_or_project="Markdown Timeline Fixture Co",
        recommendation="advance",
    )
    db_session.add(assessment)
    await db_session.flush()

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200
        html = resp.text
        assert 'data-markdown="A **bold** claim' in html
        assert "/static/js/markdown.js" in html
        assert "marked@12.0.2/marked.min.js" in html
        assert "dompurify@3.1.6/dist/purify.min.js" in html


async def test_sidecar_prose_stays_plain_text(client, db_session, admin, manager):
    """Rationale (and the other sidecar prose fields) is corpus-verified
    near-plain prose with meaningful literal `*` — e.g. `HLA-A*02:01` — that a
    markdown pass would corrupt, so it must never gain a data-markdown
    attribute, unlike the interview timeline above."""
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        recommendation="advance",
        rationale="**not markdown**",
    )
    db_session.add(assessment)
    await db_session.flush()

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200
        html = resp.text
        assert "**not markdown**" in html
        rationale_class_at = html.index('class="assessment-rationale')
        tag_start = html.rindex("<p", 0, rationale_class_at)
        tag_end = html.index(">", rationale_class_at)
        assert "data-markdown" not in html[tag_start:tag_end]


RATIONALE_MARKDOWN_CONTENT = "A **bold** claim about `HLA-A*02:01`"
NEXT_EXPERIMENT_MARKDOWN_CONTENT = "- Run the assay against `HLA-A*02:01` first"


async def test_prose_format_markdown_renders_data_markdown_divs_on_both_surfaces(
    client, db_session, admin, manager
):
    """`prose_format='markdown'` is the write-time stamp (0040) that gates
    rendering: a row written under the markdown-prompt contract renders
    `rationale` and `recommended_next_experiment` as sanitized data-markdown
    divs, on BOTH surfaces — the opposite of test_sidecar_prose_stays_plain_text
    below, which is the NULL/legacy case."""
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        recommendation="advance",
        rationale=RATIONALE_MARKDOWN_CONTENT,
        recommended_next_experiment=NEXT_EXPERIMENT_MARKDOWN_CONTENT,
        prose_format="markdown",
    )
    db_session.add(assessment)
    await db_session.flush()

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200
        html = resp.text
        assert 'data-markdown="A **bold** claim about `HLA-A*02:01`"' in html
        rationale_class_at = html.index('class="assessment-rationale')
        tag_start = html.rindex("<div", 0, rationale_class_at)
        tag_end = html.index(">", rationale_class_at)
        assert "data-markdown" in html[tag_start:tag_end]

        next_experiment_class_at = html.index('class="assessment-next-experiment')
        ne_tag_start = html.rindex("<div", 0, next_experiment_class_at)
        ne_tag_end = html.index(">", next_experiment_class_at)
        assert "data-markdown" in html[ne_tag_start:ne_tag_end]
        assert (
            'data-markdown="- Run the assay against `HLA-A*02:01` first"' in html
        )


async def test_markdown_cards_carry_the_md_content_class(
    client, db_session, admin, manager
):
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        recommendation="advance",
        rationale=RATIONALE_MARKDOWN_CONTENT,
        recommended_next_experiment=NEXT_EXPERIMENT_MARKDOWN_CONTENT,
        prose_format="markdown",
    )
    db_session.add(assessment)
    await db_session.flush()

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200
        html = resp.text
        # md-content must be on the same <div> that carries assessment-rationale
        rationale_at = html.index('class="assessment-rationale')
        tag_start = html.rindex("<div", 0, rationale_at)
        tag_end = html.index(">", rationale_at)
        assert "md-content" in html[tag_start:tag_end]


async def test_pass_recommendation_and_band_render_as_decline_on_the_detail_page(
    client, db_session, admin, manager
):
    """The stored vocabulary is unchanged (`recommendation`/`band` keep writing
    "pass") — only the display form, via `banding.pass_label`, changes. Both
    the recommendation chip and the band label route through it."""
    from src.services.blackbird_rubric import BANDING

    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        company_or_project="Decline Wording Fixture Co",
        recommendation="pass",
        weighted_score=1.50,
        band="pass",
    )
    db_session.add(assessment)
    await db_session.flush()

    assert BANDING["pass_label"] == "decline"
    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200
        html = resp.text
        assert "decline" in html
        assert 'class="band-label' in html


async def test_human_review_card_sits_between_dimension_scores_and_the_timeline(
    client, db_session, admin, manager
):
    """The Human review card moved between the Dimension scores card and the
    Interview timeline section (a pure reorder), on BOTH surfaces — the shared
    body template. String-index comparison of three stable headings."""
    _, assessment = await _seed(db_session)

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200
        html = resp.text
        dimension_scores_at = html.index("Dimension scores")
        human_review_at = html.index("Human review")
        timeline_at = html.index("Interview timeline")
        assert dimension_scores_at < human_review_at < timeline_at, (
            "Human review card must render between Dimension scores and the "
            f"Interview timeline: {path}"
        )


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


async def _seed_scale_fixture(db_session, *, funnel_stage):
    """A bare-bones assessment for exercising the banding/weights render,
    without the thread/consult apparatus ``_seed`` builds."""
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        company_or_project="Scale Fixture Co",
        funnel_stage=funnel_stage,
        rubric_version="3.0.0",
        recommendation="conditional",
        weighted_score=3.20,
        band="conditional",
        scores={"differentiation_unmet_need": 4, "venture_potential": 2},
    )
    db_session.add(assessment)
    await db_session.flush()
    return assessment


async def test_detail_page_renders_the_documents_banding_and_weights(
    client, db_session, admin
):
    """The legend and the dimension bars come from the row's own revision
    entry — the band lines (3.4/2.8) and the weight beside each bar
    (differentiation & unmet need at 25%) — whatever the row's funnel stage
    says, since one scale scores every verdict. (Live-document coverage lives
    in test_the_assessments_legend_states_the_rubric_thresholds.)"""
    assessment = await _seed_scale_fixture(db_session, funnel_stage="incubation")
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert "&ge;3.4 advance" in html
    assert "&lt;2.8" in html
    assert "25% weight" in html  # differentiation_unmet_need
    assert "10% weight" in html  # team_executability, unscored but listed


async def _seed_stamped(db_session, *, version, content_hash, scores):
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
        channel_name=CHANNEL, company_or_project="Stamped Fixture Co",
        recommendation="conditional", weighted_score=3.20, band="conditional",
        rubric_version=version, rubric_content_hash=content_hash, scores=scores,
    )
    db_session.add(assessment)
    await db_session.flush()
    return assessment


async def test_an_archived_stamp_renders_that_revisions_dimensions(
    client, db_session, admin
):
    """A v2.1.0 row must show its own 13-dimension space and 4.0/3.0 band
    lines — not six blank live dimensions under a 3.4/2.8 legend."""
    assessment = await _seed_stamped(
        db_session, version="2.1.0", content_hash="2f38fc9bce4d",
        scores={"ip_fto": 3, "differentiation": 4},
    )
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert 'class="score-ip_fto' in html
    assert "6%/4% (investment/incubation)" in html
    assert "&ge;4.0 advance" in html and "&lt;3.0" in html
    assert 'class="score-scientific_credibility' not in html, (
        "live-document dimensions leaked into an archived row's render"
    )


async def test_an_unknown_stamp_renders_the_rows_own_scores_with_a_warning(
    client, db_session, admin
):
    assessment = await _seed_stamped(
        db_session, version="9.9.9", content_hash="deadbeef0000",
        scores={"mystery_dim": 2},
    )
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert 'class="score-mystery_dim' in html
    assert "matches no entry in the revision registry" in html
    assert 'class="score-differentiation_unmet_need' not in html


async def test_an_unstamped_row_keeps_the_live_render_and_shows_extras(
    client, db_session, admin
):
    """Pre-0030 rows have no stamp: they keep today's live-document rendering
    (the banner already says so) — but any score key the live document does
    not name must still render instead of vanishing."""
    assessment = await _seed_stamped(
        db_session, version=None, content_hash=None,
        scores={"differentiation_unmet_need": 4, "ip_fto": 3},
    )
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert 'class="score-differentiation_unmet_need' in html
    assert 'class="score-scientific_credibility' in html  # live dims all listed
    assert 'class="score-ip_fto' in html                  # extra appended


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


# ---------------------------------------------------------------------------
# The panel banner's last two states: no panel was OWED, and nobody recorded
#
# A verdict the floor exempts stores the same `missing_domains=NULL` a genuine
# verification stores, and the page used to tell the reader nothing owed had
# been skipped — a claim about an audit that never ran. Production run 60c53424
# rendered it on a route-to-incubation verdict whose own content required a
# `clinical` consult that never happened.
#
# The first fix split that NULL by asking `panel_is_owed(recommendation, band)`
# AT RENDER TIME, and that re-armed the same bug one level up: the predicate has
# widened twice in one month, so every widening silently re-labels every row
# written under the older rule. 12 production rows written by the
# recommendation-only floor (which stored "no panel was owed" as
# `panel_incomplete=False, missing_domains=NULL`) came back green under the
# band-aware reader; at least five had a demonstrable gap. The page now replays
# the stored `panel_owed` column and claims nothing when the row does not carry
# one. See `src/services/assessment_detail.panel_state`.
# ---------------------------------------------------------------------------


async def _seed_exempt(db_session, recommendation: str, *, panel_owed=None):
    """One verdict with no gap and no consults recorded.

    `panel_owed` defaults to None — the shape of every row production actually
    holds, all of which predate migration 0036 — so a caller that wants the
    floor's own recorded answer has to say so, exactly as the page does.
    """
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        slack_ts=f"{time.time():.6f}",
        company_or_project="Exempt Verdict Fixture Co",
        recommendation=recommendation,
        weighted_score=2.51,
        band="pass",
        scores={"differentiation": 3},
        panel_incomplete=False,
        missing_domains=None,
        panel_owed=panel_owed,
    )
    db_session.add(assessment)
    await db_session.flush()
    return assessment


# `route-to-incubation` was dropped from this list on 2026-08-22: it is no longer
# exempt from the floor. It was exempted alongside `pass` on the reasoning that
# "a decline costs Blackbird nothing", but it is not a decline — it is the
# incubation grant Blackbird exists to award, which made it the one positive
# verdict class nobody reviewed. See `specialists.panel_is_owed`.
@pytest.mark.parametrize("recommendation", ["pass"])
async def test_an_exempt_verdict_does_not_claim_a_verified_panel(
    client, db_session, admin, recommendation
):
    assessment = await _seed_exempt(db_session, recommendation, panel_owed=False)
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text

    assert "Specialist panel: not required" in html
    # The two claims that must NOT appear: this row was never checked, so it is
    # neither a verified panel nor a demonstrated gap.
    assert "Specialist panel: no gap recorded" not in html
    assert "Nothing the verdict's own content owed a specialist" not in html
    assert "Specialist panel incomplete" not in html


def _panel_box(html: str) -> str:
    """The `id="panel"` div and nothing else, bounded by its own tag depth.

    The three `bg-green-50` probes below used to be page-wide, on the premise —
    stated in `test_an_unknown_panel_state_never_renders_green`'s docstring —
    that "`bg-green-50` appears exactly once in this template, on the panel
    box". That premise stopped being true on 2026-09-14, when the
    strengths/risks card gained a green Strengths column, and a page-wide probe
    then reported EVERY row as green regardless of its panel state — a probe
    that passes for the wrong reason, which for a test whose whole job is to
    stop an unvetted verdict reading as fine is worse than one that fails.

    Scoping restores the original meaning exactly: the assertions are as strict
    as they ever were about the panel box, and say nothing about the rest of the
    page. Bounded by walking `<div`/`</div>` depth from the element's own
    opening tag rather than by a sentinel, for the reason `_row_slice` in
    tests/integration/test_assessment_queue_controls.py records at length: a
    sentinel assumes something about what follows the element, and has silently
    over-returned twice.
    """
    start = html.find('<div id="panel"')
    assert start != -1, "the detail page no longer carries a #panel element"
    tag_end = html.find(">", start)
    assert tag_end != -1, "malformed #panel opening tag"
    pos = tag_end + 1
    depth = 1
    for tag in re.finditer(r"</div>|<div\b", html[pos:]):
        depth += 1 if tag.group() == "<div" else -1
        if depth == 0:
            # From `start`, not `pos`: the element's OWN class attribute lives in
            # the opening tag, and `bg-green-50` is one of those classes. Slicing
            # from after the tag would make the positive probe below unsatisfiable
            # while leaving the negative ones passing vacuously.
            return html[start : pos + tag.start()]
    raise AssertionError("unbalanced #panel div")


async def test_a_conditional_verdict_still_reports_a_verified_panel(
    client, db_session, admin
):
    """INVERTED 2026-08-22 — the old assertion is the headline defect itself.

    This used to seed `panel_incomplete=False, missing_domains=None` and assert
    the page reports "no gap recorded", on the reasoning that `conditional` IS
    held to the floor so an empty gap must be a real verification. That
    reasoning belongs to the ENGINE at write time; applied at read time it
    invents a verification for every row written under a different rule, which
    is precisely how 12 production rows came back green.

    The fixture is unchanged (it is what production holds); only the claim the
    page is allowed to make about it has changed. `panel_owed=True` — the floor
    recording that it evaluated this verdict — is what buys the green box now,
    and the companion test below pins that half.
    """
    assessment = await _seed_exempt(db_session, "conditional")
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Specialist panel: not recorded" in html
    assert "Specialist panel: no gap recorded" not in html
    assert "Nothing the verdict's own content owed a specialist" not in html
    # The copy must not blame the row's age: post-0036 rows land here too
    # (backfills, hand-built fixtures), and "predates panel tracking" would be
    # false for every one of them. (Narrowed to the phrase rather than the word
    # — the rubric-provenance line legitimately says a row "predates rubric
    # stamping", which is a different and checkable claim.)
    assert "predates panel" not in html.lower()
    # Never green, whatever else it says. Scoped to the panel box — see
    # `_panel_box` for why a page-wide probe stopped meaning this.
    assert "bg-green-50" not in _panel_box(html)


async def test_a_floor_checked_verdict_reports_a_verified_panel(
    client, db_session, admin
):
    """The guard on the other side, and the only route to the green box: the
    floor recorded that a panel was owed, so it evaluated this verdict, and it
    recorded no gap."""
    assessment = await _seed_exempt(db_session, "conditional", panel_owed=True)
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Specialist panel: no gap recorded" in html
    assert "Specialist panel: not required" not in html
    assert "Specialist panel: not recorded" not in html
    assert "bg-green-50" in _panel_box(html)


async def test_an_unknown_panel_state_never_renders_green(
    client, db_session, admin, monkeypatch
):
    """The template's terminal `{% else %}` used to BE the green box, so every
    state the template did not enumerate — a typo, a state added to
    `panel_state` and not to the template, a future sixth finding — rendered as
    a verified panel. Green is the one claim that must be reached only
    deliberately, so it is now an explicit `panel_state == 'verified'` branch and
    the fallback is the neutral "not recorded" box.

    Driven by forcing an off-contract state through the real render path rather
    than by reading the template source: what matters is the HTML a reader sees.
    `bg-green-50` is the probe for "did this render green", scoped to the panel
    box by `_panel_box`. It used to be a page-wide check on the premise that the
    class appeared exactly once in this template; the strengths/risks card
    (2026-09-14) made that false, so the probe is scoped rather than weakened.
    """
    assessment = await _seed_exempt(db_session, "conditional", panel_owed=True)
    monkeypatch.setattr(
        "src.services.assessment_detail.panel_state",
        lambda _assessment: "a_state_that_does_not_exist",
    )
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text
    assert "bg-green-50" not in _panel_box(html), (
        "an unhandled panel state must never render as a verified panel"
    )
    assert "Nothing the verdict's own content owed a specialist" not in html


# ---------------------------------------------------------------------------
# `retro_consult_count` counts THIS interview's consults
#
# The retro count exists for pre-`specialist_consults` rows: no durable consult
# rows, so the page recovers them from the hub's own tool log. But
# `_load_tool_turns` selects log rows by (run, phase, agent, CHANNEL, time
# window) — not by thread, which the log table cannot express. Several
# interviews share a channel, so the scan legitimately pulls in other threads'
# turns; `correlate_turns_to_messages` then fails to place them and returns
# them as `unplaced`. Summing chips over ALL scanned turns therefore counted
# other interviews' consults as this one's. Measured on production run
# 60c53424: the kevrekidis assessment reported 11 against 7 real consults, with
# 4 unplaced turns making up the difference exactly.
# ---------------------------------------------------------------------------


async def test_retro_consult_count_excludes_turns_from_other_interviews(
    client, db_session, admin
):
    """Two consults sit in the scan window and exactly one is this interview's.

    The other is on a turn that places nowhere — the shape of a turn belonging to
    a different interview that shares this channel and time window. Before the fix
    the count was 2.
    """
    from src.services.assessment_detail import build_assessment_detail

    _, assessment = await _seed(db_session, with_consult=False)

    def _consult_turn(tool_id, domain, response_text):
        return LlmCallLog(
            simulation_run_id=assessment.simulation_run_id,
            agent_id=HUB,
            phase="thread_reply",
            channel=CHANNEL,
            model="claude-opus-test",
            system_prompt="sys",
            messages_json=[
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": tool_id, "name": "consult_specialist",
                    "input": {"domain": domain, "question": "a question"},
                }]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": tool_id,
                    "content": domain.title() + " Specialist \u2014 signal: caution\n\nBody.",
                }]},
            ],
            response_text=response_text,
            created_at=datetime.now(UTC),
        )

    # Places on this thread: its posted text IS the thread's reply, so
    # correlate_turns_to_messages matches it. This is the one that counts.
    db_session.add(_consult_turn(
        "toolu_mine", "scientific",
        "<slack_message>\n" + REPLY_TEXT + "\n</slack_message>",
    ))
    # Places nowhere.
    db_session.add(_consult_turn(
        "toolu_other", "clinical",
        "<slack_message>A reply on a different thread.</slack_message>",
    ))
    await db_session.flush()

    ctx = await build_assessment_detail(db_session, assessment.id, admin_view=True)

    assert len(ctx["unplaced_turns"]) == 1, "the other interview's turn is unplaced"
    assert sum(
        1 for turn in ctx["unplaced_turns"] for chip in turn["chips"] if chip["is_consult"]
    ) == 1, "and it is a consult, so it is available to be miscounted"
    assert ctx["retro_consult_count"] == 1, (
        "only consults on turns placed in THIS interview count"
    )


# ---------------------------------------------------------------------------
# A truncated consult is not an opinion (0036's `specialist_consults.truncated`)
#
# The floor already refuses to credit one, the Slack panel note already skips
# one, and the column already records one. This page was the surviving reader
# that did not know: `_load_consults` SELECTed the whole row and dropped
# `truncated` from the dict it projected, so the parse default
# (src/agent/specialists.py — `caution`) was rendered as though a specialist had
# said it, in the chips directly under the panel-state box.
# ---------------------------------------------------------------------------

TRUNCATED_DOMAIN = "translational"


async def _seed_with_a_truncated_consult(db_session):
    """`_seed`'s own consult is the control: `caution`, and NOT truncated. The
    second is the same signal arrived at by the parser giving up."""
    run, assessment = await _seed(db_session)
    thread_id = (
        await db_session.execute(
            select(SpecialistConsult.thread_id).where(
                SpecialistConsult.simulation_run_id == run.id
            )
        )
    ).scalar_one()
    db_session.add(
        SpecialistConsult(
            simulation_run_id=run.id,
            agent_id=HUB,
            subject_agent_id=SUBJECT,
            thread_id=thread_id,
            channel_name=CHANNEL,
            domain=TRUNCATED_DOMAIN,
            question="Does the reimbursement path survive?",
            verdict_signal="caution",
            confidence="moderate",
            raw_opinion="The reimbursement picture is complicated by",
            truncated=True,
        )
    )
    await db_session.flush()
    return run, assessment


async def test_a_truncated_consult_is_not_rendered_as_a_caution_opinion(
    client, db_session, admin
):
    _, assessment = await _seed_with_a_truncated_consult(db_session)
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text

    # The control proves the assertion below is not vacuous: an untruncated
    # `caution` still reads as a `caution` in the same chip row.
    assert "scientific &middot; caution" in html
    # The truncated one must not. Its signal is what the parser defaults to
    # when it cannot read a reply, not what anyone said.
    assert f"{TRUNCATED_DOMAIN} &middot; caution" not in html
    # Twice: the summary chip under the panel-state box, and the consult's own
    # card in the timeline. Both are places a reader counts opinions.
    assert html.count("panel-cut-off") == 2
    assert TRUNCATED_DOMAIN in html, "the consult is still shown — it happened"


async def test_the_truncated_marking_survives_a_manager_render(
    client, db_session, manager
):
    """Managers read this page too, and they are the audience the panel-state
    box was rewritten for. `raw_opinion` is the only admin/manager difference."""
    _, assessment = await _seed_with_a_truncated_consult(db_session)
    html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
        )
    ).text

    assert "scientific &middot; caution" in html
    assert f"{TRUNCATED_DOMAIN} &middot; caution" not in html
    assert html.count("panel-cut-off") == 2


# ---------------------------------------------------------------------------
# Task 10: brief first, evidence collapsed, sticky nav
# ---------------------------------------------------------------------------

_DETAILS_TAG_RE = re.compile(r"<details\b|</details>", re.IGNORECASE)


def _main(html: str) -> str:
    """The page's own `<main>` region.

    Everything these template tests are about lives inside `<main>`; the
    wrappers' `<style>` and `<script>` blocks and base.html's chrome do not.
    Both legitimately MENTION the body template's own class names and tag
    names as plain text — `.assessment-brief-pitch { ... }` in a print rule,
    and the words "CSS cannot open a <details>" in a comment — so a
    whole-document substring or tag scan reads stylesheet prose as rendered
    markup. Falls back to the whole string when there is no `<main>` (the
    synthetic-HTML unit test below)."""
    if "<main" not in html:
        return html
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def _top_level_details_contents(html: str) -> str:
    """Concatenate the full contents of every TOP-LEVEL <details>...</details>
    element on the page, including everything nested inside it.

    A flat, non-greedy regex (`r"<details\\b.*?</details>"`) is the wrong tool
    here, because this page genuinely nests <details> elements: the "What the
    scale means here" disclosure inside `dimension_score_rows` (rendered
    inside both the Human-review card's Edit and Add-feedback <details>), the
    per-chip tool-call <details> the `tool_turn` macro renders inside the
    Interview timeline, and the "Full opinion (raw)" <details> on a timeline
    consult card. Against a nested pair, a non-greedy match stops at the
    FIRST inner </details> it meets, so the OUTER element's real closing tag
    is orphaned — and any plain content between the last inner match consumed
    and that orphaned tag is silently dropped from the result, even though it
    is genuinely collapsed inside the outer <details>. A test asserting a
    warning string is never inside a collapsed region would then pass by
    under-reporting what "inside" contains, not by the warning being safe.

    This instead walks every <details>/</details> occurrence in document
    order, tracking nesting depth, and captures each TOP-LEVEL element's span
    (open tag through its own matching close tag) exactly once — nested
    <details> content is included as part of its enclosing top-level span
    rather than cutting it short.

    Two failure modes are asserted LOUDLY rather than degrading to a
    valid-looking value, because both of this helper's two callers
    (`test_the_panel_banner_is_never_inside_a_collapsed_details` and
    `test_a_non_empty_red_flag_list_is_never_collapsed`) are pure ABSENCE
    assertions against the return value — `"X" not in inside`. Either failure
    mode below used to return a value that made those assertions pass
    vacuously, for a reason that has nothing to do with the thing they claim
    to guard:

    * an unclosed `<details>` (a template bug, or a change to this page that
      breaks tag balance) used to leave that element's content out of `parts`
      silently, so a warning genuinely trapped inside it would read as "not
      inside" simply because the scan never captured the span;
    * a page with no `<details>` at all — e.g. a template regression that
      drops the collapsible sections entirely, or a caller pointed at the
      wrong response body — used to return `""`, and `"X" not in ""` is
      trivially true for every `X`, so the assertion would pass while proving
      nothing about where `X` actually rendered.
    """
    parts: list[str] = []
    depth = 0
    start = None
    for m in _DETAILS_TAG_RE.finditer(html):
        if m.group().lower().startswith("<details"):
            if depth == 0:
                start = m.start()
            depth += 1
        else:
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    parts.append(html[start : m.end()])
                    start = None
    assert depth == 0, (
        "an unclosed <details> element — the scan ended still inside one, so "
        "its content would have been silently dropped from the result"
    )
    assert parts, (
        "no top-level <details> element was found on this page at all — "
        "either the page regressed and lost its collapsible sections, or "
        "this helper was pointed at the wrong HTML"
    )
    return "".join(parts)


def test_the_details_extraction_helper_catches_nested_content_a_flat_regex_misses():
    """Proof for `_top_level_details_contents`, per the fix-round request: a
    warning string sitting AFTER a nested <details> but still inside the
    enclosing outer <details> must be caught. The old flat, non-greedy regex
    (`r"<details\\b.*?</details>"`) stops at the first inner </details> and
    never sees it."""
    synthetic = (
        "<p>outside, visible</p>"
        "<details><summary>outer</summary>"
        "<details><summary>inner</summary><p>inner body</p></details>"
        "<p>WARNING-AFTER-NESTED-DETAILS</p>"
        "</details>"
    )

    old_regex_extraction = "".join(
        re.findall(r"<details\b.*?</details>", synthetic, re.DOTALL)
    )
    assert "WARNING-AFTER-NESTED-DETAILS" not in old_regex_extraction, (
        "the old flat regex was expected to MISS this — if it no longer does, "
        "the premise for replacing it is gone"
    )

    assert "WARNING-AFTER-NESTED-DETAILS" in _top_level_details_contents(synthetic)
    # And the truly-outside paragraph must still read as outside.
    assert "outside, visible" not in _top_level_details_contents(synthetic)


async def test_the_brief_leads_with_headline_pitch_and_points(
    client, db_session, admin
):
    run, assessment = await _seed(db_session)
    assessment.headline = "HEADLINE-MARKER: a blood test for immunotherapy response."
    assessment.elevator_pitch = "PITCH-MARKER. Hopkins has data on 124 patients."
    assessment.key_points = ["POINT-ONE-MARKER", "POINT-TWO-MARKER"]
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "assessment-brief" in html
    assert "HEADLINE-MARKER" in html
    assert "PITCH-MARKER" in html
    assert "POINT-ONE-MARKER" in html and "POINT-TWO-MARKER" in html
    # The brief precedes the rationale on the page, not just in the DOM tree.
    assert html.index("assessment-brief") < html.index("assessment-rationale")


async def test_a_pre_0043_row_renders_the_brief_without_empty_states(
    client, db_session, admin
):
    """A3: every row in production today has all three fields NULL. The page
    must degrade to the short label and show no bullet or pitch block."""
    run, assessment = await _seed(db_session)
    assessment.company_or_project = "ONLY-LABEL-MARKER"
    assessment.headline = None
    assessment.elevator_pitch = None
    assessment.key_points = None
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "ONLY-LABEL-MARKER" in html
    body = _main(html)
    assert "assessment-brief-pitch" not in body
    assert "assessment-brief-points" not in body


async def test_the_panel_banner_is_never_inside_a_collapsed_details(
    client, db_session, admin
):
    """N8/design §5. The panel banner is a WARNING, not evidence. Rendering a
    non-verified panel as unremarkable is a named failure mode in this repo;
    putting it behind a disclosure is the same error in a different place.

    Uses `_top_level_details_contents`, not a flat non-greedy regex: this
    page nests <details> (see that helper's docstring), and a flat regex can
    under-report what "inside" contains."""
    run, assessment = await _seed(db_session)
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    # Everything inside any <details>...</details> must not contain the banner.
    inside = _top_level_details_contents(_main(html))
    # Positive control: prove the scan actually covered the region we think it
    # did, not just that the banner text happens to be absent from it — an
    # empty or wrongly-scoped `inside` would make the assertion below pass for
    # the wrong reason.
    assert "Full rationale" in inside
    assert "Specialist panel" not in inside


async def test_a_non_empty_red_flag_list_is_never_collapsed(
    client, db_session, admin
):
    """Same reasoning: a disqualifier-grade flag behind a click is a flag the
    reviewer does not see. See `_top_level_details_contents` for why this
    uses a nesting-aware scan rather than a flat regex."""
    run, assessment = await _seed(db_session)
    assessment.red_flags = ["RED-FLAG-MARKER-MUST-BE-VISIBLE"]
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    inside = _top_level_details_contents(_main(html))
    # Positive control: same reasoning as the panel-banner test above — prove
    # the scan covered the region we think it did before trusting an absence
    # assertion against it.
    assert "Full rationale" in inside
    assert "RED-FLAG-MARKER-MUST-BE-VISIBLE" in html
    assert "RED-FLAG-MARKER-MUST-BE-VISIBLE" not in inside


async def test_the_rationale_is_collapsed_and_labelled_with_its_size(
    client, db_session, admin
):
    run, assessment = await _seed(db_session)
    assessment.rationale = "**A.** one\n\n**B.** two\n\n**C.** three"
    assessment.prose_format = "markdown"
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "Full rationale (3 paragraphs)" in html


async def test_the_jump_nav_lists_every_section(client, db_session, admin):
    run, assessment = await _seed(db_session)
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "assessment-jump-nav" in html
    for anchor in ("#brief", "#rationale", "#panel", "#scores", "#review", "#timeline"):
        assert f'href="{anchor}"' in html, anchor


async def test_the_jump_nav_omits_rationale_when_there_is_none(
    client, db_session, admin
):
    """FIX 1 (review round 1). `rationale` is nullable and the `#rationale`
    section only renders inside `{% if a.rationale %}` — the nav link must be
    gated the same way, or a NULL-rationale row ships a nav that points at
    nothing. `_seed()` always sets a rationale, which is why none of the
    original six tests caught this; null it out the way
    test_admin_detail_page_survives_a_wiped_transcript does for other fields."""
    run, assessment = await _seed(db_session)
    assessment.rationale = None
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert 'href="#rationale"' not in html
    for anchor in ("#brief", "#panel", "#scores", "#review", "#timeline"):
        assert f'href="{anchor}"' in html, anchor


async def test_a_grouped_key_points_sidecar_renders_the_three_labels_in_order(
    client, db_session, admin
):
    """Task 7 / F3. The three-group `key_points` object (scout_hub >= 1.3.0)
    must render its three labels, in order, on the admin detail page."""
    run, assessment = await _seed(db_session)
    obj = {
        "significance": ["Sig point"],
        "innovation": ["Innov point"],
        "commercial_potential": ["Comm point"],
    }
    assessment.key_points = obj
    await db_session.flush()

    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    html = resp.text
    assert "Significance" in html
    assert "Innovation" in html
    assert "Commercial potential" in html
    i, j, k = (
        html.index("Significance"),
        html.index("Innovation"),
        html.index("Commercial potential"),
    )
    assert i < j < k


async def test_the_detail_body_uses_readable_type_sizes(client, db_session, manager):
    """F5: prose at 16px (`text-base`), metadata at 14px (`text-sm`), 12px
    (`text-xs`) reserved for chips; no `text-gray-400` on running text."""
    _, assessment = await _seed(db_session)
    html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
        )
    ).text
    # Scoped to `<main>...</main>`: base.html's footer ("Blackbird
    # Laboratories") is site-wide chrome this task does not own and
    # legitimately carries its own `text-gray-400`.
    body = html.split("Assessment detail", 1)[1].split("</main>", 1)[0]
    # Measured after the sweep: 11 `text-xs` remain, all inside `rounded-full`
    # chip spans (the reserved exception). +2 slack.
    assert body.count("text-xs") <= 13, body.count("text-xs")
    assert 'class="assessment-prose' in body
    assert "text-gray-400" not in body
    assert "bg-gray-400" not in body
    assert "text-gray-500" not in body
    assert "text-base" in body


# ---------------------------------------------------------------------------
# Readability pass (2026-09-11 plan, Task A)
# ---------------------------------------------------------------------------


async def test_the_brief_is_two_columns_pitch_left_points_right(
    client, db_session, admin
):
    """The operator's layout: "In one minute" is the LEFT column and the key
    points the RIGHT one. With no key points there is no second column at
    all — an empty grid cell reads as missing data."""
    run, assessment = await _seed(db_session)
    assessment.elevator_pitch = "PITCH-MARKER. Hopkins has data on 124 patients."
    assessment.key_points = ["POINT-ONE-MARKER", "POINT-TWO-MARKER"]
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    body = _main(html)
    assert "assessment-brief-grid" in body
    assert "assessment-brief-pitch" in body
    assert "assessment-brief-keypoints" in body
    assert body.index("assessment-brief-pitch") < body.index(
        "assessment-brief-keypoints"
    )

    assessment.key_points = None
    await db_session.flush()
    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    body = _main(html)
    assert "assessment-brief-pitch" in body
    assert "assessment-brief-keypoints" not in body


async def test_a_mapping_of_only_empty_lists_renders_no_key_points_column(
    client, db_session, admin
):
    """A `key_points` mapping whose groups are all present but empty is not
    "there are key points", so the right column and its two-column grid must
    not render."""
    run, assessment = await _seed(db_session)
    assessment.elevator_pitch = "PITCH-MARKER. Hopkins has data on 124 patients."
    assessment.key_points = {
        "significance": [],
        "innovation": [],
        "commercial_potential": [],
    }
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    body = _main(html)
    assert "assessment-brief-keypoints" not in body
    assert "md:grid-cols-2" not in body


async def test_rationale_and_scores_are_open_by_default(client, db_session, admin):
    """Rationale and dimension scores are what a reviewer came for; gating and
    the timeline stay collapsed."""
    run, assessment = await _seed(db_session)
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    html = _main(html)
    assert re.search(r'<details[^>]*id="rationale"[^>]*\bopen\b', html)
    assert re.search(r'<details[^>]*id="scores"[^>]*\bopen\b', html)
    for section in ("gating", "timeline"):
        m = re.search(rf'<details[^>]*id="{section}"[^>]*>', html)
        assert m, section
        assert "open" not in m.group(), section


async def test_expand_all_controls_render(client, db_session, admin):
    """Two real <button>s in the jump nav, keyboard-operable by nature."""
    run, assessment = await _seed(db_session)
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    nav = _main(html).split('class="assessment-jump-nav', 1)[1].split("</nav>", 1)[0]
    assert 'data-details-toggle="open"' in nav
    assert 'data-details-toggle="close"' in nav
    assert nav.count('<button type="button"') >= 2
    assert "Expand all" in nav and "Collapse all" in nav


async def test_legacy_rationale_renders_paragraphs(client, db_session, admin):
    """A `prose_format=None` row is plain text, and its blank-line-separated
    paragraphs must render as separate <p> elements rather than one block."""
    run, assessment = await _seed(db_session)
    assessment.prose_format = None
    assessment.rationale = "LEGACY-PARA-ONE text.\n\nLEGACY-PARA-TWO text."
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    block = html.split("assessment-rationale", 1)[1].split("</div>", 1)[0]
    assert block.count("<p") >= 2, block
    assert "LEGACY-PARA-ONE" in block and "LEGACY-PARA-TWO" in block


async def test_gating_legend_is_visible_text(client, db_session, admin):
    """Glyph meanings were `title` tooltips only — invisible to touch, and to
    anyone who does not hover. They are visible text now, and each glyph
    carries an aria-label as well as its title."""
    run, assessment = await _seed(db_session)
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "gating-legend" in html
    match = re.search(r'<p class="gating-legend[^"]*"[^>]*>.*?</p>', html, re.DOTALL)
    assert match is not None, "gating-legend <p> element not found"
    legend = match.group(0)
    assert "met" in legend and "not met" in legend and "unconfirmed" in legend
    assert 'aria-label="Unconfirmed' in html


# ---------------------------------------------------------------------------
# Strengths / risks box, score rationale, five key-point groups
# (2026-09-14 assessment-UX plan, Task B)
# ---------------------------------------------------------------------------


def _signals_card(body: str) -> str:
    """The `#signals` card as rendered, for slicing into its three columns.

    Taken by string index rather than by a tag regex: the card nests several
    plain `<div>`s, so a non-greedy `<div id="signals".*?</div>` stops at the
    first inner close and would report a column as missing when it is
    present."""
    assert 'id="signals"' in body, "the strengths/risks card did not render"
    start = body.index('id="signals"')
    end = body.index("signals-provenance", start)
    return body[start:end]


def _signal_columns(body: str) -> tuple[str, str, str]:
    """(strengths, risks, unestablished) slices of the `#signals` card.

    The three columns render in that fixed order, so slicing between their
    class names — and ending at the always-present legend — keeps each
    assertion scoped to ONE column. A whole-card substring check cannot tell
    the green column from the red one, which is the entire point of the two
    absence tests below."""
    card = _signals_card(body)
    i = card.index("assessment-signals-strengths")
    j = card.index("assessment-signals-risks")
    k = card.index("assessment-signals-unestablished")
    legend = card.index("signals-legend")
    assert i < j < k < legend, "the three signal columns rendered out of order"
    return card[i:j], card[j:k], card[k:legend]


async def test_the_strengths_and_risks_box_renders_three_columns_and_a_footnote(
    client, db_session, admin, manager
):
    """B1. All three columns always render — an absent one would let a reader
    mistake "we did not classify this" for "there are none" — and the footnote
    names the provenance so a DERIVED summary is never read as something the
    hub wrote. On both surfaces: this is the shared body template."""
    _, assessment = await _seed(db_session)
    await db_session.flush()

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        body = _main((await client.get(path, headers=auth_headers(user.id))).text)
        assert "assessment-signals" in body
        strengths, risks, unestablished = _signal_columns(body)
        assert "Strengths" in strengths
        assert "Risks" in risks
        assert "Not established" in unestablished
        # Words, not just colour: every glyph is aria-labelled and the legend
        # restates all three in text (the gating-legend precedent).
        assert 'aria-label="Strength"' in body or "No dimension scored" in strengths
        assert "signals-legend" in body
        legend = re.search(
            r'<p class="signals-legend[^"]*"[^>]*>.*?</p>', body, re.DOTALL
        )
        assert legend is not None, "signals-legend <p> not found"
        assert "strength" in legend.group(0)
        assert "risk" in legend.group(0)
        assert "not established" in legend.group(0)
        # The footnote, always visible, naming where this came from.
        assert "signals-provenance" in body
        assert "Derived from this verdict" in body
        assert "dimension scores, gating states, red" in body
        # And the jump nav reaches it.
        assert 'href="#signals"' in body


async def test_an_unconfirmed_gate_is_in_neither_the_green_nor_the_red_column(
    client, db_session, admin
):
    """"not met" (asked and failed) and "unconfirmed" (never asked) are
    different answers, and only the first can license discounting an idea — so
    an unconfirmed gate must not be coloured as a risk, and must certainly not
    be coloured as a strength. `_seed` sets `translational_potential:
    unconfirmed` and `life_sciences_domain: met`."""
    _, assessment = await _seed(db_session)
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    strengths, risks, _unestablished = _signal_columns(body)
    assert "translational potential" not in strengths
    assert "translational potential" not in risks
    # Positive control: the gate IS classified somewhere on the card, so the
    # two absences above are about placement rather than about a card that
    # silently dropped it.
    assert "translational potential" in _signals_card(body)


async def test_a_row_with_an_unknown_revision_says_its_dimensions_could_not_be_classified(
    client, db_session, admin
):
    """`scale_known` is False when the row's stamp matches no entry in the
    revision registry: the band lines and the 1-5 anchors that decide what
    counts as a strength are not knowable, so the footnote says the dimension
    scores could not be classified rather than classifying them against a
    document that did not score the row."""
    assessment = await _seed_stamped(
        db_session, version="9.9.9", content_hash="deadbeef0000",
        scores={"mystery_dim": 2},
    )
    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    assert "could not be classified" in body
    assert "not in the registry" in body
    # Still three columns: an unclassifiable scale is not a reason to drop the
    # gating, red-flag and specialist signals that ARE knowable.
    _signal_columns(body)


async def test_the_box_is_never_inside_a_collapsed_details(
    client, db_session, admin
):
    """Same reasoning as the panel banner and the red-flag card: this is the
    summary a reviewer reads first, and a summary behind a click is a summary
    that is not read. Uses the nesting-aware scan, not a flat regex."""
    _, assessment = await _seed(db_session)
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    element = re.search(r'<div id="signals"[^>]*>', body)
    assert element is not None, "the #signals element did not render"
    assert "<details" not in body[: element.start()].rsplit("</details>", 1)[-1], (
        "an unclosed <details> opens before the #signals card"
    )
    inside = _top_level_details_contents(body)
    # Positive control: prove the scan covered the region we think it did
    # before trusting the absence assertions against it.
    assert "Full rationale" in inside
    assert 'id="signals"' not in inside
    assert "Strengths and risks" not in inside


SCORE_RATIONALE_MARKER = "SCORE-RATIONALE-MARKER: **weakest** on venture potential"


async def test_the_score_rationale_renders_markdown_only_when_stamped(
    client, db_session, admin
):
    """B2. Same write-time `prose_format` gate as the pitch and the rationale:
    a NULL stamp is legacy plain text, which may carry a literal `*` in a
    scientific identifier that a markdown pass would corrupt."""
    _, assessment = await _seed(db_session)
    assessment.score_rationale = SCORE_RATIONALE_MARKER
    assessment.prose_format = "markdown"
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    assert "assessment-brief-score-rationale" in body
    assert "Why this score" in body
    assert "assessment-brief-score-rationale-body md-content" in body
    assert "assessment-brief-score-rationale-body whitespace-pre-line" not in body
    # It sits inside the brief card, above the strengths/risks box.
    assert body.index("assessment-brief") < body.index(
        "assessment-brief-score-rationale"
    ) < body.index("assessment-signals")

    assessment.prose_format = None
    await db_session.flush()
    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    assert "assessment-brief-score-rationale-body whitespace-pre-line" in body
    assert "assessment-brief-score-rationale-body md-content" not in body
    assert SCORE_RATIONALE_MARKER in body


async def test_a_pre_0048_row_renders_no_score_rationale_block(
    client, db_session, admin
):
    """NULL on every row written before 0048, deliberately never backfilled:
    the block is skipped outright rather than rendering an empty state."""
    _, assessment = await _seed(db_session)
    assessment.score_rationale = None
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    assert "assessment-brief-score-rationale" not in body
    assert "Why this score" not in body
    # The rest of the brief is unaffected.
    assert "assessment-brief" in body


async def test_the_five_key_point_groups_render_in_order(client, db_session, admin):
    """B3. The grouped branch loops `key_point_groups`, so the two groups added
    by the five-group contract (C1) must appear with no markup change — in the
    document's order, not the object's insertion order. Seeded out of order on
    purpose."""
    _, assessment = await _seed(db_session)
    assessment.key_points = {
        "commercial_potential": ["Comm point"],
        "significance": ["Sig point"],
        "key_questions": ["Question point"],
        "innovation": ["Innov point"],
        "clinical_actionability": ["Actionability point"],
    }
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    # Scoped to the key-points column, which ends where the signals card
    # begins: the rubric dimension titles further down the page must not be
    # able to satisfy an index() lookup.
    block = body[
        body.index("assessment-brief-keypoints") : body.index("assessment-signals")
    ]
    labels = (
        "Significance",
        "Innovation",
        "Clinical actionability",
        "Key questions / experiments",
        "Commercial potential",
    )
    for label in labels:
        assert label in block, label
    positions = [block.index(label) for label in labels]
    assert positions == sorted(positions), dict(zip(labels, positions, strict=True))
    for point in (
        "Sig point", "Innov point", "Actionability point",
        "Question point", "Comm point",
    ):
        assert point in block, point


# ---------------------------------------------------------------------------
# Layer-2 "hub's own words" bullets and the strengths/risks derivation
# (2026-09-14 strengths/risks explanatory-bullets plan, Task B)
# ---------------------------------------------------------------------------

HUB_STRENGTH_ONE = "HUB STRENGTH ONE: the target has a validated genetic link."
HUB_STRENGTH_TWO = "HUB STRENGTH TWO: a differentiated mechanism versus standard of care."
HUB_RISK_ONE = "HUB RISK ONE: no in vivo efficacy data yet."
HUB_RISK_TWO = "HUB RISK TWO: freedom-to-operate is unconfirmed."

ESTABLISHED_MARKER = "ESTABLISHED SENTINEL: the assay is orthogonally validated."
CONSULT_CONCERN_MARKER = "CONCERN SENTINEL: no isogenic control was run."


async def test_hub_words_bullets_render_before_the_derived_list(
    client, db_session, admin
):
    """A row with `strengths`/`risks` shows the hub's own bullets, under "In
    the hub's words", before the derived list in each column."""
    _, assessment = await _seed(db_session)
    assessment.strengths = [HUB_STRENGTH_ONE, HUB_STRENGTH_TWO]
    assessment.risks = [HUB_RISK_ONE, HUB_RISK_TWO]
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    strengths, risks, _unestablished = _signal_columns(body)

    assert "In the hub's words" in strengths
    assert HUB_STRENGTH_ONE in strengths and HUB_STRENGTH_TWO in strengths
    assert strengths.index("In the hub's words") < strengths.index(HUB_STRENGTH_ONE)
    assert strengths.index(HUB_STRENGTH_TWO) < strengths.index("signal-derived")

    assert "In the hub's words" in risks
    assert HUB_RISK_ONE in risks and HUB_RISK_TWO in risks
    assert risks.index("In the hub's words") < risks.index(HUB_RISK_ONE)
    assert risks.index(HUB_RISK_TWO) < risks.index("signal-derived")


async def test_no_hub_words_heading_when_strengths_and_risks_are_null(
    client, db_session, admin
):
    """NULL on every pre-0049 row: no "In the hub's words" heading, and the
    column looks exactly like it did before this feature."""
    _, assessment = await _seed(db_session)
    assessment.strengths = None
    assessment.risks = None
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    strengths, risks, _unestablished = _signal_columns(body)
    assert "In the hub's words" not in strengths
    assert "In the hub's words" not in risks
    assert "Derived from the stored verdict" not in strengths
    assert "Derived from the stored verdict" not in risks


async def test_a_consults_established_items_render_as_strength_sub_bullets(
    client, db_session, admin
):
    """A consult recorded `adequate` with `established` items quotes them
    under the strength bullet, in the strengths column."""
    run = await factories.make_simulation_run(db_session)
    root_ts = f"{time.time():.6f}"
    await factories.make_agent_message(
        db_session, run=run, agent_id=SUBJECT, channel_name=CHANNEL,
        message_ts=root_ts, phase="new_post",
        content="Root post for the established-bullet test.",
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
            question="Is the assay orthogonally validated?",
            verdict_signal="adequate",
            confidence="high",
            established=[ESTABLISHED_MARKER],
            raw_opinion="not shown on this page",
        )
    )
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
        channel_name=CHANNEL, slack_ts=root_ts,
        company_or_project="Established Bullet Fixture Co",
        recommendation="conditional", weighted_score=3.20, band="conditional",
    )
    db_session.add(assessment)
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    strengths, _risks, _unestablished = _signal_columns(body)
    assert ESTABLISHED_MARKER in strengths
    assert "specialist established" in strengths


async def test_a_consults_concerns_render_as_risk_sub_bullets(
    client, db_session, admin
):
    """A consult recorded `gap` with `concerns` quotes them under the risk
    bullet, in the risks column."""
    run = await factories.make_simulation_run(db_session)
    root_ts = f"{time.time():.6f}"
    await factories.make_agent_message(
        db_session, run=run, agent_id=SUBJECT, channel_name=CHANNEL,
        message_ts=root_ts, phase="new_post",
        content="Root post for the concerns-bullet test.",
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
            question="Is there an isogenic control?",
            verdict_signal="gap",
            confidence="moderate",
            concerns=[CONSULT_CONCERN_MARKER],
            raw_opinion="not shown on this page",
        )
    )
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
        channel_name=CHANNEL, slack_ts=root_ts,
        company_or_project="Concern Bullet Fixture Co",
        recommendation="conditional", weighted_score=3.20, band="conditional",
    )
    db_session.add(assessment)
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    _strengths, risks, _unestablished = _signal_columns(body)
    assert CONSULT_CONCERN_MARKER in risks
    assert "specialist's concerns" in risks


async def _seed_live_stamped(db_session, *, scores=None, gating=None):
    from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION

    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
        channel_name=CHANNEL, company_or_project="Live Stamped Fixture Co",
        recommendation="conditional", weighted_score=3.20, band="conditional",
        rubric_version=RUBRIC_VERSION, rubric_content_hash=RUBRIC_CONTENT_HASH,
        scores=scores, gating=gating,
    )
    db_session.add(assessment)
    await db_session.flush()
    return assessment


async def test_empty_state_text_names_the_live_revisions_own_thresholds(
    client, db_session, admin
):
    """No literal "4": the empty-state prose names the row's own revision
    thresholds (4 of 5 on the live 1-5 scale)."""
    assessment = await _seed_live_stamped(db_session)
    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    strengths, risks, _unestablished = _signal_columns(body)
    assert "No dimension scored" in strengths
    assert "of 5" in strengths
    assert "at or above 4" in strengths
    assert "of 5" in risks
    assert "at or below 2" in risks


async def test_a_row_with_six_mid_scale_scores_shows_the_midscale_line(
    client, db_session, admin
):
    """Every scored dimension strictly between the two thresholds: neither
    column lists any of them, and the card says so instead of looking empty
    by omission."""
    scores = {
        "differentiation_unmet_need": 3, "scientific_credibility": 3,
        "translational_path": 3, "fundable_experiment": 3,
        "venture_potential": 3, "team_executability": 3,
    }
    assessment = await _seed_live_stamped(db_session, scores=scores)
    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    card = _signals_card(body)
    assert "signals-midscale" in card
    assert "sit mid-scale" in card
    assert "All 6 scored dimensions" in card


async def test_a_row_with_one_high_score_reports_the_remaining_midscale_count(
    client, db_session, admin
):
    """`mid_scale_count` counts scored dimensions strictly between the
    thresholds — one dimension scored 5 still leaves five mid-scale ones, and
    the line says "5 of 6", never "All 5": a universal over a count that
    excludes the very dimension listed beside it would be a false claim."""
    scores = {
        "differentiation_unmet_need": 5, "scientific_credibility": 3,
        "translational_path": 3, "fundable_experiment": 3,
        "venture_potential": 3, "team_executability": 3,
    }
    assessment = await _seed_live_stamped(db_session, scores=scores)
    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    card = _signals_card(body)
    assert "signals-midscale" in card
    assert "5 of 6 scored dimensions" in card
    assert "All " not in " ".join(card.split("signals-midscale", 1)[1].split("</p>", 1)[0].split())


async def test_the_manager_route_renders_the_signals_card(
    client, db_session, manager
):
    """The manager surface shares the same body template as the admin one."""
    _, assessment = await _seed(db_session)
    body = _main((await client.get(
        f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
    )).text)
    assert 'id="signals"' in body


async def test_a_reviewer_never_sees_the_hubs_own_bullets(client, db_session):
    """The prompt promises the hub that `strengths`/`risks` are staff-only and
    may cite unpublished results. A reviewer reaches the manager route
    (`get_review_user`) but is NOT staff (`is_staff` excludes the role by
    design), so the "In the hub's words" block must be withheld for them while
    the derived half of the card still renders. A manager, who is staff, sees
    it."""
    from src.models.user import USER_ROLE_REVIEWER

    _, assessment = await _seed(db_session)
    assessment.strengths = [HUB_STRENGTH_ONE]
    assessment.risks = [HUB_RISK_ONE]
    await db_session.flush()
    reviewer = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, email="signals-reviewer@example.org"
    )
    body = _main((await client.get(
        f"/manager/assessments/{assessment.id}", headers=auth_headers(reviewer.id)
    )).text)
    assert 'id="signals"' in body
    assert "In the hub's words" not in body
    assert HUB_STRENGTH_ONE not in body
    assert HUB_RISK_ONE not in body


async def test_hub_words_with_no_derived_entries_still_shows_the_derived_empty_state(
    client, db_session, admin
):
    """Hub bullets alone must not hide the derived half: a reader has to be
    able to tell "the derivation found nothing" from "it did not run"."""
    _, assessment = await _seed(db_session)
    assessment.strengths = [HUB_STRENGTH_ONE]
    assessment.gating = None
    assessment.scores = None
    await db_session.flush()
    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    strengths, _risks, _unestablished = _signal_columns(body)
    assert HUB_STRENGTH_ONE in strengths
    assert "Derived from the stored verdict" in strengths
    assert "signals-empty" in strengths


async def test_quoted_consult_text_is_collapsed_by_default_with_a_one_line_preview(
    client, db_session, admin
):
    """A consult entry that quotes specialist text is a <details> with NO
    `open` attribute: the summary shows label, signal and the first sentence
    only, and the full quotes sit behind the click."""
    run = await factories.make_simulation_run(db_session)
    root_ts = f"{time.time():.6f}"
    await factories.make_agent_message(
        db_session, run=run, agent_id=SUBJECT, channel_name=CHANNEL,
        message_ts=root_ts, phase="new_post",
        content="Root post for the compaction tests.",
        posted_at=time.time(),
    )
    long_concern = (
        "PREVIEW-FIRST-SENTENCE the control arm is missing. "
        + "SECOND-SENTENCE-HIDDEN this text should only appear inside the details body. " * 3
    )
    db_session.add(
        SpecialistConsult(
            simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
            thread_id=root_ts, channel_name=CHANNEL, domain="clinical",
            question="Is there a control arm?", verdict_signal="gap",
            confidence="moderate", concerns=[long_concern],
            raw_opinion="not shown on this page",
        )
    )
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
        channel_name=CHANNEL, slack_ts=root_ts,
        company_or_project="Collapsed Consult Fixture Co",
        recommendation="conditional", weighted_score=3.20, band="conditional",
    )
    db_session.add(assessment)
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    _strengths, risks, _unestablished = _signal_columns(body)
    entry = re.search(r'<details class="signal-collapsible"[^>]*>(.*?)</details>', risks, re.DOTALL)
    assert entry is not None, "the consult entry did not render as a <details>"
    assert " open" not in entry.group(0).split(">", 1)[0]
    summary = re.search(r"<summary.*?</summary>", entry.group(1), re.DOTALL).group(0)
    assert "PREVIEW-FIRST-SENTENCE" in summary
    assert "SECOND-SENTENCE-HIDDEN" not in summary
    assert "SECOND-SENTENCE-HIDDEN" in entry.group(1)


async def test_consults_in_one_domain_render_as_a_single_entry_from_the_latest(
    client, db_session, admin
):
    """Six clinical consults are one clinical row, badged with the count and
    quoting only the latest opinion; the panel card below still lists all."""
    run = await factories.make_simulation_run(db_session)
    root_ts = f"{time.time():.6f}"
    await factories.make_agent_message(
        db_session, run=run, agent_id=SUBJECT, channel_name=CHANNEL,
        message_ts=root_ts, phase="new_post",
        content="Root post for the compaction tests.",
        posted_at=time.time(),
    )
    base = datetime.now(UTC)
    for i, (signal, text) in enumerate([
        ("adequate", "EARLY-ESTABLISHED-MARKER"), ("gap", "MIDDLE-CONCERN-MARKER"),
        ("gap", "LATEST-CONCERN-MARKER"),
    ]):
        db_session.add(
            SpecialistConsult(
                simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
                thread_id=root_ts, channel_name=CHANNEL, domain="clinical",
                question=f"q{i}", verdict_signal=signal, confidence="moderate",
                concerns=[text] if signal == "gap" else None,
                established=[text] if signal == "adequate" else None,
                raw_opinion="not shown on this page",
                created_at=base.replace(microsecond=i * 1000),
            )
        )
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
        channel_name=CHANNEL, slack_ts=root_ts,
        company_or_project="Grouped Consult Fixture Co",
        recommendation="conditional", weighted_score=3.20, band="conditional",
    )
    db_session.add(assessment)
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    strengths, risks, _unestablished = _signal_columns(body)
    assert risks.count("signal-source-consult") == 1
    assert "latest of 3 consults" in risks
    assert "LATEST-CONCERN-MARKER" in risks
    assert "MIDDLE-CONCERN-MARKER" not in risks
    assert "EARLY-ESTABLISHED-MARKER" not in strengths
    assert "signal-source-consult" not in strengths


async def test_the_score_rationale_pointer_is_conditional(client, db_session, admin):
    """The footnote's jump-link to `#score-rationale` only appears when the
    row has a score rationale; the anchor is added to the amber box either
    way, so a NULL row never dangles a link at nothing that is also absent."""
    _, assessment = await _seed(db_session)
    assessment.score_rationale = "A score rationale for the pointer test."
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    assert 'href="#score-rationale"' in body
    assert 'id="score-rationale"' in body

    assessment.score_rationale = None
    await db_session.flush()
    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    assert 'href="#score-rationale"' not in body
