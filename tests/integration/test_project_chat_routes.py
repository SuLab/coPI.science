"""The project chat's routes (docs/specs/2026-10-27-projects-pages-redesign-design.md §8.1,
§8.3): who may call them, the refusal order, the headers they send, and each route's happy
path with the fake model."""

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AssessmentChatOpen,
)
from src.routers import project_chat as chat_router
from tests import factories
from tests.assessment_chat_support import parse_sse, use_fake_llm, use_test_session
from tests.fakes import ChatScript, FakeAsyncAnthropic
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_project_chat_service import PROJECT_PROMPT, seed_project

pytestmark = pytest.mark.integration

ROUTES = ("history", "ask", "clear", "opened")


@pytest.fixture
def install_llm(monkeypatch, db_session, asgi_app, tmp_path):
    """Both chat switches on, the producer on the test session, the project prompt on a
    fixture file; returns a function that installs a FakeAsyncAnthropic."""
    from src.services import assessment_chat as chat

    asgi_app.state.assessment_chat_enabled = True
    asgi_app.state.project_chat_enabled = True
    use_test_session(monkeypatch, db_session)
    prompt = tmp_path / "project-chat.md"
    prompt.write_text(PROJECT_PROMPT, encoding="utf-8")
    monkeypatch.setattr(chat, "PROJECT_PROMPT_PATH", prompt)

    def _install(*scripts: ChatScript) -> FakeAsyncAnthropic:
        fake = FakeAsyncAnthropic(list(scripts))
        use_fake_llm(monkeypatch, fake)
        return fake

    return _install


async def _user(db, role):
    return await factories.make_user(db, user_role=role)


async def _call(client, route, project_id, headers, **kwargs):
    base = f"/project-chat/{project_id}"
    if route == "history":
        return await client.get(base, headers=headers)
    path = {"ask": "/messages", "clear": "/clear", "opened": "/opened"}[route]
    if "content" not in kwargs:
        kwargs["json"] = {"question": "What is proposed?", "via": "bubble"}
    return await client.post(base + path, headers=headers, **kwargs)


async def _all(client, project_id, headers, **kwargs):
    return [await _call(client, route, project_id, headers, **kwargs) for route in ROUTES]


def test_the_router_has_exactly_the_four_routes():
    routes = {(tuple(sorted(route.methods)), route.path) for route in chat_router.router.routes}
    assert routes == {
        (("GET",), "/{project_id}"),
        (("POST",), "/{project_id}/messages"),
        (("POST",), "/{project_id}/clear"),
        (("POST",), "/{project_id}/opened"),
    }


# --- refusal order ------------------------------------------------------------


@pytest.mark.parametrize("subject_role", [USER_ROLE_MANAGER, USER_ROLE_PI])
async def test_every_route_refuses_an_impersonating_admin(
    client, db_session, install_llm, subject_role
):
    fake = install_llm()
    pid = await seed_project(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    subject = await _user(db_session, subject_role)
    headers = auth_headers(admin.id, impersonate=subject.id)
    responses = await _all(client, pid, headers)
    assert [(r.status_code, r.json()) for r in responses] == [(403, {"error": "impersonating"})] * 4
    # Impersonation wins over a malformed id too.
    bad = await client.get("/project-chat/not-a-uuid", headers=headers)
    assert (bad.status_code, bad.json()) == (403, {"error": "impersonating"})
    assert fake.calls == []


async def test_a_pi_is_forbidden_and_an_anonymous_caller_is_sent_to_login(
    client, db_session, install_llm
):
    fake = install_llm()
    pid = await seed_project(db_session)
    pi = await _user(db_session, USER_ROLE_PI)
    responses = await _all(client, pid, auth_headers(pi.id))
    assert [(r.status_code, r.json()) for r in responses] == [(403, {"error": "forbidden"})] * 4
    assert all(r.headers["cache-control"] == "no-store" for r in responses)
    anonymous = await client.get(f"/project-chat/{pid}", follow_redirects=False)
    assert anonymous.status_code == 302
    assert anonymous.headers["location"] == "/login"  # never `next=` for a JSON endpoint
    assert fake.calls == []


@pytest.mark.parametrize("switch", ["assessment_chat_enabled", "project_chat_enabled"])
async def test_either_switch_off_is_503_on_every_route(
    asgi_app, client, db_session, install_llm, switch
):
    fake = install_llm()
    pid = await seed_project(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    setattr(asgi_app.state, switch, False)
    responses = await _all(client, pid, auth_headers(admin.id))
    # 503 comes before the id checks.
    responses.append(await client.get("/project-chat/not-a-uuid", headers=auth_headers(admin.id)))
    assert [(r.status_code, r.json()) for r in responses] == [(503, {"error": "disabled"})] * 5
    assert fake.calls == []


async def test_a_bad_or_unknown_id_is_404(client, db_session, install_llm):
    fake = install_llm()
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    responses = await _all(client, "not-a-uuid", headers)
    responses += await _all(client, "00000000-0000-4000-8000-000000000000", headers)
    assert [(r.status_code, r.json()) for r in responses] == [(404, {"error": "not_found"})] * 8
    assert all(r.headers["cache-control"] == "no-store" for r in responses)
    assert fake.calls == []


async def test_an_open_project_is_409_before_the_media_type(client, db_session, install_llm):
    fake = install_llm()
    pid = await seed_project(db_session, status="posted")
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    responses = await _all(client, pid, headers)
    responses += [
        await _call(client, route, pid, {**headers, "Content-Type": "text/plain"}, content=b"{}")
        for route in ("ask", "clear", "opened")
    ]
    assert [(r.status_code, r.json()) for r in responses] == [(409, {"error": "project_open"})] * 7
    assert fake.calls == []


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
async def test_posts_without_a_json_body_are_415(client, db_session, install_llm, content_type):
    fake = install_llm()
    pid = await seed_project(db_session)
    admin = await _user(db_session, USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    if content_type:
        headers["Content-Type"] = content_type
    for route in ("ask", "clear", "opened"):
        resp = await _call(client, route, pid, headers, content=b'{"question": "q"}')
        assert (resp.status_code, resp.json()) == (415, {"error": "unsupported_media_type"})
    assert fake.calls == []


async def test_unknown_paths_under_project_chat_stay_json(client):
    r = await client.get("/project-chat/a/b/c")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": "Not Found"}


# --- happy paths ----------------------------------------------------------------


@pytest.mark.parametrize("role", [USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER])
async def test_history_ask_clear_and_opened(client, db_session, install_llm, role):
    fake = install_llm(ChatScript(segments=[("A PROJECT ANSWER.", [])]))
    pid = await seed_project(db_session)
    user = await _user(db_session, role)
    user_id = user.id
    headers = auth_headers(user_id)

    empty = await _call(client, "history", pid, headers)
    assert empty.status_code == 200
    assert empty.headers["cache-control"] == "no-store"
    body = empty.json()
    assert body["tier"] == ("reviewer" if role == USER_ROLE_REVIEWER else "staff")
    assert body["turns"] == []
    assert body["verdict_may_change"] is False

    asked = await _call(client, "ask", pid, headers)
    assert asked.status_code == 200
    assert asked.headers["content-type"].startswith("text/event-stream")
    assert asked.headers["cache-control"] == "no-store, no-transform"
    assert asked.headers["x-accel-buffering"] == "no"
    frames = parse_sse(asked.text)
    assert frames[0][0] == "turn"
    assert frames[-1][0] == "done"
    assert frames[-1][1]["turn"]["answer_text"] == "A PROJECT ANSWER."
    assert fake.calls[0]["system"] == [{"type": "text", "text": PROJECT_PROMPT}]

    history = await _call(client, "history", pid, headers)
    assert [t["question"] for t in history.json()["turns"]] == ["What is proposed?"]
    polled = await client.get(f"/project-chat/{pid}?poll=1", headers=headers)
    assert polled.status_code == 200

    opened = await _call(client, "opened", pid, headers)
    assert (opened.status_code, opened.json()) == (200, {"ok": True})
    assert opened.headers["cache-control"] == "no-store"
    rows = (
        await db_session.execute(
            select(AssessmentChatOpen).where(AssessmentChatOpen.user_id == user_id)
        )
    ).scalars().all()
    assert [(r.subject, r.project_id, r.opened_via) for r in rows] == [("project", pid, "bubble")]

    cleared = await _call(client, "clear", pid, headers)
    assert (cleared.status_code, cleared.json()) == (200, {"deleted": 1})
    assert cleared.headers["cache-control"] == "no-store"
    assert (await _call(client, "history", pid, headers)).json()["turns"] == []
