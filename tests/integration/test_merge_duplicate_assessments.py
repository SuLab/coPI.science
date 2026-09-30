"""§10.3: merge each duplicate (run, thread) group into its newest row."""
import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests import factories

pytestmark = pytest.mark.integration

_TOOL = Path(__file__).resolve().parents[2] / "scripts/migrate/merge_duplicate_assessments.py"
_CONSTRAINT = "uq_opportunity_assessments_run_thread"


def _load():
    spec = importlib.util.spec_from_file_location("_merge_dups", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves its module through sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _constraint_exists(conn) -> bool:
    return bool((await conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = :n)"
    ), {"n": _CONSTRAINT})).scalar_one())


@pytest_asyncio.fixture
async def conn(engine):
    """A connection in one transaction; the test drops 0055's constraint to seed
    duplicates. DDL is transactional, so the rollback restores it; the teardown
    verifies that and re-adds it if it is somehow missing, so later tests see it."""
    async with engine.connect() as c:
        trans = await c.begin()
        try:
            yield c
        finally:
            await trans.rollback()
    async with engine.connect() as c:
        if not await _constraint_exists(c):
            await c.execute(text(
                f"ALTER TABLE opportunity_assessments ADD CONSTRAINT {_CONSTRAINT} "
                "UNIQUE (simulation_run_id, thread_id)"
            ))
            await c.commit()
            pytest.fail(f"{_CONSTRAINT} was missing after the test and had to be restored")


async def _insert_assessment(conn, run_id, thread_id, created, *, posted=None, claimed=None, verdict=None):
    return (await conn.execute(text(
        "INSERT INTO opportunity_assessments(id, simulation_run_id, agent_id, channel_name, "
        "thread_id, panel_incomplete, created_at, raw_verdict, summary_posted_at, summary_claimed_at) "
        "VALUES (gen_random_uuid(), :r, 'blackbird', 'c', :t, false, :c, CAST(:v AS jsonb), :p, :cl) "
        "RETURNING id"
    ), {
        "r": run_id, "t": thread_id, "c": created, "p": posted, "cl": claimed,
        "v": None if verdict is None else json.dumps(verdict),
    })).scalar_one()


async def _new_run(conn):
    return (await conn.execute(text(
        "INSERT INTO simulation_runs(id, started_at, status, total_messages, total_api_calls, config) "
        "VALUES (gen_random_uuid(), now(), 'stopped', 0, 0, '{}') RETURNING id"
    ))).scalar_one()


async def _seed(conn):
    """One duplicate group of three rows (ids[0] oldest) plus bystanders.

    Returns (run_id, ids, extras)."""
    await conn.execute(text(f"ALTER TABLE opportunity_assessments DROP CONSTRAINT {_CONSTRAINT}"))
    run_id = await _new_run(conn)
    other_run = await _new_run(conn)
    # Users through the ORM on the same connection, so model defaults fill the
    # NOT NULL columns that have no server default.
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    user = await factories.make_user(session, email=f"merge-{uuid.uuid4().hex[:6]}@example.org")
    assignee_a = await factories.make_user(session, email=f"merge-a-{uuid.uuid4().hex[:6]}@example.org")
    assignee_b = await factories.make_user(session, email=f"merge-b-{uuid.uuid4().hex[:6]}@example.org")
    await session.flush()
    user_id, a_id, b_id = user.id, assignee_a.id, assignee_b.id

    base = datetime.now(UTC) - timedelta(hours=1)
    claims = [base + timedelta(seconds=10), base + timedelta(seconds=20), None]
    posted = [base, None, None]
    ids = []
    for i in range(3):
        ids.append(await _insert_assessment(
            conn, run_id, "t1", base + timedelta(minutes=i),
            posted=posted[i], claimed=claims[i], verdict={"n": i},
        ))
    null_ids = [
        await _insert_assessment(conn, run_id, None, base + timedelta(minutes=i)) for i in range(2)
    ]
    other_run_row = await _insert_assessment(conn, other_run, "t1", base)

    for i, aid in enumerate(ids):
        await conn.execute(text(
            "INSERT INTO assessment_chat_turns(id, assessment_id, user_id, context_tier, question, "
            "answer_text, status, model, fallback_used, record_sha256_12, prompt_sha256_12, created_at) "
            "VALUES (gen_random_uuid(), :a, :u, 'staff', :q, 'ans', 'complete', 'm', false, "
            "'aaaaaaaaaaaa', 'bbbbbbbbbbbb', now())"
        ), {"a": aid, "u": user_id, "q": f"q{i}"})
    for i in (0, 1):
        await conn.execute(text(
            "INSERT INTO assessment_reviews(id, assessment_id, reviewer_user_id, reviewer_name, "
            "score, feedback_mode) VALUES (gen_random_uuid(), :a, :u, 'R', :s, 'log_only')"
        ), {"a": ids[i], "u": user_id, "s": i + 1})
    await conn.execute(text(
        "INSERT INTO assessment_review_events(id, assessment_id, action, actor_user_id, actor_name) "
        "VALUES (gen_random_uuid(), :a, 'approved', :u, 'R')"
    ), {"a": ids[0], "u": user_id})
    # Assignee A is on two OLDER rows; assignee B on an older row AND the kept row.
    for aid, uid in ((ids[0], a_id), (ids[1], a_id), (ids[1], b_id), (ids[2], b_id)):
        await conn.execute(text(
            "INSERT INTO assessment_review_assignments(id, assessment_id, assignee_user_id, "
            "assignee_name, assigned_by_name) VALUES (gen_random_uuid(), :a, :u, 'N', 'Admin')"
        ), {"a": aid, "u": uid})
    await conn.execute(text(
        "INSERT INTO assessment_chat_usage(id, user_id, assessment_id, context_tier, model, status) "
        "VALUES (gen_random_uuid(), :u, :a, 'staff', 'm', 'complete')"
    ), {"u": user_id, "a": ids[0]})
    suggestion_id = (await conn.execute(text(
        "INSERT INTO prompt_change_suggestions(id, assessment_id, feedback_snapshot, target, "
        "prompt_files, suggestion, transcript_available) "
        "VALUES (gen_random_uuid(), :a, '[]', 'scout_hub', '[]', 's', false) RETURNING id"
    ), {"a": ids[1]})).scalar_one()
    job_ids = {}
    for status, aid in (("pending", ids[0]), ("failed", ids[1]), ("completed", ids[0])):
        job_ids[status] = (await conn.execute(text(
            "INSERT INTO jobs(id, type, status, payload, attempts, max_attempts) VALUES "
            "(gen_random_uuid(), CAST('review_feedback_analysis' AS job_type_enum), "
            "CAST(:s AS job_status_enum), CAST(:p AS json), 0, 3) RETURNING id"
        ), {"s": status, "p": json.dumps({"assessment_id": str(aid)})})).scalar_one()
    extras = {
        "claims": claims, "posted": posted, "null_ids": null_ids, "other_run_row": other_run_row,
        "other_run": other_run, "suggestion_id": suggestion_id, "job_ids": job_ids,
        "a_id": a_id, "b_id": b_id,
    }
    return run_id, ids, extras


@pytest.mark.asyncio
async def test_merge_keeps_the_newest_and_repoints_children(conn):
    mod = _load()
    run_id, ids, extras = await _seed(conn)
    groups = await mod.find_groups(conn)
    assert (run_id, "t1") in groups
    assert (extras["other_run"], "t1") not in groups, "the same thread id in another run is no group"
    result = await mod.merge_group(conn, run_id, "t1")
    assert result.kept_id == ids[2]
    assert sorted(result.deleted_ids) == sorted(ids[:2])
    kept = (await conn.execute(text(
        "SELECT verdict_revision, summary_posted_at FROM opportunity_assessments WHERE id = :k"
    ), {"k": ids[2]})).one()
    assert kept.verdict_revision == 3
    assert kept.summary_posted_at is not None, "any posted row keeps the thread announced"
    turns = (await conn.execute(text(
        "SELECT question, verdict_revision FROM assessment_chat_turns "
        "WHERE assessment_id = :k ORDER BY question"
    ), {"k": ids[2]})).all()
    assert [(t.question, t.verdict_revision) for t in turns] == [("q0", 1), ("q1", 2), ("q2", 3)]
    drops = (await conn.execute(text(
        "SELECT reason, detail, raw_verdict FROM assessment_drops "
        "WHERE simulation_run_id = :r ORDER BY created_at"
    ), {"r": run_id})).all()
    assert [d.reason for d in drops] == ["duplicate_thread_verdict"] * 2
    assert {d.detail for d in drops} == {"merged before 0055"}
    assert sorted(d.raw_verdict["n"] for d in drops) == [0, 1]
    left = (await conn.execute(text(
        "SELECT count(*) FROM opportunity_assessments WHERE simulation_run_id = :r AND thread_id = 't1'"
    ), {"r": run_id})).scalar_one()
    assert left == 1
    assert (run_id, "t1") not in await mod.find_groups(conn)


@pytest.mark.asyncio
async def test_merge_carries_the_newest_claim_to_the_kept_row(conn):
    mod = _load()
    run_id, ids, extras = await _seed(conn)
    await mod.merge_group(conn, run_id, "t1")
    kept = (await conn.execute(text(
        "SELECT summary_claimed_at, summary_posted_at FROM opportunity_assessments WHERE id = :k"
    ), {"k": ids[2]})).one()
    assert kept.summary_claimed_at == extras["claims"][1], "an in-doubt headline keeps its claim"
    assert kept.summary_posted_at == extras["posted"][0]


@pytest.mark.asyncio
async def test_merge_repoints_assignments_one_row_per_assignee(conn):
    mod = _load()
    run_id, ids, extras = await _seed(conn)
    await mod.merge_group(conn, run_id, "t1")
    rows = (await conn.execute(text(
        "SELECT assignee_user_id FROM assessment_review_assignments WHERE assessment_id = :k"
    ), {"k": ids[2]})).scalars().all()
    assert sorted(rows) == sorted([extras["a_id"], extras["b_id"]])


@pytest.mark.asyncio
async def test_merge_repoints_every_other_child_and_leaves_bystanders(conn):
    mod = _load()
    run_id, ids, extras = await _seed(conn)
    await mod.merge_group(conn, run_id, "t1")
    kept = ids[2]
    for table, expected in (
        ("assessment_reviews", 2),
        ("assessment_review_events", 1),
        ("assessment_chat_usage", 1),
    ):
        n = (await conn.execute(text(
            f"SELECT count(*) FROM {table} WHERE assessment_id = :k"
        ), {"k": kept})).scalar_one()
        assert n == expected, table
    suggestion = (await conn.execute(text(
        "SELECT assessment_id FROM prompt_change_suggestions WHERE id = :i"
    ), {"i": extras["suggestion_id"]})).scalar_one()
    assert suggestion == kept
    payloads = {
        status: (await conn.execute(text(
            "SELECT payload->>'assessment_id' FROM jobs WHERE id = :i"
        ), {"i": jid})).scalar_one()
        for status, jid in extras["job_ids"].items()
    }
    assert payloads["pending"] == str(kept)
    assert payloads["failed"] == str(kept)
    assert payloads["completed"] == str(ids[0]), "a finished job is history and stays as written"
    # NULL-thread rows and the other run's same-thread row are untouched.
    null_left = (await conn.execute(text(
        "SELECT count(*) FROM opportunity_assessments WHERE id = ANY(:i)"
    ), {"i": extras["null_ids"]})).scalar_one()
    assert null_left == 2
    other = (await conn.execute(text(
        "SELECT verdict_revision FROM opportunity_assessments WHERE id = :i"
    ), {"i": extras["other_run_row"]})).one()
    assert other.verdict_revision is None
    runs = (await conn.execute(text(
        "SELECT count(*) FROM simulation_runs WHERE id = ANY(:i)"
    ), {"i": [run_id, extras["other_run"]]})).scalar_one()
    assert runs == 2


def test_dry_run_is_the_default():
    mod = _load()
    args = mod.build_arg_parser().parse_args(["--database-url", "postgresql://x/y"])
    assert args.apply is False
