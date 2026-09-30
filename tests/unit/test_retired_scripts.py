"""The retired one-off scripts are gone and nothing points at them."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
RETIRED = (
    "backfill_slack_ts", "backfill_agents", "backfill_agent_tokens",
    "migrate_tenure_map", "backfill_dropped_verdicts", "generate_sparsedata_user",
    "backfill_slack_history_to_db",
)
_REF = re.compile(r"\b(" + "|".join(RETIRED) + r")(\.py)?\b")


def test_the_retired_scripts_are_deleted():
    assert [s for s in RETIRED if (ROOT / "scripts" / f"{s}.py").exists()] == []


def test_nothing_in_src_scripts_templates_static_or_the_runbook_names_them():
    hits = []
    roots = [ROOT / d for d in ("src", "scripts", "templates", "static")]
    runbook = [ROOT / "CLAUDE.md", ROOT / "src/agent/CLAUDE.md", ROOT / "alembic/CLAUDE.md",
               *sorted((ROOT / "docs/operations").glob("*.md")), ROOT / "docs/production-migration.md"]
    files = [p for r in roots for p in r.rglob("*") if p.is_file() and p.suffix in {".py", ".sh", ".html", ".js", ".md"}]
    for path in files + runbook:
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _REF.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert hits == [], "\n".join(hits)


def test_the_orphaned_prompt_files_stay_on_disk():
    """The prompt freeze forbids touching prompts/, deletions included."""
    assert (ROOT / "prompts/profile-synthesis-sparse.md").exists()
    assert (ROOT / "prompts/email-reply-classify.md").exists()
