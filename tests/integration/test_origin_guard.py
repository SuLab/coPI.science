"""CSRF: the Origin / Referer guard on state-changing requests (E1.1).

There was no request-side CSRF check anywhere in src/ — ``grep -rn
"Origin\\|Referer\\|csrf" src/`` found none — and the only defence was
``same_site="lax"`` on the session cookie. That defence is void in this
deployment: one nginx serves ``blackbird.copi.science``, ``copi.science`` (an
unrelated production tenant) and ``devel.copi.science``. SameSite is computed on
the *registrable* domain, so all three are **same-site** and a page on either
sibling could auto-submit a top-level POST with the victim's ``copi-session``
cookie attached. Reachable that way: ``POST /profile/delete-account`` (cascades
nine tables) and, against a signed-in admin, ``POST /admin/users/{id}/role``.

The probe here is ``POST /logout``. It is the codebase's own documented CSRF
target — src/routers/auth.py's ``logout`` docstring says it was made POST-only
for exactly this reason (SEC-8) and names SameSite=lax as the mitigation — it
needs no fixtures beyond a session, it has a crisp observable (the session is
cleared, or it is not), and it is rate-limited by nothing. Every refusal below
asserts that observable, not only the status code: a 403 on a request that was
inert anyway would prove nothing.
"""

from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from src.config import get_settings
from src.models import User
from src.services.email_notifications import _generate_unsubscribe_token
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

SESSION_COOKIE = "copi-session"

# Sibling tenants on the same registrable domain (so SameSite=lax lets them
# through), plus the two shapes a hostile page can produce instead of a real
# origin: a foreign site, and the literal "null" a sandboxed iframe sends.
FOREIGN_ORIGINS = [
    "https://copi.science",
    "https://devel.copi.science",
    "http://blackbird.copi.science",  # right host, wrong scheme
    "https://evil.example",
    "null",
]


def _own_origin() -> str:
    """Derived at runtime from settings, and deliberately NOT via src/main.py's
    own helper — a test that computed the expected value with the code under
    test would agree with it however wrong it was."""
    parts = urlsplit(get_settings().base_url)
    return f"{parts.scheme}://{parts.netloc}"


def _session_cookies(response) -> list[str]:
    return [
        v
        for k, v in response.headers.multi_items()
        if k.lower() == "set-cookie" and v.startswith(f"{SESSION_COOKIE}=")
    ]


def _session_was_cleared(response) -> bool:
    """Starlette ends a session by re-setting the cookie to the literal "null"."""
    for raw in _session_cookies(response):
        jar = SimpleCookie()
        jar.load(raw)
        if jar[SESSION_COOKIE].value in ("", "null"):
            return True
    return False


async def test_a_post_without_an_origin_is_refused(client_without_origin, db_session):
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post("/logout", headers=auth_headers(user.id))
    assert r.status_code == 403, r.text
    # The logout did not run: SessionMiddleware re-sets the cookie only when
    # the session is modified, and a refused request never gets that far.
    #
    # Read this as "the refusal was effective", NOT as evidence about middleware
    # ORDER. It carries no ordering signal at all — a refused request does not
    # modify the session wherever the guard sits in the stack, so this passes
    # with the guard innermost too (measured). Ordering is pinned separately by
    # test_the_guard_is_the_outermost_middleware.
    assert not _session_cookies(r), "the POST was refused but the logout ran anyway"


@pytest.mark.parametrize("origin", FOREIGN_ORIGINS)
async def test_a_post_from_a_sibling_domain_is_refused(
    client_without_origin, db_session, origin
):
    """``copi.science`` and ``devel.copi.science`` are the real neighbours on
    this host; SameSite=lax considers all three of us the same site, which is
    the whole reason this guard exists."""
    assert origin != _own_origin(), (
        f"{origin} IS this deployment's own origin — the case is vacuous here"
    )
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post(
        "/logout", headers={**auth_headers(user.id), "Origin": origin}
    )
    assert r.status_code == 403, r.text
    assert not _session_was_cleared(r), f"{origin} logged the victim out"


async def test_a_post_from_our_own_origin_is_allowed(client_without_origin, db_session):
    """The control. Without it, a guard that refused everything would score
    perfectly on every other test in this file."""
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post(
        "/logout", headers={**auth_headers(user.id), "Origin": _own_origin()}
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert _session_was_cleared(r), "the POST was allowed but did nothing"


async def test_a_post_with_only_a_referer_is_matched_on_its_origin(
    client_without_origin, db_session
):
    """Referer is the fallback when there is no Origin, and only its origin
    component counts — a Referer is a full URL with a path."""
    user = await factories.make_user(db_session)
    await db_session.flush()

    ours = await client_without_origin.post(
        "/logout",
        headers={**auth_headers(user.id), "Referer": f"{_own_origin()}/profile?tab=x"},
    )
    assert ours.status_code == 302

    theirs = await client_without_origin.post(
        "/logout",
        headers={**auth_headers(user.id), "Referer": "https://copi.science/profile"},
    )
    assert theirs.status_code == 403


async def test_a_trailing_slash_on_base_url_does_not_break_the_match(
    client_without_origin, db_session, monkeypatch
):
    """Trap: production's BASE_URL has no trailing slash, other environments'
    may, and a browser's Origin header never does. Comparing the raw strings
    would 403 every POST in any deployment configured with one."""
    real = get_settings()
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: real.model_copy(update={"base_url": _own_origin() + "/"}),
    )
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post(
        "/logout", headers={**auth_headers(user.id), "Origin": _own_origin()}
    )
    assert r.status_code == 302, r.text


async def test_a_get_needs_no_origin(client_without_origin):
    """Safe methods are exempt, or the whole site is unreachable. The ORCID
    callback is a GET, which is why the login round-trip survives this."""
    for path in ("/", "/login", "/api/health", "/auth/callback?error=access_denied"):
        r = await client_without_origin.get(path)
        assert r.status_code != 403, f"GET {path} was refused ({r.status_code})"


async def test_one_click_unsubscribe_still_works_without_an_origin(
    client_without_origin, db_session
):
    """RFC 8058. src/services/email_notifications.py sets
    ``List-Unsubscribe-Post: List-Unsubscribe=One-Click``, and the matching POST
    is issued **server-side by Gmail / Apple / Yahoo**, with no Origin and no
    Referer. A guard that exempted nothing would break one-click unsubscribe and
    bulk-sender compliance — src/routers/auth.py already treats this same path
    as a non-browser exemption (``_POST_LOGIN_DENY_PREFIXES``).
    """
    user = await factories.make_user(db_session, email_notification_frequency="weekly")
    await db_session.flush()
    token = _generate_unsubscribe_token(str(user.id))

    r = await client_without_origin.post(f"/settings/unsubscribe/{token}")
    assert r.status_code == 200, r.text
    assert (
        await db_session.scalar(
            select(User.email_notification_frequency).where(User.id == user.id)
        )
        == "off"
    )


async def test_the_unsubscribe_exemption_requires_no_session_cookie(
    client_without_origin, db_session
):
    """The exemption is gated on the request carrying no session, so it cannot
    be turned into a CSRF gadget. A real mail provider's one-click POST has no
    cookies for us; a forged one from a sibling tab necessarily does."""
    user = await factories.make_user(db_session, email_notification_frequency="weekly")
    await db_session.flush()
    token = _generate_unsubscribe_token(str(user.id))

    r = await client_without_origin.post(
        f"/settings/unsubscribe/{token}", headers=auth_headers(user.id)
    )
    assert r.status_code == 403, r.text
    assert (
        await db_session.scalar(
            select(User.email_notification_frequency).where(User.id == user.id)
        )
        == "weekly"
    ), "a cookie-bearing cross-site POST unsubscribed the victim"


def test_the_guard_is_the_outermost_middleware():
    """Structural, because no request-level assertion in this file can see it.

    The obvious behavioural proxy — "a refused request set no session cookie",
    asserted in ``test_a_post_without_an_origin_is_refused`` — carries ZERO
    ordering signal, and the first version of this file wrongly claimed it did.
    Starlette's ``SessionMiddleware.send_wrapper`` emits ``Set-Cookie`` only
    ``if session.modified and session``, and a request the guard refuses never
    touches the session at all. Demote the guard to innermost and that
    assertion still passes; measured, 12/12 green with
    ``add_middleware(OriginGuardMiddleware)`` moved to the first call in
    ``create_app()``. So the invariant needs a direct look at the stack.

    Why outermost is the requirement and not a preference: everything the guard
    is in front of costs something on a request it is going to refuse. In
    particular ``AgentBadgeMiddleware`` (src/main.py) opens its own database
    session and runs an own-agents SELECT, a delegated-agents JOIN, and a
    ThreadDecision/ProposalReview COUNT pair PER agent — for any request
    carrying a session cookie. A demoted guard therefore turns every forged
    cross-site POST into an unauthenticated database-load amplifier: the
    attacker still cannot change anything, but they can make each refused
    request cost several queries. Refusing on headers alone, before any of that
    is constructed, is the point.

    ``user_middleware`` is in outermost-to-innermost order (Starlette's
    ``build_middleware_stack`` wraps in reverse), and ``add_middleware``
    PREPENDS — so "outermost" means "added last in create_app()", which reads
    backwards and is exactly the kind of thing a later edit gets wrong.
    """
    from src.main import create_app

    order = [m.cls.__name__ for m in create_app().user_middleware]
    assert order[0] == "OriginGuardMiddleware", (
        f"the CSRF guard is not outermost; stack is {order}"
    )


# ---------------------------------------------------------------------------
# Sec-Fetch-Site (fix round 1)
#
# Origin-or-Referer alone is an availability defect, not just an incomplete
# guard: a browser under a `no-referrer` policy — set by an extension or by
# enterprise config — sends `Origin: null` AND no Referer on a **same-origin**
# form POST. Every form on the site then returns a bare-text 403 with no way to
# recover. `Sec-Fetch-Site` is the header that distinguishes that user from an
# attacker, and the browser computes it.
# ---------------------------------------------------------------------------


async def test_a_no_referrer_browser_is_accepted_on_sec_fetch_site(
    client_without_origin, db_session
):
    """The availability case: same-origin POST, Origin: null, no Referer."""
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post(
        "/logout",
        headers={
            **auth_headers(user.id),
            "Origin": "null",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert r.status_code == 302, r.text
    assert _session_was_cleared(r), "allowed, but the logout did not run"


@pytest.mark.parametrize("fetch_site", ["same-site", "cross-site", "none", ""])
async def test_only_sec_fetch_site_same_origin_is_accepted(
    client_without_origin, db_session, fetch_site
):
    """``same-site`` is the one that matters and the one that is refused.

    It is computed on the REGISTRABLE domain, so ``copi.science`` and
    ``devel.copi.science`` attacking ``blackbird.copi.science`` send exactly
    that — accepting it would hand back the whole attack this guard exists to
    stop. ``none`` (typed URL / bookmark) and ``cross-site`` are refused too.
    """
    user = await factories.make_user(db_session)
    await db_session.flush()

    headers = {**auth_headers(user.id), "Origin": "null"}
    if fetch_site:
        headers["Sec-Fetch-Site"] = fetch_site

    r = await client_without_origin.post("/logout", headers=headers)
    assert r.status_code == 403, f"Sec-Fetch-Site: {fetch_site!r} was accepted"
    assert not _session_was_cleared(r)


@pytest.mark.parametrize("origin", ["https://copi.science", "https://evil.example"])
async def test_a_wrong_origin_is_refused_even_with_sec_fetch_site_same_origin(
    client_without_origin, db_session, origin
):
    """Precedence: a real Origin decides on its own.

    ``Sec-Fetch-Site`` is only consulted when there is no usable Origin, so the
    two signals can never be played off against each other — an attacker has to
    beat whichever one the browser actually sent, not the weakest of them.
    """
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post(
        "/logout",
        headers={
            **auth_headers(user.id),
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert r.status_code == 403, r.text
    assert not _session_was_cleared(r)


@pytest.mark.parametrize(
    "base_url,origin,allowed",
    [
        # The default port, spelled out, is the same origin.
        ("https://lab.example", "https://lab.example:443", True),
        ("https://lab.example:443", "https://lab.example", True),
        ("http://lab.example", "http://lab.example:80", True),
        ("http://lab.example:80", "http://lab.example", True),
        # ...but only the DEFAULT one. A non-default port is a different origin,
        # and so is the default port of the *other* scheme.
        ("https://lab.example", "https://lab.example:8443", False),
        ("https://lab.example", "https://lab.example:80", False),
        ("http://lab.example", "http://lab.example:443", False),
        # Host comparison is case-insensitive; scheme is still significant.
        ("https://lab.example", "https://LAB.EXAMPLE", True),
        ("https://lab.example", "http://lab.example", False),
    ],
)
async def test_default_ports_are_normalised_away(
    client_without_origin, db_session, monkeypatch, base_url, origin, allowed
):
    """``https://host:443`` and ``https://host`` are the same origin by
    definition (RFC 6454 §4 normalises the default port away). Browsers happen
    never to spell it out, but a reverse proxy, a redirect or a non-browser
    client will — so comparing the strings is merely adequate, not correct."""
    real = get_settings()
    monkeypatch.setattr(
        "src.main.get_settings",
        lambda: real.model_copy(update={"base_url": base_url}),
    )
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post(
        "/logout", headers={**auth_headers(user.id), "Origin": origin}
    )
    assert r.status_code == (302 if allowed else 403), (
        f"base_url={base_url} origin={origin} -> {r.status_code}"
    )


async def test_the_refusal_body_is_the_string_the_e2e_probe_matches_on(
    client_without_origin, db_session
):
    """Drift alarm for a consumer outside this file.

    ``tests/e2e/test_browser_flows.py``'s
    ``_the_server_agrees_about_its_own_origin`` distinguishes "the CSRF guard
    refused me" from "the app returned 403 for some other reason" by grepping
    the response body for this exact substring. It cannot import the constant —
    that tier talks to a separately deployed server, possibly on older code —
    so a reworded message would silently turn its directed configuration error
    back into the scattering of unexplained 403s it exists to replace.
    """
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post("/logout", headers=auth_headers(user.id))
    assert r.status_code == 403
    assert "Cross-site request refused" in r.text, r.text


async def test_an_opaque_origin_is_not_rescued_by_a_referer(
    client_without_origin, db_session
):
    """``Origin: null`` may be rescued by ``Sec-Fetch-Site`` and by nothing else.

    ``null`` is a header the browser DID send, carrying an opaque origin — it is
    an answer, not a missing header, and it is the answer a sandboxed
    ``<iframe>`` gets. So it does not fall through to the Referer check the way
    an absent Origin does.

    That distinction is not a formality:

    * It rescues nobody. The users the ``Sec-Fetch-Site`` path exists for are on
      a ``no-referrer`` policy — they send Origin: null AND no Referer, so a
      Referer fallback never fires for them anyway.
    * The shape that WOULD produce "opaque origin plus a Referer on our own
      origin" is a sandboxed iframe pointed at one of our own pages, and the
      only thing standing in its way today is nginx's ``X-Frame-Options: DENY``
      — a header set in a different tier's config file. This guard's
      correctness must not depend on that.
    """
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post(
        "/logout",
        headers={
            **auth_headers(user.id),
            "Origin": "null",
            "Referer": f"{_own_origin()}/profile",
        },
    )
    assert r.status_code == 403, r.text
    assert not _session_was_cleared(r), "an opaque origin was rescued by its Referer"


async def test_an_opaque_origin_with_sec_fetch_site_is_still_allowed(
    client_without_origin, db_session
):
    """The control for the test above: closing the Referer path must not close
    the ``Sec-Fetch-Site`` path that the availability fix depends on, including
    when a Referer happens to be present as well."""
    user = await factories.make_user(db_session)
    await db_session.flush()

    r = await client_without_origin.post(
        "/logout",
        headers={
            **auth_headers(user.id),
            "Origin": "null",
            "Referer": f"{_own_origin()}/profile",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert r.status_code == 302, r.text
    assert _session_was_cleared(r)
