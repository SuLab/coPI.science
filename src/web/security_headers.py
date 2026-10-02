"""Security response headers and the per-request CSP nonce (spec §6.3; M-03, A-06).

``SecurityHeadersMiddleware`` gives every HTTP request a fresh nonce on
``request.state.csp_nonce`` (templates mark each inline ``<script>`` with it) and sets,
on every response it sends:

* ``Content-Security-Policy: ENFORCED_POLICY; REPORT_ONLY_POLICY`` — framing,
  ``<base>`` and plugins off, plus the script policy with this request's nonce,
  reporting violations to ``CSP_REPORT_PATH``;
* ``X-Frame-Options``, ``X-Content-Type-Options`` and ``Referrer-Policy``.

The script policy is enforced since Phase 2 (``SCRIPT_POLICY_ENFORCED = True``, spec
§7): no ``Content-Security-Policy-Report-Only`` header is sent. ``REPORT_ONLY_POLICY``
keeps its Phase 1 name so no caller changes.

A plain ASGI middleware, not ``BaseHTTPMiddleware``: it only rewrites the
``http.response.start`` message, so a streamed body passes through untouched. A 500
from an unhandled exception is sent by Starlette's ``ServerErrorMiddleware``, which
sits outside every user middleware, so that response carries these headers only if
the app's 500 handler adds ``security_header_items`` itself. ``csp_nonce`` is already
on that request's state.
"""

from __future__ import annotations

import secrets

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The violation-report sink (src/routers/csp_report.py). Named in REPORT_ONLY_POLICY
#: and exempted, for this exact path, from OriginGuardMiddleware.
CSP_REPORT_PATH = "/api/csp-report"

#: Always enforced (spec §6.3).
ENFORCED_POLICY = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"

#: The script policy: a ``str.format`` template with one ``{nonce}`` field. Enforced
#: since Phase 2 despite the name. ``form-action`` admits Slack because both
#: provisioning forms answer with a 302 to Slack's OAuth URL, and Chromium applies
#: ``form-action`` to a form submission's redirect.
REPORT_ONLY_POLICY = (
    "default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; form-action 'self' https://slack.com https://*.slack.com; report-uri " + CSP_REPORT_PATH
)

#: True since Phase 2 (spec §7): the script policy is enforced together with
#: ENFORCED_POLICY in one Content-Security-Policy header, `report-uri` kept.
SCRIPT_POLICY_ENFORCED = True

_NONCE_BYTES = 16


def new_nonce() -> str:
    """16 random bytes, URL-safe base64 (22 characters): valid in a CSP nonce-source
    and in an HTML attribute without escaping."""
    return secrets.token_urlsafe(_NONCE_BYTES)


def security_header_items(
    nonce: str, *, enforce_script_policy: bool = SCRIPT_POLICY_ENFORCED
) -> list[tuple[str, str]]:
    """The headers every response carries, for one request's nonce.

    ``enforce_script_policy=False`` is the Phase 1 split (script policy report-only),
    kept as the rollback switch.
    """
    script_policy = REPORT_ONLY_POLICY.format(nonce=nonce)
    items = [
        ("X-Frame-Options", "DENY"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ]
    if enforce_script_policy:
        items.append(("Content-Security-Policy", f"{ENFORCED_POLICY}; {script_policy}"))
    else:
        items.append(("Content-Security-Policy", ENFORCED_POLICY))
        items.append(("Content-Security-Policy-Report-Only", script_policy))
    return items


class SecurityHeadersMiddleware:
    """Sets ``request.state.csp_nonce`` and the headers of ``security_header_items``.

    A header a route already set under one of these names is replaced, so these are
    the only policies the app sends. Non-HTTP scopes pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = new_nonce()
        scope.setdefault("state", {})["csp_nonce"] = nonce
        items = security_header_items(nonce)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in items:
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
