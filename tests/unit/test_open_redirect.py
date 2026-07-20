"""Pins the open-redirect defense in auth.is_safe_next_url (the rejection layer).

The accept path calls _resolves_to_get_page, which needs live app routing; the
security-critical rejections all short-circuit before that, so they are unit-testable
with a bare request. These pin the guard added for post-login redirect resume.
"""

import pytest
from starlette.requests import Request

from src.routers.auth import is_safe_next_url


def _req() -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.com/x",  # absolute URL
        "http://evil.com",  # absolute URL
        "//evil.com",  # protocol-relative -> netloc set
        "/legit\\..\\evil",  # backslash (browsers may fold to /)
        "/x\r\nSet-Cookie: y=1",  # CRLF header injection
        "/x\ndrop",  # bare newline
        "/x\x00",  # null byte
        "relative/no/leading/slash",  # not path-absolute
        "",  # empty
        "x" * 2001,  # over the 2000-char cap
    ],
)
def test_is_safe_next_url_rejects_open_redirects(target):
    assert is_safe_next_url(_req(), target) is False


def test_is_safe_next_url_rejects_non_string():
    assert is_safe_next_url(_req(), None) is False
    assert is_safe_next_url(_req(), 12345) is False
