"""Unit-level guards for the two concurrent-first-write races in issue #24 (V5).

Both `waitlist_submit` and `review_proposal` do a SELECT-then-INSERT with no
`IntegrityError` handling, so two concurrent first-time requests can both pass
the guard SELECT and race on a unique constraint. The savepoint-per-test fixture
(`tests/conftest.py` `db_session`) makes a TRUE concurrent commit hard to
reproduce (both "concurrent" sessions would really be the same nested
savepoint), so these tests call the route functions directly with a fake
AsyncSession that raises IntegrityError at the exact point autoflush/commit
would in the real race, instead of driving two real concurrent HTTP requests.
"""

import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.routers import agent_page, public


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        # I2/I3 (#24 V5, V5-2): both reopen_proposal's and review_proposal's
        # post-rollback re-selects use scalar_one() -- a winner row (or the
        # reloaded ThreadDecision) must exist once IntegrityError has fired.
        assert self._value is not None, "scalar_one() called with no row queued"
        return self._value

    def scalars(self):
        return self

    def all(self):
        return [] if self._value is None else [self._value]


class _FakeRequest:
    """Just enough of a Starlette Request for client_ip() / the rate limiter.

    `host` defaults to a fresh uuid per instance (Task 24 test minor, V5):
    `_waitlist_limiter` in src/routers/public.py is a MODULE-GLOBAL
    SlidingWindowRateLimiter keyed by client_ip() -- shared across every test in the
    whole run, not just this file. A fixed "127.0.0.1" would let one test's calls
    count against another's budget depending on run order. Pass an explicit `host`
    only when a test needs two requests to land in the SAME bucket.
    """

    def __init__(self, host: str | None = None):
        self.headers: dict[str, str] = {}
        self.client = types.SimpleNamespace(host=host or f"127.0.0.1-{uuid.uuid4()}")


def _stub_templates(monkeypatch):
    """Replace Jinja rendering with a passthrough so these stay pure unit tests
    -- no app, no request-routing context, no template files touched."""
    captured = {}

    def _fake_response(request, name, context, status_code=200):
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return types.SimpleNamespace(name=name, context=context, status_code=status_code)

    monkeypatch.setattr(public.templates, "TemplateResponse", _fake_response)
    return captured


class _RaceSession:
    """Fake AsyncSession: serves canned SELECT results in order; raises the
    given exception on the first commit() to model a lost race caught there."""

    def __init__(self, select_results, raise_on_commit=None):
        self._select_results = list(select_results)
        self._raise_on_commit = raise_on_commit
        self.added: list[object] = []
        self.commits = 0
        self.rolled_back = False

    async def execute(self, _stmt):
        return _FakeResult(self._select_results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1
        if self.commits == 1 and self._raise_on_commit is not None:
            raise self._raise_on_commit

    async def rollback(self):
        self.rolled_back = True


async def test_waitlist_submit_survives_a_lost_race_on_email(monkeypatch):
    """V5-1/V5-9: two concurrent first-time signups for the same email both see
    `existing is None`, both `db.add`, and the loser's `commit()` raises
    IntegrityError on the unique `email` constraint (`access.py:43`). Before the
    fix there is no `except` anywhere in `waitlist_submit` -- this is an
    unhandled 500 all the way out to FastAPI's default handler."""
    captured = _stub_templates(monkeypatch)
    db = _RaceSession(
        select_results=[None],  # the guard SELECT finds no existing row
        raise_on_commit=IntegrityError(
            "INSERT INTO waitlist_signups ...", {}, Exception("dup")
        ),
    )

    resp = await public.waitlist_submit(
        request=_FakeRequest(), email="race@example.edu", name="", institution="",
        note="", db=db,
    )

    assert resp.status_code == 200
    assert captured["context"]["waitlist_success"] is True, (
        "the response must be the same success page the existing-row branch renders"
    )
    assert db.rolled_back is True
    assert len(db.added) == 1, "the row must still be staged before the race is caught"
    assert db.commits == 1, (
        "commit() must be attempted exactly once -- a caught IntegrityError must not "
        "be retried, and the guard must not skip calling commit() altogether"
    )


class _ReviewRaceSession:
    """Fake AsyncSession for review_proposal: serves the three guard SELECTs
    (agent, thread_decision, existing-review) in order, then raises on the
    4th execute() -- the call `record_engagement` makes. This models the
    red-team's finding that SQLAlchemy autoflush surfaces the loser's
    IntegrityError there, three lines before `commit()`, not at commit."""

    def __init__(self, select_results, raise_at, raise_exc):
        self._select_results = list(select_results)
        self._raise_at = raise_at
        self._raise_exc = raise_exc
        self._calls = 0
        self.added: list[object] = []
        self.rolled_back = False
        self.commits = 0

    async def execute(self, _stmt):
        self._calls += 1
        if self._calls == self._raise_at:
            raise self._raise_exc
        if not self._select_results:
            return _FakeResult(None)  # post-guard cleanup selects (V4-4b's except arm)
        return _FakeResult(self._select_results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rolled_back = True


async def test_review_proposal_survives_a_lost_race_via_autoflush():
    """V5-3/V5-9, corrected per the red-team: the SELECT guard (agent_page.py:487-494)
    stops a SEQUENTIAL re-review, but two concurrent first-time reviews both pass it
    and race on `uq_proposal_reviews_decision_agent`. Because record_engagement and
    mark_notification_responded each run their own db.execute() on the SAME session
    after db.add(review), SQLAlchemy's autoflush (default True; session factory kwargs
    at src/database.py:39-43 do not disable it) fires the pending INSERT at the FIRST
    of those calls -- so the IntegrityError surfaces from record_engagement's SELECT,
    not from db.commit(). A guard that wraps only commit() (the vote endpoint's
    pattern) would not catch it; the guard must span from db.add through commit."""
    pi_id = uuid.uuid4()
    td_id = uuid.uuid4()
    agent_registry_id = uuid.uuid4()
    agent = types.SimpleNamespace(
        id=agent_registry_id, agent_id="alpha", user_id=pi_id, status="active"
    )
    td = types.SimpleNamespace(id=td_id, agent_a="alpha", agent_b="beta")
    current_user = types.SimpleNamespace(id=pi_id, name="PI Alpha")

    db = _ReviewRaceSession(
        select_results=[agent, td, None],
        raise_at=4,
        raise_exc=IntegrityError(
            "INSERT INTO proposal_reviews ...", {}, Exception("dup")
        ),
    )

    with pytest.raises(HTTPException) as ei:
        await agent_page.review_proposal(
            agent_id="alpha", thread_decision_id=td_id, request=_FakeRequest(),
            rating=3, comment="", db=db, current_user=current_user,
        )

    assert ei.value.status_code == 400
    assert ei.value.detail == "Already reviewed"
    assert db.rolled_back is True
    assert len(db.added) == 1, "the review row must still be staged before the guard rolls back"
    # V4-4b / Task 21.13 reconciliation item 2 (COORD_A.md): unlike a bare 24.2-only
    # guard, the except arm here does a SECOND commit after rollback() -- it retires
    # the race LOSER's own outstanding notification, which rollback() would otherwise
    # discard. This supersedes the 24.2-review carried minor "assert db.committed is
    # False" (progress.md): that pinned the pre-21.13 shape, where the except arm
    # never committed at all. `commits == 1` now pins that exactly the recovery commit
    # ran -- not the (rolled-back) one inside the try, and not a retry of either.
    assert db.commits == 1, (
        "the except arm's own retire-then-commit for the race loser's notification "
        "never ran"
    )


def _reopen_fixture(pi_id, td_id, agent_registry_id):
    agent = types.SimpleNamespace(
        id=agent_registry_id, agent_id="alpha", user_id=pi_id, status="active",
    )
    td = types.SimpleNamespace(
        id=td_id, agent_a="alpha", agent_b="beta", thread_id="1700000000.000100",
        channel="degrader-chem", origin_visibility="public", refined_in_channel=None,
    )
    current_user = types.SimpleNamespace(id=pi_id, name="PI Alpha")
    return agent, td, current_user


async def test_reopen_proposal_upgrades_the_engines_implicit_marker_after_a_lost_race(monkeypatch):
    """I2 (#24 V5, audit-issue-24.md): `reopen_proposal`'s write block (`db.add(review)`
    through `commit()`) had NO `except IntegrityError` at all -- the exact guard Task
    24.2 added to review_proposal, left off its sibling three hundred lines below. With
    no existing review, the reopen takes the INSERT branch after
    `migrate_public_thread_to_private` has already created the private Slack channel
    and (flush-only) set `refined_in_channel`. If a concurrent writer -- the engine's
    implicit marker here -- wins the race on `uq_proposal_reviews_decision_agent`, the
    except arm must re-select, find the winning row, upgrade it in place (the same D6
    upgrade the `elif existing_row.rating == -1` branch performs), re-bind
    `refined_in_channel` on a freshly reloaded `ThreadDecision`, and redirect --
    NOT re-run the migration and NOT 500.

    A real-Postgres integration test cannot make a genuinely concurrent writer's row
    survive `reopen_proposal`'s own `rollback()` in this repo's single-savepoint test
    harness (empirically confirmed in
    `tests/integration/test_proposal_review.py::
    test_reopen_write_race_does_not_500_and_recovers_refined_in_channel`, which covers
    the real-Postgres half of I2: the guard actually catches a real IntegrityError and
    `refined_in_channel` survives). This fake-session test is the other half: it proves
    the upgrade-in-place recovery LOGIC itself is correct once a winning row is found --
    mirroring `test_review_proposal_upgrades_the_engines_implicit_marker_after_a_lost_
    race` above, and reusing the same `_ReviewRaceSession` harness (the call-then-raise
    shape is identical; only the SELECT sequence differs).

    Control: `test_reopen_proposal_leaves_a_real_winner_alone_after_a_lost_race` below
    pins that a REAL winning review (rating != -1) is left untouched instead.
    """
    pi_id = uuid.uuid4()
    td_id = uuid.uuid4()
    agent_registry_id = uuid.uuid4()
    agent, td, current_user = _reopen_fixture(pi_id, td_id, agent_registry_id)

    async def _fake_migrate(db, *, thread_decision, creator_agent_id, creator_pi_user, guidance_text):
        thread_decision.refined_in_channel = "fake-priv-channel"
        return types.SimpleNamespace(channel_name="fake-priv-channel")

    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _fake_migrate,
    )

    implicit_winner = types.SimpleNamespace(
        id=uuid.uuid4(), rating=-1, comment=None, submitted_via="engine",
        user_id=None, delegate_user_id=None, reviewed_by_user_id=None, reviewed_at=None,
    )

    db = _ReviewRaceSession(
        # 1 agent, 2 td, 3 pre-migration :695 guard (None -> proceed), 4 post-migration
        # :806 guard (None -> INSERT branch chosen), raise at #5 (record_engagement's
        # autoflush). #6/#7 are the except arm's own re-selects: ThreadDecision, then
        # the winning ProposalReview.
        select_results=[agent, td, None, None, td, implicit_winner],
        raise_at=5,
        raise_exc=IntegrityError(
            "INSERT INTO proposal_reviews ...", {}, Exception("dup")
        ),
    )

    resp = await agent_page.reopen_proposal(
        agent_id="alpha", thread_decision_id=td_id, request=_FakeRequest(),
        guidance="Actually, let's narrow the scope first.", db=db,
        current_user=current_user,
    )

    assert resp.status_code == 302, "the winning implicit marker must be upgraded, not rejected"
    assert db.rolled_back is True
    assert implicit_winner.rating == 0
    assert implicit_winner.comment == "[Reopened] Actually, let's narrow the scope first."
    assert implicit_winner.submitted_via == "web"
    assert implicit_winner.reviewed_by_user_id == pi_id
    assert implicit_winner.delegate_user_id is None, "the PI themself reopened (is_owner=True)"
    assert implicit_winner.reviewed_at is not None, (
        "reviewed_at must be bumped to when the explicit action happened"
    )
    assert td.refined_in_channel == "fake-priv-channel", (
        "refined_in_channel must survive the recovery instead of being silently lost, "
        "orphaning the Slack channel already created for real"
    )
    assert db.commits == 1, "exactly one recovery commit"


async def test_reopen_proposal_leaves_a_real_winner_alone_after_a_lost_race(monkeypatch):
    """I2 (#24 V5) sibling case: the concurrent winner is a REAL decision (a delegate's
    /review, or an e-mail reply), not the engine's implicit marker. The except arm must
    leave it untouched -- the existing WARNING path, same as the `elif` -> `else`
    branch structure in the happy-path guard above -- while still recovering
    `refined_in_channel` and still redirecting rather than 500ing.
    """
    pi_id = uuid.uuid4()
    td_id = uuid.uuid4()
    agent_registry_id = uuid.uuid4()
    agent, td, current_user = _reopen_fixture(pi_id, td_id, agent_registry_id)

    async def _fake_migrate(db, *, thread_decision, creator_agent_id, creator_pi_user, guidance_text):
        thread_decision.refined_in_channel = "fake-priv-channel"
        return types.SimpleNamespace(channel_name="fake-priv-channel")

    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _fake_migrate,
    )

    real_winner = types.SimpleNamespace(rating=3)

    db = _ReviewRaceSession(
        select_results=[agent, td, None, None, td, real_winner],
        raise_at=5,
        raise_exc=IntegrityError(
            "INSERT INTO proposal_reviews ...", {}, Exception("dup")
        ),
    )

    resp = await agent_page.reopen_proposal(
        agent_id="alpha", thread_decision_id=td_id, request=_FakeRequest(),
        guidance="Actually, let's narrow the scope first.", db=db,
        current_user=current_user,
    )

    assert resp.status_code == 302, "a real winning review must still redirect, not 500"
    assert db.rolled_back is True
    assert real_winner.rating == 3, "a real winning review must be left alone, not overwritten"
    assert td.refined_in_channel == "fake-priv-channel", (
        "refined_in_channel must survive the recovery instead of being silently lost"
    )
    assert db.commits == 1, "exactly one recovery commit"
