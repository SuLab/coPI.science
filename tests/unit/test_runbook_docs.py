"""Doc-accuracy checks for the final-fix-wave Unit F controller documentation
(#27 I2 I3 I5): the prod runbook (Part R of the close-issues-20-27 plan doc)
must fold in the implementation-review deploy notes with the corrected UID
10001 chown scope, the plan doc must carry a controller-rulings appendix,
docs/production-migration.md's routine-deploy section must document the
fail-closed escape hatch and the reboot-vs-`migrate` restart-policy mismatch,
and CLAUDE.md's agent-restart runbook must call out that step 3 now also
drives the `migrate` one-shot service.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_DOC = REPO_ROOT / "docs" / "plans" / "2026-09-02-close-issues-20-27.md"
PROD_MIGRATION_DOC = REPO_ROOT / "docs" / "production-migration.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"


def test_plan_doc_has_controller_rulings_appendix():
    text = PLAN_DOC.read_text()
    assert "Controller rulings during implementation" in text


def test_plan_doc_has_correct_uid_10001_chown_scope():
    text = PLAN_DOC.read_text()
    assert "chown -R 10001:10001 profiles/ data/" in text
    assert "chown -R 10001:10001 profiles/ prompts/" not in text


def test_production_migration_doc_documents_fail_closed_escape_hatch():
    text = PROD_MIGRATION_DOC.read_text()
    assert "--no-deps app worker grantbot" in text


def test_production_migration_doc_documents_reboot_restart_policy():
    text = PROD_MIGRATION_DOC.read_text()
    assert 'restart: "no"' in text


def test_claude_md_restart_step_mentions_migrate():
    text = CLAUDE_MD.read_text()
    idx = text.index("Before restarting")
    restart_section = text[idx : idx + 2000]
    assert "migrate" in restart_section
