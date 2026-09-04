"""scripts/generate_sparsedata_user.py's _resolve_agent_id must stay in lockstep with
scripts/backfill_agents.py's (pinned in tests/unit/test_backfill_agents.py) and with the
web path (agent_page.derive_agent_identity, issue #26 Task 26.9): the numeric branch
extends the PREFIXED candidate ('pwu2'), not the bare stem ('wu2') (#26 C2, D22).

This file mirrors the three cases from test_backfill_agents.py against the sparsedata
script's own copy of the function so a future edit to one that isn't mirrored to the
other is caught here rather than in production disambiguation.
"""

from types import SimpleNamespace

from scripts.generate_sparsedata_user import _bot_name_for, _resolve_agent_id


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


async def test_resolve_agent_id_raises_when_the_numeric_range_is_exhausted():
    taken = {"wu", "pwu"} | {f"pwu{i}" for i in range(2, 20)}
    db = _FakeAgentRegistryDb(taken=taken)
    try:
        await _resolve_agent_id("Ping Wu", db)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError when range(2, 20) is exhausted")


async def test_bot_name_matches_the_web_and_backfill_paths_on_the_third_collision():
    """issue #26 I1: _persist's inline bot-name derivation was not digit-aware,
    so a THIRD same-initial namesake ('pwu2') got 'PWuBot' — a duplicate of
    the second Wu's bot name. It must match backfill_agents._bot_name_for.
    """
    db = _FakeAgentRegistryDb(taken=["wu", "pwu"])
    agent_id = await _resolve_agent_id("Pei Wu", db)
    assert (agent_id, _bot_name_for(agent_id, "Pei Wu")) == ("pwu2", "PWu2Bot")


async def test_bot_name_matches_the_web_and_backfill_paths_on_the_fourth_collision():
    db = _FakeAgentRegistryDb(taken=["wu", "pwu", "pwu2"])
    agent_id = await _resolve_agent_id("Ping Wu", db)
    assert (agent_id, _bot_name_for(agent_id, "Ping Wu")) == ("pwu3", "PWu3Bot")
