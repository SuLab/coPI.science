"""The two columns the specialist floor writes when it declines to destroy a
verdict. See docs/specs/2026-08-18-specialist-panel-remediation-design.md §6."""

from src.models.opportunity import OpportunityAssessment


def test_assessment_has_panel_incomplete_defaulting_to_false():
    cols = OpportunityAssessment.__table__.columns
    assert "panel_incomplete" in cols
    assert cols["panel_incomplete"].nullable is False, (
        "a verdict with an unknown panel state is worse than useless for "
        "triage — the column must always answer"
    )
    assert cols["panel_incomplete"].server_default is not None, (
        "needs a server default so the migration can run BEFORE the new code "
        "serves, the way 0028 had to"
    )


def test_assessment_has_nullable_missing_domains():
    cols = OpportunityAssessment.__table__.columns
    assert "missing_domains" in cols
    assert cols["missing_domains"].nullable is True, (
        "NULL means 'no gap', which is different from an empty list meaning "
        "'a gap we could not name'"
    )
