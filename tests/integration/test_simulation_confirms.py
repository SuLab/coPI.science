"""Confirm dialogs for the irreversible admin actions (spec §6.8, D11)."""
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    AssessmentReview,
    OpportunityAssessment,
    SimulationCommand,
    SimulationProcessStatus,
    SimulationRun,
    ThreadDecision,
)
from tests import factories
from tests.assessment_chat_support import seed_interview
from tests.flash_support import session_flashes
from tests.integration.test_admin_simulation_liveness import _alive
from tests.integration.test_finalize_run_route import _stopped_run
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _forms(html: str) -> list[tuple[str, str]]:
    """``(opening <form> tag, whole form markup)`` for every form on the page."""
    out = []
    for match in re.finditer(r"<form\b[^>]*>.*?</form>", html, re.S):
        out.append((re.match(r"<form\b[^>]*>", match.group(0)).group(0), match.group(0)))
    return out


def _form_tag(html: str, marker: str) -> str:
    """The opening <form ...> tag of the first form whose markup contains ``marker``."""
    for tag, body in _forms(html):
        if marker in body:
            return tag
    raise AssertionError(f"no form containing {marker!r}")


def _stop_forms(html: str) -> tuple[str, str]:
    """The opening tags of the announcing Stop form and of "Stop — hold open interviews"."""
    stops = [(t, b) for t, b in _forms(html) if 'action="/admin/simulation/stop"' in t]
    announcing = [t for t, b in stops if 'name="hold_open"' not in b]
    hold = [t for t, b in stops if 'name="hold_open"' in b]
    assert len(announcing) == 1 and len(hold) == 1, stops
    return announcing[0], hold[0]


def _verdict(run, thread_id, posted=False):
    return OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="c", thread_id=thread_id,
        summary_posted_at=datetime.now(UTC) if posted else None,
    )


def _closed(run, thread_id):
    return ThreadDecision(simulation_run_id=run.id, thread_id=thread_id, channel="c",
                          agent_a="blackbird", agent_b="lab", outcome="timeout")


async def test_the_announcing_stop_names_the_live_runs_counts(client, db_session, monkeypatch):
    _alive(monkeypatch, True)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    now = datetime.now(UTC)
    live = SimulationRun(status="running", started_at=now - timedelta(hours=3))
    newer = SimulationRun(status="stopped", started_at=now - timedelta(hours=1))
    db_session.add_all([live, newer])
    await db_session.flush()
    # Live run: one closed and owed, one closed and posted, two still open -> 3 owed, 2 open.
    db_session.add_all([
        _verdict(live, "closed-owed"), _closed(live, "closed-owed"),
        _verdict(live, "closed-posted", posted=True), _closed(live, "closed-posted"),
        _verdict(live, "open-1"), _verdict(live, "open-2"),
    ])
    # The newer run is the page's default selection; its numbers must not leak in.
    db_session.add_all([_verdict(newer, f"newer-{n}") for n in range(5)])
    db_session.add(SimulationProcessStatus(id=1, state="running", simulation_run_id=live.id,
                                           updated_at=now))
    await db_session.commit()

    r = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    stop, hold = _stop_forms(r.text)
    assert ('data-confirm="Posts 3 of 3 owed headlines to Slack (2 from interviews '
            'still open; at most 25 per stop); cannot be undone"') in stop
    assert "data-confirm" not in hold


async def test_the_stop_dialog_without_a_live_run_id_still_warns(client, db_session, monkeypatch):
    _alive(monkeypatch, True)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    db_session.add(SimulationProcessStatus(id=1, state="starting", updated_at=datetime.now(UTC)))
    await db_session.commit()
    r = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    stop, _hold = _stop_forms(r.text)
    assert ('data-confirm="Posts every owed headline to Slack, including interviews still '
            'open; cannot be undone"') in stop


async def test_reset_to_file_default_asks_first(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    reset = _form_tag(r.text, 'name="reset" value="true"')
    assert 'data-confirm="Reset the run-start announcement to the file default? ' in reset


async def test_finalize_asks_and_carries_the_short_id_field(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _stopped_run(db_session)
    short = str(run.id)[:8]
    r = await client.get(f"/admin/activity/{run.id}", headers=auth_headers(admin.id))
    form = _form_tag(r.text, 'action="/admin/simulation/finalize-run"')
    assert f'data-confirm="Finalize run {short}: ' in form
    assert 'name="confirm_run"' in r.text and '<label for="confirm-run"' in r.text


async def test_finalize_with_the_wrong_short_id_is_refused(client, db_session, monkeypatch):
    _alive(monkeypatch, False)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _stopped_run(db_session)
    short = str(run.id)[:8]
    for wrong in ("", "00000000", str(run.id)):
        r = await client.post(
            "/admin/simulation/finalize-run", data={"run_id": str(run.id), "confirm_run": wrong},
            headers=auth_headers(admin.id), follow_redirects=False,
        )
        assert r.headers["location"] == f"/admin/activity/{run.id}", wrong
        assert session_flashes(r) == [{
            "text": f"Type the run's short id ({short}) to confirm Finalize run.", "kind": "error",
        }], wrong
    assert (await db_session.execute(select(SimulationCommand))).scalars().all() == []


async def test_finalize_with_the_right_short_id_enqueues(client, db_session, monkeypatch):
    _alive(monkeypatch, False)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _stopped_run(db_session)
    r = await client.post(
        "/admin/simulation/finalize-run",
        data={"run_id": str(run.id), "confirm_run": f"  {str(run.id)[:8].upper()} "},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    cmd = (await db_session.execute(select(SimulationCommand))).scalar_one()
    assert cmd.payload == {"finalize": True, "run_id": str(run.id)}


async def test_review_delete_asks_first(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    interview = await seed_interview(db_session)
    review = AssessmentReview(assessment_id=interview.assessment_id, reviewer_user_id=admin.id,
                              reviewer_name=admin.name, score=3, comment="c", feedback_mode="learn")
    db_session.add(review)
    await db_session.flush()
    r = await client.get(f"/admin/assessments/{interview.assessment_id}",
                         headers=auth_headers(admin.id))
    form = _form_tag(r.text, f'action="/reviews/feedback/{review.id}/delete"')
    assert 'data-confirm="Delete this review? This cannot be undone."' in form
