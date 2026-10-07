"""Read-time industry percentiles (spec 2026-10-05 §6.2, P18, D13): each PI's latest row of
the current SCORER_VERSION, ranked against every such row; older versions are
"rescoring" and never join the cohort; the manager pages show the view and the partial
badge."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from src.models import USER_ROLE_MANAGER, USER_ROLE_PI, PiIndustryScore
from src.services import directory
from src.services.industry_score import SCORER_VERSION
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

CUME_DIST_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (user_id) user_id, raw_sum, tenure_start_used
    FROM pi_industry_scores WHERE scorer_version = :v
   ORDER BY user_id, computed_at DESC, id DESC),
cohort AS (SELECT user_id, raw_sum FROM latest WHERE tenure_start_used IS NOT NULL AND raw_sum > 0)
SELECT user_id, 100 * cume_dist() OVER (ORDER BY raw_sum) FROM cohort
"""


async def _score(db, user, raw, *, tenure=2015, version=SCORER_VERSION, coverage=None, at=None):
    db.add(PiIndustryScore(
        user_id=user.id, raw_sum=raw, components={}, evidence_count=1 if raw else 0,
        tenure_start_used=tenure, scorer_version=version,
        coverage={"openalex": "ok"} if coverage is None else coverage,
        **({"computed_at": at} if at else {}),
    ))
    await db.flush()


async def test_read_time_percentile_equals_cume_dist(db_session):
    users = [await factories.make_user(db_session) for _ in range(5)]
    for user, raw in zip(users, (1.0, 2.0, 2.0, 5.0, 9.0), strict=True):
        await _score(db_session, user, raw)
    views = await directory.industry_views(db_session, [u.id for u in users])
    expected = dict((await db_session.execute(text(CUME_DIST_SQL), {"v": SCORER_VERSION})).all())
    for user in users:
        assert views[user.id].reason == "ok"
        assert abs(views[user.id].percentile - float(expected[user.id])) < 0.051
    assert views[users[1].id].percentile == views[users[2].id].percentile == 60.0


async def test_old_version_rows_show_rescoring_and_never_join_the_cohort(db_session):
    users = [await factories.make_user(db_session) for _ in range(4)]
    for user, raw in zip(users[:3], (1.0, 2.0, 3.0), strict=True):
        await _score(db_session, user, raw)
    await _score(db_session, users[3], 50.0, version="1.0.0")
    views = await directory.industry_views(db_session, [u.id for u in users])
    assert views[users[3].id].reason == "rescoring" and views[users[3].id].row is None
    assert {views[u.id].reason for u in users[:3]} == {"cohort_too_small"}   # 3 rows: 2 peers each


async def test_reasons_follow_the_row(db_session):
    peers = [await factories.make_user(db_session) for _ in range(3)]
    for user, raw in zip(peers, (1.0, 2.0, 3.0), strict=True):
        await _score(db_session, user, raw)
    no_tenure, no_evidence, never = [await factories.make_user(db_session) for _ in range(3)]
    await _score(db_session, no_tenure, 5.0, tenure=None)
    await _score(db_session, no_evidence, 0.0)
    views = await directory.industry_views(
        db_session, [no_tenure.id, no_evidence.id, never.id, peers[0].id]
    )
    assert views[no_tenure.id].reason == "no_tenure_start"
    assert views[no_evidence.id].reason == "no_evidence"
    assert views[never.id].reason == "not_computed"
    assert views[peers[0].id].reason == "cohort_too_small"   # neither no-tenure nor zero rows count


async def test_the_latest_current_row_wins(db_session):
    users = [await factories.make_user(db_session) for _ in range(4)]
    for user, raw in zip(users[:3], (1.0, 2.0, 3.0), strict=True):
        await _score(db_session, user, raw)
    await _score(db_session, users[3], 9.0, at=datetime(2026, 10, 1, tzinfo=UTC))
    await _score(db_session, users[3], 0.5, at=datetime(2026, 10, 2, tzinfo=UTC))
    view = (await directory.industry_views(db_session, [users[3].id]))[users[3].id]
    assert view.row.raw_sum == 0.5 and view.percentile == 25.0


async def test_the_pages_show_the_view_and_the_partial_badge(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    users = [await factories.make_user(db_session, user_role=USER_ROLE_PI) for _ in range(4)]
    for user, raw in zip(users, (1.0, 2.0, 3.0, 9.0), strict=True):
        await _score(db_session, user, raw)
    old = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await _score(db_session, old, 40.0, version="1.0.0")
    partial = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await _score(db_session, partial, 4.0, coverage={"openalex": "ok", "uspto": "truncated",
                                                      "ctgov": "unavailable:http_503"})
    await db_session.commit()
    page = (await client.get(f"/workspace/pis/{partial.id}", headers=auth_headers(mgr.id))).text
    assert ">partial</span>" in page
    assert "uspto truncated" in page and "ctgov unavailable:http_503" in page   # JSONB reorders keys
    assert "80.0" in page                              # 4.0 is 4th of 5 cohort members
    page = (await client.get(f"/workspace/pis/{old.id}", headers=auth_headers(mgr.id))).text
    assert "Rescoring" in page
    listing = (await client.get("/workspace/pis", headers=auth_headers(mgr.id))).text
    assert "rescoring" in listing and "· partial" in listing and "100.0" in listing


async def test_reviewer_sees_scores_but_not_scoring_or_job_diagnostics(client, db_session):
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    users = [await factories.make_user(db_session) for _ in range(4)]
    for user, raw in zip(users, (1.0, 2.0, 3.0, 13.5), strict=True):
        await _score(db_session, user, raw, coverage={"patents": "COVERAGE_DIAGNOSTIC"})
    score = await db_session.scalar(select(PiIndustryScore).where(PiIndustryScore.user_id == users[-1].id))
    score.components = {"COMPONENT_DIAGNOSTIC": {"distinct_companies": 9, "weighted": 4.0}}
    old = await factories.make_user(db_session)
    await _score(db_session, old, 99.0, version="OLD_SCORER_SECRET")
    await db_session.flush()
    headers = auth_headers(reviewer.id)
    response = await client.get(f"/workspace/pis/{users[-1].id}", headers=headers)
    assert response.status_code == 200
    assert "100.0" in response.text and "Raw score 13.5" in response.text
    assert "1 evidence rows" in response.text
    listing = await client.get("/workspace/pis", headers=headers)
    assert listing.status_code == 200 and "100.0" in listing.text
    unavailable = await client.get(f"/workspace/pis/{old.id}", headers=headers)
    assert unavailable.status_code == 200 and "No score available." in unavailable.text
    for html in (response.text, listing.text, unavailable.text):
        for diagnostic in ("COVERAGE_DIAGNOSTIC", "OLD_SCORER_SECRET", "Rescoring",
                           "rescoring", "· partial", ">partial</span>", "scorer v", "industry job", "COMPONENT DIAGNOSTIC"):
            assert diagnostic not in html
