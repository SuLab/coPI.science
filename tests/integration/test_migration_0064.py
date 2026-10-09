"""Migration 0064 (spec 2026-10-09 §4.3): researcher_profiles.full_career_fallback."""
import pytest
from sqlalchemy import text

from tests import factories

pytestmark = pytest.mark.integration


async def test_the_column_is_a_nullable_boolean_with_no_default(db_session):
    row = (await db_session.execute(text(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'researcher_profiles' AND column_name = 'full_career_fallback'"
    ))).one()
    assert tuple(row) == ("boolean", "YES", None)


async def test_a_new_profile_reads_null_until_a_generation_writes_it(db_session):
    profile = await factories.make_profile(db_session)
    assert profile.full_career_fallback is None
