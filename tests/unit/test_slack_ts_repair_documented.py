"""CLAUDE.md and README.md must mention the one-time backfill_slack_ts.py
repair (issue #26 DOC-7/A3): a workspace with pre-Stage-6 agent_messages rows
(slack_ts IS NULL) silently keeps Slack replies to those threads off Slack
(_slack_parent_ts returns None for them, src/agent/simulation.py), and
docs/production-migration.md's own coverage of the repair is scoped to
starting points 0018-0021 — a workspace already at head with never-repaired
legacy rows has no other doc that tells it to run this.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_claude_md_mentions_the_repair_script():
    text = (REPO_ROOT / "CLAUDE.md").read_text()
    assert "backfill_slack_ts.py" in text
    assert "--apply" in text


def test_readme_mentions_the_repair_script():
    text = (REPO_ROOT / "README.md").read_text()
    assert "backfill_slack_ts.py" in text
