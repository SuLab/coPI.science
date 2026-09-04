"""Doc-accuracy check for CLAUDE.md's "Last-name collisions" paragraph
(issue #26 DOC-C). ``derive_agent_identity`` (src/routers/agent_page.py) and
``_resolve_agent_id``/``_bot_name_for`` (scripts/backfill_agents.py) both
handle a THIRD same-initial namesake by appending a numeric suffix to the
prefixed candidate (``pwu2`` / ``PWu2Bot``), but CLAUDE.md previously stopped
documenting collision handling after the second (prefixed) case. This test
pins the documented third-collision example.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = (REPO_ROOT / "CLAUDE.md").read_text()


def test_last_name_collisions_documents_third_collision_suffix():
    collisions_line = next(
        line for line in CLAUDE_MD.splitlines() if line.startswith("**Last-name collisions:**")
    )
    assert "pwu2" in collisions_line
    assert "PWu2Bot" in collisions_line
