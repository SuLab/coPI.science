"""The Projects list and details pages: access, filters, defaults, sorting and what each
viewer sees."""

import itertools
import re
from datetime import UTC, datetime, timedelta

import pytest

from src.models import SimulationProcessStatus, SpecialistConsult
from src.models.hub_project import HubProject, HubProjectCall, HubProjectReview
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


def _titles(html: str) -> list[str]:
    return re.findall(r'hover:underline">(T-[^<]+)</a>', html)


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
    assert sorted(_titles(await _list(client, headers))) == ["T-reviewed", "T-unreviewed"]
    assert _titles(await _list(client, headers, "?mine=1")) == ["T-unreviewed"]


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


async def _detailed_project(db):
    run = await _run(db)
    project = await _project(
        db, run, "T-detailed", status="not_agreed", finished=T0 + timedelta(hours=4),
        choice=CHOICE, final=FINAL, posting_ts="100.000100", reply_ts="200.000200",
        posted_at=T0 + timedelta(hours=1),
    )
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
    db.add(SpecialistConsult(
        simulation_run_id=run.id, agent_id="blackbird", thread_id="200.000200",
        domain="scientific", question="CONSULT QUESTION", verdict_signal="blocking",
        confidence="moderate", raw_opinion="RAW OPINION TEXT", truncated=False,
        concerns=["CONSULT CONCERN"], established=["CONSULT ESTABLISHED"],
        questions_to_ask=["CONSULT ASK"],
    ))
    db.add(HubProjectCall(project_id=project.id, step="choose", model="claude-opus-5",
                          status="done", usage_by_model=None))
    await db.flush()
    return run, project


async def test_detail_shows_plan_positions_gaps_sources_and_reviews(client, db_session):
    _, project = await _detailed_project(db_session)
    author = await factories.make_user(db_session, user_role="manager")
    db_session.add(HubProjectReview(project_id=project.id, reviewer_user_id=author.id,
                                    reviewer_name="AUTHOR NAME", rating=4, comment="REVIEW TEXT",
                                    fund=True))
    await db_session.flush()
    _, headers = await _headers(db_session, "reviewer")
    html = (await client.get(f"/workspace/projects/{project.id}", headers=headers)).text
    final_rows = html[html.index(">Plan</h2>"):html.index(">Positions</h2>")]
    labels = re.findall(r'font-medium text-gray-700">([^<]+):</dt>', final_rows)
    assert labels[:3] == ["Indication", "Target or mechanism", "Model"]
    assert "FINAL INDICATION" in final_rows and "OPENING INDICATION" not in final_rows
    opening = html[html.index(">Opening plan</h2>"):html.index(">Conversation</h2>")]
    assert "OPENING INDICATION" in opening
    assert "Rosetta — <span class=\"text-green-700\">in the lab's profile" in html
    assert "PyMOL — <span class=\"text-amber-700\">not found in the lab's profile" in html
    assert "BlackbirdBot: Agreed" in html and "The lab: Not agreed" in html
    for text in ("GAP TEXT", "DISAGREEMENT TEXT", "WHY THIS USE TEXT", "OTHER USE", "OTHER NOTE",
                 "ANCHOR ITEM", "PAPER TITLE", "TRIAL TITLE", "PATENT TITLE", "SUMMARY TEXT",
                 "POSTING TEXT", "LAB REPLY TEXT", "HUB THREAD TEXT", "AUTHOR NAME",
                 "REVIEW TEXT", "found by the scientific specialist",
                 "found by the clinical specialist"):
        assert text in html, text
    assert 'href="https://pubmed.ncbi.nlm.nih.gov/31500698/"' in html
    assert 'href="https://clinicaltrials.gov/study/NCT01903460"' in html
    assert "US11234567B2" in html and "US11234567B2</a>" not in html
    assert html.index("POSTING TEXT") < html.index("LAB REPLY TEXT") < html.index("HUB THREAD TEXT")
    assert "/help/projects#detail" in html
    assert 'action="/reviews/projects/' in html


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
