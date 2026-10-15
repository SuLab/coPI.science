import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def healthy_openalex_meter(monkeypatch):
    from src.services import openalex_budget as ob

    async def meter():
        return ob.Meter(1000, 1000, 600)

    monkeypatch.setattr(ob, "read_meter", meter)


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


async def test_additions_come_from_the_uncapped_list_with_provenance_and_verified_doi(
    db_session, monkeypatch, tmp_path
):
    import scripts.repair_pi_corpus as rp
    from src.models import Publication
    from src.services import profile_export
    from src.services.corpus import CorpusResult
    from tests import factories

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user)
    ranked = [{"pmid": str(100 + i), "title": f"P{i}", "year": 2020, "doi": f"10.1/{i}",
               "stages": ["s1"]} for i in range(60)]

    async def fake_refetch(pmids):
        return {}

    async def fake_resolve(orcid, name, institution):
        return CorpusResult(kept=ranked[:50], flagged=[], ranked=ranked,
                            orcid_dois={"100": "10.1/0"})

    monkeypatch.setattr(rp, "refetch_pmids", fake_refetch)
    monkeypatch.setattr(rp, "resolve_corpus", fake_resolve)
    plan = await rp.build_plan_for_pi(db_session, user, agent.agent_id, only="additions")
    assert len(plan.additions) == 60 and not hasattr(plan, "over_cap")
    assert await rp._apply_plan(db_session, plan) is True
    row = (await db_session.execute(select(Publication).where(
        Publication.user_id == user.id, Publication.pmid == "100"))).scalar_one()
    assert (row.provenance, row.doi, row.doi_verified) == ("s1", "10.1/0", True)
    revisions = (await db_session.execute(text(
        "SELECT mechanism FROM profile_revisions WHERE agent_registry_id = :a"),
        {"a": agent.id})).scalars().all()
    assert revisions == ["reexport"]


async def test_repair_additions_are_bounded(db_session):
    import scripts.repair_pi_corpus as rp
    from src.models import Publication
    from tests import factories

    user = await factories.make_user(db_session)
    plan = rp.PiRepairPlan(user_id=user.id, orcid=user.orcid, name=user.name, agent_id=None)
    plan.additions = [{"pmid": "779", "title": "Long", "year": 2024, "journal": "J" * 300}]
    await rp._apply_plan(db_session, plan)
    journal = (await db_session.execute(
        select(Publication.journal).where(Publication.user_id == user.id))).scalar_one()
    assert len(journal) == 255


async def _plan_with_one_removal(db_session, rp, pmid):
    from src.models import Publication
    from tests import factories

    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user)
    row = Publication(user_id=user.id, pmid=pmid, title="Not the PI's", year=2020,
                      provenance="s1")
    db_session.add(row)
    await db_session.flush()
    plan = rp.PiRepairPlan(user_id=user.id, orcid=user.orcid, name=user.name,
                           agent_id=agent.agent_id)
    plan.removals = [rp.RemovalCandidate(publication_id=str(row.id), pmid=pmid,
                                         reason="no_individual_author_match",
                                         title=row.title)]
    return plan, row, agent


async def test_apply_excludes_instead_of_deleting(db_session, monkeypatch, tmp_path):
    import scripts.repair_pi_corpus as rp
    from src.models import ProfileRevision
    from src.services import profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    plan, row, agent = await _plan_with_one_removal(db_session, rp, "500")
    assert await rp._apply_plan(db_session, plan) is True
    await db_session.refresh(row)
    assert row.excluded_at is not None and row.excluded_reason == "repair_script"
    assert row.excluded_by_user_id is None
    summaries = (await db_session.execute(select(ProfileRevision.change_summary).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all()
    assert summaries == ["Corpus repaired (repair_pi_corpus)"]


async def test_apply_skips_a_row_made_manual_since_planning(db_session, monkeypatch, tmp_path,
                                                           capsys):
    import scripts.repair_pi_corpus as rp
    from src.services import profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    plan, row, _agent = await _plan_with_one_removal(db_session, rp, "501")
    row.provenance = "manual"  # staff kept it after the preview
    await db_session.flush()
    assert await rp._apply_plan(db_session, plan) is False
    await db_session.refresh(row)
    assert row.excluded_at is None and row.provenance == "manual"
    assert "skipped (now manual or excluded): pmid=501" in capsys.readouterr().out


def test_the_script_imports_no_delete():
    import ast

    source = (Path(__file__).resolve().parents[2] / "scripts" / "repair_pi_corpus.py").read_text(
        encoding="utf-8")
    imported = {alias.name for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy"
                for alias in node.names}
    assert "delete" not in imported and "select" in imported, imported
