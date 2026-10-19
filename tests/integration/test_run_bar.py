"""The run bar on Assessments and Discussions, and the service rules behind its badges."""
from datetime import UTC, datetime, timedelta

import pytest

from src.models import OpportunityAssessment, SimulationProcessStatus
from src.services import runs
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _two_runs(db, *, assessments_in_newer=4, **older_overrides):
    now = datetime.now(UTC)
    older = await factories.make_simulation_run(
        db, started_at=now - timedelta(days=1), **older_overrides
    )
    newer = await factories.make_simulation_run(db, started_at=now)
    for _ in range(assessments_in_newer):
        db.add(OpportunityAssessment(
            simulation_run_id=newer.id, agent_id="hub", subject_agent_id="lab-a",
            channel_name="test", weighted_score=3,
        ))
    await db.flush()
    return older, newer


async def _set_status(db, *, state, run_id, updated_at):
    row = await db.get(SimulationProcessStatus, 1)
    if row is None:
        row = SimulationProcessStatus(id=1)
        db.add(row)
    row.state = state
    row.simulation_run_id = run_id
    row.updated_at = updated_at
    await db.flush()


async def test_run_options_keep_stored_and_group_empty_runs(client, db_session):
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    await _two_runs(db_session)
    html = (await client.get("/workspace/assessments", headers=auth_headers(reviewer.id))).text
    assert 'id="assessments-run-select"' in html
    assert '<optgroup label="Runs with stored assessments">' in html
    assert '<optgroup label="Runs with no stored assessments">' in html
    assert "— 4 stored" in html and "(current)" in html
    assert "Counts are stored assessments across every lab and assignment." in html


async def test_changing_sort_on_an_older_run_keeps_the_run(client, db_session):
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    older, _ = await _two_runs(db_session)
    html = (await client.get(
        f"/workspace/assessments?run_id={older.id}&sort=recent", headers=auth_headers(reviewer.id)
    )).text
    # The header's filter form only, from its <form to its </form>.
    sort_at = html.index('id="assessments-sort-select"')
    filter_form = html[html.rindex("<form", 0, sort_at):html.index("</form>", sort_at)]
    assert f'<input type="hidden" name="run_id" value="{older.id}">' in filter_form


async def test_run_form_carries_sort_lab_review_assignment(client, db_session):
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    _, newer = await _two_runs(db_session)
    html = (await client.get(
        f"/workspace/assessments?run_id={newer.id}&sort=lab&lab=lab-a&review=all&assignment=all",
        headers=auth_headers(reviewer.id),
    )).text
    # Only the run bar's own form: the filter form carries `review` too.
    start = html.index('<form method="GET"', html.index("</h1>"))
    start = html.index('<form method="GET"', start + 1)  # the filter form comes first
    bar = html[start:html.index('id="assessments-run-select"')]
    for name, value in (("sort", "lab"), ("lab", "lab-a"), ("review", "all"), ("assignment", "all")):
        assert f'<input type="hidden" name="{name}" value="{value}">' in bar


async def test_reviewer_sees_live_and_held_badges_without_run_config(client, db_session):
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    now = datetime.now(UTC)
    live = await factories.make_simulation_run(
        db_session, started_at=now, config={"rubric_version": "9.9.9"}
    )
    held = await factories.make_simulation_run(
        db_session, started_at=now - timedelta(days=1), held_at=now - timedelta(hours=1),
        config={
            "rubric_version": "9.9.8",
            "prompt_stamps": {"arbitrary": "PRIVATE_PROMPT_STAMP"},
            "announcement_text": "PRIVATE_ANNOUNCEMENT",
        },
    )
    await _set_status(db_session, state="running", run_id=live.id, updated_at=now)
    # The badge belongs to the selected run's summary; the options carry no status.
    for run, badge in ((live, ">Live<"), (held, ">Held<")):
        html = (await client.get(
            f"/workspace/assessments?run_id={run.id}", headers=auth_headers(reviewer.id)
        )).text
        assert badge in html
        assert "rubric 9.9.8" in html
        assert "PRIVATE_PROMPT_STAMP" not in html and "PRIVATE_ANNOUNCEMENT" not in html


async def test_stale_heartbeat_is_not_live(db_session):
    now = datetime.now(UTC)
    run = await factories.make_simulation_run(db_session)
    await _set_status(db_session, state="running", run_id=run.id, updated_at=now - timedelta(seconds=121))
    assert await runs.live_run_id(db_session, now=now) is None
    await _set_status(db_session, state="running", run_id=run.id, updated_at=now - timedelta(seconds=119))
    assert await runs.live_run_id(db_session, now=now) == run.id
    await _set_status(db_session, state="idle", run_id=run.id, updated_at=now)
    assert await runs.live_run_id(db_session, now=now) is None


async def test_discussions_run_form_carries_every_filter(client, db_session):
    manager = await factories.make_user(db_session, user_role="manager")
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(db_session, run=run, phase="new_post")
    html = (await client.get(
        f"/workspace/discussions?run_id={run.id}&channel_filter=c1&status_filter=timeout"
        "&agent_filter=a&agent_filter=b",
        headers=auth_headers(manager.id),
    )).text
    assert 'id="discussions-run"' in html
    for name, value in (("channel_filter", "c1"), ("status_filter", "timeout"),
                        ("agent_filter", "a"), ("agent_filter", "b")):
        assert f'<input type="hidden" name="{name}" value="{value}">' in html
    assert ">All Runs<" in html
    assert "— 1 threads" in html


async def test_discussions_run_bar_counts_roots_and_orphan_threads(client, db_session):
    manager = await factories.make_user(db_session, user_role="manager")
    run = await factories.make_simulation_run(db_session)
    root = await factories.make_agent_message(
        db_session, run=run, phase="new_post", message_ts="500.000001"
    )
    # A decision on the root's own thread (not an orphan), and one orphan thread with two
    # decisions, which counts once.
    await factories.make_thread_decision(db_session, run=run, thread_id=root.message_ts)
    await factories.make_thread_decision(db_session, run=run, thread_id="999.000001")
    await factories.make_thread_decision(db_session, run=run, thread_id="999.000001")
    empty = await factories.make_simulation_run(
        db_session, started_at=run.started_at - timedelta(days=1)
    )
    html = (await client.get(
        f"/workspace/discussions?run_id={run.id}", headers=auth_headers(manager.id)
    )).text
    assert "— 2 threads" in html
    bar = html[html.index('id="discussions-run"'):html.index("</select>", html.index('id="discussions-run"'))]
    empty_option = bar[bar.index(f'value="{empty.id}"'):]
    assert "— 0 threads" in empty_option[:200]
