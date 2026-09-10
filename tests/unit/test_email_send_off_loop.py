"""S-7 (audit 2026-09-10): synchronous boto3 SES sends must run off the event
loop, through src.services.io_executor.run_blocking, from every async call
site in src/services/email_inbound.py and src/services/email_notifications.py.

No DB/Docker needed: these exercise pure functions with monkeypatched
send helpers, asserting on the calling thread's identity -- the same
"actually moved off the loop" proof pattern as
tests/unit/test_slack_executor.py.
"""

import threading
from types import SimpleNamespace

import src.services.email_inbound as email_inbound
import src.services.email_notifications as email_notifications


def _record_thread(sink: dict, return_value=True):
    def _fake(*_args, **_kwargs):
        sink["ident"] = threading.get_ident()
        sink["name"] = threading.current_thread().name
        return return_value
    return _fake


async def test_notify_reply_expired_sends_off_the_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict = {}
    monkeypatch.setattr(email_inbound, "_send_simple_email", _record_thread(seen))

    await email_inbound._notify_reply_expired("pi@scripps.edu", "notif-1")

    assert seen["ident"] != loop_thread
    assert seen["name"].startswith("io-blocking")


async def test_notify_instruction_failure_sends_off_the_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict = {}
    monkeypatch.setattr(email_inbound, "_send_simple_email", _record_thread(seen))

    await email_inbound._notify_instruction_failure(
        "pi@scripps.edu", "SuBot", "notif-2", will_retry=True
    )

    assert seen["ident"] != loop_thread
    assert seen["name"].startswith("io-blocking")


async def test_send_help_email_sends_off_the_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict = {}
    monkeypatch.setattr(email_inbound, "_send_simple_email", _record_thread(seen))
    monkeypatch.setattr(email_inbound, "build_reply_address", lambda token: f"review+{token}@x")

    user = SimpleNamespace(email="pi@scripps.edu")
    notification = SimpleNamespace(reply_token="tok")
    await email_inbound._send_help_email(user, notification)

    assert seen["ident"] != loop_thread
    assert seen["name"].startswith("io-blocking")


async def test_maybe_send_stale_token_bounce_sends_off_the_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict = {}

    def _fake_outcome(*_a, **_kw):
        seen["ident"] = threading.get_ident()
        seen["name"] = threading.current_thread().name
        return email_notifications.SendOutcome.SENT

    monkeypatch.setattr(email_inbound, "send_html_email_outcome", _fake_outcome)
    monkeypatch.setattr(
        "src.services.email.is_allowed_recipient", lambda addr: True,
    )

    await email_inbound._maybe_send_stale_token_bounce("pi@scripps.edu")

    assert seen["ident"] != loop_thread
    assert seen["name"].startswith("io-blocking")


async def test_send_paused_email_sends_off_the_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict = {}
    monkeypatch.setattr(email_notifications, "_send_html_email", _record_thread(seen))

    user = SimpleNamespace(email="pi@scripps.edu", id="u1")
    await email_notifications._send_paused_email(user)

    assert seen["ident"] != loop_thread
    assert seen["name"].startswith("io-blocking")
