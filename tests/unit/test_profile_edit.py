"""apply_profile_edits: shared field-mutation logic for self-service
profile.py:/profile/save and the manager's PI-edit route."""
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from src.models import ProfileRevision, ResearcherProfile, User
from src.services.profile_edit import apply_profile_edits
from tests import factories

pytestmark = pytest.mark.integration


async def test_applies_edits_and_creates_a_profile_row_if_none_existed(db_session):
    pi = await factories.make_user(db_session, name="Old Name", email="old@example.edu")

    error = await apply_profile_edits(
        db_session,
        target_user=pi,
        changed_by_user_id=pi.id,
        name="New Name", email="new@example.edu",
        institution="New U", department="New Dept",
        research_summary="Studies new things.",
        techniques="crispr, sequencing",
        experimental_models="mouse",
        disease_areas="cancer",
        key_targets="TP53",
        keywords="oncology",
    )

    assert error is None
    await db_session.refresh(pi)
    assert pi.name == "New Name"
    assert pi.email == "new@example.edu"

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.research_summary == "Studies new things."
    assert profile.techniques == ["crispr", "sequencing"]
    assert profile.profile_version == 1


async def test_a_manager_editing_a_pi_attributes_the_revision_to_the_manager(db_session):
    manager = await factories.make_user(db_session, user_role="manager")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, status="active")

    # Mock export_profile_to_markdown to return a temporary path to avoid
    # filesystem permission issues in the test environment. The import is
    # inside the function, so patch it where it's imported from.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir) / "test_profile.md"
        tmppath.write_text("# Test Profile")

        with patch("src.services.profile_export.export_profile_to_markdown") as mock_export:
            mock_export.return_value = tmppath

            error = await apply_profile_edits(
                db_session,
                target_user=pi,
                changed_by_user_id=manager.id,
                name=pi.name, email=pi.email or "",
                institution="", department="",
                research_summary="Edited by a manager.",
                techniques="", experimental_models="",
                disease_areas="", key_targets="", keywords="",
            )

    assert error is None
    revision = (await db_session.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id.isnot(None))
        .order_by(ProfileRevision.created_at.desc())
    )).scalars().first()
    assert revision is not None
    assert revision.changed_by_user_id == manager.id


async def test_rejects_an_email_already_used_by_someone_else(db_session):
    await factories.make_user(db_session, email="taken@example.edu")
    pi = await factories.make_user(db_session, email="mine@example.edu")

    error = await apply_profile_edits(
        db_session, target_user=pi, changed_by_user_id=pi.id,
        name=pi.name, email="taken@example.edu",
        institution="", department="", research_summary="",
        techniques="", experimental_models="", disease_areas="",
        key_targets="", keywords="",
    )

    assert error == "email_taken"
    await db_session.refresh(pi)
    assert pi.email == "mine@example.edu"
