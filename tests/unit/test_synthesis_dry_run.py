"""scripts/synthesis_dry_run.py rebuilds a PI's synthesis context from the stored corpus
and writes nothing (spec 2026-10-05 §6.3). The script is loaded by path (scripts/ is not a
package), the idiom of tests/unit/test_migration_checks.py."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from src.models import Publication, ResearcherProfile
from tests import factories
from tests.characterization.test_profile_pipeline_gm import _VALID_PROFILE

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "synthesis_dry_run.py"


def _load():
    spec = importlib.util.spec_from_file_location("synthesis_dry_run", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _seed(db_session, orcid):
    user = await factories.make_user(db_session, name="Kim Davis", orcid=orcid,
                                     institution="Dry Run Institute")
    await factories.make_profile(db_session, user=user, research_summary="Stored summary.")
    db_session.add_all([
        Publication(user_id=user.id, pmid="901", title="First stored paper",
                    abstract="Abstract one.", year=2021, methods_text="Methods one."),
        Publication(user_id=user.id, pmid="902", title="Second stored paper",
                    abstract="Abstract two.", year=2022),
        Publication(user_id=user.id, pmid="903", title="Excluded stored paper",
                    abstract="Abstract three.", year=2023, excluded_at=datetime.now(UTC)),
    ])
    await db_session.flush()
    return user


async def _snapshot(db_session, user_id):
    await db_session.flush()

    def cols(row):
        return {c.key: getattr(row, c.key) for c in row.__table__.columns}

    pubs = (await db_session.execute(
        select(Publication).where(Publication.user_id == user_id)
        .order_by(Publication.pmid).execution_options(populate_existing=True)
    )).scalars().all()
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    return [cols(p) for p in pubs], cols(profile)


async def test_the_dry_run_builds_the_stored_context_and_writes_nothing(db_session, monkeypatch):
    mod = _load()
    contexts = []

    async def synth(context, name):
        contexts.append(context)
        return dict(_VALID_PROFILE)

    monkeypatch.setattr(mod, "synthesize_profile", synth)
    user = await _seed(db_session, "0000-0002-1825-0401")
    before = await _snapshot(db_session, user.id)

    assert await mod.run(db_session, user.orcid) == 0

    [context] = contexts
    assert "First stored paper" in context and "Second stored paper" in context
    assert "Excluded stored paper" not in context
    assert "- Institution: Dry Run Institute" in context
    assert "Methods one." in context
    assert await _snapshot(db_session, user.id) == before


async def test_a_reply_that_is_not_a_profile_exits_2(db_session, monkeypatch):
    mod = _load()

    async def synth(context, name):
        return {}

    monkeypatch.setattr(mod, "synthesize_profile", synth)
    user = await _seed(db_session, "0000-0002-1825-0402")
    assert await mod.run(db_session, user.orcid) == 2
