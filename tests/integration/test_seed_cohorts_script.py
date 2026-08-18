"""Coverage for scripts/seed_cohorts.py's core run function.

Before this file, scripts/seed_cohorts.py had zero tests: every path (the
unknown-agent abort, the dry-run no-write guarantee, the applied-write path)
was only ever exercised by hand against a real database. `_run_with_session`
is exercised here directly against the sandboxed `db_session` fixture --
never against a subprocess or a real CLI invocation, and never against any
database outside the test's own rolled-back transaction (see
tests/conftest.py). This is not "running the seeder against a database" in
the production sense; it is the same kind of test as
tests/integration/test_cohort_seed_apply.py.
"""

import json

import pytest
from sqlalchemy import func, select

from scripts.seed_cohorts import _run_with_session
from src.models import Cohort, CohortAuditEvent, CohortMembership
from tests import factories

pytestmark = pytest.mark.integration


async def _counts(db):
    cohorts = (
        await db.execute(select(func.count()).select_from(Cohort))
    ).scalar_one()
    memberships = (
        await db.execute(select(func.count()).select_from(CohortMembership))
    ).scalar_one()
    events = (
        await db.execute(select(func.count()).select_from(CohortAuditEvent))
    ).scalar_one()
    return cohorts, memberships, events


def _write_manifest(tmp_path, manifest: dict):
    p = tmp_path / "cohorts.json"
    p.write_text(json.dumps(manifest))
    return p


class TestRunWithSession:
    async def test_unknown_agent_id_returns_1_and_writes_nothing(
        self, db_session, tmp_path
    ):
        manifest_path = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s", "members": ["ghost"]},
            }
        })

        rc = await _run_with_session(
            db_session, manifest_path, dry_run=False, prune=False
        )

        assert rc == 1
        assert await _counts(db_session) == (0, 0, 0)

    async def test_valid_manifest_applies_and_returns_0(self, db_session, tmp_path):
        user = await factories.make_user(db_session, email="su@example.org")
        await factories.make_agent(
            db_session, user=user, agent_id="su", bot_name="SuBot",
            pi_name="Su", status="active",
        )
        manifest_path = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s", "members": ["su"]},
            }
        })

        rc = await _run_with_session(
            db_session, manifest_path, dry_run=False, prune=False
        )

        assert rc == 0
        cohorts, memberships, events = await _counts(db_session)
        assert (cohorts, memberships) == (1, 1)
        assert events == 2  # one 'created', one 'agent_added'

    async def test_dry_run_writes_nothing(self, db_session, tmp_path):
        user = await factories.make_user(db_session, email="su@example.org")
        await factories.make_agent(
            db_session, user=user, agent_id="su", bot_name="SuBot",
            pi_name="Su", status="active",
        )
        manifest_path = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s", "members": ["su"]},
            }
        })

        rc = await _run_with_session(
            db_session, manifest_path, dry_run=True, prune=False
        )

        assert rc == 0
        assert await _counts(db_session) == (0, 0, 0)

    async def test_second_run_is_a_noop_and_returns_0(self, db_session, tmp_path):
        user = await factories.make_user(db_session, email="su@example.org")
        await factories.make_agent(
            db_session, user=user, agent_id="su", bot_name="SuBot",
            pi_name="Su", status="active",
        )
        manifest_path = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s", "members": ["su"]},
            }
        })

        await _run_with_session(db_session, manifest_path, dry_run=False, prune=False)
        before = await _counts(db_session)

        rc = await _run_with_session(
            db_session, manifest_path, dry_run=False, prune=False
        )

        assert rc == 0
        assert await _counts(db_session) == before
