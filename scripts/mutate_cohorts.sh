#!/usr/bin/env bash
#
# Mutation check for the cohort gate. Each mutant must be KILLED — at least one test
# must fail with it applied. A SURVIVING mutant means the suite does not actually test
# that behaviour, whatever its test names claim.
#
# This exists because four cohort tests were written that structurally could not fail,
# and each hid something. Two of the mutants below are the real defects those tests
# missed (M2, M6): both were found by a real multi-turn run, not by the suite. Running
# this after adding a cohort test is how you find that out in seconds instead.
#
# Offline: runs only the non-real_llm tests, so no API key and no spend.
#
# NOTHING IN THIS REPOSITORY IS EVER WRITTEN TO.
# Until 2026-08-04 this script mutated src/ IN PLACE and restored from a `.mutbak`
# copy — the same strategy scripts/mutate_system.sh documents as having been
# auto-reverted mid-run by a repo guard, silently corrupting three earlier agents'
# results (mutants reported as SURVIVING would in fact have been killed). It now uses
# mutate_system.sh's strategy instead, so the two harnesses share one isolation model:
# copy the tree into a fresh 0700 mktemp directory OUTSIDE the repository, mutate the
# COPY, run pytest from the host's .venv-test with the copy as its working directory, and
# PROVE — by importing `src` and checking `src.__file__` — that the copy is what is under
# test. That last check is not ceremony: if `import src` resolved to the repository (an
# editable install) or to a site-packages copy, a run would exercise unmutated code and
# report every mutant as SURVIVED.
#
# Run it ON THE HOST, not over sshfs (CLAUDE.md): through a FUSE mount the copy and every
# pytest run can be 100-400x slower. The images have no pytest, so there is no container
# path.
#
# Three guards, all lifted from mutate_system.sh:
#   1. provenance — `import src` from the copy must resolve inside the copy;
#   2. the mutant must still IMPORT — otherwise a SyntaxError fakes a kill, and a
#      harness that cannot tell "the behaviour is tested" from "the file no longer
#      parses" is not measuring anything. Reported as VOID, never as killed;
#   3. `git diff --quiet -- src/` before the first mutant and after the last.
# After every mutant the copy's file is restored from the repository and `cmp`-checked,
# so one bad edit cannot silently pollute the rest of the run. A selection that hangs
# past MUT_TIMEOUT, or a pytest exit other than 0 or 1, is an ERROR, never a kill: a kill
# must name a failing test. So is exit 1 without a `FAILED ` summary line in the log (a
# setup/fixture ERROR, a failed `cd`, a failed log redirect).
#
# THE INERT MUTANT IS NOT OPTIONAL. M0 below changes no behaviour (a docstring) and
# MUST SURVIVE. Without it, a selection that is red for any unrelated reason — a dead
# fixture, a migrated-away column, a leftover row — kills every mutant and looks maximally
# sensitive when it is merely broken. It is listed FIRST so that failure is detected
# before any of the real mutants are believed.
#
# Usage:
#   ./scripts/mutate_cohorts.sh
#
# Overridable env:
#   MUT_PYTHON          interpreter with pytest (default: ./.venv-test/bin/python, the
#                       host venv; create it ON THE HOST as CLAUDE.md describes)
#   TEST_DATABASE_URL   throwaway asyncpg DSN (default: unset, so conftest starts an
#                       ephemeral testcontainers Postgres); refused if it names `copi`
#   MUT_TIMEOUT         seconds per pytest run before it is killed and scored ERROR
#                       (default: 900)
#   MUTCOH_COPY_DIR     where the mutated tree lives (default: a fresh mktemp dir); must
#                       be absent or empty, outside the repository, not a symlink and
#                       owned by you; it is forced to 0700
#   MUTCOH_LOGDIR       where per-mutant pytest logs are kept (default: a mktemp dir);
#                       same symlink/owner checks, forced to 0700
#   MUTCOH_KEEP_COPY    set to 1 to leave the mutated tree behind for inspection
#
# `RUNNER` is gone. It used to be a whole pytest invocation pasted in as a string,
# which cannot express "run with the copy as cwd" — the override and the isolation
# strategy were mutually exclusive. Use MUT_PYTHON / MUTCOH_COPY_DIR instead.
#
# MEASURED 2026-08-04, after the conversion: 9/9 real mutants killed, inert control
# survived, src/ clean. Same 9/9 the in-place harness reported, so no mutant moved —
# but that agreement is now backed by asserted provenance rather than assumed, and by
# an inert control the old harness did not have. The unmutated selection is 239 passed,
# checked separately, which is the other half of why the 9/9 means something.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT=$(pwd -P)
# Shared isolation, guard and scoring functions (also used by the other two harnesses).
source "$ROOT/scripts/lib/mutation_harness.sh"

TESTS="tests/unit/test_cohort_isolation.py tests/integration/test_cohort_engine_live.py tests/integration/test_cohort_admin.py"
PY="${MUT_PYTHON:-$PWD/.venv-test/bin/python}"
[ -x "$PY" ] || { echo "ERROR: $PY missing — create .venv-test ON THE HOST (CLAUDE.md); the images have no pytest" >&2; exit 1; }
case "$PY" in /*) ;; *) PY="$ROOT/$PY" ;; esac  # pytest runs from the copy, not from here
MUT_TIMEOUT="${MUT_TIMEOUT:-900}"

# The log dir is created, not assumed, because MUTCOH_LOGDIR is documented as overridable
# and an absent directory makes every `>"$log"` redirect fail — which the shell once
# scored as a nonzero exit, i.e. as a kill, for every mutant including the inert control.
# Measured on mutate_system.sh, which had this bug: 6/6 "killed" and 0/4 inert survived.
# Only the inert control distinguished that from a real result. (A failed redirect can no
# longer score a kill anyway: a kill now needs a FAILED line in the log.)
if [ -n "${MUTCOH_LOGDIR:-}" ]; then
  LOGDIR="$MUTCOH_LOGDIR"
  secure_override_dir MUTCOH_LOGDIR "$LOGDIR" || exit 1
else
  LOGDIR=$(mktemp -d) || { echo "ERROR: mktemp failed for the log dir" >&2; exit 1; }
fi

# Deliberately NOT the live database, and asserted rather than assumed: the cohort
# engine and admin suites commit. Unset or empty means conftest's own throwaway Postgres
# (tests/conftest.py treats an empty value as unset). The database name is parsed rather
# than pattern-matched, so `.../copi/`, `.../copi#x` and `.../copi?x` are all caught.
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
# Guard 3a: the working tree is never touched. Checked here, and again at the end.
# ---------------------------------------------------------------------------
if ! git diff --quiet -- src/; then
  echo "ERROR: src/ has uncommitted changes." >&2
  echo "This script does not edit src/ — it mutates a copy outside the repository — but a" >&2
  echo "dirty tree means the copy would carry changes that are not the mutant, so every" >&2
  echo "result below would be unattributable. Commit or stash first." >&2
  exit 1
fi

# file ~~ exact source substring ~~ replacement ~~ what it breaks
# The delimiter is ~~ and not | because one target contains a pipe
# (`gates[aid] = mates | unrestricted`) — the very line whose mutation is M2.
# `\n` in the FROM/TO fields is a newline (see the applier below).
MUTANTS=(
# The inert control runs FIRST: if it does not survive, no number below is a score.
"src/services/cohorts.py~~    \"\"\"Counts for logging and the admin banner.\"\"\"~~    \"\"\"Counts for logging and for the admin banner. [INERT EDIT]\"\"\"~~M0 INERT docstring — MUST SURVIVE"
"src/services/cohorts.py~~gates[aid] = set() if isolate_uncohorted else None~~gates[aid] = set()~~M1 open-policy uncohorted agent is silenced instead of unrestricted"
"src/services/cohorts.py~~gates[aid] = mates | unrestricted~~gates[aid] = mates~~M2 the open-policy asymmetry (a REAL defect the suite missed)"
"src/services/cohorts.py~~effective = cohort_count if live_members is None else live_members~~effective = cohort_count~~M3 preflight counts cohorts, not live members, so an empty cohort silences the roster"
"src/agent/message_log.py~~    if not entry.is_bot:\n        return True~~    if entry.sender_agent_id is None:\n        return True~~M4 the human bypass keys on a NULL agent_id, so an unattributable bot row leaks"
"src/agent/message_log.py~~    if entry.visibility == VISIBILITY_COLLAB_PRIVATE:~~    if False:~~M5 the private-channel exemption is dead"
# M6 was pinned to `visibility=self._resolve_channel_visibility(channel),` — the
# keyword argument inside the LogEntry(...) call. d311170 hoisted the resolution out of
# that call so the chunk loop could reuse one value, and the old target stopped existing.
# Re-pointed 2026-08-04 at the assignment, which is the same defect: every chunk of every
# outbound post is then stamped public. Nothing detected the drift for five days because
# nothing re-ran this script; when it was re-run it reported ERROR rather than a false
# kill, which is the one thing the old harness did get right.
"src/agent/engine/slack_io.py~~        visibility = self._resolve_channel_visibility(channel)~~        visibility = VISIBILITY_PUBLIC~~M6 outbound messages are never stamped collab_private (a REAL defect the suite missed)"
# M7 was pinned to `_owes_reply`'s grandfathered skip; D12 of
# docs/plans/2026-09-25-rca-remediation-plan.md retired the rule and the function is
# gone. Removed rather than re-pointed: `grandfathered` is now reporting-only, so there
# is no behaviour left for a mutant to break.
# M8 was pinned to `_select_agent`'s reactive-tier valve
# (`self._reactive_streak < settings.max_consecutive_reactive_turns`). Task 11 of
# docs/plans/2026-08-14-two-lane-concurrent-scheduler.md deleted the reactive tier
# outright — replies leave the paced pool entirely — so there is no equivalent line
# left to mutate; removed rather than re-pointed.
"src/agent/engine/roster.py~~            if target_id == agent.agent_id or target_id in allowed:~~            if True:~~M9 the outbound tag strip never strips"
)

# ---------------------------------------------------------------------------
# Build the mutable copy OUTSIDE the repository and PROVE it is what runs.
# (copy_is_safe and the guards live in scripts/lib/mutation_harness.sh.)
# ---------------------------------------------------------------------------
if [ -n "${MUTCOH_COPY_DIR:-}" ]; then
  COPY="$MUTCOH_COPY_DIR"
  secure_override_dir MUTCOH_COPY_DIR "$COPY" empty || exit 1
else
  COPY=$(mktemp -d "${TMPDIR:-/tmp}/mutcoh.XXXXXX") || { echo "ERROR: mktemp failed" >&2; exit 1; }
fi
[ -n "$COPY" ] || { echo "ERROR: empty copy directory" >&2; exit 1; }
COPY=$(cd -- "$COPY" && pwd -P) || { echo "ERROR: cannot resolve the copy directory" >&2; exit 1; }
copy_is_safe || exit 1

# INT, TERM and HUP are trapped too, so an interrupted run still removes the copy (which
# holds `.env`). A signal handler cleans up, disarms the EXIT trap and exits 128+signal;
# the `cleaned` flag makes a second call a no-op either way.
cleaned=0
cleanup() {
  [ "$cleaned" -eq 1 ] && return 0
  cleaned=1
  if [ "${MUTCOH_KEEP_COPY:-0}" = "1" ]; then
    echo "(left the mutated tree at ${COPY} — MUTCOH_KEEP_COPY=1)"
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
mh_make_copy || exit 1

# ---------------------------------------------------------------------------
# Guard 1: provenance.
# ---------------------------------------------------------------------------
mh_check_provenance || exit 1

echo "logs: $LOGDIR"
echo

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
fail=0 killed=0 survived=0 void=0 errors=0 broken_inert=0 inert_ok=0 n=0
declare -a SURVIVORS=()

for m in "${MUTANTS[@]}"; do
  file="${m%%~~*}"; rest="${m#*~~}"
  from="${rest%%~~*}"; rest="${rest#*~~}"
  to="${rest%%~~*}"; label="${rest#*~~}"
  n=$((n + 1))
  short="${label%% *}"

  inert=0; [[ "$label" == *INERT* ]] && inert=1

  # --- apply the mutation to the COPY --------------------------------------------------
  if ! mh_apply_mutant "$file" "$from" "$to"
  then
    echo "ERROR     $label — target string not found (or not unique); the code moved," >&2
    echo "          fix this script rather than the test." >&2
    fail=1; errors=$((errors + 1))
    cp -- "$file" "$COPY/$file" >/dev/null 2>&1
    continue
  fi

  # --- Guard 2: the mutant must still import ------------------------------------------
  # A SyntaxError makes every test in the selection error out, which is indistinguishable
  # from a kill unless it is checked for. Derived from the path so a new mutant in a new
  # file is covered without editing this line.
  if ! mh_check_imports "$file"; then
    echo "VOID      $label — the mutated module does not import, so a kill here would" >&2
    echo "          only mean 'the file no longer parses'. Fix the replacement text." >&2
    void=$((void + 1)); fail=1
    cp -- "$file" "$COPY/$file" >/dev/null 2>&1
    continue
  fi

  log="$LOGDIR/$(printf '%02d' "$n")-${short}.log"
  # -x: stop at the first failure. The killer's name is what the report needs, and the
  # inert control above is what makes attributing it sound. -rfE: the summary lists
  # `FAILED <node>` for failed tests (the kill evidence) and `ERROR <node>` for
  # setup/collection errors (diagnostics only, never a kill). timeout bounds a hung
  # mutant (the repo has no pytest-timeout); `exec` makes the kill reach pytest itself.
  envargs=()
  # Inherited through the environment, never passed on `env`'s argv, where `ps` shows it.
  [ -z "${TEST_DATABASE_URL:-}" ] || export TEST_DATABASE_URL
  (cd "$COPY" && env ${envargs[@]+"${envargs[@]}"} timeout -k 30 "$MUT_TIMEOUT" \
     sh -c "exec \"$PY\" -m pytest $TESTS -q -x -rfE -m 'not real_llm' -p no:cacheprovider") \
     >"$log" 2>&1
  rc=$?
  mh_score "$rc" "$log" "$label" "$inert"

  # --- restore the copy from the repository, and verify it -----------------------------
  mh_restore "$file" || exit 1
done

# ---------------------------------------------------------------------------
# Guard 3b: the working tree must be exactly as we found it.
# ---------------------------------------------------------------------------
if ! git diff --quiet -- src/; then
  echo >&2
  echo "ERROR: src/ is dirty. This script never writes to src/, so something else did." >&2
  echo "Inspect 'git diff -- src/' before doing anything else." >&2
  exit 1
fi

mh_report cohorts
echo "${void} void"
echo "src/ clean: yes"
if [ "$fail" -eq 0 ]; then
  echo "all mutants killed and the inert control survived — the cohort suite has teeth"
fi
exit "$fail"
