"""The chat's three routes (spec §6, §9): who may call them, what they refuse, the
headers they send, and one question streamed end to end."""

import logging
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AssessmentChatUsage,
)
from src.routers import assessment_chat as chat_router
from src.services import assessment_chat as chat_service
from tests import factories
from tests.assessment_chat_support import (
    PITCH_TEXT,
    ask_url,
    citation,
    clear_url,
    history_url,
    parse_sse,
    seed_interview,
    use_fake_llm,
    use_test_session,
)
from tests.fakes import ChatScript, FakeAsyncAnthropic
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def install_llm(monkeypatch, db_session):
    """Route the producer to the test session; return a function that installs a
    FakeAsyncAnthropic with the given scripts."""
    use_test_session(monkeypatch, db_session)

    def _install(*scripts: ChatScript) -> FakeAsyncAnthropic:
        fake = FakeAsyncAnthropic(list(scripts))
        use_fake_llm(monkeypatch, fake)
        return fake

    return _install


async def _user(db_session, role):
    return await factories.make_user(db_session, user_role=role)


def test_the_router_has_exactly_the_three_routes():
    routes = {(tuple(sorted(route.methods)), route.path) for route in chat_router.router.routes}
    assert routes == {
        (("GET",), "/{assessment_id}"),
        (("POST",), "/{assessment_id}/messages"),
        (("POST",), "/{assessment_id}/clear"),
    }


@pytest.mark.parametrize("role", [USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER])
async def test_staff_and_reviewers_may_read_their_history(client, db_session, install_llm, role):
    install_llm()
    seeded = await seed_interview(db_session)
    user = await _user(db_session, role)
    resp = await client.get(history_url(seeded.assessment_id), headers=auth_headers(user.id))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["tier"] == ("reviewer" if role == USER_ROLE_REVIEWER else "staff")
    assert body["turns"] == []
    assert (body["daily_limit"], body["max_question_chars"], body["max_turns"]) == (100, 4000, 50)
    assert body["questions_used_24h"] == 0
    assert body["verdict_may_change"] is True  # the seeded run is live and unannounced


async def test_a_pi_is_refused_and_an_anonymous_caller_is_sent_to_login(client, db_session, install_llm):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    pi = await _user(db_session, USER_ROLE_PI)
    responses = [
        await client.get(history_url(seeded.assessment_id), headers=auth_headers(pi.id)),
        *[
            await client.post(url, json={"question": "q"}, headers=auth_headers(pi.id))
            for url in (ask_url(seeded.assessment_id), clear_url(seeded.assessment_id))
        ],
    ]
    # Same {"error": "forbidden"} shape as get_review_user's own predicate would
    # produce, but from _refused, so an impersonating admin is checked first (below).
    assert [(r.status_code, r.json()) for r in responses] == [(403, {"error": "forbidden"})] * 3
    assert all(r.headers["cache-control"] == "no-store" for r in responses)
    anonymous = await client.get(history_url(seeded.assessment_id), follow_redirects=False)
    assert anonymous.status_code == 302
    assert anonymous.headers["location"] == "/login"  # never `next=` for a JSON endpoint
    assert fake.calls == []


@pytest.mark.parametrize("subject_role", [USER_ROLE_MANAGER, USER_ROLE_PI])
async def test_every_route_refuses_an_impersonating_admin(client, db_session, install_llm, subject_role):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    subject = await _user(db_session, subject_role)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={subject.id}"
    responses = [
        await client.get(history_url(seeded.assessment_id), headers=headers),
        await client.post(ask_url(seeded.assessment_id), json={"question": "q"}, headers=headers),
        await client.post(clear_url(seeded.assessment_id), json={}, headers=headers),
    ]
    # Impersonation wins even when the impersonated user (here a PI) would ALSO fail
    # the staff-or-reviewer check on its own — _refused checks impersonation first.
    assert [(r.status_code, r.json()) for r in responses] == [(403, {"error": "impersonating"})] * 3
    assert fake.calls == []
    plain = await client.get(history_url(seeded.assessment_id), headers=auth_headers(admin.id))
    assert plain.status_code == 200


async def test_impersonation_wins_over_a_malformed_assessment_id(client, db_session, install_llm):
    fake = install_llm()
    admin = await _user(db_session, USER_ROLE_ADMIN)
    pi = await _user(db_session, USER_ROLE_PI)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={pi.id}"
    resp = await client.get("/assessment-chat/not-a-uuid", headers=headers)
    assert (resp.status_code, resp.json()) == (403, {"error": "impersonating"})
    assert fake.calls == []


@pytest.mark.parametrize(
    "make_request",
    [
        lambda c, h: c.get("/assessment-chat/not-a-uuid", headers=h),
        lambda c, h: c.post("/assessment-chat/not-a-uuid/messages", json={"question": "q"}, headers=h),
        lambda c, h: c.post("/assessment-chat/not-a-uuid/clear", json={}, headers=h),
    ],
)
async def test_a_malformed_assessment_id_is_404_and_not_cached(client, db_session, install_llm, make_request):
    fake = install_llm()
    admin = await _user(db_session, USER_ROLE_ADMIN)
    resp = await make_request(client, auth_headers(admin.id))
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert resp.headers["cache-control"] == "no-store"
    assert fake.calls == []


async def test_a_disabled_chat_is_503_on_every_route(asgi_app, client, db_session, install_llm):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    asgi_app.state.assessment_chat_enabled = False
    headers = auth_headers(admin.id)
    responses = [
        await client.get(history_url(seeded.assessment_id), headers=headers),
        await client.post(ask_url(seeded.assessment_id), json={"question": "q"}, headers=headers),
        await client.post(clear_url(seeded.assessment_id), json={}, headers=headers),
    ]
    assert [(r.status_code, r.json()) for r in responses] == [(503, {"error": "disabled"})] * 3
    assert fake.calls == []


async def test_an_unknown_assessment_is_404_and_not_cached(client, db_session, install_llm):
    install_llm()
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    missing = uuid.uuid4()
    responses = [
        await client.get(history_url(missing), headers=headers),
        await client.post(ask_url(missing), json={"question": "q"}, headers=headers),
        await client.post(clear_url(missing), json={}, headers=headers),
    ]
    assert [(r.status_code, r.json()) for r in responses] == [(404, {"error": "not_found"})] * 3
    assert all(r.headers["cache-control"] == "no-store" for r in responses)


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded"]
)
async def test_posts_without_a_json_body_are_415(client, db_session, install_llm, content_type):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    if content_type:
        headers["Content-Type"] = content_type
    for url in (ask_url(seeded.assessment_id), clear_url(seeded.assessment_id)):
        resp = await client.post(url, content=b'{"question": "q"}', headers=headers)
        assert (resp.status_code, resp.json()) == (415, {"error": "unsupported_media_type"})
    assert fake.calls == []


async def test_cross_site_posts_are_refused_before_the_route(client_without_origin, db_session, install_llm):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    no_origin = await client_without_origin.post(
        ask_url(seeded.assessment_id), json={"question": "q"}, headers=headers
    )
    sibling = await client_without_origin.post(
        ask_url(seeded.assessment_id),
        json={"question": "q"},
        headers={**headers, "Origin": "https://copi.science"},
    )
    for resp in (no_origin, sibling):
        assert resp.status_code == 403
        assert "Cross-site request refused." in resp.text
    assert fake.calls == []


@pytest.mark.parametrize("question", ["", "   ", "x" * 4001, "nul" + chr(0) + "byte", None])
async def test_invalid_questions_are_400(client, db_session, install_llm, question):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    resp = await client.post(
        ask_url(seeded.assessment_id), json={"question": question}, headers=auth_headers(admin.id)
    )
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_question"})
    assert fake.calls == []


async def test_malformed_json_body_reads_as_a_missing_question(client, db_session, install_llm):
    """`_question` swallows a `request.json()` ValueError and returns None (syntax
    error, or non-UTF-8 bytes), so a malformed body reaches `chat.prepare_turn` exactly
    like an explicit `{"question": None}` and gets the same 400."""
    fake = install_llm()
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = {**auth_headers(admin.id), "Content-Type": "application/json"}
    resp = await client.post(ask_url(seeded.assessment_id), content=b"{not json", headers=headers)
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_question"})
    assert fake.calls == []


async def test_clear_succeeds_with_no_store(client, db_session, install_llm):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    resp = await client.post(clear_url(seeded.assessment_id), json={}, headers=auth_headers(admin.id))
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 0}  # nothing to delete: no turns were asked yet
    assert resp.headers["cache-control"] == "no-store"
    assert fake.calls == []


@pytest.mark.parametrize(
    "route,patch_target",
    [
        ("history", "list_history"),
        ("clear", "clear_history"),
    ],
)
async def test_a_storage_error_is_500_and_logs_only_the_class(
    client, db_session, install_llm, monkeypatch, caplog, route, patch_target
):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    question_text = "SECRET-QUESTION-TEXT-must-not-be-logged"

    async def _raise(*args, **kwargs):
        raise OperationalError("statement", {"question": question_text}, Exception("boom"))

    monkeypatch.setattr(chat_service, patch_target, _raise)

    with caplog.at_level(logging.ERROR, logger="src.routers.assessment_chat"):
        if route == "history":
            resp = await client.get(history_url(seeded.assessment_id), headers=headers)
        else:
            resp = await client.post(clear_url(seeded.assessment_id), json={}, headers=headers)

    assert (resp.status_code, resp.json()) == (500, {"error": "storage_error"})
    assert resp.headers["cache-control"] == "no-store"
    assert "OperationalError" in caplog.text
    assert question_text not in caplog.text
    assert fake.calls == []


async def test_a_question_streams_end_to_end_and_persists(client, db_session, install_llm):
    fake = install_llm(
        ChatScript(segments=[("The lab pitched CHAT-FIXTURE-PANEL.", [citation(1, 0, PITCH_TEXT)])])
    )
    seeded = await seed_interview(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)

    resp = await client.post(
        ask_url(seeded.assessment_id), json={"question": "What is proposed?"}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-store, no-transform"
    assert resp.headers["x-accel-buffering"] == "no"
    frames = parse_sse(resp.text)
    names = [name for name, _ in frames]
    assert names[0] == "turn" and names[-1] == "done"
    assert {"status", "text", "citation"} <= set(names)
    done = frames[-1][1]
    turn = done["turn"]
    assert turn["status"] == "complete"
    assert turn["answer_text"] == "The lab pitched CHAT-FIXTURE-PANEL."
    assert turn["citations"][0]["anchor"] == f"m-{seeded.message_ids[0]}"
    assert (done["questions_used_24h"], done["daily_limit"]) == (1, 100)
    assert frames[0][1]["turn_id"] == turn["id"]

    history = (await client.get(history_url(seeded.assessment_id), headers=headers)).json()
    assert [t["id"] for t in history["turns"]] == [turn["id"]]
    assert history["turns"][0]["in_window"] is True
    assert history["turns"][0]["record_changed"] is False

    ledger = (
        await db_session.execute(
            select(AssessmentChatUsage)
            .where(AssessmentChatUsage.turn_id == uuid.UUID(turn["id"]))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert ledger.status == "complete"
    assert (ledger.input_tokens, ledger.output_tokens, ledger.cache_creation_input_tokens) == (
        1200, 80, 30000,
    )
    [call] = fake.calls
    assert (call["model"], call["max_tokens"], call["fallbacks"]) == ("claude-opus-5-5", 12000, "default")
