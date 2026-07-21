import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_container_is_migrated(engine):
    async with engine.connect() as conn:
        v = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert v == "0020"  # bumped by db-primary-conversations migrations 0019 + 0020


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
