"""Lab counts drift constantly (issue #26 DOC-3/A10: AGENT.md says 10 labs in
one place, 8 bots/8 pilot labs in three others, README says 14+; orcids.txt
has 48). Current-state claims must stop hardcoding a number and instead point
at the live source of truth, /admin/agents. Historical snapshots (the dated
Decisions Log entry and ORCID table) are deliberately left alone — this test
only checks the current-state lines.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_MD = (REPO_ROOT / "AGENT.md").read_text()
README = (REPO_ROOT / "README.md").read_text()


def test_agent_md_pilot_line_has_no_hardcoded_count():
    pilot_line = next(line for line in AGENT_MD.splitlines() if line.startswith("**Pilot:**"))
    assert "/admin/agents" in pilot_line


def test_agent_md_scope_line_has_no_hardcoded_bot_count():
    scope_line = next(line for line in AGENT_MD.splitlines() if "simulation engine" in line)
    assert "8 bots" not in scope_line


def test_agent_md_implementation_status_has_no_hardcoded_lab_count():
    status_line = next(
        line for line in AGENT_MD.splitlines() if "Agent profiles" in line
    )
    assert "8 pilot labs" not in status_line


def test_readme_intro_has_no_hardcoded_count():
    assert "14+ labs" not in README
    assert "/admin/agents" in README
