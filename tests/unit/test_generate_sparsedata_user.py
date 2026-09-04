"""scripts/generate_sparsedata_user.py's _resolve_agent_id must stay in lockstep with
scripts/backfill_agents.py's (pinned in tests/unit/test_backfill_agents.py) and with the
web path (agent_page.derive_agent_identity, issue #26 Task 26.9): the numeric branch
extends the PREFIXED candidate ('pwu2'), not the bare stem ('wu2') (#26 C2, D22).

This file mirrors the three cases from test_backfill_agents.py against the sparsedata
script's own copy of the function so a future edit to one that isn't mirrored to the
other is caught here rather than in production disambiguation.
"""

from types import SimpleNamespace

from scripts.generate_sparsedata_user import _resolve_agent_id


class _FakeAgentRegistryDb:
    """Serves AgentRegistry.agent_id == <candidate> lookups from a fixed set."""

    def __init__(self, taken):
        self._taken = set(taken)

    async def execute(self, stmt):
        candidate = stmt.whereclause.right.value
        hit = candidate if candidate in self._taken else None
        return SimpleNamespace(scalar_one_or_none=lambda: hit)


async def test_resolve_agent_id_bare_stem_when_free():
    assert await _resolve_agent_id("Chunlei Wu", _FakeAgentRegistryDb(taken=[])) == "wu"


async def test_resolve_agent_id_prefixes_on_first_collision():
    assert await _resolve_agent_id("Peng Wu", _FakeAgentRegistryDb(taken=["wu"])) == "pwu"


async def test_resolve_agent_id_numeric_suffix_extends_the_prefixed_candidate():
    db = _FakeAgentRegistryDb(taken=["wu", "pwu"])
    assert await _resolve_agent_id("Pei Wu", db) == "pwu2"
