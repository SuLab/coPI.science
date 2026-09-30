"""0054: the three verdict-store columns and the chat-turn revision exist, are
nullable, and default to NULL (never backfilled; NULL reads as revision 1)."""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_0054_columns_exist_and_are_nullable(engine):
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE (table_name = 'opportunity_assessments' AND column_name IN "
            "('verdict_revision','verdict_write_id','verdict_ordinal')) "
            "OR (table_name = 'assessment_chat_turns' AND column_name = 'verdict_revision') "
            "ORDER BY table_name, column_name"
        ))).all()
    assert [(r.table_name, r.column_name, r.data_type, r.is_nullable) for r in rows] == [
        ("assessment_chat_turns", "verdict_revision", "integer", "YES"),
        ("opportunity_assessments", "verdict_ordinal", "integer", "YES"),
        ("opportunity_assessments", "verdict_revision", "integer", "YES"),
        ("opportunity_assessments", "verdict_write_id", "uuid", "YES"),
    ]


@pytest.mark.asyncio
async def test_a_row_written_without_them_reads_null(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        row = OpportunityAssessment(
            id=uuid.uuid4(), simulation_run_id=run.id, agent_id="blackbird",
            channel_name="c",
        )
        db.add(row)
        await db.flush()
        await db.refresh(row)
        assert row.verdict_revision is None
        assert row.verdict_write_id is None
        assert row.verdict_ordinal is None
        await db.rollback()
