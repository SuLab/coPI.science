"""RB-09 under spec 2026-10-05 §4.3: the revision records exactly what the post-commit
writer writes; no agent, no revision; mechanism=None records nothing."""
import pytest
from sqlalchemy import select

from src.models import ProfileRevision, Publication
from src.services import profile_export
from src.services.grant_sections import load_grant_sections
from src.services.profile_publish import export_and_record, write_persona_files
from src.services.tenure_scope import scoped_publications_for_export
from tests import factories

pytestmark = pytest.mark.integration


async def _seed(db_session, tmp_path, monkeypatch, n_pubs=25):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await factories.make_user(db_session, name="Ada Parity", institution="JHU",
                                     department="Bio")
    profile = await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user, agent_id="parity1")
    for i in range(n_pubs):
        db_session.add(Publication(user_id=user.id, pmid=str(9000 + i), title=f"P{i}",
                                   year=2020 if i in (4, 19) else 2024 - i // 3, journal="J"))
    await db_session.flush()
    return user, profile, agent


async def _revisions(db_session, agent_id):
    return (await db_session.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent_id)
    )).scalars().all()


@pytest.mark.parametrize(
    "mechanism,summary", [("web", None), ("web", "Profile saved during onboarding")]
)
async def test_the_revision_is_what_the_writer_writes(db_session, tmp_path, monkeypatch,
                                                      mechanism, summary):
    user, profile, agent = await _seed(db_session, tmp_path, monkeypatch)
    pubs = await scoped_publications_for_export(db_session, user.id, agent.agent_id)
    grants = await load_grant_sections(db_session, user.id)
    rendered = await export_and_record(
        db_session, user=user, profile=profile, agent=agent, publications=pubs, grants=grants,
        mechanism=mechanism, changed_by_user_id=user.id, change_summary=summary,
    )
    await db_session.commit()
    path = await write_persona_files(db_session, user.id)
    assert path.read_text() == rendered
    [rev] = await _revisions(db_session, agent.id)
    assert (rev.content, rev.mechanism, rev.change_summary, rev.changed_by_user_id) == (
        rendered, mechanism, summary, user.id
    )


async def test_no_agent_means_no_revision(db_session, tmp_path, monkeypatch):
    user, profile, _ = await _seed(db_session, tmp_path, monkeypatch, n_pubs=0)
    grants = await load_grant_sections(db_session, user.id)
    assert await export_and_record(
        db_session, user=user, profile=profile, agent=None, publications=None, grants=grants,
        mechanism="web",
    ) is None


async def test_mechanism_none_records_nothing(db_session, tmp_path, monkeypatch):
    user, profile, agent = await _seed(db_session, tmp_path, monkeypatch)
    pubs = await scoped_publications_for_export(db_session, user.id, agent.agent_id)
    grants = await load_grant_sections(db_session, user.id)
    assert await export_and_record(
        db_session, user=user, profile=profile, agent=agent, publications=pubs, grants=grants,
        mechanism=None,
    )
    assert await _revisions(db_session, agent.id) == []
