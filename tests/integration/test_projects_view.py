"""The Projects read models (``src.services.projects_view``): the list's tabs, tiles, banner
reasons, sorts and stage times, and the details' posting, plan, positions, timeline,
evidence, reviews, assignments and admin-only keys."""

import itertools
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from src.models import SpecialistConsult
from src.models.hub_project import HubProject, HubProjectReview, HubProjectReviewAssignment
from src.services import projects_view
from src.services.projects_view import build_project_detail, build_projects_list
from tests import factories

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BASE = T0.timestamp()
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
CHOICE = {"title": "Choice title", "summary": "SUMMARY TEXT", "plan": PLAN}
FINAL = {
    "plan": {**PLAN, "indication": "FINAL INDICATION", "lab_tools": ["Rosetta", "PyMOL"]},
    "hub_position": "agree",
    "lab_position": "disagree",
    "gaps": ["GAP TEXT"],
    "disagreements": ["DISAGREEMENT TEXT"],
}


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


async def _review(db, project, user, *, rating=3, fund=False, **overrides):
    review = HubProjectReview(project_id=project.id, reviewer_user_id=user.id,
                              reviewer_name=user.name, rating=rating, fund=fund, **overrides)
    db.add(review)
    await db.flush()
    return review


async def _message(db, run, *, agent, phase, ts, at, content, thread_ts=None, sender=None):
    return await factories.make_agent_message(
        db, run=run, agent_id=agent, channel_name="general", phase=phase, message_ts=ts,
        thread_ts=thread_ts, content=content, sender_name=sender or f"{agent}Bot",
        posted_at=BASE + at,
    )


def _consult(run, *, at, domain="scientific", thread="200.000200", **overrides):
    values = dict(
        simulation_run_id=run.id, agent_id="blackbird", thread_id=thread, domain=domain,
        question="CONSULT QUESTION", verdict_signal="adequate", confidence="high",
        raw_opinion="opinion", concerns=["CONCERN"], established=["ESTABLISHED"],
        questions_to_ask=["ASK"], created_at=datetime.fromtimestamp(BASE + at, UTC),
    )
    values.update(overrides)
    return SpecialistConsult(**values)


async def _list(db, viewer, **kwargs):
    values = dict(viewer=viewer, impersonating=False, run_id=None, lab=None, outcome=None,
                  review=None, sort=None, mine=False)
    values.update(kwargs)
    return await build_projects_list(db, **values)


async def _detail(db, project, *, viewer=None, is_admin=False, impersonating=False):
    view = await build_project_detail(db, project.id, viewer=viewer, is_admin=is_admin,
                                      impersonating=impersonating)
    return view["project"]


def _titles(view):
    return [card["title"] for card in view["projects"]]


# --- the list ------------------------------------------------------------------------------


async def _four(db):
    run = await _run(db)
    reviewed = await _project(db, run, "T-reviewed", finished=T0 + timedelta(hours=1))
    await _project(db, run, "T-ready", finished=T0 + timedelta(hours=2))
    await _project(db, run, "T-not-agreed", status="not_agreed", finished=T0 + timedelta(hours=3))
    await _project(db, run, "T-not-finished", status="not_finished", reason="research_failed",
                   finished=T0 + timedelta(hours=4))
    author = await factories.make_user(db, user_role="manager")
    await _review(db, reviewed, author)
    return run


def _tiles(view):
    return {tile["kind"]: tile["count"] for tile in view["tiles"]}


async def test_review_counts_and_tiles(db_session):
    await _four(db_session)
    viewer = await factories.make_user(db_session, user_role="manager")
    view = await _list(db_session, viewer)
    assert view["review_counts"] == {"unreviewed": 2, "reviewed": 1, "all": 3}
    assert [t["label"] for t in view["tiles"]] == [
        "Total", "Ready", "Not agreed", "Not finished", "In progress",
    ]
    expected = {"total": 3, "ready": 1, "not_agreed": 1, "not_finished": 1, "in_progress": 0}
    assert _tiles(view) == expected
    filtered = await _list(db_session, viewer, outcome="not_finished")
    assert _tiles(filtered) == expected
    assert _titles(filtered) == ["T-not-finished"]


async def test_tabs_filter_cards(db_session):
    await _four(db_session)
    viewer = await factories.make_user(db_session, user_role="manager")
    assert _titles(await _list(db_session, viewer, review="reviewed")) == ["T-reviewed"]
    assert sorted(_titles(await _list(db_session, viewer))) == ["T-not-agreed", "T-ready"]
    assert len((await _list(db_session, viewer, review="all"))["projects"]) == 3


async def test_not_finished_reasons_are_run_wide(db_session):
    run = await _run(db_session)
    for agent in ("lab1", "lab1"):
        await _project(db_session, run, "T-timeout", status="not_finished", agent=agent,
                       reason="thread_timeout", finished=T0)
    await _project(db_session, run, "T-failed", status="not_finished", agent="lab2",
                   reason="research_failed", finished=T0)
    viewer = await factories.make_user(db_session, user_role="manager")
    view = await _list(db_session, viewer, lab="lab2")
    assert view["not_finished_reasons"] == [
        {"label": "The thread ended without a close", "count": 2},
        {"label": "Research failed", "count": 1},
    ]


async def test_not_finished_without_reason_reads_no_reason_recorded(db_session):
    run = await _run(db_session)
    await _project(db_session, run, "T-none", status="not_finished", finished=T0)
    viewer = await factories.make_user(db_session, user_role="manager")
    view = await _list(db_session, viewer, outcome="not_finished")
    assert view["projects"][0]["reason_label"] == "No reason recorded"
    assert view["not_finished_reasons"] == [{"label": "No reason recorded", "count": 1}]


async def _sortable(db):
    run = await _run(db)
    await _project(db, run, "T-b", agent="b-lab", finished=T0 + timedelta(hours=1))
    await _project(db, run, "T-a", agent="a-lab", status="not_agreed",
                   finished=T0 + timedelta(hours=2))
    await _project(db, run, "T-c", agent="c-lab", finished=T0 + timedelta(hours=3))
    return run


async def test_sorts(db_session):
    await _sortable(db_session)
    viewer = await factories.make_user(db_session, user_role="manager")
    assert _titles(await _list(db_session, viewer)) == ["T-c", "T-a", "T-b"]
    assert _titles(await _list(db_session, viewer, sort="lab")) == ["T-a", "T-b", "T-c"]
    assert _titles(await _list(db_session, viewer, sort="outcome")) == ["T-c", "T-b", "T-a"]


async def test_outcome_sort_puts_not_finished_after_not_agreed(db_session):
    run = await _run(db_session)
    await _project(db_session, run, "T-nf", status="not_finished", reason="no_idea",
                   finished=T0 + timedelta(hours=3))
    viewer = await factories.make_user(db_session, user_role="manager")
    # Only one finished kind passes any outcome filter but the default; the order key is
    # checked directly through the cards of the Not finished filter and the order map.
    view = await _list(db_session, viewer, outcome="not_finished", sort="outcome")
    assert _titles(view) == ["T-nf"]
    order = projects_view._OUTCOME_ORDER
    assert order["ready"] < order["not_agreed"] < order["not_finished"] < order["paused"]
    assert order["paused"] == order["in_progress"]


async def test_next_id_follows_rendered_order(db_session):
    await _sortable(db_session)
    viewer = await factories.make_user(db_session, user_role="manager")
    cards = (await _list(db_session, viewer, sort="lab"))["projects"]
    assert [c["next_id"] for c in cards] == [str(cards[1]["id"]), str(cards[2]["id"]), ""]


async def test_stage_labels_and_times(db_session):
    run = await _run(db_session, status="stopped")
    refining = await _project(db_session, run, "T-refining", status="refining",
                              reply_ts="200.000200", posted_at=T0)
    researching = await _project(db_session, run, "T-researching", status="researching",
                                 updated_at=T0 + timedelta(minutes=5))
    await _message(db_session, run, agent="lab1", phase="project_reply", ts="200.000200",
                   at=200, content="REPLY")
    viewer = await factories.make_user(db_session, user_role="manager")
    cards = {c["id"]: c for c in (await _list(db_session, viewer, outcome="paused"))["projects"]}
    card = cards[refining.id]
    assert card["stage"] == "refining" and card["stage_label"] == "Refining"
    assert card["stage_time"] == datetime.fromtimestamp(BASE + 200, UTC)
    assert card["stage_time_label"] == "lab replied"
    assert card["stage_paused"] is True
    card = cards[researching.id]
    assert card["stage_label"] == "Researching"
    assert card["stage_time"] == T0 + timedelta(minutes=5)
    assert card["stage_time_label"] == "last activity"


async def test_run_bar_has_no_rubric_version(db_session):
    run = await _run(db_session, config={"rubric_version": "3.5.0"})
    await _project(db_session, run, "T-one", finished=T0)
    viewer = await factories.make_user(db_session, user_role="manager")
    view = await _list(db_session, viewer)
    assert view["run_bar"]
    assert all(entry["rubric_version"] is None for entry in view["run_bar"])


async def test_assignment_filter_and_names(db_session):
    run = await _run(db_session)
    assigned = await _project(db_session, run, "T-assigned", finished=T0 + timedelta(hours=1))
    await _project(db_session, run, "T-other", finished=T0 + timedelta(hours=2))
    viewer = await factories.make_user(db_session, user_role="manager", name="Viewer Person")
    other = await factories.make_user(db_session, user_role="reviewer", name="Other Person")
    for user, minutes in ((other, 1), (viewer, 2)):
        db_session.add(HubProjectReviewAssignment(
            project_id=assigned.id, assignee_user_id=user.id, assignee_name=user.name,
            assigned_by_user_id=viewer.id, assigned_by_name=viewer.name,
            created_at=T0 + timedelta(minutes=minutes),
        ))
    await db_session.flush()
    everyone = await _list(db_session, viewer)
    assert everyone["assignment_filter"] == "all"
    names = {c["title"]: c["assigned_names"] for c in everyone["projects"]}
    assert names == {"T-assigned": ["Other Person", "Viewer Person"], "T-other": []}
    mine = await _list(db_session, viewer, assignment="mine")
    assert mine["assignment_filter"] == "mine"
    assert _titles(mine) == ["T-assigned"]
    assert mine["review_counts"]["all"] == 1
    assert mine["list_state"] == {"assignment": "mine"}


# --- the details ---------------------------------------------------------------------------


async def test_detail_posting_is_full_text_with_partial_note(db_session, monkeypatch):
    monkeypatch.setattr(projects_view, "split_for_wire", lambda text: ["A", "B"])
    run = await _run(db_session)
    project = await _project(db_session, run, "T-posting", status="posted",
                             posting_ts="100.000100", posting_text="A\nB", posted_at=T0)
    await _message(db_session, run, agent="blackbird", phase="hub_project", ts="100.000100",
                   at=100, content="A", sender="BlackbirdBot")
    timeline = (await _detail(db_session, project))["timeline"]
    assert timeline[0]["kind"] == "posting"
    assert timeline[0]["text"] == "A\nB"
    assert timeline[0]["partial"] is True
    assert timeline[0]["at"] == datetime.fromtimestamp(BASE + 100, UTC)
    await _message(db_session, run, agent="blackbird", phase="hub_project", ts="101.000100",
                   thread_ts="100.000100", at=101, content="B", sender="BlackbirdBot")
    assert (await _detail(db_session, project))["timeline"][0]["partial"] is False


async def test_posting_falls_back_to_logged_chunk(db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-chunk", status="posted",
                             posting_ts="100.000100", posted_at=T0)
    await _message(db_session, run, agent="blackbird", phase="hub_project", ts="100.000100",
                   at=100, content="FIRST CHUNK", sender="BlackbirdBot")
    posting = (await _detail(db_session, project))["timeline"][0]
    assert posting["text"] == "FIRST CHUNK" and posting["partial"] is False
    bare = await _project(db_session, run, "T-bare", status="posted",
                          posting_ts="900.000900", posted_at=T0)
    assert (await _detail(db_session, bare))["timeline"] == []


async def test_plan_once_with_change_marks(db_session):
    run = await _run(db_session)
    closed = await _project(db_session, run, "T-closed", status="not_agreed", finished=T0,
                            choice=CHOICE, final=FINAL)
    detail = await _detail(db_session, closed)
    assert detail["plan_title"] == "Plan"
    rows = {row["label"]: row for row in detail["plan_rows"]}
    assert "The lab's tools and models used" not in rows
    assert rows["Indication"]["value"] == "FINAL INDICATION"
    assert rows["Indication"]["changed"] is True
    assert rows["Indication"]["opening_value"] == "OPENING INDICATION"
    assert rows["Model"]["changed"] is False and rows["Model"]["opening_value"] is None
    assert [t["item"] for t in detail["tool_rows"]] == ["Rosetta", "PyMOL"]

    open_project = await _project(db_session, run, "T-open", status="posted", choice=CHOICE)
    detail = await _detail(db_session, open_project)
    assert detail["plan_title"] == "Plan (as posted)"
    assert not any(row["changed"] for row in detail["plan_rows"])
    assert len(detail["plan_rows"]) == 9


async def test_positions_labels(db_session):
    run = await _run(db_session)
    closed = await _project(db_session, run, "T-closed", status="ready", finished=T0,
                            final={"hub_position": "agree", "lab_position": None})
    detail = await _detail(db_session, closed)
    assert detail["hub_position"] == "Agreed"
    assert detail["lab_position"] == "No position stated"
    assert detail["has_close"] is True
    open_project = await _project(db_session, run, "T-open", status="posted")
    detail = await _detail(db_session, open_project)
    assert detail["hub_position"] == detail["lab_position"] == "No close recorded"


async def _threaded(db, *, final=None):
    run = await _run(db)
    project = await _project(db, run, "T-thread", status="ready" if final else "refining",
                             finished=T0 if final else None, final=final,
                             posting_ts="100.000100", posting_text="POSTING",
                             reply_ts="200.000200", posted_at=T0)
    await _message(db, run, agent="blackbird", phase="hub_project", ts="100.000100", at=100,
                   content="POSTING", sender="BlackbirdBot")
    await _message(db, run, agent="lab1", phase="project_reply", ts="200.000200", at=200,
                   content="REPLY")
    await _message(db, run, agent="blackbird", phase="thread_reply", ts="300.000300",
                   thread_ts="200.000200", at=300, content="HUB ONE", sender="BlackbirdBot")
    await _message(db, run, agent="lab1", phase="thread_reply", ts="400.000400",
                   thread_ts="200.000200", at=400, content="LAB TWO")
    return run, project


async def test_timeline_interleaves_consults_with_anchors(db_session):
    run, project = await _threaded(db_session)
    db_session.add(_consult(run, at=350))
    await db_session.flush()
    detail = await _detail(db_session, project)
    timeline = detail["timeline"]
    assert [e["kind"] for e in timeline] == ["posting", "post", "post", "consult", "post"]
    assert [e["anchor"] for e in timeline] == ["m-1", "m-2", "m-3", "consult-1", "m-4"]
    assert [e.get("text") for e in timeline if e["kind"] == "post"] == [
        "REPLY", "HUB ONE", "LAB TWO",
    ]
    assert detail["counts"]["consults"] == 1 and detail["counts"]["posts"] == 3


async def test_closed_thread_marks_last_hub_post(db_session):
    run, project = await _threaded(db_session, final={"hub_position": "agree"})
    await _message(db_session, run, agent="blackbird", phase="thread_reply", ts="500.000500",
                   thread_ts="200.000200", at=500, content="CLOSE", sender="BlackbirdBot")
    posts = [e for e in (await _detail(db_session, project))["timeline"] if e["kind"] == "post"]
    assert [p["closed_thread"] for p in posts] == [False, False, False, True]
    assert posts[-1]["text"] == "CLOSE"

    _, open_project = await _threaded(db_session)
    posts = [e for e in (await _detail(db_session, open_project))["timeline"]
             if e["kind"] == "post"]
    assert not any(p["closed_thread"] for p in posts)


async def test_truncated_consult_flagged(db_session):
    run, project = await _threaded(db_session)
    db_session.add(_consult(run, at=310, domain="clinical", truncated=True))
    db_session.add(_consult(run, at=320, domain="legal", truncated=None))
    await db_session.flush()
    detail = await _detail(db_session, project)
    consults = [e for e in detail["timeline"] if e["kind"] == "consult"]
    assert [c["truncated"] for c in consults] == [True, False]
    assert [e["truncated"] for e in detail["evidence"]] == [True, False]


async def test_thread_unavailable(db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-lost", status="refining",
                             reply_ts="200.000200", posted_at=T0)
    assert (await _detail(db_session, project))["thread_unavailable"] is True
    _, threaded = await _threaded(db_session)
    assert (await _detail(db_session, threaded))["thread_unavailable"] is False


async def test_evidence_latest_per_domain(db_session):
    run, project = await _threaded(db_session)
    db_session.add(_consult(run, at=310, concerns=["EARLY"]))
    db_session.add(_consult(run, at=320, domain="clinical"))
    db_session.add(_consult(run, at=350, concerns=["LATE"]))
    await db_session.flush()
    evidence = (await _detail(db_session, project))["evidence"]
    assert [e["domain"] for e in evidence] == ["scientific", "clinical"]
    scientific = evidence[0]
    assert scientific["earlier_count"] == 1
    assert scientific["concerns"] == ["LATE"]
    assert scientific["anchor"] == "consult-3"


async def test_viewer_none(db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-reviewed", finished=T0)
    author = await factories.make_user(db_session, user_role="manager")
    await _review(db_session, project, author)
    detail = await _detail(db_session, project, viewer=None)
    assert detail["my_review"] is None
    assert detail["can_review_projects"] is False
    assert detail["review_capable_users"] == []
    assert [r["can_edit"] for r in detail["reviews"]] == [False]
    own = await _detail(db_session, project, viewer=author)
    assert own["my_review"] is not None and own["reviews"][0]["can_edit"] is True


async def test_review_edited_flag(db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-edited", finished=T0)
    author = await factories.make_user(db_session, user_role="manager")
    await _review(db_session, project, author, created_at=T0, updated_at=T0 + timedelta(minutes=1))
    other = await factories.make_user(db_session, user_role="reviewer")
    await _review(db_session, project, other, created_at=T0 + timedelta(minutes=2),
                  updated_at=T0 + timedelta(minutes=2))
    detail = await _detail(db_session, project, viewer=author)
    assert [r["edited"] for r in detail["reviews"]] == [True, False]


async def test_admin_only_keys(db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-admin", finished=T0, choice=CHOICE,
                             final=FINAL)
    viewer = await factories.make_user(db_session, user_role="admin")
    admin_keys = ("raw_choice", "raw_final", "research_cost_usd", "refinement_cost_usd",
                  "calls_url")
    detail = await _detail(db_session, project, viewer=viewer, is_admin=True)
    for key in admin_keys:
        assert key in detail, key
    assert detail["raw_choice"] == CHOICE and detail["raw_final"] == FINAL
    detail = await _detail(db_session, project, viewer=viewer, is_admin=False)
    for key in admin_keys:
        assert key not in detail, key


async def test_no_signal_or_confidence_anywhere(db_session):
    run, project = await _threaded(db_session, final={"hub_position": "agree"})
    db_session.add(_consult(run, at=350, verdict_signal="blocking", confidence="moderate",
                            raw_opinion="RAW OPINION TEXT"))
    await db_session.flush()
    viewer = await factories.make_user(db_session, user_role="admin")
    text = repr(await _detail(db_session, project, viewer=viewer, is_admin=True))
    for word in ("blocking", "moderate", "RAW OPINION TEXT", "verdict_signal", "confidence"):
        assert word not in text, word
    assert "CONSULT QUESTION" in text


async def test_review_capable_users_only_for_staff(db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-roster", finished=T0)
    manager = await factories.make_user(db_session, user_role="manager", name="Mm Manager")
    reviewer = await factories.make_user(db_session, user_role="reviewer", name="Aa Reviewer")
    pending = await factories.make_user(db_session, user_role="reviewer", name="Pp Pending",
                                        access_status="pending")
    pi = await factories.make_user(db_session, user_role="pi", name="Zz PI")

    detail = await _detail(db_session, project, viewer=manager)
    roster = detail["review_capable_users"]
    ids = [entry["id"] for entry in roster]
    assert str(manager.id) in ids and str(reviewer.id) in ids
    assert str(pending.id) not in ids and str(pi.id) not in ids
    assert ids.index(str(reviewer.id)) < ids.index(str(manager.id))
    assert all(set(entry) == {"id", "name"} for entry in roster)

    assert (await _detail(db_session, project, viewer=reviewer))["review_capable_users"] == []
    impersonated = await _detail(db_session, project, viewer=manager, impersonating=True)
    assert impersonated["review_capable_users"] == []


async def test_detail_assignments_oldest_first(db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-assign", finished=T0)
    manager = await factories.make_user(db_session, user_role="manager", name="First Person")
    reviewer = await factories.make_user(db_session, user_role="reviewer", name="Second Person")
    for user, minutes in ((manager, 1), (reviewer, 2)):
        db_session.add(HubProjectReviewAssignment(
            project_id=project.id, assignee_user_id=user.id, assignee_name=user.name,
            assigned_by_user_id=manager.id, assigned_by_name=manager.name,
            created_at=T0 + timedelta(minutes=minutes),
        ))
    await db_session.flush()
    detail = await _detail(db_session, project, viewer=manager)
    assert detail["assignments"] == [
        {"assignee_user_id": str(manager.id), "assignee_name": "First Person"},
        {"assignee_user_id": str(reviewer.id), "assignee_name": "Second Person"},
    ]


async def test_list_never_selects_research(db_session, engine):
    """F11: the list defers ``HubProject.research`` (the worker's research state can be large),
    so no statement the list runs selects that column."""
    run = await _run(db_session)
    await _project(db_session, run, "T-research", finished=T0,
                   research={"sessions": ["RESEARCH STATE " * 200]})
    viewer = await factories.make_user(db_session, user_role="manager")
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        view = await _list(db_session, viewer)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    assert _titles(view) == ["T-research"]
    assert any("FROM hub_projects" in statement for statement in seen)
    assert not any(re.search(r"hub_projects\.research\b", statement) for statement in seen)


async def test_finished_sort_uses_the_displayed_outcome(db_session):
    """An open project of a finalized run shows as Not finished, so "Recently finished" sorts it
    with the finished projects (by its request time, as it has no finish time)."""
    run = await _run(db_session, status="stopped", finalized_at=T0 + timedelta(hours=5))
    await _project(db_session, run, "T-closed-nf", status="not_finished", reason="thread_timeout",
                   finished=T0)
    await _project(db_session, run, "T-open-nf", status="refining",
                   requested=T0 + timedelta(hours=1))
    viewer = await factories.make_user(db_session, user_role="manager")
    view = await _list(db_session, viewer, outcome="not_finished")
    assert _titles(view) == ["T-open-nf", "T-closed-nf"]


async def test_close_without_a_plan_shows_the_plan_as_posted(db_session):
    run = await _run(db_session)
    project = await _project(db_session, run, "T-no-final-plan", status="not_agreed",
                             finished=T0, choice=CHOICE, final={"hub_position": "agree"})
    detail = await _detail(db_session, project)
    assert detail["has_close"] is True
    assert detail["plan_title"] == "Plan (as posted)"
    assert not any(row["changed"] for row in detail["plan_rows"])


async def test_posting_logged_as_one_row_is_not_partial(db_session, monkeypatch):
    """With Slack off the engine logs a split post as one row holding the whole text."""
    monkeypatch.setattr(projects_view, "split_for_wire", lambda text: ["A", "B"])
    run = await _run(db_session)
    project = await _project(db_session, run, "T-one-row", status="posted",
                             posting_ts="100.000100", posting_text="A\nB", posted_at=T0)
    await _message(db_session, run, agent="blackbird", phase="hub_project", ts="100.000100",
                   at=100, content="A\nB", sender="BlackbirdBot")
    assert (await _detail(db_session, project))["timeline"][0]["partial"] is False


#: A persona whose Key Methods section marks the first tool of ``_TOOLED_PLAN`` and
#: nothing else, so a recompute is visible against a stored list that says otherwise.
_PROFILE = "## Key Methods and Technologies\n- Rosetta protein design\n\n## Model Systems\n"
_TOOLED_PLAN = {**PLAN, "lab_tools": ["Rosetta protein design, relaxed constraints", "PyMOL"]}


def _profile_reads(monkeypatch, text=_PROFILE):
    """Serve ``text`` as every lab's persona file and count the reads."""
    reads = []
    monkeypatch.setattr(projects_view, "persona_file_text",
                        lambda agent_id: reads.append(agent_id) or text)
    return reads


async def test_the_page_shows_the_stored_marks(db_session, monkeypatch):
    """One computation at the close is what every reader sees: a close carrying ``matched``
    and ``tool_profile_seen`` is rendered as stored, without reading the profile."""
    reads = _profile_reads(monkeypatch)
    stored = [
        {"item": "Rosetta protein design, relaxed constraints", "in_profile": False,
         "matched": None},
        {"item": "PyMOL", "in_profile": True, "matched": "molecular graphics"},
    ]
    run = await _run(db_session)
    project = await _project(
        db_session, run, "T-stored", status="not_agreed", finished=T0, choice=CHOICE,
        final={"plan": _TOOLED_PLAN, "tool_marks": stored, "tool_profile_seen": True},
    )
    detail = await _detail(db_session, project)
    assert detail["tool_rows"] == stored
    assert reads == []


async def test_a_pre_change_close_is_recomputed(db_session, monkeypatch):
    """Marks stored before the ``matched`` key existed cannot be rendered, so the page
    recomputes them from the live profile."""
    _profile_reads(monkeypatch)
    run = await _run(db_session)
    project = await _project(
        db_session, run, "T-pre-change", status="not_agreed", finished=T0, choice=CHOICE,
        final={
            "plan": _TOOLED_PLAN,
            "tool_marks": [{"item": "Rosetta protein design, relaxed constraints",
                            "in_profile": False},
                           {"item": "PyMOL", "in_profile": False}],
            "tool_profile_seen": True,
        },
    )
    detail = await _detail(db_session, project)
    assert detail["tool_rows"] == [
        {"item": "Rosetta protein design, relaxed constraints", "in_profile": True,
         "matched": "Rosetta protein design"},
        {"item": "PyMOL", "in_profile": False, "matched": None},
    ]


async def test_a_close_with_no_profile_seen_is_recomputed(db_session, monkeypatch):
    """A close written with no profile in memory stored an all-unmarked list; the page
    must not freeze it, and marks the in-profile tool from the live profile."""
    _profile_reads(monkeypatch)
    run = await _run(db_session)
    project = await _project(
        db_session, run, "T-no-profile-seen", status="not_agreed", finished=T0, choice=CHOICE,
        final={
            "plan": _TOOLED_PLAN,
            "tool_marks": [{"item": "Rosetta protein design, relaxed constraints",
                            "in_profile": False, "matched": None},
                           {"item": "PyMOL", "in_profile": False, "matched": None}],
            "tool_profile_seen": False,
        },
    )
    detail = await _detail(db_session, project)
    assert [(r["item"], r["in_profile"]) for r in detail["tool_rows"]] == [
        ("Rosetta protein design, relaxed constraints", True),
        ("PyMOL", False),
    ]
