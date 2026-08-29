"""T11 — /admin/jobs offers `review_feedback_analysis` as a type filter option.

`src/routers/admin.py::admin_jobs` filters in Python with no vocabulary
whitelist (`if type_filter and job.type != type_filter`), so the handler needs
no change for a new job type to be filterable — this pins the template
option that lets an admin actually select it from the page, per the plan's
Task 11 brief. Uses the `client` fixture (rollback `db_session`), not
`test_worker.py`'s committing harness — the two session regimes are
deliberately incompatible.
"""

from __future__ import annotations

import pytest

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_jobs_page_offers_the_review_type_filter(client, db_session):
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="jobs-page-admin@example.org"
    )

    html = (
        await client.get("/admin/jobs", headers=auth_headers(admin.id))
    ).text

    assert 'value="review_feedback_analysis"' in html, (
        "the Type filter on /admin/jobs has no option for review_feedback_analysis"
    )
    assert "Review Feedback Analysis" in html


async def test_the_review_type_option_is_marked_selected_when_filtered(client, db_session):
    """Non-vacuity control: the option renders as `selected` when the page is
    actually filtered to it, so the assertion above is about the option
    existing and not about a static, unwired string in the template."""
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="jobs-page-admin-2@example.org"
    )

    html = (
        await client.get(
            "/admin/jobs?type_filter=review_feedback_analysis",
            headers=auth_headers(admin.id),
        )
    ).text

    assert 'value="review_feedback_analysis" selected' in html, (
        "the review_feedback_analysis option is not marked selected when filtered to it"
    )
