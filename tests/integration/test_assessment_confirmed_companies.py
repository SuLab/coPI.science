"""Staff-confirmed PI companies on the assessment detail brief (spec §7.4, O6): the
block's placement, its content (confirmed rows only), its links, its absence when the
subject resolves to no PI (Review Focus #4), and its absence from the chat record."""
import uuid
from datetime import UTC, date, datetime

import pytest
from markupsafe import escape

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    OpportunityAssessment,
    PiCompany,
)
from src.services.assessment_chat_record import load_chat_record
from src.services.pi_companies import format_funding
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

AGENT_ID = "companies-lab"
HEADING = "Companies (staff-confirmed, as of 2026-09-30)"
PUBMED = "https://pubmed.ncbi.nlm.nih.gov/39433569/"
EDGAR = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001783735"


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


def _company(pi, name, *, status, reviewed_at=None, pi_role="founder", funding_usd=None,
             funding_as_of=None, funding_source_url=None):
    return PiCompany(
        id=uuid.uuid4(), user_id=pi.id, company_name=name, normalized_name=name.lower(),
        pi_role=pi_role, funding_usd=funding_usd, funding_as_of=funding_as_of,
        source_url=PUBMED, funding_source_url=funding_source_url, status=status,
        origin="discovered", evidence=None, created_by_user_id=None,
        reviewed_by_user_id=None, reviewed_at=reviewed_at,
    )


async def _seed(db_session, *, key_points, subject=AGENT_ID, with_companies=True):
    """One PI with a registry row and four companies (two confirmed, one suggested,
    one rejected — the rejected one reviewed LAST, so it would move the as-of date if
    it leaked in), and one verdict whose subject is `subject`."""
    pi = await factories.make_user(db_session, name="Companies PI")
    await factories.make_agent(db_session, user=pi, agent_id=AGENT_ID)
    if with_companies:
        db_session.add_all([
            _company(pi, "COMPANYSENTINEL Diagnostics", status="confirmed",
                     reviewed_at=datetime(2026, 9, 30, 23, 30, tzinfo=UTC),
                     funding_usd=224_999_876, funding_as_of=date(2022, 3, 1),
                     funding_source_url=EDGAR),
            _company(pi, "SECONDSENTINEL Therapeutics", status="confirmed", pi_role="co_founder",
                     reviewed_at=datetime(2026, 9, 28, 9, 0, tzinfo=UTC)),
            _company(pi, "SUGGESTEDSENTINEL Bio", status="suggested"),
            _company(pi, "REJECTEDSENTINEL Labs", status="rejected",
                     reviewed_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC)),
        ])
    run = await factories.make_simulation_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id=subject,
        channel_name="companies-channel", company_or_project="Companies Fixture Co",
        recommendation="conditional", weighted_score=3.2, band="conditional",
        key_points=key_points,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _brief(client, path, user):
    resp = await client.get(path, headers=auth_headers(user.id))
    assert resp.status_code == 200
    return resp.text[resp.text.index('id="brief"'):resp.text.index('id="signals"')]


@pytest.mark.parametrize("role, surface", [
    (USER_ROLE_ADMIN, "admin"), (USER_ROLE_MANAGER, "manager"), (USER_ROLE_REVIEWER, "manager"),
], ids=["admin", "manager", "reviewer"])
async def test_the_block_sits_directly_under_lab_background_with_confirmed_rows_only(
    client, db_session, role, surface
):
    row = await _seed(db_session, key_points={
        "indication_audience": ["INDICATION main."],
        "lab_background": ["LAB main."],
        "proposal": ["PROPOSAL main."],
    })
    viewer = await factories.make_user(db_session, user_role=role)
    brief = await _brief(client, f"/{surface}/assessments/{row.id}", viewer)
    assert brief.index("Lab Background") < brief.index(HEADING) < brief.index("Proposal")
    assert "COMPANYSENTINEL Diagnostics &mdash; Founder" in brief
    assert "SECONDSENTINEL Therapeutics &mdash; Co-founder" in brief
    assert str(escape(format_funding(224_999_876, date(2022, 3, 1), EDGAR))) in brief
    assert (
        f'<a class="citation-link" href="{PUBMED}" title="{PUBMED}" rel="noreferrer">source</a>'
    ) in brief
    assert 'href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0001783735"' in brief
    assert 'rel="noreferrer">SEC filings</a>' in brief
    assert "SUGGESTEDSENTINEL" not in brief and "REJECTEDSENTINEL" not in brief


@pytest.mark.parametrize("key_points, last_point", [
    ({"significance": ["SIGNIFICANCE main."]}, "SIGNIFICANCE main."),
    (["FLAT one.", "FLAT two."], "FLAT two."),
], ids=["legacy-groups", "flat-list"])
async def test_without_lab_background_the_block_ends_the_key_points(
    client, db_session, admin, key_points, last_point
):
    row = await _seed(db_session, key_points=key_points)
    brief = await _brief(client, f"/admin/assessments/{row.id}", admin)
    keypoints = brief[brief.index('class="assessment-brief-keypoints"'):]
    assert keypoints.index(last_point) < keypoints.index(HEADING)
    assert '<div class="assessment-brief-companies mt-2">' in keypoints


async def test_without_key_points_the_block_stands_on_its_own(client, db_session, admin):
    row = await _seed(db_session, key_points=None)
    brief = await _brief(client, f"/admin/assessments/{row.id}", admin)
    assert "assessment-brief-keypoints" not in brief
    assert '<div class="assessment-brief-companies assessment-prose max-w-none mt-4">' in brief
    assert HEADING in brief


@pytest.mark.parametrize("subject", ["ghost-lab-without-registry", "orphan-lab", None],
                         ids=["no-registry-row", "registry-row-without-user", "no-subject"])
async def test_a_subject_that_resolves_to_no_pi_renders_no_block(client, db_session, admin, subject):
    """Review Focus #4: the companies exist (for AGENT_ID's PI) but this verdict's
    subject resolves to no PI, so the page renders, without the block."""
    await factories.make_agent(db_session, agent_id="orphan-lab")
    row = await _seed(db_session, key_points={"lab_background": ["LAB main."]}, subject=subject)
    resp = await client.get(f"/admin/assessments/{row.id}", headers=auth_headers(admin.id))
    assert resp.status_code == 200
    assert "assessment-brief-companies" not in resp.text
    assert "Companies (staff-confirmed" not in resp.text


async def test_a_pi_with_no_confirmed_company_renders_no_block(client, db_session, admin):
    row = await _seed(db_session, key_points={"lab_background": ["LAB main."]},
                      with_companies=False)
    resp = await client.get(f"/admin/assessments/{row.id}", headers=auth_headers(admin.id))
    assert resp.status_code == 200
    assert "assessment-brief-companies" not in resp.text


async def test_the_companies_never_reach_the_chat_record(db_session):
    row = await _seed(db_session, key_points={"lab_background": ["LAB main."]})
    for tier in ("staff", "reviewer"):
        record, _assessment = await load_chat_record(db_session, row.id, tier=tier)
        text = "\n".join(
            block["text"] for doc in record.documents for block in doc["source"]["content"]
        )
        assert "COMPANYSENTINEL" not in text and "SECONDSENTINEL" not in text, tier
