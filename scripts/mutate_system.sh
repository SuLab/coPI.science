#!/usr/bin/env bash
#
# Mutation check for the subsystems T1–T10 claim to protect: ORCID, PubMed/NCBI, the
# job-queue worker, the profile pipeline, the public graph, onboarding/impersonation/
# profile export, and the agent page.
#
# Each mutant must be KILLED — at least one test in the named selection must fail with it
# applied. A SURVIVING mutant means the suite does not actually test that behaviour,
# whatever its test names claim. Same discipline as scripts/mutate_cohorts.sh and
# scripts/mutate_slack_mirror.sh, and the same `~~` field delimiter, chosen there
# because one mutation target contains a `|` and kept here because several contain `~`-free
# SQL with pipes and quotes of both kinds.
#
# As of 2026-08-04 mutate_cohorts.sh shares this file's isolation strategy — it was
# converted from in-place editing to copy+provenance, and its 9/9 was re-measured under
# the new strategy and held. mutate_slack_mirror.sh was converted the same way once live
# Slack credentials became available (see its header).
#
# THE INERT MUTANTS ARE NOT OPTIONAL. Every tier below carries one edit that changes no
# behaviour (a docstring, a comment, a log string) and MUST SURVIVE. Without it a tier
# that is broken for any unrelated reason — a dead credential, a migrated-away column, a
# leftover row — scores 100% and looks sensitive when it is merely failing. Each tier's
# inert mutant is listed FIRST so a broken tier is detected before any money is spent on
# it.
#
# NOTHING IN THIS REPOSITORY IS EVER WRITTEN TO.
# Three agents previously applied mutations by editing src/ in place. A repo guard
# auto-reverted them mid-run and silently corrupted the results: mutants reported as
# SURVIVING would in fact have been killed. So this script copies the tree into a fresh
# 0700 mktemp directory OUTSIDE the repository, mutates the COPY, runs pytest from the
# host's .venv-test with the copy as its working directory, and proves — by importing
# `src` and checking `src.__file__` — that the copy is what is under test.
# `git diff --quiet -- src/` is asserted before the first mutant and after the last one.
#
# Run it ON THE HOST, not over sshfs (CLAUDE.md): through a FUSE mount the copy and every
# pytest run can be 100-400x slower. The images have no pytest, so there is no container
# path. With TEST_DATABASE_URL unset, tests/conftest.py starts its own testcontainers
# Postgres, exactly as scripts/ci.sh does.
#
# THE KILLING MUTATION OF EVERY TEST FIXED FOR VACUITY STAYS HERE (RCA §8 cause 3). Each
# `vac_*` tier selects only the one fixed test's node, because with a whole file and `-x`
# whichever test fails first counts as the kill — under M15 an older test in the same file
# already fails, so a whole-file tier would report "killed" whether or not the fixed test
# can see the mutation. `vacuity` is their union and holds only the inert control M13.
#
# Usage:
#   # offline tiers only (free, no third-party calls):
#   ./scripts/mutate_system.sh
#
#   # + the live ORCID / NCBI tiers (free, but real HTTP):
#   LIVE_API_TESTS=1 ./scripts/mutate_system.sh
#
#   # + the profile-pipeline tier (real Anthropic tokens, ~7 calls total):
#   LIVE_API_TESTS=1 ANTHROPIC_API_KEY=sk-ant-... ./scripts/mutate_system.sh
#
#   # the live ORCID / NCBI tiers only, with nothing that can spend Anthropic tokens:
#   LIVE_API_TESTS=1 ANTHROPIC_API_KEY=sk-ant-dummy-no-spend \
#     MUT_TIERS="orcid pubmed_tool pubmed_doi pubmed_both" ./scripts/mutate_system.sh
#
# A tier whose credentials are absent, or that MUT_TIERS leaves out, is reported as
# `skipped`, never as `killed`. A selection that hangs past MUT_TIMEOUT is an ERROR, never
# a kill, and so is any pytest exit other than 0 (passed) or 1 (tests failed): a usage
# error, an interrupted collection or a `-k` that selects nothing names no failing test.
# Exit 1 is a kill only when the log carries a `FAILED ` summary line; exit 1 without one
# (a setup/fixture ERROR, a failed `cd`, a failed log redirect) is an ERROR too.
#
# Cost control: every mutant runs against ONLY the test file (and often only the single
# test) that is supposed to kill it, never the whole suite. That is what keeps the
# Anthropic spend at ~7 calls and the NCBI traffic inside the 3 req/s anonymous policy.
#
# MEASURED 2026-08-04, offline tiers only, no credentials present:
#
#   killed 6/6 real mutants        M4, M5 (worker); M7 (graph); M8, M9 (onboarding);
#                                  M10 (agentpage)
#   inert controls 4/4 survived    M12c, M12e, M12f, M12g
#   9 skipped for credentials      orcid: M12a, M1, M1b
#                                  pubmed: M12b, M2, M3
#                                  pipeline: M12d, M6, M6b
#   src/ clean, exit 0
#
# NO REAL MUTANT SURVIVED ANY TIER THAT COULD BE RUN. The list below is therefore not a
# list of survivors; it is the standing record for the two mutants this script's own
# tiers cannot judge without credentials, plus the resolution of one that used to survive.
# Do not weaken any of them.
#
#   M1b  UNMEASURED as of 2026-08-04 — its tier (orcid) needs LIVE_API_TESTS=1, which was
#        not available, so it reported `skipped`. It was last measured as SURVIVING on
#        2026-07-31 and nothing has changed tests/live_api/test_orcid_live.py since, so
#        treat it as still open: fetch_orcid_profile hardcoded to "Josiah Carberry"
#        survives that file. Its only defence against a constant name is the dated
#        `"Carberry" in prof["name"]` assertion, which a hardcode of the expected value
#        satisfies. Nothing in the live tier compares the parsed name against the record
#        it came from, and the docstring's claimed control ("the parser must NOT return
#        the same thing for a different id") is not implemented — the id it checks is
#        copied from the argument, not parsed. Measured then: the PRE-EXISTING contract
#        test tests/contract/test_orcid_contract.py::
#        test_fetch_orcid_profile_falls_back_to_orcid_when_no_name DOES kill it, so this
#        is a gap in the new tier rather than in the repo. Re-run with LIVE_API_TESTS=1
#        before claiming it either way.
#
#   M6   RESOLVED 2026-08-04. _validate_profile hardwired to `return True` is now KILLED
#        by tests/characterization/test_profile_pipeline_gm.py (3 failed:
#        stores_the_retry_not_the_rejected_first_synthesis,
#        marks_a_profile_that_fails_validation_twice,
#        rerun_that_fails_validation_keeps_the_stored_profile), and M6b (always False) by
#        7 — so the validator's effect is now visible in BOTH directions, which it was
#        not before. An inert docstring edit on the same function survived the same
#        selection (11 passed), so those kills are the mutation and not a red suite.
#        Fix 2 (d311170) is what closed it: the return value now gates step 9 instead of
#        being computed and discarded. The old note here claimed M6 survived the entire
#        offline suite; that stopped being true and nothing updated it.
#
#        CAVEAT, so this is not misread: the kill comes from the OFFLINE characterization
#        file, not from this script's `pipeline` tier, which is
#        tests/integration/test_profile_pipeline_live.py and still needs LIVE_API_TESTS=1
#        plus ANTHROPIC_API_KEY. M6/M6b therefore still report `skipped` in a
#        credential-free run — see the 2026-08-04 measurement above. The evidence was
#        produced with this script's own copy+provenance pattern (tree copied into the
#        container, `src.__file__` asserted under the copy, the mutated module
#        import-checked), not by editing src/. If you want this harness to see M6 by
#        itself, the characterization file has to join a tier whose CREDS are "".
#
# 2026-08-04, the Slack chokepoint mutants — 8515f65's control was a PROVENANCE ARTIFACT.
# That commit recorded, as the most important line in its report, that the four
# chokepoint mutants "run against the offline selection ALL SURVIVED, at exactly 1093",
# and flagged the suspiciously round figure as needing reproduction before belief. It has
# now been reproduced, and the claim does not hold: against
# tests/unit/test_slack_client_contract.py + tests/unit/test_transport.py (81 passed
# unmutated) all four are KILLED, with an inert docstring edit on the same file surviving.
# The pagination mutant was additionally run against the WHOLE offline suite, which is the
# selection the 1093 figure came from: 9 failed / 1158 passed / 120 skipped, the same 9
# tests. So the full suite kills it too — and today's collection is 1158, not 1093, which
# is a second reason that figure describes a run that was not measuring what it claimed.
#
#   pagination            _paginate returns after page 1  ...............  9 failed
#   ts-ordering           the pre-fix list(reversed(...)) in
#                         _conversation_messages  ........................  4 failed
#                         (the stronger `key=_by_ts, reverse=True` variant:  7 failed)
#   splitting             split_for_slack never splits (`if True: return [text]`)  7 failed
#   thread_ts normalise   normalize_inbound_message's self-reference test dead  5 failed
#   INERT control         _conversation_messages docstring reworded  ....  survived
#
# So Fix 4's offline tests do protect all four mechanisms; the earlier "all survived" was
# the failure mode this script's provenance check exists to catch — unmutated code under
# test, every mutant falsely surviving. Which is also why the exact-1093 count was the
# tell: a selection that never loaded the mutant cannot move.
#
# Overridable env:
#   MUT_PYTHON          interpreter with pytest (default: ./.venv-test/bin/python, the
#                       host venv; create it ON THE HOST as CLAUDE.md describes)
#   TEST_DATABASE_URL   throwaway asyncpg DSN (default: unset, so conftest starts an
#                       ephemeral testcontainers Postgres); refused if it names `copi`
#   MUT_TIERS           space-separated allow-list of tier names (default: every tier)
#   MUT_TIMEOUT         seconds per pytest run before it is killed and scored ERROR
#                       (default: 900)
#   MUTSYS_COPY_DIR     where the mutated tree lives (default: a fresh mktemp dir); must
#                       be absent or empty, outside the repository, not a symlink and
#                       owned by you; it is forced to 0700
#   MUTSYS_LOGDIR       where per-mutant pytest logs are kept (default: a mktemp dir);
#                       same symlink/owner checks, forced to 0700
#   MUTSYS_KEEP_COPY    set to 1 to leave the mutated tree behind for inspection
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT=$(pwd -P)

PY="${MUT_PYTHON:-$PWD/.venv-test/bin/python}"
[ -x "$PY" ] || { echo "ERROR: $PY missing — create .venv-test ON THE HOST (CLAUDE.md); the images have no pytest" >&2; exit 1; }
case "$PY" in /*) ;; *) PY="$ROOT/$PY" ;; esac  # pytest runs from the copy, not from here
MUT_TIMEOUT="${MUT_TIMEOUT:-900}"

# An operator-supplied directory is accepted only if it is a real directory (not a
# symlink, checked before anything resolves it) owned by the current user, and is then
# forced to 0700: `mkdir -p -m 0700` sets no mode on a directory that already exists, so
# a pre-made 0755 copy directory would otherwise hold a world-readable copy of `.env`.
# $1 = the variable's name (for messages), $2 = its path, $3 = "empty" to also require
# that the directory be absent or empty.
secure_override_dir() {
  local d="$2"
  while [ "$d" != "/" ] && [ "${d%/}" != "$d" ]; do d="${d%/}"; done
  if [ -L "$d" ]; then
    echo "ERROR: $1=$2 is a symlink; refusing it." >&2; return 1
  fi
  if [ "${3:-}" = "empty" ] && [ -e "$d" ] && [ -n "$(ls -A -- "$d" 2>/dev/null)" ]; then
    echo "ERROR: $1=$2 exists and is not empty; refusing to reuse it." >&2; return 1
  fi
  mkdir -p -m 0700 -- "$d" || { echo "ERROR: cannot create $1=$2" >&2; return 1; }
  if [ -L "$d" ] || [ ! -d "$d" ] || [ ! -O "$d" ]; then
    echo "ERROR: $1=$2 is not a directory owned by $(id -un); refusing it." >&2; return 1
  fi
  chmod 0700 -- "$d" || { echo "ERROR: cannot chmod 0700 $1=$2" >&2; return 1; }
}

# The log dir is created, not assumed, because MUTSYS_LOGDIR is documented as overridable
# and this script once did not create it. Measured 2026-08-04: pass a path that does not
# exist and every `>"$log"` redirect fails, which the shell scored as a nonzero exit —
# i.e. as a KILL — for every mutant, inert controls included. The run reported "killed
# 6/6 real mutants" and "inert controls: 0/4 survived", and only that second line
# distinguished it from a perfect score. Any earlier run of this script made with
# MUTSYS_LOGDIR set to a nonexistent directory reported every mutant as killed and is
# void. (A failed redirect can no longer score a kill anyway: a kill now needs a FAILED
# line in the log.)
if [ -n "${MUTSYS_LOGDIR:-}" ]; then
  LOGDIR="$MUTSYS_LOGDIR"
  secure_override_dir MUTSYS_LOGDIR "$LOGDIR" || exit 1
else
  LOGDIR=$(mktemp -d) || { echo "ERROR: mktemp failed for the log dir" >&2; exit 1; }
fi

# Deliberately NOT the live database, and asserted rather than assumed: several of these
# suites commit (the worker tests need another connection to see the write, so they cannot
# use the rolled-back session fixture). Unset or empty means conftest's own throwaway
# Postgres (tests/conftest.py treats an empty value as unset). The database name is parsed
# rather than pattern-matched, so `.../copi/`, `.../copi#x` and `.../copi?x` are all caught.
if [ -n "${TEST_DATABASE_URL:-}" ]; then
  # Every database name the URL could select: the path's first segment, and any
  # `database=`/`dbname=` query parameter the driver might honour. The DSN reaches
  # Python through the environment, never argv, so `ps` does not show it.
  dbnames=$(TEST_DATABASE_URL="$TEST_DATABASE_URL" "$PY" -c 'import os; from urllib.parse import parse_qs, unquote, urlsplit; u = urlsplit(os.environ["TEST_DATABASE_URL"]); q = parse_qs(u.query); print("\n".join([unquote(u.path.strip("/").split("/")[0])] + [v for k in ("database", "dbname") for v in q.get(k, [])]))') \
    || { echo "ERROR: cannot parse TEST_DATABASE_URL" >&2; exit 1; }
  if printf '%s\n' "$dbnames" | grep -qx copi; then
    echo "ERROR: TEST_DATABASE_URL points at the live 'copi' database. These suites" >&2
    echo "commit. Use a throwaway database." >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Rule 1: the working tree is never touched. Check before, and again at the end.
# ---------------------------------------------------------------------------
if ! git diff --quiet -- src/; then
  echo "ERROR: src/ has uncommitted changes." >&2
  echo "This script does not edit src/ — it mutates a copy outside the repository — but a" >&2
  echo "dirty tree means the copy would carry changes that are not the mutant, so every" >&2
  echo "result below would be unattributable. Commit or stash first." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Tiers: the selection each mutant is judged against, and what it costs.
#
# CREDS: "" = offline; "live" = needs LIVE_API_TESTS=1; "live+llm" = also real Anthropic.
# Every tier needs a TIER_CREDS entry: under `set -u` a missing key aborts the run.
# The vac_i23_* tiers name one parametrization of test_floor_arithmetic by its explicit
# pytest.param id. The node is single-quoted inside the string because `sh -c` would
# otherwise treat its `[...]` as a glob.
# ---------------------------------------------------------------------------
declare -A TIER_SELECT=(
  [orcid]="tests/live_api/test_orcid_live.py"
  [pubmed_tool]="tests/live_api/test_pubmed_live.py -k test_ncbi_get_sends_the_required_tool_and_email_parameters"
  [pubmed_doi]="tests/live_api/test_pubmed_live.py -k test_reconcile_pub_doi_separates_a_real_match_from_a_near_miss"
  [pubmed_both]="tests/live_api/test_pubmed_live.py -k 'test_ncbi_get_sends_the_required_tool_and_email_parameters or test_reconcile_pub_doi_separates_a_real_match_from_a_near_miss'"
  [worker]="tests/integration/test_worker.py"
  [pipeline]="tests/integration/test_profile_pipeline_live.py -k test_t41_one_real_orcid_becomes_a_stored_profile_grounded_in_its_works"
  [graph]="tests/integration/test_public_graph.py"
  [onboarding]="tests/integration/test_onboarding_flow.py"
  [agentpage]="tests/integration/test_agent_page.py"
  [vac_i20]="tests/unit/test_reply_lane.py::test_thread_lock_then_agent_lock_does_not_deadlock_against_an_agent_lock_only_caller"
  [vac_i23_route]="'tests/unit/test_specialist_floor.py::test_floor_arithmetic[route-to-incubation-armed-owes-pair]'"
  [vac_i23_pass]="'tests/unit/test_specialist_floor.py::test_floor_arithmetic[pass-armed-exempt]'"
  [vac_c2]="tests/unit/test_consult_accounting.py::test_a_consults_own_truncation_retry_is_booked"
  [vac_i21]="tests/unit/test_roles.py::test_conclude_prompt_asks_for_the_inline_verdict_and_keeps_scores_in_the_sidecar"
  [vac_i28]="tests/unit/test_delegates.py::TestDelegateInvitation::test_default_status"
  [vac_i29]="tests/integration/test_concurrent_web_writes.py::test_concurrent_proposal_reviews_do_not_500"
  [vac_i24b]="tests/integration/test_cohort_admin.py::test_pi_facing_thread_view_is_never_cohort_filtered"
  [vacuity]="tests/unit/test_reply_lane.py::test_thread_lock_then_agent_lock_does_not_deadlock_against_an_agent_lock_only_caller 'tests/unit/test_specialist_floor.py::test_floor_arithmetic[route-to-incubation-armed-owes-pair]' 'tests/unit/test_specialist_floor.py::test_floor_arithmetic[pass-armed-exempt]' tests/unit/test_consult_accounting.py::test_a_consults_own_truncation_retry_is_booked tests/unit/test_roles.py::test_conclude_prompt_asks_for_the_inline_verdict_and_keeps_scores_in_the_sidecar tests/unit/test_delegates.py::TestDelegateInvitation::test_default_status tests/integration/test_concurrent_web_writes.py::test_concurrent_proposal_reviews_do_not_500 tests/integration/test_cohort_admin.py::test_pi_facing_thread_view_is_never_cohort_filtered"
)
declare -A TIER_CREDS=(
  [orcid]="live"       [pubmed_tool]="live"  [pubmed_doi]="live"  [pubmed_both]="live"
  [worker]=""          [pipeline]="live+llm" [graph]=""           [onboarding]=""
  [agentpage]=""
  [vac_i20]=""         [vac_i23_route]=""    [vac_i23_pass]=""    [vac_c2]=""
  [vac_i21]=""         [vac_i28]=""          [vac_i29]=""         [vac_i24b]=""
  [vacuity]=""
)

# MUT_TIERS (D18): an allow-list, so the live ORCID/NCBI tiers can run without `pipeline`,
# the one tier that spends Anthropic calls. An unknown name is refused rather than
# silently skipping everything.
if [ -n "${MUT_TIERS:-}" ]; then
  for t in $MUT_TIERS; do
    [ -n "${TIER_SELECT[$t]+x}" ] || { echo "ERROR: MUT_TIERS names unknown tier '$t'" >&2; exit 1; }
  done
fi

# tier ~~ file ~~ exact source substring ~~ replacement ~~ label
#
# `\n` in the FROM/TO fields is a newline (see the applier below). Entries containing a
# double quote, a backtick or a `$` are single-quoted here; the others are double-quoted.
# No entry needs both kinds of quote. tests/unit/test_mutation_harness_targets.py checks
# that every target occurs exactly once and that every tier has an inert control.
MUTANTS=(
# --- ORCID (T1) ------------------------------------------------------------------------
'orcid~~src/services/orcid.py~~    """Extract name, affiliation, and email from ORCID record."""~~    """Extract the name, affiliation and email from an ORCID record. [INERT EDIT]"""~~M12a INERT docstring — MUST SURVIVE'
'orcid~~src/services/orcid.py~~    result["name"] = f"{given} {family}".strip() or orcid_id~~    result["name"] = "Ada Lovelace"~~M1 fetch_orcid_profile returns a constant name instead of parsing person.name'
# M1b is the same defect as M1 with the constant chosen to equal today's expected value.
# It is the difference between "the test reads the record" and "the test restates the
# answer". SURVIVES the live tier (see M1b in the header above).
'orcid~~src/services/orcid.py~~    result["name"] = f"{given} {family}".strip() or orcid_id~~    result["name"] = "Josiah Carberry"~~M1b the same hardcode, set to the value the test pins (probes whether the assertion is derived from the live record or merely restated)'
# --- PubMed / NCBI (T2) ----------------------------------------------------------------
'pubmed_both~~src/services/pubmed.py~~    """Make a rate-limited, identified GET request to NCBI, with retry.~~    """Make a rate-limited, identified GET request to NCBI E-utilities, with retry. [INERT EDIT]~~M12b INERT docstring — MUST SURVIVE'
"pubmed_doi~~src/services/pubmed.py~~    if assigned.lower() == auth.lower():~~    if True:~~M2 reconcile_pub_doi always reports a match, so a PMID keeps whatever DOI it arrived with"
'pubmed_tool~~src/services/pubmed.py~~    params.setdefault("tool", _NCBI_TOOL)~~    pass  # tool= no longer sent~~M3 _ncbi_get stops identifying itself to NCBI (throttle, then IP block)'
# --- worker (T5) -----------------------------------------------------------------------
'worker~~src/worker/main.py~~    logger.info("Job %s completed", job.id)~~    logger.info("Job %s has completed", job.id)~~M12c INERT log string — MUST SURVIVE'
"worker~~src/worker/main.py~~        .with_for_update(skip_locked=True)~~        .with_for_update()~~M4 claim_job drops SKIP LOCKED, so a worker pool serialises behind the slowest job"
'worker~~src/worker/main.py~~            if job.type == "generate_profile":~~            job.status = "completed"; job.completed_at = datetime.now(timezone.utc); await db.commit()\n            if job.type == "generate_profile":~~M5 the job is marked completed and committed BEFORE the work is dispatched'
# --- profile pipeline (T4) -------------------------------------------------------------
"pipeline~~src/services/profile_pipeline.py~~    Validate synthesized profile fields.~~    Validate the synthesized profile fields. [INERT EDIT]~~M12d INERT docstring — MUST SURVIVE"
"pipeline~~src/services/profile_pipeline.py~~def _validate_profile(profile: dict[str, Any]) -> bool:~~def _validate_profile(profile: dict[str, Any]) -> bool:\n    return True~~M6 _validate_profile always returns True, so no profile is ever rejected"
"pipeline~~src/services/profile_pipeline.py~~def _validate_profile(profile: dict[str, Any]) -> bool:~~def _validate_profile(profile: dict[str, Any]) -> bool:\n    return False~~M6b the same function always returns False — the paired control for M6, which shows whether the tier can see validation's effect in EITHER direction"
# --- public graph (T9) -----------------------------------------------------------------
"graph~~src/routers/public.py~~            -- The agent-only proposal for a thread is the FIRST one the bots~~            -- [INERT EDIT] the agent-only proposal for a thread is the FIRST one the bots~~M12e INERT SQL comment inside the mutated query — MUST SURVIVE"
"graph~~src/routers/public.py~~                  AND origin_visibility = 'public'\n                  AND decided_at >= :decided_floor{window_end_clause}~~                  AND decided_at >= :decided_floor{window_end_clause}~~M7 the pairs CTE stops filtering on origin_visibility, so collab_private proposals reach the public graph"
# --- onboarding / impersonation / profile export (T7) ----------------------------------
"onboarding~~src/dependencies.py~~    # Impersonation: admin can view as another user~~    # Impersonation [INERT EDIT]: an admin can view the site as another user~~M12f INERT comment — MUST SURVIVE"
"onboarding~~src/dependencies.py~~    if impersonate_id and session_user.is_admin:~~    if impersonate_id:~~M8 copi-impersonate is honoured for non-admins — any logged-in user can become any other user"
'onboarding~~src/services/profile_export.py~~    path = PROFILES_DIR / f"{agent_id}.md"~~    if profile.private_profile_md:\n        lines.append(profile.private_profile_md)\n    path = PROFILES_DIR / f"{agent_id}.md"~~M9 the PUBLIC profile export appends private_profile_md'
# --- agent page (T8) -------------------------------------------------------------------
'agentpage~~src/routers/agent_page.py~~            "Ignoring duplicate reopen of proposal %s by %s "~~            "Ignoring a duplicate reopen of proposal %s by %s "~~M12g INERT log string — MUST SURVIVE'
"agentpage~~src/routers/agent_page.py~~    if already_reviewed is not None:~~    if False:~~M10 the reopen idempotency guard is gone, so a replayed POST migrates the thread twice"
# --- vacuity (RCA §8 cause 3): the killing mutation of every test fixed for vacuity ----
# Each real mutant is judged against ONLY its fixed test's node (see the header). M13 is
# the shared inert control, run against the union of those nodes.
'vacuity~~src/agent/locks.py~~"""Per-key asyncio locks with deadlock-free multi-acquire.~~"""Per-key asyncio locks with deadlock-free multi-acquire. [INERT EDIT]~~M13 INERT docstring — MUST SURVIVE'
"vac_i20~~src/agent/locks.py~~                await lock.acquire()~~                if lock.locked(): await asyncio.Event().wait()\n                await lock.acquire()~~M14 I20/E2 a contended acquire_all waits forever, so the thread-lock-then-agent-lock path deadlocks"
'vac_i23_route~~src/agent/specialists.py~~PANEL_EXEMPT_RECOMMENDATIONS: frozenset[str] = frozenset({"pass"})~~PANEL_EXEMPT_RECOMMENDATIONS: frozenset[str] = frozenset({"pass", "route-to-incubation"})~~M15 I23/E3 route-to-incubation is exempted from the specialist floor again'
'vac_i23_pass~~src/agent/specialists.py~~PANEL_EXEMPT_RECOMMENDATIONS: frozenset[str] = frozenset({"pass"})~~PANEL_EXEMPT_RECOMMENDATIONS: frozenset[str] = frozenset()~~M16 I23 a pass verdict owes a panel too, so no recommendation is exempt'
"vac_c2~~src/agent/tools.py~~            on_retry=on_api_call,~~            on_retry=None,~~M17 C2/R3 a consult's own max_tokens retry is not booked as an API call"
'vac_i21~~prompts/roles/scout_hub/phase4-thread-reply.md~~Only your inline verdict also appears in `<slack_message>`~~None of it may appear anywhere in `<slack_message>`~~M18 I21/E7 the phase-4 prompt forbids the inline verdict again (the pre-df4d975 sentence)'
'vac_i28~~src/models/delegate.py~~String(20), nullable=False, default="pending"~~String(20), nullable=False, default="accepted"~~M19 I28/E14 a new delegate invitation defaults to accepted'
'vac_i29~~src/routers/agent_page.py~~        # the SELECT guard above answers a sequential duplicate.\n        await db.rollback()\n        return RedirectResponse(\n            url=f"/agent/{agent_id}/dashboard", status_code=302\n        )~~        # the SELECT guard above answers a sequential duplicate.\n        await db.rollback()\n        from fastapi.responses import Response\n        return Response(status_code=500)~~M20 I29/R2 review_proposal answers a lost race with a 500'
"vac_i24b~~src/services/directory.py~~    root_posts = roots_result.scalars().all()~~    root_posts = []~~M21 I24b/R1 the discussions view lists no threads"
)

# ---------------------------------------------------------------------------
# Build the mutable copy OUTSIDE the repository and PROVE it is what runs.
#
# The copy directory is guarded before the tar and before every `rm -rf`. If mktemp
# failed, COPY would be empty, bash's `cd ""` would succeed as a no-op, `pwd -P` would
# return the repo root, the mutants would land in the live tree and the EXIT trap would
# delete it. Physical paths are compared with physical paths.
# ---------------------------------------------------------------------------
copy_is_safe() {
  case "$COPY" in
    ""|/) echo "ERROR: unsafe copy directory '${COPY}'" >&2; return 1 ;;
    /*) ;;
    *) echo "ERROR: copy directory '$COPY' is not absolute" >&2; return 1 ;;
  esac
  case "$COPY/" in
    "$ROOT"/*) echo "ERROR: copy directory $COPY is the repository or inside it ($ROOT)" >&2; return 1 ;;
  esac
  case "$ROOT/" in
    "$COPY"/*) echo "ERROR: copy directory $COPY contains the repository ($ROOT)" >&2; return 1 ;;
  esac
  return 0
}

if [ -n "${MUTSYS_COPY_DIR:-}" ]; then
  COPY="$MUTSYS_COPY_DIR"
  secure_override_dir MUTSYS_COPY_DIR "$COPY" empty || exit 1
else
  COPY=$(mktemp -d "${TMPDIR:-/tmp}/mutsys.XXXXXX") || { echo "ERROR: mktemp failed" >&2; exit 1; }
fi
[ -n "$COPY" ] || { echo "ERROR: empty copy directory" >&2; exit 1; }
COPY=$(cd -- "$COPY" && pwd -P) || { echo "ERROR: cannot resolve the copy directory" >&2; exit 1; }
copy_is_safe || exit 1

# INT and TERM are trapped too, so an interrupted run still removes the copy (which holds
# `.env`). A signal handler cleans up, disarms the EXIT trap and exits 128+signal; the
# `cleaned` flag makes a second call a no-op either way.
cleaned=0
cleanup() {
  [ "$cleaned" -eq 1 ] && return 0
  cleaned=1
  if [ "${MUTSYS_KEEP_COPY:-0}" = "1" ]; then
    echo "(left the mutated tree at ${COPY} — MUTSYS_KEEP_COPY=1)"
  elif copy_is_safe; then
    rm -rf -- "$COPY"
  fi
}
on_signal() { cleanup; trap - EXIT; exit "$1"; }
trap cleanup EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
trap 'on_signal 129' HUP

echo "building a throwaway copy of the tree at ${COPY} (the repo is never written to)"
copy_is_safe || exit 1
# backups/ holds the production dumps (RCA S1). .env stays in, for parity with ci.sh:
# Settings reads a cwd-relative .env, and the copy is a 0700 directory removed on EXIT,
# INT or TERM.
if ! tar -C "$ROOT" \
      --exclude=./.git --exclude=./.venv-test --exclude=./backups --exclude=./logs \
      --exclude=./mutants --exclude=./build --exclude=./.hypothesis --exclude=./.pytest_cache \
      --exclude=./.ruff_cache --exclude=./.playwright-mcp --exclude=__pycache__ \
      -cf - . | tar -C "$COPY" -xf -; then
  echo "ERROR: could not copy $ROOT into $COPY." >&2
  exit 1
fi

# Provenance. If `import src` from the copy resolved anywhere else (an editable install of
# the repo, a stray site-packages copy), the run would exercise unmutated code and report
# every mutant as SURVIVED. That is the failure mode this whole script exists to avoid, so
# it is asserted rather than assumed.
prov=$(cd "$COPY" && "$PY" -c 'import src; print(src.__file__)' 2>/dev/null)
case "$prov" in
  "$COPY"/src/__init__.py) echo "provenance OK: pytest will import $prov" ;;
  *)
    echo "ERROR: from $COPY, 'import src' resolves to '${prov:-<nothing>}', not" >&2
    echo "$COPY/src/__init__.py. The mutants would not be under test. Refusing to run." >&2
    exit 1 ;;
esac

echo "logs: $LOGDIR"
echo

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
fail=0 killed=0 survived=0 skipped=0 skipped_tiers=0 broken_inert=0 inert_ok=0 errors=0 n=0
declare -a SURVIVORS=()

for m in "${MUTANTS[@]}"; do
  tier="${m%%~~*}"; rest="${m#*~~}"
  file="${rest%%~~*}"; rest="${rest#*~~}"
  from="${rest%%~~*}"; rest="${rest#*~~}"
  to="${rest%%~~*}"; label="${rest#*~~}"
  n=$((n + 1))
  short="${label%% *}"

  inert=0; [[ "$label" == *INERT* ]] && inert=1
  select="${TIER_SELECT[$tier]}"
  creds="${TIER_CREDS[$tier]}"

  # --- tier filter (MUT_TIERS) ---------------------------------------------------------
  if [ -n "${MUT_TIERS:-}" ] && [[ " $MUT_TIERS " != *" $tier "* ]]; then
    echo "skipped   $label  (MUT_TIERS)"; skipped_tiers=$((skipped_tiers + 1)); continue
  fi

  # --- credentials gate: a tier we cannot run is `skipped`, never `killed` -------------
  # The host environment is inherited; these entries only pin what the tier depends on.
  envargs=()
  # Inherited through the environment, never passed on `env`'s argv, where `ps` shows it.
  [ -z "${TEST_DATABASE_URL:-}" ] || export TEST_DATABASE_URL
  case "$creds" in
    live|live+llm)
      if [ -z "${LIVE_API_TESTS:-}" ]; then
        echo "skipped   $label  (needs LIVE_API_TESTS=1)"; skipped=$((skipped + 1)); continue
      fi
      envargs+=("LIVE_API_TESTS=$LIVE_API_TESTS") ;;
  esac
  if [ "$creds" = "live+llm" ]; then
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
      echo "skipped   $label  (needs ANTHROPIC_API_KEY — this tier spends real tokens)"
      skipped=$((skipped + 1)); continue
    fi
    export ANTHROPIC_API_KEY  # inherited, never on argv
  fi

  # --- apply the mutation to the COPY --------------------------------------------------
  if ! FROM="$from" TO="$to" "$PY" - "$COPY/$file" <<'PY' 2>&1
import os, pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
frm = os.environ["FROM"].replace("\\n", "\n")
to = os.environ["TO"].replace("\\n", "\n")
if frm not in s:
    sys.stderr.write(f"mutation target not found in {p}:\n{frm!r}\n"); sys.exit(1)
if s.count(frm) != 1:
    sys.stderr.write(f"target occurs {s.count(frm)} times in {p}; it must be unique\n")
    sys.exit(1)
p.write_text(s.replace(frm, to, 1))
PY
  then
    echo "ERROR     $label — target string not found (or not unique); the code moved," >&2
    echo "          fix this script rather than the test." >&2
    fail=1; errors=$((errors + 1))
    cp -- "$file" "$COPY/$file" >/dev/null 2>&1
    continue
  fi

  log="$LOGDIR/$(printf '%02d' "$n")-${short}.log"
  # -x: stop at the first failure. The killer's name is what the report needs, and a
  # narrow selection plus a per-tier inert control is what makes attributing it sound.
  # -rfE: the summary lists `FAILED <node>` for failed tests (the kill evidence) and
  # `ERROR <node>` for setup/collection errors (diagnostics only, never a kill).
  # timeout bounds a hung mutant (the repo has no pytest-timeout); `exec` makes the kill
  # reach pytest itself rather than only `sh`.
  (cd "$COPY" && env ${envargs[@]+"${envargs[@]}"} timeout -k 30 "$MUT_TIMEOUT" \
     sh -c "exec \"$PY\" -m pytest $select -q -x -rfE -p no:cacheprovider") >"$log" 2>&1
  rc=$?
  case "$rc" in
    0)
      if [ "$inert" -eq 1 ]; then
        echo "survived (expected)  $label"
        killed=$((killed + 1)); inert_ok=$((inert_ok + 1))
      else
        echo "SURVIVED  $label"
        SURVIVORS+=("$label")
        survived=$((survived + 1)); fail=1
      fi ;;
    1)
      # Exit 1 is a kill only if a test FAILED. It also comes from a setup or fixture
      # ERROR under -x (testcontainers/Docker in the DB-backed tiers), a failed `cd`, or
      # a failed `>"$log"` redirect — none of which names a failing test.
      killer=$(grep -m1 '^FAILED ' "$log" 2>/dev/null | sed 's/^FAILED //')
      if [ -z "$killer" ]; then
        echo "ERROR     $label — pytest exit 1 with no FAILED line (setup/collection ERROR," >&2
        echo "          failed cd, or failed log redirect) names no failing test; see $log" >&2
        fail=1; errors=$((errors + 1))
      elif [ "$inert" -eq 1 ]; then
        echo "KILLED AN INERT MUTANT  $label" >&2
        echo "          -> $killer" >&2
        echo "          This tier is failing for a reason that is NOT the mutation, so" >&2
        echo "          every other number for it is meaningless." >&2
        broken_inert=$((broken_inert + 1)); fail=1
      else
        echo "killed    $label"
        echo "          by $killer"
        killed=$((killed + 1))
      fi ;;
    124|137)
      # Never a kill, inert or real: a kill must name a failing test.
      echo "ERROR     $label — TIMEOUT after ${MUT_TIMEOUT}s (exit $rc); see $log" >&2
      fail=1; errors=$((errors + 1)) ;;
    *)
      # 2 interrupted (e.g. a collection error), 3 internal error, 4 usage error,
      # 5 no tests collected: none of them names a failing test, so none is a kill.
      echo "ERROR     $label — pytest exit $rc names no failing test; see $log" >&2
      fail=1; errors=$((errors + 1)) ;;
  esac

  # --- restore the copy from the repository, and verify it -----------------------------
  cp -- "$file" "$COPY/$file" >/dev/null 2>&1
  if ! cmp -s -- "$file" "$COPY/$file"; then
    echo "ERROR: $COPY/$file no longer matches $file; the copy is polluted and" >&2
    echo "every result after this point is unattributable. Stopping." >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# The working tree must be exactly as we found it.
# ---------------------------------------------------------------------------
if ! git diff --quiet -- src/; then
  echo >&2
  echo "ERROR: src/ is dirty. This script never writes to src/, so something else did." >&2
  echo "Inspect 'git diff -- src/' before doing anything else." >&2
  exit 1
fi

real_killed=$((killed - inert_ok))
real_total=$((real_killed + survived))
echo
echo "killed ${real_killed}/${real_total} real mutants"
echo "inert controls: ${inert_ok}/$((inert_ok + broken_inert)) survived (all of them must)"
echo "${errors} errors (target moved, timeout, or no failing test named)"
echo "${skipped} skipped for missing credentials, ${skipped_tiers} skipped by MUT_TIERS"
echo "src/ clean: yes"

if [ "$broken_inert" -gt 0 ]; then
  echo >&2
  echo "AN INERT MUTANT WAS KILLED. Read nothing else in this run as a score: the tier it" >&2
  echo "belongs to is red for an unrelated reason, which makes a broken suite look" >&2
  echo "maximally sensitive. Fix that first, then re-run." >&2
fi
if [ "${#SURVIVORS[@]}" -gt 0 ]; then
  echo >&2
  echo "SURVIVING MUTANTS — each is a behaviour the suite does not protect:" >&2
  for s in "${SURVIVORS[@]}"; do echo "  - $s" >&2; done
  echo "Add the test that kills it. Do not weaken the mutant." >&2
fi
if [ "$fail" -eq 0 ]; then
  echo "every judged mutant was killed and every inert control survived"
fi
exit "$fail"
