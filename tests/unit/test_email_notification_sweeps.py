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

import pytest

import src.services.email_notifications as en


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
