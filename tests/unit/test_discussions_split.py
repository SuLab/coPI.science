"""The Task 119 split of ``build_discussions_view`` equals the frozen original."""

import pytest

from src.services import directory
from tests import factories
from tests.unit._frozen_discussions import build_discussions_view as frozen

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded(db_session):
    run = await factories.make_simulation_run(db_session)
    for i in range(12):
        root = await factories.make_agent_message(
            db_session, run=run, phase="new_post", thread_ts=None,
            message_ts=f"100.{i}", channel_name=f"c{i % 3}",
            agent_id=None if i == 5 else f"lab{i % 4}",
        )
        for j in range(i % 3):
            await factories.make_agent_message(
                db_session, run=run, phase="thread_reply",
                thread_ts=root.message_ts, message_ts=f"100.{i}.{j}", agent_id="hub",
            )
        if i % 4 == 0:
            await factories.make_thread_decision(
                db_session, run=run, thread_id=root.message_ts,
                outcome="timeout" if i % 8 else "no_proposal",
                agent_a=f"lab{i % 4}", agent_b="hub", channel=f"c{i % 3}",
            )
    await factories.make_thread_decision(
        db_session, run=run, thread_id="orphan.1", outcome="timeout",
        agent_a="lab9", agent_b="hub", channel="c9",
    )
    return run


def _by_ts(threads):
    # Every row of the seeded run shares one transaction timestamp, so the
    # created_at order of the roots is unspecified; compare order-free.
    return sorted(threads, key=lambda t: t["message_ts"])


@pytest.mark.parametrize("kw", [
    dict(run_id=None, channel_filter=None, status_filter=None, agent_filter=[]),
    dict(run_id="all", channel_filter="c1", status_filter=None, agent_filter=[]),
    dict(run_id="all", channel_filter=None, status_filter="timeout", agent_filter=["hub"]),
    dict(run_id="not-a-uuid", channel_filter=None, status_filter="active", agent_filter=["lab1", "lab2"]),
])
@pytest.mark.parametrize("page", [None, 1])
async def test_view_equals_today(db_session, seeded, kw, page):
    new = await directory.build_discussions_view(db_session, page=page, **kw)
    old = await frozen(db_session, **kw)
    assert {k: new[k] for k in old if k != "threads"} == {k: old[k] for k in old if k != "threads"}
    assert _by_ts(new["threads"]) == _by_ts(old["threads"])


async def test_pages_partition_the_threads(db_session, seeded, monkeypatch):
    monkeypatch.setattr(directory, "DISCUSSIONS_PAGE_SIZE", 5)
    kw = dict(run_id="all", channel_filter=None, status_filter=None, agent_filter=[])
    whole = (await directory.build_discussions_view(db_session, page=None, **kw))["threads"]
    paged = []
    for p in (1, 2, 3):
        v = await directory.build_discussions_view(db_session, page=p, **kw)
        paged += v["threads"]
        assert v["thread_total"] == len(whole)
        assert v["page_count"] == 3
    assert [t["message_ts"] for t in paged] == [t["message_ts"] for t in whole]


async def test_all_runs_parity_with_duplicate_ts_and_a_twice_decided_orphan(db_session):
    from datetime import UTC, datetime

    run_a = await factories.make_simulation_run(db_session)
    run_b = await factories.make_simulation_run(db_session)
    for run in (run_a, run_b):
        root = await factories.make_agent_message(
            db_session, run=run, phase="new_post", thread_ts=None,
            message_ts="dup.1", channel_name="dup", agent_id="lab1",
        )
        await factories.make_agent_message(
            db_session, run=run, phase="thread_reply", thread_ts=root.message_ts,
            message_ts=f"dup.1.{run.id.hex[:4]}", agent_id="hub",
        )
    await factories.make_thread_decision(
        db_session, run=run_a, thread_id="orphan.2", outcome="timeout",
        agent_a="lab7", agent_b="hub", channel="c7", decided_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    # Distinct decided_at: inside one test transaction server_default now() ties, and
    # "last decision" would then be the database's arbitrary pick in both versions.
    await factories.make_thread_decision(
        db_session, run=run_b, thread_id="orphan.2", outcome="proposal",
        agent_a="lab7", agent_b="hub", channel="c7", decided_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    for kw in (
        dict(run_id="all", channel_filter=None, status_filter=None, agent_filter=[]),
        dict(run_id="all", channel_filter=None, status_filter=None, agent_filter=["lab7"]),
        dict(run_id=str(run_a.id), channel_filter="dup", status_filter="active", agent_filter=["hub"]),
    ):
        new = await directory.build_discussions_view(db_session, page=None, **kw)
        old = await frozen(db_session, **kw)
        assert {k: new[k] for k in old if k != "threads"} == {k: old[k] for k in old if k != "threads"}
        assert _by_ts(new["threads"]) == _by_ts(old["threads"])
