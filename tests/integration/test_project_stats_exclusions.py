"""Project threads are not interviews: the run statistics that count interviews
(``funnel``, ``specialist_mix``, ``consult_fanout``, ``cost_per_interview``) and the lab
page's thread count leave out threads rooted at a lab's ``project_reply`` post, while
``cost_by_stage`` keeps project calls under their own stage names. Every exclusion is
NULL-safe, so thread-less calls and consults still count where they did. The admin
calls page's ``thread`` filter lists one thread's calls and its pagination keeps it."""

import re
from decimal import Decimal

import pytest

from src.models import USER_ROLE_ADMIN, LlmCallLog, SpecialistConsult
from src.models.hub_project import THREAD_OUTCOME_PROJECT_CLOSED
from src.services import simulation_stats
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

HUB = "blackbird"
LAB = "tstprojlab"
INTERVIEW_TS = "1810000000.000100"
PROJECT_TS = "1810000000.000200"
OTHER_RUN_PROJECT_TS = "1810000000.000300"
PRICED = "claude-opus-5"


def _consult(run, *, thread_id, domain, signal="adequate"):
    return SpecialistConsult(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=LAB,
        thread_id=thread_id,
        channel_name="general",
        domain=domain,
        question="Q?",
        verdict_signal=signal,
        confidence="high",
        concerns=[],
        questions_to_ask=[],
        raw_opinion="{}",
    )


async def _call(db_session, run, *, thread_ts, phase, thread_phase, agent_id=HUB, text="r"):
    return await factories.make_llm_call_log(
        db_session, run=run, agent_id=agent_id, phase=phase, thread_ts=thread_ts,
        thread_phase=thread_phase, model=PRICED, input_tokens=1000, output_tokens=100,
        cache_read_input_tokens=0, cache_creation_input_tokens=0, response_text=text,
    )


async def _seed(db_session):
    run = await factories.make_simulation_run(db_session)
    # The interview: a root, a lab reply and a decision.
    await factories.make_agent_message(
        db_session, run=run, phase="new_post", thread_ts=None, message_ts=INTERVIEW_TS,
        channel_name="general", agent_id=LAB,
    )
    await factories.make_agent_message(
        db_session, run=run, phase="thread_reply", thread_ts=INTERVIEW_TS,
        channel_name="general", agent_id=LAB,
    )
    await factories.make_thread_decision(
        db_session, run=run, thread_id=INTERVIEW_TS, outcome="proposal",
        agent_a=HUB, agent_b=LAB, channel="general",
    )
    # The project thread: the lab's reply is the root, then a lab reply in it, and a close.
    await factories.make_agent_message(
        db_session, run=run, phase="project_reply", thread_ts=None, message_ts=PROJECT_TS,
        channel_name="general", agent_id=LAB,
    )
    await factories.make_agent_message(
        db_session, run=run, phase="thread_reply", thread_ts=PROJECT_TS,
        channel_name="general", agent_id=LAB,
    )
    await factories.make_thread_decision(
        db_session, run=run, thread_id=PROJECT_TS, outcome=THREAD_OUTCOME_PROJECT_CLOSED,
        agent_a=HUB, agent_b=LAB, channel="general",
    )
    db_session.add_all([
        _consult(run, thread_id=INTERVIEW_TS, domain="scientific"),
        _consult(run, thread_id=PROJECT_TS, domain="legal", signal="blocking"),
        _consult(run, thread_id=PROJECT_TS, domain="legal", signal="blocking"),
        _consult(run, thread_id=None, domain="chemistry", signal="gap"),
    ])
    await _call(db_session, run, thread_ts=INTERVIEW_TS, phase="thread_reply", thread_phase="open")
    await _call(
        db_session, run, thread_ts=PROJECT_TS, phase="project_thread",
        thread_phase="project_refine",
    )
    # The lab's reply step: made before the thread exists, so it has no thread id.
    await _call(
        db_session, run, thread_ts=None, phase="project_reply", thread_phase="project_reply",
        agent_id=LAB,
    )
    await _call(db_session, run, thread_ts=None, phase="memory", thread_phase=None)
    await db_session.flush()
    return run


async def test_funnel_counts_only_the_interview(db_session):
    run = await _seed(db_session)
    assert (await simulation_stats.funnel(db_session, run.id)).interviews_opened == 1


async def test_specialist_mix_leaves_out_project_consults_but_keeps_thread_less_ones(db_session):
    run = await _seed(db_session)
    mix = {m.domain: m for m in await simulation_stats.specialist_mix(db_session, run.id)}
    assert set(mix) == {"scientific", "chemistry"}
    assert mix["scientific"].adequate == 1
    assert mix["chemistry"].gap == 1, "a consult with no thread still counts"


async def test_consult_fanout_counts_only_the_interview(db_session):
    run = await _seed(db_session)
    buckets = await simulation_stats.consult_fanout(db_session, run.id)
    assert [(b.consult_count, b.interview_count) for b in buckets] == [(1, 1)]


async def test_cost_per_interview_drops_the_project_thread_and_keeps_the_unattributed_bucket(
    db_session,
):
    run = await _seed(db_session)
    rows = await simulation_stats.cost_per_interview(db_session, run.id)
    by_thread = {r.thread_ts: r for r in rows}
    assert set(by_thread) == {INTERVIEW_TS, None}
    assert by_thread[INTERVIEW_TS].cost > Decimal(0)
    one_call = by_thread[INTERVIEW_TS].cost
    # The memory call and the lab's reply step: both have no thread id.
    assert by_thread[None].cost == 2 * one_call


async def test_cost_by_stage_keeps_project_calls_under_their_own_stages(db_session):
    run = await _seed(db_session)
    stages = {s.thread_phase for s in await simulation_stats.cost_by_stage(db_session, run.id)}
    assert {"project_refine", "project_reply", "open"} <= stages


async def test_the_lab_page_thread_count_leaves_out_project_threads_of_every_run(
    client, db_session,
):
    run = await _seed(db_session)
    other = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session, run=other, phase="project_reply", thread_ts=None,
        message_ts=OTHER_RUN_PROJECT_TS, channel_name="general", agent_id=LAB,
    )
    await factories.make_agent_message(
        db_session, run=other, phase="thread_reply", thread_ts=OTHER_RUN_PROJECT_TS,
        channel_name="general", agent_id=LAB,
    )
    pi = await factories.make_user(db_session, name="Pat Project", email="pat-project@example.org")
    await factories.make_profile(db_session, user=pi)
    await factories.make_agent(
        db_session, user=pi, agent_id=LAB, bot_name="ProjBot", pi_name="Pat Project",
    )
    await db_session.flush()
    assert run.id != other.id

    page = await client.get(f"/agent/{LAB}/dashboard", headers=auth_headers(pi.id))

    assert page.status_code == 200
    assert re.search(r'text-blue-600">\s*1\s*<', page.text), "threads_count was not 1"


async def test_the_calls_page_thread_filter_lists_one_thread_and_pages_with_it(
    client, db_session,
):
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="proj-calls-admin@example.org"
    )
    run = await factories.make_simulation_run(db_session)
    for i in range(51):
        db_session.add(
            LlmCallLog(
                simulation_run_id=run.id, agent_id=HUB, phase="project_thread",
                thread_ts=PROJECT_TS, thread_phase="project_refine", model="claude-test",
                system_prompt="sys", messages_json=[], response_text=f"PROJECT-{i}",
            )
        )
    other = await _call(
        db_session, run, thread_ts=INTERVIEW_TS, phase="thread_reply", thread_phase="open",
        text="INTERVIEW-CALL",
    )
    await db_session.flush()
    base = f"/admin/activity/{run.id}/llm-calls"

    page1 = (await client.get(f"{base}?thread={PROJECT_TS}", headers=auth_headers(admin.id))).text
    assert "Showing 50 of 51 logged turns" in page1
    assert f"{base}/{other.id}/bodies" not in page1
    assert f'name="thread" value="{PROJECT_TS}"' in page1
    assert f"?page=2&thread={PROJECT_TS}" in page1
    assert page1.count('<option value=""') == 4, "no new select for the thread filter"
    assert f'href="{base}"' in page1, "Clear shows for a thread-only filter"

    page2 = (
        await client.get(f"{base}?thread={PROJECT_TS}&page=2", headers=auth_headers(admin.id))
    ).text
    assert "Showing 1 of 51 logged turns" in page2
    assert f"?page=1&thread={PROJECT_TS}" in page2

    unfiltered = (await client.get(base, headers=auth_headers(admin.id))).text
    assert "Showing 50 of 52 logged turns" in unfiltered, "the filter, not the fixture, narrows"
