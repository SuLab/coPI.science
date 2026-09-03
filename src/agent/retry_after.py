"""Parse a Slack ``Retry-After`` header into a bounded sleep duration.

Slack's Retry-After is documented as an integer count of seconds, but real responses have been observed
as an RFC 7231 HTTP-date, a float count of seconds ("2.5"), and either of those with the wrong sign. A raw
``int(header)`` (or ``float(header)``) therefore raises ``ValueError`` for the date/negative forms — and
because that raise can happen inside a caller's ``except SlackApiError:`` block, it escapes as an unrelated
exception type instead of a retryable one (issue #23 V7e). This module gives every Retry-After call site
(``src/agent/slack_client.py``; Part 24's provisioning-loop cap) one place to get it right: parse a
delta-seconds float, or an HTTP-date and compute the delta from ``now``, clamp to ``[0, cap]``, and fall
back to ``default`` when nothing parses. Ported from the equivalent inline logic in
``src/services/slack_web.py:_call`` (``:104-120``), which already does this correctly for the web-layer
Slack client — that call site is left as-is (YAGNI; it already works).
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_retry_after(value: str | None, default: float, cap: float = 30.0) -> float:
    """Return a sleep duration in seconds, always within ``[0, cap]``.

    ``value`` is the raw ``Retry-After`` header text (or ``None``). Tries, in order: a bare float (covers
    Slack's documented integer-seconds form and the float variant some proxies emit), then an RFC 7231
    HTTP-date (delta from ``now``). Anything that parses as neither falls back to ``default``. The result
    is always clamped into ``[0, cap]`` so a malformed or hostile header can never make a caller sleep
    longer than ``cap``, and a negative or past-dated value can never reach ``time.sleep`` as a negative
    number (which itself raises ``ValueError``).
    """
    if value is None:
        seconds = default
    else:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                seconds = (when - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, IndexError):
                seconds = default
    return max(0.0, min(seconds, cap))
