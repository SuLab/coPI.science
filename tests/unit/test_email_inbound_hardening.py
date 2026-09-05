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
from sqlalchemy import select

import src.services.email_inbound as inbound
from src.models import AgentChannel, EmailNotification
from src.services.email_inbound import (
    MAX_REPLIES_PER_TOKEN_PER_HOUR,
    _authentication_results_ok,
    _extract_reply_body,
    _reply_rate_ok,
    poll_inbound_emails,
    process_inbound_email,
)
from tests import factories
from tests.factories import SES_PASS_HEADER
from tests.fakes import FakeSlackClient


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


def test_unknown_declared_charset_falls_back_instead_of_raising():
    raw = (
        "From: pi@scripps.edu\n"
        'Content-Type: text/plain; charset="unknown-8bit"\n'
        "\n"
        "3 sounds great\n"
    )
    body = _extract_reply_body(_msg(raw))
    assert "3 sounds great" in body


def test_a_garbled_but_known_charset_still_decodes_lossily():
    """Control: a KNOWN charset with genuinely undecodable bytes must still use
    errors='replace' rather than raising — this pins that the fix didn't remove the
    existing fallback for the case it always handled correctly."""
    raw_bytes = (
        b"From: pi@scripps.edu\n"
        b'Content-Type: text/plain; charset="utf-8"\n'
        b"\n"
        b"3 sounds great \xff\xfe garbled\n"
    )
    body = _extract_reply_body(email.message_from_bytes(raw_bytes))
    assert "3 sounds great" in body


def test_a_bytes_to_bytes_codec_charset_falls_back_to_utf8():
    """`base64` is a real codec name, but a bytes-to-bytes one: `.decode("base64")`
    raises `LookupError: ... not a text encoding`, not the plain "unknown encoding"
    LookupError an unrecognized name raises. The guarded decode in _decode_part must
    catch this shape too, not just codecs.lookup's original probe case."""
    raw = (
        b"From: pi@scripps.edu\n"
        b'Content-Type: text/plain; charset="base64"\n'
        b"\n"
        b"3 sounds great\n"
    )
    body = inbound._decode_part(email.message_from_bytes(raw))
    assert "3 sounds great" in body


def test_a_nul_containing_charset_falls_back_to_utf8():
    """A charset value with an embedded NUL raises ValueError from .decode() (not
    LookupError) — the guarded decode in _decode_part must catch both."""
    raw = b'From: pi@scripps.edu\nContent-Type: text/plain; charset="utf-8\x00"\n\n3 sounds great\n'
    body = inbound._decode_part(email.message_from_bytes(raw))
    assert "3 sounds great" in body


# --- Auto-submitted mail is dropped before any processing --------------------


async def test_auto_submitted_reply_is_ignored_before_touching_the_db():
    """RFC 3834: an OOO auto-reply answering our help email must not trigger
    another help email (mail loop). db=None proves the early return."""
    raw = (
        SES_PASS_HEADER
        + "Auto-Submitted: auto-replied\n"
        "From: pi@scripps.edu\n"
        "To: review+sometoken@reply.copi.science\n"
        "\n"
        "I am out of the office.\n"
    ).encode()
    await process_inbound_email(raw, db=None)  # must not raise


async def test_precedence_bulk_autoresponder_is_ignored_before_touching_the_db():
    """Ticketing systems and older Exchange mark auto-replies with legacy
    headers (Precedence: bulk/junk/list, X-Autoreply, X-Auto-Response-Suppress)
    instead of Auto-Submitted. Now that the help email is reply-able (it
    carries a token Reply-To), missing these re-opens the mail loop that the
    Auto-Submitted check exists to prevent."""
    raw = (
        SES_PASS_HEADER
        + "Precedence: bulk\n"
        "From: pi@scripps.edu\n"
        "To: review+sometoken@reply.copi.science\n"
        "\n"
        "Your message has been received.\n"
    ).encode()
    await process_inbound_email(raw, db=None)  # must not raise


def test_legacy_auto_reply_markers_are_detected():
    for headers in (
        "Precedence: bulk\n",
        "Precedence: junk\n",
        "Precedence: list\n",
        "Precedence: auto_reply\n",
        "X-Autoreply: yes\n",
        "X-Autorespond: OOO\n",
        "X-Auto-Response-Suppress: All\n",
    ):
        msg = _msg(headers + "From: pi@scripps.edu\n\nbody")
        assert inbound._is_auto_submitted(msg) is True, headers


def test_ordinary_reply_headers_are_not_flagged_as_auto():
    msg = _msg(
        "Precedence: first-class\nFrom: pi@scripps.edu\n\n3 great idea"
    )
    assert inbound._is_auto_submitted(msg) is False


async def test_auto_submitted_no_is_not_treated_as_an_auto_reply():
    """``Auto-Submitted: no`` explicitly marks human-generated mail; it must
    proceed into normal processing (here: to the token lookup, which needs a
    db — the AttributeError on db=None is the evidence it got past the gate)."""
    raw = (
        SES_PASS_HEADER
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
        self.list_calls: list[dict] = []

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        """Real pagination by key order, so a >MaxKeys backlog exercises more than
        one page — the pre-fix caller never passed ContinuationToken at all, so
        this stays 100% backward compatible with every existing single-page test."""
        self.list_calls.append({"ContinuationToken": ContinuationToken})
        all_keys = sorted(self.objects)
        start = int(ContinuationToken) if ContinuationToken else 0
        page = all_keys[start : start + MaxKeys]
        end = start + len(page)
        result = {"Contents": [{"Key": k} for k in page], "KeyCount": len(page)}
        if end < len(all_keys):
            result["IsTruncated"] = True
            result["NextContinuationToken"] = str(end)
        else:
            result["IsTruncated"] = False
        return result

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


async def test_list_objects_v2_is_paginated_across_multiple_pages(monkeypatch):
    keys = [f"inbound/msg-{i:03d}" for i in range(120)]
    fake = _FakeS3(keys)
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    monkeypatch.setattr(inbound, "_S3_FAILURE_COUNTS", {})

    processed: list[bytes] = []

    async def _record(raw, db):
        processed.append(raw)

    monkeypatch.setattr(inbound, "process_inbound_email", _record)
    count = await poll_inbound_emails(_NullSessionFactory())

    assert count == 120, (
        f"only {count}/120 objects were processed — the listing stopped after the first page"
    )
    assert len(fake.deleted) == 120
    assert len(fake.list_calls) >= 3, (
        f"expected at least 3 pages of 50 for 120 keys, got {len(fake.list_calls)} list_objects_v2 calls"
    )


class _StuckCursorS3:
    """Always claims IsTruncated with the SAME NextContinuationToken — a buggy or
    misbehaving S3-compatible endpoint. Without a cursor-repeat guard, poll_inbound_emails
    would fetch this same page _MAX_S3_LIST_PAGES times."""

    def __init__(self):
        self.calls = 0

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        self.calls += 1
        return {
            "Contents": [{"Key": "inbound/msg-stuck"}],
            "IsTruncated": True,
            "NextContinuationToken": "stuck-token",
        }

    def get_object(self, Bucket, Key):
        import io

        return {"Body": io.BytesIO(b"raw email bytes")}

    def delete_object(self, Bucket, Key):
        pass


async def test_a_repeated_continuation_token_stops_pagination(monkeypatch):
    fake = _StuckCursorS3()
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    monkeypatch.setattr(inbound, "_S3_FAILURE_COUNTS", {})

    async def _record(raw, db):
        pass

    monkeypatch.setattr(inbound, "process_inbound_email", _record)
    count = await poll_inbound_emails(_NullSessionFactory())

    assert fake.calls == 2, (
        "pagination must stop as soon as the continuation token repeats, not loop "
        "up to _MAX_S3_LIST_PAGES times"
    )
    assert count == 1, "the same key fetched across the two (pre-stop) pages must be processed only once"


async def test_duplicate_keys_across_pages_are_processed_once(monkeypatch):
    """De-duplicated independently of the cursor-repeat guard above: S3 listing
    consistency can hand back the same key on two different (genuinely advancing)
    pages — each key must still be processed exactly once."""
    pages = [
        {"Contents": [{"Key": "inbound/msg-a"}, {"Key": "inbound/msg-b"}],
         "IsTruncated": True, "NextContinuationToken": "page2"},
        {"Contents": [{"Key": "inbound/msg-b"}, {"Key": "inbound/msg-c"}],
         "IsTruncated": False},
    ]
    seen_calls: list[dict] = []

    class _OverlapS3:
        def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
            seen_calls.append({"ContinuationToken": ContinuationToken})
            return pages[len(seen_calls) - 1]

        def get_object(self, Bucket, Key):
            import io

            return {"Body": io.BytesIO(b"raw email bytes")}

        def delete_object(self, Bucket, Key):
            pass

    fake = _OverlapS3()
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    monkeypatch.setattr(inbound, "_S3_FAILURE_COUNTS", {})

    processed: list[bytes] = []

    async def _record(raw, db):
        processed.append(raw)

    monkeypatch.setattr(inbound, "process_inbound_email", _record)
    count = await poll_inbound_emails(_NullSessionFactory())

    assert len(seen_calls) == 2, "both pages must still be fetched — dedup happens after listing"
    assert count == 3, "msg-a, msg-b (once, not twice), msg-c — 3 distinct keys"


# --- The LLM-classified rating is coerced to int, rejecting bool ------------


class TestCoerceRating:
    def test_accepts_a_numeric_string(self):
        assert inbound._coerce_rating("3") == 3

    def test_accepts_a_numeric_string_with_surrounding_space(self):
        assert inbound._coerce_rating(" 3 ") == 3

    def test_accepts_a_whole_float(self):
        assert inbound._coerce_rating(3.0) == 3

    def test_accepts_a_whole_float_as_a_string(self):
        assert inbound._coerce_rating("3.0") == 3

    def test_accepts_a_real_int(self):
        assert inbound._coerce_rating(3) == 3

    def test_rejects_bool_even_though_bool_is_an_int_subclass(self):
        assert inbound._coerce_rating(True) is None
        assert inbound._coerce_rating(False) is None

    def test_rejects_a_fractional_value(self):
        assert inbound._coerce_rating(2.5) is None

    def test_rejects_unparseable_strings_and_none(self):
        assert inbound._coerce_rating("abc") is None
        assert inbound._coerce_rating(None) is None

    def test_passes_through_an_out_of_range_int_unchanged(self):
        """_coerce_rating only type-coerces; range validation is process_inbound_email's
        job (see test_an_out_of_range_rating_falls_back_to_the_help_email_path in
        test_email_inbound_reply_paths.py, which pins what happens to it downstream)."""
        assert inbound._coerce_rating(7) == 7


async def test_classify_reply_coerces_a_string_rating_to_int(monkeypatch):
    """End-to-end through classify_reply: a model that returns the rating as a numeric
    string ('3') must come back as int 3, not a string process_inbound_email's own
    `rating < 1 or rating > 4` guard would TypeError on."""
    from tests.fakes import FakeAnthropic, text_response

    fake = FakeAnthropic(responses=[
        text_response('{"category": "review", "rating": "3", "comment": "", "instruction": ""}')
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    result = await inbound.classify_reply("3 sounds great", "A proposal.")

    assert result["rating"] == 3
    assert isinstance(result["rating"], int) and not isinstance(result["rating"], bool)


# --- The retry/terminal split follows the Slack mutation, not the exception ---


class _UnhappySlackClient(FakeSlackClient):
    """Raises one identical exception at a configurable point in the migration.

    ``connect()`` is reached by ``_make_client`` before any Slack write; the first
    ``invite_to_channel`` happens immediately after ``conversations.create`` has
    returned a real private channel. Same type, same message, opposite sides of the
    migration's point of no return.
    """

    fail_at = "before"

    def connect(self) -> bool:
        if self.fail_at == "before":
            raise RuntimeError("slack is unhappy")
        return True

    def invite_to_channel(self, channel_id, user_ids):
        if self.fail_at == "after":
            raise RuntimeError("slack is unhappy")
        return super().invite_to_channel(channel_id, user_ids)


async def _instruction_world(db, *, email_addr, token):
    owner = await factories.make_user(db)
    recipient = await factories.make_user(db, email=email_addr)
    agent = await factories.make_agent(db, user=owner)
    td = await factories.make_thread_decision(
        db, agent_a=agent.agent_id, summary_text="A proposal to collaborate."
    )
    notification = EmailNotification(
        user_id=recipient.id,
        thread_decision_id=td.id,
        agent_registry_id=agent.id,
        reply_token=token,
        category="proposal_review",
        status="sent",
    )
    db.add(notification)
    await db.flush()
    return recipient, td, notification


@pytest.mark.integration
@pytest.mark.parametrize("fail_at,retried", [("before", True), ("after", False)])
async def test_the_retry_split_follows_the_slack_mutation_not_the_exception_type(
    db_session, monkeypatch, fail_at, retried,
):
    """#21 COR-32: what makes a failure terminal is that a Slack channel now exists.

    Both halves raise the *same* ``RuntimeError("slack is unhappy")`` out of
    ``migrate_public_thread_to_private``; the only difference is where the migration
    was when it happened. So nothing about the exception can be used to route it, and
    a handler that routes on type (or that treats "the migration raised" as terminal
    full stop, which is the pre-fix behaviour) gets exactly one of the two wrong.

    The fact the split is made on is asserted directly: ``created_channels`` is 0 on
    the retried side and 1 on the terminal side.
    """
    inbound._INSTRUCTION_FAILURE_EMAILS_SENT.clear()
    addr = f"pi.boundary.{fail_at}@scripps.edu"
    recipient, td, notification = await _instruction_world(
        db_session, email_addr=addr, token=f"boundary{fail_at}".ljust(48, "b")[:48]
    )
    await db_session.commit()
    run_id = td.simulation_run_id

    mails: list[dict] = []

    def _record(to_email, subject, text_body, reply_to=None):
        mails.append({"to": to_email, "subject": subject, "body": text_body})
        return True

    monkeypatch.setattr(inbound, "_send_simple_email", _record)

    made: list[_UnhappySlackClient] = []

    async def _on(*a, **k):
        return True

    async def _token(db, agent_id):
        return f"xoxb-fake-{agent_id}"

    def _build(agent_id, bot_token):
        client = _UnhappySlackClient(agent_id=agent_id, bot_token=bot_token)
        client.fail_at = fail_at
        made.append(client)
        return client

    monkeypatch.setattr("src.services.private_channels._slack_enabled_for_migration", _on)
    monkeypatch.setattr("src.services.private_channels._get_or_fail_bot_token", _token)
    monkeypatch.setattr("src.services.private_channels.AgentSlackClient", _build)

    if retried:
        with pytest.raises(inbound.InstructionApplyFailed):
            await inbound._handle_instruction(
                user=recipient, notification=notification, td=td,
                instruction="focus on X", db=db_session,
            )
    else:
        assert await inbound._handle_instruction(
            user=recipient, notification=notification, td=td,
            instruction="focus on X", db=db_session,
        ) is False

    created = sum(len(c.created_channels) for c in made)
    assert created == (0 if retried else 1), (
        "the test's own premise: 'before' must not have created a channel, "
        "'after' must have"
    )
    channels = (await db_session.execute(
        select(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
    )).scalars().all()
    assert channels == [], "neither half commits an AgentChannel row"
    (mail,) = mails
    assert ("will not be retried" in mail["body"]) is not retried
    assert ("We'll retry automatically" in mail["body"]) is retried
