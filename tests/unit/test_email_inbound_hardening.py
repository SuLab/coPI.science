"""Hardening for inbound email reply processing.

These pin the defects found while investigating the dead prod reply flow
(2026-08-11): a sender-forged ``Authentication-Results: ... pass`` header
defeated the SEC-5 anti-spoofing gate, HTML-only replies were silently
dropped, auto-responders could loop with the help email, the declared
per-token rate limit was never enforced, and a poison message in the inbound
bucket was retried forever.
"""

import email

import pytest

import src.services.email_inbound as inbound
from src.services.email_inbound import (
    MAX_REPLIES_PER_TOKEN_PER_HOUR,
    _authentication_results_ok,
    _extract_reply_body,
    _reply_rate_ok,
    poll_inbound_emails,
    process_inbound_email,
)


def _msg(raw: str) -> email.message.Message:
    return email.message_from_string(raw)


# --- Authentication-Results: only SES's own (topmost) header is trusted -----


def test_forged_pass_header_below_ses_fail_is_rejected():
    """SES prepends its header on receipt, so a sender-supplied pass sits below
    it. Merging verdicts across headers let the forged pass win (SEC-5)."""
    raw = (
        "Authentication-Results: amazonses.com; spf=fail smtp.mailfrom=evil.com; "
        "dkim=none; dmarc=fail header.from=scripps.edu\n"
        "Authentication-Results: amazonses.com; spf=pass; dkim=pass; dmarc=pass\n"
        "From: pi@scripps.edu\n\nbody"
    )
    assert _authentication_results_ok(_msg(raw)) is False


def test_verdicts_below_the_topmost_header_are_ignored_entirely():
    raw = (
        "Authentication-Results: amazonses.com; spf=pass smtp.mailfrom=scripps.edu; "
        "dkim=pass; dmarc=pass header.from=scripps.edu\n"
        "Authentication-Results: evil.example; spf=fail; dkim=fail; dmarc=fail\n"
        "From: pi@scripps.edu\n\nbody"
    )
    assert _authentication_results_ok(_msg(raw)) is True


def test_topmost_header_with_foreign_authserv_id_is_rejected():
    """Everything on our receipt path is stamped by amazonses.com; anything
    else means the message did not transit SES receiving."""
    raw = (
        "Authentication-Results: mx.evil.example; spf=pass; dkim=pass; dmarc=pass\n"
        "From: pi@scripps.edu\n\nbody"
    )
    assert _authentication_results_ok(_msg(raw)) is False


# --- HTML-only replies are not silently dropped ------------------------------


def test_html_only_reply_body_falls_back_to_stripped_html():
    raw = (
        "From: pi@scripps.edu\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/alternative; boundary="xyz"\n'
        "\n"
        "--xyz\n"
        'Content-Type: text/html; charset="UTF-8"\n'
        "\n"
        "<div dir=\"ltr\">4 &mdash; excellent, go ahead!<br></div>\n"
        '<div class="gmail_quote"><blockquote>quoted proposal text '
        "1 = Not a good idea</blockquote></div>\n"
        "\n"
        "--xyz--\n"
    )
    body = _extract_reply_body(_msg(raw))
    assert "4" in body and "excellent" in body
    assert "Not a good idea" not in body  # quoted HTML must not leak through


def test_singlepart_html_reply_body_is_extracted():
    raw = (
        "From: pi@scripps.edu\n"
        'Content-Type: text/html; charset="UTF-8"\n'
        "\n"
        "<p>2 &amp; please focus on assay development</p>\n"
    )
    body = _extract_reply_body(_msg(raw))
    assert "2 & please focus on assay development" in body


def test_plain_text_part_still_wins_over_html():
    raw = (
        "From: pi@scripps.edu\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/alternative; boundary="qq"\n'
        "\n"
        "--qq\n"
        'Content-Type: text/plain; charset="UTF-8"\n'
        "\n"
        "3 sounds great\n"
        "\n"
        "--qq\n"
        'Content-Type: text/html; charset="UTF-8"\n'
        "\n"
        "<div>3 sounds great</div>\n"
        "\n"
        "--qq--\n"
    )
    assert _extract_reply_body(_msg(raw)) == "3 sounds great"


# --- Auto-submitted mail is dropped before any processing --------------------


_SES_PASS = "Authentication-Results: amazonses.com; spf=pass; dkim=pass; dmarc=pass\n"


async def test_auto_submitted_reply_is_ignored_before_touching_the_db():
    """RFC 3834: an OOO auto-reply answering our help email must not trigger
    another help email (mail loop). db=None proves the early return."""
    raw = (
        _SES_PASS
        + "Auto-Submitted: auto-replied\n"
        "From: pi@scripps.edu\n"
        "To: review+sometoken@reply.copi.science\n"
        "\n"
        "I am out of the office.\n"
    ).encode()
    await process_inbound_email(raw, db=None)  # must not raise


async def test_auto_submitted_no_is_not_treated_as_an_auto_reply():
    """``Auto-Submitted: no`` explicitly marks human-generated mail; it must
    proceed into normal processing (here: to the token lookup, which needs a
    db — the AttributeError on db=None is the evidence it got past the gate)."""
    raw = (
        _SES_PASS
        + "Auto-Submitted: no\n"
        "From: pi@scripps.edu\n"
        "To: review+sometoken@reply.copi.science\n"
        "\n"
        "3 great idea\n"
    ).encode()
    with pytest.raises(AttributeError):
        await process_inbound_email(raw, db=None)


# --- The declared per-token rate limit is enforced ---------------------------


def test_reply_rate_limit_blocks_the_11th_reply_in_an_hour(monkeypatch):
    monkeypatch.setattr(inbound, "_RECENT_REPLY_TIMES", {})
    token = "tok-" + "x" * 60
    base = 1_000_000.0
    for i in range(MAX_REPLIES_PER_TOKEN_PER_HOUR):
        assert _reply_rate_ok(token, now=base + i) is True
    assert _reply_rate_ok(token, now=base + 60) is False


def test_reply_rate_limit_window_slides(monkeypatch):
    monkeypatch.setattr(inbound, "_RECENT_REPLY_TIMES", {})
    token = "tok-" + "y" * 60
    base = 2_000_000.0
    for i in range(MAX_REPLIES_PER_TOKEN_PER_HOUR):
        assert _reply_rate_ok(token, now=base + i) is True
    # An hour later the old entries have aged out.
    assert _reply_rate_ok(token, now=base + 3601) is True


# --- Poison messages are quarantined, not retried forever --------------------


class _FakeS3:
    """Just enough of the S3 client for poll_inbound_emails."""

    def __init__(self, keys):
        self.objects = {k: b"raw email bytes" for k in keys}
        self.copied: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        return {
            "Contents": [{"Key": k} for k in sorted(self.objects)],
            "KeyCount": len(self.objects),
        }

    def get_object(self, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def copy_object(self, Bucket, CopySource, Key):
        self.copied.append((CopySource["Key"], Key))
        self.objects[Key] = self.objects[CopySource["Key"]]

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


class _NullSessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass


async def test_poison_email_is_quarantined_after_repeated_failures(monkeypatch):
    fake = _FakeS3(["inbound/poison"])
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    monkeypatch.setattr(inbound, "_S3_FAILURE_COUNTS", {})

    async def _boom(raw, db):
        raise RuntimeError("unparseable in a way that always raises")

    monkeypatch.setattr(inbound, "process_inbound_email", _boom)

    for _ in range(inbound.MAX_S3_PROCESS_ATTEMPTS):
        assert await poll_inbound_emails(_NullSessionFactory()) == 0

    assert fake.copied == [("inbound/poison", "failed/poison")]
    assert fake.deleted == ["inbound/poison"]
    # Quarantined: the next poll sees only failed/ (outside the prefix filter
    # in real S3; the fake returns everything, so assert the key is gone).
    assert "inbound/poison" not in fake.objects


async def test_a_transient_failure_is_retried_not_quarantined(monkeypatch):
    fake = _FakeS3(["inbound/flaky"])
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    monkeypatch.setattr(inbound, "_S3_FAILURE_COUNTS", {})

    async def _boom(raw, db):
        raise RuntimeError("db briefly down")

    monkeypatch.setattr(inbound, "process_inbound_email", _boom)
    await poll_inbound_emails(_NullSessionFactory())

    assert fake.copied == []
    assert "inbound/flaky" in fake.objects  # still there for the next poll
