"""Unit-level coverage for the three email_notifications.py sweep loops' per-item error handling.
No DB: session_factory is a small fake that records commit()/rollback() calls in order, standing
in for "a second user's failure must not be able to discard an earlier user's already-committed
work" without needing real Postgres to reproduce the shared-session hazard.

Fix round 1 (#21 V4-1 review Critical #1): each sweep now captures only plain ids before the
loop and re-loads the row inside the guarded block (src/worker/main.py's reap_stale_jobs
pattern), rather than reading an attribute off the object the bulk query returned. These fakes'
`execute()` therefore has to answer TWO different query shapes in order: the initial bulk
SELECT (`.scalars().all()`), then one per-item reload (`.scalar_one_or_none()`) for each id in
turn -- a fake has no SQL to introspect, so it distinguishes purely by call order, which is
safe here because both queries process items in the same fixed order this file provides.
"""

import logging

import pytest

import src.services.email_notifications as en
from src.config import get_settings


class _FakeSessionFactory:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_an_earlier_users_committed_row_survives_a_later_users_failure(monkeypatch):
    """V4-1's actual harm: the sweep shares one session, so a rollback triggered by user 2
    must not discard user 1's notification row — user 1's email has already been sent and
    without a durable row the next cycle re-sends it with a fresh token."""
    events: list[str] = []

    class _U:
        def __init__(self, i):
            self.id = i

    class _Sess:
        def __init__(self):
            self._users = [_U("u1"), _U("u2")]
            self._calls = 0

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

        async def execute(self, *a, **k):
            call = self._calls
            self._calls += 1
            if call == 0:
                users = self._users

                class _Bulk:
                    def scalars(s):
                        return s

                    def all(s):
                        return users

                return _Bulk()

            user = self._users[call - 1]

            class _Reload:
                def scalar_one_or_none(s):
                    return user

            return _Reload()

    seen = []

    async def _per_user(user, db):
        seen.append(user.id)
        if user.id == "u2":
            raise RuntimeError("poisoned flush")
        return True

    monkeypatch.setattr(en, "_process_user_notifications", _per_user)
    sess = _Sess()
    await en.check_and_send_notifications(_FakeSessionFactory(sess))

    assert seen == ["u1", "u2"]
    assert events[:2] == ["commit", "rollback"], (
        "user 1's work was not committed before user 2's failure rolled the shared session "
        f"back — u1 got an email with no durable row. events={events}"
    )


@pytest.mark.asyncio
async def test_check_and_send_status_overviews_commits_an_earlier_users_send_before_a_later_failure(
    monkeypatch,
):
    """Same shared-session hazard (M4), in the status_overview sweep."""
    events: list[str] = []

    class _U:
        def __init__(self, i):
            self.id = i

    class _Pref:
        enabled = True
        frequency = "daily"
        last_sent_at = None

    class _Sess:
        def __init__(self):
            self._users = [_U("u1"), _U("u2")]
            self._calls = 0

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

        async def execute(self, *a, **k):
            call = self._calls
            self._calls += 1
            if call == 0:
                users = self._users

                class _Bulk:
                    def scalars(s):
                        return s

                    def all(s):
                        return users

                return _Bulk()

            user = self._users[call - 1]

            class _Reload:
                def scalar_one_or_none(s):
                    return user

            return _Reload()

    async def _fake_pref(user_id, category, db):
        return _Pref()

    seen = []

    async def _send(user, pref, db):
        seen.append(user.id)
        if user.id == "u2":
            raise RuntimeError("poisoned flush")
        return True

    monkeypatch.setattr(en, "get_or_create_pref", _fake_pref)
    monkeypatch.setattr(en, "_is_time_to_send", lambda *a, **k: True)
    monkeypatch.setattr(en, "_send_status_overview", _send)
    sess = _Sess()
    await en.check_and_send_status_overviews(_FakeSessionFactory(sess))

    assert seen == ["u1", "u2"]
    assert events[:2] == ["commit", "rollback"], (
        "user 1's status overview was not committed before user 2's failure rolled the shared "
        f"session back — u1 got an email with no durable record. events={events}"
    )


@pytest.mark.asyncio
async def test_check_and_send_new_proposal_emails_commits_an_earlier_agents_send_before_a_later_failure(
    monkeypatch,
):
    """Same shared-session hazard (M4), in the new_proposal sweep — here the two per-item slots
    are the two agent sides (agent_a, agent_b) of ONE proposal, not two users."""
    events: list[str] = []

    class _TD:
        id = "td1"
        agent_a = "alpha"
        agent_b = "beta"
        outcome = "proposal"

    class _Sess:
        def __init__(self):
            self._td = _TD()
            self._calls = 0

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

        async def execute(self, *a, **k):
            call = self._calls
            self._calls += 1
            td = self._td
            if call == 0:

                class _Bulk:
                    def scalars(s):
                        return s

                    def all(s):
                        return [td]

                return _Bulk()

            class _Reload:
                def scalar_one_or_none(s):
                    return td

            return _Reload()

    seen = []

    async def _maybe_send(td, agent_id_str, db):
        seen.append(agent_id_str)
        if agent_id_str == "beta":
            raise RuntimeError("poisoned flush")
        return True

    monkeypatch.setattr(en, "_maybe_send_new_proposal", _maybe_send)
    sess = _Sess()
    await en.check_and_send_new_proposal_emails(_FakeSessionFactory(sess))

    assert seen == ["alpha", "beta"]
    assert events[:2] == ["commit", "rollback"], (
        "agent alpha's new-proposal send was not committed before agent beta's failure rolled "
        f"back the shared session — alpha got an email with no durable record. events={events}"
    )


# ---------------------------------------------------------------------------
# V4-4: every outbound send in this module goes through the recipient allowlist
# ---------------------------------------------------------------------------


class _RecordingSES:
    """Stands in for the boto3 SES client. Records BOTH send shapes, so the assertion is
    "nothing left this module", not "nothing left via the API it happens to use today"."""

    def __init__(self):
        self.calls: list[dict] = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "m-1"}

    def send_raw_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "m-1"}


class _U:
    """Minimal stand-in for User: _send_paused_email reads only id and email."""

    id = "6f1b9e1e-0000-4000-8000-000000000001"
    email = "pi.paused@scripps.edu"


@pytest.mark.asyncio
async def test_the_paused_notification_email_honours_the_outbound_allowlist(monkeypatch, caplog):
    """V4-4: `_send_paused_email` reached boto3 directly with no `is_allowed_recipient` check,
    while every other send in this module has one (`_process_user_notifications`,
    `send_proposal_notification`, `_send_html_email`). The allowlist exists so a staging or
    partially-migrated deployment cannot mail real PIs; a single ungated path defeats it for
    whoever the downgrade ladder happens to auto-pause. Suppression must also be LOGGED — a
    silent one is its own defect.
    """
    caplog.set_level(logging.INFO, logger="src.services.email_notifications")
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "someone.else@scripps.edu")
    recorder = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)

    await en._send_paused_email(_U())

    assert recorder.calls == [], (
        "the paused-notification e-mail went to SES for a recipient the outbound allowlist "
        f"excludes: {recorder.calls!r}"
    )
    assert any(
        "suppressed by outbound allowlist" in r.getMessage() and _U.email in r.getMessage()
        for r in caplog.records
    ), f"the suppression was silent; records={[r.getMessage() for r in caplog.records]}"


@pytest.mark.asyncio
async def test_the_paused_notification_email_still_reaches_an_allowed_recipient(monkeypatch):
    """Control for the test above: the gate must not be a blanket off-switch."""
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", _U.email)
    recorder = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)

    await en._send_paused_email(_U())

    assert len(recorder.calls) == 1
    sent = recorder.calls[0]
    assert _U.email in repr(sent)
    assert "CoPI proposal notifications paused" in repr(sent)


@pytest.mark.asyncio
async def test_send_proposal_notification_never_reaches_ses_for_a_blocked_recipient(
    monkeypatch, caplog,
):
    """Step 8 of the same sweep: the module's third raw `boto3` site is inside
    `send_proposal_notification`, and it has no gate of its own — it is gated by the check at
    the top of the same function, ahead of every DB read and the whole body build. `db=None`
    is the proof: any statement past that gate would raise AttributeError on it, so returning
    False without touching SES can only mean the gate ran first.
    """
    caplog.set_level(logging.INFO, logger="src.services.email_notifications")
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "someone.else@scripps.edu")
    recorder = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)

    class _TD:
        id = "td-1"

    sent = await en.send_proposal_notification(
        user=_U(), thread_decision=_TD(), agent=object(),
        other_bot_name="OtherBot", total_unreviewed=1, db=None,
    )

    assert sent is False
    assert recorder.calls == []
    assert any("suppressed by outbound allowlist" in r.getMessage() for r in caplog.records)


# --- RC-4 follow-up: _expire_lapsed_outstanding must not trust its callers ---------


class _Notif:
    """Minimal stand-in for EmailNotification: the helper only touches id/status/sent_at."""

    def __init__(self, sent_at):
        self.id = "6f1b9e1e-0000-4000-8000-000000000002"
        self.status = "sent"
        self.sent_at = sent_at


class _NoFlushDB:
    """Fails the test loudly if the helper writes anything -- a real AsyncSession's
    flush() would otherwise silently no-op over an in-window row that should never have
    been touched."""

    async def flush(self):
        raise AssertionError("_expire_lapsed_outstanding flushed a row it should not have")


@pytest.mark.asyncio
async def test_expire_lapsed_outstanding_leaves_an_in_window_row_alone(monkeypatch):
    """Opus review, RC-4 follow-up: the helper previously trusted its caller's earlier
    age check (40 lines away in `_process_user_notifications`) to guarantee any row
    reaching it was already past the reply window. It must re-check `sent_at` against
    `settings.email_notification_expiry_days` itself, so correctness does not depend on
    control flow the reader has to trace elsewhere."""
    from datetime import UTC, datetime

    notification = _Notif(sent_at=datetime.now(UTC))  # sent moments ago -- well inside the window

    await en._expire_lapsed_outstanding(notification, _U(), _NoFlushDB(), reason="test")

    assert notification.status == "sent", "an in-window row must not be expired"


@pytest.mark.asyncio
async def test_expire_lapsed_outstanding_still_expires_a_row_past_the_window():
    """Control: the helper's own purpose still works when given a genuinely lapsed row."""
    from datetime import UTC, datetime, timedelta

    from src.config import get_settings as _get_settings

    class _FlushingDB:
        def __init__(self):
            self.flushed = False

        async def flush(self):
            self.flushed = True

    old_sent_at = datetime.now(UTC) - timedelta(
        days=_get_settings().email_notification_expiry_days + 1
    )
    notification = _Notif(sent_at=old_sent_at)
    db = _FlushingDB()

    await en._expire_lapsed_outstanding(notification, _U(), db, reason="test")

    assert notification.status == "expired"
    assert db.flushed is True
