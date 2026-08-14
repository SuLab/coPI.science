"""`retrieve_profile` takes its argument straight from the model, so it is a
path-traversal surface.

`retrieve_profile` is in `DEFAULT_TOOLS`, so every `pi_lab` spoke has it, and its
argument is whatever the model puts in `tool_input["agent_id"]`. It built
`PROFILES_DIR / "public" / f"{agent_id}.md"` with no sanitisation, so a spoke
could read outside `profiles/public/` entirely:

  * `../private/blackbird` → the hub's private screening rubric (still on disk;
    the loader was removed in the 2026-08-12 cycle but the file was not),
  * `../memory/<id>/public` → any other lab's working memory.

Under a star topology whose whole point is that spokes cannot see each other,
that is a confidentiality hole reachable by one tool call. See SEC-14 (the
`delimit()` fencing on the same return path) for the adjacent concern.
"""

import asyncio
from pathlib import Path

from src.agent.tools import _execute_retrieve_profile


def _run(agent_id: str) -> str:
    return asyncio.run(_execute_retrieve_profile(agent_id))


class TestTraversalIsRefused:
    def test_parent_escape_to_the_private_tree_is_refused(self):
        out = _run("../private/blackbird")
        assert "Operational Screening Instructions" not in out
        assert "no public profile" in out.lower() or "invalid" in out.lower()

    def test_parent_escape_to_the_memory_tree_is_refused(self):
        out = _run("../memory/blackbird/public")
        assert "Working Memory" not in out
        assert "no public profile" in out.lower() or "invalid" in out.lower()

    def test_a_nested_traversal_is_refused(self):
        out = _run("x/../../private/blackbird")
        assert "Operational Screening Instructions" not in out

    def test_an_absolute_path_is_refused(self):
        out = _run("/etc/passwd")
        assert "root:" not in out

    def test_a_bare_separator_is_refused(self):
        out = _run("private/blackbird")
        assert "Operational Screening Instructions" not in out


class TestLegitimateReadsStillWork:
    def test_a_real_public_profile_is_still_returned(self):
        """The tool must keep working for the case it exists for."""
        available = sorted(Path("profiles/public").glob("*.md"))
        if not available:
            import pytest
            pytest.skip("no public profiles on this host to read")
        agent_id = available[0].stem
        out = _run(agent_id)
        assert "<agent_profile>" in out
        assert len(out) > 50

    def test_an_unknown_but_well_formed_id_reports_not_found(self):
        out = _run("nosuchlab")
        assert "No public profile found" in out
