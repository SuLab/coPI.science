import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_container_is_migrated(engine):
    async with engine.connect() as conn:
        v = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        # Head-revision pin: bump it deliberately with each new migration. This is
        # the guard that catches a branch whose migration was renumbered late into
        # a duplicate revision id.
        # 0019-0021 db-primary-conversations, 0022 cohorts,
        # 0023 researcher_profiles synthesis provenance, 0024 agents.role column,
        # 0025 publications unique (user_id, pmid), 0026 pcm user FK cascade,
        # 0027 FK-target + badge-count indexes, 0028 thread_decisions.reopened_at,
        # 0030 agent_messages.sender_user_id + pi_dm_messages.handled_at (0029: pi_engaged_at + pi_inbound_state)
        assert v == "0030"


async def test_writes_are_rolled_back_part1(db_session):
    await db_session.execute(text(
        "INSERT INTO simulation_runs(id,started_at,status,total_messages,total_api_calls,config)"
        " VALUES (gen_random_uuid(), now(), 'running', 0, 0, '{}')"
    ))
    n = (await db_session.execute(text("SELECT count(*) FROM simulation_runs"))).scalar_one()
    assert n == 1


async def test_writes_are_rolled_back_part2(db_session):
    n = (await db_session.execute(text("SELECT count(*) FROM simulation_runs"))).scalar_one()
    assert n == 0  # part1's insert was rolled back
