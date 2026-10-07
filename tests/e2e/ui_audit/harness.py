"""The object journeys and the crawl drive: a browser plus forged sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: The axe-core bundle the crawl and later phases' journeys inject. run.py downloads it
#: once to AXE_PATH (gitignored) and refuses a file whose sha256 differs from the pin
#: (axe-core 4.13.0 axe.min.js, measured 2026-10-01). Journeys read it from AXE_PATH or
#: use ``h.axe_source``.
AXE_VERSION = "4.13.0"
AXE_URL = f"https://cdn.jsdelivr.net/npm/axe-core@{AXE_VERSION}/axe.min.js"
AXE_SHA256 = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
AXE_PATH = Path(__file__).with_name("axe.min.js")


def expected_http_error(url, status, method, role, base_url, ids):
    """Only intentional negative crawl requests may omit a network violation."""
    parsed = urlsplit(url)
    if not url.startswith(base_url + '/') or method != 'GET':
        return False
    path = parsed.path
    if status == 404:
        bad = ids['bad_uuid']
        return path in {'/agent/nosuch/dashboard', f'/admin/users/{bad}',
                        f'/workspace/assessments/{bad}'}
    if status != 403:
        return False
    if role == 'pending':
        return path not in {'/', '/login', '/access-pending'}
    if path.startswith('/admin/'):
        return role != 'admin'
    if path.startswith(('/workspace/pis', '/workspace/assessments', '/assessment-chat/')):
        return role not in {'admin', 'manager', 'reviewer'}
    if path.startswith('/workspace/'):
        return role not in {'admin', 'manager'}
    if path.startswith(('/profile', '/onboarding', '/agent')):
        return role in {'manager', 'reviewer'} or (path.startswith('/agent/obrien/') and role not in {'pi', 'delegate'})
    return False


@dataclass
class Harness:
    base_url: str
    ids: dict[str, Any]
    browser: Any
    cookie_name: str
    secret_key: str
    axe_source: str = ""
    #: The exact environment harness subprocesses get (env.harness_env); never os.environ.
    env: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def cookie_for(self, role: str) -> str:
        import os

        os.environ["SECRET_KEY"] = self.secret_key
        from tests.e2e.session import forge_session_cookie

        return forge_session_cookie(self.ids[role])

    async def page(self, role: str | None, width: int = 1280, bypass_csp: bool = False,
                   answer_dialogs: bool = True):
        """A fresh context signed in as ``role`` (an ``ids`` key); None or "anon" = no session.
        Every dialog is logged and dismissed, unless ``answer_dialogs`` is False: then the
        caller installs the page's only dialog handler (a dialog answered twice raises)."""
        ctx = await self.browser.new_context(
            viewport={"width": width, "height": 900}, bypass_csp=bypass_csp)
        # None and "anon" both mean a visitor with no session cookie (later phases'
        # journeys pass "anon" from their role tuples).
        if role and role != "anon":
            await ctx.add_cookies([{"name": self.cookie_name, "value": self.cookie_for(role),
                                    "url": self.base_url}])
        page = await ctx.new_page()
        log: dict[str, list[str]] = {
            "console": [], "pageerror": [], "dialogs": [], "requestfailed": [],
            "unexpected_network": [], "expected_403": [], "expected_404": [],
        }
        def console(message):
            if message.type != 'error':
                return
            url = message.location.get('url', '')
            network_notice = 'server responded with a status of' in message.text
            if network_notice and any(expected_http_error(url, status, 'GET', role,
                                      self.base_url, self.ids) for status in (403, 404)):
                return
            log['console'].append(f'{message.type}: {message.text}'[:300])

        page.on('console', console)
        page.on("pageerror", lambda e: log["pageerror"].append(str(e)[:300]))
        page.on("requestfailed", lambda request: log["requestfailed"].append(
            f"{request.method} {request.url}: {request.failure}"[:300]))

        def response(response):
            if expected_http_error(response.url, response.status, response.request.method,
                                   role, self.base_url, self.ids):
                log[f"expected_{response.status}"].append(
                    f"{response.request.method} {response.url}"[:300])
            elif response.status >= 400:
                log["unexpected_network"].append(
                    f"{response.status} {response.request.method} {response.url}"[:300])

        page.on("response", response)

        async def _dialog(d):
            log["dialogs"].append(f"{d.type}: {d.message[:160]}")
            await d.dismiss()

        if answer_dialogs:
            page.on("dialog", _dialog)
        return ctx, page, log
