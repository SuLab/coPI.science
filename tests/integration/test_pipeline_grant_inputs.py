"""Pipeline step 2 (soft ORCID fetch), synthesis grant input (D41), the post-commit write
and step-10 pacing (spec 2026-10-05 §4.2, §4.3, §6.1)."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.models import Job, PiOrcidFunding
from src.services import profile_pipeline, profile_publish
from src.services.grant_sections import GrantLine, GrantSections
from src.services.orcid_fundings import OrcidFunding
from src.services.profile_pipeline import _build_synthesis_context, run_profile_pipeline
from tests.unit.test_pipeline_corpus_integration import (  # noqa: F401
    _make_pi,
    _rec,
    _uncapped,
    wired,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]
LIVE = OrcidFunding(
    "ext:grant_number:GF1", "Organoid Foundation Award", "Golden Foundation", "grant",
    2024, 1, 2099, 12, ({"type": "grant_number", "value": "GF1", "relationship": "self"},),
)


async def _funding_titles(db_session, user_id):
    return (await db_session.execute(
        select(PiOrcidFunding.title).where(PiOrcidFunding.user_id == user_id)
    )).scalars().all()


async def test_step2_stores_fundings_and_synthesis_sees_the_sections(
    db_session, wired  # noqa: F811
):
    wired.fundings = [LIVE]
    wired.corpus = _uncapped([_rec(1, 2020, "Paper", hopkins_pi=True)])
    user, _agent, job = await _make_pi(db_session)
    await run_profile_pipeline(user.id, db_session, job.id)
    assert await _funding_titles(db_session, user.id) == ["Organoid Foundation Award"]
    context = wired.contexts[0]
    assert "## Active Grants\n- Organoid Foundation Award (Golden Foundation, 2024–2099)" in context
    assert "## Grant Titles" not in context


async def test_step2_orcid_outage_keeps_stored_rows_and_pipeline_completes(
    db_session, wired  # noqa: F811
):
    wired.corpus = _uncapped([_rec(1, 2020, "Paper", hopkins_pi=True)])
    user, _agent, job = await _make_pi(db_session)
    db_session.add(PiOrcidFunding(user_id=user.id, group_key="hash:kept", title="Kept award"))
    await db_session.flush()
    wired.fundings = None  # soft failure
    profile = await run_profile_pipeline(user.id, db_session, job.id)
    assert profile.research_summary
    assert await _funding_titles(db_session, user.id) == ["Kept award"]


async def test_the_persona_writer_locks_come_before_the_fundings_and_profile_writes(
    db_session, wired, monkeypatch  # noqa: F811
):
    """Before: the run wrote tenure keys, publications and the profile row before it took
    the persona writer locks (at step 9), holding those row locks through the LLM calls, so
    a concurrent account deletion could hang or deadlock with it (lock order, spec §4.3)."""
    events = []
    real_lock, real_store = profile_publish.lock_persona_writer, profile_pipeline.store_orcid_fundings
    real_clear = profile_pipeline.clear_provisional_tenure_start
    real_provisional = profile_pipeline.set_provisional_tenure_start
    real_corpus = profile_pipeline.lock_corpus

    async def lock(db, user_id):
        events.append("lock")
        await real_lock(db, user_id)

    async def store(db, user_id, fundings):
        events.append("store")
        await real_store(db, user_id, fundings)

    async def clear(db, user_id):
        events.append("tenure")
        await real_clear(db, user_id)

    async def provisional(db, user_id, year):
        events.append("tenure")
        await real_provisional(db, user_id, year)

    async def corpus(db, user_id):
        events.append("corpus")
        await real_corpus(db, user_id)

    monkeypatch.setattr(profile_publish, "lock_persona_writer", lock)
    monkeypatch.setattr(profile_pipeline, "store_orcid_fundings", store)
    monkeypatch.setattr(profile_pipeline, "clear_provisional_tenure_start", clear)
    monkeypatch.setattr(profile_pipeline, "set_provisional_tenure_start", provisional)
    monkeypatch.setattr(profile_pipeline, "lock_corpus", corpus)
    wired.fundings = [LIVE]
    wired.corpus = _uncapped([_rec(1, 2020, "Paper", hopkins_pi=True)])
    user, _agent, job = await _make_pi(db_session)
    await run_profile_pipeline(user.id, db_session, job.id)
    # Step 2 (before the store); before the tenure keys and the publications, held from
    # there through synthesis and step 9; the export's own.
    assert events == ["lock", "store", "lock", "tenure", "corpus", "lock"], events


async def test_step2_store_error_rolls_back_only_the_savepoint(
    db_session, wired, monkeypatch  # noqa: F811
):
    wired.fundings = [LIVE]
    wired.profile["name"] = "Rachel Green™"
    wired.corpus = _uncapped([_rec(1, 2020, "Paper", hopkins_pi=True)])

    async def broken_store(db, user_id, fundings):
        # A real write inside the savepoint, then SQL that fails there: a second row under
        # an existing (user_id, group_key) violates uq_pi_orcid_fundings_user_group.
        db.add(PiOrcidFunding(user_id=user_id, group_key="hash:new", title="Half-stored"))
        await db.flush()
        db.add(PiOrcidFunding(user_id=user_id, group_key="hash:kept", title="Duplicate"))
        await db.flush()

    monkeypatch.setattr(profile_pipeline, "store_orcid_fundings", broken_store)
    user, _agent, job = await _make_pi(db_session)
    user.name = ""
    db_session.add(PiOrcidFunding(user_id=user.id, group_key="hash:kept", title="Kept award"))
    await db_session.flush()
    profile = await run_profile_pipeline(user.id, db_session, job.id)
    assert profile.research_summary
    # Step 1's committed name and the pre-existing funding survive; the savepoint's own
    # write is gone.
    assert user.name == "Rachel Green" and user.name_sanitized_at is not None
    assert await _funding_titles(db_session, user.id) == ["Kept award"]


async def test_after_commit_defers_the_write_and_none_writes_inline(
    db_session, wired  # noqa: F811
):
    wired.corpus = _uncapped([_rec(1, 2020, "Paper", hopkins_pi=True)])
    user, agent, job = await _make_pi(db_session)
    callbacks = []
    await run_profile_pipeline(user.id, db_session, job.id, after_commit=callbacks)
    path = wired.export_dir / f"{agent.agent_id}.md"
    assert len(callbacks) == 2 and not path.exists()
    await db_session.commit()
    for callback in callbacks:
        await callback(db_session)
    assert path.exists()
    path.unlink()
    job.status = "completed"
    job2 = Job(type="generate_profile", user_id=user.id, payload={"user_id": str(user.id)})
    db_session.add(job2)
    await db_session.flush()
    await run_profile_pipeline(user.id, db_session, job2.id)
    assert path.exists(), "a direct call with no after_commit list writes inline"


async def test_step10_carries_the_followon_slot(db_session, wired):  # noqa: F811
    wired.corpus = _uncapped([_rec(1, 2020, "Paper", hopkins_pi=True)])
    user, _agent, job = await _make_pi(db_session)
    slot = datetime(2030, 2, 1, tzinfo=UTC)
    await run_profile_pipeline(user.id, db_session, job.id, followon_not_before=slot)
    follow = (await db_session.execute(select(Job).where(
        Job.user_id == user.id, Job.type.in_(("enrich_grants", "industry_evidence"))
    ))).scalars().all()
    assert follow and all(j.not_before == slot for j in follow)


async def test_step1_sanitises_a_name_filled_from_orcid(db_session, wired):  # noqa: F811
    wired.profile["name"] = "Rachel Green™"
    wired.corpus = _uncapped([_rec(1, 2020, "Paper", hopkins_pi=True)])
    user, _agent, job = await _make_pi(db_session)
    user.name = ""
    await db_session.flush()
    await run_profile_pipeline(user.id, db_session, job.id)
    assert user.name == "Rachel Green" and user.name_sanitized_at is not None


def test_synthesis_context_renders_the_sections_and_orders_ties_by_pmid():
    grants = GrantSections(
        active=(GrantLine("nih_reporter", "R01A", "Live", "NIH R01", 2021, 2027),),
        past=(GrantLine("orcid", "orcid:k", "Old", "Golden Foundation", 2012, 2016),),
        tenure_start=2010,
    )
    pubs = [{"pmid": "100", "title": "Low", "journal": "J", "year": 2020, "abstract": "a"},
            {"pmid": "200", "title": "High", "journal": "J", "year": 2020, "abstract": "b"}]
    context = _build_synthesis_context(orcid_profile={"name": "Jane Wang"}, grants=grants,
                                       publications=pubs, methods_by_pmid={})
    assert "\n## Active Grants\n- Live (NIH R01, 2021–2027)" in context
    assert "\n## Past Grants (since 2010)\n- Old (Golden Foundation, 2012–2016)" in context
    assert context.index("### High") < context.index("### Low")
