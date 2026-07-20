"""Regression tests for the post-login "next" redirect guard.

When a logged-out user requests a protected page, they are bounced to /login
with the intended path remembered so we can resume there after ORCID auth.
The stored value is attacker-influenceable (it rides in the URL), so
``is_safe_next_url`` must:

  1. reject anything that isn't a purely relative, same-origin path
     (open-redirect defense), and
  2. only accept paths that resolve to a real GET page, excluding the
     auth/session flow and state-mutating GET links.
"""

from types import SimpleNamespace

import pytest

from src.main import create_app
from src.routers.auth import is_safe_next_url

# One app instance is enough — routes don't change between requests.
_APP = create_app()


def _req():
    return SimpleNamespace(app=_APP)


@pytest.mark.parametrize(
    "target",
    [
        "/settings",           # the case this feature exists to fix
        "/profile",
        "/profile/edit",
        "/agent/smith/dashboard",   # path-param route
        "/profile?tab=activity",    # query string preserved
        "/admin",                   # real page; per-route auth still applies
    ],
)
def test_accepts_real_local_pages(target):
    assert is_safe_next_url(_req(), target) is True


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.com",         # absolute URL
        "http://evil.com/x",
        "//evil.com",               # protocol-relative
        "https:evil.com",           # scheme-only
        "/\\evil.com",              # backslash folded to / by some browsers
        "\\/evil.com",
        "javascript:alert(1)",      # non-http scheme
        "/path\r\nSet-Cookie: x=1",  # header injection
        "",
        None,
        123,                         # non-string
    ],
)
def test_rejects_open_redirects_and_junk(target):
    assert is_safe_next_url(_req(), target) is False


@pytest.mark.parametrize(
    "target",
    [
        "/does-not-exist",          # not a registered page -> would 404
        "/login",                   # auth flow
        "/login/start",
        "/auth/callback",
        "/logout",                  # would immediately log the user back out
        "/settings/unsubscribe/sometoken",  # state-mutating GET link
    ],
)
def test_rejects_nonpages_and_sensitive_endpoints(target):
    assert is_safe_next_url(_req(), target) is False
