"""V4-1 fix round 1 (review Critical #1): Session.rollback() expires EVERY object in the
identity map, not just the item that failed. The three sweeps in email_notifications.py
pre-load all their rows with one bulk SELECT and then, at the top of each loop iteration,
read an attribute off the PRE-LOADED object (e.g. `user.id`, `td.id`) to capture an id for
logging/keying. After ANY earlier iteration's rollback, that read lands on an expired
instance and this async session raises MissingGreenlet instead of transparently re-querying
-- the plain Exception escapes the whole sweep (past the `async with session_factory()`
block), which aborts every user/proposal ordered after the first failure. A fake session has
no real identity map or expiry semantics and cannot reproduce this -- these are
real-Postgres tests. See src/worker/main.py's reap_stale_jobs (commit 75f265b) for the
re-load-by-id pattern this fix round ports into the three sweeps.
"""

import pytest
from sqlalchemy import select

import src.services.email_notifications as en
from src.models import ThreadDecision, User
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_ses(monkeypatch):
    """Nothing in this file may reach SES -- both tests replace the per-item worker
    entirely, so a real send would mean the patch never took effect."""

    def _boom(*a, **k):
        raise AssertionError(f"boto3.client() called unexpectedly: {a!r} {k!r}")

    monkeypatch.setattr("boto3.client", _boom)


class _FixtureSessionFactory:
    """Route a self-opened session (the sweep's) at the rolled-back test session. Same
    shape as tests/integration/test_proposal_review.py's helper of the same name: the test
    session runs in `create_savepoint` mode, so the sweep's own `db.commit()` calls only
    release a savepoint, and `__aexit__` must NOT close the fixture-owned session."""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


async def test_check_and_send_notifications_survives_a_poisoned_earlier_user(
    db_session, monkeypatch,
):
    """Two real users. The per-item worker is patched to touch the DB and then raise for
    whichever user the sweep processes FIRST -- row order between two UUID-keyed users is
    not guaranteed by the sweep's un-ordered SELECT, so the test keys off processing order
    rather than a named user. The user processed second must still be processed, and the
    sweep must return normally instead of propagating MissingGreenlet."""
    user_a = await factories.make_user(db_session, email="sweep.a@scripps.edu")
    user_b = await factories.make_user(db_session, email="sweep.b@scripps.edu")
    # Captured now: the sweep shares db_session's identity map, so its own internal
    # rollback (for whichever user is poisoned) expires these SAME objects -- reading
    # user_a.id / user_b.id AFTER the sweep call, on this un-awaited assertion line,
    # would itself raise MissingGreenlet for the same reason the fix addresses inside
    # the sweep.
    user_a_id, user_b_id = user_a.id, user_b.id
    # Commit (not just flush) so these rows are NOT part of the sweep's own
    # in-flight transaction -- matching production, where the sweep only ever
    # SELECTs rows that were committed long ago. (A row still tracked as "new"
    # in the CURRENT SessionTransaction gets EXPUNGED-to-transient on rollback
    # instead of merely expired, since its own INSERT is undone too -- that
    # masks this bug rather than reproducing it.)
    await db_session.commit()

    processed: list = []
    state = {"raised": False}

    async def _poison_first(user, db):
        if not state["raised"]:
            state["raised"] = True
            # Touch the DB before raising, mirroring a real flush-time failure rather
            # than a bare exception that never went near the session.
            await db.execute(select(User).where(User.id == user.id))
            raise RuntimeError("boom")
        processed.append(user.id)
        return True

    monkeypatch.setattr(en, "_process_user_notifications", _poison_first)

    sent = await en.check_and_send_notifications(_FixtureSessionFactory(db_session))

    assert state["raised"] is True, "the patch never fired -- test setup is broken"
    assert processed == [user_a_id] or processed == [user_b_id], (
        "the surviving user was never processed -- the sweep aborted instead of "
        f"continuing past the poisoned item. processed={processed}"
    )
    assert sent == 1, f"expected exactly one successful send after the poisoned item, got {sent}"


async def test_check_and_send_new_proposal_emails_survives_a_poisoned_earlier_proposal(
    db_session, monkeypatch,
):
    """Two real proposals (four (proposal, agent-side) slots total). The per-item worker is
    patched to touch the DB and then raise for whichever slot the sweep processes FIRST; the
    remaining three slots must still be processed and the sweep must return normally."""
    await factories.make_thread_decision(
        db_session, agent_a="np-alpha", agent_b="np-beta", outcome="proposal",
    )
    await factories.make_thread_decision(
        db_session, agent_a="np-gamma", agent_b="np-delta", outcome="proposal",
    )
    # Commit (not just flush) -- see the comment in the notifications test above.
    await db_session.commit()

    processed: list = []
    state = {"raised": False}

    async def _poison_first(td, agent_id_str, db):
        if not state["raised"]:
            state["raised"] = True
            await db.execute(select(ThreadDecision).where(ThreadDecision.id == td.id))
            raise RuntimeError("boom")
        processed.append(agent_id_str)
        return True

    monkeypatch.setattr(en, "_maybe_send_new_proposal", _poison_first)

    sent = await en.check_and_send_new_proposal_emails(_FixtureSessionFactory(db_session))

    assert state["raised"] is True, "the patch never fired -- test setup is broken"
    assert len(processed) == 3, (
        "expected the 3 surviving (proposal, agent-side) slots to still be processed after "
        f"the first one raised; processed={processed}"
    )
    assert sent == 3, f"expected exactly 3 successful sends after the poisoned slot, got {sent}"
