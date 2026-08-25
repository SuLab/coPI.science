"""apply_profile_edits: the SECOND export site must tenure-filter too (H3).

The pipeline's export was fixed alongside it, but a manager saving any profile
field re-exports profiles/public/{agent_id}.md from ALL stored Publication
rows — for a recent-recruit PI with a full-career store that silently put
pre-tenure papers back into the agent's prompt (the 2026-08-14 nine-agent
regression, re-openable from a form POST). Also: the manager form's optional
JHU tenure-year field writes a source="manual" entry.
"""

import pytest

from src.models import Publication
from src.services import profile_export
from src.services.jhu_rules import get_tenure_start, set_tenure_start
from src.services.profile_edit import apply_profile_edits
from tests import factories

pytestmark = pytest.mark.integration


async def _pi_with_full_career_store(db_session):
    user = await factories.make_user(
        db_session, orcid="0000-0011-0000-0001", name="Recent Recruit",
    )
    await factories.make_agent(
        db_session, user=user, agent_id="recruit", bot_name="RecruitBot"
    )
    for pmid, year, title in [
        ("21", 2005, "Pre-tenure classic"),
        ("22", 2019, "In-tenure result"),
    ]:
        db_session.add(
            Publication(user_id=user.id, pmid=pmid, title=title,
                        abstract="A.", year=year)
        )
    await db_session.flush()
    return user


def _edit_kwargs(user, **overrides):
    kwargs = dict(
        target_user=user, changed_by_user_id=user.id,
        name=user.name, email="", institution="", department="",
        research_summary="Summary.", techniques="t1, t2",
        experimental_models="m", disease_areas="d", key_targets="k",
        keywords="w",
    )
    kwargs.update(overrides)
    return kwargs


async def test_manager_edit_reexport_is_tenure_filtered(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await _pi_with_full_career_store(db_session)
    await set_tenure_start(user.id, 2018, "manual", db=db_session)
    await db_session.flush()

    error = await apply_profile_edits(db_session, **_edit_kwargs(user))
    assert error is None

    exported = (tmp_path / "recruit.md").read_text()
    assert "In-tenure result" in exported
    assert "Pre-tenure classic" not in exported, (
        "the manager-edit export re-leaked a pre-tenure paper (audit H3)"
    )


async def test_the_tenure_year_field_writes_a_manual_entry(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await _pi_with_full_career_store(db_session)

    error = await apply_profile_edits(
        db_session, **_edit_kwargs(user, jhu_tenure_start="2016")
    )
    assert error is None
    assert await get_tenure_start(db_session, user.id) == 2016


async def test_a_nonsense_tenure_year_is_rejected_not_stored(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await _pi_with_full_career_store(db_session)

    error = await apply_profile_edits(
        db_session, **_edit_kwargs(user, jhu_tenure_start="20x6")
    )
    assert error == "invalid_tenure_year"
    assert await get_tenure_start(db_session, user.id) is None


async def test_a_blank_tenure_field_changes_nothing(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await _pi_with_full_career_store(db_session)
    await set_tenure_start(user.id, 2018, "orcid_employment", db=db_session)
    await db_session.flush()

    error = await apply_profile_edits(
        db_session, **_edit_kwargs(user, jhu_tenure_start="")
    )
    assert error is None
    assert await get_tenure_start(db_session, user.id) == 2018
