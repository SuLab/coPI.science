"""Unit test: profile_version bump uses an atomic SQL-side increment (issue #22 C1)."""

import uuid

from sqlalchemy.dialects import postgresql

from src.services.profile_pipeline import bump_profile_version_stmt

PROFILE_ID = uuid.uuid4()


def test_bump_is_a_sql_side_coalesce_plus_one_with_returning():
    sql = str(
        bump_profile_version_stmt(PROFILE_ID).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "SET profile_version=(coalesce(researcher_profiles.profile_version, 0) + 1)" in sql
    assert f"WHERE researcher_profiles.id = '{PROFILE_ID}'" in sql
    assert sql.endswith("RETURNING researcher_profiles.profile_version")
