"""The staff Interviews page: lines per post, panel folding, outcomes and access."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from src.models import (
    AssessmentDrop,
    HubDecline,
    InterviewTrajectory,
    OpportunityAssessment,
    SimulationProcessStatus,
    SpecialistConsult,
)
from src.services.interview_posts import merge_chunks, posts_hash
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

BASE = datetime.now(UTC).timestamp()


async def _post(db, run, ts, agent, content, *, root, phase=None, channel="general"):
    return await factories.make_agent_message(
        db, run=run, agent_id=agent, channel_name=channel,
        phase=phase or ("new_post" if root == ts else "thread_reply"),
        message_ts=ts, thread_ts=None if root == ts else root,
        content=content, sender_name=f"{agent}-name", posted_at=BASE + float(ts),
    )


async def _staff_headers(db, role="manager"):
    user = await factories.make_user(db, user_role=role)
    return auth_headers(user.id)


async def _interview(db, run, root="100", *, lab_reply=True):
    rows = [await _post(db, run, root, "lab1", "PITCH first sentence. FULL TEXT TAIL", root=root)]
    rows.append(await _post(db, run, "130", "blackbird", "Hub asks a question. More.", root=root))
    if lab_reply:
        rows.append(await _post(db, run, "160", "lab1", "Thanks — I read it. FULL TEXT TAIL", root=root))
    return rows


async def test_access_matrix(client, db_session):
    await factories.make_simulation_run(db_session)
    admin = await factories.make_user(db_session, user_role="admin")
    pi = await factories.make_user(db_session, user_role="pi")
    expected = {"admin": 200, "manager": 200, "reviewer": 403, "pi": 403}
    for role, status in expected.items():
        headers = auth_headers(
            admin.id if role == "admin"
            else (await factories.make_user(db_session, user_role=role)).id
        )
        response = await client.get("/workspace/interviews", headers=headers)
        assert response.status_code == status, role
    impersonated = await client.get(
        "/workspace/interviews", headers=auth_headers(admin.id, impersonate=pi.id)
    )
    assert impersonated.status_code == 403


async def test_trajectory_lines_panel_round_and_fallback(client, db_session):
    run = await factories.make_simulation_run(db_session)
    rows = await _interview(db_session, run)
    for domain, signal in (("scientific", "adequate"), ("legal", "gap")):
        db_session.add(SpecialistConsult(
            simulation_run_id=run.id, agent_id="blackbird", thread_id="100",
            domain=domain, question="q", verdict_signal=signal, confidence="high",
            raw_opinion="o", truncated=False,
            # Between the pitch (BASE + 100) and the hub reply (BASE + 130) it answers.
            created_at=datetime.fromtimestamp(BASE + 120, UTC),
        ))
    posts = merge_chunks([
        type("R", (), {"id": r.id, "agent_id": r.agent_id, "sender_name": r.sender_name,
                       "phase": r.phase, "posted_at": r.posted_at, "content": r.content})
        for r in rows
    ])
    db_session.add(InterviewTrajectory(
        simulation_run_id=run.id, thread_id="100", status="ready",
        lines={str(posts[0].first_row_id): "MODEL LINE FOR PITCH"},
        model="m", prompt_sha256_12="0" * 12, posts_sha256_12=posts_hash(posts),
    ))
    await db_session.flush()
    html = (
        await client.get(
            f"/workspace/interviews?run_id={run.id}", headers=await _staff_headers(db_session)
        )
    ).text
    assert "MODEL LINE FOR PITCH" in html
    assert "Panel:" in html and "scientific" in html
    assert "Thanks — I read it." in html
    assert "FULL TEXT TAIL" not in html
    assert html.index("Panel:") < html.index("Hub asks a question.")


async def test_changed_post_set_falls_back(client, db_session):
    run = await factories.make_simulation_run(db_session)
    rows = await _interview(db_session, run)
    db_session.add(InterviewTrajectory(
        simulation_run_id=run.id, thread_id="100", status="ready",
        lines={str(rows[0].id): "STALE MODEL LINE"},
        model="m", prompt_sha256_12="0" * 12, posts_sha256_12="f" * 12,
    ))
    await db_session.flush()
    html = (
        await client.get(
            f"/workspace/interviews?run_id={run.id}", headers=await _staff_headers(db_session)
        )
    ).text
    assert "STALE MODEL LINE" not in html
    assert "PITCH first sentence." in html


async def test_decisionless_interview_of_a_stopped_run_reads_run_ended(client, db_session):
    run = await factories.make_simulation_run(
        db_session, status="stopped", ended_at=datetime.now(UTC) - timedelta(hours=1)
    )
    await _interview(db_session, run)
    html = (
        await client.get(
            f"/workspace/interviews?run_id={run.id}", headers=await _staff_headers(db_session)
        )
    ).text
    assert "Run ended before a close" in html


async def test_unanswered_pitch_is_counted_not_listed(client, db_session):
    run = await factories.make_simulation_run(db_session)
    await _interview(db_session, run)
    await _post(db_session, run, "500", "lab2", "UNANSWERED PITCH", root="500")
    html = (
        await client.get(
            f"/workspace/interviews?run_id={run.id}", headers=await _staff_headers(db_session)
        )
    ).text
    assert "UNANSWERED PITCH" not in html
    assert "1 interviews · 1 unanswered pitches not listed" in html


async def test_empty_run_shows_no_interviews_sentence(client, db_session):
    run = await factories.make_simulation_run(db_session)
    html = (
        await client.get(
            f"/workspace/interviews?run_id={run.id}", headers=await _staff_headers(db_session)
        )
    ).text
    assert "No interviews in this run." in html


async def test_lab_pause_decision_reads_lab_pause(client, db_session):
    run = await factories.make_simulation_run(db_session)
    await _interview(db_session, run)
    await factories.make_thread_decision(
        db_session, run=run, thread_id="100", outcome="no_proposal", closed_by_role="pi_lab"
    )
    html = (
        await client.get(
            f"/workspace/interviews?run_id={run.id}", headers=await _staff_headers(db_session)
        )
    ).text
    assert "Lab ⏸️" in html


async def _page(client, db, run):
    return (
        await client.get(f"/workspace/interviews?run_id={run.id}", headers=await _staff_headers(db))
    ).text


async def _seed_outcome(db, run, rows, seed):
    """One interview's outcome record, by name; returns the assessment when one is made."""
    if seed in ("assessment", "legacy_assessment"):
        legacy = seed == "legacy_assessment"
        row = OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="lab1",
            channel_name="general", thread_id=None if legacy else "100",
            slack_ts=rows[1].message_ts if legacy else None,
            recommendation="conditional" if legacy else "advance",
            band="conditional" if legacy else "advance",
        )
        db.add(row)
        await db.flush()
        return row
    if seed == "hub_decline_row":
        db.add(HubDecline(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="lab1",
            channel_name="general", thread_id="100", reason_text="Not yet.",
        ))
    elif seed == "live":
        status = await db.get(SimulationProcessStatus, 1) or SimulationProcessStatus(id=1)
        status.state, status.simulation_run_id = "running", run.id
        status.updated_at = datetime.now(UTC)
        db.add(status)
    else:
        outcome, role = {
            "hub_decision": ("no_proposal", "scout_hub"),
            "lab_decision": ("no_proposal", "pi_lab"),
            "timeout": ("timeout", None),
            "abandoned": ("abandoned", None),
            "closed": ("proposal", None),
            "dropped": ("proposal", None),
        }[seed]
        await factories.make_thread_decision(
            db, run=run, thread_id="100", outcome=outcome, closed_by_role=role
        )
        if seed == "dropped":
            db.add(AssessmentDrop(
                simulation_run_id=run.id, agent_id="blackbird", thread_id="100",
                reason="duplicate_thread_verdict",
            ))
    await db.flush()
    return None


@pytest.mark.parametrize(
    ("seed", "label"),
    [
        ("assessment", "Verdict: advance"),
        ("legacy_assessment", "Verdict: conditional"),
        ("hub_decline_row", "Hub decline"),
        ("hub_decision", "Hub decline"),
        ("lab_decision", "Lab ⏸️"),
        ("timeout", "Timeout"),
        ("abandoned", "Abandoned"),
        ("closed", "Closed"),
        ("dropped", "Verdict dropped"),
        ("live", "Open"),
    ],
)
async def test_every_outcome_label(client, db_session, seed, label):
    run = await factories.make_simulation_run(db_session)
    rows = await _interview(db_session, run)
    assessment = await _seed_outcome(db_session, run, rows, seed)
    html = await _page(client, db_session, run)
    # The badge itself, not any page text that happens to contain the word.
    assert re.search(r'rounded-full text-xs [^"]*">' + re.escape(label), html), label
    if assessment is not None:
        assert f"/workspace/assessments/{assessment.id}" in html
    if seed == "hub_decline_row":
        assert "#declined" in html
    if seed == "hub_decision":
        assert "#declined" not in html  # no recorded decline row to link to


async def test_panel_notes_fall_back_with_their_signals(client, db_session):
    run = await factories.make_simulation_run(db_session)
    await _interview(db_session, run)
    await _post(db_session, run, "120", "blackbird",
                '🧪 Panel · legal — ⛔ blocking — asked: "q"', root="100", phase="panel_note")
    await _post(db_session, run, "121", "blackbird",
                '🧪 Panel · budget — ☑️ adequate for stage — asked: "q"', root="100",
                phase="panel_note")
    html = await _page(client, db_session, run)
    assert "Panel:" in html
    assert 'class="text-red-600">legal' in html
    assert 'class="text-green-700">budget' in html
