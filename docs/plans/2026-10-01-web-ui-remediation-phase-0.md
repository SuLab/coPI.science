# Web UI remediation — Phase 0 (hotfix) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo, plan execution goes through `/engineering:plan-execution`.

**Goal:** Close the high-severity web UI findings A-01, A-02, B-01, C-01 (with C-08, C-09, C-27, FN-05) and commit the browser harness that proves it.

**Architecture:** One shared `data-confirm` listener replaces inline confirm handlers; the page markdown renderer escapes raw HTML and sanitizes with an explicit allowlist; the simulation panel refreshes unit by unit and never touches a focused or edited form; the timeline re-measures clamped messages when a `<details>` opens. A committed Playwright harness runs the audit's reproductions against a throwaway local instance.

**Tech Stack:** FastAPI 0.142.2 / Starlette 1.7.0 (production), Jinja2 3.1.6, vanilla JS, marked 12.0.2, DOMPurify 3.1.6 (CDN, unchanged in this phase), Playwright (Python, `.venv-test` has 1.62.0), Docker (`postgres:15`).

**Spec:** `docs/specs/2026-10-01-web-ui-remediation-design.md` (§5). Audit: `docs/audits/2026-10-01-web-ui/README.md`.

## Global Constraints

- Branch `webui/phase-0` cut from `blackbird`; merged back when §Task 0-7's gates pass. Never push `origin` (spec D15).
- Deploy unit: web and worker images only; no migration; no agent rebuild (spec §4).
- No change under `prompts/`; never touch `docker-compose.prod.yml`; no simulation start.
- `./scripts/ci.sh` must pass: ruff zero findings on `tests/` (incl. `tests/e2e`), `src/` ratchet, C901 ≤ 20, 200-line function gate, coverage floor.
- Page renderer allowlist (spec §5.2): `ALLOWED_TAGS = p br strong em code pre blockquote ul ol li a h1 h2 h3 h4 h5 h6 hr table thead tbody tr th td`; `ALLOWED_URI_REGEXP = /^(?:https?:|mailto:|#)/i`; `ALLOWED_ATTR = href title start align` with `ALLOW_DATA_ATTR` and `ALLOW_ARIA_ATTR` off — `title` carries `md_citations`' cited URL (spec amendment A1), `start`/`align` keep marked's list numbering and table alignment, and DOMPurify would otherwise keep `data-*`/`aria-*` (amendment A13, §12).
- A lone `<br>` (regex `^<br\s*\/?>$`, case-insensitive, after trim) is the only raw HTML rendered as HTML.
- The harness never reads the repo's `.env` and never writes the repo's `profiles/`: it runs with a temporary working directory (own empty `profiles/`) and explicit environment (Task 0-1), blocks all outbound email (`OUTBOUND_EMAIL_ALLOWLIST=nobody@invalid.example`) and disables AWS credential discovery (`AWS_EC2_METADATA_DISABLED=true`, invalid static keys).

## Review Focus

1. **A name containing a backslash, a double quote, `</script>`, or a curly apostrophe** must still show the Delete dialog and run nothing — Task 0-3 tests all four.
2. **Markdown that is only raw HTML on one line inside a paragraph** (inline `<b>x</b>`) and **block HTML at line start** (`<div>`): both must render as visible text — Task 0-4's harness journey checks both shapes.
3. **The simulation page while a run starts mid-view** (sections appear that were not on the page): they must be inserted, and a dirty Start form must still be kept — Task 0-5 journey `sim_new_sections_inserted`.
4. **A refresh while focus is on a Stop button or a `<summary>`** must not move focus — Task 0-5 journey `sim_focus_kept`.
5. **Opening the timeline with "Expand all" rather than a click** must reveal the toggle — Task 0-6 journey uses the button.

## File map

| File | Responsibility |
|---|---|
| `tests/e2e/ui_audit/__init__.py` | package marker |
| `tests/e2e/ui_audit/env.py` | isolated working directory and environment for the harness processes |
| `tests/e2e/ui_audit/postgres.py` | throwaway Postgres container lifecycle |
| `tests/e2e/ui_audit/seed.py` | adversarial seed (run as a module against the throwaway DB) |
| `tests/e2e/ui_audit/serve.py` | app server with the fake model (run as a module) |
| `tests/e2e/ui_audit/harness.py` | `Harness`: browser, cookie forging, page factory |
| `tests/e2e/ui_audit/crawl.py` | role × route crawl and its invariant checks |
| `tests/e2e/ui_audit/journeys_phase0.py` | Phase 0 journeys |
| `tests/e2e/ui_audit/run.py` | CLI: `serve`, `crawl`, `journeys --phase N`, `all --phase N` |
| `tests/e2e/ui_audit/README.md` | how to run, what it touches, safety |
| `.gitignore` | ignores the downloaded, checksum-pinned `tests/e2e/ui_audit/axe.min.js` |
| `docs/operations/testing.md`, `docs/operations/host-and-simulation.md` | the before-every-deploy harness rule (D17) |
| `static/js/confirm.js` | the one confirm mechanism (`data-confirm`) |
| `templates/base.html` | loads `confirm.js` |
| `templates/admin/user_detail.html`, `templates/admin/cohorts.html`, `templates/admin/cohort_detail.html` | `data-confirm` instead of inline handlers |
| `static/js/markdown.js` | page profile escapes raw HTML; `PAGE_PURIFY` |
| `templates/cabo_graph.html` | modal uses the page sanitize config until Phase 1 deletes the page |
| `templates/admin/simulation.html` | unit-wise refresh, notice, URL cleanup, run selector action |
| `templates/admin/_assessment_detail_body.html` | `toggle` re-measure |
| `tests/unit/test_confirm_and_markdown_js.py` | source-level pins for the two JS files |
| `tests/integration/test_destructive_confirms.py` | rendered `data-confirm` and no inline confirm |
| `tests/integration/test_admin_simulation_page.py` | refresh-script pins (extend) |
| `tests/integration/test_assessment_detail_timeline_toggle.py` | toggle-listener pin |
| `docs/audits/open-findings.md` | rows set to `fixed` |

---

### Task 0-1: Browser harness infrastructure

**Files:**
- Create: `tests/e2e/ui_audit/__init__.py`, `env.py`, `postgres.py`, `seed.py`, `serve.py`, `harness.py`, `crawl.py`, `run.py`, `README.md`
- Reference (do not modify): `docs/audits/2026-10-01-web-ui/raw/harness/*` (the audit's scratch scripts this task makes reusable)

**Interfaces:**
- Consumes: `tests.factories` (`make_user`, `make_profile`, `make_agent`, `make_simulation_run`, `make_llm_call_log`, `make_agent_message`), `tests.assessment_chat_support.seed_interview`, `tests.fakes.ChatScript`, `tests.fakes.FakeAsyncAnthropic`, `tests.assessment_chat_support.citation`, `PITCH_TEXT`, `RECORD_URL`, `tests.e2e.session.forge_session_cookie`, `src.main.SESSION_COOKIE`.
- Produces: `Harness` (`base_url: str`, `ids: dict`, `async page(role: str | None, width: int = 1280, bypass_csp: bool = False) -> tuple[BrowserContext, Page, dict]`), journey contract `async def journey_<name>(h: Harness) -> dict` with `{"ok": bool, ...}`, module-level `JOURNEYS: list` in `journeys_phase<n>.py`; CLI `python -m tests.e2e.ui_audit.run {serve,crawl,journeys,all} [--phase N] [--browser chromium|firefox] [--executable PATH] [--out PATH]`.
- Contract for later phases: the harness takes the session cookie name from `src.main.SESSION_COOKIE` (`run.py`, `_run`). The harness runs with `ALLOW_HTTP_SESSIONS=true`, so Phase 1's `__Host-` rename does not apply to it; if Phase 1 replaces the constant with a function of the settings, the same task updates `run.py` to call it.

- [ ] **Step 1: Cut the branch**

```bash
cd /home/ubuntu/blackbird-copi-science   # on the host
git switch blackbird && git switch -c webui/phase-0
```

- [ ] **Step 2: Create `tests/e2e/ui_audit/__init__.py`**

```python
"""Browser harness for the 2026-10-01 web UI audit and its remediation phases.

Not collected by pytest (no ``test_*`` modules). Run with
``python -m tests.e2e.ui_audit.run`` — see README.md in this directory.
"""
```

- [ ] **Step 3: Create `tests/e2e/ui_audit/env.py`**

```python
"""Process isolation for the harness.

``src.config`` reads ``.env`` from the CURRENT WORKING DIRECTORY, and the host's
checkout holds production secrets there. Every harness process therefore runs in a
temporary directory that contains symlinks to the code-side directories the app
resolves relative to its cwd (templates, static, prompts, profiles, alembic) and no
``.env`` at all; the settings it needs come from explicit environment variables,
which pydantic-settings prefers over a dotenv file anyway.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Relative paths the app READS from its cwd (Jinja2Templates("templates"),
#: StaticFiles("static"), the prompt files, alembic.ini). Never "profiles": the web app
#: WRITES and DELETES there (src/services/profile_export.py PROFILES_DIR,
#: src/services/user_deletion.py _PUBLIC_DIR/_MEMORY_DIR), and the repo's profiles/ is the
#: live agent's mounted directory. The harness gets its own empty profiles/ instead.
LINKED = ("templates", "static", "prompts", "alembic", "alembic.ini")

#: The throwaway database's name; seed.py refuses any other.
DB_NAME = "copi_uiaudit"


def isolated_workdir() -> Path:
    """A fresh temp directory with symlinks to the code-side paths and no .env."""
    work = Path(tempfile.mkdtemp(prefix="uiaudit-"))
    for name in LINKED:
        target = REPO_ROOT / name
        if target.exists():
            (work / name).symlink_to(target)
    # A private, empty profiles tree: profile exports and account deletions in a journey
    # or a manual `serve` session land here, never in the live agent's files.
    (work / "profiles" / "public").mkdir(parents=True)
    (work / "profiles" / "memory").mkdir(parents=True)
    assert not (work / ".env").exists()
    return work


def harness_env(*, database_url: str, base_url: str, secret_key: str) -> dict[str, str]:
    """Environment for every harness subprocess. Starts from a minimal copy of the
    caller's environment (PATH, HOME, locale, Docker/Playwright paths), never the
    whole thing, so no production credential exported in a shell leaks in."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "DOCKER_HOST",
            "PLAYWRIGHT_BROWSERS_PATH", "XDG_RUNTIME_DIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(
        PYTHONPATH=str(REPO_ROOT),
        DATABASE_URL=database_url,
        BASE_URL=base_url,
        ENVIRONMENT="development",
        ALLOW_HTTP_SESSIONS="true",
        SECRET_KEY=secret_key,
        ANTHROPIC_API_KEY="",
        # Every send is suppressed: the allowlist names one undeliverable address.
        OUTBOUND_EMAIL_ALLOWLIST="nobody@invalid.example",
        # boto3 must not find the host's instance-role credentials.
        AWS_EC2_METADATA_DISABLED="true",
        AWS_ACCESS_KEY_ID="uiaudit-invalid",
        AWS_SECRET_ACCESS_KEY="uiaudit-invalid",
        AWS_DEFAULT_REGION="us-east-1",
    )
    return env


def new_secret() -> str:
    return "uiaudit-" + secrets.token_hex(16)
```

- [ ] **Step 4: Create `tests/e2e/ui_audit/postgres.py`**

```python
"""A throwaway postgres:15 container, named uiaudit-pg-<pid> on a free 127.0.0.1 port.

Only containers this module created (by that exact name) are ever removed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time

from tests.e2e.ui_audit.env import DB_NAME


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ThrowawayPostgres:
    def __init__(self) -> None:
        self.name = f"uiaudit-pg-{os.getpid()}"
        self.port = free_port()

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://copi:copi@127.0.0.1:{self.port}/{DB_NAME}"

    def start(self) -> None:
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.name, "--label", "uiaudit=1",
             "-e", "POSTGRES_USER=copi", "-e", "POSTGRES_PASSWORD=copi",
             "-e", f"POSTGRES_DB={DB_NAME}", "-p", f"127.0.0.1:{self.port}:5432",
             "postgres:15"],
            check=True, capture_output=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", self.name, "pg_isready", "-U", "copi", "-d", DB_NAME],
                capture_output=True,
            )
            if ready.returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError(f"{self.name} did not become ready")

    def stop(self) -> None:
        subprocess.run(["docker", "stop", self.name], capture_output=True)
```

- [ ] **Step 5: Create `tests/e2e/ui_audit/seed.py`**

```python
"""Adversarial seed for the harness database. Run as
``python -m tests.e2e.ui_audit.seed`` inside the harness environment; prints the
seed ids as JSON on stdout.

The content is hostile on purpose (names that break JS strings, raw HTML, a form
that would promote a user, long unbroken URLs) and benign in effect: the database is
a throwaway container and no outbound path is enabled.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from tests.e2e.ui_audit.env import DB_NAME

XSS = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
EVIL_NAME = "O'Brien \"Q\" <b>bold</b> \\"
LONG = "https://example.org/" + "a" * 300
MD_EVIL = "[click](javascript:window.__xss=1) and <script>window.__xss=1</script> " + XSS
SCRIPT_NAME = "x'+(window.__xss=1)+'"
APOSTROPHE_NAME = "Mary O'Brien"


async def main() -> None:
    if not os.environ.get("DATABASE_URL", "").endswith("/" + DB_NAME):
        sys.exit(f"refusing to seed: DATABASE_URL must name {DB_NAME}")

    from src.database import get_engine, get_session_factory
    from src.models import (
        USER_ROLE_ADMIN,
        USER_ROLE_MANAGER,
        USER_ROLE_PI,
        USER_ROLE_REVIEWER,
        AgentDelegate,
        Job,
    )
    from tests import factories
    from tests.assessment_chat_support import seed_interview

    async with get_session_factory()() as s:
        mk = factories.make_user
        admin = await mk(s, user_role=USER_ROLE_ADMIN, name="Audit Admin",
                         orcid="0000-0002-1111-0001", email="admin@uiaudit.test")
        manager = await mk(s, user_role=USER_ROLE_MANAGER, name="Audit Manager",
                           orcid="0000-0002-1111-0002", email="mgr@uiaudit.test")
        reviewer = await mk(s, user_role=USER_ROLE_REVIEWER, name="Audit Reviewer",
                            orcid="0000-0002-1111-0003", email="rev@uiaudit.test")
        pi = await mk(s, user_role=USER_ROLE_PI, name=EVIL_NAME, orcid="0000-0002-1111-0004",
                      email="pi@uiaudit.test", institution=XSS)
        pi2 = await mk(s, user_role=USER_ROLE_PI, name="Bert Vogelstein",
                       orcid="0000-0002-1111-0005", email="bv@uiaudit.test")
        delegate = await mk(s, user_role=USER_ROLE_PI, name="Del Egate",
                            orcid="0000-0002-1111-0006", email="del@uiaudit.test")
        pending = await mk(s, user_role=USER_ROLE_PI, name="Pending " + XSS,
                           orcid="0000-0002-1111-0007", email="pend@uiaudit.test",
                           access_status="pending", onboarding_complete=False)
        script_user = await mk(s, name=SCRIPT_NAME, orcid="0000-0002-1111-0091",
                               email="x1@uiaudit.test")
        apostrophe_user = await mk(s, name=APOSTROPHE_NAME, orcid="0000-0002-1111-0092",
                                   email="x2@uiaudit.test")
        await factories.make_profile(s, user=pi, research_summary=MD_EVIL + "\n\n" + LONG,
                                     techniques=[XSS, "CRISPR"], keywords=[LONG, "k2"])
        await factories.make_profile(s, user=pi2)
        evil_agent = await factories.make_agent(s, user=pi, agent_id="obrien",
                                                bot_name="OBrienBot", pi_name=EVIL_NAME)
        await factories.make_agent(s, user=pi2, agent_id="vogelstein",
                                   bot_name="VogelsteinBot", pi_name="Bert Vogelstein")
        await factories.make_agent(s, agent_id="blackbird", bot_name="BlackbirdBot",
                                   pi_name="Blackbird", role="scout_hub")
        s.add(AgentDelegate(agent_registry_id=evil_agent.id, user_id=delegate.id))

        now = datetime.now(UTC)
        run = await factories.make_simulation_run(
            s, status="completed", started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=1))
        a1 = await seed_interview(s, run=run, subject="vogelstein", channel="interview-one")
        a2 = await seed_interview(
            s, run=run, subject="obrien", channel="interview-two",
            headline="EVIL " + XSS + " " + LONG, company_or_project=EVIL_NAME,
            rationale=MD_EVIL, red_flags=[XSS, LONG], strengths=[MD_EVIL],
            elevator_pitch=MD_EVIL + "\n" + LONG,
            key_points={"significance": [MD_EVIL, LONG], "innovation": [XSS]},
            recommended_next_experiment=MD_EVIL,
            recommendation="pass", band="pass", weighted_score=1.1,
        )
        # A stopped, never-finalized run: the Finalize journeys (Phase 1) need one.
        stopped = await factories.make_simulation_run(
            s, status="stopped", started_at=now - timedelta(hours=6),
            ended_at=now - timedelta(hours=5))
        a3 = await seed_interview(s, run=run, subject="vogelstein", channel="interview-three",
                                  with_messages=False, recommendation="advance",
                                  band="advance", weighted_score=4.6, headline="Strong one")
        # A long hub reply (timeline clamp) and a raw-HTML form that would promote pi2.
        long_reply = ("Paragraph of a very long hub reply. " * 40 + "\n\n") * 6
        injected = (
            "Thanks for the question.\n\n"
            f'<form method="post" action="/admin/users/{pi2.id}/role">'
            '<input type="hidden" name="user_role" value="admin">'
            '<button id="injected-btn" style="position:fixed;inset:0;opacity:0.01;'
            'z-index:9999">x</button></form>\n\n'
            "Inline <b>bold</b> and a lone line<br>break."
        )
        for agent_id, content in (("blackbird", long_reply), ("obrien", injected)):
            await factories.make_agent_message(
                s, run=run, agent_id=agent_id, channel_name="interview-two",
                message_ts=f"{time.time():.6f}", thread_ts=a2.root_ts,
                phase="thread_reply", content=content, posted_at=time.time())
        for _ in range(6):
            await factories.make_llm_call_log(s, run=run)
        for status, owner in (("pending", pi), ("processing", pi2), ("completed", pi),
                              ("failed", pi)):
            s.add(Job(type="generate_profile", status=status, user_id=owner.id,
                      payload={"note": XSS},
                      last_error=XSS if status == "failed" else None))
        await s.commit()
        ids = {
            "admin": str(admin.id), "manager": str(manager.id), "reviewer": str(reviewer.id),
            "pi": str(pi.id), "pi2": str(pi2.id), "delegate": str(delegate.id),
            "pending": str(pending.id), "script_user": str(script_user.id),
            "apostrophe_user": str(apostrophe_user.id), "run": str(run.id),
            "stopped_run": str(stopped.id),
            "agent_obrien": str(evil_agent.id),
            "assessments": [str(a1.assessment_id), str(a2.assessment_id),
                            str(a3.assessment_id)],
            "a2_root_ts": a2.root_ts,
            "bad_uuid": str(uuid.UUID(int=0)),
        }
    await get_engine().dispose()
    print(json.dumps(ids))


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 6: Create `tests/e2e/ui_audit/serve.py`**

```python
"""Serve the app for the harness with the fake model. Run as
``python -m tests.e2e.ui_audit.serve --port N`` inside the harness environment."""

from __future__ import annotations

import argparse

from tests.e2e.ui_audit.env import DB_NAME


def main() -> None:
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL", "").endswith("/" + DB_NAME):
        raise SystemExit(f"refusing to serve: DATABASE_URL must name {DB_NAME}")

    import uvicorn

    from src.main import create_app
    from src.services import assessment_chat
    from tests.assessment_chat_support import PITCH_TEXT, RECORD_URL, citation
    from tests.fakes import ChatScript, FakeAsyncAnthropic

    answer = (
        "**Answer.** <img src=x onerror=\"window.__xss=1\"> [js](javascript:window.__xss=1) "
        "![pixel](https://attacker.example/p.png) " + "x" * 200
        + f" The record cites {RECORD_URL}."
    )
    fake = FakeAsyncAnthropic(
        [ChatScript(delay=1.0, segments=[(answer, [citation(1, 0, PITCH_TEXT)])])
         for _ in range(200)]
    )
    assessment_chat.get_async_anthropic_client = lambda: fake
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Create `tests/e2e/ui_audit/harness.py`**

```python
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

    async def page(self, role: str | None, width: int = 1280, bypass_csp: bool = False):
        """A fresh context signed in as ``role`` (an ``ids`` key); None or "anon" = no session."""
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

        page.on("dialog", _dialog)
        return ctx, page, log
```

- [ ] **Step 8: Create `tests/e2e/ui_audit/crawl.py`**

```python
"""Role x route crawl. Invariants (any violation fails the run):

* no 5xx on any page;
* the XSS canary ``window.__xss`` stays 0 on every HTML page;
* no uncaught page error;
* an anonymous visitor never gets a 200 on a non-public route without being
  redirected to /login;
* the role gates in EXPECTED hold (a role outside a prefix's allowed set never gets a
  200 there);
* no console error from an ENFORCED Content-Security-Policy (report-only messages,
  which Chromium prefixes "[Report Only]", are reported, not failed).

Overflow and axe findings (1280 px, five roles) are reported, not failed; later phases
gate them in their own journeys.
"""

from __future__ import annotations

import asyncio

ROLES = ("anon", "admin", "manager", "reviewer", "pi", "pi2", "delegate", "pending")
PUBLIC = ("/", "/login", "/access-pending", "/invite/")
AXE_ROLES = ("anon", "admin", "manager", "reviewer", "pi")

#: Authorization gates the 2026-10-01 audit observed and the remediation keeps:
#: (path prefix, roles that may get a 200 there). First matching prefix wins.
EXPECTED: tuple[tuple[str, frozenset[str]], ...] = (
    ("/admin/", frozenset({"admin"})),
    ("/manager/pis", frozenset({"admin", "manager", "reviewer"})),
    ("/manager/assessments", frozenset({"admin", "manager", "reviewer"})),
    ("/manager/", frozenset({"admin", "manager"})),
    ("/agent/obrien/", frozenset({"pi", "delegate"})),
)


def routes(ids: dict) -> list[str]:
    a1, a2, a3 = ids["assessments"]
    run, bad = ids["run"], ids["bad_uuid"]
    return [
        "/", "/login", "/access-pending", "/settings", "/onboarding", "/profile",
        "/profile/edit", "/profile/delete-account", "/agent", "/agent/obrien/dashboard",
        "/agent/obrien/conversations", "/agent/obrien/public-profile",
        "/agent/obrien/public-profile/edit", "/agent/nosuch/dashboard",
        "/admin/users", f"/admin/users/{ids['pi']}", f"/admin/users/{bad}",
        f"/admin/users/{ids['script_user']}", "/admin/jobs", "/admin/activity",
        f"/admin/activity/{run}", f"/admin/activity/{run}/llm-calls", "/admin/discussions",
        "/admin/agents", f"/admin/agents/{ids['agent_obrien']}", "/admin/assessments",
        f"/admin/assessments/{a1}", f"/admin/assessments/{a2}", f"/admin/assessments/{a3}",
        f"/admin/assessments/{bad}", "/admin/cohorts", "/admin/cohorts/topology",
        "/admin/access-requests", "/admin/simulation", "/manager/pis",
        f"/manager/pis/{ids['pi']}", "/manager/assessments", f"/manager/assessments/{a2}",
        "/manager/discussions", "/manager/activity", f"/manager/activity/{run}",
        "/manager/slack-bots", "/manager/prompt-suggestions", f"/assessment-chat/{a2}",
        "/invite/badtoken",
    ]


async def _one(h, role: str, route: str, width: int) -> dict:
    ctx, page, log = await h.page(None if role == "anon" else role, width)
    rec: dict = {"role": role, "route": route, "width": width, **log}
    try:
        resp = await page.goto(h.base_url + route, wait_until="networkidle", timeout=30000)
        rec["status"] = resp.status if resp else None
        rec["final"] = page.url.replace(h.base_url, "")
        ctype = resp.headers.get("content-type", "") if resp else ""
        rec["ctype"] = ctype
        if "html" in ctype:
            await page.wait_for_timeout(1500)  # markdown renders after DOMContentLoaded
            rec["xss"] = await page.evaluate("window.__xss || 0")
            rec["overflow"] = await page.evaluate(
                "document.documentElement.scrollWidth - window.innerWidth")
    except Exception as exc:  # noqa: BLE001 - every failure is evidence
        rec["error"] = str(exc)[:300]
    finally:
        await ctx.close()
    if width == 1280 and role in AXE_ROLES and h.axe_source and "html" in rec.get("ctype", ""):
        rec["axe"] = await _axe(h, role, route)
    return rec


async def _axe(h, role: str, route: str) -> list:
    """axe-core in its OWN context with CSP bypassed: Playwright injects the bundle as an
    inline script, which an enforced script-src (Phase 2) blocks. The CSP-active load
    above is the one whose console is checked."""
    ctx, page, _log = await h.page(None if role == "anon" else role, 1280, bypass_csp=True)
    try:
        await page.goto(h.base_url + route, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        await page.add_script_tag(content=h.axe_source)
        return await page.evaluate("""async () => (await axe.run(document,
            {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa',
             'wcag22aa']}})).violations.map(v => [v.id, v.impact, v.nodes.length])""")
    except Exception as exc:  # noqa: BLE001 - reported, not failed
        return [["axe-error", str(exc)[:120], 0]]
    finally:
        await ctx.close()


def violations(records: list[dict]) -> list[str]:
    out = []
    for r in records:
        where = f"{r['role']} {r['width']} {r['route']}"
        if r.get("error"):
            out.append(f"{where}: {r['error']}")
        if (r.get("status") or 0) >= 500:
            out.append(f"{where}: HTTP {r['status']}")
        if r.get("xss"):
            out.append(f"{where}: XSS canary fired")
        if r.get("pageerror"):
            out.append(f"{where}: page error {r['pageerror'][0]}")
        public = r["route"] in PUBLIC[:3] or r["route"].startswith(PUBLIC[3])
        if (r["role"] == "anon" and not public and r.get("status") == 200
                and not (r.get("final") or "").startswith("/login")):
            out.append(f"{where}: anonymous 200 without a login redirect")
        allowed = next((roles for prefix, roles in EXPECTED if r["route"].startswith(prefix)),
                       None)
        landed = (r.get("final") or "").split("?")[0]
        if (allowed is not None and r["role"] not in allowed and r.get("status") == 200
                and landed == r["route"].split("?")[0]):
            out.append(f"{where}: {r['role']} got a 200 outside the gate")
        for line in r.get("console", []):
            if "Content Security Policy" in line and "[Report Only]" not in line:
                out.append(f"{where}: enforced CSP violation: {line[:160]}")
    return out


async def crawl(h, widths: tuple[int, ...] = (1280, 375)) -> dict:
    sem = asyncio.Semaphore(6)

    async def job(role, route, width):
        async with sem:
            return await _one(h, role, route, width)

    tasks = [job(role, route, 1280) for role in ROLES for route in routes(h.ids)]
    if 375 in widths:
        tasks += [job(role, route, 375) for role in ("admin", "pi", "reviewer", "anon")
                  for route in routes(h.ids)]
    records = await asyncio.gather(*tasks)
    axe_counts: dict[str, int] = {}
    for r in records:
        for rule, _impact, nodes in r.get("axe", []):
            axe_counts[rule] = axe_counts.get(rule, 0) + nodes
    return {"records": len(records), "violations": violations(records), "axe": axe_counts,
            "overflow_375": sorted({(r["role"], r["route"], r["overflow"])
                                    for r in records
                                    if r["width"] == 375 and (r.get("overflow") or 0) > 0})}
```

- [ ] **Step 9: Create `tests/e2e/ui_audit/run.py`**

```python
"""CLI for the browser harness.

    python -m tests.e2e.ui_audit.run serve
    python -m tests.e2e.ui_audit.run all --phase 0 [--browser firefox] [--executable PATH]

``all`` starts a throwaway Postgres, migrates, seeds, serves, runs the crawl and the
phase's journeys, writes a JSON report and tears everything down. Exit status 1 when
any crawl invariant or journey fails.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from tests.e2e.ui_audit.env import REPO_ROOT, harness_env, isolated_workdir, new_secret
from tests.e2e.ui_audit.harness import AXE_PATH, AXE_SHA256, AXE_URL, Harness
from tests.e2e.ui_audit.postgres import ThrowawayPostgres, free_port


def load_axe() -> str:
    """The pinned axe-core bundle, downloaded to AXE_PATH on first use."""
    import hashlib

    if not AXE_PATH.exists():
        with urllib.request.urlopen(AXE_URL, timeout=30) as r:  # noqa: S310 - pinned CDN
            AXE_PATH.write_bytes(r.read())
    data = AXE_PATH.read_bytes()
    if hashlib.sha256(data).hexdigest() != AXE_SHA256:
        AXE_PATH.unlink()
        raise RuntimeError(f"{AXE_PATH} does not match the pinned sha256; deleted it")
    return data.decode()


def _wait_http(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - localhost only
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


class Stack:
    """Postgres + migrated schema + seed + app, all isolated from .env."""

    def __init__(self) -> None:
        self.pg = ThrowawayPostgres()
        self.port = free_port()
        self.base_url = f"http://localhost:{self.port}"
        self.secret = new_secret()
        self.work = isolated_workdir()
        self.server: subprocess.Popen | None = None
        self.ids: dict = {}

    def env(self) -> dict[str, str]:
        return harness_env(database_url=self.pg.url, base_url=self.base_url,
                           secret_key=self.secret)

    def up(self) -> None:
        self.pg.start()
        py = sys.executable
        subprocess.run([py, "-m", "alembic", "upgrade", "head"], cwd=self.work,
                       env=self.env(), check=True, capture_output=True)
        seeded = subprocess.run([py, "-m", "tests.e2e.ui_audit.seed"], cwd=self.work,
                                env=self.env(), check=True, capture_output=True, text=True)
        self.ids = json.loads(seeded.stdout.strip().splitlines()[-1])
        self.server = subprocess.Popen(
            [py, "-m", "tests.e2e.ui_audit.serve", "--port", str(self.port)],
            cwd=self.work, env=self.env())
        _wait_http(self.base_url + "/api/health")

    def down(self) -> None:
        if self.server:
            self.server.terminate()
            self.server.wait(timeout=20)
        self.pg.stop()


async def _run(stack: Stack, args) -> dict:
    os.chdir(stack.work)  # this process too: src.config must never see the repo's .env
    os.environ.update(stack.env())
    from playwright.async_api import async_playwright

    from src.main import SESSION_COOKIE

    report: dict = {"phase": args.phase, "browser": args.browser, "base_url": stack.base_url}
    async with async_playwright() as p:
        launcher = getattr(p, args.browser)
        browser = await launcher.launch(executable_path=args.executable or None)
        axe = load_axe()
        h = Harness(base_url=stack.base_url, ids=stack.ids, browser=browser,
                    cookie_name=SESSION_COOKIE, secret_key=stack.secret, axe_source=axe,
                    env=stack.env())
        if args.command in ("crawl", "all"):
            from tests.e2e.ui_audit.crawl import crawl

            report["crawl"] = await crawl(h)
        if args.command in ("journeys", "all"):
            module = importlib.import_module(f"tests.e2e.ui_audit.journeys_phase{args.phase}")
            report["journeys"] = {}
            for journey in module.JOURNEYS:
                try:
                    report["journeys"][journey.__name__] = await journey(h)
                except Exception as exc:  # noqa: BLE001 - a crash is a failed journey
                    report["journeys"][journey.__name__] = {"ok": False, "error": str(exc)}
        await browser.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.ui_audit.run")
    parser.add_argument("command", choices=("serve", "crawl", "journeys", "all"))
    parser.add_argument("--phase", type=int, default=0)
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="chromium")
    parser.add_argument("--executable", default="")
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir())
                                             / f"uiaudit-{int(time.time())}.json"))
    args = parser.parse_args()
    os.chdir(REPO_ROOT)
    stack = Stack()
    try:
        stack.up()
        if args.command == "serve":
            print(json.dumps({"base_url": stack.base_url, "ids": stack.ids,
                              "secret_key": stack.secret}), flush=True)
            stack.server.wait()
            return
        report = asyncio.run(_run(stack, args))
    finally:
        stack.down()
    Path(args.out).write_text(json.dumps(report, indent=1))
    failed = report.get("crawl", {}).get("violations", []) + [
        name for name, r in report.get("journeys", {}).items() if not r.get("ok")]
    print(json.dumps({"report": args.out, "failed": failed}, indent=1))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 10: Create `tests/e2e/ui_audit/README.md`**

````markdown
# Web UI browser harness

Reproduces the 2026-10-01 web UI audit (`docs/audits/2026-10-01-web-ui/`) against a
throwaway local instance and gates each remediation phase (spec §9).

```bash
# once per machine: a browser for Playwright (about 150 MB)
.venv-test/bin/python -m playwright install chromium-headless-shell
# Phase gate: crawl + the phase's journeys; exit 1 on any failure
.venv-test/bin/python -m tests.e2e.ui_audit.run all --phase 0
# try Firefox too (B-01); report it as unverified if the download fails
.venv-test/bin/python -m playwright install firefox && \
  .venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0 --browser firefox
# keep an instance up for manual browsing (prints base URL, seed ids, secret)
.venv-test/bin/python -m tests.e2e.ui_audit.run serve
```

What it touches: one Docker container `uiaudit-pg-<pid>` (postgres:15, 127.0.0.1, removed
at exit), one local uvicorn on a free port, a temp working directory with its own empty
`profiles/` (profile exports and deletions never reach the repo's `profiles/`, which is the
live agent's directory). It never reads the
repo's `.env`, sends no email (allowlist set to an undeliverable address), cannot reach
AWS credentials, has no Slack token, and answers the assessment chat from
`tests/fakes.FakeAsyncAnthropic`. It needs network once for the axe-core bundle from
jsDelivr and, until Phase 1 vendors them, for the CDN scripts the pages load.
````

- [ ] **Step 11: Host prerequisites (once per machine)**

```bash
docker info >/dev/null && echo docker-ok
.venv-test/bin/python -c "import playwright; print('playwright', __import__('importlib.metadata').metadata.version('playwright'))"
.venv-test/bin/python -m playwright install chromium-headless-shell
# if the browser then fails to launch for missing system libraries:
#   sudo .venv-test/bin/python -m playwright install-deps chromium
curl -sfI https://cdn.jsdelivr.net/npm/axe-core@4.13.0/axe.min.js >/dev/null && echo jsdelivr-ok
curl -sfI https://cdn.tailwindcss.com >/dev/null && echo tailwind-cdn-ok
printf '%s\n' 'tests/e2e/ui_audit/axe.min.js' >> .gitignore
```

Expected: `docker-ok`, a playwright version, the browser installed, `jsdelivr-ok`, `tailwind-cdn-ok`. The `.gitignore` line keeps the downloaded axe bundle out of git (it is checksum-pinned in `harness.py`).

- [ ] **Step 12: Lint and smoke-run the stack with an empty journey list**

Create a temporary `tests/e2e/ui_audit/journeys_phase0.py` containing only `JOURNEYS: list = []` (Task 0-2 replaces it), then:

Run: `.venv-test/bin/python -m ruff check tests/e2e/ui_audit && .venv-test/bin/python -m tests.e2e.ui_audit.run all --phase 0`
Expected: ruff "All checks passed!"; the run prints `"failed": []` or crawl violations only. Record the crawl violations — on the current code there must be none (the audit found no 5xx, no canary, no page error). If the run fails before crawling, fix the harness, not the app.

- [ ] **Step 13: Commit**

```bash
git add tests/e2e/ui_audit .gitignore
git commit -m "test(webui-0): committed browser harness for the web UI audit (isolated from .env)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 0-2: Phase 0 journeys (red first)

**Files:**
- Modify: `tests/e2e/ui_audit/journeys_phase0.py` (replace the placeholder list)

**Interfaces:**
- Consumes: `Harness.page`, `h.ids` keys from Task 0-1 (`script_user`, `apostrophe_user`, `assessments`, `pi2`, `admin`).
- Produces: `JOURNEYS` with `journey_delete_confirm_script_name`, `journey_delete_confirm_apostrophe`, `journey_delete_confirm_hostile_names`, `journey_injected_transcript_form`, `journey_raw_html_shapes`, `journey_sim_start_form_survives_refresh`, `journey_sim_focus_kept`, `journey_sim_notice_on_session_expiry`, `journey_sim_new_sections_inserted`, `journey_sim_template_error_kept`, `journey_timeline_toggle_after_open`.

- [ ] **Step 1: Write the journeys**

```python
"""Phase 0 journeys (spec §9). Each returns {"ok": bool, ...evidence}."""

from __future__ import annotations

import asyncio


async def _delete_dialog(h, user_key: str) -> dict:
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/users/{h.ids[user_key]}", wait_until="networkidle")
        await page.click("form[action$='/delete'] button[type=submit]")
        await page.wait_for_timeout(1500)
        still_here = f"/admin/users/{h.ids[user_key]}" in page.url
        xss = await page.evaluate("window.__xss || 0") if still_here else None
        return {"ok": bool(log["dialogs"]) and still_here and not xss and not log["pageerror"],
                "dialogs": log["dialogs"], "url": page.url, "xss": xss,
                "pageerror": log["pageerror"]}
    finally:
        await ctx.close()


async def journey_delete_confirm_script_name(h) -> dict:
    return await _delete_dialog(h, "script_user")


async def journey_delete_confirm_apostrophe(h) -> dict:
    return await _delete_dialog(h, "apostrophe_user")


async def journey_delete_confirm_hostile_names(h) -> dict:
    """Review focus 1: backslash, double quote, </script>, curly apostrophe."""
    import json
    import subprocess
    import sys

    names = ['ends in a backslash \\', 'has "double" quotes', 'has </script> inside',
             "curly O’Brien"]
    script = (
        "import asyncio, json, sys\n"
        "from src.database import get_engine, get_session_factory\n"
        "from tests import factories\n"
        "async def m():\n"
        "    out = []\n"
        "    async with get_session_factory()() as s:\n"
        "        for i, n in enumerate(json.loads(sys.argv[1])):\n"
        "            u = await factories.make_user(s, name=n, orcid=f'0000-0002-2222-{i:04d}',"
        " email=f'h{i}@uiaudit.test')\n"
        "            out.append(str(u.id))\n"
        "        await s.commit()\n"
        "    await get_engine().dispose()\n"
        "    print(json.dumps(out))\n"
        "asyncio.run(m())\n"
    )
    made = subprocess.run([sys.executable, "-c", script, json.dumps(names)], env=h.env,
                          check=True, capture_output=True, text=True)
    user_ids = json.loads(made.stdout.strip().splitlines()[-1])
    results = {}
    for i, uid in enumerate(user_ids):
        h.ids[f"hostile_{i}"] = uid
        results[names[i]] = await _delete_dialog(h, f"hostile_{i}")
    return {"ok": all(r["ok"] for r in results.values()), "results": results}


async def journey_injected_transcript_form(h) -> dict:
    out = {}
    for role, prefix in (("admin", "/admin"), ("reviewer", "/manager")):
        ctx, page, log = await h.page(role)
        try:
            await page.goto(f"{h.base_url}{prefix}/assessments/{h.ids['assessments'][1]}",
                            wait_until="networkidle")
            await page.wait_for_timeout(3000)
            await page.evaluate("document.getElementById('timeline').open = true")
            await page.wait_for_timeout(500)
            out[role] = {
                "forms_injected": await page.locator("#injected-btn").count(),
                "escaped_visible": await page.evaluate(
                    "document.body.innerText.includes('<form method=\"post\"')"),
            }
        finally:
            await ctx.close()
    ok = all(v["forms_injected"] == 0 and v["escaped_visible"] for v in out.values())
    return {"ok": ok, **out}


async def journey_raw_html_shapes(h) -> dict:
    """Review focus 2: inline <b> and block <form> both render as text; a lone <br>
    stays a line break; markdown images become links."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/assessments/{h.ids['assessments'][1]}",
                        wait_until="networkidle")
        await page.wait_for_timeout(3000)
        await page.evaluate("document.getElementById('timeline').open = true")
        probe = await page.evaluate("""() => {
            const md = window.copiRenderMarkdown(
              'a <b>x</b> b\\n\\n<div id="z">block</div>\\n\\nline<br>break ![p](https://e.example/p.png)');
            const box = document.createElement('div'); box.innerHTML = md;
            return {html: md, bold: box.querySelectorAll('b').length,
                    div: box.querySelectorAll('div').length, br: box.querySelectorAll('br').length,
                    img: box.querySelectorAll('img').length, a: box.querySelectorAll('a').length};
        }""")
        ok = probe["bold"] == 0 and probe["div"] == 0 and probe["br"] == 1 \
            and probe["img"] == 0 and probe["a"] == 1
        return {"ok": ok, "probe": probe}
    finally:
        await ctx.close()


async def _sim_values(page) -> list[str]:
    return await page.evaluate(
        "[...document.querySelectorAll(\"input[name='max_runtime'], input[name='max_proposals']\")]"
        ".map(e => e.value)")


async def journey_sim_start_form_survives_refresh(h) -> dict:
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation", wait_until="networkidle")
        await page.fill("input[name='max_runtime']", "60")
        await page.fill("input[name='max_proposals']", "20")
        await page.mouse.click(5, 300)
        await page.wait_for_timeout(63000)  # two refresh ticks
        values = await _sim_values(page)
        return {"ok": values == ["60", "20"], "values": values, "log": log}
    finally:
        await ctx.close()


async def journey_sim_focus_kept(h) -> dict:
    """Review focus 4: focus on a non-input control survives a refresh."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation", wait_until="networkidle")
        await page.focus("nav.sim-jump-nav a[href='#sec-status']")
        await page.wait_for_timeout(33000)
        focused = await page.evaluate(
            "document.activeElement && document.activeElement.getAttribute('href')")
        return {"ok": focused == "#sec-status", "focused": focused}
    finally:
        await ctx.close()


async def journey_sim_notice_on_session_expiry(h) -> dict:
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation?msg=Start+requested.",
                        wait_until="networkidle")
        cleaned = "msg=" not in page.url
        await ctx.clear_cookies()
        await page.wait_for_timeout(33000)
        notice = page.locator("#sim-refresh-notice")
        visible = await notice.is_visible()
        text = await notice.inner_text() if visible else ""
        return {"ok": cleaned and visible and "reload" in text.lower(), "url": page.url,
                "notice": text}
    finally:
        await ctx.close()


async def journey_sim_new_sections_inserted(h) -> dict:
    """Review focus 3: reconciliation by unit id. The stats sections exist whenever any
    run exists (`_resolve_selected_run` falls back to the newest run,
    src/services/simulation_view.py:139-157), so the journey simulates the two
    transitions directly: it removes `#sec-cost` (a unit the next fetch brings back,
    which must be re-inserted after `#sec-run`) and adds a bogus `#sec-bogus` (a unit
    the fetch no longer has, which must be removed), with a dirty Start form that must
    survive."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation", wait_until="networkidle")
        await page.fill("input[name='max_runtime']", "45")
        await page.mouse.click(5, 300)
        await page.evaluate("""() => {
            document.getElementById('sec-cost').remove();
            const bogus = document.createElement('section');
            bogus.id = 'sec-bogus';
            document.getElementById('sim-body').appendChild(bogus);
        }""")
        await page.wait_for_timeout(33000)
        state = await page.evaluate("""() => ({
            cost: !!document.getElementById('sec-cost'),
            bogus: !!document.getElementById('sec-bogus'),
            costAfterRun: (() => {
                const ids = [...document.querySelectorAll('#sim-body > section[id]')].map(s => s.id);
                return ids.indexOf('sec-cost') === ids.indexOf('sec-run') + 1;
            })()
        })""")
        values = await _sim_values(page)
        ok = state["cost"] and not state["bogus"] and state["costAfterRun"] \
            and values[:1] == ["45"]
        return {"ok": ok, **state, "values": values}
    finally:
        await ctx.close()


async def journey_sim_template_error_kept(h) -> dict:
    """Q1-04: a rejected template and its error survive a refresh tick."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/simulation", wait_until="networkidle")
        area = page.locator("form[action='/admin/simulation/announce-template'] textarea[name=body]")
        await area.fill("Bad {nope} template")
        await page.locator("form[action='/admin/simulation/announce-template'] "
                           "button[type=submit]").first.click()
        await page.wait_for_load_state("networkidle")
        before = await area.input_value()
        await page.wait_for_timeout(33000)
        after = await area.input_value()
        error_kept = await page.locator("#sec-announce").inner_text()
        return {"ok": before == after == "Bad {nope} template" and "KeyError" in error_kept,
                "before": before, "after": after}
    finally:
        await ctx.close()


async def journey_timeline_toggle_after_open(h) -> dict:
    """B-01 and review focus 5: open the timeline with "Expand all"."""
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/assessments/{h.ids['assessments'][1]}",
                        wait_until="networkidle")
        await page.wait_for_timeout(1500)
        await page.click("[data-details-toggle='open']")
        await asyncio.sleep(1)
        state = await page.evaluate("""() => [...document.querySelectorAll('#timeline [data-clamp]')]
            .map(box => ({sh: box.scrollHeight, ch: box.clientHeight,
                          btn: !box.nextElementSibling.hidden
                               && box.nextElementSibling.offsetParent !== null}))""")
        long_ones = [s for s in state if s["sh"] > s["ch"] + 2]
        return {"ok": bool(long_ones) and all(s["btn"] for s in long_ones), "state": state}
    finally:
        await ctx.close()


JOURNEYS = [
    journey_delete_confirm_script_name,
    journey_delete_confirm_apostrophe,
    journey_delete_confirm_hostile_names,
    journey_injected_transcript_form,
    journey_raw_html_shapes,
    journey_sim_start_form_survives_refresh,
    journey_sim_focus_kept,
    journey_sim_notice_on_session_expiry,
    journey_sim_new_sections_inserted,
    journey_sim_template_error_kept,
    journey_timeline_toggle_after_open,
]
```

- [ ] **Step 2: Run them against the unfixed code and record the red set**

Run: `.venv-test/bin/python -m ruff check tests/e2e/ui_audit && .venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0`
Expected (current code): FAIL for `delete_confirm_script_name` (xss 1), `delete_confirm_apostrophe` (no dialog, navigated), `injected_transcript_form` (1 form), `raw_html_shapes` (bold/div present), `sim_start_form_survives_refresh` (values `0,0`), `sim_focus_kept`, `sim_notice_on_session_expiry` (no notice element), `sim_new_sections_inserted`. `timeline_toggle_after_open` passes in Chromium. `delete_confirm_hostile_names` may partially pass (curly apostrophe is safe today). Paste the summary into the commit message body.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/ui_audit/journeys_phase0.py
git commit -m "test(webui-0): Phase 0 browser journeys, red against the unfixed code

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 0-3: Shared confirm (A-01)

**Files:**
- Create: `static/js/confirm.js`
- Modify: `templates/base.html` (before `{% block scripts %}{% endblock %}`, line 189), `templates/admin/user_detail.html:177-178`, `templates/admin/cohorts.html:107-108`, `templates/admin/cohort_detail.html:27-28`
- Test: `tests/unit/test_confirm_and_markdown_js.py` (create), `tests/integration/test_destructive_confirms.py` (create)

**Interfaces:**
- Produces: `static/js/confirm.js` (document `submit` listener, capture phase, honouring `data-confirm`), the `data-confirm` attribute convention used by Phase 1.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_confirm_and_markdown_js.py`:

```python
"""Source pins for the two security-relevant scripts (spec 2026-10-01 §5.1, §5.2).
This repo has no JS runner; the browser harness (tests/e2e/ui_audit) exercises them."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIRM = ROOT / "static" / "js" / "confirm.js"


def test_confirm_js_reads_the_message_from_an_attribute_and_never_evaluates_it():
    js = CONFIRM.read_text()
    assert 'addEventListener("submit"' in js
    assert 'getAttribute("data-confirm")' in js
    assert "window.confirm(message)" in js
    assert "event.preventDefault()" in js
    assert re.search(r"\beval\(|new Function|setTimeout\(\s*['\"]", js) is None


def test_no_template_builds_a_confirm_in_an_inline_handler():
    offenders = [
        str(p.relative_to(ROOT)) for p in (ROOT / "templates").rglob("*.html")
        if re.search(r"on(submit|click)=\"[^\"]*confirm\(", p.read_text())
    ]
    assert offenders == []
```

`tests/integration/test_destructive_confirms.py`:

```python
"""A-01: the Delete User confirm carries the name as an inert attribute."""

import pytest

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

HOSTILE = [
    "x'+(window.__xss=1)+'",
    "Mary O'Brien",
    "ends in a backslash \\",
    'has "double" quotes',
    "has </script> inside",
]


@pytest.mark.parametrize("name", HOSTILE)
async def test_delete_user_confirm_is_an_escaped_data_attribute(client, db_session, name):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    target = await factories.make_user(db_session, name=name)
    await db_session.commit()
    html = (await client.get(f"/admin/users/{target.id}", headers=auth_headers(admin.id))).text
    form = html[html.index(f'action="/admin/users/{target.id}/delete"'):]
    form = form[: form.index(">")]
    assert "onsubmit" not in form
    assert 'data-confirm="Delete ' in form
    assert "'" not in form.split('data-confirm="', 1)[1].split('"', 1)[0]
    assert "</script>" not in form
    assert '<script src="/static/js/confirm.js"></script>' in html
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_confirm_and_markdown_js.py tests/integration/test_destructive_confirms.py -v`
Expected: FAIL — `confirm.js` missing; `test_no_template_builds_a_confirm_in_an_inline_handler` lists the three templates; every parametrized case fails on `onsubmit`.

- [ ] **Step 3: Create `static/js/confirm.js`**

```javascript
// The one confirmation mechanism for destructive forms (spec 2026-10-01 §5.1).
//
// A form opts in with data-confirm="<message>". The message is an HTML attribute
// value, autoescaped by Jinja, and is only ever passed to window.confirm as a
// string: a quote, backslash or tag in a user's name cannot change what runs.
// Inline onsubmit="return confirm('...{{ name }}...')" handlers were the A-01 bug
// (an entity-decoded quote closed the JS string) and must not come back.
(function () {
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var message = form.getAttribute("data-confirm");
    if (message === null) return;
    if (!window.confirm(message)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
})();
```

- [ ] **Step 4: Load it from `templates/base.html`**

Before the existing line `{% block scripts %}{% endblock %}`:

```html
<script src="/static/js/confirm.js"></script>
{% block scripts %}{% endblock %}
```

- [ ] **Step 5: Convert the three forms**

`templates/admin/user_detail.html:177-178` — replace

```html
        <form method="POST" action="/admin/users/{{ target_user.id }}/delete"
              onsubmit="return confirm('Delete {{ target_user.name }}? This cannot be undone.')">
```

with

```html
        <form method="POST" action="/admin/users/{{ target_user.id }}/delete"
              data-confirm="Delete {{ target_user.name }}? This cannot be undone.">
```

`templates/admin/cohorts.html:107-108` — replace

```html
                    <form method="POST" action="/admin/cohorts/{{ c.id }}/delete"
                          onsubmit="return confirm('Delete empty cohort “{{ c.name }}”?');">
```

with

```html
                    <form method="POST" action="/admin/cohorts/{{ c.id }}/delete"
                          data-confirm="Delete empty cohort “{{ c.name }}”?">
```

`templates/admin/cohort_detail.html:27-28` — replace

```html
        <form method="POST" action="/admin/cohorts/{{ cohort.id }}/delete"
              onsubmit="return confirm('Delete empty cohort “{{ cohort.name }}”?');">
```

with

```html
        <form method="POST" action="/admin/cohorts/{{ cohort.id }}/delete"
              data-confirm="Delete empty cohort “{{ cohort.name }}”?">
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_confirm_and_markdown_js.py tests/integration/test_destructive_confirms.py tests/integration/test_cohort_admin.py tests/integration/test_account_deletion_routes.py -v`
Expected: PASS (the existing cohort and deletion suites still pass: the POST routes are unchanged).

- [ ] **Step 7: Run the delete journeys**

Run: `.venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0`
Expected: `delete_confirm_script_name`, `delete_confirm_apostrophe`, `delete_confirm_hostile_names` now `ok: true` (dialog shown, nothing ran, user kept).

- [ ] **Step 8: Commit**

```bash
git add static/js/confirm.js templates/base.html templates/admin/user_detail.html \
  templates/admin/cohorts.html templates/admin/cohort_detail.html \
  tests/unit/test_confirm_and_markdown_js.py tests/integration/test_destructive_confirms.py
git commit -m "fix(webui-0): destructive confirms read data-confirm, never inline JS (A-01)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 0-4: Page markdown renderer (A-02)

**Files:**
- Modify: `static/js/markdown.js` (whole file, 85 lines), `templates/cabo_graph.html:632-640` (the `renderMarkdown` sanitize call)
- Test: `tests/unit/test_confirm_and_markdown_js.py` (extend); `tests/unit/test_markdown_renderer_config.py` (modify `test_chat_profile_keeps_its_raw_html_hardening`, Step 4b — the chat branch now assigns the shared `rawTagTokenizer`; plan audit Q1-01)

**Interfaces:**
- Consumes: marked 12.0.2 positional renderer API (`html(html, block)`, `image(href, title, text)` where `text` is already escaped by marked's `outputLink`).
- Produces: `PAGE_PURIFY` (module constant), `createSanitizingMarked("page")` with raw-HTML escaping, `window.copiRenderMarkdown(md)` unchanged name.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_confirm_and_markdown_js.py`)

```python
MARKDOWN = ROOT / "static" / "js" / "markdown.js"


def _page_profile(js: str) -> str:
    start = js.index('profile === "page"')
    # "} else" (not "} else if"): Phase 1 Task 1A-2 deletes the graph branch, after
    # which the page branch is followed by the final "} else {".
    return js[start: js.index("} else", start + 1)]


def test_page_profile_escapes_raw_html_except_a_lone_br():
    js = MARKDOWN.read_text()
    page = _page_profile(js)
    assert "tokenizer.tag = rawTagTokenizer" in page
    assert "LONE_BR.test" in page and "escapeHtml(" in page
    assert r"var LONE_BR = /^<br\s*\/?>$/i;" in js


def test_page_profile_renders_images_as_links():
    page = _page_profile(MARKDOWN.read_text())
    assert "image: function (href, title, text)" in page
    assert "<img" not in page


def test_render_markdown_uses_the_explicit_page_allowlist():
    js = MARKDOWN.read_text()
    assert "DOMPurify.sanitize(pageMarked.parse(md), PAGE_PURIFY)" in js
    assert "DOMPurify.sanitize(marked.parse(md))" not in js
    block = js[js.index("var PAGE_PURIFY"): js.index("};", js.index("var PAGE_PURIFY"))]
    for forbidden in ('"form"', '"input"', '"button"', '"img"', '"style"', '"iframe"',
                      '"select"', '"textarea"'):
        assert forbidden not in block
    assert 'ALLOWED_ATTR: ["href", "title", "start", "align"]' in block
    assert "ALLOW_DATA_ATTR: false" in block and "ALLOW_ARIA_ATTR: false" in block
    assert r"ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i" in block
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_confirm_and_markdown_js.py -v`
Expected: FAIL on the three new tests (`profile === "page"` branch has no `rawTagTokenizer`; `PAGE_PURIFY` absent).

- [ ] **Step 3: Replace `static/js/markdown.js`**

```javascript
// Shared sanitizing markdown renderer (SEC-2; spec 2026-10-01 §5.2).
//
// Renders every element carrying a `data-markdown` attribute as markdown. The source
// is LLM/agent-generated text — untrusted — so raw HTML in it is shown as TEXT, and the
// output passes an explicit DOMPurify allowlist before it reaches innerHTML. A lone
// <br> is the one raw tag kept (the only raw HTML in production data, 2026-10-01).
// Load this AFTER marked and DOMPurify. Exposes window.copiRenderMarkdown(md).
(function () {
  // Disable GFM strikethrough: the corpus uses single tildes for "approximately"
  // (e.g. "~30-37%"), which marked otherwise pairs into a <del> span.
  if (window.marked && typeof marked.use === "function") {
    marked.use({ tokenizer: { del() { return undefined; } } });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/["]/g, "&quot;")
      .replace(/[']/g, "&#39;");
  }

  var LONE_BR = /^<br\s*\/?>$/i;

  // marked's own `tag` tokenizer enters a raw-block state after <code>, <pre>, <kbd>
  // or <script> in which later text is emitted UNESCAPED (RSEC-1); raw HTML is
  // rendered as text by the profiles below, so that state is never wanted.
  function rawTagTokenizer(src) {
    const cap = this.rules.inline.tag.exec(src);
    if (cap) {
      return { type: "html", raw: cap[0], inLink: false, inRawBlock: false, block: false, text: cap[0] };
    }
    return undefined;
  }

  // One factory for every sanitizing renderer (LC-02): the detail pages ("page"),
  // the assessment chat ("chat") and the collaboration graph ("graph"). Each call
  // returns a PRIVATE marked instance. Every profile treats "~" as literal text.
  function createSanitizingMarked(profile) {
    if (!window.marked || !window.marked.Marked) return null;
    const instance = new window.marked.Marked();
    const tokenizer = { del() { return undefined; } };
    const ext = { tokenizer: tokenizer };
    if (profile === "chat") {
      tokenizer.tag = rawTagTokenizer;
      ext.renderer = {
        html: function (html) { return escapeHtml(html); }
      };
    } else if (profile === "page") {
      tokenizer.tag = rawTagTokenizer;
      ext.renderer = {
        html: function (html) {
          var s = String(html);
          return LONE_BR.test(s.trim()) ? "<br>" : escapeHtml(s);
        },
        // marked 12 passes `text` (the alt text) already escaped (outputLink).
        image: function (href, title, text) {
          if (!href) return text || "";
          return '<a href="' + escapeHtml(href) + '">' + (text || escapeHtml(href)) + "</a>";
        }
      };
    } else if (profile === "graph") {
      ext.gfm = true;
      ext.breaks = true;
    } else {
      throw new Error("unknown markdown profile: " + profile);
    }
    instance.use(ext);
    return instance;
  }
  window.createSanitizingMarked = createSanitizingMarked;

  // Exactly what markdown produces; no form controls, media, styles or ids. `title`
  // carries md_citations' cited URL (src/services/prose_citations.py); `start` keeps an
  // ordered list's numbering and `align` a table column's alignment (both inert, both
  // emitted by marked). DOMPurify keeps data-* and aria-* by default even with an
  // explicit ALLOWED_ATTR, so both are switched off: the site's document-level
  // behaviours key on data-* attributes.
  var PAGE_PURIFY = {
    ALLOWED_TAGS: ["p", "br", "strong", "em", "code", "pre", "blockquote", "ul", "ol", "li",
                   "a", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
                   "table", "thead", "tbody", "tr", "th", "td"],
    ALLOWED_ATTR: ["href", "title", "start", "align"],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i
  };
  window.COPI_PAGE_PURIFY = PAGE_PURIFY;

  var pageMarked = null;

  function renderMarkdown(md) {
    if (!md) return "";
    if (!pageMarked) pageMarked = createSanitizingMarked("page");
    if (!pageMarked || !window.DOMPurify) {
      // Fail closed: never inject unsanitized HTML if a dependency is missing.
      return escapeHtml(md);
    }
    return DOMPurify.sanitize(pageMarked.parse(md), PAGE_PURIFY);
  }

  function renderAll() {
    document.querySelectorAll("[data-markdown]").forEach(function (el) {
      var md = el.getAttribute("data-markdown");
      if (md) el.innerHTML = renderMarkdown(md);
    });
  }

  window.copiRenderMarkdown = renderMarkdown;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderAll);
  } else {
    renderAll();
  }
})();
```

- [ ] **Step 4: Graph modal uses the page allowlist until Phase 1 deletes the page**

In `templates/cabo_graph.html`, inside `function renderMarkdown(s)` (around line 632-640), replace

```javascript
    return DOMPurify.sanitize(graphMarked.parse(text));
```

with

```javascript
    // Spec 2026-10-01 §5.2: same allowlist as the page renderer (forms, media,
    // styles and ids are dropped). This page is deleted in Phase 1.
    return DOMPurify.sanitize(graphMarked.parse(text), window.COPI_PAGE_PURIFY);
```

- [ ] **Step 4b: Re-point the chat-profile pin**

In `tests/unit/test_markdown_renderer_config.py` replace `test_chat_profile_keeps_its_raw_html_hardening` with:

```python
def test_chat_profile_keeps_its_raw_html_hardening():
    factory = JS[JS.index("function createSanitizingMarked"):JS.index("window.createSanitizingMarked =")]
    start = factory.index('profile === "chat"')
    chat = factory[start:factory.index("} else", start)]
    # The raw-block-free tag tokenizer is shared with the page profile (spec §5.2).
    assert "tokenizer.tag = rawTagTokenizer" in chat
    assert "html: function (html)" in chat
    tokenizer = JS[JS.index("function rawTagTokenizer"):JS.index("function createSanitizingMarked")]
    assert "inRawBlock: false" in tokenizer
```

- [ ] **Step 5: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_confirm_and_markdown_js.py tests/unit/test_markdown_renderer_config.py -v`
Expected: PASS (the three-profile and strikethrough pins still hold; the chat pin follows the shared tokenizer).

- [ ] **Step 6: Run the renderer journeys**

Run: `.venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0`
Expected: `injected_transcript_form` and `raw_html_shapes` now `ok: true`; the chat answer in the crawl still renders its record link (unchanged chat profile).

- [ ] **Step 7: Commit**

```bash
git add static/js/markdown.js templates/cabo_graph.html tests/unit/test_confirm_and_markdown_js.py \
  tests/unit/test_markdown_renderer_config.py
git commit -m "fix(webui-0): page markdown escapes raw HTML and uses an explicit allowlist (A-02)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 0-5: Simulation panel refresh (C-01, C-08, C-09, C-27, FN-05)

**Files:**
- Modify: `templates/admin/simulation.html:97-102` (banners), `:322` (template form), `:408` (run selector), `:729-754` (pause control and script; line 727 is `</div><!-- #sim-body -->` and stays)
- Test: `tests/integration/test_admin_simulation_page.py:788-793` (extend the existing pin)

**Interfaces:**
- Produces: element ids `sim-refresh-notice` (`role="status"`, `hidden` until stop) and `sim-refresh-updated`; attribute `data-sim-flash` on the two banners.

- [ ] **Step 1: Write the failing test** (replace `test_live_tab_refresh_script_preserves_open_details_by_key` at lines 788-793 with)

```python
async def test_live_tab_refresh_script_preserves_open_details_by_key(client, db_session):
    admin = await _admin(db_session, "sim-admin-js@example.org")
    html = (await client.get("/admin/simulation?msg=hello", headers=auth_headers(admin.id))).text
    assert "details[open][data-sc-key]" in html and "el.dataset.scKey" in html
    # Never replaces a unit holding focus or an edited form (C-01, C-09).
    assert "contains(document.activeElement)" in html
    assert "formIsDirty" in html and "defaultValue" in html and "defaultChecked" in html
    # Units, not #sim-body wholesale.
    assert ":scope > nav.sim-jump-nav, :scope > section[id]" in html
    assert "cur.innerHTML = next.innerHTML" not in html
    # Fetches the canonical URL and refuses redirected/non-HTML/failed responses (C-08).
    assert "new URL('/admin/simulation', location.origin)" in html
    assert "fetch(location.href)" not in html
    assert "r.redirected" in html and "r.ok" in html
    assert 'id="sim-refresh-notice"' in html and 'role="status"' in html
    # The flash is shown once (C-27) and the banner is a removable non-unit.
    assert "history.replaceState" in html and 'data-sim-flash' in html
    # A rejected template is kept, and a kept unit says it is not refreshed (Q1-04, Q1-13).
    assert "[data-sim-keep]" in html and "data-sim-stale-note" in html


async def test_a_rejected_template_marks_its_form_to_be_kept(client, db_session):
    admin = await _admin(db_session, "sim-admin-keep@example.org")
    resp = await client.post(
        "/admin/simulation/announce-template",
        data={"body": "Bad {nope} template", "reset": "false"},
        headers=auth_headers(admin.id),
    )
    assert resp.status_code == 200
    form = resp.text[resp.text.index('action="/admin/simulation/announce-template"'):]
    assert "data-sim-keep" in form[: form.index(">")]


async def test_run_selector_submits_to_the_page_route(client, db_session):
    admin = await _admin(db_session, "sim-admin-sel@example.org")
    html = (await client.get("/admin/simulation", headers=auth_headers(admin.id))).text
    assert '<form method="get" action="/admin/simulation"' in html
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_simulation_page.py -k "refresh_script or run_selector" -v`
Expected: FAIL (`formIsDirty` absent; selector has no action).

- [ ] **Step 3: Mark the banners** (`templates/admin/simulation.html:97-102`)

```html
{% if msg %}
<div data-sim-flash class="mb-4 rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-base text-green-800">{{ msg }}</div>
{% endif %}
{% if error %}
<div data-sim-flash class="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-base text-red-800">{{ error }}</div>
{% endif %}
```

- [ ] **Step 3b: Keep a rejected template on screen** (`:322`; plan audit Q1-04)

A rejected template is re-rendered with the rejected text as the textarea's DEFAULT content, so
`formIsDirty` cannot see it. Mark that form:

```html
    <form action="/admin/simulation/announce-template" method="post" class="space-y-3"{% if template_error %} data-sim-keep{% endif %}>
```

- [ ] **Step 4: Give the run selector an action** (`:408`)

```html
    <form method="get" action="/admin/simulation" class="flex items-center gap-3">
```

- [ ] **Step 5: Replace the pause control and script** (`:729-754`, everything from `<label class="mb-4 flex items-center gap-2 text-sm text-gray-600">` to the closing `</script>`)

```html
<label class="mb-2 flex items-center gap-2 text-sm text-gray-600">
    <input type="checkbox" id="sim-refresh-pause" class="rounded border-gray-300">
    Pause auto-refresh <span class="sc-meta">(refreshes every 30 s; open tables stay open; your edits and focus are kept)</span>
    <span id="sim-refresh-updated" class="sc-meta"></span>
</label>
<p id="sim-refresh-notice" role="status" hidden
   class="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900"></p>
<script>
(function () {
  // Spec 2026-10-01 §5.3. Units are the jump nav and each top-level <section id>;
  // a unit that holds focus or an edited form is never replaced or removed (C-01,
  // C-09). The fetch targets the page route, never location.href, and a redirected,
  // failed or non-HTML answer stops the refresh with a visible notice (C-08).
  const REFRESH_MS = 30000;
  const MAX_NETWORK_FAILURES = 3;
  const body = document.getElementById('sim-body');
  const notice = document.getElementById('sim-refresh-notice');
  const updated = document.getElementById('sim-refresh-updated');
  if (!body) return;

  // C-27: a ?msg=/?error= flash is shown once; a reload must not repeat it.
  const here = new URL(location.href);
  if (here.searchParams.has('msg') || here.searchParams.has('error')) {
    here.searchParams.delete('msg');
    here.searchParams.delete('error');
    history.replaceState(history.state, '', here.pathname + here.search + here.hash);
  }

  function refreshUrl() {
    const target = new URL('/admin/simulation', location.origin);
    const run = new URL(location.href).searchParams.get('run');
    if (run !== null) target.searchParams.set('run', run);
    return target.toString();
  }

  function unitsOf(root) {
    return Array.from(root.querySelectorAll(':scope > nav.sim-jump-nav, :scope > section[id]'));
  }

  function unitKey(el) {
    return el.tagName === 'NAV' ? 'nav' : el.id;
  }

  function formIsDirty(form) {
    for (const el of Array.from(form.elements)) {
      if (el.type === 'checkbox' || el.type === 'radio') {
        if (el.checked !== el.defaultChecked) return true;
      } else if (el.tagName === 'SELECT') {
        for (const opt of Array.from(el.options)) {
          if (opt.selected !== opt.defaultSelected) return true;
        }
      } else if ('defaultValue' in el && el.value !== el.defaultValue) {
        return true;
      }
    }
    return false;
  }

  function isPinned(unit) {
    const active = document.activeElement;
    if (active && active !== document.body && unit.contains(document.activeElement)) return true;
    // A rejected template re-rendered with its error (data-sim-keep) is never replaced.
    if (unit.querySelector('[data-sim-keep]')) return true;
    return Array.from(unit.querySelectorAll('form')).some(formIsDirty);
  }

  // A pinned unit says so, rather than silently showing figures older than the
  // "last updated" time next to the pause box (plan audit Q1-13).
  function markStale(unit) {
    if (unit.querySelector('[data-sim-stale-note]')) return;
    const note = document.createElement('p');
    note.setAttribute('data-sim-stale-note', '');
    note.setAttribute('role', 'status');
    note.className = 'mb-2 text-sm text-amber-800';
    note.textContent = 'Not refreshed while this section has unsaved edits or keyboard focus.';
    unit.prepend(note);
  }

  function reconcile(next) {
    const open = new Set(Array.from(body.querySelectorAll('details[open][data-sc-key]'))
      .map(el => el.dataset.scKey));
    const current = new Map(unitsOf(body).map(el => [unitKey(el), el]));
    const fresh = unitsOf(next);
    const freshKeys = new Set(fresh.map(unitKey));
    for (const [key, el] of current) {
      if (!freshKeys.has(key) && !isPinned(el)) el.remove();
    }
    let anchor = null;
    for (const el of fresh) {
      const old = current.get(unitKey(el));
      const incoming = document.importNode(el, true);
      if (old && old.isConnected) {
        if (isPinned(old)) { markStale(old); anchor = old; continue; }
        old.replaceWith(incoming);
      } else if (anchor) {
        anchor.after(incoming);
      } else {
        const first = unitsOf(body)[0];
        if (first) first.before(incoming); else body.appendChild(incoming);
      }
      anchor = incoming;
    }
    body.querySelectorAll('details[data-sc-key]').forEach(el => {
      if (open.has(el.dataset.scKey)) el.open = true;
    });
    body.querySelectorAll('[data-sim-flash]').forEach(el => el.remove());
  }

  let inFlight = false;
  let failures = 0;
  let timer = null;

  function stop(message) {
    clearInterval(timer);
    if (notice) {
      notice.textContent = message;
      notice.hidden = false;
    }
  }

  function tick() {
    if (document.getElementById('sim-refresh-pause')?.checked) return;
    // Polite: skip a hidden tab and never stack a second fetch on a slow one.
    if (document.hidden || inFlight) return;
    inFlight = true;
    fetch(refreshUrl(), { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(r => {
        const type = r.headers.get('content-type') || '';
        if (!r.ok || r.redirected || !type.includes('text/html')) {
          stop('Auto-refresh stopped — reload the page ('
               + (r.redirected ? 'your session has ended' : 'HTTP ' + r.status) + ').');
          return null;
        }
        return r.text();
      })
      .then(h => {
        if (h === null) return;
        const next = new DOMParser().parseFromString(h, 'text/html').getElementById('sim-body');
        if (!next) { stop('Auto-refresh stopped — reload the page.'); return; }
        reconcile(next);
        failures = 0;
        if (updated) updated.textContent = '· last updated ' + new Date().toLocaleTimeString();
      })
      .catch(() => {
        failures += 1;
        if (failures >= MAX_NETWORK_FAILURES) {
          stop('Auto-refresh stopped after repeated network errors — reload the page.');
        }
      })
      .finally(() => { inFlight = false; });
  }

  timer = setInterval(tick, REFRESH_MS);
})();
</script>
```

- [ ] **Step 6: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_simulation_page.py -v`
Expected: PASS (all existing simulation page tests plus the two new ones).

- [ ] **Step 7: Run the simulation journeys**

Run: `.venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0`
Expected: `sim_start_form_survives_refresh`, `sim_focus_kept`, `sim_notice_on_session_expiry`, `sim_new_sections_inserted`, `sim_template_error_kept` now `ok: true`.

- [ ] **Step 8: Commit**

```bash
git add templates/admin/simulation.html tests/integration/test_admin_simulation_page.py
git commit -m "fix(webui-0): simulation refresh keeps edits and focus, stops visibly, shows a flash once (C-01, C-08, C-09, C-27, FN-05)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 0-6: Timeline toggle re-measure (B-01)

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html:1429-1449`
- Test: `tests/integration/test_assessment_detail_timeline_toggle.py` (create)

**Interfaces:**
- Consumes: the existing `check` closure per `[data-unclamp]` button.

- [ ] **Step 1: Write the failing test**

```python
"""B-01: clamp toggles are re-measured when any <details> opens."""

import pytest

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.assessment_chat_support import seed_interview
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_detail_page_re_measures_clamps_on_toggle(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    seeded = await seed_interview(db_session)
    await db_session.commit()
    html = (await client.get(f"/admin/assessments/{seeded.assessment_id}",
                             headers=auth_headers(admin.id))).text
    assert "document.addEventListener('toggle'" in html
    assert "clampChecks.push" in html
    assert "}, true);" in html[html.index("document.addEventListener('toggle'"):]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_timeline_toggle.py -v`
Expected: FAIL (`addEventListener('toggle'` absent).

- [ ] **Step 3: Implement** — replace lines 1429-1449 (from `// Timeline messages are clamped` through the closing `});` of the `forEach`) with

```javascript
    // Timeline messages are clamped to 16rem with a fade; the button lifts
    // the clamp for that one message. Hidden when the message is short.
    var clampChecks = [];
    root.querySelectorAll('[data-unclamp]').forEach(function (btn) {
        var box = btn.previousElementSibling;
        if (!box) { return; }
        var check = function () {
            var clamped = box.scrollHeight > box.clientHeight + 2;
            btn.hidden = !clamped && !box.classList.contains('is-unclamped');
            var fade = box.querySelector('.timeline-message-fade');
            if (fade) { fade.hidden = !clamped; }
        };
        btn.addEventListener('click', function () {
            var lifted = box.classList.toggle('is-unclamped');
            box.classList.toggle('max-h-64', !lifted);
            box.classList.toggle('overflow-hidden', !lifted);
            btn.textContent = lifted ? 'Show less' : 'Show full message';
            check();
        });
        clampChecks.push({ box: box, check: check });
        // Markdown renders asynchronously after this script; re-check late.
        setTimeout(check, 0); setTimeout(check, 500);
    });
    // B-01 (spec 2026-10-01 §5.4): a message inside a closed <details> can measure
    // 0 at load (browsers that give closed content no box), which hid its toggle for
    // good. Re-measure when any <details> opens — by click, "Expand all", a
    // data-open-details link or the chat's "Show in page". `toggle` does not bubble,
    // so this listens in the capture phase.
    document.addEventListener('toggle', function (event) {
        var d = event.target;
        if (!(d instanceof HTMLDetailsElement) || !d.open) { return; }
        clampChecks.forEach(function (c) { if (d.contains(c.box)) { c.check(); } });
    }, true);
```

The click handler is unchanged; the only behaviour change is `clampChecks` plus the `toggle` listener.

- [ ] **Step 4: Run the test and the existing detail-page suites**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_timeline_toggle.py tests/integration -k "assessment_detail" -v`
Expected: PASS.

- [ ] **Step 5: Run the toggle journey in Chromium and attempt Firefox**

Run: `.venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0` then `.venv-test/bin/python -m playwright install firefox && .venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0 --browser firefox`
Expected: `timeline_toggle_after_open` `ok: true` in Chromium; in Firefox either `ok: true` or, if the browser download fails, record "Firefox unverified: <error>" in the Task 0-7 report.

- [ ] **Step 6: Commit**

```bash
git add templates/admin/_assessment_detail_body.html tests/integration/test_assessment_detail_timeline_toggle.py
git commit -m "fix(webui-0): re-measure timeline clamps when a details element opens (B-01)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 0-7: Phase gate, records, merge, deploy

**Files:**
- Modify: `docs/audits/open-findings.md` (rows `2026-10-01/A-01`, `A-02`, `B-01`, `C-01`, `C-08`, `C-09`, `C-27`, `FN-05` → `fixed` with commit evidence)

- [ ] **Step 0: Commit the program's records (D15: committed locally, not pushed)**

On `webui/phase-0` before anything else in this task (if they are not already committed on `blackbird`):

```bash
git add docs/specs/2026-10-01-web-ui-remediation-design.md \
  docs/plans/2026-10-01-web-ui-remediation-phase-0.md \
  docs/plans/2026-10-01-web-ui-remediation-phase-1.md \
  docs/plans/2026-10-01-web-ui-remediation-phase-2.md
git commit -m "docs(webui): remediation plans and spec amendments A1-A16

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 1: Full gate**

Run: `./scripts/ci.sh`
Expected: exit 0. On failure, keep the exact failing command and output and repair before continuing.

- [ ] **Step 2: Harness gate**

Run: `.venv-test/bin/python -m tests.e2e.ui_audit.run all --phase 0`
Expected: exit 0: `"failed": []` (no crawl violation; every Phase 0 journey `ok`). Save the report path.

- [ ] **Step 3: Adversarial audit of the merged phase**

Dispatch `engineering:auditor` (model `opus`) with: the phase diff (`git diff blackbird...webui/phase-0`), spec §5, this plan, and the harness report; ask it to refute each fixed finding and to look for regressions in every page that loads `confirm.js` or `markdown.js`. Fix confirmed defects before Step 4.

- [ ] **Step 3b: Document the harness rule (D17)**

In `docs/operations/testing.md` add a section "Browser harness (before every web deploy)" stating: run `.venv-test/bin/python -m tests.e2e.ui_audit.run all --phase <n>` for the phase being deployed and `journeys --phase <m>` for every earlier phase; it must exit 0; it is not part of `ci.sh`; see `tests/e2e/ui_audit/README.md`. In `docs/operations/host-and-simulation.md`, in the deploy section, add one line: "Before building, the browser harness must pass (docs/operations/testing.md, 'Browser harness')." Commit both with `docs(webui-0): the browser harness gates every web deploy (D17)` and the attribution line.

- [ ] **Step 4: Mark the register rows fixed** (for B-01, write "Chromium verified; Firefox unverified" in the evidence unless the Firefox run of Task 0-6 Step 5 passed)

For each of `2026-10-01/A-01`, `A-02`, `B-01`, `C-01`, `C-08`, `C-09`, `C-27`, `FN-05` set `status` to `fixed` and append to `evidence` the backticked commit hash of its task (Task 0-3, 0-4, 0-6, 0-5 commits) — e.g. `` Fixed in `<hash>` (Phase 0). `` Then:

Run: `.venv-test/bin/python -m pytest tests/unit/test_open_findings_register.py -v`
Expected: PASS (every fixed row's commit resolves).

```bash
git add docs/audits/open-findings.md
git commit -m "docs(webui-0): register rows for Phase 0 fixes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Merge**

```bash
git switch blackbird && git merge --no-ff webui/phase-0 -m "Merge branch 'webui/phase-0' into blackbird

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Deploy (operator; read `docs/operations/host-and-simulation.md` first)**

```bash
git tag rollback-pre-webui-0 HEAD^1      # the pre-merge blackbird commit (spec §10)
git rev-parse rollback-pre-webui-0       # must equal the blackbird commit before the merge
# image rollback points, before building (images are copi-blackbird-<service>:latest,
# checked on the host 2026-10-01; same loop as the 0057 box in Phase 1 Task 1B-1)
for s in blackbird-app worker; do
  docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-webui-0
done
docker image ls | grep rollback-pre-webui-0
git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini
# must print nothing
DC="docker compose -f docker-compose.prod.yml"
$DC build blackbird-app worker
$DC up -d blackbird-app worker           # no migration; agent untouched
curl -s -o /dev/null -w "%{http_code}\n" https://blackbird.copi.science/login   # 200
curl -s https://blackbird.copi.science/static/js/confirm.js | head -2          # the new file
```

Expected: login 200; `confirm.js` served. Do not start a simulation. Tell the owner the agent image is unchanged by this phase.
