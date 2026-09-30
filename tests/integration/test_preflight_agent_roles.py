import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

_PF = Path(__file__).resolve().parents[2] / "scripts/migrate/preflight.py"


def _pf():
    spec = importlib.util.spec_from_file_location("_pf_roles", _PF)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # @dataclass resolves its module through sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_an_unregistered_role_blocks(engine):
    pf = _pf()
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            _title, status, _detail, _rem, data = await pf.check_agent_roles(conn)
            assert status == pf.PASS
            await conn.execute(text(
                "INSERT INTO agents(id, agent_id, bot_name, pi_name, role, status, requested_at) "
                "VALUES (gen_random_uuid(), 'oddbot', 'OddBot', 'Odd', 'grantbot', 'inactive', now())"
            ))
            _title, status, _detail, _rem, data = await pf.check_agent_roles(conn)
            assert status == pf.BLOCK and data["unknown"] == ["grantbot"]
        finally:
            await trans.rollback()
