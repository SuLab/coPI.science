import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytestmark = pytest.mark.integration


def test_private_matcher_copy_is_gone():
    import scripts.repair_pi_corpus as rp

    assert not hasattr(rp, "matched_used_bare_initial")
    assert not hasattr(rp, "select_additions")


async def test_apply_reselects_stored_pmids_under_the_lock(db_session):
    import scripts.repair_pi_corpus as rp
    from src.models import Publication
    from tests import factories

    user = await factories.make_user(db_session)
    plan = rp.PiRepairPlan(user_id=user.id, orcid=user.orcid, name=user.name, agent_id=None)
    plan.additions = [{"pmid": "777", "title": "Race", "year": 2024}]
    db_session.add(Publication(user_id=user.id, pmid="777", title="Race"))  # landed after planning
    await db_session.flush()
    await rp._apply_plan(db_session, plan)
    rows = (
        await db_session.execute(select(Publication).where(Publication.user_id == user.id))
    ).scalars().all()
    assert [p.pmid for p in rows] == ["777"]


async def test_apply_inserts_a_new_addition(db_session):
    import scripts.repair_pi_corpus as rp
    from src.models import Publication
    from tests import factories

    user = await factories.make_user(db_session)
    plan = rp.PiRepairPlan(user_id=user.id, orcid=user.orcid, name=user.name, agent_id=None)
    plan.additions = [{"pmid": "778", "title": "New", "year": 2024}]
    await rp._apply_plan(db_session, plan)
    rows = (
        await db_session.execute(select(Publication.pmid).where(Publication.user_id == user.id))
    ).scalars().all()
    assert rows == ["778"]


async def test_build_plan_makes_no_network_call_inside_a_transaction(db_session, monkeypatch):
    import scripts.repair_pi_corpus as rp
    from src.models import Publication
    from tests import factories

    user = await factories.make_user(db_session)
    db_session.add(Publication(user_id=user.id, pmid="1", title="T"))
    await db_session.flush()
    seen: list[bool] = []

    async def fake_refetch(pmids):
        seen.append(db_session.in_transaction())
        return {}

    async def fake_resolve(orcid, name, institution):
        seen.append(db_session.in_transaction())
        return SimpleNamespace(kept=[{"pmid": "2", "title": "U", "stages": ["s2"]}])

    monkeypatch.setattr(rp, "refetch_pmids", fake_refetch)
    monkeypatch.setattr(rp, "resolve_corpus", fake_resolve)
    plan = await rp.build_plan_for_pi(db_session, user, None, only=None)
    assert seen == [False, False]
    # An unanchored (s2-only) find is reported for review, never stored.
    assert plan.additions == []
    assert [(r.pmid, r.reason) for r in plan.review if r.reason == "unanchored_addition"] == [
        ("2", "unanchored_addition")
    ]


async def test_only_duplicate_pmids_plans_just_that_partition(db_session, monkeypatch):
    import scripts.repair_pi_corpus as rp
    from src.models import Publication
    from tests import factories

    user = await factories.make_user(db_session)
    db_session.add(Publication(user_id=user.id, pmid="5", title="T"))
    await db_session.flush()

    async def boom(*_a, **_k):
        raise AssertionError("no network for the duplicate-pmids partition")

    monkeypatch.setattr(rp, "refetch_pmids", boom)
    monkeypatch.setattr(rp, "resolve_corpus", boom)
    plan = await rp.build_plan_for_pi(db_session, user, None, only="duplicate-pmids")
    assert plan.removals == [] and plan.additions == []
