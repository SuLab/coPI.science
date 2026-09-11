"""A genuine concurrent-insert test for the two write-guard endpoints.

`tests/unit/test_concurrent_write_guards.py` drives the route
functions with a hand-built fake `AsyncSession` that raises a scripted `IntegrityError`
at a specific call — nothing inserts, nothing races, and no real unique constraint is
ever exercised, because the shared, savepoint-joined
`db_session` fixture (tests/conftest.py) cannot produce genuine cross-transaction
concurrency. `tests/integration/test_profile_version_race.py`
already races two independent sessions from the session-scoped `engine` fixture with
`asyncio.gather`, and this file copies that shape for the two write-guard endpoints.

Both tests use TWO independent `async_sessionmaker(engine)` sessions — separate
connections, separate real Postgres transactions — never the shared `db_session`. The
unique-constraint conflict is real: Postgres serializes the second writer's INSERT
against the first's row lock and raises a genuine `IntegrityError` once unblocked, so
no `asyncio.Barrier` is needed to force the overlap.
"""

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import ProposalReview, WaitlistSignup
from src.routers import agent_page, public
from tests import factories

pytestmark = pytest.mark.integration


class _Req:
    """Just enough of a Starlette Request for client_ip()/the rate limiter -- a
    fresh host per instance so two concurrent requests never share the waitlist
    limiter's module-global bucket (mirrors _FakeRequest in
    tests/unit/test_concurrent_write_guards.py)."""

    def __init__(self, host: str | None = None):
        self.headers: dict[str, str] = {}
        self.client = types.SimpleNamespace(host=host or f"127.0.0.1-{uuid.uuid4()}")


@pytest.fixture
def _stub_templates(monkeypatch):
    """Replace Jinja rendering with a passthrough -- this test is about the real
    DB race, not HTML rendering, and the plain fake Request above carries none of
    what Jinja2Templates might otherwise expect."""
    captured: list[dict] = []

    def _fake_response(request, name, context, status_code=200):
        rec = {"name": name, "context": context, "status_code": status_code}
        captured.append(rec)
        return types.SimpleNamespace(**rec)

    monkeypatch.setattr(public.templates, "TemplateResponse", _fake_response)
    return captured


async def test_two_concurrent_first_time_waitlist_signups_race_on_email(
    engine, _stub_templates,
):
    """V5-1/V5-9: two concurrent first-time signups for the SAME email, each on its
    own connection/transaction. Both must see `existing is None` (no upsert branch to
    take), both INSERT, and Postgres's unique `email` constraint (access.py:43) forces
    one of the two real `db.commit()` calls to raise a genuine IntegrityError.

    RED reasoning (pre-fix `waitlist_submit`, no `except IntegrityError` around the
    insert branch's commit): the loser's `await db.commit()` raises uncaught, which
    FastAPI's default handler turns into a 500 -- reproduced against `18ba52c` in the
    report.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    email = f"race-{uuid.uuid4().hex[:16]}@example.edu"

    s1, s2 = factory(), factory()
    try:
        r1, r2 = await asyncio.gather(
            public.waitlist_submit(
                request=_Req(), email=email, name="Racer One", institution="",
                note="", db=s1,
            ),
            public.waitlist_submit(
                request=_Req(), email=email, name="Racer Two", institution="",
                note="", db=s2,
            ),
        )
    finally:
        await s1.close()
        await s2.close()

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.context["waitlist_success"] is True
    assert r2.context["waitlist_success"] is True

    async with factory() as check:
        rows = (await check.execute(
            select(WaitlistSignup).where(WaitlistSignup.email == email)
        )).scalars().all()
    assert len(rows) == 1, (
        f"expected exactly one waitlist_signups row for a lost race, got {len(rows)}"
    )
    assert rows[0].name in ("Racer One", "Racer Two"), (
        "the surviving row must be one writer's own insert, not a mixture"
    )

    async with factory() as cleanup:
        await cleanup.execute(
            text("DELETE FROM waitlist_signups WHERE email = :e"), {"e": email}
        )
        await cleanup.commit()


async def _seed_review_world(db):
    """One PI, one active agent they own, one concluded proposal thread -- ready for
    two concurrent first-time /review requests to race on
    uq_proposal_reviews_decision_agent."""
    pi = await factories.make_user(db, name="Race PI")
    agent = await factories.make_agent(
        db, user=pi, agent_id=f"race{uuid.uuid4().hex[:10]}", status="active",
    )
    td = await factories.make_thread_decision(db, agent_a=agent.agent_id)
    await db.commit()
    return pi.id, agent.agent_id, td.id, td.simulation_run_id


async def test_two_concurrent_first_time_reviews_race_on_the_unique_constraint(
    engine,
):
    """V5-3/V5-8/V5-9: two concurrent first-time /review submissions for the same
    (thread_decision, agent), each on its own connection/transaction, race on
    uq_proposal_reviews_decision_agent. SQLAlchemy autoflush fires the pending INSERT
    at record_engagement's own SELECT (agent_page.py:609), not at commit -- see
    test_review_proposal_survives_a_lost_race_via_autoflush's docstring -- so Postgres
    raises the genuine IntegrityError there for whichever session loses the race.

    Exactly one ProposalReview row must exist afterward, its contents must be one
    writer's own rating/comment (not a mixture), and neither request may 500: the
    winner gets a 302, the loser either upgrades an implicit -1 marker (not reachable
    here -- there is none) or gets a clean 400 "Already reviewed" HTTPException, never
    an uncaught exception.

    RED reasoning (pre-fix `review_proposal`, no `except IntegrityError` spanning
    db.add()..commit()): the loser's IntegrityError escapes uncaught -- reproduced
    against `18ba52c` in the report.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup_db:
        pi_id, agent_slug, td_id, run_id = await _seed_review_world(setup_db)

    current_user = types.SimpleNamespace(id=pi_id, name="Race PI")
    s1, s2 = factory(), factory()
    try:
        results = await asyncio.gather(
            agent_page.review_proposal(
                agent_id=agent_slug, thread_decision_id=td_id, request=_Req(),
                rating=3, comment="from s1", db=s1, current_user=current_user,
            ),
            agent_page.review_proposal(
                agent_id=agent_slug, thread_decision_id=td_id, request=_Req(),
                rating=4, comment="from s2", db=s2, current_user=current_user,
            ),
            return_exceptions=True,
        )
    finally:
        await s1.close()
        await s2.close()

    for r in results:
        if isinstance(r, HTTPException):
            assert r.status_code == 400, f"unexpected HTTPException: {r!r}"
        elif isinstance(r, BaseException):
            raise AssertionError(
                f"review_proposal raised instead of guarding the race: {r!r}"
            )
        else:
            assert r.status_code == 302

    async with factory() as check:
        rows = (await check.execute(
            select(ProposalReview).where(ProposalReview.thread_decision_id == td_id)
        )).scalars().all()
    assert len(rows) == 1, (
        f"expected exactly one proposal_reviews row for a lost race, got {len(rows)}"
    )
    assert (rows[0].rating, rows[0].comment) in [(3, "from s1"), (4, "from s2")], (
        "the surviving row must be one writer's own rating+comment, not a mixture"
    )

    async with factory() as cleanup:
        await cleanup.execute(
            text("DELETE FROM proposal_reviews WHERE thread_decision_id = :t"),
            {"t": td_id},
        )
        await cleanup.execute(
            text("DELETE FROM thread_decisions WHERE id = :t"), {"t": td_id}
        )
        await cleanup.execute(
            text("DELETE FROM agents WHERE agent_id = :a"), {"a": agent_slug}
        )
        await cleanup.execute(text("DELETE FROM users WHERE id = :u"), {"u": pi_id})
        await cleanup.execute(
            text("DELETE FROM simulation_runs WHERE id = :r"), {"r": run_id}
        )
        await cleanup.commit()
