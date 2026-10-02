"""Phase 1 journeys owned by Part 1B (spec 2026-10-01 §9): sessions, impersonation and
invite acceptance. Each returns {"ok": bool, ...evidence}.

Registered from journeys_phase1.py (``JOURNEYS = [..., *JOURNEYS_1B]``). Each journey
creates its own users in the harness database, so it never moves a seeded user's
``session_epoch``: the crawl and the other journeys forge sessions with no epoch key,
which only an account that was never bumped accepts.

ORCID cannot be driven from the harness, so "login" here is the login page rendering
for a visitor plus the session a login writes after a sign-out (epoch 1) being accepted;
the callback itself is covered by tests/integration/test_session_epoch.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

UNVERIFIED_TEXT = (
    "An administrator must verify your email address before you can accept this invitation."
)

_PREAMBLE = (
    "import asyncio, json\n"
    "from datetime import UTC, datetime, timedelta\n"
    "from src.database import get_engine, get_session_factory\n"
    "from src.models import DelegateInvitation\n"
    "from tests import factories\n"
)


def _seed(h, body: str) -> dict:
    """Run ``body`` as the inside of ``async def m(s)`` on the harness database, commit,
    and return the dict it returns. The subprocess gets ``h.env`` (the harness stack's
    environment), never ``os.environ``."""
    script = (
        _PREAMBLE
        + "async def m(s):\n"
        + "".join(f"    {line}\n" for line in body.strip("\n").splitlines())
        + "async def main():\n"
        + "    async with get_session_factory()() as s:\n"
        + "        out = await m(s)\n"
        + "        await s.commit()\n"
        + "    await get_engine().dispose()\n"
        + "    print(json.dumps(out))\n"
        + "asyncio.run(main())\n"
    )
    made = subprocess.run([sys.executable, "-c", script], env=h.env, check=True,
                          capture_output=True, text=True)
    return json.loads(made.stdout.strip().splitlines()[-1])


def _cookie(h, user_id: str, **extra) -> str:
    os.environ["SECRET_KEY"] = h.secret_key
    from tests.e2e.session import forge_session_cookie

    return forge_session_cookie(user_id, **extra)


async def _page_as(h, user_id: str, **extra):
    ctx, page, log = await h.page(None)
    await ctx.add_cookies([{"name": h.cookie_name, "value": _cookie(h, user_id, **extra),
                            "url": h.base_url}])
    return ctx, page, log


async def journey_login_logout_second_device(h) -> dict:
    """Two devices on one account: signing out on one signs out the other; a session
    issued at the bumped epoch (what the next login writes) is accepted."""
    ids = _seed(h, '''
u = await factories.make_user(s, name="Two Device", orcid="0000-0002-4444-0001", email="two@uiaudit.test")
await factories.make_profile(s, user=u)
return {"user": str(u.id)}
''')
    uid = ids["user"]
    a_ctx, a, a_log = await _page_as(h, uid)
    b_ctx, b, b_log = await _page_as(h, uid)
    anon_ctx, anon, anon_log = await h.page(None)
    out: dict = {}
    try:
        await anon.goto(f"{h.base_url}/login", wait_until="networkidle")
        out["login_page"] = anon.url
        await a.goto(f"{h.base_url}/profile", wait_until="networkidle")
        await b.goto(f"{h.base_url}/profile", wait_until="networkidle")
        out["before"] = [a.url, b.url]
        await a.click("form[action='/logout'] button[type=submit]")
        await a.wait_for_url("**/login**")
        out["a_after_logout"] = a.url
        await b.goto(f"{h.base_url}/profile", wait_until="networkidle")
        out["b_after_other_logout"] = b.url
        c_ctx, c, c_log = await _page_as(h, uid, epoch=1)
        try:
            await c.goto(f"{h.base_url}/profile", wait_until="networkidle")
            out["relogin"] = c.url
        finally:
            await c_ctx.close()
        out["pageerror"] = a_log["pageerror"] + b_log["pageerror"] + anon_log["pageerror"] \
            + c_log["pageerror"]
        ok = (out["login_page"].endswith("/login")
              and all(url.endswith("/profile") for url in out["before"])
              and "/login" in out["a_after_logout"]
              and "/login" in out["b_after_other_logout"]
              and out["relogin"].endswith("/profile")
              and not out["pageerror"])
        return {"ok": ok, **out}
    finally:
        for ctx in (a_ctx, b_ctx, anon_ctx):
            await ctx.close()


async def journey_impersonate_and_stop(h) -> dict:
    """An admin impersonates by ORCID, sees the banner, stops; no cookie but the session."""
    _seed(h, '''
t = await factories.make_user(s, name="Imp Target", orcid="0000-0002-4444-0002", email="imp@uiaudit.test")
await factories.make_profile(s, user=t)
return {"target": str(t.id)}
''')
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/users", wait_until="networkidle")
        await page.fill("form[action='/admin/impersonate'] input[name=orcid]",
                        "0000-0002-4444-0002")
        await page.click("form[action='/admin/impersonate'] button[type=submit]")
        await page.wait_for_load_state("networkidle")
        during_url = page.url
        banner = await page.locator("form[action='/admin/impersonate/stop']").count()
        viewing = await page.get_by_text("Viewing as Imp Target").count()
        cookies_during = sorted(c["name"] for c in await ctx.cookies())
        if banner:
            await page.click("form[action='/admin/impersonate/stop'] button[type=submit]")
            await page.wait_for_load_state("networkidle")
        after_url = page.url
        banner_after = await page.locator("form[action='/admin/impersonate/stop']").count()
        cookies_after = sorted(c["name"] for c in await ctx.cookies())
        ok = (banner == 1 and viewing >= 1 and after_url.endswith("/admin/users")
              and banner_after == 0
              and cookies_during == [h.cookie_name] and cookies_after == [h.cookie_name]
              and not log["pageerror"])
        return {"ok": ok, "during_url": during_url, "banner": banner, "viewing": viewing,
                "after_url": after_url, "banner_after": banner_after,
                "cookies_during": cookies_during, "cookies_after": cookies_after,
                "pageerror": log["pageerror"]}
    finally:
        await ctx.close()


async def journey_invite_accept_verified_and_unverified(h) -> dict:
    """A verified invitee accepts and lands on the agent dashboard; an unverified one is
    told an administrator must verify the address and gets no accept form."""
    ids = _seed(h, '''
pi = await factories.make_user(s, name="Inviting PI", orcid="0000-0002-4444-0003", email="inviter@uiaudit.test")
agent = await factories.make_agent(s, user=pi, agent_id="inviter1b", bot_name="Inviter1bBot", pi_name="Inviting PI")
good = await factories.make_user(s, name="Verified Delegate", orcid="0000-0002-4444-0004", email="verified@uiaudit.test", email_verified_at=datetime.now(UTC))
bad = await factories.make_user(s, name="Unverified Delegate", orcid="0000-0002-4444-0005", email="unverified@uiaudit.test")
for email, token in (("verified@uiaudit.test", "uiaudit-1b-verified"), ("unverified@uiaudit.test", "uiaudit-1b-unverified")):
    s.add(DelegateInvitation(agent_registry_id=agent.id, invited_by_user_id=pi.id, email=email, token=token, status="pending", expires_at=datetime.now(UTC) + timedelta(days=1)))
await s.flush()
return {"good": str(good.id), "bad": str(bad.id), "agent": agent.agent_id}
''')
    good_ctx, good, good_log = await _page_as(h, ids["good"])
    bad_ctx, bad, bad_log = await _page_as(h, ids["bad"])
    try:
        accept = "form[action='/invite/uiaudit-1b-verified/accept']"
        await good.goto(f"{h.base_url}/invite/uiaudit-1b-verified", wait_until="networkidle")
        good_form = await good.locator(accept).count()
        if good_form:
            await good.click(f"{accept} button[type=submit]")
            await good.wait_for_load_state("networkidle")
        accepted_url = good.url

        await bad.goto(f"{h.base_url}/invite/uiaudit-1b-unverified", wait_until="networkidle")
        bad_text = await bad.content()
        bad_form = await bad.locator("form[action='/invite/uiaudit-1b-unverified/accept']").count()
        pageerror = good_log["pageerror"] + bad_log["pageerror"]
        ok = (good_form == 1
              and f"/agent/{ids['agent']}/dashboard" in accepted_url
              and UNVERIFIED_TEXT in bad_text and bad_form == 0
              and not pageerror)
        return {"ok": ok, "good_form": good_form, "accepted_url": accepted_url,
                "unverified_message": UNVERIFIED_TEXT in bad_text, "bad_form": bad_form,
                "pageerror": pageerror}
    finally:
        for ctx in (good_ctx, bad_ctx):
            await ctx.close()


JOURNEYS_1B = [
    journey_login_logout_second_device,
    journey_impersonate_and_stop,
    journey_invite_accept_verified_and_unverified,
]
