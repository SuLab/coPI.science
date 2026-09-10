"""SEC-F3 (opus review, audit 2026-09-08): agent_blocking_account_delete's SELECT
must lock the row it reads (FOR UPDATE) so the check and the subsequent delete in
the same session cannot race an admin activating the agent concurrently -- without
a lock, an admin's UPDATE ... SET status='active' could commit between this read
and the caller's delete, orphaning the very agent this guard exists to protect.

SEC2-2 (audit 2026-09-08) tightens this further: the lock has to be taken by
owner (``user_id``) alone, not ``user_id AND status IN (...)``. A status
predicate in the WHERE clause locks nothing when the agent is currently
inactive, so a concurrent activation of that same row is free to commit and
slip past the guard entirely -- the status check has to happen in Python,
after the row (and its lock) is already held.
"""

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from src.services.account_deletion import agent_blocking_account_delete


class _Agent:
    def __init__(self, status):
        self.status = status


class _FakeResult:
    def __init__(self, agent=None):
        self._agent = agent

    def scalar_one_or_none(self):
        return self._agent


class _CapturingSession:
    """Stands in for AsyncSession: records the statement passed to execute()."""

    def __init__(self, agent=None):
        self.statements = []
        self._agent = agent

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self._agent)


class _U:
    id = uuid.uuid4()


def _compiled(stmt):
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.mark.asyncio
async def test_the_agent_lookup_locks_the_row_for_update():
    session = _CapturingSession()

    await agent_blocking_account_delete(session, _U())

    assert len(session.statements) == 1
    compiled = _compiled(session.statements[0])
    assert "FOR UPDATE" in compiled.upper(), (
        f"expected the agent lookup to lock the row it reads, got: {compiled}"
    )


@pytest.mark.asyncio
async def test_the_lock_is_keyed_on_owner_alone_not_status():
    session = _CapturingSession()

    await agent_blocking_account_delete(session, _U())

    compiled = _compiled(session.statements[0]).upper()
    where_clause = compiled.split("WHERE", 1)[1]
    assert "STATUS" not in where_clause, (
        "the WHERE clause must not filter on status -- otherwise the row is "
        f"never locked while inactive, and a concurrent activation slips "
        f"past the guard. got: {compiled}"
    )


@pytest.mark.asyncio
async def test_an_inactive_agent_does_not_block_the_delete():
    session = _CapturingSession(agent=_Agent(status="inactive"))

    result = await agent_blocking_account_delete(session, _U())

    assert result is None


@pytest.mark.asyncio
async def test_an_active_agent_blocks_the_delete():
    active = _Agent(status="active")
    session = _CapturingSession(agent=active)

    result = await agent_blocking_account_delete(session, _U())

    assert result is active


@pytest.mark.asyncio
async def test_for_update_false_skips_the_lock_for_the_read_only_confirmation_page():
    session = _CapturingSession()

    await agent_blocking_account_delete(session, _U(), for_update=False)

    compiled = _compiled(session.statements[0]).upper()
    assert "FOR UPDATE" not in compiled, (
        f"the GET confirmation page must not hold a row lock: {compiled}"
    )
