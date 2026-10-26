"""The projects prototype's models (migration 0073): constraints, foreign keys and the
status, reason and outcome vocabularies other packages code against."""


def test_hub_project_tables_and_constraints():
    from src.models import HubProject, HubProjectCall, HubProjectClock, HubProjectReview

    t = HubProject.__table__
    assert {c.name for c in t.constraints if c.name} >= {
        "uq_hub_projects_run_agent_slot", "ck_hub_projects_status"}

    # Column.foreign_keys is the shared metadata's own set: read it, never .pop() it
    # (tests/unit/test_run_scoped_tables.py reads the same columns later in the session).
    def only_fk(col):
        (fk,) = col.foreign_keys
        return fk

    assert only_fk(t.c.simulation_run_id).ondelete == "CASCADE"
    assert not t.c.agent_id.foreign_keys
    assert only_fk(HubProjectClock.__table__.c.simulation_run_id).ondelete == "CASCADE"
    assert only_fk(HubProjectReview.__table__.c.reviewer_user_id).ondelete == "SET NULL"
    assert only_fk(HubProjectCall.__table__.c.project_id).ondelete == "CASCADE"
    assert HubProjectReview.__table__.c.reviewer_name.type.length == 255


def test_status_and_reason_vocabularies():
    from src.models import hub_project as hp

    assert hp.OPEN_STATUSES == ("requested", "researching", "ready_to_post", "posted", "refining")
    assert hp.FINISHED_STATUSES == ("ready", "not_agreed", "not_finished")
    assert all(len(r) <= 40 for r in hp.REASONS) and len(hp.REASONS) == 9
    assert hp.THREAD_OUTCOME_PROJECT_CLOSED == "project_closed"


def test_thread_outcome_enum_has_project_closed():
    from src.models.agent_activity import ThreadDecision

    assert "project_closed" in ThreadDecision.__table__.c.outcome.type.enums
