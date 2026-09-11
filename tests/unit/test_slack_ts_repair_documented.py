"""CLAUDE.md and README.md must mention the one-time backfill_slack_ts.py
repair: a workspace with pre-Stage-6 agent_messages rows
(slack_ts IS NULL) silently keeps Slack replies to those threads off Slack
(_slack_parent_ts returns None for them, src/agent/simulation.py), and
docs/production-migration.md's own coverage of the repair is scoped to
starting points 0018-0021 — a workspace already at head with never-repaired
legacy rows has no other doc that tells it to run this.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


# -e PYTHONPATH=/app is part of the command, not decoration: sys.path[0] is the script's
# own directory, so `import src` would otherwise resolve to the copy baked into
# site-packages rather than /app. docs/production-migration.md and
# run_migration.sh's step-8 text must say so; the runbook paragraphs must not
# omit it.
_WRAPPED_COMMAND = (
    "docker compose exec -e PYTHONPATH=/app app python scripts/backfill_slack_ts.py --apply"
)


def test_claude_md_mentions_the_repair_script():
    text = (REPO_ROOT / "CLAUDE.md").read_text()
    assert _WRAPPED_COMMAND in text, (
        "CLAUDE.md must tell the operator to run the repair through "
        "docker compose exec, per the repo convention"
    )


def test_readme_mentions_the_repair_script():
    text = (REPO_ROOT / "README.md").read_text()
    assert _WRAPPED_COMMAND in text, (
        "README.md must tell the operator to run the repair through "
        "docker compose exec, per the repo convention"
    )
