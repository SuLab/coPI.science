"""The rendered-page gate (spec §6.10, decision D12; X-01, X-02, X-05, and §6.3's
inline-handler removal).

Every GET page the app serves is rendered against factory data as an anonymous visitor, a
PI who owns an agent, a reviewer, a manager and an admin. Every HTML response that is not a
redirect (error pages included) must hold four properties:

* every ``input`` (other than hidden/submit/button/reset/image), ``select`` and ``textarea``
  has an accessible name: a non-empty ``aria-label`` or ``aria-labelledby``, a wrapping
  ``<label>`` with text, or a ``<label for>`` with text pointing at its ``id`` (a ``title``
  or ``placeholder`` alone does not count);
* no ``text-gray-300``/``text-gray-400`` anywhere in the document, scripts included (X-01);
* no ``<tr onclick>`` (X-05);
* no inline ``on…=`` attribute on any element (the Phase 2 enforced CSP blocks them).

The page list is the app's own route table (``http_routes``, shared with
``tests/unit/test_reachability.py``): a new GET route fails
``test_every_get_route_is_walked_or_skipped`` until it is added to ``PAGES`` with a URL
built from the seed, or to ``SKIPPED`` with the reason it renders no HTML page. Phase 2
extends ``page_problems``.
"""
import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    Cohort,
    CohortMembership,
    DelegateInvitation,
    PromptChangeSuggestion,
)
from tests import factories
from tests.assessment_chat_support import seed_interview
from tests.integration.test_manager_access import auth_headers
from tests.unit.test_reachability import http_routes

pytestmark = pytest.mark.integration

ROLES = ("anon", "pi", "reviewer", "manager", "admin")
_PROMPT_FILE = "prompts/agent-system.md"


@dataclass
class GateWorld:
    users: dict[str, Any]
    agent: Any
    run: Any
    assessment_id: uuid.UUID
    root_ts: str
    cohort: Any
    suggestion: Any
    invite_token: str


PAGES: dict[str, Callable[[GateWorld], str]] = {
    "/": lambda w: "/",
    "/access-pending": lambda w: "/access-pending",
    "/admin": lambda w: "/admin",
    "/admin/access-requests": lambda w: "/admin/access-requests",
    "/admin/activity": lambda w: "/admin/activity",
    "/admin/activity/{run_id}": lambda w: f"/admin/activity/{w.run.id}",
    "/admin/activity/{run_id}/llm-calls": lambda w: f"/admin/activity/{w.run.id}/llm-calls",
    "/admin/agents": lambda w: "/admin/agents",
    "/admin/agents/{agent_id}": lambda w: f"/admin/agents/{w.agent.id}",
    "/admin/assessments": lambda w: "/admin/assessments",
    "/admin/assessments/{assessment_id}": lambda w: f"/admin/assessments/{w.assessment_id}",
    "/admin/cohorts": lambda w: "/admin/cohorts",
    "/admin/cohorts/topology": lambda w: "/admin/cohorts/topology",
    "/admin/cohorts/{cohort_id}": lambda w: f"/admin/cohorts/{w.cohort.id}",
    "/admin/discussions": lambda w: "/admin/discussions",
    "/admin/jobs": lambda w: "/admin/jobs",
    "/admin/simulation": lambda w: "/admin/simulation",
    "/admin/users": lambda w: "/admin/users",
    "/admin/users/{user_id}": lambda w: f"/admin/users/{w.users['pi'].id}",
    "/agent": lambda w: "/agent",
    "/agent/{agent_id}/conversations": lambda w: f"/agent/{w.agent.agent_id}/conversations",
    "/agent/{agent_id}/dashboard": lambda w: f"/agent/{w.agent.agent_id}/dashboard",
    "/agent/{agent_id}/public-profile": lambda w: f"/agent/{w.agent.agent_id}/public-profile",
    "/agent/{agent_id}/public-profile/edit": lambda w: f"/agent/{w.agent.agent_id}/public-profile/edit",
    "/agent/{agent_id}/thread/{message_ts}": lambda w: f"/agent/{w.agent.agent_id}/thread/{w.root_ts}",
    "/invite/{token}": lambda w: f"/invite/{w.invite_token}",
    "/login": lambda w: "/login",
    "/manager": lambda w: "/manager",
    "/manager/activity": lambda w: "/manager/activity",
    "/manager/activity/{run_id}": lambda w: f"/manager/activity/{w.run.id}",
    "/manager/assessments": lambda w: "/manager/assessments",
    "/manager/assessments/{assessment_id}": lambda w: f"/manager/assessments/{w.assessment_id}",
    "/manager/discussions": lambda w: "/manager/discussions",
    "/manager/pis": lambda w: "/manager/pis",
    "/manager/pis/{user_id}": lambda w: f"/manager/pis/{w.users['pi'].id}",
    "/manager/prompt-suggestions": lambda w: "/manager/prompt-suggestions",
    "/manager/prompt-suggestions/{suggestion_id}": lambda w: f"/manager/prompt-suggestions/{w.suggestion.id}",
    "/manager/slack-bots": lambda w: "/manager/slack-bots",
    "/onboarding": lambda w: "/onboarding",
    "/profile": lambda w: "/profile",
    "/profile/delete-account": lambda w: "/profile/delete-account",
    "/profile/edit": lambda w: "/profile/edit",
    "/settings": lambda w: "/settings",
}

SKIPPED: dict[str, str] = {
    "/admin/agents/slack/callback": "Slack OAuth redirect target; redirects, renders no page",
    "/auth/callback": "ORCID OAuth redirect target; redirects, renders no page",
    "/login/start": "redirects to ORCID",
    "/assessment-chat/{assessment_id}": "JSON for static/js/assessment_chat.js",
    "/api/health": "JSON health probe",
    "/admin/activity/{run_id}/llm-calls/{call_id}/bodies": "HTML fragment for the llm_calls page; no layout",
}

#: Walked (any HTML they return is still checked) but expected to answer every role with a
#: redirect or an error page, so the "something rendered 200" control skips them.
NO_200_EXPECTED: dict[str, str] = {
    "/": "redirects to /profile or /login",
    "/manager": "redirects to /manager/pis",
    "/onboarding": "an onboarded user is redirected to /profile; anonymous to /login",
    "/invite/{token}": "accepting needs the invitee's own verified address (spec §6.6); no gate role is the invitee",
}

_SKIP_INPUT_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})
_LOW_CONTRAST = re.compile(r"(?<![\w-])text-gray-[34]00(?![\w-])")


class _GateParser(HTMLParser):
    """Collects form controls, the labels naming them, and inline event attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[dict[str, Any]] = []
        self.label_text_for: dict[str, str] = {}
        self.inline_handlers: set[str] = set()
        self.tr_onclick = 0
        self._labels: list[dict[str, Any]] = []

    def handle_starttag(self, tag, attrs):
        a = {name: (value or "") for name, value in attrs}
        for name in a:
            if name.startswith("on"):
                self.inline_handlers.add(f"<{tag} {name}=>")
                if tag == "tr" and name == "onclick":
                    self.tr_onclick += 1
        if tag == "label":
            self._labels.append({"for": a.get("for"), "text": [], "controls": []})
            return
        if tag in ("input", "select", "textarea"):
            if tag == "input" and a.get("type", "text").lower() in _SKIP_INPUT_TYPES:
                return
            self.controls.append({"tag": tag, "attrs": a, "line": self.getpos()[0], "wrapped": False})
            for label in self._labels:
                label["controls"].append(len(self.controls) - 1)

    def handle_endtag(self, tag):
        if tag != "label" or not self._labels:
            return
        label = self._labels.pop()
        text = " ".join("".join(label["text"]).split())
        if not text:
            return
        for index in label["controls"]:
            self.controls[index]["wrapped"] = True
        if label["for"]:
            self.label_text_for[label["for"]] = text

    def handle_data(self, data):
        for label in self._labels:
            label["text"].append(data)

    def unnamed_controls(self) -> list[str]:
        out = []
        for control in self.controls:
            a = control["attrs"]
            if a.get("aria-label", "").strip() or a.get("aria-labelledby", "").strip():
                continue
            if control["wrapped"] or (a.get("id") and a["id"] in self.label_text_for):
                continue
            out.append(
                f"line {control['line']}: <{control['tag']} name={a.get('name')!r} "
                f"id={a.get('id')!r}> has no accessible name"
            )
        return out


def page_problems(html: str) -> list[str]:
    """Every gate violation in one HTML document."""
    parser = _GateParser()
    parser.feed(html)
    parser.close()
    problems = parser.unnamed_controls()
    if _LOW_CONTRAST.search(html):
        problems.append("uses text-gray-300/text-gray-400 (X-01)")
    if parser.tr_onclick:
        problems.append(f"{parser.tr_onclick} <tr onclick> row(s) (X-05)")
    problems += [f"inline handler {h}" for h in sorted(parser.inline_handlers)]
    return problems


def test_page_problems_catches_each_defect_and_accepts_each_naming():
    bad = (
        '<input name="a"><select name="b"></select><textarea name="c" title="t"></textarea>'
        '<p class="text-gray-400">x</p><table><tr onclick="go()"><td>1</td></tr></table>'
        '<button onclick="x()">b</button>'
    )
    problems = page_problems(bad)
    assert sum("has no accessible name" in p for p in problems) == 3
    assert any("text-gray-300/text-gray-400" in p for p in problems)
    assert any("<tr onclick>" in p for p in problems)
    assert "inline handler <button onclick=>" in problems
    good = (
        '<label for="a">A</label><input id="a" name="a">'
        '<label>B <select name="b"></select></label>'
        '<textarea name="c" aria-label="C"></textarea>'
        '<input type="hidden" name="h"><input type="submit" value="Go">'
        '<p class="text-gray-600">x</p><script>var s = "<tr onclick>";</script>'
    )
    assert page_problems(good) == []


def test_every_get_route_is_walked_or_skipped():
    routes = {r.path for r in http_routes() if r.method == "GET"}
    assert not set(PAGES) & set(SKIPPED)
    missing = routes - set(PAGES) - set(SKIPPED)
    stale = (set(PAGES) | set(SKIPPED)) - routes
    assert not missing, f"GET routes the gate neither walks nor skips: {sorted(missing)}"
    assert not stale, f"gate entries for routes that no longer exist: {sorted(stale)}"
    assert set(NO_200_EXPECTED) <= set(PAGES)


@pytest.fixture
async def gate_world(db_session) -> GateWorld:
    users = {
        "admin": await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Gate Admin"),
        "manager": await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Gate Manager"),
        "reviewer": await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Gate Reviewer"),
        "pi": await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Gate PI"),
    }
    await factories.make_profile(
        db_session, user=users["pi"], evidence_pub_count=1,
        techniques=["cryo-EM", "1,2-dichloroethane"], keywords=["kinase"],
    )
    agent = await factories.make_agent(
        db_session, user=users["pi"], agent_id="gatepi", bot_name="GatePiBot", pi_name="Gate PI",
    )
    await factories.make_agent(
        db_session, agent_id="blackbird", bot_name="BlackbirdBot", pi_name="Blackbird",
        role="scout_hub",
    )
    now = datetime.now(UTC)
    run = await factories.make_simulation_run(
        db_session, status="stopped", started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=1),
    )
    interview = await seed_interview(db_session, run=run, subject="gatepi", channel="gate-interview")
    db_session.add(AssessmentReview(
        assessment_id=interview.assessment_id, reviewer_user_id=users["reviewer"].id,
        reviewer_name="Gate Reviewer", score=3, comment="Gate review", feedback_mode="learn",
    ))
    cohort = Cohort(name="gate-cohort", created_by=users["admin"].id)
    db_session.add(cohort)
    await db_session.flush()
    db_session.add(CohortMembership(cohort_id=cohort.id, agent_id="gatepi", added_by=users["admin"].id))
    suggestion = PromptChangeSuggestion(
        assessment_id=interview.assessment_id,
        subject_label="Gate PI — Widget Co",
        feedback_snapshot=[{
            "id": str(uuid.uuid4()), "reviewer_name": "Gate Reviewer", "score": 3,
            "feedback_mode": "learn", "comment": "Missed the IP angle.",
            "created_at": "2026-10-01T00:00:00+00:00",
        }],
        target="scout_hub",
        prompt_files=[{
            "path": _PROMPT_FILE,
            "sha256_12": hashlib.sha256(Path(_PROMPT_FILE).read_bytes()).hexdigest()[:12],
        }],
        suggestion="Ask about the assay.",
        transcript_available=True,
    )
    db_session.add(suggestion)
    db_session.add(DelegateInvitation(
        agent_registry_id=agent.id, invited_by_user_id=users["pi"].id,
        email="invitee@example.org", token="gate-invite-token", status="pending",
        expires_at=now + timedelta(days=1),
    ))
    await db_session.flush()
    return GateWorld(
        users=users, agent=agent, run=run, assessment_id=interview.assessment_id,
        root_ts=interview.root_ts, cohort=cohort, suggestion=suggestion,
        invite_token="gate-invite-token",
    )


async def test_every_page_passes_the_gate(client, gate_world):
    problems: list[str] = []
    rendered_200: set[str] = set()
    for route, build in sorted(PAGES.items()):
        url = build(gate_world)
        for role in ROLES:
            headers = {} if role == "anon" else auth_headers(gate_world.users[role].id)
            r = await client.get(url, headers=headers, follow_redirects=False)
            if r.status_code >= 500:
                problems.append(f"{role} {url}: HTTP {r.status_code}")
                continue
            if 300 <= r.status_code < 400:
                continue
            if not r.headers.get("content-type", "").startswith("text/html"):
                continue
            if r.status_code == 200:
                rendered_200.add(route)
            problems += [f"{role} {url}: {p}" for p in page_problems(r.text)]
    never = set(PAGES) - rendered_200 - set(NO_200_EXPECTED)
    assert not never, f"no role rendered these with 200, so the gate checked nothing there: {sorted(never)}"
    assert not problems, "\n".join(problems)
