"""Doc-accuracy checks for the prod deploy runbook: the plan doc's deploy
section must fold in the deploy notes with the corrected UID 10001 chown
scope and carry a controller-rulings appendix,
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


def test_claude_md_restart_step_3_points_at_redeploy_script():
    """A bare `docker compose up -d --build app worker`
    on a running stack does not guarantee `migrate` reruns before the new containers
    start. Step 3 must drive scripts/redeploy.sh, which enforces the ordering
    explicitly, not a raw compose invocation."""
    text = CLAUDE_MD.read_text()
    idx = text.index("Before restarting")
    restart_section = text[idx : idx + 2000]
    assert "scripts/redeploy.sh" in restart_section
    assert "up -d --build app worker" not in restart_section, (
        "step 3 must no longer recreate app/worker with a raw compose command that "
        "cannot guarantee migrate reran first"
    )


def test_production_migration_doc_routine_deploy_section_points_at_redeploy_script():
    """The routine-deploy section (10.1) describes a race between a raw
    `up -d --build app worker grantbot` invocation and `migrate`; it must tell the
    reader to run scripts/redeploy.sh instead of that raw invocation."""
    text = PROD_MIGRATION_DOC.read_text()
    idx = text.index("### 10.1 The routine path")
    section = text[idx : idx + 2500]
    assert "scripts/redeploy.sh" in section


def test_production_migration_doc_step_9_points_at_redeploy_script():
    """Step 9 must not tell the reader
    to run the raw `up -d --build app worker` that section 10.1 documents as
    racy against a running stack. It must point at scripts/redeploy.sh too."""
    text = PROD_MIGRATION_DOC.read_text()
    idx = text.index("### Step 9")
    section = text[idx : idx + 700]
    assert "scripts/redeploy.sh" in section
    assert "docker compose up -d --build app worker" not in section


def test_production_migration_doc_lists_0024_as_a_supported_starting_point():
    """0024 is a supported starting point for a fresh migration and must be
    documented as such."""
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
    """The runbook must not name a fixed alembic-head target that the tree can move
    past.

    `run_migration.sh` derives the target, so the number here is documentation
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
# The deploy runbook (Part R) must additionally cover: the per-user pipeline
# re-run, 0029's measured lock row, and deploy note 18's discharged sentence.
# --------------------------------------------------------------------------- #


def test_part_r_carries_the_publication_text_repair_step():
    """896 rows still hold truncated PubMed text and need
    `scripts/repair_publication_text.py`; without an ordered runbook step the repair is
    prose nobody re-reads."""
    text = PLAN_DOC.read_text()
    assert "scripts/repair_publication_text.py --all" in text, (
        "Part R must carry the repair step, including --all (the targeted "
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
        "R.6's lock table must carry 0029's measured lock row (whole revision, "
        "1.6 - 3.1 ms)"
    )
    assert "0.77 – 1.27 ms" in text, "R.6 must carry 0029's thread_decisions ADD COLUMN timing"
    assert "0.28 – 0.42 ms" in text, "R.6 must carry 0029's agent_messages ADD COLUMN timing"


def test_part_r_lock_table_says_what_each_figure_measures():
    """preflight's published "worst-case lock window 0.3 s"
    is `estimate_lock_window_ms(publications_rows)`, a model calibrated on 0019's
    agent_messages index build -- not a measurement of this chain. A table that mixes
    measured statement times with that number and labels the column "Measured" is wrong."""
    text = PLAN_DOC.read_text()
    assert "not a measurement of this chain" in text, (
        "R.6's lock table must say which figures are measurements and which are model "
        "output; the 0.3 s figure is neither"
    )


def test_deploy_note_18_no_longer_defers_the_agent_measurement():
    text = PLAN_DOC.read_text()
    assert "docker stats --no-stream` during a turn before tightening." not in text, (
        "deploy note 18's 'measure before tightening' sentence must be replaced "
        "by the measurement itself"
    )
    assert "3.4× headroom" in text, (
        "deploy note 18 must carry the measured peak behind the agent's 768m cap"
    )


# --------------------------------------------------------------------------- #
# The post-merge closure handoff. The deploy runbook tells an operator how to
# DEPLOY a branch; the closure handoff separately records how to CLOSE the
# issues it fixes afterwards -- dispositions, exceptions and follow-ups.
# These pins are deliberately written to fail with a sentence rather than a
# StopIteration or an IndexError: every read is a plain `in`, every slice is
# guarded by an explicit `find() != -1`, and every assert carries a message.
# --------------------------------------------------------------------------- #

CLOSURE_HANDOFF = REPO_ROOT / "docs" / "plans" / "2026-09-04-issue-closure-handoff.md"

ISSUE_DISPOSITIONS = {
    "#20": "closes at merge",
    "#21": "close by hand with a stated carve-out",
    "#22": "closes at merge",
    "#23": "closes at merge",
    "#24": "closes at merge",
    "#25": "closes at merge",
    "#26": "closes after deploy verification",
    "#27": "close by hand with a stated carve-out",
}


def _closure_handoff_text() -> str:
    """Read the handoff, or fail with the path rather than a FileNotFoundError."""
    assert CLOSURE_HANDOFF.is_file(), (
        f"the post-merge closure handoff is missing: expected {CLOSURE_HANDOFF}. "
        "The deploy runbook covers deploying the branch; "
        "this is the closure layer on top of it."
    )
    return CLOSURE_HANDOFF.read_text()


def _section(text: str, heading: str) -> str:
    """The body under `heading`, up to the next heading of the same level.

    Returns "" when the heading is absent so the caller's assert reports the missing
    heading instead of raising.
    """
    start = text.find(heading)
    if start == -1:
        return ""
    level = heading.split(" ", 1)[0]
    rest = text[start + len(heading) :]
    end = rest.find(f"\n{level} ")
    return rest if end == -1 else rest[:end]


def test_closure_handoff_exists_and_names_its_reader():
    text = _closure_handoff_text()
    assert "no context" in text, (
        "the handoff must state that its reader is an independent agent with no context "
        "and no one to ask; that constraint is why it repeats rather than cross-references"
    )


def test_closure_handoff_carries_the_per_issue_disposition_table():
    """Each issue's Definition-of-Done clauses were graded once against a documented
    verdict; the handoff copies that verdict so a reader does not re-derive it
    (possibly differently).
    """
    text = _closure_handoff_text()
    missing = [
        f"{issue} -> {disposition!r}"
        for issue, disposition in ISSUE_DISPOSITIONS.items()
        if f"{issue}" not in text or disposition not in text
    ]
    assert not missing, (
        "the handoff's per-issue table must carry every issue and its disposition; "
        f"not found: {', '.join(missing)}"
    )
    for issue in ("#20", "#26"):
        assert f"| **{issue}**" in text, (
            f"{issue} must appear as a row of the disposition table (a `| **{issue}**` cell), "
            "not only as prose"
        )


def test_closure_handoff_gives_the_exact_closes_line_and_the_omissions():
    """#21, #26 and #27 must NOT ride the merge's `Closes` line."""
    text = _closure_handoff_text()
    assert "Closes #20, #22, #23, #24, #25" in text, (
        "the handoff must quote the exact `Closes` line the PR body carries "
        "(`Closes #20, #22, #23, #24, #25`) so it cannot be reconstructed from memory"
    )
    for omitted in ("#21", "#26", "#27"):
        assert f"Closes #20, #22, #23, #24, #25, {omitted}" not in text, (
            f"{omitted} must never appear on the `Closes` line -- it is hand-closed"
        )
    assert "must not be on the `Closes` line" in text, (
        "the handoff must say in so many words that the three omitted issues are kept off "
        "the `Closes` line, and why"
    )


def test_closure_handoff_does_not_claim_the_backfill_script_was_tested_locally():
    """One Definition-of-Done clause requires testing a backfill script locally, which
    no pre-merge work can satisfy.

    `scripts/backfill_slack_ts.py` makes outbound Slack calls for every candidate row and
    its CLI is `"--apply" in sys.argv`, so there is no offline mode and no `--help` probe.
    """
    text = _closure_handoff_text()
    assert "Do not claim the script was tested locally" in text, (
        "the handoff must carry the prohibition verbatim -- the script has never "
        "been run against the local copy, because it would fire real requests at slack.com"
    )
    assert "no `--help`" in text, (
        "the handoff must warn that `--help` is not a safe no-op probe: the CLI is "
        '`"--apply" in sys.argv`, so any invocation goes straight to a DB connect'
    )
    assert "23" in text and "28" in text, (
        "the handoff must state both NULL-slack_ts counts (23 on the local copy and 28 "
        "on the live count) and mark the live count authoritative"
    )


def test_closure_handoff_corrects_21s_zero_percent_coverage_premise():
    """#21's clause was met by declaring its premise false, not by raising coverage from 0."""
    text = _closure_handoff_text()
    assert "71.83" in text, (
        "#21's closing comment must quote the measured pre-fix coverage of worker/main.py "
        "(71.83 %), because the issue's 'from 0 % coverage' premise is false"
    )
    assert "77.72" in text, "#21's comment must quote the post-fix figure (77.72 %) beside it"


def test_closure_handoff_carries_the_three_overturned_rulings():
    """A reader who sees only the outcome will assume the plan was followed. It was not,
    three times, and each reversal has a measured reason that must travel with it."""
    text = _closure_handoff_text()
    for needle, why in (
        ("carries no proposal identity", "the reversal choosing option (b)"),
        ("re-opens phase-8 C2", "the rejection of pool_pre_ping on the probe engine"),
        ("thread_outcome_enum", "the parked-thread ruling choosing option (c)"),
    ):
        assert needle in text, (
            f"the handoff must record {why}; expected the phrase {needle!r} and did not "
            "find it"
        )


def test_closure_handoff_turns_every_follow_up_into_a_runnable_command():
    """A residual that is only prose is a residual nobody re-reads: every named
    follow-up needs its own runnable `gh issue create` line."""
    text = _closure_handoff_text()
    commands = text.count("gh issue create")
    assert commands >= 26, (
        "every named follow-up needs its own `gh issue create` line; "
        f"found only {commands}"
    )
    for fixed in ("F23", "F24"):
        section = _section(text, "## Step 8")
        assert section, "the handoff must have a `## Step 8` follow-up section"
        assert f"**{fixed}**" not in section or "FIXED" in section, (
            f"{fixed} was already fixed and must not be listed as an open follow-up"
        )


def test_closure_handoff_says_what_to_do_when_verification_fails():
    text = _closure_handoff_text()
    section = _section(text, "## Step 7")
    assert section, (
        "the handoff must have a `## Step 7` telling the reader what to do when a "
        "verification step fails"
    )
    assert "do not close" in section.lower(), (
        "Step 7 must say plainly: do not close the issue -- a closed issue stops being "
        "re-verified"
    )
