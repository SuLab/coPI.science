"""The Discussions page leaves out project threads (a thread rooted at a lab's
``project_reply`` post): neither its list nor its run bar shows one, while an ordinary
orphaned decision (its root post gone) is still listed and counted."""

import pytest

from src.models.hub_project import THREAD_OUTCOME_PROJECT_CLOSED
from src.services import directory
from tests import factories

pytestmark = pytest.mark.integration

_KW = dict(channel_filter=None, status_filter=None, agent_filter=[])

PROJECT_TS = "1800000000.000100"
INTERVIEW_TS = "1800000000.000200"
ORPHAN_TS = "1800000000.000300"


async def _seed(db_session):
    run = await factories.make_simulation_run(db_session)
    # A closed project thread: the lab's reply is the root, both bots reply in it.
    await factories.make_agent_message(
        db_session, run=run, phase="project_reply", thread_ts=None,
        message_ts=PROJECT_TS, channel_name="general", agent_id="lab1",
    )
    for i, sender in enumerate(("blackbird", "lab1")):
        await factories.make_agent_message(
            db_session, run=run, phase="thread_reply", thread_ts=PROJECT_TS,
            message_ts=f"1800000001.00{i}", channel_name="general", agent_id=sender,
        )
    await factories.make_thread_decision(
        db_session, run=run, thread_id=PROJECT_TS, outcome=THREAD_OUTCOME_PROJECT_CLOSED,
        agent_a="blackbird", agent_b="lab1", channel="general",
    )
    # Positive controls: an interview root and an orphaned decision.
    await factories.make_agent_message(
        db_session, run=run, phase="new_post", thread_ts=None,
        message_ts=INTERVIEW_TS, channel_name="general", agent_id="lab2",
    )
    await factories.make_thread_decision(
        db_session, run=run, thread_id=ORPHAN_TS, outcome="timeout",
        agent_a="lab3", agent_b="blackbird", channel="general",
    )
    await db_session.flush()
    return run


async def test_a_project_thread_is_not_a_discussion(db_session):
    run = await _seed(db_session)

    view = await directory.build_discussions_view(db_session, run_id=str(run.id), page=1, **_KW)

    listed = {t["message_ts"] for t in view["threads"]}
    assert PROJECT_TS not in listed
    assert {INTERVIEW_TS, ORPHAN_TS} <= listed, "the interview and the orphan are still listed"
    assert view["thread_total"] == 2
    (entry,) = [e for e in view["run_bar"] if e["id"] == run.id]
    assert entry["count"] == 2, "the run bar counts the root and the orphan, not the project"


async def test_the_export_leaves_project_threads_out_too(db_session):
    run = await _seed(db_session)

    export = await directory.build_discussions_view(
        db_session, run_id=str(run.id), page=None, **_KW
    )

    assert PROJECT_TS not in {t["message_ts"] for t in export["threads"]}
    assert ORPHAN_TS in {t["message_ts"] for t in export["threads"]}


async def test_a_project_root_in_another_run_does_not_hide_a_same_ts_orphan(db_session):
    """The exclusion is correlated on the decision's run: a ``project_reply`` root with
    the same ts in another run does not make this run's orphan a project thread."""
    run = await _seed(db_session)
    other = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session, run=other, phase="project_reply", thread_ts=None,
        message_ts=ORPHAN_TS, channel_name="general", agent_id="lab3",
    )
    await db_session.flush()

    view = await directory.build_discussions_view(db_session, run_id=str(run.id), page=1, **_KW)

    assert ORPHAN_TS in {t["message_ts"] for t in view["threads"]}
