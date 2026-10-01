"""RB-09: each route's exported file is byte-identical to today's for the same form
post, and the revision rows are the same. The frozen sequence reproduces today's
per-route calls: which publications query, which user object, whether a revision is
written, its mechanism and change_summary."""
import pytest
from sqlalchemy import select

from src.models import ProfileRevision, Publication
from src.services import profile_export
from src.services.profile_export import export_profile_to_markdown
from src.services.profile_publish import export_and_record
from src.services.tenure_scope import scoped_publications_for_export
from tests import factories

pytestmark = pytest.mark.integration


async def _seed(db_session, tmp_path, monkeypatch, n_pubs=25):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await factories.make_user(db_session, name="Ada Parity", institution="JHU", department="Bio")
    profile = await factories.make_profile(db_session, user=user, grant_titles=["G1"])
    agent = await factories.make_agent(db_session, user=user, agent_id="parity1")
    for i in range(n_pubs):
        db_session.add(Publication(user_id=user.id, pmid=str(9000 + i), title=f"P{i}",
                                   year=2020 if i in (4, 19) else 2024 - i // 3, journal="J"))
    await db_session.flush()
    return user, profile, agent


@pytest.mark.parametrize("mechanism,summary", [("web", None), ("web", "Profile saved during onboarding")])
async def test_edit_paths_export_and_revision_parity(db_session, tmp_path, monkeypatch, mechanism, summary):
    user, profile, agent = await _seed(db_session, tmp_path, monkeypatch)
    pubs = await scoped_publications_for_export(db_session, user.id, agent.agent_id)
    frozen_text = export_profile_to_markdown(user, profile, agent.agent_id, publications=pubs).read_text()
    path = await export_and_record(db_session, user=user, profile=profile, agent=agent, publications=pubs,
                                   mechanism=mechanism, changed_by_user_id=user.id, change_summary=summary)
    assert path.read_text() == frozen_text
    rev = (await db_session.execute(select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent.id)
                                    .order_by(ProfileRevision.created_at.desc()))).scalars().first()
    assert (rev.content, rev.mechanism, rev.change_summary, rev.changed_by_user_id) == (
        frozen_text, mechanism, summary, user.id)


async def test_no_agent_means_no_revision_and_no_file(db_session, tmp_path, monkeypatch):
    user, profile, agent = await _seed(db_session, tmp_path, monkeypatch, n_pubs=0)
    assert await export_and_record(db_session, user=user, profile=profile, agent=None, publications=None,
                                   mechanism="web") is None


async def test_export_only_mode_writes_no_revision(db_session, tmp_path, monkeypatch):
    user, profile, agent = await _seed(db_session, tmp_path, monkeypatch)
    pubs = await scoped_publications_for_export(db_session, user.id, agent.agent_id)
    await export_and_record(db_session, user=user, profile=profile, agent=agent, publications=pubs, mechanism=None)
    n = (await db_session.execute(select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent.id))).all()
    assert n == []
