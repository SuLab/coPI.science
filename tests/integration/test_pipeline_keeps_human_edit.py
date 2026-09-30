"""An edit committed between pipeline steps 6 and 9 survives in the database and
in the export; the run still stores its publications and grants."""
import pytest
from sqlalchemy import func, select, update

from src.models import Publication, ResearcherProfile
from src.services import profile_export, profile_pipeline
from tests import factories
from tests.characterization.test_profile_pipeline_gm import _install_fakes

pytestmark = pytest.mark.integration


async def test_a_concurrent_human_edit_survives_steps_6_to_9(db_session, monkeypatch, tmp_path):
    _install_fakes(monkeypatch)
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0097",
        institution=None, department=None,
    )
    profile = await factories.make_profile(
        db_session, user=user, research_summary="Stored before the run.", profile_version=2,
    )
    await factories.make_agent(db_session, user=user, agent_id="p015pipe", pi_name="Ada Lovelace")
    real_synthesize = profile_pipeline.synthesize_profile

    async def synthesize_while_a_human_edits(context, name):
        # Step 7 runs after step 6 loaded the profile: the human's edit lands now.
        await db_session.execute(
            update(ResearcherProfile)
            .where(ResearcherProfile.id == profile.id)
            .values(research_summary="HUMAN EDIT WINS",
                    profile_version=ResearcherProfile.profile_version + 1)
            .execution_options(synchronize_session=False)
        )
        return await real_synthesize(context, name)

    monkeypatch.setattr(profile_pipeline, "synthesize_profile", synthesize_while_a_human_edits)

    await profile_pipeline.run_profile_pipeline(user.id, db_session)

    stored = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert (stored.research_summary, stored.profile_version) == ("HUMAN EDIT WINS", 3)
    assert stored.grant_titles == ["Difference Engine Program", "Analytical Engine Grant"]
    assert await db_session.scalar(
        select(func.count()).select_from(Publication).where(Publication.user_id == user.id)
    ) == 2, "publications are still stored"
    exported = (tmp_path / "public" / "p015pipe.md").read_text(encoding="utf-8")
    assert "HUMAN EDIT WINS" in exported
