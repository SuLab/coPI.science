"""scripts/generate_sparsedata_user.py's _resolve_agent_id must stay in lockstep with
scripts/backfill_agents.py's (pinned in tests/unit/test_backfill_agents.py) and with the
web path (agent_page.derive_agent_identity): the numeric branch
extends the PREFIXED candidate ('pwu2'), not the bare stem ('wu2').

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
    """_persist's inline bot-name derivation must be digit-aware, or a THIRD
    same-initial namesake ('pwu2') gets 'PWuBot' — a duplicate of the second
    Wu's bot name. It must match backfill_agents._bot_name_for.
    """
    db = _FakeAgentRegistryDb(taken=["wu", "pwu"])
    agent_id = await _resolve_agent_id("Pei Wu", db)
    assert (agent_id, _bot_name_for(agent_id, "Pei Wu")) == ("pwu2", "PWu2Bot")


async def test_bot_name_matches_the_web_and_backfill_paths_on_the_fourth_collision():
    db = _FakeAgentRegistryDb(taken=["wu", "pwu", "pwu2"])
    agent_id = await _resolve_agent_id("Ping Wu", db)
    assert (agent_id, _bot_name_for(agent_id, "Ping Wu")) == ("pwu3", "PWu3Bot")


def test_bot_name_derivation_is_byte_identical_to_the_backfill_copy():
    """The two `scripts/` copies must agree on every shape.

    `_bot_name_for` is duplicated in `scripts/generate_sparsedata_user.py` because
    `scripts/` is not an importable package and that file is run directly (see
    `test_the_script_can_be_run_the_way_its_docstring_documents`). This test is the
    pin that keeps the duplicate honest.
    """
    from scripts.backfill_agents import _bot_name_for as backfill_copy
    from scripts.generate_sparsedata_user import _bot_name_for as sparse_copy

    cases = [
        ("wu", "Chunlei Wu"),
        ("pwu", "Peng Wu"),
        ("pwu2", "Pei Wu"),
        ("pwu3", "Ping Wu"),
        ("pwu19", "Po Wu"),
        ("mcdonald", "Ann McDonald"),
        ("obrien", "Sean O'Brien"),
        ("su", "Andrew Su"),
    ]
    assert [sparse_copy(a, n) for a, n in cases] == [backfill_copy(a, n) for a, n in cases]


def test_the_script_can_be_run_the_way_its_docstring_documents():
    """`python scripts/generate_sparsedata_user.py` must import cleanly.

    The docstring's own usage line is
    `docker compose exec app python scripts/generate_sparsedata_user.py ...`, which
    puts `scripts/` on `sys.path`, not the repo root — so any `from scripts.… import`
    in this file raises ModuleNotFoundError for the operator while still passing
    under pytest (which inserts the repo root). This test is the guard.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "scripts/generate_sparsedata_user.py", "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr
