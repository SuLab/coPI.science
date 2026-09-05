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


def test_production_migration_doc_lists_0024_as_a_supported_starting_point():
    """issue #26 Minor 10: A4's doc widening (Part M) added 0024 as a
    supported starting point but no test pinned it."""
    text = PROD_MIGRATION_DOC.read_text()
    assert "**0024**" in text
    assert "is also a supported starting point" in text


def _alembic_head() -> str:
    """The tree's single head, derived the same way preflight and run_migration.sh do."""
    import re

    versions = REPO_ROOT / "alembic" / "versions"
    ids: set[str] = set()
    parents: set[str] = set()
    for path in sorted(versions.glob("*.py")):
        src = path.read_text()
        m = re.search(r'^revision(?::\s*str)?\s*=\s*"([^"]+)"', src, re.M)
        d = re.search(r'^down_revision[^=]*=\s*"([^"]+)"', src, re.M)
        if m:
            ids.add(m.group(1))
        if d:
            parents.add(d.group(1))
    heads = sorted(ids - parents)
    assert len(heads) == 1, f"expected exactly one alembic head, found {heads}"
    return heads[0]


def test_production_migration_doc_states_the_current_alembic_head():
    """#27 I2 / F23: the runbook named a target that the tree had moved past.

    `run_migration.sh` now derives the target, so the number here is documentation
    rather than configuration -- but a runbook that quotes a stale head still sends the
    operator looking for a mismatch that is not there, so it is pinned to the tree.
    """
    head = _alembic_head()
    text = PROD_MIGRATION_DOC.read_text()
    marker = f"the alembic tree's single head, **{head}** at the time of writing"
    assert marker in text, (
        f"docs/production-migration.md must state the current head; expected the phrase "
        f"{marker!r} and did not find it. The tree's head is {head}."
    )


def test_production_migration_doc_says_the_target_is_derived_not_pinned():
    text = PROD_MIGRATION_DOC.read_text()
    assert "derives that target from `alembic/versions/`" in text, (
        "docs/production-migration.md must say run_migration.sh derives its target from "
        "the alembic tree, so a reader does not go looking for a constant to bump"
    )


# --------------------------------------------------------------------------- #
# Part R additions deferred to Task 33 (plan Step 11): the per-user pipeline
# re-run, 0029's measured lock row, and deploy note 18's discharged sentence.
# --------------------------------------------------------------------------- #


def test_part_r_carries_the_publication_text_repair_step():
    """Task 20 measured 896 rows still holding truncated PubMed text and shipped
    `scripts/repair_publication_text.py`; without an ordered runbook step the repair is
    prose nobody re-reads."""
    text = PLAN_DOC.read_text()
    assert "scripts/repair_publication_text.py --all" in text, (
        "Part R must carry Task 20's repair step, including --all (the targeted "
        "predicate reaches only 248 of the 896 wrong rows)"
    )
    assert "--all --apply" in text, (
        "Part R must show the apply invocation as well as the dry run, since the script "
        "is dry-run by default"
    )


def test_part_r_carries_task_20s_selection_query_verbatim():
    """The operator must be able to inventory before and verify after."""
    text = PLAN_DOC.read_text()
    predicate = (
        "WHERE p.title ~ '[[:space:]]$'\n"
        "   OR char_length(btrim(p.title)) < 2\n"
        "   OR char_length(btrim(coalesce(p.abstract, ''))) < 80"
    )
    assert predicate in text, (
        "Part R must reproduce SELECTION_PREDICATE_SQL verbatim (three-arm WHERE clause); "
        "a paraphrase would select a different population than the script does"
    )


def test_part_r_states_why_a_pipeline_re_run_is_not_enough_on_its_own():
    """The decisive measurement: 84 of 512 flagged rows are unreachable from ORCID."""
    text = PLAN_DOC.read_text()
    assert "84 of the 512" in text, (
        "Part R must state that 84 of the 512 signature-flagged rows carry neither a PMID "
        "nor a DOI on their owner's ORCID record, so a pipeline re-run never sees them"
    )
    assert "twelve PIs whose ORCID works list is empty" in text


def test_part_r_lock_table_has_0029s_measured_row():
    text = PLAN_DOC.read_text()
    assert "1.6 – 3.1 ms" in text, (
        "R.6's lock table must carry Task 8's measured 0029 row (whole revision, "
        "1.6 - 3.1 ms)"
    )
    assert "0.77 – 1.27 ms" in text, "R.6 must carry 0029's thread_decisions ADD COLUMN timing"
    assert "0.28 – 0.42 ms" in text, "R.6 must carry 0029's agent_messages ADD COLUMN timing"


def test_part_r_lock_table_says_what_each_figure_measures():
    """audit-phase8-migration.md N4: preflight's published "worst-case lock window 0.3 s"
    is `estimate_lock_window_ms(publications_rows)`, a model calibrated on 0019's
    agent_messages index build -- not a measurement of this chain. A table that mixes
    measured statement times with that number and labels the column "Measured" is wrong."""
    text = PLAN_DOC.read_text()
    assert "not a measurement of this chain" in text, (
        "R.6's lock table must say which figures are measurements and which are model "
        "output; audit-phase8-migration.md N4 records the 0.3 s figure as neither"
    )


def test_deploy_note_18_no_longer_defers_the_agent_measurement():
    text = PLAN_DOC.read_text()
    assert "docker stats --no-stream` during a turn before tightening." not in text, (
        "deploy note 18's 'measure before tightening' sentence is discharged by Task 30b "
        "and must be replaced by the measurement itself"
    )
    assert "3.4× headroom" in text, (
        "deploy note 18 must carry the measured peak behind the agent's 768m cap"
    )
