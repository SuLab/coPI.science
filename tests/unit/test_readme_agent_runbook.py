"""README.md's agent-simulation runbook must not repeat two stale claims
(issue #26 DOC-1): (a) PILOT_LABS in src/agent/simulation.py — the roster is
AgentRegistry-driven and PILOT_LABS has zero matches in src/; (b) the
agent-run container "mounts source" — under prod compose it BAKES the source
(CLAUDE.md "Running the Agent Simulation"), so a code change needs
`--profile agent build agent`, not just a restart.
"""

from pathlib import Path

README = (Path(__file__).resolve().parents[2] / "README.md").read_text()


def test_no_pilot_labs_reference():
    assert "PILOT_LABS" not in README


def test_no_stale_mounts_source_claim():
    assert "mounts source code but only loads modules" not in README


def test_points_to_claude_md_for_the_prod_compose_flags():
    assert "docker-compose.prod.yml" in README
