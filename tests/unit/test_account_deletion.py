"""SEC-F3 (opus review, audit 2026-09-08): agent_blocking_account_delete's SELECT
must lock the row it reads (FOR UPDATE) so the check and the subsequent delete in
the same session cannot race an admin activating the agent concurrently -- without
a lock, an admin's UPDATE ... SET status='active' could commit between this read
and the caller's delete, orphaning the very agent this guard exists to protect.
"""

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from src.services.account_deletion import agent_blocking_account_delete


class _FakeResult:
    def scalar_one_or_none(self):
        return None


class _CapturingSession:
    """Stands in for AsyncSession: records the statement passed to execute()."""

    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult()


class _U:
    id = uuid.uuid4()


@pytest.mark.asyncio
async def test_the_agent_lookup_locks_the_row_for_update():
    session = _CapturingSession()

    await agent_blocking_account_delete(session, _U())

    assert len(session.statements) == 1
    compiled = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "FOR UPDATE" in compiled.upper(), (
        f"expected the agent lookup to lock the row it reads, got: {compiled}"
    )
