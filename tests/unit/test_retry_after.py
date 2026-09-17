"""parse_retry_after: bounded, type-safe Retry-After header parsing.

Ported from src/services/slack_web.py's `_call` (float + try/except + cap), which already gets this
right for the web-layer Slack client. src/agent/slack_client.py's engine-side `_call_with_retry` used a
raw `int(header)` that raised ValueError for an HTTP-date, a float-string, or a negative value — from
inside an `except SlackApiError:` block, so the ValueError escaped as an unrelated exception type.
This module gives both that call site and the provisioning-loop cap one place to
get it right.
"""

import pytest

from src.agent.retry_after import parse_retry_after


@pytest.mark.parametrize("value,expected", [
    ("17", 17.0),
    ("2.5", 2.5),                    # a legal delta-seconds header some proxies emit as a float
    ("99999999", 30.0),              # huge value is capped
    ("garbage", 5.0),                # unparseable falls back to default
    (None, 5.0),                     # missing header falls back to default
    ("0", 5.0),                      # zero is not a positive number -> default, not a hot retry
    ("-5", 5.0),                     # negative falls back to default, not 0.0
    ("nan", 5.0),                    # NaN is not a finite number -> default
    ("inf", 5.0),                    # +inf is not a *finite* number -> default (still ends up capped)
    ("-inf", 5.0),                   # -inf falls back to default, not a hot retry
    ("1e400", 5.0),                  # overflows to +inf -> default, same end result as before
])
def test_parse_retry_after_table(value, expected):
    assert parse_retry_after(value, default=5.0, cap=30.0) == expected


def test_an_http_date_in_the_past_falls_back_to_default_not_zero():
    # RFC 7231 permits an HTTP-date; a date already in the past yields a negative
    # delta. #24 Minor 2: that must fall back to `default` (not clamp to 0), or a
    # rate-limited caller retries immediately against the API that just 429'd it.
    assert parse_retry_after(
        "Wed, 21 Oct 2015 07:28:00 GMT", default=5.0, cap=30.0
    ) == 5.0


def test_an_http_date_in_the_future_is_honoured_and_capped():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=10))
    got = parse_retry_after(soon, default=5.0, cap=30.0)
    assert 8.0 <= got <= 10.0  # allow for test execution slop

    far = format_datetime(datetime.now(UTC) + timedelta(hours=1))
    assert parse_retry_after(far, default=5.0, cap=30.0) == 30.0


def test_a_list_or_dict_value_does_not_raise_and_falls_back_to_default():
    # #24 Minor 1: float() raises TypeError for a list/dict, and the pre-fix
    # fallback then called parsedate_to_datetime on the same non-str value,
    # raising AttributeError -- uncaught, since only (TypeError, ValueError,
    # IndexError) were handled. Coercing to str up front routes both through
    # the ordinary "unparseable" -> default path instead.
    assert parse_retry_after(["Retry-After"], default=5.0, cap=30.0) == 5.0
    assert parse_retry_after({"seconds": 5}, default=5.0, cap=30.0) == 5.0


def test_never_raises_for_any_of_these_inputs():
    for value in ("17", "-5", "99999999", "2.5", "garbage", None,
                  "Wed, 21 Oct 2015 07:28:00 GMT", "", "nan", "-inf",
                  ["Retry-After"], {"seconds": 5}):
        parse_retry_after(value, default=5.0, cap=30.0)  # must not raise
