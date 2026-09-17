"""Unit test: profile_version bump uses an atomic SQL-side increment."""

import pathlib
import re
import uuid

import pytest
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


# ---------------------------------------------------------------------------
# Mutation guard: every call site that bumps profile_version must go through
# the atomic `bump_profile_version` helper,
# not a Python-side read-modify-write. `X.profile_version = (X.profile_version
# or 0) + 1` silently drops a concurrent writer's increment — see the two-
# session race pinned in tests/integration/test_profile_version_race.py.
#
# The regex requires the assignment prefix (`profile_version = (`) so it does
# NOT trip on `bump_profile_version`'s own docstring, which quotes the old
# expression (without an assignment) to explain why the helper exists, nor on
# unrelated reads like `_stored_is_worth_keeping`'s
# `(profile.profile_version or 0) > 0`.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_OLD_PATTERN = re.compile(r"profile_version\s*=\s*\(\s*\w+\.profile_version or 0\)\s*\+\s*1")

_ATOMIC_BUMP_CALL_SITES = [
    _REPO_ROOT / "src" / "routers" / "profile.py",
    _REPO_ROOT / "src" / "routers" / "onboarding.py",
    _REPO_ROOT / "src" / "routers" / "agent_page.py",
    _REPO_ROOT / "src" / "services" / "profile_pipeline.py",
    # Fix round 1 (Minor 5): the six operator scripts previously did the same
    # Python read-modify-write and several sweep every user while the web app
    # is up.
    _REPO_ROOT / "scripts" / "import_profile_from_md.py",
    _REPO_ROOT / "scripts" / "regen_profiles_from_web.py",
    _REPO_ROOT / "scripts" / "resynth_from_current_pubs.py",
    _REPO_ROOT / "scripts" / "vet_publications.py",
    _REPO_ROOT / "scripts" / "generate_sparsedata_user.py",
    _REPO_ROOT / "scripts" / "regen_profile_from_cv.py",
]


class TestBumpProfileVersionIsUsedEverywhere:
    @pytest.mark.parametrize("path", _ATOMIC_BUMP_CALL_SITES, ids=lambda p: p.name)
    def test_no_python_side_read_modify_write(self, path):
        src = path.read_text()
        assert not _OLD_PATTERN.search(src), (
            f"{path} reintroduced the non-atomic bump "
            "`X.profile_version = (X.profile_version or 0) + 1` — use "
            "`X.profile_version = await bump_profile_version(db, X.id)` instead."
        )

    @pytest.mark.parametrize("path", _ATOMIC_BUMP_CALL_SITES, ids=lambda p: p.name)
    def test_calls_the_atomic_helper(self, path):
        src = path.read_text()
        assert "bump_profile_version(" in src, f"{path} does not call bump_profile_version(...)"
