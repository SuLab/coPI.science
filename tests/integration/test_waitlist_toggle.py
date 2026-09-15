"""The ``waitlist_enabled`` kill switch: the endpoint AND the form, together.

Disabling public signups has to close two doors, and closing only one is the
failure this file exists to catch:

1. **The endpoint.** ``POST /waitlist`` is reachable without the form — the app
   serves ``/openapi.json`` publicly, so the route is machine-discoverable. A
   template-only change would leave a live, documented write endpoint on an
   unauthenticated path.
2. **The form.** Leaving it rendered while the handler refuses invites visitors
   to fill in four fields and receive an error.

Every "signups are off" assertion is paired with an enabled-state positive
control in the same test, because both halves of this switch are absence
assertions — and "the form isn't rendered" is trivially true of a page that
failed to render at all.

Real ASGI requests, real Postgres, real Jinja. ``get_settings`` is
``@lru_cache``d, so each test sets the env var and clears the cache (the
``waitlist_flag`` fixture), matching how prod picks the flag up: a container
recreate, not a signal.
"""

import uuid

import pytest
from sqlalchemy import func, select

from src.config import get_settings
from src.models import WaitlistSignup

pytestmark = pytest.mark.integration

# The form's own markup, not merely the word "waitlist": the announcement bar
# and the hero CTA both mention the waitlist in prose, so asserting on the
# POST target is what actually distinguishes "form present" from "form gone".
FORM_MARKER = 'action="/waitlist"'

# The section wrapper itself. The form can be gone while the section survives as
# an empty gradient band, so "no form" and "no section" are separate assertions.
SECTION_MARKER = 'id="waitlist"'

# The waitlist band is the last thing on the page, so hiding it costs the page
# its closing call to action. A sign-in band takes over; this is its heading.
CTA_MARKER = "Ready to meet your co-PI?"


@pytest.fixture
def waitlist_flag(monkeypatch):
    """Set WAITLIST_ENABLED and clear the Settings cache, restoring it after.

    The cache is cleared on the way out as well as in: a stale True/False left
    in the lru_cache would leak into every later test in the session.
    """

    def _set(value: bool):
        monkeypatch.setenv("WAITLIST_ENABLED", "true" if value else "false")
        get_settings.cache_clear()
        assert get_settings().waitlist_enabled is value, "env var did not take effect"

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


async def _signup_count(db) -> int:
    return (await db.execute(select(func.count()).select_from(WaitlistSignup))).scalar()


def _unique_email() -> str:
    return f"toggle-{uuid.uuid4().hex[:12]}@example.edu"


# --- the endpoint ------------------------------------------------------------


async def test_disabled_post_is_refused_and_writes_nothing(client, db_session, waitlist_flag):
    """403 and zero rows written — the whole point of the switch."""
    waitlist_flag(False)
    before = await _signup_count(db_session)

    r = await client.post("/waitlist", data={"email": _unique_email()})

    assert r.status_code == 403
    assert await _signup_count(db_session) == before, "a refused signup still wrote a row"


async def test_enabled_post_still_writes(client, db_session, waitlist_flag):
    """Positive control for the test above: the switch is off, so signups work."""
    waitlist_flag(True)
    email = _unique_email()
    before = await _signup_count(db_session)

    r = await client.post("/waitlist", data={"email": email})

    assert r.status_code == 200
    assert await _signup_count(db_session) == before + 1
    row = (
        await db_session.execute(select(WaitlistSignup).where(WaitlistSignup.email == email))
    ).scalar_one()
    assert row.email == email


async def test_disabled_refuses_an_existing_email_without_updating_it(
    client, db_session, waitlist_flag
):
    """The handler UPSERTS on email, so the disabled path must not fall through
    to the update branch either — refusing only the INSERT would still let
    anyone rewrite the name/institution/note of an existing signup."""
    email = _unique_email()
    db_session.add(WaitlistSignup(email=email, name="Original", institution="Original U"))
    await db_session.commit()

    waitlist_flag(False)
    r = await client.post(
        "/waitlist",
        data={"email": email, "name": "Overwritten", "institution": "Overwritten U"},
    )

    assert r.status_code == 403
    row = (
        await db_session.execute(select(WaitlistSignup).where(WaitlistSignup.email == email))
    ).scalar_one()
    assert row.name == "Original", "disabled endpoint overwrote an existing signup"
    assert row.institution == "Original U"


async def test_disabled_refuses_before_validation(client, waitlist_flag):
    """An invalid email must still get 403, not 400.

    The guard belongs ahead of validation and the rate limiter: if it sat after
    them, the endpoint would keep leaking its validation behaviour (and keep
    consuming per-IP limiter budget) while nominally disabled.
    """
    waitlist_flag(False)
    r = await client.post("/waitlist", data={"email": "not-an-email"})
    assert r.status_code == 403


# --- the form ----------------------------------------------------------------


async def test_disabled_landing_page_renders_without_the_form(client, waitlist_flag):
    waitlist_flag(False)
    r = await client.get("/")

    assert r.status_code == 200, "the landing page must still render with signups off"
    assert FORM_MARKER not in r.text, "the form is still on the page while signups are off"


async def test_enabled_landing_page_renders_the_form(client, waitlist_flag):
    """Positive control: FORM_MARKER is a marker that really does appear."""
    waitlist_flag(True)
    r = await client.get("/")

    assert r.status_code == 200
    assert FORM_MARKER in r.text


async def test_disabled_landing_page_drops_every_trace_of_the_waitlist(
    client, waitlist_flag
):
    """With signups off the page says nothing about a waitlist at all.

    Asserting on the section anchor as well as the prose is what stops a future
    edit from leaving the <section> in place with all of its inner branches
    false: that is invisible in a markup diff but renders as a bare band of
    indigo gradient at the foot of the page.
    """
    waitlist_flag(False)
    r = await client.get("/")

    assert r.status_code == 200
    assert SECTION_MARKER not in r.text, "an empty waitlist section is still rendered"
    assert "paused" not in r.text.lower()
    assert "waitlist" not in r.text.lower()


async def test_enabled_landing_page_keeps_the_waitlist_section(client, waitlist_flag):
    """Positive control: the markers above really do appear when signups are on.

    Also the other half of the swap — with the waitlist band back, the stand-in
    sign-in band must not render, or the page ends on two closing CTAs.
    """
    waitlist_flag(True)
    r = await client.get("/")

    assert r.status_code == 200
    assert SECTION_MARKER in r.text
    assert "waitlist" in r.text.lower()
    assert CTA_MARKER not in r.text, "both closing bands rendered at once"


async def test_disabled_landing_page_closes_on_a_signin_cta(client, waitlist_flag):
    """Hiding the waitlist must not leave the page trailing off the end of the
    "How it works" section: a sign-in band closes it instead, pointing at
    /login (the explainer) rather than /login/start (a bare ORCID redirect).
    """
    waitlist_flag(False)
    r = await client.get("/")

    assert r.status_code == 200
    assert CTA_MARKER in r.text, "the page has no closing call to action"
    assert 'href="/login"' in r.text


async def test_disabled_refusal_page_renders_without_the_form(client, waitlist_flag):
    """The 403 body is the landing page, so it must not re-offer the form —
    otherwise a direct POST hands the visitor a form that cannot work."""
    waitlist_flag(False)
    r = await client.post("/waitlist", data={"email": _unique_email()})

    assert r.status_code == 403
    assert "text/html" in r.headers["content-type"]
    assert FORM_MARKER not in r.text


# --- the flag itself ---------------------------------------------------------


async def test_default_is_enabled():
    """The flag ships ON: adding it must not silently close signups on deploy.

    Read from the model default rather than the process env, so a
    WAITLIST_ENABLED left in the developer's environment cannot make this pass.
    """
    from src.config import Settings

    assert Settings.model_fields["waitlist_enabled"].default is True
