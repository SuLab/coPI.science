"""Synthesis failure fails the job (spec 2026-10-05 §6.3, D18), a model refusal is dead at
once (§7), and an ORCID-iD or empty name is refused before any call (§6.3 Names)."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import Job, ResearcherProfile
from src.services import profile_pipeline
from src.services.job_queue import NonRetryableJobError
from src.services.llm import SynthesisRefused
from src.services.profile_limits import SUMMARY_MAX_CHARS
from src.worker.main import process_job
from tests import factories
from tests.characterization.test_profile_pipeline_gm import _VALID_PROFILE, _install_fakes
from tests.fakes import FakeAnthropic

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]


@pytest.mark.parametrize("human_edited", [False, True])
async def test_oversized_synthesis_neither_stores_nor_stages_a_draft(
    db_session, monkeypatch, human_edited,
):
    _install_fakes(monkeypatch)
    user = await factories.make_user(db_session, name="Ada Lovelace",
                                     orcid="0000-0002-1825-0208")
    uid = user.id
    await factories.make_profile(
        db_session, user=user, research_summary="Keep this profile.", profile_version=3,
        profile_generated_at=datetime.now(UTC) - timedelta(days=1),
        human_edited_at=datetime.now(UTC) if human_edited else None,
    )

    async def oversized(context, name):
        return {**_VALID_PROFILE, "research_summary": "x" * (SUMMARY_MAX_CHARS + 1)}

    monkeypatch.setattr(profile_pipeline, "synthesize_profile", oversized)
    with pytest.raises(profile_pipeline.SynthesisOutputError, match="characters"):
        await profile_pipeline.run_profile_pipeline(uid, db_session)
    await db_session.rollback()
    profile = await db_session.scalar(select(ResearcherProfile).where(
        ResearcherProfile.user_id == uid))
    assert profile.research_summary == "Keep this profile."
    assert profile.profile_version == 3 and profile.pending_profile is None


async def test_a_synthesis_exception_fails_the_job_and_leaves_the_profile(db_session, monkeypatch):
    _install_fakes(monkeypatch)
    monkeypatch.setattr("src.services.llm.get_anthropic_client",
                        lambda: FakeAnthropic([json.dumps(_VALID_PROFILE)]))
    user = await factories.make_user(db_session, name="Ada Lovelace", orcid="0000-0002-1825-0201")
    first = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    await db_session.commit()
    summary, version = first.research_summary, first.profile_version
    user_id = user.id

    async def boom(context, name):
        raise ValueError("unparseable")

    monkeypatch.setattr(profile_pipeline, "synthesize_profile", boom)
    with pytest.raises(ValueError, match="unparseable"):
        await profile_pipeline.run_profile_pipeline(user.id, db_session)
    await db_session.rollback()
    profile = (await db_session.execute(select(ResearcherProfile).where(
        ResearcherProfile.user_id == user_id))).scalar_one()
    assert (profile.research_summary, profile.profile_version) == (summary, version)


@pytest.mark.parametrize("reply", [
    {"research_summary": ["not", "a", "string"]},
    {"research_summary": "Fine.", "techniques": "CRISPR, RNA-seq"},
    {},
])
async def test_wrongly_typed_output_raises(db_session, monkeypatch, reply):
    _install_fakes(monkeypatch)

    async def synth(context, name):
        return reply

    monkeypatch.setattr(profile_pipeline, "synthesize_profile", synth)
    user = await factories.make_user(db_session, name="Ada Lovelace",
                                     orcid=f"0000-0002-1825-{uuid.uuid4().int % 9000 + 1000}")
    with pytest.raises(profile_pipeline.SynthesisOutputError):
        await profile_pipeline.run_profile_pipeline(user.id, db_session)


async def test_an_orcid_id_name_is_refused_before_any_call(db_session, monkeypatch):
    _install_fakes(monkeypatch)
    calls = []

    async def profile_without_name(orcid_id):
        return {"name": None, "orcid": orcid_id}

    async def no_corpus(*a, **k):
        calls.append("corpus")
        raise AssertionError("the corpus must not be resolved")

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", profile_without_name)
    monkeypatch.setattr(profile_pipeline, "resolve_corpus", no_corpus)
    user = await factories.make_user(db_session, name="0000-0002-1825-0202",
                                     orcid="0000-0002-1825-0202")
    with pytest.raises(NonRetryableJobError, match="ORCID iD"):
        await profile_pipeline.run_profile_pipeline(user.id, db_session)
    assert calls == []


async def test_a_model_refusal_is_dead_at_once(db_session, monkeypatch):
    _install_fakes(monkeypatch)

    async def refuse(context, name):
        raise SynthesisRefused("refused")

    monkeypatch.setattr(profile_pipeline, "synthesize_profile", refuse)
    user = await factories.make_user(db_session, name="Ada Lovelace", orcid="0000-0002-1825-0205")
    with pytest.raises(NonRetryableJobError, match="refused by the model"):
        await profile_pipeline.run_profile_pipeline(user.id, db_session)


async def test_an_unparseable_reply_stays_retryable(db_session, monkeypatch):
    _install_fakes(monkeypatch)
    monkeypatch.setattr("src.services.llm.get_anthropic_client",
                        lambda: FakeAnthropic(["this is not JSON"]))
    user = await factories.make_user(db_session, name="Ada Lovelace", orcid="0000-0002-1825-0206")
    with pytest.raises(ValueError) as ei:
        await profile_pipeline.run_profile_pipeline(user.id, db_session)
    assert not isinstance(ei.value, NonRetryableJobError)


async def test_an_empty_name_is_refused_before_any_call(db_session, monkeypatch):
    _install_fakes(monkeypatch)

    async def profile_without_name(orcid_id):
        return {"name": None, "orcid": orcid_id}

    async def no_corpus(*a, **k):
        raise AssertionError("the corpus must not be resolved")

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", profile_without_name)
    monkeypatch.setattr(profile_pipeline, "resolve_corpus", no_corpus)
    user = await factories.make_user(db_session, name="  ", orcid="0000-0002-1825-0207")
    with pytest.raises(NonRetryableJobError, match="has no name"):
        await profile_pipeline.run_profile_pipeline(user.id, db_session)


async def test_step1_replaces_an_orcid_id_name_with_orcids_real_name(db_session, monkeypatch):
    _install_fakes(monkeypatch)     # ORCID answers "Ada Lovelace"
    user = await factories.make_user(db_session, name="0000-0002-1825-0203",
                                     orcid="0000-0002-1825-0203")
    await profile_pipeline.run_profile_pipeline(user.id, db_session)
    await db_session.refresh(user)
    assert user.name == "Ada Lovelace"


async def test_the_worker_marks_a_refused_job_dead_at_once(engine, monkeypatch):
    """process_job end to end on committed rows: NonRetryableJobError -> dead, attempts 1."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def refuse(*args, **kwargs):
        raise NonRetryableJobError("name is an ORCID iD; enter the PI's name")

    monkeypatch.setattr("src.worker.main.run_profile_pipeline", refuse)
    async with factory() as s:
        user = await factories.make_user(s)
        job = Job(type="generate_profile", user_id=user.id, payload={"user_id": str(user.id)},
                  status="processing", attempts=1)
        s.add(job)
        await s.commit()
        job_id, user_id = job.id, user.id
    try:
        await process_job(job_id, "generate_profile", 1, 3, factory)
        async with factory() as s:
            row = await s.get(Job, job_id)
            assert (row.status, row.attempts) == ("dead", 1)
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            await s.commit()
