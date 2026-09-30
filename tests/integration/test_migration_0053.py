"""Migration 0053: five additive nullable columns, mapped on the models."""
import pytest
from sqlalchemy import text

from src.models import Job, OpportunityAssessment, SimulationRun, User

pytestmark = pytest.mark.integration

_EXPECTED = [
    ("jobs", "not_before", "YES", "timestamp with time zone"),
    ("opportunity_assessments", "summary_claimed_at", "YES", "timestamp with time zone"),
    ("simulation_runs", "finalized_at", "YES", "timestamp with time zone"),
    ("simulation_runs", "held_at", "YES", "timestamp with time zone"),
    ("users", "contact_email_unverified", "YES", "character varying"),
]


async def test_0053_columns_exist_and_are_nullable(db_session):
    rows = (await db_session.execute(text(
        "SELECT table_name, column_name, is_nullable, data_type "
        "FROM information_schema.columns WHERE table_schema = 'public' AND "
        "(table_name, column_name) IN (('jobs','not_before'),"
        "('users','contact_email_unverified'),"
        "('opportunity_assessments','summary_claimed_at'),"
        "('simulation_runs','finalized_at'),('simulation_runs','held_at')) "
        "ORDER BY table_name, column_name"
    ))).all()
    assert [tuple(r) for r in rows] == _EXPECTED


def test_the_models_map_the_0053_columns():
    assert Job.not_before.property.columns[0].nullable
    assert User.contact_email_unverified.property.columns[0].type.length == 255
    assert OpportunityAssessment.summary_claimed_at.property.columns[0].nullable
    assert SimulationRun.finalized_at.property.columns[0].nullable
    assert SimulationRun.held_at.property.columns[0].nullable
