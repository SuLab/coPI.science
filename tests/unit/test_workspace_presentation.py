"""Privacy at the page boundary, independent of whether Jinja renders a field."""

import uuid

import pytest

from src.models import (
    OpportunityAssessment,
    PiIndustryScore,
    ResearcherProfile,
    SimulationRun,
    User,
)
from src.services.industry_score import IndustryView
from src.services.web_permissions import capabilities_for
from src.web.identity import identity_for
from src.web.presentation import PageRecord, project_workspace


@pytest.mark.parametrize("role", ["reviewer", "manager", "admin"])
def test_recursive_projection_drops_unselected_model_columns(role):
    viewer = User(id=uuid.uuid4(), name="Viewer", user_role=role)
    pi = User(
        id=uuid.uuid4(),
        name="RESEARCH_NAME",
        user_role="pi",
        email="CONTACT_SECRET",
        onboarding_complete=True,
        orcid="0000-0001-0000-0001",
    )
    pi.unexpected_future_secret = "FUTURE_SECRET"
    profile = ResearcherProfile(
        research_summary="RESEARCH_SUMMARY", private_profile_seed="PRIVATE_SECRET"
    )
    assessment = OpportunityAssessment(
        headline="RESEARCH_HEADLINE",
        strengths=["STAFF_SECRET"],
        raw_verdict={"detail": "ADMIN_SECRET"},
    )
    page = project_workspace(
        "pis",
        {
            "target_user": pi,
            "profile": profile,
            "timeline": [{"assessment": assessment, "raw_opinion": "ADMIN_OPINION"}],
        },
        capabilities_for(viewer),
    )
    assert isinstance(page["target_user"], PageRecord)
    assert page["target_user"].name == "RESEARCH_NAME"
    assert page["profile"].research_summary == "RESEARCH_SUMMARY"
    assert "PRIVATE_SECRET" not in repr(page) and "FUTURE_SECRET" not in repr(page)
    assert ("CONTACT_SECRET" in repr(page)) == (role != "reviewer")
    assert ("STAFF_SECRET" in repr(page)) == (role != "reviewer")
    assert ("ADMIN_SECRET" in repr(page)) == (role == "admin")
    assert ("ADMIN_OPINION" in repr(page)) == (role == "admin")


def test_effective_role_not_actor_controls_privileges():
    admin = User(id=uuid.uuid4(), name="Actor", user_role="admin")
    reviewer = User(id=uuid.uuid4(), name="Effective", user_role="reviewer")
    reviewer._is_impersonated = True
    reviewer._real_admin = admin
    identity = identity_for(reviewer)
    assert identity.actor.is_admin and identity.effective.is_reviewer
    assert identity.permissions.research
    assert not identity.permissions.staff and not identity.permissions.admin
    assert not identity.permissions.chat


def test_page_records_support_the_mapping_contract_and_attribute_reads():
    record = PageRecord({"name": "Research", "version": 2})
    assert dict(record) == {"name": "Research", "version": 2}
    assert list(record.values()) == ["Research", 2]
    assert list(record.items()) == [("name", "Research"), ("version", 2)]
    assert record.name == record["name"]
    with pytest.raises(AttributeError):
        _ = record.unselected


def test_unrecognized_nested_model_fails_closed():
    viewer = User(id=uuid.uuid4(), name="Viewer", user_role="admin")
    with pytest.raises(TypeError, match="No page projection"):
        project_workspace("pis", {"jobs": object()}, capabilities_for(viewer))


@pytest.mark.parametrize("role", ["reviewer", "manager", "admin"])
def test_industry_scores_remain_research_data_but_diagnostics_are_staff_only(role):
    viewer = User(id=uuid.uuid4(), name="Viewer", user_role=role)
    score = PiIndustryScore(
        raw_sum=13.5,
        evidence_count=3,
        tenure_start_used=2015,
        scorer_version="SCORER_DIAGNOSTIC",
        components={"patent": {"distinct_companies": 2, "weighted": 4.0}},
        coverage={"patents": "COVERAGE_DIAGNOSTIC"},
    )
    view = IndustryView("SCORING_DIAGNOSTIC", 75.0, score, score.coverage)
    page = project_workspace("pis", {"industry": view}, capabilities_for(viewer))
    assert page["industry"].percentile == 75.0
    assert page["industry"].row.raw_sum == 13.5
    assert page["industry"].row.evidence_count == 3
    assert ("components" in page["industry"].row) == (role != "reviewer")
    assert ("tenure_start_used" in page["industry"].row) == (role != "reviewer")
    if role != "reviewer":
        assert page["industry"].row.components["patent"]["weighted"] == 4.0
    for diagnostic in ("SCORER_DIAGNOSTIC", "COVERAGE_DIAGNOSTIC", "SCORING_DIAGNOSTIC"):
        assert (diagnostic in repr(page)) == (role != "reviewer")
    assert ("partial" in page["industry"]) == (role != "reviewer")


@pytest.mark.parametrize("role", ["reviewer", "manager", "admin"])
def test_run_configuration_is_not_arbitrary_reviewer_page_data(role):
    viewer = User(id=uuid.uuid4(), name="Viewer", user_role=role)
    run = SimulationRun(id=uuid.uuid4(), status="completed", total_api_calls=42,
                        config={"rubric_version": "RESEARCH_VERSION",
                                "prompt_stamps": {"arbitrary": "PROMPT_CONFIG_SECRET"},
                                "announcement_text": "ANNOUNCEMENT_SECRET"})
    page = project_workspace("assessments", {"runs": [run]}, capabilities_for(viewer))
    projected = page["runs"][0]
    assert projected.config["rubric_version"] == "RESEARCH_VERSION"
    assert ("total_api_calls" in projected) == (role != "reviewer")
    for secret in ("PROMPT_CONFIG_SECRET", "ANNOUNCEMENT_SECRET"):
        assert (secret in repr(page)) == (role != "reviewer")


def test_reviewer_run_version_label_cannot_carry_nested_configuration():
    viewer = User(id=uuid.uuid4(), name="Reviewer", user_role="reviewer")
    run = SimulationRun(config={"rubric_version": {"arbitrary": "NESTED_SECRET"}})
    page = project_workspace("assessments", {"runs": [run]}, capabilities_for(viewer))
    assert page["runs"][0].config == {}
    assert "NESTED_SECRET" not in repr(page)
