"""scripts/verify_industry_remediation.py (spec 2026-10-05 §9): each check passes on a
clean repair and names the PI it fails on."""
from datetime import UTC, datetime, timedelta

import pytest

from scripts.verify_industry_remediation import mention_names_pi, run_checks, truncated_suffix
from src.models import USER_ROLE_PI, PiIndustryEvidence, PiIndustryScore
from src.services.industry_evidence import NOT_REFRESHED
from src.services.industry_score import SCORER_VERSION
from tests import factories

DEPLOY = datetime(2026, 1, 1, tzinfo=UTC)


async def _clean_pis(db_session, n=4):
    users = []
    for i in range(n):
        user = await factories.make_user(
            db_session, user_role=USER_ROLE_PI, name=f"Gyanu Lamichhane{'' if i == 0 else i}")
        db_session.add(PiIndustryScore(user_id=user.id, raw_sum=float(i + 1), components={},
                                       evidence_count=1, tenure_start_used=2015,
                                       scorer_version=SCORER_VERSION, coverage={"openalex": "ok"}))
        users.append(user)
    await db_session.flush()
    return users


def _coi(user, company, mention, span):
    return PiIndustryEvidence(user_id=user.id, source="pubmed", kind="coi_relationship",
                              external_id=f"1:{company.lower()}", company_name=company,
                              company_class="pharma_biotech", year=2021, pi_role=None,
                              in_tenure=True,
                              evidence={"relationship": "consultant", "attributed": True,
                                        "pi_mention": mention, "span": span})


@pytest.mark.integration
async def test_a_clean_repair_passes_every_check(db_session):
    users = await _clean_pis(db_session)
    db_session.add(_coi(users[0], "Acme Therapeutics", "G.L.",
                        "G.L. is a consultant for Acme Therapeutics."))
    await db_session.flush()
    problems = await run_checks(db_session, DEPLOY)
    assert problems == {"completeness": [], "attribution": [], "suffixes": [], "coverage": [],
                        "ranks": []}


@pytest.mark.integration
async def test_each_check_names_the_pi_it_fails_on(db_session):
    users = await _clean_pis(db_session)
    late = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Old Row")
    db_session.add(PiIndustryScore(user_id=late.id, raw_sum=1.0, components={}, evidence_count=1,
                                   tenure_start_used=2015, scorer_version="1.0.0"))
    db_session.add(_coi(users[0], "Pfizer Inc", "J.S.", "J.S. consults for Pfizer Inc."))
    db_session.add(_coi(users[0], "Johns Hopkins Co", "G.L.",
                        "G.L. consults for Johns Hopkins Consortium on Aging."))
    users[1].name = "No Coverage"
    db_session.add(PiIndustryScore(user_id=users[1].id, raw_sum=2.0, components={},
                                   evidence_count=1, tenure_start_used=2015,
                                   scorer_version=SCORER_VERSION, coverage=None,
                                   computed_at=datetime.now(UTC) + timedelta(seconds=1)))
    await db_session.flush()
    problems = await run_checks(db_session, DEPLOY)
    assert [p.split(":")[0] for p in problems["completeness"]] == ["Old Row"]
    assert problems["attribution"] == ["Gyanu Lamichhane: 'Pfizer Inc' credited via 'J.S.'"]
    assert problems["suffixes"] == ["Gyanu Lamichhane: 'Johns Hopkins Co' is a truncated word"]
    assert problems["coverage"] == ["No Coverage: latest 2.0.0 row has no coverage"]
    assert problems["ranks"] == []


@pytest.mark.integration
async def test_orcid_scopes_every_check_to_one_pi(db_session):
    users = await _clean_pis(db_session)
    late = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Old Row")
    db_session.add(PiIndustryScore(user_id=late.id, raw_sum=1.0, components={}, evidence_count=1,
                                   tenure_start_used=2015, scorer_version="1.0.0"))
    await db_session.flush()
    problems = await run_checks(db_session, DEPLOY, orcid=users[0].orcid)
    assert not any(problems.values())                    # "Old Row" is outside the scope
    assert (await run_checks(db_session, DEPLOY, orcid=late.orcid))["completeness"]


@pytest.mark.integration
async def test_a_veto_only_row_is_not_complete(db_session):
    await _clean_pis(db_session)
    vetoed = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Vetoed Only")
    db_session.add(PiIndustryScore(user_id=vetoed.id, raw_sum=1.0, components={},
                                   evidence_count=1, tenure_start_used=2015,
                                   scorer_version=SCORER_VERSION,
                                   coverage={"openalex": NOT_REFRESHED, "pubmed": NOT_REFRESHED}))
    await db_session.flush()
    problems = await run_checks(db_session, DEPLOY)
    assert [p.split(":")[0] for p in problems["completeness"]] == ["Vetoed Only"]


@pytest.mark.parametrize("mention,ok", [
    ("All authors", True), ("each author", True), ("G.L.", True), ("G. L.", True), ("GL", True),
    ("Dr. Lamichhane", True), ("Gyanu Lamichhane", True), ("J.S.", False), ("Dr. Smith", False),
    ("", False),
])
def test_mention_names_pi(mention, ok):
    assert mention_names_pi(mention, "Gyanu Lamichhane") is ok


def test_truncated_suffix():
    assert truncated_suffix("Johns Hopkins Co",
                            "G.L. consults for Johns Hopkins Consortium on Aging.")
    assert not truncated_suffix("Paratek Pharmaceuticals, Inc",
                                "J.S. is an employee of Paratek Pharmaceuticals, Inc.")
    assert not truncated_suffix("Cut Off Bio", "a span that ends before the na")
