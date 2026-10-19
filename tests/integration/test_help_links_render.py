"""Every Help link, checked as rendered (spec 2026-10-14 §6.6, §10.2).

A static scan cannot check these links: help ids and links sit inside
``{% if permissions.staff %}`` and ``{% if microsoft_sign_in %}`` blocks, the blind spot
tests/unit/test_reachability.py records. So each viewer (reviewer, manager, admin), with
Microsoft sign-in off and on, renders the help index, every reference page and tutorial
step it can open, and every page with a "Help with this page" link. Every ``/help`` link
in that HTML must open for the same viewer, and its fragment must be an ``id`` in the
target as that viewer sees it. The rendered-page gate walks only three help routes, so the
help pages and steps also get ``page_problems`` here.

Help pages render screenshots, so this needs the committed manifest and PNGs.
"""
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from src.services.web_permissions import capabilities_for
from src.web.help_pages import pages_for, tutorials_for
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_rendered_page_gate import (  # noqa: F401
    GateWorld,
    gate_world,
    page_problems,
)
from tests.oidc_fakes import make_config

pytestmark = pytest.mark.integration

HELP_LINK_TEXT = "Help with this page"

#: Spec §6.6: (page URL from the seed, the help link it must render, staff only).
LINKED_PAGES: tuple[tuple[Callable[[GateWorld], str], str, bool], ...] = (
    (lambda w: "/workspace/pis", "/help/pis#directory", False),
    (lambda w: f"/workspace/pis/{w.users['pi'].id}", "/help/pis#detail", False),
    (lambda w: "/workspace/assessments", "/help/assessments#list", False),
    (lambda w: f"/workspace/assessments/{w.assessment_id}", "/help/assessments#detail", False),
    (lambda w: "/settings", "/help/getting-started#settings", False),
    (lambda w: "/workspace/slack-bots", "/help/managing-pis#slack-bots", True),
    (lambda w: "/workspace/staff", "/help/staff-accounts", True),
    (lambda w: "/workspace/discussions", "/help/discussions-activity#discussions", True),
    (lambda w: "/workspace/interviews", "/help/discussions-activity#interviews", True),
    (lambda w: "/workspace/activity", "/help/discussions-activity#activity", True),
    (lambda w: f"/workspace/activity/{w.run.id}", "/help/discussions-activity#run-detail", True),
    (lambda w: "/workspace/prompt-suggestions", "/help/prompt-suggestions", True),
    (lambda w: f"/workspace/prompt-suggestions/{w.suggestion.id}",
     "/help/prompt-suggestions#detail", True),
)


class _Links(HTMLParser):
    """Every ``<a href>`` in a document, with its text content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)


def _links(html: str) -> list[tuple[str, str]]:
    parser = _Links()
    parser.feed(html)
    parser.close()
    return parser.links


def _is_help_href(href: str) -> bool:
    parts = urlsplit(href)
    return (not parts.scheme and not parts.netloc
            and (parts.path == "/help" or parts.path.startswith("/help/")))


def _help_urls(permissions) -> list[str]:
    urls = ["/help"] + [f"/help/{page.slug}" for page in pages_for(permissions)]
    for tutorial in tutorials_for(permissions):
        urls += [f"/help/tutorials/{tutorial.key}/{n}" for n in range(1, len(tutorial.steps) + 1)]
    return urls


@pytest.mark.parametrize("microsoft", [False, True], ids=["microsoft-off", "microsoft-on"])
@pytest.mark.parametrize("role", [USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN])
async def test_every_help_link_opens_and_every_fragment_exists(client, asgi_app, gate_world, role, microsoft):  # noqa: F811
    viewer = gate_world.users[role]
    headers = auth_headers(viewer.id)
    permissions = capabilities_for(viewer)
    previous = asgi_app.state.microsoft_sign_in
    asgi_app.state.microsoft_sign_in = make_config() if microsoft else None
    try:
        problems: list[str] = []
        targets: dict[str, httpx.Response] = {}

        async def open_target(path: str) -> httpx.Response:
            if path not in targets:
                targets[path] = await client.get(path, headers=headers, follow_redirects=True)
            return targets[path]

        rendered: dict[str, str] = {}
        for url in _help_urls(permissions):
            response = await open_target(url)
            if response.status_code != 200:
                problems.append(f"{url}: HTTP {response.status_code}")
                continue
            rendered[url] = response.text
            problems += [f"{url}: {p}" for p in page_problems(response.text)]

        for build, expected, staff_only in LINKED_PAGES:
            if staff_only and not permissions.staff:
                continue
            url = build(gate_world)
            response = await client.get(url, headers=headers, follow_redirects=False)
            if response.status_code != 200:
                problems.append(f"{url}: HTTP {response.status_code}")
                continue
            rendered[url] = response.text
            help_links = [href for href, text in _links(response.text) if HELP_LINK_TEXT in text]
            if help_links != [expected]:
                problems.append(f"{url}: help links {help_links}, expected [{expected!r}]")

        for url, html in rendered.items():
            for href, _text in _links(html):
                if not _is_help_href(href):
                    continue
                parts = urlsplit(href)
                target = await open_target(href.split("#", 1)[0])
                if target.status_code != 200:
                    problems.append(f"{url}: {href} -> HTTP {target.status_code}")
                elif parts.fragment and f'id="{parts.fragment}"' not in target.text:
                    problems.append(f"{url}: {href} -> no id={parts.fragment!r} in the target")
    finally:
        asgi_app.state.microsoft_sign_in = previous
    assert not problems, "\n".join(problems)


async def test_settings_has_no_help_link_for_a_pi(client, gate_world):  # noqa: F811
    response = await client.get("/settings", headers=auth_headers(gate_world.users["pi"].id))
    assert response.status_code == 200
    assert HELP_LINK_TEXT not in response.text
    assert not [href for href, _text in _links(response.text) if _is_help_href(href)]
