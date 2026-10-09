"""Phase A of spec 2026-10-09 (§4.2-§4.4): a thin JHU-era record uses the full career for
synthesis and the persona, and the profile remembers it."""
from datetime import UTC, datetime, timedelta

import pytest

from src.services import profile_pipeline
from src.services.profile_drafts import read_draft
from src.services.profile_pipeline import run_profile_pipeline
from src.services.profile_review import accept_draft
from tests import factories
from tests.unit.test_pipeline_corpus_integration import (  # noqa: F401
    _make_pi,
    _rec,
    _uncapped,
    wired,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]

_HOPKINS_2018 = [{"organization": "Johns Hopkins University", "start_year": 2018, "current": True}]


def _window(n, *, first_pmid=100, year=2020):
    """``n`` ORCID-anchored records dated in a 2018 tenure window."""
    return [_rec(first_pmid + i, year, f"JHU-era paper {i}") for i in range(n)]


async def _steps(db_session, job):
    await db_session.refresh(job)
    return [p["step"] for p in job.payload.get("progress", [])]


async def test_a_thin_record_synthesizes_and_exports_the_full_career(db_session, wired):  # noqa: F811
    user, agent, job = await _make_pi(db_session)
    wired.profile["employments"] = _HOPKINS_2018
    wired.corpus = _uncapped([*_window(3), _rec(1, 2010, "Postdoc paper")])

    profile = await run_profile_pipeline(user.id, db_session, job.id)

    assert "Postdoc paper" in wired.contexts[0]
    assert "Postdoc paper" in (wired.export_dir / f"{agent.agent_id}.md").read_text()
    assert profile.full_career_fallback is True
    assert (profile.evidence_pmid_count, profile.evidence_pub_count) == (4, 4)
    assert "full_career_fallback" in await _steps(db_session, job)


async def test_eleven_jhu_era_papers_keep_the_tenure_window(db_session, wired):  # noqa: F811
    user, agent, job = await _make_pi(db_session)
    wired.profile["employments"] = _HOPKINS_2018
    wired.corpus = _uncapped([*_window(11), _rec(1, 2010, "Postdoc paper")])

    profile = await run_profile_pipeline(user.id, db_session, job.id)

    assert "Postdoc paper" not in wired.contexts[0]
    assert "Postdoc paper" not in (wired.export_dir / f"{agent.agent_id}.md").read_text()
    assert profile.full_career_fallback is False
    assert "full_career_fallback" not in await _steps(db_session, job)


async def test_a_hire_with_no_jhu_era_paper_is_grounded_on_earlier_work(db_session, wired):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    wired.profile["employments"] = [
        {"organization": "Johns Hopkins University", "start_year": 2026, "current": True}
    ]
    wired.corpus = _uncapped([_rec(1, 2024, "Postdoc paper"), _rec(2, 2022, "PhD paper")])

    profile = await run_profile_pipeline(user.id, db_session, job.id)

    assert profile.full_career_fallback is True and profile.evidence_state == "grounded"


async def test_without_a_tenure_year_the_flag_is_false(db_session, wired):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    wired.corpus = _uncapped([_rec(1, 2010, "Old paper")])
    profile = await run_profile_pipeline(user.id, db_session, job.id)
    assert profile.full_career_fallback is False


async def test_a_draft_carries_the_flag_and_accepting_it_writes_it(db_session, wired):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    now = datetime.now(UTC)
    await factories.make_profile(
        db_session, user=user, research_summary="Edited by staff.", profile_version=2,
        profile_generated_at=now - timedelta(days=5), human_edited_at=now - timedelta(days=1),
    )
    wired.profile["employments"] = _HOPKINS_2018
    wired.corpus = _uncapped([*_window(2), _rec(1, 2010, "Postdoc paper")])

    profile = await run_profile_pipeline(user.id, db_session, job.id)

    assert profile.full_career_fallback is None, "the edited text is not this run's"
    assert read_draft(profile).full_career_fallback is True
    job.status = "completed"
    await db_session.flush()
    assert await accept_draft(db_session, user.id) is None
    await db_session.refresh(profile)
    assert profile.full_career_fallback is True


async def test_a_kept_stored_profile_keeps_its_flag_and_exports_by_it(
    db_session, wired, monkeypatch,  # noqa: F811
):
    user, agent, job = await _make_pi(db_session)
    await factories.make_profile(
        db_session, user=user, research_summary="Stored.", profile_version=3,
        synthesis_validated=True, evidence_pmid_count=11, evidence_pub_count=11,
        full_career_fallback=False, profile_generated_at=datetime.now(UTC) - timedelta(days=5),
    )

    async def invalid(context, name):
        return {"research_summary": "Too short.", "techniques": [], "disease_areas": []}

    monkeypatch.setattr(profile_pipeline, "synthesize_profile", invalid)
    wired.profile["employments"] = _HOPKINS_2018
    wired.corpus = _uncapped([*_window(2), _rec(1, 2010, "Postdoc paper")])

    profile = await run_profile_pipeline(user.id, db_session, job.id)

    assert profile.research_summary == "Stored." and profile.full_career_fallback is False
    assert "Postdoc paper" not in (wired.export_dir / f"{agent.agent_id}.md").read_text()
