"""parse_retry_after: bounded, type-safe Retry-After header parsing.

Ported from src/services/slack_web.py's `_call` (float + try/except + cap), which already gets this
right for the web-layer Slack client. src/agent/slack_client.py's engine-side `_call_with_retry` used a
raw `int(header)` that raised ValueError for an HTTP-date, a float-string, or a negative value — from
inside an `except SlackApiError:` block, so the ValueError escaped as an unrelated exception type
(issue #23 V7e). This module gives both that call site and Part 24's provisioning-loop cap one place to
get it right.
"""

import pytest

from src.agent.retry_after import parse_retry_after


@pytest.mark.parametrize("value,expected", [
    ("17", 17.0),
    ("0", 0.0),
    ("2.5", 2.5),                    # a legal delta-seconds header some proxies emit as a float
    ("-5", 0.0),                     # negative clamps to 0, never raises
    ("99999999", 30.0),              # huge value is capped
    ("garbage", 5.0),                # unparseable falls back to default
    (None, 5.0),                     # missing header falls back to default
])
def test_parse_retry_after_table(value, expected):
    assert parse_retry_after(value, default=5.0, cap=30.0) == expected


def test_an_http_date_in_the_past_clamps_to_zero_not_negative():
    # RFC 7231 permits an HTTP-date; a date already in the past yields a
    # negative delta, which must clamp to 0, not raise and not go negative.
    assert parse_retry_after(
        "Wed, 21 Oct 2015 07:28:00 GMT", default=5.0, cap=30.0
    ) == 0.0


def test_an_http_date_in_the_future_is_honoured_and_capped():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=10))
    got = parse_retry_after(soon, default=5.0, cap=30.0)
    assert 8.0 <= got <= 10.0  # allow for test execution slop

    far = format_datetime(datetime.now(UTC) + timedelta(hours=1))
    assert parse_retry_after(far, default=5.0, cap=30.0) == 30.0


def test_never_raises_for_any_of_these_inputs():
    for value in ("17", "-5", "99999999", "2.5", "garbage", None,
                  "Wed, 21 Oct 2015 07:28:00 GMT", ""):
        parse_retry_after(value, default=5.0, cap=30.0)  # must not raise
