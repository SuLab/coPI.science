"""The assessment -> PI profile lookup: resolvable, unresolvable, and stale
subject_agent_id all render distinctly (design D9)."""
import pytest

from src.services.assessment_detail import build_assessment_detail
from src.services.directory import list_assessments
from tests import factories

pytestmark = pytest.mark.integration


async def _assessment(db_session, run, subject_agent_id):
    from src.models import OpportunityAssessment
    a = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id=subject_agent_id, channel_name="general",
    )
    db_session.add(a)
    await db_session.flush()
    return a


async def test_list_assessments_resolves_pi_user_ids(db_session):
    run = await factories.make_simulation_run(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="wang")
    await _assessment(db_session, run, "wang")

    view = await list_assessments(db_session, str(run.id))
    assert view["pi_user_ids"]["wang"] == str(pi.id)


async def test_list_assessments_omits_an_unresolvable_subject(db_session):
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "decommissioned-slug")

    view = await list_assessments(db_session, str(run.id))
    assert "decommissioned-slug" not in view["pi_user_ids"]


async def test_list_assessments_omits_an_unlinked_agent(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="unlinked")  # no user=
    await _assessment(db_session, run, "unlinked")

    view = await list_assessments(db_session, str(run.id))
    assert "unlinked" not in view["pi_user_ids"]


async def test_build_assessment_detail_resolves_pi_user_id(db_session):
    run = await factories.make_simulation_run(db_session)
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="wu")
    a = await _assessment(db_session, run, "wu")

    detail = await build_assessment_detail(db_session, a.id, admin_view=True)
    assert detail["pi_user_id"] == str(pi.id)


async def test_build_assessment_detail_pi_user_id_is_none_when_unresolvable(db_session):
    run = await factories.make_simulation_run(db_session)
    a = await _assessment(db_session, run, None)

    detail = await build_assessment_detail(db_session, a.id, admin_view=True)
    assert detail["pi_user_id"] is None
