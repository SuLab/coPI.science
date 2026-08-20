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
from src.models import AgentRegistry, Cohort, CohortAuditEvent, CohortMembership
from tests import factories

pytestmark = pytest.mark.integration


async def _memberships(db):
    rows = (await db.execute(
        select(Cohort.name, CohortMembership.agent_id)
        .join(CohortMembership, CohortMembership.cohort_id == Cohort.id)
    )).all()
    return {(n, a) for n, a in rows}


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


def _write_manifest(tmp_path, manifest: dict, name: str = "cohorts.json"):
    p = tmp_path / name
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


class TestServiceBotMembership:
    """grantbot has no AgentRegistry row and never will: it is a standalone
    service bot, not a roster agent. It is a member of all three shipped
    cohorts so its funding posts pass every gated agent's allowed-sender set,
    which means the seeder must accept a member id the roster does not contain
    -- `_run_with_session` unions `SERVICE_AGENT_IDS` into the queried roster.
    Before that, seeding the shipped manifest aborted with rc 1.
    """

    @pytest.fixture
    async def su(self, db_session):
        user = await factories.make_user(db_session, email="su@example.org")
        return await factories.make_agent(
            db_session, user=user, agent_id="su", bot_name="SuBot",
            pi_name="Su", status="active",
        )

    async def test_seeds_with_no_registry_row_for_the_service_bot(
        self, db_session, tmp_path, su
    ):
        manifest_path = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s",
                          "members": ["su", "grantbot"]},
                "beta": {"description": "d", "source": "s",
                         "members": ["su", "grantbot"]},
            }
        })

        rc = await _run_with_session(
            db_session, manifest_path, dry_run=False, prune=False
        )

        assert rc == 0
        assert await _memberships(db_session) == {
            ("alpha", "su"), ("alpha", "grantbot"),
            ("beta", "su"), ("beta", "grantbot"),
        }
        # The union is a validation-only allowance: no registry row is
        # conjured for the service bot as a side effect of seeding.
        registered = {
            aid for (aid,) in await db_session.execute(select(AgentRegistry.agent_id))
        }
        assert registered == {"su"}
        cohorts, memberships, events = await _counts(db_session)
        assert (cohorts, memberships) == (2, 4)
        assert events == 6  # two 'created', four 'agent_added'
        added = (await db_session.execute(
            select(CohortAuditEvent.cohort_name).where(
                CohortAuditEvent.action == "agent_added",
                CohortAuditEvent.agent_id == "grantbot",
            )
        )).scalars().all()
        assert sorted(added) == ["alpha", "beta"]

    async def test_adding_the_service_bot_to_seeded_cohorts_adds_one_row_each(
        self, db_session, tmp_path, su
    ):
        """The actual shape of the manifest change: cohorts already seeded, one
        member appended to each. The plan must be exactly +1 membership per
        cohort -- no cohort re-created, no existing membership disturbed."""
        without = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s", "members": ["su"]},
                "beta": {"description": "d", "source": "s", "members": ["su"]},
            }
        }, name="before.json")
        await _run_with_session(db_session, without, dry_run=False, prune=False)
        cohorts_before, memberships_before, events_before = await _counts(db_session)

        with_bot = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s",
                          "members": ["su", "grantbot"]},
                "beta": {"description": "d", "source": "s",
                         "members": ["su", "grantbot"]},
            }
        }, name="after.json")
        rc = await _run_with_session(db_session, with_bot, dry_run=False, prune=False)

        assert rc == 0
        cohorts, memberships, events = await _counts(db_session)
        assert cohorts == cohorts_before
        assert memberships == memberships_before + 2  # +1 per cohort
        assert events == events_before + 2  # two 'agent_added', nothing else

    async def test_dry_run_with_the_service_bot_writes_nothing(
        self, db_session, tmp_path, su
    ):
        """Validation passing must not become "and therefore it wrote"."""
        manifest_path = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s",
                          "members": ["su", "grantbot"]},
            }
        })

        rc = await _run_with_session(
            db_session, manifest_path, dry_run=True, prune=False
        )

        assert rc == 0
        assert await _counts(db_session) == (0, 0, 0)

    async def test_near_miss_of_a_service_id_still_aborts(
        self, db_session, tmp_path, su
    ):
        """The union covers exactly SERVICE_AGENT_IDS. "grantbo" is a typo, and
        a typo'd membership is a phantom allowed sender invisible on every admin
        screen -- it must still abort the run with nothing written."""
        manifest_path = _write_manifest(tmp_path, {
            "cohorts": {
                "alpha": {"description": "d", "source": "s",
                          "members": ["su", "grantbo"]},
            }
        })

        rc = await _run_with_session(
            db_session, manifest_path, dry_run=False, prune=False
        )

        assert rc == 1
        assert await _counts(db_session) == (0, 0, 0)
