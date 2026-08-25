"""Task 7 — the first-run experience: onboarding, profile and settings.

Thirteen HTTP endpoints across ``src/routers/onboarding.py`` (3),
``src/routers/profile.py`` (6) and ``src/routers/settings.py`` (4) had no direct
coverage, and ``src/services/profile_export.py`` had no test referencing it at all.

(It was seventeen until ``POST /onboarding/complete`` and ``GET /onboarding/done``
were deleted as an unreachable duplicate of the terminal step — see
``test_the_terminal_step_*`` below, which inherited their controls. It dropped
again to thirteen when the private-instructions removal cycle deleted the
``GET``/``POST /onboarding/private-profile`` step outright and relocated its
completion side effects onto ``POST /onboarding/save-profile``, which is now
the terminal step.)

Real ASGI requests, real Postgres, real Jinja templates, real ``profile_export``.
Nothing external runs: the ORCID and Anthropic entry points are replaced with
raising stubs (a first-run route that reached for one would fail loudly rather
than quietly make a network call), SES is a recorder, and the two export
directories are redirected into ``tmp_path`` so the suite never writes into
``profiles/``.

Discipline (see ``.notes/full-system-test-plan.md``): every absence assertion
carries a positive control in the same test. "The victim's row did not change"
is worthless next to a route that changes nothing for anybody, so each negative
is paired with the same request producing the effect it is supposed to produce.
"""

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from itsdangerous import TimestampSigner, URLSafeTimedSerializer
from sqlalchemy import func, select

from src.config import get_settings
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_PI,
    EmailEngagementTracker,
    EmailNotificationPreference,
    Job,
    ProfileRevision,
    Publication,
    ResearcherProfile,
    User,
)
from src.routers import onboarding as onboarding_router
from src.routers import profile as profile_router
from src.routers import settings as settings_router
from src.services import profile_export
from src.services.email_notifications import _generate_unsubscribe_token
from tests import factories

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


def _auth_as(user_id, impersonate_id) -> dict:
    """Session for ``user_id`` plus the copi-impersonate cookie pointed at another user.

    src/dependencies.get_current_user honours that cookie *only* when the session
    user is an admin. It is the one handle any of these 17 endpoints gives a
    caller on somebody else's identity, so it is the vector the sweep attacks.
    """
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {
        "Cookie": (
            f"copi-session={signer.sign(data).decode()}; "
            f"copi-impersonate={impersonate_id}"
        )
    }


@pytest.fixture(autouse=True)
def export_dirs(tmp_path, monkeypatch):
    """Redirect the export directory so no test writes into the repo's profiles/."""
    pub = tmp_path / "public"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", pub)
    return SimpleNamespace(public=pub)


@pytest.fixture(autouse=True)
def welcome_emails(monkeypatch):
    """Recording double for the one SES call these routers make."""
    sent: list[dict] = []

    import src.services.email as email_mod

    def _record(to_email, name=None, *, user_id=None, force=False):
        sent.append({"to": to_email, "name": name, "user_id": user_id})
        return True

    monkeypatch.setattr(email_mod, "send_welcome_email", _record)
    return sent


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    """ORCID and Anthropic must never be reached from a first-run route.

    Profile generation is the worker's job; these routes only enqueue it. A stub
    that raises turns "we accidentally made a network call in a request handler"
    into a test failure instead of a slow, flaky, billable test.
    """

    def _boom(label):
        async def _f(*_a, **_k):
            raise AssertionError(f"{label} was called from a first-run HTTP route")

        return _f

    for fn in (
        "fetch_orcid_record",
        "fetch_orcid_profile",
        "fetch_orcid_grants",
        "fetch_orcid_works",
    ):
        monkeypatch.setattr(f"src.services.orcid.{fn}", _boom(f"orcid.{fn}"))
    for fn in ("synthesize_profile", "generate_agent_response"):
        monkeypatch.setattr(f"src.services.llm.{fn}", _boom(f"llm.{fn}"))


# --- fresh reads -----------------------------------------------------------
# Column selects rather than ORM loads: the routes commit on the very session
# the test holds, so an already-loaded ORM instance can be stale while a column
# select always shows what is actually in the row.


async def _flag(db, uid) -> bool:
    return (
        await db.execute(select(User.onboarding_complete).where(User.id == uid))
    ).scalar_one()


async def _user_row(db, uid):
    return (
        await db.execute(
            select(
                User.name,
                User.email,
                User.institution,
                User.department,
                User.onboarding_complete,
                User.email_notification_frequency,
                User.email_notifications_paused_by_system,
            ).where(User.id == uid)
        )
    ).mappings().first()


async def _prof(db, uid):
    return (
        await db.execute(
            select(
                ResearcherProfile.research_summary,
                ResearcherProfile.techniques,
                ResearcherProfile.experimental_models,
                ResearcherProfile.disease_areas,
                ResearcherProfile.key_targets,
                ResearcherProfile.keywords,
                ResearcherProfile.private_profile_md,
                ResearcherProfile.private_profile_seed,
                ResearcherProfile.profile_version,
            ).where(ResearcherProfile.user_id == uid)
        )
    ).mappings().first()


async def _job_count(db, uid) -> int:
    return (
        await db.execute(select(func.count()).select_from(Job).where(Job.user_id == uid))
    ).scalar_one()


async def _prefs(db, uid) -> dict:
    rows = (
        await db.execute(
            select(
                EmailNotificationPreference.category,
                EmailNotificationPreference.enabled,
                EmailNotificationPreference.frequency,
            ).where(EmailNotificationPreference.user_id == uid)
        )
    ).all()
    return {c: (e, f) for c, e, f in rows}


async def _snapshot(db, uid):
    """Everything the 11 session-authenticated endpoints between them can change.

    One tuple, so a single equality covers "this endpoint touched the victim in
    any way at all" without the sweep needing per-endpoint knowledge.
    """
    user = await _user_row(db, uid)
    if user is None:
        return None
    prof = await _prof(db, uid)
    prof_t = None
    if prof is not None:
        prof_t = tuple(
            tuple(v) if isinstance(v, list) else v for v in prof.values()
        )
    return (tuple(user.values()), prof_t, await _job_count(db, uid), tuple(sorted(
        (await _prefs(db, uid)).items()
    )))


# --- rendered-settings readers ---------------------------------------------


def _toggle(html: str, key: str) -> str:
    m = re.search(rf'name="{key}_on" id="{key}_on" value="(\d)"', html)
    assert m, f"no {key} toggle rendered on the settings page"
    return m.group(1)


def _frequency(html: str, key: str) -> str:
    parts = html.split(f'name="{key}_frequency"', 1)
    assert len(parts) == 2, f"no {key} frequency select rendered on the settings page"
    m = re.search(r'value="([a-z_]+)" selected', parts[1].split("</select>", 1)[0])
    assert m, f"no option selected for {key}_frequency"
    return m.group(1)


ALL_OFF = {
    "proposal_review_on": "0",
    "status_overview_on": "0",
    "new_proposal_on": "0",
    "news_updates_on": "0",
}


def _all_on(review="daily", overview="monthly") -> dict:
    return {
        "proposal_review_on": "1",
        "proposal_review_frequency": review,
        "status_overview_on": "1",
        "status_overview_frequency": overview,
        "new_proposal_on": "1",
        "news_updates_on": "1",
    }


# ---------------------------------------------------------------------------
# the endpoint inventory — the list the authorization sweeps iterate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ep:
    method: str
    path: str
    build_data: Callable | None = None
    auth: str = "session"  # "session" | "token" (unsubscribe links carry no session)
    onboarding_complete: bool = True  # state the actor needs for this route to render

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


def _onboarding_form(u):
    return {"email": u.email or "", "research_summary": f"SWEEP-{u.orcid}"}


def _profile_form(u):
    return {
        "name": f"renamed-{u.orcid}",
        "email": u.email or "",
        "research_summary": f"SWEEP-{u.orcid}",
    }


ENDPOINTS: list[Ep] = [
    # --- src/routers/onboarding.py (3) ---
    Ep("GET", "/onboarding", onboarding_complete=False),
    Ep("POST", "/onboarding/save-profile", _onboarding_form, onboarding_complete=False),
    Ep("POST", "/onboarding/retry", lambda u: {}),
    # --- src/routers/profile.py (6) ---
    Ep("GET", "/profile"),
    Ep("GET", "/profile/edit"),
    Ep("POST", "/profile/save", _profile_form),
    Ep("POST", "/profile/refresh", lambda u: {}),
    Ep("GET", "/profile/delete-account"),
    Ep("POST", "/profile/delete-account", lambda u: {"confirm": "delete"}),
    # --- src/routers/settings.py (4) ---
    Ep("GET", "/settings"),
    Ep("POST", "/settings/save", lambda u: _all_on()),
    Ep("GET", "/settings/unsubscribe/{token}", auth="token"),
    Ep("POST", "/settings/unsubscribe/{token}", auth="token"),
]

_ID = {e: e.label for e in ENDPOINTS}


async def _send(client, ep: Ep, actor, headers: dict, token: str | None = None):
    """Fire ``ep`` as ``actor`` would. Empty headers means genuinely logged out."""
    if "Cookie" not in headers:
        # httpx keeps a cookie jar; a Set-Cookie from an earlier authenticated
        # request in the same test would otherwise silently log this one in.
        client.cookies.clear()
    path = ep.path.replace("{token}", token or "no-token")
    if ep.method == "GET":
        return await client.get(path, headers=headers)
    data = ep.build_data(actor) if ep.build_data else None
    return await client.post(path, data=data, headers=headers)


def test_the_endpoint_inventory_is_the_whole_first_run_surface():
    """The sweeps below are only as complete as this list.

    Read the routes off the three routers rather than trusting a hand-count, so
    a 16th endpoint fails here loudly instead of quietly escaping the
    authorization sweeps.
    """
    live = set()
    for prefix, module in (
        ("/onboarding", onboarding_router),
        ("/profile", profile_router),
        ("/settings", settings_router),
    ):
        for route in module.router.routes:
            for method in route.methods:
                if method in ("GET", "POST"):
                    live.add((method, prefix + route.path))

    declared = {(e.method, e.path) for e in ENDPOINTS}
    assert declared == live, (
        "the endpoint inventory has drifted from the routers; "
        f"missing from the tests: {sorted(live - declared)}; "
        f"no longer in the code: {sorted(declared - live)}"
    )
    assert len(ENDPOINTS) == 13

    # The two exemptions below are asserted, not assumed: unsubscribe links are
    # clicked from an email client with no session.
    assert {e.label for e in ENDPOINTS if e.auth == "token"} == {
        "GET /settings/unsubscribe/{token}",
        "POST /settings/unsubscribe/{token}",
    }


# ---------------------------------------------------------------------------
# 1. the onboarding sequence
# ---------------------------------------------------------------------------


@pytest.fixture
async def newcomer(db_session):
    return await factories.make_user(
        db_session,
        name="Newcomer Nadia",
        email="nadia@example.org",
        onboarding_complete=False,
        access_status="allowed",
    )


async def test_the_onboarding_walk_completes_only_at_the_final_step(
    client, db_session, newcomer, welcome_emails
):
    """start -> ORCID-derived profile review -> complete.

    onboarding_complete is checked after *every* step, so a router that set it
    early (which would drop a user into /profile with a blank agent) fails here.

    Since the private-instructions removal cycle deleted the private-profile
    step, POST /onboarding/save-profile is now the terminal step: its
    completion side effects (onboarding_complete flip, welcome email,
    invite/redirect resume) relocated onto it.
    """
    h = _auth(newcomer.id)

    # Step 1 — the start page. The user arrived straight from the ORCID login
    # with nothing generated yet; the page self-heals by enqueueing the
    # generate_profile job the worker will pick up.
    r = await client.get("/onboarding", headers=h)
    assert r.status_code == 200
    assert "Building Your Profile" in r.text
    assert await _job_count(db_session, newcomer.id) == 1
    assert await _flag(db_session, newcomer.id) is False

    # Step 2 — the worker's leg (ORCID + Anthropic) is out of scope here, so
    # stand in for its result and re-request the page.
    job = (
        await db_session.execute(select(Job).where(Job.user_id == newcomer.id))
    ).scalar_one()
    job.status = "completed"
    await factories.make_profile(
        db_session,
        user=newcomer,
        research_summary="Generated summary about kinase signalling.",
        techniques=["cryo-EM"],
        keywords=["kinase"],
    )
    await db_session.flush()

    r = await client.get("/onboarding", headers=h)
    assert r.status_code == 200
    assert "Generated summary about kinase signalling." in r.text
    assert await _job_count(db_session, newcomer.id) == 1, "self-heal re-fired with a job present"
    assert await _flag(db_session, newcomer.id) is False

    # Step 3 — the PI edits and saves the public profile. This is now the
    # terminal step: it flips onboarding_complete and sends the welcome email.
    r = await client.post(
        "/onboarding/save-profile",
        headers=h,
        data={
            "email": "nadia@example.org",
            "research_summary": "Edited by the PI during onboarding.",
            "techniques": "cryo-EM, mass spec",
            "experimental_models": "mouse",
            "disease_areas": "cancer",
            "key_targets": "KRAS",
            "keywords": "kinase, structure",
        },
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/profile?onboarding_complete=1"
    prof = await _prof(db_session, newcomer.id)
    assert prof["research_summary"] == "Edited by the PI during onboarding."
    assert prof["techniques"] == ["cryo-EM", "mass spec"]
    assert prof["keywords"] == ["kinase", "structure"]
    assert prof["profile_version"] == 2
    assert await _flag(db_session, newcomer.id) is True
    assert [e["to"] for e in welcome_emails] == ["nadia@example.org"]

    # And onboarding is now closed to this user.
    r = await client.get("/onboarding", headers=h)
    assert r.status_code == 302 and r.headers["location"] == "/profile"


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("GET", "/onboarding", None),
        ("POST", "/onboarding/retry", {}),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_skipping_to_a_step_does_not_complete_onboarding(
    client, db_session, newcomer, method, path, data
):
    """Control for the walk above: none of the non-terminal steps may finish it.

    The positive control is in the same test — the terminal step is fired at the
    end and must flip the flag, so a User whose onboarding_complete simply could
    not change would fail rather than pass.
    """
    h = _auth(newcomer.id)
    await factories.make_profile(db_session, user=newcomer)

    if method == "GET":
        r = await client.get(path, headers=h)
    else:
        r = await client.post(path, headers=h, data=data)
    assert r.status_code in (200, 302)
    assert await _flag(db_session, newcomer.id) is False, f"{method} {path} completed onboarding"

    r = await client.post(
        "/onboarding/save-profile",
        headers=h,
        data={"email": "nadia@example.org", "research_summary": "done"},
    )
    assert r.status_code == 302
    assert await _flag(db_session, newcomer.id) is True, "the terminal step no longer completes it"


async def test_the_start_page_enqueues_a_job_only_when_there_is_nothing_to_show(
    client, db_session
):
    """The self-heal in onboarding_start, and the two conditions that gate it."""
    allowed = await factories.make_user(
        db_session, onboarding_complete=False, access_status="allowed"
    )
    pending = await factories.make_user(
        db_session, onboarding_complete=False, access_status="pending"
    )
    has_profile = await factories.make_user(
        db_session, onboarding_complete=False, access_status="allowed"
    )
    await factories.make_profile(db_session, user=has_profile)
    await db_session.flush()

    # positive: a stranded allowed user gets exactly one job, and only one.
    assert (await client.get("/onboarding", headers=_auth(allowed.id))).status_code == 200
    assert await _job_count(db_session, allowed.id) == 1
    assert (await client.get("/onboarding", headers=_auth(allowed.id))).status_code == 200
    assert await _job_count(db_session, allowed.id) == 1

    # controls: the two guards the self-heal is written with.
    #
    # READ THIS BEFORE TRUSTING THE PENDING CASE BELOW. Since E1.2,
    # get_current_user bounces any user whose access_status is not 'allowed' to
    # /access-pending, so onboarding_start NEVER RUNS for `pending`. What the
    # next three lines now pin is that bounce — a different property from the
    # one they were written for.
    #
    # The job-count assertion in particular is a REDUNDANT CONTROL: it holds
    # because the handler never executed, so it holds whatever the handler
    # says. src/routers/onboarding.py's own
    # `current_user.access_status == "allowed"` condition is consequently an
    # UNTESTED BACKSTOP — delete it and this suite stays green. That is a known
    # gap, not coverage: the state it defends (a non-'allowed' user inside
    # onboarding_start) is unreachable over HTTP, so there is no honest HTTP
    # test to write for it, and a unit test that called the handler directly
    # would be pinning a call the app cannot make. It is kept because it is the
    # only thing left if the E1.2 bounce is ever loosened.
    r_pending = await client.get("/onboarding", headers=_auth(pending.id))
    assert r_pending.status_code == 302
    assert r_pending.headers["location"] == "/access-pending"
    assert await _job_count(db_session, pending.id) == 0, "self-heal ignored access_status"
    assert (await client.get("/onboarding", headers=_auth(has_profile.id))).status_code == 200
    assert await _job_count(db_session, has_profile.id) == 0, "self-heal ignored the profile"


async def test_onboarding_save_profile_requires_a_valid_unused_email(client, db_session):
    """Email is mandatory at onboarding and a rejected submission persists nothing."""
    other = await factories.make_user(db_session, email="taken@example.org")
    u = await factories.make_user(db_session, email=None, onboarding_complete=False)
    await db_session.flush()
    h = _auth(u.id)

    for value, expected in (
        ("", "error=email_required"),
        ("   ", "error=email_required"),
        ("not-an-email", "error=invalid_email"),
        ("taken@example.org", "error=email_taken"),
    ):
        r = await client.post(
            "/onboarding/save-profile",
            headers=h,
            data={"email": value, "research_summary": "should not be stored"},
        )
        assert r.status_code == 302
        assert expected in r.headers["location"], f"{value!r} -> {r.headers['location']}"
        assert await _prof(db_session, u.id) is None, f"{value!r} persisted a profile anyway"
        assert (await _user_row(db_session, u.id))["email"] is None

    # The email_taken branch is a cross-user write attempt: the other account
    # must be untouched.
    assert (await _user_row(db_session, other.id))["email"] == "taken@example.org"

    # positive control: a valid, unused address is accepted and stored.
    r = await client.post(
        "/onboarding/save-profile",
        headers=h,
        data={"email": "Fresh@Example.ORG", "research_summary": "stored"},
    )
    assert r.headers["location"] == "/profile?onboarding_complete=1"
    assert (await _user_row(db_session, u.id))["email"] == "fresh@example.org"
    assert (await _prof(db_session, u.id))["research_summary"] == "stored"


async def test_the_terminal_step_flips_the_flag_and_welcomes_exactly_once(
    client, db_session, newcomer, welcome_emails
):
    """The replay control on ``_maybe_send_welcome``'s ``was_complete`` guard.

    Aimed at POST /onboarding/save-profile because that is now the only
    terminal step left: the private-profile step that used to own this
    (POST /onboarding/private-profile) was deleted with private instructions,
    and its completion side effects relocated here. Nothing stops a replay of
    this one — there is no ``if current_user.onboarding_complete``
    short-circuit — so the guard is load-bearing and a second welcome email is
    reachable without it.
    """
    h = _auth(newcomer.id)
    data = {"email": "nadia@example.org", "research_summary": "# Mine"}
    r = await client.post("/onboarding/save-profile", headers=h, data=data)
    assert r.status_code == 302
    assert r.headers["location"] == "/profile?onboarding_complete=1"
    assert await _flag(db_session, newcomer.id) is True
    assert [e["to"] for e in welcome_emails] == ["nadia@example.org"]

    # control on the was_complete guard: a replay must not send a second welcome.
    r = await client.post("/onboarding/save-profile", headers=h, data=data)
    assert r.status_code == 302
    assert len(welcome_emails) == 1, "the welcome email is sent again on every replay"


async def test_the_terminal_step_resumes_a_pending_invite_before_the_default_redirect(
    client, db_session, newcomer
):
    """The invite branch in save_profile. Control: no token -> /profile.

    Also inherited from the deleted POST /onboarding/complete, which carried the
    same branch verbatim, and then from the deleted POST
    /onboarding/private-profile after this removal cycle relocated it again.
    """
    h = _auth(newcomer.id)
    data = {"email": "nadia@example.org", "research_summary": "# Mine"}
    r = await client.post("/onboarding/save-profile", headers=h, data=data)
    assert r.headers["location"] == "/profile?onboarding_complete=1"

    signer = TimestampSigner(get_settings().secret_key)
    payload = {"user_id": str(newcomer.id), "pending_invite_token": "tok-123"}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    r = await client.post(
        "/onboarding/save-profile",
        headers={"Cookie": f"copi-session={cookie}"},
        data=data,
    )
    assert r.headers["location"] == "/invite/tok-123"


def _session_cookie(user_id, **extra) -> dict:
    signer = TimestampSigner(get_settings().secret_key)
    payload = {"user_id": str(user_id), **extra}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    return {"Cookie": f"copi-session={cookie}"}


async def test_finishing_onboarding_resumes_only_a_safe_post_login_destination(
    client, db_session
):
    """The terminal step honours post_login_redirect. It is attacker-influenced
    (it comes off the /login query string), so the open-redirect guard has to
    hold here too, not only in auth.py.

    This was parametrised over two endpoints until POST /onboarding/complete —
    which duplicated the same resume block — was deleted. It moved again, to
    POST /onboarding/save-profile, once the private-instructions removal
    cycle deleted POST /onboarding/private-profile (the second such endpoint)
    outright.
    """
    endpoint = "/onboarding/save-profile"
    for stashed, expected in (
        ("/settings", "/settings"),  # positive: a real GET page resumes
        ("https://evil.example.com/steal", "/profile?onboarding_complete=1"),
        ("//evil.example.com/steal", "/profile?onboarding_complete=1"),
        ("/logout", "/profile?onboarding_complete=1"),  # deny-listed
        ("/not-a-page", "/profile?onboarding_complete=1"),  # not a GET route
    ):
        u = await factories.make_user(db_session, onboarding_complete=False)
        await db_session.flush()
        # email matches the user's own existing address, so save-profile's
        # cross-user uniqueness check never fires for this loop.
        data = {"email": u.email, "research_summary": "finished"}
        r = await client.post(
            endpoint,
            headers=_session_cookie(u.id, post_login_redirect=stashed),
            data=data,
        )
        assert r.status_code == 302
        assert r.headers["location"] == expected, f"{endpoint} with next={stashed!r}"
        assert await _flag(db_session, u.id) is True


async def test_retry_enqueues_another_generate_profile_job(client, db_session, newcomer):
    await factories.make_profile(db_session, user=newcomer)
    await db_session.flush()
    assert await _job_count(db_session, newcomer.id) == 0  # profile present, no self-heal

    r = await client.post("/onboarding/retry", headers=_auth(newcomer.id))
    assert r.status_code == 302 and r.headers["location"] == "/onboarding"
    assert await _job_count(db_session, newcomer.id) == 1

    job = (
        await db_session.execute(select(Job).where(Job.user_id == newcomer.id))
    ).scalar_one()
    assert job.type == "generate_profile"
    assert job.status == "pending"
    assert job.payload["orcid"] == newcomer.orcid


# ---------------------------------------------------------------------------
# 2. src/routers/profile.py
# ---------------------------------------------------------------------------


async def test_profile_view_is_gated_on_onboarding(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=False)
    await factories.make_profile(db_session, user=u, research_summary="VIEW-SUMMARY")
    await db_session.flush()

    r = await client.get("/profile", headers=_auth(u.id))
    assert r.status_code == 302 and r.headers["location"] == "/onboarding"

    # control: the identical request renders once onboarding is complete.
    u.onboarding_complete = True
    await db_session.flush()
    r = await client.get("/profile", headers=_auth(u.id))
    assert r.status_code == 200
    assert "VIEW-SUMMARY" in r.text


async def test_profile_view_lists_publications_newest_first(client, db_session):
    u = await factories.make_user(db_session, name="Pub Owner")
    await factories.make_profile(db_session, user=u)
    for year, title in ((2011, "Older paper"), (2021, "Newer paper")):
        db_session.add(
            Publication(user_id=u.id, title=title, journal="Cell", year=year)
        )
    await db_session.flush()

    r = await client.get("/profile", headers=_auth(u.id))
    assert r.status_code == 200
    assert "Newer paper" in r.text and "Older paper" in r.text
    assert r.text.index("Newer paper") < r.text.index("Older paper")


async def test_profile_edit_page_shows_the_current_values(client, db_session):
    u = await factories.make_user(db_session, name="Edit Me", email="edit@example.org")
    await factories.make_profile(
        db_session, user=u, research_summary="EDITABLE-SUMMARY", techniques=["ct-a", "ct-b"]
    )
    await db_session.flush()

    r = await client.get("/profile/edit", headers=_auth(u.id))
    assert r.status_code == 200
    assert "EDITABLE-SUMMARY" in r.text
    assert "ct-a" in r.text and "ct-b" in r.text
    assert "edit@example.org" in r.text


async def test_profile_save_persists_user_and_profile_fields_and_bumps_the_version(
    client, db_session
):
    u = await factories.make_user(db_session, name="Before Name", email="before@example.org")
    await factories.make_profile(db_session, user=u, profile_version=3)
    await db_session.flush()

    r = await client.post(
        "/profile/save",
        headers=_auth(u.id),
        data={
            "name": "After Name",
            "email": "after@example.org",
            "institution": "New Institute",
            "department": "New Dept",
            "research_summary": "new summary",
            "techniques": "t1, t2",
            "experimental_models": "m1",
            "disease_areas": "d1, d2",
            "key_targets": "k1",
            "keywords": "kw1, kw2",
        },
    )
    assert r.status_code == 302 and r.headers["location"] == "/profile?saved=1"

    user = await _user_row(db_session, u.id)
    assert user["name"] == "After Name"
    assert user["email"] == "after@example.org"
    assert user["institution"] == "New Institute"
    assert user["department"] == "New Dept"
    prof = await _prof(db_session, u.id)
    assert prof["research_summary"] == "new summary"
    assert prof["techniques"] == ["t1", "t2"]
    assert prof["experimental_models"] == ["m1"]
    assert prof["disease_areas"] == ["d1", "d2"]
    assert prof["key_targets"] == ["k1"]
    assert prof["keywords"] == ["kw1", "kw2"]
    assert prof["profile_version"] == 4


async def test_profile_save_rejects_a_bad_or_taken_email_and_persists_nothing(
    client, db_session
):
    other = await factories.make_user(db_session, email="owned@example.org", name="Owner")
    u = await factories.make_user(db_session, name="Keep Me", email="keep@example.org")
    await factories.make_profile(db_session, user=u, research_summary="untouched")
    await db_session.flush()
    h = _auth(u.id)

    for value, expected in (
        ("bogus", "error=invalid_email"),
        ("owned@example.org", "error=email_taken"),
    ):
        r = await client.post(
            "/profile/save",
            headers=h,
            data={"name": "Hijacked", "email": value, "research_summary": "hijacked"},
        )
        assert r.status_code == 302
        assert expected in r.headers["location"]
        user = await _user_row(db_session, u.id)
        assert (user["name"], user["email"]) == ("Keep Me", "keep@example.org")
        assert (await _prof(db_session, u.id))["research_summary"] == "untouched"

    # the taken-email attempt must not have moved the other account's address
    assert (await _user_row(db_session, other.id))["email"] == "owned@example.org"

    # control: a legitimate save goes through, so "nothing persisted" above is
    # not just a route that never writes.
    r = await client.post(
        "/profile/save",
        headers=h,
        data={"name": "Renamed", "email": "keep@example.org", "research_summary": "written"},
    )
    assert r.headers["location"] == "/profile?saved=1"
    assert (await _user_row(db_session, u.id))["name"] == "Renamed"


async def test_profile_refresh_enqueues_exactly_one_job(client, db_session):
    u = await factories.make_user(db_session)
    await db_session.flush()
    r = await client.post("/profile/refresh", headers=_auth(u.id))
    assert r.status_code == 302 and r.headers["location"] == "/profile?refreshing=1"
    assert await _job_count(db_session, u.id) == 1


async def test_delete_account_confirmation_page_renders(client, db_session):
    u = await factories.make_user(db_session)
    await db_session.flush()
    r = await client.get("/profile/delete-account", headers=_auth(u.id))
    assert r.status_code == 200
    assert "Delete Account" in r.text


async def test_delete_account_needs_the_confirmation_word(client, db_session):
    u = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=u)
    db_session.add(Publication(user_id=u.id, title="doomed paper"))
    await db_session.flush()
    h = _auth(u.id)

    for word in ("", "yes", "DELETE ME"):
        r = await client.post("/profile/delete-account", headers=h, data={"confirm": word})
        assert r.status_code == 302
        assert "error=1" in r.headers["location"], word
        assert await _user_row(db_session, u.id) is not None, f"{word!r} deleted the account"

    # control: the word does delete, cascading to the profile and publications.
    # (Case-insensitive by design — profile.py lowercases the input.)
    r = await client.post("/profile/delete-account", headers=h, data={"confirm": "Delete"})
    assert r.status_code == 302 and r.headers["location"] == "/login?deleted=1"
    assert await _user_row(db_session, u.id) is None
    assert await _prof(db_session, u.id) is None
    assert (
        await db_session.execute(
            select(func.count()).select_from(Publication).where(Publication.user_id == u.id)
        )
    ).scalar_one() == 0


# ---------------------------------------------------------------------------
# 3. src/services/profile_export.py — no test referenced this module at all
# ---------------------------------------------------------------------------


async def test_the_public_export_never_carries_the_private_profile(
    db_session, export_dirs
):
    """The highest-consequence assertion in this task.

    The public export is what any agent (and anything downstream of the agent)
    reads. ``ResearcherProfile.private_profile_md`` is the PI's confidential
    content — both writers of it (the onboarding-side ``export_private_profile``
    and the agent-dashboard editor in ``src/routers/agent_page.py``) were
    retired along with the rest of private instructions (2026-08-12 removal
    cycle); the column stays as legacy-tolerance for any pre-cycle rows, and
    must never appear in the public export. The control asserts the canary is
    real, non-empty content on the row — not an empty field that would make
    "absent from the export" trivial.
    """
    user = await factories.make_user(
        db_session, name="Export Pi", institution="Scripps", department="Mol Bio"
    )
    prof = await factories.make_profile(
        db_session,
        user=user,
        research_summary="Summary of the lab's work.",
        techniques=["cryo-EM", "MD simulation"],
        experimental_models=["zebrafish"],
        disease_areas=["glioma"],
        key_targets=["EGFR"],
        keywords=["kinase", "structure"],
        grant_titles=["R01 Something Important"],
        private_profile_md="PRIVATE-CANARY-never-export-me",
    )
    pubs = [
        Publication(
            user_id=user.id,
            title="A structural paper.",
            journal="Cell",
            year=2020,
            doi="10.1016/j.cell.2020.01.001",
        )
    ]

    path = profile_export.export_profile_to_markdown(user, prof, "exportpi", publications=pubs)
    assert path == export_dirs.public / "exportpi.md"
    text = path.read_text(encoding="utf-8")

    for expected in (
        "Export Pi Lab — Public Profile",
        "**PI:** Export Pi",
        "**Institution:** Scripps",
        "**Department:** Mol Bio",
        "Summary of the lab's work.",
        "- cryo-EM",
        "- MD simulation",
        "- zebrafish",
        "- glioma",
        "- EGFR",
        "kinase, structure",
        "- R01 Something Important",
        "A structural paper. *Cell*. (2020). https://doi.org/10.1016/j.cell.2020.01.001",
    ):
        assert expected in text, f"the public export dropped {expected!r}"

    assert "PRIVATE-CANARY-never-export-me" not in text, (
        "the public profile export leaks private_profile_md"
    )

    # CONTROL — the canary is real content on the row, not an empty field.
    assert prof.private_profile_md == "PRIVATE-CANARY-never-export-me"


async def test_the_public_export_is_gated_on_an_agent_registry_id(db_session, export_dirs):
    user = await factories.make_user(db_session)
    prof = await factories.make_profile(db_session, user=user)

    assert profile_export.export_profile_to_markdown(user, prof, None) is None
    assert not export_dirs.public.exists()

    # control: with an agent id the export writes.
    assert profile_export.export_profile_to_markdown(user, prof, "gated") is not None


async def test_the_export_drops_a_doi_that_contradicts_the_journal(db_session):
    """_validate_doi_journal, through the export. A DOI attributed to the wrong
    journal is a paper attributed to the wrong lab."""
    user = await factories.make_user(db_session)
    prof = await factories.make_profile(db_session, user=user)

    mismatch = Publication(
        user_id=user.id,
        title="Mislinked paper",
        journal="Cell",
        year=2019,
        doi="10.1126/science.aaa1234",
        pmid="31111111",
    )
    text = profile_export.export_profile_to_markdown(
        user, prof, "doipi", publications=[mismatch]
    ).read_text(encoding="utf-8")
    assert "https://pubmed.ncbi.nlm.nih.gov/31111111/" in text
    assert "10.1126/science.aaa1234" not in text

    # control: the same DOI on the journal it belongs to is kept.
    match = Publication(
        user_id=user.id,
        title="Correctly linked paper",
        journal="Science",
        year=2019,
        doi="10.1126/science.aaa1234",
        pmid="31111111",
    )
    text = profile_export.export_profile_to_markdown(
        user, prof, "doipi", publications=[match]
    ).read_text(encoding="utf-8")
    assert "https://doi.org/10.1126/science.aaa1234" in text


async def test_the_export_keeps_the_twenty_most_recent_publications(db_session):
    user = await factories.make_user(db_session)
    prof = await factories.make_profile(db_session, user=user)
    pubs = [
        Publication(user_id=user.id, title=f"Paper {year}", journal="J", year=year)
        for year in range(1990, 2015)  # 25 of them
    ]
    text = profile_export.export_profile_to_markdown(
        user, prof, "manypi", publications=pubs
    ).read_text(encoding="utf-8")

    assert "Paper 2014" in text  # newest kept
    assert "Paper 1990" not in text  # oldest dropped
    assert text.count("- Paper ") == 20


async def test_saving_the_profile_writes_the_export_and_records_a_public_revision(
    client, db_session, export_dirs
):
    """The route side of the export, plus its AgentRegistry gate."""
    user = await factories.make_user(db_session, name="Route Pi")
    agent = await factories.make_agent(
        db_session, user=user, agent_id="routepi", bot_name="RoutePiBot"
    )
    await factories.make_profile(db_session, user=user)
    await db_session.flush()

    r = await client.post(
        "/profile/save",
        headers=_auth(user.id),
        data={
            "name": "Route Pi",
            "email": user.email,
            "research_summary": "EXPORTED-VIA-ROUTE",
            "techniques": "route-technique",
        },
    )
    assert r.status_code == 302

    written = (export_dirs.public / "routepi.md").read_text(encoding="utf-8")
    assert "EXPORTED-VIA-ROUTE" in written
    assert "- route-technique" in written

    revs = (
        await db_session.execute(
            select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent.id)
        )
    ).scalars().all()
    assert [rv.profile_type for rv in revs] == ["public"]
    assert revs[0].mechanism == "web"
    assert revs[0].changed_by_user_id == user.id
    assert revs[0].content == written, "the revision must record what was exported"

    # control: the same save by a user with no AgentRegistry writes no file and
    # no revision, which is why the gate exists.
    plain = await factories.make_user(db_session, name="No Agent")
    await factories.make_profile(db_session, user=plain)
    await db_session.flush()
    r = await client.post(
        "/profile/save",
        headers=_auth(plain.id),
        data={"name": "No Agent", "email": plain.email, "research_summary": "no export"},
    )
    assert r.status_code == 302
    assert sorted(p.name for p in export_dirs.public.iterdir()) == ["routepi.md"]
    assert (
        await db_session.execute(select(func.count()).select_from(ProfileRevision))
    ).scalar_one() == 1


# ---------------------------------------------------------------------------
# 4. src/routers/settings.py
# ---------------------------------------------------------------------------


async def test_every_setting_persists_and_is_reflected_on_the_next_request(
    client, db_session
):
    u = await factories.make_user(db_session)
    await db_session.flush()
    h = _auth(u.id)

    # The GET before any POST shows CATEGORY_DEFAULTS.
    r = await client.get("/settings", headers=h)
    assert r.status_code == 200
    assert _toggle(r.text, "proposal_review") == "1"
    assert _frequency(r.text, "proposal_review") == "weekly"
    assert _toggle(r.text, "status_overview") == "1"
    assert _toggle(r.text, "new_proposal") == "0"
    assert _toggle(r.text, "news_updates") == "1"

    # Turn everything on, with non-default frequencies.
    r = await client.post("/settings/save", headers=h, data=_all_on("daily", "monthly"))
    assert r.status_code == 302 and r.headers["location"] == "/settings?saved=1"
    assert (await _user_row(db_session, u.id))["email_notification_frequency"] == "daily"
    prefs = await _prefs(db_session, u.id)
    assert prefs["status_overview"] == (True, "monthly")
    assert prefs["new_proposal"][0] is True
    assert prefs["news_updates"][0] is True

    r = await client.get("/settings", headers=h)
    assert _frequency(r.text, "proposal_review") == "daily"
    assert _frequency(r.text, "status_overview") == "monthly"
    for key in ("proposal_review", "status_overview", "new_proposal", "news_updates"):
        assert _toggle(r.text, key) == "1", key

    # Control for the above: turning everything off must also round-trip, so
    # "reflected on the next request" is not satisfied by a page hard-coded on.
    r = await client.post("/settings/save", headers=h, data=ALL_OFF)
    assert r.status_code == 302
    assert (await _user_row(db_session, u.id))["email_notification_frequency"] == "off"
    prefs = await _prefs(db_session, u.id)
    assert prefs["status_overview"] == (False, "off")
    assert prefs["new_proposal"][0] is False
    assert prefs["news_updates"][0] is False

    r = await client.get("/settings", headers=h)
    for key in ("proposal_review", "status_overview", "new_proposal", "news_updates"):
        assert _toggle(r.text, key) == "0", key


async def test_the_settings_page_reads_defaults_without_inserting_rows(client, db_session):
    """The GET is documented as insert-free; the POST is what materialises rows."""
    u = await factories.make_user(db_session)
    await db_session.flush()
    h = _auth(u.id)

    assert (await client.get("/settings", headers=h)).status_code == 200
    assert await _prefs(db_session, u.id) == {}, "the settings GET inserted preference rows"

    # control
    assert (await client.post("/settings/save", headers=h, data=ALL_OFF)).status_code == 302
    assert set(await _prefs(db_session, u.id)) == {
        "status_overview",
        "new_proposal",
        "news_updates",
    }


async def test_an_invalid_review_frequency_falls_back_to_weekly(client, db_session):
    u = await factories.make_user(db_session)
    await db_session.flush()
    h = _auth(u.id)

    await client.post(
        "/settings/save",
        headers=h,
        data={**ALL_OFF, "proposal_review_on": "1", "proposal_review_frequency": "hourly"},
    )
    assert (await _user_row(db_session, u.id))["email_notification_frequency"] == "weekly"

    # control: a valid value is stored as given, not coerced.
    await client.post(
        "/settings/save",
        headers=h,
        data={**ALL_OFF, "proposal_review_on": "1", "proposal_review_frequency": "biweekly"},
    )
    assert (await _user_row(db_session, u.id))["email_notification_frequency"] == "biweekly"


async def test_re_enabling_review_emails_clears_a_system_pause(client, db_session):
    u = await factories.make_user(
        db_session, email_notification_frequency="off",
        email_notifications_paused_by_system=True,
    )
    await db_session.flush()
    h = _auth(u.id)

    # control first: staying off leaves the pause in place.
    await client.post("/settings/save", headers=h, data=ALL_OFF)
    assert (await _user_row(db_session, u.id))["email_notifications_paused_by_system"] is True

    await client.post(
        "/settings/save",
        headers=h,
        data={**ALL_OFF, "proposal_review_on": "1", "proposal_review_frequency": "weekly"},
    )
    assert (await _user_row(db_session, u.id))["email_notifications_paused_by_system"] is False


async def test_changing_the_review_frequency_resets_the_missed_counter(client, db_session):
    u = await factories.make_user(db_session, email_notification_frequency="weekly")
    db_session.add(EmailEngagementTracker(user_id=u.id, consecutive_missed=3))
    await db_session.flush()
    h = _auth(u.id)

    async def missed():
        return (
            await db_session.execute(
                select(EmailEngagementTracker.consecutive_missed).where(
                    EmailEngagementTracker.user_id == u.id
                )
            )
        ).scalar_one()

    # control: re-saving the same frequency must not reset it.
    await client.post(
        "/settings/save",
        headers=h,
        data={**ALL_OFF, "proposal_review_on": "1", "proposal_review_frequency": "weekly"},
    )
    assert await missed() == 3

    await client.post(
        "/settings/save",
        headers=h,
        data={**ALL_OFF, "proposal_review_on": "1", "proposal_review_frequency": "daily"},
    )
    assert await missed() == 0


async def test_the_unsubscribe_get_is_read_only_and_the_post_performs_it(client, db_session):
    """Email-security scanners fetch every link; the GET must not mutate."""
    u = await factories.make_user(db_session, email_notification_frequency="weekly")
    await db_session.flush()
    token = _generate_unsubscribe_token(str(u.id))

    r = await client.get(f"/settings/unsubscribe/{token}")
    assert r.status_code == 200
    assert "Invalid or expired" not in r.text
    assert (await _user_row(db_session, u.id))["email_notification_frequency"] == "weekly", (
        "the unsubscribe GET unsubscribed the user"
    )

    # control: the POST does what the GET refused to.
    r = await client.post(f"/settings/unsubscribe/{token}")
    assert r.status_code == 200
    assert (await _user_row(db_session, u.id))["email_notification_frequency"] == "off"


async def test_an_unsubscribe_token_only_affects_the_user_it_was_minted_for(
    client, db_session
):
    a = await factories.make_user(db_session, email_notification_frequency="weekly")
    b = await factories.make_user(db_session, email_notification_frequency="weekly")
    await db_session.flush()

    r = await client.post(f"/settings/unsubscribe/{_generate_unsubscribe_token(str(a.id))}")
    assert r.status_code == 200
    assert (await _user_row(db_session, a.id))["email_notification_frequency"] == "off"
    assert (await _user_row(db_session, b.id))["email_notification_frequency"] == "weekly"

    # control: b's own token turns b off, so "b untouched" is not just an
    # endpoint that never works.
    await client.post(f"/settings/unsubscribe/{_generate_unsubscribe_token(str(b.id))}")
    assert (await _user_row(db_session, b.id))["email_notification_frequency"] == "off"


async def test_unsubscribe_handles_a_token_for_a_user_that_no_longer_exists(
    client, db_session
):
    ghost = await factories.make_user(db_session)
    token = _generate_unsubscribe_token(str(ghost.id))
    await db_session.delete(ghost)
    await db_session.flush()

    assert "User not found" in (await client.get(f"/settings/unsubscribe/{token}")).text
    r = await client.post(f"/settings/unsubscribe/{token}")
    assert r.status_code == 404 and "User not found" in r.text


# ---------------------------------------------------------------------------
# 5. authorization, asserted per endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ep", ENDPOINTS, ids=_ID.get)
async def test_every_endpoint_that_needs_a_session_redirects_a_logged_out_caller(
    client, db_session, ep
):
    """Half one of the sweep, per endpoint.

    The redirect alone is not enough: /profile/delete-account redirects to
    /login on success too, so a route that let an anonymous caller through
    would still land on a Location starting with "/login". Each case therefore
    also asserts the anonymous request changed nothing, and pairs that with the
    same request carrying a session, which must do the thing — otherwise an
    endpoint that is simply broken would score as correctly protected.
    """
    u = await factories.make_user(
        db_session,
        email=f"sweep-{ep.method.lower()}{abs(hash(ep.path)) % 10**8}@example.org",
        onboarding_complete=ep.onboarding_complete,
        access_status="allowed",
    )
    await factories.make_profile(db_session, user=u)
    await db_session.flush()
    token = _generate_unsubscribe_token(str(u.id))

    before = await _snapshot(db_session, u.id)
    logged_out = await _send(client, ep, u, {}, token=token)
    after_anonymous = await _snapshot(db_session, u.id)

    if ep.auth == "token":
        # Documented exemption: unsubscribe links are clicked from an email
        # client. Their protection is the signed token, tested in the next sweep.
        assert logged_out.status_code == 200, ep.label
        assert (await _send(client, ep, u, _auth(u.id), token=token)).status_code == 200
        return

    assert logged_out.status_code == 302, f"{ep.label} served a logged-out caller"
    assert logged_out.headers["location"].startswith("/login"), ep.label
    assert after_anonymous == before, f"{ep.label} acted on behalf of a logged-out caller"

    logged_in = await _send(client, ep, u, _auth(u.id), token=token)
    after_session = await _snapshot(db_session, u.id)
    if ep.method == "POST":
        assert after_session != before, (
            f"{ep.label} does nothing even with a valid session, so 'nothing "
            "happened for the anonymous caller' proves nothing"
        )
    else:
        assert logged_in.status_code == 200, (
            f"{ep.label} does not render even with a valid session, so the "
            "redirect above is not evidence of authorization"
        )


@pytest.mark.parametrize("ep", ENDPOINTS, ids=_ID.get)
async def test_no_logged_in_user_can_read_or_write_another_users_data(client, db_session, ep):
    """Half two, the one worth most, per endpoint.

    None of these routes takes a target user id, so the only handle a caller has
    on another identity is the ``copi-impersonate`` cookie, which
    get_current_user honours for admins only. Each case fires the attacker's
    request with that cookie pointed at the victim and asserts the effect landed
    on the attacker; then fires the identical request as a real admin and
    asserts it DOES land on the victim — so a renamed or removed cookie could
    not make the negative half pass vacuously.

    For the two unsubscribe endpoints, which carry no session at all, the
    cross-user question is instead whether the attacker can mint a token for the
    victim; three forgeries are tried and the genuine token is the control.
    """
    victim = await factories.make_user(
        db_session,
        name="Victim Alpha",
        email="victim@example.org",
        institution="Victim Institute",
        onboarding_complete=ep.onboarding_complete,
        access_status="allowed",
    )
    await factories.make_profile(
        db_session,
        user=victim,
        research_summary="VICTIM-SECRET-SUMMARY",
        private_profile_md="VICTIM-PRIVATE-SECRET",
        private_profile_seed=None,
    )
    # A completed job, so GET /onboarding renders the review form rather than
    # self-healing a new job into the victim's snapshot.
    db_session.add(
        Job(type="generate_profile", status="completed", user_id=victim.id, payload={})
    )

    attacker = await factories.make_user(
        db_session,
        name="Attacker Beta",
        email="attacker@example.org",
        user_role=USER_ROLE_PI,
        onboarding_complete=ep.onboarding_complete,
        access_status="allowed",
    )
    await factories.make_profile(
        db_session,
        user=attacker,
        research_summary="attacker summary",
        private_profile_md="attacker private",
        private_profile_seed=None,
    )
    db_session.add(
        Job(type="generate_profile", status="completed", user_id=attacker.id, payload={})
    )
    await db_session.flush()

    victim_before = await _snapshot(db_session, victim.id)
    attacker_before = await _snapshot(db_session, attacker.id)

    if ep.auth == "token":
        secret = get_settings().secret_key
        forgeries = {
            "the bare user id": str(victim.id),
            "a token signed with another secret": URLSafeTimedSerializer(
                "not-the-real-secret", salt="unsubscribe"
            ).dumps(str(victim.id)),
            "a token signed with the wrong salt": URLSafeTimedSerializer(
                secret, salt="not-unsubscribe"
            ).dumps(str(victim.id)),
        }
        for how, tok in forgeries.items():
            r = await _send(client, ep, victim, _auth(attacker.id), token=tok)
            assert "Invalid or expired" in r.text, f"{ep.label} accepted {how}"
            assert await _snapshot(db_session, victim.id) == victim_before, (
                f"{ep.label} let {how} change another user's settings"
            )

        # CONTROL — the genuine token is accepted, so the rejections above are
        # about the signature and not about a route that rejects everything.
        genuine = _generate_unsubscribe_token(str(victim.id))
        r = await _send(client, ep, victim, {}, token=genuine)
        assert r.status_code == 200 and "Invalid or expired" not in r.text
        after = await _snapshot(db_session, victim.id)
        if ep.method == "GET":
            assert after == victim_before, "the unsubscribe GET is supposed to be read-only"
        else:
            assert after != victim_before, "the genuine token did nothing"
        return

    r = await _send(client, ep, attacker, _auth_as(attacker.id, victim.id))
    assert r.status_code in (200, 302), f"{ep.label} errored for the attacker: {r.status_code}"

    victim_after = await _snapshot(db_session, victim.id)
    attacker_after = await _snapshot(db_session, attacker.id)
    assert victim_after == victim_before, (
        f"AUTHORIZATION HOLE: {ep.label} let a non-admin change another user's data"
    )

    if ep.method == "GET":
        assert r.status_code == 200, f"{ep.label} did not render for the attacker"
        for leaked in ("Viewing as Victim Alpha", "VICTIM-SECRET-SUMMARY",
                       "VICTIM-PRIVATE-SECRET", "Victim Institute"):
            assert leaked not in r.text, (
                f"AUTHORIZATION HOLE: {ep.label} showed a non-admin {leaked!r}"
            )
    else:
        assert attacker_after != attacker_before, (
            f"{ep.label} changed nothing for the caller either, so the "
            "'victim unchanged' assertion above proves nothing"
        )

    # CONTROL — the same cookie, from a real admin, must reach the victim.
    admin = await factories.make_user(
        db_session, name="Real Admin", email="realadmin@example.org", user_role=USER_ROLE_ADMIN
    )
    await db_session.flush()
    control_before = await _snapshot(db_session, victim.id)
    r2 = await _send(client, ep, victim, _auth_as(admin.id, victim.id))
    control_after = await _snapshot(db_session, victim.id)

    if ep.method == "GET":
        assert r2.status_code == 200, ep.label
        assert "Viewing as Victim Alpha" in r2.text, (
            "the copi-impersonate cookie is inert even for an admin, so the "
            "negative assertions above are not testing anything"
        )
    else:
        if ep.method == "POST" and ep.path == "/profile/delete-account":
            # The deletion impersonation guard (deletion audit F8) refuses
            # this one mutation outright. The 403 is still positive proof the
            # cookie was honoured: the guard fires on _is_impersonated, which
            # only get_current_user's impersonation path sets — an inert
            # cookie would have self-deleted the admin with a 302.
            assert r2.status_code == 403, (
                "the delete-account impersonation guard did not fire for an admin"
            )
            assert control_after == control_before, (
                "the refused delete still changed the victim's data"
            )
        else:
            assert control_after != control_before, (
                "the copi-impersonate cookie is inert even for an admin, so the "
                "negative assertions above are not testing anything"
            )
