"""The object journeys and the crawl drive: a browser plus forged sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The axe-core bundle the crawl and later phases' journeys inject. run.py downloads it
#: once to AXE_PATH (gitignored) and refuses a file whose sha256 differs from the pin
#: (axe-core 4.13.0 axe.min.js, measured 2026-10-01). Journeys read it from AXE_PATH or
#: use ``h.axe_source``.
AXE_VERSION = "4.13.0"
AXE_URL = f"https://cdn.jsdelivr.net/npm/axe-core@{AXE_VERSION}/axe.min.js"
AXE_SHA256 = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
AXE_PATH = Path(__file__).with_name("axe.min.js")


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
        log: dict[str, list[str]] = {"console": [], "pageerror": [], "dialogs": []}
        page.on("console", lambda m: log["console"].append(f"{m.type}: {m.text}"[:300])
                if m.type == "error" else None)
        page.on("pageerror", lambda e: log["pageerror"].append(str(e)[:300]))

        async def _dialog(d):
            log["dialogs"].append(f"{d.type}: {d.message[:160]}")
            await d.dismiss()

        if answer_dialogs:
            page.on("dialog", _dialog)
        return ctx, page, log
