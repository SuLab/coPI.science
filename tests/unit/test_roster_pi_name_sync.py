"""A name edit reaches a running engine at the next roster sync (spec 2026-10-05 §6.4, D23)."""
from tests.unit.test_roster_sync import _make_engine, _patch_client, _row


async def test_a_surviving_agent_takes_the_rows_pi_name(monkeypatch):
    _patch_client(monkeypatch)
    row = _row("su")
    row.pi_name = "Andrew I. Su"
    engine = _make_engine([row], existing_agents=["su"])
    await engine._sync_roster_from_db()
    assert engine.agents["su"].pi_name == "Andrew I. Su"
