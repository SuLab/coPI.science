"""The staff-only prompt-suggestions pages (manager router) and the
suggestion-status and generate actions (reviews router).

``PromptChangeSuggestion`` rows are written by the review bot
(``src/services/review_bot.py``), never by a human — these pages are
read-and-triage only. The status action and the generate button are the two
writes, and both live on ``/reviews`` (not ``/manager``), same split as every
other review write in this app.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    Job,
    OpportunityAssessment,
    PromptChangeSuggestion,
    SimulationRun,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

# A real file on disk, so a "current hash" computation has something to read.
_REAL_PROMPT_FILE = "prompts/agent-system.md"
_REAL_PROMPT_FILE_HASH = hashlib.sha256(Path(_REAL_PROMPT_FILE).read_bytes()).hexdigest()[:12]


async def _seed_assessment(db) -> OpportunityAssessment:
    run = SimulationRun()
    db.add(run)
    await db.flush()
    a = OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird", channel_name="c")
    db.add(a)
    await db.flush()
    return a


def _make_suggestion(*, assessment_id=None, **overrides) -> PromptChangeSuggestion:
    defaults = dict(
        assessment_id=assessment_id,
        subject_label="Wang — Widget Co",
        feedback_snapshot=[
            {
                "id": str(uuid.uuid4()),
                "reviewer_name": "Rhonda Reviewer",
                "score": 2,
                "feedback_mode": "learn",
                "comment": "Missed the IP angle entirely.",
                "created_at": "2026-08-28T00:00:00+00:00",
            }
        ],
        target="scout_hub",
        prompt_files=[{"path": _REAL_PROMPT_FILE, "sha256_12": _REAL_PROMPT_FILE_HASH}],
        suggestion="Tighten the IP-diligence question.",
        transcript_available=True,
    )
    defaults.update(overrides)
    return PromptChangeSuggestion(**defaults)


async def _seed_suggestion(db, **overrides) -> PromptChangeSuggestion:
    s = _make_suggestion(**overrides)
    db.add(s)
    await db.flush()
    return s


@pytest.mark.parametrize(
    "role,expected",
    [(USER_ROLE_MANAGER, 200), (USER_ROLE_ADMIN, 200), (USER_ROLE_REVIEWER, 403), (USER_ROLE_PI, 403)],
)
async def test_staff_see_the_list_and_reviewers_and_pis_do_not(
    client, db_session, role, expected
):
    user = await factories.make_user(db_session, user_role=role)
    await _seed_suggestion(db_session)

    r = await client.get(
        "/manager/prompt-suggestions", headers=auth_headers(user.id), follow_redirects=False
    )
    assert r.status_code == expected
    if expected == 200:
        assert "Wang — Widget Co" in r.text


@pytest.mark.parametrize(
    "role,expected",
    [(USER_ROLE_MANAGER, 200), (USER_ROLE_REVIEWER, 403), (USER_ROLE_PI, 403)],
)
async def test_staff_see_the_detail_and_reviewers_and_pis_do_not(
    client, db_session, role, expected
):
    user = await factories.make_user(db_session, user_role=role)
    s = await _seed_suggestion(db_session)

    r = await client.get(
        f"/manager/prompt-suggestions/{s.id}",
        headers=auth_headers(user.id),
        follow_redirects=False,
    )
    assert r.status_code == expected


async def test_detail_renders_suggestion_as_sanitized_markdown(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    s = await _seed_suggestion(db_session, suggestion="**bold** <script>x</script>")

    body = (
        await client.get(f"/manager/prompt-suggestions/{s.id}", headers=auth_headers(mgr.id))
    ).text

    assert 'data-markdown="**bold** &lt;script&gt;x&lt;/script&gt;"' in body
    assert '<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"' in body
    assert '<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"' in body
    assert '<script src="/static/js/markdown.js">' in body
    assert "<script>x</script>" not in body


async def test_detail_shows_provenance_and_stale_badges(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    s = await _seed_suggestion(
        db_session,
        prompt_files=[
            {"path": _REAL_PROMPT_FILE, "sha256_12": "deadbeefdead"},  # deliberately wrong
            {"path": "prompts/identity.md", "sha256_12": None},
        ],
    )

    body = (
        await client.get(f"/manager/prompt-suggestions/{s.id}", headers=auth_headers(mgr.id))
    ).text

    # Provenance: the feedback snapshot row.
    assert "Rhonda Reviewer" in body
    assert "Missed the IP angle entirely." in body
    # prompt_files listed.
    assert _REAL_PROMPT_FILE in body
    assert "prompts/identity.md" in body
    # Staleness / missing badges.
    assert "STALE" in body
    assert "file missing" in body


async def test_status_transitions_record_attribution(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr Attrib")
    s = await _seed_suggestion(db_session)

    r = await client.post(
        f"/reviews/suggestions/{s.id}/status",
        data={"action": "dismissed"},
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/manager/prompt-suggestions/{s.id}"

    await db_session.refresh(s)
    assert s.status == "dismissed"
    assert s.status_set_by_user_id == mgr.id
    assert s.status_set_by_name == "Mgr Attrib"
    assert s.status_set_at is not None

    r2 = await client.post(
        f"/reviews/suggestions/{s.id}/status",
        data={"action": "implemented"},
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r2.status_code == 302
    await db_session.refresh(s)
    assert s.status == "implemented"


async def test_reviewer_and_pi_are_refused_the_status_action(client, db_session):
    s = await _seed_suggestion(db_session)
    for role in (USER_ROLE_REVIEWER, USER_ROLE_PI):
        user = await factories.make_user(db_session, user_role=role)
        r = await client.post(
            f"/reviews/suggestions/{s.id}/status",
            data={"action": "dismissed"},
            headers=auth_headers(user.id),
            follow_redirects=False,
        )
        assert r.status_code == 403, role


async def test_bad_or_missing_target_suggestion_is_400_or_404(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    s = await _seed_suggestion(db_session)

    bad = await client.post(
        f"/reviews/suggestions/{s.id}/status",
        data={"action": "not-a-real-status"},
        headers=auth_headers(mgr.id),
    )
    assert bad.status_code == 400

    missing = await client.post(
        f"/reviews/suggestions/{uuid.uuid4()}/status",
        data={"action": "dismissed"},
        headers=auth_headers(mgr.id),
    )
    assert missing.status_code == 404

    missing_get = await client.get(
        f"/manager/prompt-suggestions/{uuid.uuid4()}", headers=auth_headers(mgr.id)
    )
    assert missing_get.status_code == 404


async def test_status_filter_and_cap(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await _seed_suggestion(db_session, subject_label="Open One", status="open")
    await _seed_suggestion(db_session, subject_label="Dismissed One", status="dismissed")

    filtered = (
        await client.get(
            "/manager/prompt-suggestions?status=dismissed", headers=auth_headers(mgr.id)
        )
    ).text
    assert "Dismissed One" in filtered
    assert "Open One" not in filtered

    from src.routers.manager import SUGGESTIONS_LIMIT

    for i in range(SUGGESTIONS_LIMIT + 1):
        db_session.add(_make_suggestion(subject_label=f"Cap Fixture {i}", status="open"))
    await db_session.flush()

    capped = (
        await client.get(
            "/manager/prompt-suggestions?status=open", headers=auth_headers(mgr.id)
        )
    ).text
    assert str(SUGGESTIONS_LIMIT) in capped
    assert capped.count("prompt-suggestion-row") == SUGGESTIONS_LIMIT


async def test_suggestion_survives_assessment_deletion(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    a = await _seed_assessment(db_session)
    s = await _seed_suggestion(
        db_session, assessment_id=a.id, subject_label="Doomed Assessment Co"
    )
    suggestion_id = s.id  # captured before expiry, below, makes `s` unsafe to touch

    await db_session.execute(
        text("DELETE FROM opportunity_assessments WHERE id = :i"), {"i": str(a.id)}
    )
    await db_session.flush()
    # The raw SQL DELETE above bypasses the ORM, so the identity-mapped `s`
    # object still holds its pre-delete `assessment_id` in memory even though
    # the SET NULL fired at the DB level. A real request never hits this: each
    # one gets a brand-new session (get_db), so there is nothing cached to go
    # stale. This test reuses one session across both halves (client and
    # db_session share it — see conftest.py's asgi_app override), so it has
    # to expire `s` itself to see what a fresh request would (not expire_all:
    # that would also expire `mgr`, whose `.id` auth_headers() needs below,
    # outside of any async context that could lazy-load it).
    db_session.expire(s)

    assert (
        await db_session.scalar(
            select(PromptChangeSuggestion.assessment_id).where(
                PromptChangeSuggestion.id == suggestion_id
            )
        )
    ) is None

    r = await client.get(
        f"/manager/prompt-suggestions/{suggestion_id}", headers=auth_headers(mgr.id)
    )
    assert r.status_code == 200
    assert "Doomed Assessment Co" in r.text
    assert "(assessment no longer available)" in r.text


async def _seed_learn_review(db, assessment, *, reviewer_name="R", consumed=False):
    review = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=None,
        reviewer_name=reviewer_name,
        score=2,
        comment="Needs work.",
        feedback_mode="learn",
        consumed_at=datetime.now(UTC) if consumed else None,
    )
    db.add(review)
    await db.flush()
    return review


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_ADMIN])
async def test_generate_button_renders_for_staff_with_eligible_count(
    client, db_session, role
):
    user = await factories.make_user(db_session, user_role=role)
    a = await _seed_assessment(db_session)
    await _seed_learn_review(db_session, a)
    await db_session.commit()

    body = (
        await client.get("/manager/prompt-suggestions", headers=auth_headers(user.id))
    ).text

    assert 'action="/reviews/suggestions/generate"' in body
    assert 'method="post"' in body
    assert "Generate suggestions from current reviews" in body
    assert "1 assessment" in body
    assert "no suggestion has consumed yet" in body
    button_start = body.index("Generate suggestions from current reviews")
    button_markup = body[max(0, button_start - 300) : button_start]
    assert "disabled" not in button_markup


async def test_generate_button_disabled_when_nothing_eligible(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    body = (
        await client.get("/manager/prompt-suggestions", headers=auth_headers(mgr.id))
    ).text

    assert 'action="/reviews/suggestions/generate"' in body
    assert "Nothing to analyse" in body
    # The button itself carries the disabled attribute.
    button_start = body.index("Generate suggestions from current reviews")
    button_markup = body[max(0, button_start - 300) : button_start]
    assert "disabled" in button_markup


async def test_generate_button_absent_while_impersonating(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr Two")

    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={mgr.id}"

    body = (await client.get("/manager/prompt-suggestions", headers=headers)).text

    assert "Generate suggestions from current reviews" not in body


async def test_reviewer_still_403s_the_whole_page_with_the_button_present(
    client, db_session
):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)

    r = await client.get(
        "/manager/prompt-suggestions", headers=auth_headers(reviewer.id)
    )

    assert r.status_code == 403


async def test_flash_renders_from_query_string(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    body = (
        await client.get(
            "/manager/prompt-suggestions?generated=2&eligible=3",
            headers=auth_headers(mgr.id),
        )
    ).text

    assert "Queued 2 of 3" in body


async def test_generate_enqueues_exactly_the_eligible_assessments_and_second_press_is_noop(
    client, db_session
):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    eligible = await _seed_assessment(db_session)
    await _seed_learn_review(db_session, eligible)
    already_consumed = await _seed_assessment(db_session)
    await _seed_learn_review(db_session, already_consumed, consumed=True)
    await db_session.commit()

    r = await client.post(
        "/reviews/suggestions/generate",
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/prompt-suggestions?generated=1&eligible=1"

    jobs = (
        (
            await db_session.execute(
                select(Job).where(Job.type == "review_feedback_analysis")
            )
        )
        .scalars()
        .all()
    )
    assert len(jobs) == 1
    assert jobs[0].payload["assessment_id"] == str(eligible.id)

    r2 = await client.post(
        "/reviews/suggestions/generate",
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r2.status_code == 302
    assert r2.headers["location"] == "/manager/prompt-suggestions?generated=0&eligible=1"

    jobs_after = (
        (
            await db_session.execute(
                select(Job).where(Job.type == "review_feedback_analysis")
            )
        )
        .scalars()
        .all()
    )
    assert len(jobs_after) == 1
