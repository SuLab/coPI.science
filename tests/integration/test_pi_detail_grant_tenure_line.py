"""The staff PI page's Grants "Tenure filter:" line follows the effective tenure year, and the
publication and candidate copy that help pages quote (help-data-accuracy plan, T9)."""
import json
import re
from datetime import UTC, datetime

import pytest

from src.models import (
    USER_ROLE_MANAGER,
    AppSetting,
    PiGrant,
    Publication,
    PublicationCandidate,
)
from src.services.jhu_rules import PROVISIONAL_KEY_PREFIX, TENURE_KEY_PREFIX
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

ORG_ONLY = "organisation only — no JHU tenure start recorded"
ORG_AND_YEAR = "organisation and fiscal year ≥ tenure start"
CANDIDATE_NOTE = (
    "Found only by OpenAlex or the name search, with no ORCID anchor. Accept stores the paper. "
    "A rejected paper is not offered again, but a later generation stores it if the PI's ORCID "
    "record or PubMed's ORCID field confirms it."
)


STALE_NOTE = (
    "Some awards below were found under an earlier tenure setting; "
    "the next grant lookup refreshes them."
)


async def _setup(db_session):
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    await factories.make_agent(db_session, user=pi)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await db_session.flush()
    return pi, mgr


async def _page(client, db_session, pi, mgr) -> str:
    await db_session.commit()
    r = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert r.status_code == 200
    return re.sub(r"\s+", " ", r.text)


def _setting(prefix, pi, year):
    return AppSetting(
        key=f"{prefix}{pi.id}",
        value=json.dumps({"year": year}),
        updated_at=datetime.now(UTC),
    )


async def test_no_tenure_year_reads_organisation_only(client, db_session):
    pi, mgr = await _setup(db_session)
    html = await _page(client, db_session, pi, mgr)
    assert ORG_ONLY in html
    assert ORG_AND_YEAR not in html


async def test_provisional_year_reads_organisation_and_year(client, db_session):
    pi, mgr = await _setup(db_session)
    db_session.add(_setting(PROVISIONAL_KEY_PREFIX, pi, 2016))
    html = await _page(client, db_session, pi, mgr)
    assert ORG_AND_YEAR in html
    assert ORG_ONLY not in html


async def test_recorded_year_ignores_a_vetoed_first_org_only_row(client, db_session):
    pi, mgr = await _setup(db_session)
    db_session.add(_setting(TENURE_KEY_PREFIX, pi, 2014))
    db_session.add(PiGrant(
        user_id=pi.id, core_project_num="R01OLDVETO", title="Old vetoed award",
        org_name="Test University", tenure_filter_mode="org_only",
        vetoed_at=datetime.now(UTC), vetoed_by_user_id=mgr.id,
    ))
    html = await _page(client, db_session, pi, mgr)
    assert ORG_AND_YEAR in html
    assert ORG_ONLY not in html
    assert STALE_NOTE not in html




async def test_recorded_year_flags_live_awards_fetched_without_it(client, db_session):
    """A year recorded after the last grant lookup: the line shows the filter the next lookup
    applies, and a note says the listed awards were found under the old setting."""
    pi, mgr = await _setup(db_session)
    db_session.add(_setting(TENURE_KEY_PREFIX, pi, 2014))
    db_session.add(PiGrant(
        user_id=pi.id, core_project_num="R01OLDLIVE", title="Award fetched before the year",
        org_name="Test University", tenure_filter_mode="org_only",
    ))
    html = await _page(client, db_session, pi, mgr)
    assert ORG_AND_YEAR in html
    assert STALE_NOTE in html


async def test_no_stale_note_when_live_awards_match_the_setting(client, db_session):
    pi, mgr = await _setup(db_session)
    db_session.add(_setting(TENURE_KEY_PREFIX, pi, 2014))
    db_session.add(PiGrant(
        user_id=pi.id, core_project_num="R01CURRENT", title="Award fetched with the year",
        org_name="Test University", tenure_filter_mode="org_and_year",
    ))
    html = await _page(client, db_session, pi, mgr)
    assert ORG_AND_YEAR in html
    assert STALE_NOTE not in html


async def test_no_year_flags_live_awards_fetched_with_a_year(client, db_session):
    """A tenure year removed after the last lookup: the line reads organisation only and the
    note says the listed awards were found under the old setting."""
    pi, mgr = await _setup(db_session)
    db_session.add(PiGrant(
        user_id=pi.id, core_project_num="R01YEARGONE", title="Award fetched with a year",
        org_name="Test University", tenure_filter_mode="org_and_year",
    ))
    html = await _page(client, db_session, pi, mgr)
    assert ORG_ONLY in html
    assert STALE_NOTE in html


async def test_publication_without_methods_and_pending_candidate_copy(client, db_session):
    pi, mgr = await _setup(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="991101", title="Invented paper"))
    db_session.add(PublicationCandidate(
        user_id=pi.id, pmid="991102", title="Invented candidate", reason="no_orcid_anchor",
        status="pending", stages="s2,s4",
    ))
    html = await _page(client, db_session, pi, mgr)
    assert "No methods fetched" in html
    assert CANDIDATE_NOTE in html
    assert "stages s2,s4" in html
