"""scripts/backfill_agents.py's collision/bot-name logic must match the web
path (agent_page.derive_agent_identity, fixed in issue #26 Task 26.9): the
numeric branch extends the PREFIXED candidate ('pwu2'), not the bare stem
('wu2') — before the fix these diverged, and _bot_name_for('wu2', ...) even
produced 'WWuBot' (agent_id[0] of 'wu2' is 'w'), silently colliding with any
bare-stem 'w...' bot via SimulationEngine._bot_name_to_id (issue #26 C2,
red-team sharpening).
"""

from types import SimpleNamespace

from scripts.backfill_agents import _bot_name_for, _resolve_agent_id


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


def test_bot_name_for_bare_stem():
    assert _bot_name_for("wu", "Chunlei Wu") == "WuBot"


def test_bot_name_for_prefixed():
    assert _bot_name_for("pwu", "Peng Wu") == "PWuBot"


def test_bot_name_for_numeric_suffix_matches_the_web_path():
    assert _bot_name_for("pwu2", "Pei Wu") == "PWu2Bot"
