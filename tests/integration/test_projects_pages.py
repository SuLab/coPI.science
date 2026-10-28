"""The Projects list and details pages: access, filters, defaults, sorting and what each
viewer sees."""

import itertools
import re
from datetime import UTC, datetime, timedelta

import pytest

from src.agent.slack_client import split_for_wire
from src.models import SimulationProcessStatus, SpecialistConsult
from src.models.hub_project import (
    HubProject,
    HubProjectCall,
    HubProjectReview,
    HubProjectReviewAssignment,
)
from src.services.project_research import ledger_row_cost
from src.services.projects_report import refinement_cost
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_SLOTS = itertools.count(1)
PLAN = {
    "indication": "OPENING INDICATION",
    "target": "a kinase",
    "model": "organoids",
    "positive_control": "drug A",
    "negative_control": "vehicle",
    "standard_of_care": "drug B",
    "readout": "imaging",
    "design": "two arms",
    "time_and_budget": "six months",
    "lab_tools": ["Rosetta"],
}
CHOICE = {
    "chosen": {"use": "A new use", "why": "w", "domain": "scientific"},
    "why_chosen": "WHY THIS USE TEXT",
    "others": [{"use": "OTHER USE", "domain": "clinical", "note": "OTHER NOTE"}],
    "title": "Choice title",
    "summary": "SUMMARY TEXT",
    "plan": PLAN,
    "anchors": [{"section": "Model Systems", "item": "ANCHOR ITEM"}],
    "sources": [
        {"key": "pmid:31500698", "kind": "pubmed", "title": "PAPER TITLE",
         "domain": "scientific", "shows": "s"},
        {"key": "nct:NCT01903460", "kind": "trial", "title": "TRIAL TITLE",
         "domain": "clinical", "shows": "s"},
        {"key": "patent:US11234567B2", "kind": "patent", "title": "PATENT TITLE",
         "domain": "legal", "shows": "s"},
    ],
}
FINAL = {
    "plan": {**PLAN, "indication": "FINAL INDICATION", "lab_tools": ["Rosetta", "PyMOL"]},
    "tool_marks": [{"item": "Rosetta", "in_profile": True}, {"item": "PyMOL", "in_profile": False}],
    "hub_position": "agree",
    "lab_position": "disagree",
    "gaps": ["GAP TEXT"],
    "disagreements": ["DISAGREEMENT TEXT"],
}


async def _headers(db, role="manager", *, impersonate=None):
    user = await factories.make_user(db, user_role=role)
    return user, auth_headers(user.id, impersonate=impersonate)


async def _run(db, started=T0, **overrides):
    return await factories.make_simulation_run(db, started_at=started, **overrides)


async def _project(db, run, title, *, status="ready", agent="lab1", slot=None, reason=None,
                   finished=None, requested=T0, **overrides):
    project = HubProject(
        simulation_run_id=run.id, agent_id=agent, slot=slot or next(_SLOTS),
        status=status, reason_code=reason, requested_run_seconds=0.0, requested_at=requested,
        title=title, finished_at=finished, **overrides,
    )
    db.add(project)
    await db.flush()
    return project


_TITLE_RE = re.compile(r'class="project-card-title[^"]*">\s*<a [^>]*>([^<]+)</a>')
_TILE_RE = re.compile(
    r'class="project-tile(?: [^"]*)?">\s*<div class="project-tile-count[^"]*">\s*(\d+)\s*</div>'
    r"\s*<div[^>]*>\s*([^<]+?)\s*</div>"
)
_PLACEHOLDER = '<option value="" disabled selected>—</option>'
_DETAIL_IDS = ("brief", "evidence", "plan", "positions", "others", "research", "reviews",
               "timeline")


def _titles(html: str) -> list[str]:
    """The card titles in rendered order (the details link inside each card's title)."""
    return [title.strip() for title in _TITLE_RE.findall(html)]


def _card(html: str, project_id) -> str:
    """The markup of one project's card: from its ``id="p-…"`` to the next card or the end."""
    start = html.index(f'id="p-{project_id}"')
    following = html.find('id="p-', start + 1)
    return html[start:following if following != -1 else len(html)]


def _section(html: str, section_id: str) -> str:
    """One details-page section: from its id to the next section id in page order."""
    start = html.index(f'id="{section_id}"')
    rest = _DETAIL_IDS[_DETAIL_IDS.index(section_id) + 1:]
    ends = [html.find(f'id="{other}"', start) for other in rest]
    ends = [end for end in ends if end != -1]
    return html[start:min(ends) if ends else len(html)]


def _hidden(markup: str, name: str) -> list[str]:
    """The values of every input named ``name`` in ``markup``."""
    values = []
    for tag in _tags(markup, "input", name):
        found = re.search(r'\bvalue="([^"]*)"', tag)
        values.append(found.group(1) if found else None)
    return values


def _tags(markup: str, tag: str, name: str) -> list[str]:
    """Every ``<tag …>`` start tag named ``name`` in ``markup``, whatever its attribute order."""
    return [t for t in re.findall(rf"<{tag}\b[^>]*>", markup) if f'name="{name}"' in t]


def _without_help_intro(html: str) -> str:
    """The page minus base.html's first-sign-in box, whose "Don't show again" form returns the
    reader to the URL exactly as requested (it is not the page's own state)."""
    return re.sub(r'<div role="region" aria-label="Getting started".*?</form>\s*</div>', "",
                  html, flags=re.S)


async def _detail(client, headers, project, query=""):
    response = await client.get(f"/workspace/projects/{project.id}{query}", headers=headers)
    assert response.status_code == 200
    return response.text


async def _review(db, project, *, name="REVIEWER NAME", rating=4, fund=True, user=None,
                  created=T0, updated=None, comment=""):
    user = user or await factories.make_user(db, user_role="reviewer", name=name)
    review = HubProjectReview(project_id=project.id, reviewer_user_id=user.id,
                              reviewer_name=name, rating=rating, fund=fund, comment=comment,
                              created_at=created, updated_at=updated or created)
    db.add(review)
    await db.flush()
    return review


async def _list(client, headers, query=""):
    response = await client.get(f"/workspace/projects{query}", headers=headers)
    assert response.status_code == 200
    return response.text


async def test_access_matrix(client, db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-one", finished=T0)
    admin, admin_headers = await _headers(db_session, "admin")
    pi = await factories.make_user(db_session, user_role="pi")
    for role in ("admin", "manager", "reviewer"):
        headers = admin_headers if role == "admin" else (await _headers(db_session, role))[1]
        assert (await client.get("/workspace/projects", headers=headers)).status_code == 200
        detail = await client.get(f"/workspace/projects/{project.id}", headers=headers)
        assert detail.status_code == 200, role
    pi_headers = auth_headers(pi.id)
    impersonating = auth_headers(admin.id, impersonate=pi.id)
    for headers in (pi_headers, impersonating):
        assert (await client.get("/workspace/projects", headers=headers)).status_code == 403
        detail = await client.get(f"/workspace/projects/{project.id}", headers=headers)
        assert detail.status_code == 403
    missing = await client.get(
        "/workspace/projects/00000000-0000-0000-0000-000000000000", headers=admin_headers
    )
    assert missing.status_code == 404


async def _one_of_each(db):
    run = await _run(db, status="stopped", ended_at=T0 + timedelta(hours=5))
    await _project(db, run, "T-ready", finished=T0 + timedelta(hours=1))
    await _project(db, run, "T-not-agreed", status="not_agreed", finished=T0 + timedelta(hours=2))
    await _project(db, run, "T-not-finished", status="not_finished", reason="research_failed",
                   finished=T0 + timedelta(hours=3))
    await _project(db, run, "T-posted", status="posted")
    return run


async def test_list_defaults_to_ready_and_not_agreed(client, db_session):
    await _one_of_each(db_session)
    html = await _list(client, (await _headers(db_session))[1])
    assert sorted(_titles(html)) == ["T-not-agreed", "T-ready"]


async def test_outcome_filters(client, db_session):
    await _one_of_each(db_session)
    headers = (await _headers(db_session))[1]
    html = await _list(client, headers, "?outcome=not_finished")
    assert _titles(html) == ["T-not-finished"]
    assert "Research failed" in html
    assert _titles(await _list(client, headers, "?outcome=paused")) == ["T-posted"]


async def test_not_reviewed_by_me_filter(client, db_session):
    run = await _run(db_session)
    reviewed = await _project(db_session, run, "T-reviewed", finished=T0)
    await _project(db_session, run, "T-unreviewed", finished=T0 + timedelta(hours=1))
    viewer, headers = await _headers(db_session, "reviewer")
    db_session.add(HubProjectReview(project_id=reviewed.id, reviewer_user_id=viewer.id,
                                    reviewer_name=viewer.name, rating=3, fund=False))
    await db_session.flush()
    assert _titles(await _list(client, headers)) == ["T-unreviewed"]
    everything = await _list(client, headers, "?review=all")
    assert sorted(_titles(everything)) == ["T-reviewed", "T-unreviewed"]
    assert _titles(await _list(client, headers, "?review=all&mine=1")) == ["T-unreviewed"]


async def test_unknown_parameters_fall_back_to_defaults(client, db_session):
    await _one_of_each(db_session)
    headers = (await _headers(db_session))[1]
    html = await _list(client, headers, "?run_id=not-a-uuid&lab=nobody&outcome=bogus&mine=yes")
    assert sorted(_titles(html)) == ["T-not-agreed", "T-ready"]


async def test_default_run_is_the_newest_with_projects(client, db_session):
    older = await _run(db_session, started=T0)
    await _project(db_session, older, "T-older-run", finished=T0)
    await _run(db_session, started=T0 + timedelta(days=1))
    html = await _list(client, (await _headers(db_session))[1])
    assert _titles(html) == ["T-older-run"]
    assert f'value="{older.id}" selected' in html


async def test_sort_order(client, db_session):
    run = await _run(db_session)
    await _project(db_session, run, "T-early", finished=T0 + timedelta(hours=1))
    await _project(db_session, run, "T-late", status="not_agreed", finished=T0 + timedelta(hours=2))
    await _project(db_session, run, "T-open-old", status="posted", requested=T0)
    await _project(db_session, run, "T-open-new", status="refining",
                   requested=T0 + timedelta(hours=3))
    headers = (await _headers(db_session))[1]
    assert _titles(await _list(client, headers)) == ["T-late", "T-early"]
    assert _titles(await _list(client, headers, "?outcome=paused")) == ["T-open-new", "T-open-old"]


async def test_empty_state_text(client, db_session):
    run = await _run(db_session)
    await _project(db_session, run, "T-only-open", status="posted")
    html = await _list(client, (await _headers(db_session))[1])
    assert "No projects match these filters in this run." in html


async def test_open_project_of_a_finalized_run_shows_not_finished_run_finalized(
    client, db_session
):
    finalized = await _run(db_session, status="stopped", finalized_at=T0 + timedelta(hours=2))
    await _project(db_session, finalized, "T-finalized-open", status="posted")
    stopped = await _run(db_session, started=T0 - timedelta(days=1), status="stopped")
    await _project(db_session, stopped, "T-stopped-open", status="posted")
    headers = (await _headers(db_session))[1]
    html = await _list(client, headers, f"?run_id={finalized.id}&outcome=not_finished")
    assert _titles(html) == ["T-finalized-open"]
    assert "The run was finalized first" in html
    html = await _list(client, headers, f"?run_id={stopped.id}&outcome=paused")
    assert _titles(html) == ["T-stopped-open"]
    assert "Paused" in html


async def test_open_project_of_the_live_run_reads_in_progress(client, db_session):
    run = await _run(db_session)
    await _project(db_session, run, "T-live-open", status="refining")
    status = await db_session.get(SimulationProcessStatus, 1) or SimulationProcessStatus(id=1)
    status.state, status.simulation_run_id = "running", run.id
    status.updated_at = datetime.now(UTC)
    db_session.add(status)
    await db_session.flush()
    html = await _list(client, (await _headers(db_session))[1], "?outcome=paused")
    assert _titles(html) == ["T-live-open"]
    assert "In progress" in html


async def _detailed_project(db, *, status="not_agreed", choice=CHOICE, final=FINAL,
                           truncated=False, **overrides):
    """A closed project with a logged posting, the lab's reply, two hub posts in the thread
    and one scientific consult made between them."""
    run = await _run(db)
    fields = dict(
        status=status, finished=T0 + timedelta(hours=4), choice=choice, final=final,
        posting_ts="100.000100", reply_ts="200.000200", posted_at=T0 + timedelta(hours=1),
    )
    fields.update(overrides)
    project = await _project(db, run, "T-detailed", **fields)
    base = T0.timestamp()
    await factories.make_agent_message(
        db, run=run, agent_id="blackbird", channel_name="general", phase="hub_project",
        message_ts="100.000100", content="POSTING TEXT", sender_name="BlackbirdBot",
        posted_at=base + 100,
    )
    await factories.make_agent_message(
        db, run=run, agent_id="lab1", channel_name="general", phase="project_reply",
        message_ts="200.000200", content="LAB REPLY TEXT", sender_name="Lab1Bot",
        posted_at=base + 200,
    )
    await factories.make_agent_message(
        db, run=run, agent_id="blackbird", channel_name="general", phase="thread_reply",
        message_ts="300.000300", thread_ts="200.000200", content="HUB THREAD TEXT",
        sender_name="BlackbirdBot", posted_at=base + 300,
    )
    await factories.make_agent_message(
        db, run=run, agent_id="blackbird", channel_name="general", phase="thread_reply",
        message_ts="400.000400", thread_ts="200.000200", content="HUB SECOND TEXT",
        sender_name="BlackbirdBot", posted_at=base + 400,
    )
    db.add(SpecialistConsult(
        simulation_run_id=run.id, agent_id="blackbird", thread_id="200.000200",
        domain="scientific", question="CONSULT QUESTION", verdict_signal="blocking",
        confidence="moderate", raw_opinion="RAW OPINION TEXT", truncated=truncated,
        concerns=["CONSULT CONCERN"], established=["CONSULT ESTABLISHED"],
        questions_to_ask=["CONSULT ASK"], created_at=T0 + timedelta(seconds=350),
    ))
    db.add(HubProjectCall(project_id=project.id, step="choose", model="claude-opus-5",
                          status="done", usage_by_model=None))
    await db.flush()
    return run, project


async def test_detail_shows_plan_positions_gaps_sources_and_reviews(client, db_session):
    """The details page shows the brief, plan, positions, others, research, reviews and
    the conversation of a closed project."""
    _, project = await _detailed_project(db_session)
    await _review(db_session, project, name="AUTHOR NAME", comment="REVIEW TEXT")
    _, headers = await _headers(db_session, "reviewer")
    html = await _detail(client, headers, project)
    brief = _section(html, "brief")
    for text in ("T-detailed", "SUMMARY TEXT", "WHY THIS USE TEXT", "Model Systems", "ANCHOR ITEM",
                 "In one minute", "Why this use", "Key points", "Built on"):
        assert text in brief, text
    plan = _section(html, "plan")
    assert plan.index("Indication") < plan.index("Target or mechanism") < plan.index(
        "Time and budget"
    )
    assert "FINAL INDICATION" in plan
    assert plan.index("As first posted") < plan.index("OPENING INDICATION")
    assert "The lab's tools and models used" not in plan
    assert "Rosetta" in plan and "PyMOL" in plan
    positions = _section(html, "positions")
    for text in ("Agreed", "Not agreed", "Gaps", "GAP TEXT", "Disagreements",
                 "DISAGREEMENT TEXT"):
        assert text in positions, text
    others = _section(html, "others")
    assert "Also considered (1)" in others
    assert "OTHER USE" in others and "OTHER NOTE" in others
    research = _section(html, "research")
    assert "Research (3 sources)" in research
    for text in ("Papers", "Clinical trials", "Patents", "PAPER TITLE", "TRIAL TITLE",
                 "PATENT TITLE", "found by the scientific specialist",
                 "found by the clinical specialist"):
        assert text in research, text
    assert 'href="https://pubmed.ncbi.nlm.nih.gov/31500698/"' in research
    assert 'href="https://clinicaltrials.gov/study/NCT01903460"' in research
    assert "US11234567B2" in research and "US11234567B2</a>" not in research
    reviews = _section(html, "reviews")
    assert "AUTHOR NAME" in reviews and "REVIEW TEXT" in reviews
    assert 'action="/reviews/projects/' in reviews
    timeline = _section(html, "timeline")
    assert (timeline.index("POSTING TEXT") < timeline.index("LAB REPLY TEXT")
            < timeline.index("HUB THREAD TEXT"))
    assert "/help/projects#detail" in html


async def test_reviewer_detail_shows_no_labels_and_no_cost(client, db_session):
    run, project = await _detailed_project(db_session)
    _, headers = await _headers(db_session, "reviewer")
    html = (await client.get(f"/workspace/projects/{project.id}", headers=headers)).text
    lowered = html.lower()
    for word in ("blocking", "adequate", "moderate", "raw opinion text"):
        assert word not in lowered, word
    assert "CONSULT QUESTION" in html and "CONSULT CONCERN" in html
    assert "CONSULT ESTABLISHED" in html and "CONSULT ASK" in html
    assert "Cost:" not in html
    assert "llm-calls" not in html
    assert 'action="/reviews/projects/' in html


async def test_admin_sees_research_and_refinement_cost_and_the_calls_link(client, db_session):
    run, project = await _detailed_project(db_session)
    _, headers = await _headers(db_session, "admin")
    html = (await client.get(f"/workspace/projects/{project.id}", headers=headers)).text
    research = ledger_row_cost(None, "claude-opus-5", "choose")
    refinement = await refinement_cost(db_session, project)
    assert f"Cost: research ${research:.2f}, refinement ${refinement:.2f}" in html
    assert f'href="/admin/activity/{run.id}/llm-calls?thread=200.000200"' in html


async def test_impersonating_admin_sees_no_review_form(client, db_session):
    _, project = await _detailed_project(db_session)
    admin = await factories.make_user(db_session, user_role="admin")
    manager = await factories.make_user(db_session, user_role="manager")
    headers = auth_headers(admin.id, impersonate=manager.id)
    response = await client.get(f"/workspace/projects/{project.id}", headers=headers)
    assert response.status_code == 200
    assert "/reviews/projects/" not in response.text
    assert "Cost:" not in response.text


async def test_projects_nav_item_for_reviewers_and_staff(client, db_session):
    await _run(db_session)
    for role in ("reviewer", "manager", "admin"):
        _, headers = await _headers(db_session, role)
        html = await _list(client, headers)
        assert re.search(r'href="/workspace/projects"[^>]*>\s*Projects\s*<', html), role


# --- The list: tabs, tiles, banner, cards and the quick review ------------------------------


async def test_default_tab_is_unreviewed_with_counts(client, db_session):
    """The list opens on Unreviewed; each tab names its count; one review moves a card."""
    run = await _run(db_session)
    reviewed = await _project(db_session, run, "T-reviewed", finished=T0)
    await _project(db_session, run, "T-first", finished=T0 + timedelta(hours=1))
    await _project(db_session, run, "T-second", status="not_agreed",
                   finished=T0 + timedelta(hours=2))
    await _review(db_session, reviewed)
    headers = (await _headers(db_session))[1]
    html = await _list(client, headers)
    assert _titles(html) == ["T-second", "T-first"]
    for text in ("Unreviewed (2)", "Reviewed (1)", "All (3)"):
        assert text in html, text
    assert re.search(r'<nav\b[^>]*aria-label="Review status"', html)
    assert _titles(await _list(client, headers, "?review=reviewed")) == ["T-reviewed"]
    assert _titles(await _list(client, headers, "?review=all")) == [
        "T-second", "T-first", "T-reviewed"
    ]


async def test_tiles_render_five_counts(client, db_session):
    """Five tiles count the run's projects by outcome, ignoring the outcome filter."""
    await _one_of_each(db_session)
    headers = (await _headers(db_session))[1]
    for query in ("", "?outcome=ready"):
        tiles = {label: int(count) for count, label in _TILE_RE.findall(
            await _list(client, headers, query)
        )}
        assert tiles == {"Total": 4, "Ready": 1, "Not agreed": 1, "Not finished": 1,
                         "In progress": 1}, query


async def test_not_finished_banner_and_link(client, db_session):
    """The banner counts each not-finished reason over the whole run, whatever the filters,
    and links to the Not finished filter."""
    run = await _run(db_session)
    for title in ("T-timeout-1", "T-timeout-2"):
        await _project(db_session, run, title, status="not_finished", reason="thread_timeout",
                       finished=T0)
    await _project(db_session, run, "T-failed", status="not_finished", reason="research_failed",
                   finished=T0)
    await _project(db_session, run, "T-ready-lab2", agent="lab2", finished=T0)
    headers = (await _headers(db_session))[1]
    html = await _list(client, headers, "?lab=lab2")
    assert _titles(html) == ["T-ready-lab2"]
    assert "in this run did not finish:" in html
    timeout = re.search(r"The thread ended without a close (?:×|&times;) 2", html)
    failed = re.search(r"Research failed (?:×|&times;) 1", html)
    assert timeout and failed and timeout.start() < failed.start()
    assert "Counted across the whole run, whatever the filters." in html
    link = re.search(
        r'href="(/workspace/projects\?[^"]*outcome=not_finished[^"]*)"[^>]*>\s*'
        r"Show not finished projects",
        html,
    )
    assert link and f"run_id={run.id}" in link.group(1)


async def test_no_not_finished_banner_without_one(client, db_session):
    """A run whose projects all finished shows no not-finished banner."""
    run = await _run(db_session)
    await _project(db_session, run, "T-ready", finished=T0)
    html = await _list(client, (await _headers(db_session))[1])
    assert "did not finish:" not in html


async def test_card_shows_summary_key_points_and_reviewers(client, db_session):
    """A finished card shows the summary, key points from the close, positions, gap and
    disagreement counts, and its reviewers oldest first."""
    run = await _run(db_session)
    project = await _project(db_session, run, "T-card", status="not_agreed", finished=T0,
                             choice=CHOICE, final=FINAL, posted_at=T0)
    viewer, headers = await _headers(db_session, "reviewer")
    await _review(db_session, project, name="ALPHA REVIEWER", rating=2, fund=False)
    await _review(db_session, project, name=viewer.name, user=viewer, rating=4, fund=True,
                  created=T0 + timedelta(hours=1))
    card = _card(await _list(client, headers, "?review=all"), project.id)
    for text in ("In one minute", "SUMMARY TEXT", "Key points", "Indication", "FINAL INDICATION",
                 "Target or mechanism", "a kinase", "Time and budget", "six months",
                 "Agreed", "Not agreed", "Reviewed by", "reviewed by you"):
        assert text in card, text
    assert "OPENING INDICATION" not in card
    assert re.search(r"\b1 gaps?\b", card) and re.search(r"\b1 disagreements?\b", card)
    assert card.index("ALPHA REVIEWER") < card.index(viewer.name)
    assert "2/5" in card and "4/5" in card
    assert f'href="/workspace/projects/{project.id}?review=all"' in card


async def test_card_without_reviews_shows_a_dash(client, db_session):
    """A card nobody reviewed reads "Reviewed by: —" and is not marked as reviewed by you."""
    run = await _run(db_session)
    project = await _project(db_session, run, "T-bare", finished=T0)
    card = _card(await _list(client, (await _headers(db_session))[1]), project.id)
    assert "reviewed by you" not in card
    assert re.search(r"Reviewed by:?\s*(?:<[^>]+>\s*)*—", card)


async def test_lab_without_pi_user_is_plain_text(client, db_session):
    """A lab with no registry row, or a registry row without a user, is plain text."""
    run = await _run(db_session)
    await factories.make_agent(db_session, agent_id="unlinked")
    orphan = await _project(db_session, run, "T-orphan", agent="noregistry", finished=T0)
    unlinked = await _project(db_session, run, "T-unlinked", agent="unlinked", finished=T0)
    html = await _list(client, (await _headers(db_session))[1])
    for project, lab in ((orphan, "noregistry"), (unlinked, "unlinked")):
        card = _card(html, project.id)
        assert lab in card
        assert "/workspace/pis/" not in card


async def test_lab_links_to_pi_page(client, db_session):
    """A lab linked to a PI links to that PI's page, on the card and the details page."""
    run = await _run(db_session)
    pi = await factories.make_user(db_session, user_role="pi")
    await factories.make_agent(db_session, user=pi, agent_id="lab1")
    project = await _project(db_session, run, "T-linked", finished=T0, choice=CHOICE,
                             final=FINAL)
    headers = (await _headers(db_session))[1]
    assert f'href="/workspace/pis/{pi.id}"' in _card(await _list(client, headers), project.id)
    assert f'href="/workspace/pis/{pi.id}"' in _section(
        await _detail(client, headers, project), "brief"
    )


async def test_open_card_shows_stage_not_review_form(client, db_session):
    """An open project's card shows its stage and time, "Paused" when its run is not live,
    and no review controls."""
    run = await _run(db_session, status="stopped")
    posted = await _project(db_session, run, "T-posted", status="posted", posted_at=T0)
    untitled = await _project(db_session, run, None, status="requested")
    html = await _list(client, (await _headers(db_session))[1], "?outcome=paused")
    card = _card(html, posted.id)
    assert "Paused" in card and "Posted" in card and "posted" in card
    assert 'action="/reviews/projects/' not in card and "Quick review" not in card
    card = _card(html, untitled.id)
    assert "Untitled project" in card and "Waiting for research" in card
    assert "requested" in card
    assert 'action="/reviews/projects/' not in html


async def test_quick_review_form_fields(client, db_session):
    """The quick review posts to the project's review route with the list's surface and
    state, the next card's id ("" on the last), a "—" rating and required fields."""
    run = await _run(db_session)
    first = await _project(db_session, run, "T-first", finished=T0 + timedelta(hours=2))
    last = await _project(db_session, run, "T-last", finished=T0 + timedelta(hours=1))
    html = await _list(client, (await _headers(db_session))[1], "?sort=lab")
    assert _titles(html) == ["T-first", "T-last"]
    card = _card(html, first.id)
    assert "Quick review" in card
    assert re.search(r"<details\b[^>]*\sopen\b", card)
    assert f'action="/reviews/projects/{first.id}/review"' in card
    assert 'method="post"' in card
    assert _hidden(card, "surface") == ["projects-list"]
    assert _hidden(card, "sort") == ["lab"]
    assert _hidden(card, "next_id") == [str(last.id)]
    assert _hidden(_card(html, last.id), "next_id") == [""]
    assert _PLACEHOLDER in card
    selects = _tags(card, "select", "rating")
    assert selects and all("required" in tag for tag in selects)
    radios = _tags(card, "input", "fund")
    assert len(radios) == 2 and all('type="radio"' in radio for radio in radios)
    assert any("required" in radio for radio in radios)
    assert re.search(r'<textarea\b[^>]*\bmaxlength="10000"', card)
    assert "Submit review" in card
    assert "One review per person; you can edit yours on the project's page." in card
    assert "Rates the project itself, not how the bots performed." in card


async def test_my_review_replaces_form(client, db_session):
    """Once the viewer has reviewed a project its card shows their review and a link to edit
    it on the details page instead of the form."""
    run = await _run(db_session)
    project = await _project(db_session, run, "T-mine", finished=T0)
    viewer, headers = await _headers(db_session, "reviewer")
    await _review(db_session, project, name=viewer.name, user=viewer, rating=4, fund=True)
    card = _card(await _list(client, headers, "?review=all"), project.id)
    assert "Your review: 4/5 · Fund" in card
    assert f'href="/workspace/projects/{project.id}?review=all#reviews"' in card
    assert "edit it on the project" in card
    assert 'action="/reviews/projects/' not in card


async def test_impersonation_hides_quick_review(client, db_session):
    """Under impersonation a finished card says reviews can't be written and has no form."""
    run = await _run(db_session)
    project = await _project(db_session, run, "T-imp", finished=T0)
    admin = await factories.make_user(db_session, user_role="admin")
    manager = await factories.make_user(db_session, user_role="manager")
    html = await _list(client, auth_headers(admin.id, impersonate=manager.id))
    card = _card(html, project.id)
    assert "Reviews can't be written while impersonating." in card
    assert 'action="/reviews/projects/' not in html


async def test_detail_links_carry_list_state(client, db_session):
    """Card links and tab links carry the list's state; junk values are not echoed."""
    run = await _run(db_session)
    project = await _project(db_session, run, "T-state", finished=T0)
    headers = (await _headers(db_session))[1]
    html = await _list(client, headers, "?sort=lab&lab=lab1")
    assert f'href="/workspace/projects/{project.id}?lab=lab1&amp;sort=lab"' in html
    assert re.search(r'href="/workspace/projects\?lab=lab1&amp;review=reviewed&amp;sort=lab"',
                     html)
    html = _without_help_intro(await _list(client, headers, "?sort=bogus&review=nonsense"))
    assert f'href="/workspace/projects/{project.id}"' in html
    assert "bogus" not in html and "nonsense" not in html


async def test_empty_states_per_tab(client, db_session):
    """Each tab's empty state names why it is empty."""
    headers = (await _headers(db_session))[1]
    empty_run = await _run(db_session, started=T0 - timedelta(days=3))
    html = await _list(client, headers, f"?run_id={empty_run.id}")
    assert "No projects in this run yet — the run menu shows each run's count." in html

    lone_run = await _run(db_session, started=T0 + timedelta(days=1))
    await _project(db_session, lone_run, "T-c", finished=T0)
    html = await _list(client, headers, f"?run_id={lone_run.id}&review=reviewed")
    assert ("No reviewed projects match these filters — 1 still await a review on the "
            "Unreviewed tab.") in html
    only = await _run(db_session, started=T0 + timedelta(days=2))
    done = await _project(db_session, only, "T-d", finished=T0)
    await _review(db_session, done)
    html = await _list(client, headers, f"?run_id={only.id}")
    assert "No unreviewed projects match these filters — all 1 have a review." in html


async def test_no_rubric_in_run_bar(client, db_session):
    """The projects run bar names no rubric version: projects carry no rubric score."""
    run = await _run(db_session, config={"rubric_version": "9.8.7"})
    await _project(db_session, run, "T-rubric", finished=T0)
    html = await _list(client, (await _headers(db_session))[1])
    assert "9.8.7" not in html


# --- The details page -------------------------------------------------------------------


async def test_sections_and_ids(client, db_session):
    """The details page has one h1 "Project detail", every section id in page order, a named
    jump bar with Expand all / Collapse all, and an id per post and consult."""
    _, project = await _detailed_project(db_session)
    html = await _detail(client, (await _headers(db_session))[1], project)
    assert len(re.findall(r"<h1\b", html)) == 1
    assert re.search(r"<h1\b[^>]*>\s*Project detail\s*</h1>", html)
    positions = [html.index(f'id="{section}"') for section in _DETAIL_IDS]
    assert positions == sorted(positions)
    for anchor in ("m-1", "m-2", "m-3", "m-4", "consult-1"):
        assert f'id="{anchor}"' in html, anchor
    assert re.search(r'<nav\b[^>]*aria-label="On this page"', html)
    assert "Expand all" in html and "Collapse all" in html
    assert "/static/js/assessment_timeline.js" in html


async def test_plan_change_marks_and_tools(client, db_session):
    """A closed project's plan marks each changed field with its opening value; a tool that
    contains an anchor reads "in the lab's profile", the rest "stated by the lab"; without a
    close the plan is "Plan (as posted)" with no change marks."""
    final = {**FINAL, "plan": {**FINAL["plan"],
                               "lab_tools": ["ANCHOR ITEM assays (published)", "PyMOL"]}}
    _, project = await _detailed_project(db_session, final=final)
    headers = (await _headers(db_session))[1]
    plan = _section(await _detail(client, headers, project), "plan")
    assert plan.count("changed since posting") == 1
    assert "FINAL INDICATION" in plan
    assert plan.index("As first posted") < plan.index("OPENING INDICATION")
    assert "The lab's tools and models" in plan
    assert "The lab's tools and models used" not in plan
    tools = plan[plan.index("The lab's tools and models"):]
    assert tools.index("ANCHOR ITEM assays (published)") < tools.index("in the lab's profile")
    assert "matches: ANCHOR ITEM" in tools
    assert tools.index("PyMOL") < tools.index("stated by the lab")
    assert "not found in the lab's profile" not in plan

    run = await _run(db_session)
    opening = await _project(db_session, run, "T-open-plan", status="posted", choice=CHOICE,
                             posted_at=T0)
    plan = _section(await _detail(client, headers, opening), "plan")
    assert "Plan (as posted)" in plan
    assert "OPENING INDICATION" in plan and "changed since posting" not in plan
    assert "Rosetta" in plan and "stated by the lab" in plan


async def test_positions_labels(client, db_session):
    """Positions read Agreed / Not agreed, "No position stated" for a close without one, "No
    close recorded" without a close; a Ready project's gaps are Acknowledged limits."""
    _, project = await _detailed_project(db_session)
    headers = (await _headers(db_session))[1]
    positions = _section(await _detail(client, headers, project), "positions")
    assert "Not agreed" in positions and "Gaps" in positions
    assert "Acknowledged limits" not in positions

    run = await _run(db_session)
    ready = await _project(db_session, run, "T-ready", finished=T0, choice=CHOICE,
                           final={**FINAL, "lab_position": None, "disagreements": []})
    positions = _section(await _detail(client, headers, ready), "positions")
    assert "No position stated" in positions and "No close recorded" not in positions
    assert "Acknowledged limits" in positions and "GAP TEXT" in positions

    open_project = await _project(db_session, run, "T-open", status="posted", choice=CHOICE)
    positions = _section(await _detail(client, headers, open_project), "positions")
    assert "No close recorded" in positions and "No position stated" not in positions


async def test_evidence_summary_groups_by_domain(client, db_session):
    """The evidence summary shows each domain's latest consult and says how many earlier
    consults in that domain are in the conversation."""
    run, project = await _detailed_project(db_session)
    for domain, minute, text in (("scientific", 1, "OLD ESTABLISHED"),
                                 ("clinical", 7, "CLINICAL ESTABLISHED")):
        db_session.add(SpecialistConsult(
            simulation_run_id=run.id, agent_id="blackbird", thread_id="200.000200",
            domain=domain, question="Q", verdict_signal="adequate", confidence="high",
            raw_opinion="r", truncated=False, concerns=[], established=[text],
            questions_to_ask=[], created_at=T0 + timedelta(minutes=minute),
        ))
    await db_session.flush()
    html = await _detail(client, (await _headers(db_session))[1], project)
    evidence = _section(html, "evidence")
    for text in ("Established", "Concerns", "Questions to ask", "scientific", "clinical",
                 "CONSULT ESTABLISHED", "CLINICAL ESTABLISHED", "CONSULT CONCERN",
                 "CONSULT ASK"):
        assert text in evidence, text
    assert re.search(r"\b1 earlier consult", evidence)
    assert "OLD ESTABLISHED" not in evidence
    assert "OLD ESTABLISHED" in _section(html, "timeline")
    assert "Quoted from the specialists' stored replies" in evidence

    bare_run = await _run(db_session)
    bare = await _project(db_session, bare_run, "T-bare", finished=T0, choice=CHOICE,
                          final=FINAL)
    evidence = _section(await _detail(client, (await _headers(db_session))[1], bare),
                        "evidence")
    assert "No specialist was consulted in this project's thread." in evidence


async def test_conversation_is_collapsed_and_ordered(client, db_session):
    """The conversation is a closed disclosure; the posting, reply, posts and consult come in
    time order with their anchors, and the hub's last post is marked as closing the thread."""
    _, project = await _detailed_project(db_session)
    html = await _detail(client, (await _headers(db_session))[1], project)
    tag = re.search(r'<details\b[^>]*\bid="timeline"[^>]*>', html)
    assert tag and not re.search(r"\sopen(?:[\s>=]|$)", tag.group(0))
    timeline = _section(html, "timeline")
    order = [timeline.index(text) for text in (
        'id="m-1"', "POSTING TEXT", 'id="m-2"', "LAB REPLY TEXT", 'id="m-3"', "HUB THREAD TEXT",
        'id="consult-1"', "CONSULT QUESTION", 'id="m-4"', "HUB SECOND TEXT",
    )]
    assert order == sorted(order)
    assert timeline.count("closed the thread") == 1
    assert timeline.index("closed the thread") > timeline.index('id="m-4"')


async def test_replies_use_data_markdown_and_posting_is_plain(client, db_session):
    """The lab's reply and thread posts render through data-markdown; the posting is plain."""
    _, project = await _detailed_project(db_session)
    timeline = _section(await _detail(client, (await _headers(db_session))[1], project),
                        "timeline")
    assert 'data-markdown="LAB REPLY TEXT"' in timeline
    assert 'data-markdown="HUB THREAD TEXT"' in timeline
    assert 'data-markdown="POSTING TEXT"' not in timeline
    assert "POSTING TEXT" in timeline


async def test_full_posting_text_shown(client, db_session):
    """The posting shows its whole stored text, with a note when fewer chunks were logged
    than it splits into, and none when every chunk was logged."""
    posting = "POSTING HEAD\n" + ("A sentence of the posting body. " * 40 + "\n") * 8 + (
        "POSTING TAIL"
    )
    chunks = split_for_wire(posting)
    assert len(chunks) >= 2
    headers = (await _headers(db_session))[1]
    for logged, partial in ((chunks[:1], True), (chunks, False)):
        run = await _run(db_session)
        project = await _project(db_session, run, "T-long", status="posted", choice=CHOICE,
                                 posting_text=posting, posting_ts="100.000100", posted_at=T0)
        for index, chunk in enumerate(logged):
            await factories.make_agent_message(
                db_session, run=run, agent_id="blackbird", channel_name="general",
                phase="hub_project", message_ts=f"{100 + index}.000100",
                thread_ts=None if index == 0 else "100.000100", content=chunk,
                sender_name="BlackbirdBot", posted_at=T0.timestamp() + index,
            )
        timeline = _section(await _detail(client, headers, project), "timeline")
        assert "POSTING HEAD" in timeline and "POSTING TAIL" in timeline
        note = "Part of this posting may not have reached Slack."
        assert (note in timeline) is partial


async def test_truncated_consult_chip(client, db_session):
    """A cut-off consult carries the "reply cut off" chip and the partial-reply sentence."""
    headers = (await _headers(db_session))[1]
    _, project = await _detailed_project(db_session, truncated=True)
    html = await _detail(client, headers, project)
    assert "reply cut off" in _section(html, "timeline")
    assert "reply cut off" in _section(html, "evidence")
    assert ("This specialist's reply stopped before it finished, so what follows is partial."
            in html)
    _, whole = await _detailed_project(db_session, truncated=None)
    html = await _detail(client, headers, whole)
    assert "reply cut off" not in html


async def test_thread_unavailable_box(client, db_session):
    """With posting and reply ids but no logged rows the page says both are unavailable."""
    run = await _run(db_session)
    project = await _project(db_session, run, "T-unlogged", status="refining", choice=CHOICE,
                             posting_ts="100.000100", reply_ts="200.000200", posted_at=T0)
    timeline = _section(await _detail(client, (await _headers(db_session))[1], project),
                        "timeline")
    assert "Thread messages unavailable." in timeline
    assert "Posting unavailable." in timeline
    assert "Nothing has been posted yet." not in timeline


async def test_new_review_rating_placeholder(client, db_session):
    """The details page's new-review rating opens on a disabled, selected "—"."""
    _, project = await _detailed_project(db_session)
    reviews = _section(await _detail(client, (await _headers(db_session, "reviewer"))[1],
                                     project), "reviews")
    assert _PLACEHOLDER in reviews
    selects = _tags(reviews, "select", "rating")
    assert selects and all("required" in tag for tag in selects)
    assert _hidden(reviews, "surface") == ["project-detail"]
    for text in ("Your review rates the project itself", "Rate it from 1"):
        assert text in reviews, text


async def test_edited_marker(client, db_session):
    """A review updated after it was written reads "(edited)"; an untouched one does not."""
    _, project = await _detailed_project(db_session)
    await _review(db_session, project, name="EDITED AUTHOR", fund=False,
                  updated=T0 + timedelta(hours=1))
    await _review(db_session, project, name="FRESH AUTHOR", created=T0 + timedelta(hours=2))
    reviews = _section(await _detail(client, (await _headers(db_session))[1], project),
                       "reviews")
    assert "EDITED AUTHOR" in reviews and "FRESH AUTHOR" in reviews
    assert reviews.count("(edited)") == 1
    assert "Don't fund" in reviews


async def test_back_link_keeps_state(client, db_session):
    """The back link and every review form carry the list's validated state."""
    _, project = await _detailed_project(db_session)
    author, headers = await _headers(db_session, "reviewer")
    await _review(db_session, project, name=author.name, user=author)
    html = await _detail(client, headers, project, "?lab=lab1")
    assert re.search(r'href="/workspace/projects\?lab=lab1"[^>]*>\s*← Projects', html)
    reviews = _section(html, "reviews")
    forms = re.findall(r"<form\b[^>]*>.*?</form>", reviews, re.S)
    assert forms
    for form in forms:
        assert _hidden(form, "lab") == ["lab1"], form[:120]
        assert _hidden(form, "surface") == ["project-detail"], form[:120]
    html = _without_help_intro(await _detail(client, headers, project, "?sort=bogus&run_id=x"))
    assert re.search(r'href="/workspace/projects"[^>]*>\s*← Projects', html)
    assert "bogus" not in html


async def test_admin_raw_json_only_for_admin(client, db_session):
    """Only an admin sees "Raw choice and close", with every stored key of both."""
    _, project = await _detailed_project(db_session)
    html = await _detail(client, (await _headers(db_session, "admin"))[1], project)
    assert "Raw choice and close" in html
    raw = html[html.index("Raw choice and close"):]
    for key in (*CHOICE, *FINAL):
        assert f'"{key}"' in raw, key
    for role in ("manager", "reviewer"):
        html = await _detail(client, (await _headers(db_session, role))[1], project)
        assert "Raw choice and close" not in html, role
        assert "tool_marks" not in html, role


async def test_malformed_project_json_renders(client, db_session):
    """A project whose stored JSON has the wrong shapes renders both pages with empty
    sections, never a 500."""
    run = await _run(db_session)
    choice = {"plan": "not a dict", "sources": ["x", 3, {"kind": "pubmed"}], "others": "str",
              "anchors": "x", "summary": 5, "why_chosen": ["a"]}
    final = {"plan": [1], "gaps": "a string", "disagreements": None, "lab_tools": "x",
             "hub_position": 3}
    shaped = await _project(db_session, run, "T-malformed", status="not_agreed", finished=T0,
                            choice=choice, final=final, posted_at=T0)
    listed = await _project(db_session, run, "T-list-json", finished=T0, choice=["x"],
                            final="text")
    headers = (await _headers(db_session, "admin"))[1]
    html = await _list(client, headers, "?review=all")
    assert sorted(_titles(html)) == ["T-list-json", "T-malformed"]
    for project in (shaped, listed):
        html = await _detail(client, headers, project)
        assert "GAP" not in _section(html, "positions")


async def test_no_label_or_confidence_for_any_role(client, db_session):
    """No specialist signal, confidence or raw opinion reaches the details page of any role."""
    _, project = await _detailed_project(db_session)
    for role in ("admin", "manager", "reviewer"):
        lowered = (await _detail(client, (await _headers(db_session, role))[1], project)).lower()
        for word in ("blocking", "moderate", "raw opinion text"):
            assert word not in lowered, (role, word)


# --- Assignments (Phase 2) -----------------------------------------------------------------


async def _assign(db, project, assignee, by):
    db.add(HubProjectReviewAssignment(
        project_id=project.id, assignee_user_id=assignee.id, assignee_name=assignee.name,
        assigned_by_user_id=by.id, assigned_by_name=by.name,
    ))
    await db.flush()


async def test_card_and_detail_show_assigned_names(client, db_session):
    """An assigned project names its assignees on the card and in the Reviews card."""
    run = await _run(db_session)
    project = await _project(db_session, run, "T-assigned", finished=T0, choice=CHOICE,
                             final=FINAL)
    plain = await _project(db_session, run, "T-plain", finished=T0)
    manager, headers = await _headers(db_session)
    assignee = await factories.make_user(db_session, user_role="reviewer", name="ASSIGNEE NAME")
    await _assign(db_session, project, assignee, manager)
    html = await _list(client, headers)
    assert "Assigned:" in _card(html, project.id) and "ASSIGNEE NAME" in _card(html, project.id)
    assert "Assigned:" not in _card(html, plain.id)
    reviews = _section(await _detail(client, headers, project), "reviews")
    assert "Assigned (1)" in reviews and "ASSIGNEE NAME" in reviews
    reviews = _section(await _detail(client, headers, plain), "reviews")
    assert "No one assigned." in reviews


async def test_assign_form_only_for_staff(client, db_session):
    """Only staff, not impersonating, get the Assign and Remove forms."""
    run = await _run(db_session)
    project = await _project(db_session, run, "T-assign", finished=T0, choice=CHOICE,
                             final=FINAL)
    manager, manager_headers = await _headers(db_session)
    assignee = await factories.make_user(db_session, user_role="reviewer", name="ASSIGNEE NAME")
    await _assign(db_session, project, assignee, manager)
    html = await _detail(client, manager_headers, project)
    assert f'action="/reviews/projects/{project.id}/assign"' in html
    assert f'action="/reviews/projects/{project.id}/unassign"' in html
    assert "Assign to" in html
    admin = await factories.make_user(db_session, user_role="admin")
    for headers in ((await _headers(db_session, "reviewer"))[1],
                    auth_headers(admin.id, impersonate=manager.id)):
        html = await _detail(client, headers, project)
        assert f'action="/reviews/projects/{project.id}/assign"' not in html
        assert f'action="/reviews/projects/{project.id}/unassign"' not in html
        assert "Assign to" not in html


async def test_assigned_to_me_filter_and_counts(client, db_session):
    """"Assigned to me" keeps only the viewer's assigned projects, before tabs and tiles."""
    run = await _run(db_session)
    mine = await _project(db_session, run, "T-mine", finished=T0)
    await _project(db_session, run, "T-other", finished=T0 + timedelta(hours=1))
    viewer, headers = await _headers(db_session, "reviewer")
    manager = await factories.make_user(db_session, user_role="manager")
    await _assign(db_session, mine, viewer, manager)
    html = await _list(client, headers)
    assert "Assigned to me" in html
    assert sorted(_titles(html)) == ["T-mine", "T-other"]
    html = await _list(client, headers, "?assignment=mine")
    assert _titles(html) == ["T-mine"]
    assert "All (1)" in html and "Unreviewed (1)" in html
    tiles = {label: int(count) for count, label in _TILE_RE.findall(html)}
    assert tiles["Total"] == 1
    assert _hidden(_card(html, mine.id), "assignment") == ["mine"]


def _project_chat(app, on: bool = True) -> None:
    """Both switches the project chat needs (spec §8.3)."""
    app.state.assessment_chat_enabled = on
    app.state.project_chat_enabled = on


async def test_detail_drawer_only_when_the_chat_is_available(client, asgi_app, db_session):
    """The drawer renders for a finished project with both switches on; never with either
    switch off, on an open project, or under impersonation (Task 3.4)."""
    run, project = await _detailed_project(db_session)
    open_project = await _project(db_session, run, "T-open-chat", status="posted",
                                  posting_ts="900.000900", posted_at=T0)
    manager, headers = await _headers(db_session)
    drawer, history = 'id="assessment-chat"', f'historyUrl: "/project-chat/{project.id}"'
    _project_chat(asgi_app)
    html = await _detail(client, headers, project)
    assert drawer in html and history in html and "Ask about this project" in html
    assert drawer not in await _detail(client, headers, open_project)
    for switch in ("assessment_chat_enabled", "project_chat_enabled"):
        _project_chat(asgi_app)
        setattr(asgi_app.state, switch, False)
        html = await _detail(client, headers, project)
        assert drawer not in html and "data-chat-ask" not in html, switch
    _project_chat(asgi_app)
    admin = await factories.make_user(db_session, user_role="admin")
    html = await _detail(client, auth_headers(admin.id, impersonate=manager.id), project)
    assert drawer not in html and "data-chat-ask" not in html
    assert "Chat unavailable while impersonating" in html


async def test_inline_asks_sit_in_their_sections(client, asgi_app, db_session):
    """With the chat on, each opening question about a card is also an "Ask:" button at the
    end of that card: Brief, Evidence, Plan and Positions (Task 3.5)."""
    _, project = await _detailed_project(db_session)
    _project_chat(asgi_app)
    html = await _detail(client, (await _headers(db_session))[1], project)
    for section in ("brief", "evidence", "plan", "positions"):
        assert "data-chat-ask" in _section(html, section), section


async def test_card_chat_link_only_on_finished_cards_with_the_chat(client, asgi_app, db_session):
    """The card's "chat" button opens the details page on #chat, only for a finished project
    and only while the chat is available (Task 3.5)."""
    run = await _run(db_session)
    finished = await _project(db_session, run, "T-finished-chat", finished=T0, choice=CHOICE,
                              final=FINAL)
    open_project = await _project(db_session, run, "T-open-chat", status="posted",
                                  posted_at=T0)
    _, headers = await _headers(db_session)
    _project_chat(asgi_app)
    card = _card(await _list(client, headers), finished.id)
    assert 'class="project-chat-link' in card
    assert f'href="/workspace/projects/{finished.id}#chat"' in card
    assert "project-chat-link" not in _card(await _list(client, headers, "?outcome=paused"),
                                            open_project.id)
    _project_chat(asgi_app, on=False)
    assert "project-chat-link" not in await _list(client, headers)


async def test_open_card_shows_assigned_names(client, db_session):
    """An open project assigned from its details page names its assignees on the list too."""
    run = await _run(db_session)
    open_project = await _project(db_session, run, "T-open-assigned", status="posted",
                                  posted_at=T0)
    manager, headers = await _headers(db_session)
    assignee = await factories.make_user(db_session, user_role="reviewer", name="ASSIGNEE NAME")
    await _assign(db_session, open_project, assignee, manager)
    card = _card(await _list(client, headers, "?outcome=paused"), open_project.id)
    assert "Assigned: ASSIGNEE NAME" in card


async def test_not_finished_banner_link_shows_every_tab(client, db_session):
    """The banner counts not-finished projects across the run, reviewed or not, so its link
    opens the All tab."""
    run = await _run(db_session)
    await _project(db_session, run, "T-nf", status="not_finished", reason="thread_timeout",
                   finished=T0)
    html = await _list(client, (await _headers(db_session))[1])
    assert (f'href="/workspace/projects?run_id={run.id}&amp;outcome=not_finished&amp;review=all"'
            in html)
