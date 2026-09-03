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

from sqlalchemy.exc import IntegrityError

from src.routers import public


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeRequest:
    """Just enough of a Starlette Request for client_ip() / the rate limiter."""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.client = types.SimpleNamespace(host="127.0.0.1")


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
