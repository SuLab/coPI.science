"""Step 9 stages a draft for a human-edited profile (spec 2026-10-05 §6.3, D19), and
"complete" is recorded only after the job's commit (§4.3, G-14)."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import ProfileRevision
from src.services import profile_pipeline
from src.services.profile_drafts import read_draft
from tests import factories
from tests.characterization.test_profile_pipeline_gm import _VALID_PROFILE, _install_fakes

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]


async def _pi(db_session, *, edited: bool, orcid: str):
    _now = datetime.now(UTC)
    user = await factories.make_user(db_session, name="Ada Lovelace", orcid=orcid)
    profile = await factories.make_profile(
        db_session, user=user, research_summary="Written by Ada herself.", profile_version=4,
        profile_generated_at=_now - timedelta(days=10),
        human_edited_at=(_now - timedelta(days=1)) if edited else None,
    )
    return user, profile


async def test_an_edited_profile_gets_a_draft_not_an_overwrite(db_session, monkeypatch):
    _install_fakes(monkeypatch)
    user, profile = await _pi(db_session, edited=True, orcid="0000-0002-1825-0301")
    generated_at = profile.profile_generated_at
    out = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    assert out.research_summary == "Written by Ada herself."
    assert (out.profile_version, out.profile_generated_at) == (4, generated_at)
    draft = read_draft(out)
    assert draft.fields["research_summary"] == _VALID_PROFILE["research_summary"]
    assert draft.base_profile_version == 4 and draft.synthesis_validated is True
    assert draft.evidence_pub_count == 2 and out.pending_profile_created_at is not None


async def test_an_unedited_profile_is_stored_as_before(db_session, monkeypatch):
    _install_fakes(monkeypatch)
    user, profile = await _pi(db_session, edited=False, orcid="0000-0002-1825-0302")
    out = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    assert out.research_summary == _VALID_PROFILE["research_summary"]
    assert out.profile_version == 5 and out.pending_profile is None
    assert out.evidence_flagged_count == 0


async def test_an_edit_older_than_the_generation_does_not_draft(db_session, monkeypatch):
    _install_fakes(monkeypatch)
    user, profile = await _pi(db_session, edited=False, orcid="0000-0002-1825-0303")
    profile.human_edited_at = profile.profile_generated_at - timedelta(days=1)
    await db_session.flush()
    out = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    assert out.pending_profile is None and out.profile_version == 5


async def test_complete_is_recorded_after_commit(db_session, monkeypatch):
    from src.models import Job

    _install_fakes(monkeypatch)
    user, _profile = await _pi(db_session, edited=False, orcid="0000-0002-1825-0304")
    job = Job(type="generate_profile", user_id=user.id, payload={"user_id": str(user.id)})
    db_session.add(job)
    await db_session.flush()
    callbacks = []
    await profile_pipeline.run_profile_pipeline(user.id, db_session, job.id, after_commit=callbacks)
    await db_session.refresh(job)
    assert "complete" not in [p["step"] for p in job.payload.get("progress", [])]
    await db_session.commit()
    for cb in callbacks:
        await cb(db_session)
    await db_session.refresh(job)
    assert [p["step"] for p in job.payload["progress"]][-1] == "complete"


async def test_an_agent_linked_during_resolution_gets_the_generated_persona(
    db_session, monkeypatch, tmp_path,
):
    from src.services import profile_export

    _install_fakes(monkeypatch)
    monkeypatch.setattr(profile_export, 'PROFILES_DIR', tmp_path)
    user, _profile = await _pi(db_session, edited=False, orcid='0000-0002-1825-0305')
    original = profile_pipeline._steps3_4_resolve_corpus
    linked = []
    async def resolve_then_link(run):
        assert run.agent_reg is None
        await original(run)
        linked.append(await factories.make_agent(
            run.db, user=user, agent_id='linked-during-resolution'))
    monkeypatch.setattr(profile_pipeline, '_steps3_4_resolve_corpus', resolve_then_link)
    await profile_pipeline.run_profile_pipeline(user.id, db_session)
    await db_session.commit()
    from src.services.profile_publish import write_persona_files
    await write_persona_files(db_session, user.id)
    revisions = list((await db_session.execute(select(ProfileRevision).where(
        ProfileRevision.agent_registry_id == linked[0].id))).scalars())
    assert len(revisions) == 1 and revisions[0].mechanism == 'pipeline'
    assert _VALID_PROFILE['research_summary'] in revisions[0].content
    assert (tmp_path/'linked-during-resolution.md').read_text() == revisions[0].content
