import pytest
from sqlalchemy import select

from src import cli
from src.models import User
from src.services import pi_onboarding
from src.services.jhu_rules import get_tenure_start

pytestmark = pytest.mark.integration


def test_validate_orcid():
    assert pi_onboarding.validate_orcid(" 0000-0001-2345-678X ") == "0000-0001-2345-678X"
    with pytest.raises(ValueError):
        pi_onboarding.validate_orcid("not-an-orcid")


def _db_from(db_session):
    class _E:
        async def dispose(self):
            return None

    class _F:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    async def _get_db():
        return _E(), _F()

    return _get_db


async def test_seed_refuses_a_malformed_orcid(db_session, monkeypatch):
    monkeypatch.setattr(cli, "_get_db", _db_from(db_session))
    await cli._seed_one_orcid("DROP TABLE users", run_pipeline=False)
    assert (await db_session.execute(select(User).where(User.orcid == "DROP TABLE users"))).first() is None


async def test_seed_records_employment_tenure(db_session, monkeypatch):
    async def fake_profile(orcid):
        return {"name": "Seed Pi", "orcid": orcid,
                "employments": [{"organization": "Johns Hopkins University",
                                 "start_year": 2012, "current": True}]}

    monkeypatch.setattr(cli, "_get_db", _db_from(db_session))
    monkeypatch.setattr("src.services.orcid.fetch_orcid_profile", fake_profile)
    await cli._seed_one_orcid("0000-0002-0000-0001", run_pipeline=False)
    user = (await db_session.execute(select(User).where(User.orcid == "0000-0002-0000-0001"))).scalar_one()
    assert await get_tenure_start(db_session, user.id) == 2012
