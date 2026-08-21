import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_container_is_migrated(engine):
    async with engine.connect() as conn:
        v = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        # Head-revision pin: bump it deliberately with each new migration. This is
        # the guard that catches a branch whose migration was renumbered late — see
        # .notes/cohort-system-v2.md §14 for what a duplicate revision id costs.
        # 0019-0021 db-primary-conversations, 0022 cohorts,
        # 0023 researcher_profiles synthesis provenance, 0024 agents.role column,
        # 0025 opportunity_assessments (BlackbirdBot screening verdicts),
        # 0027 add_assessment_drops, 0028 users.user_role account types,
        # 0029 opportunity_assessments.panel_incomplete / .missing_domains,
        # 0030 specialist_consults + opportunity_assessments rubric stamps,
        # 0031 normalize missing_domains JSONB 'null' -> SQL NULL (data-only),
        # 0032 llm_call_logs.call_stats (per-API-call breakdown of a logged turn),
        # 0033 thread_decisions badge composites + 18 unindexed ondelete-FK columns
        assert v == "0033"


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
