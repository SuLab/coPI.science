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
# copy the tree into the container's /tmp, mutate the COPY, run pytest with the copy as
# its working directory, and PROVE — by importing `src` and checking `src.__file__` —
# that the copy is what is under test. That last check is not ceremony: `src` is also
# installed into site-packages in this image, so without it a run can exercise
# unmutated code and report every mutant as SURVIVED.
#
# Three guards, all lifted from mutate_system.sh:
#   1. provenance — `import src` from the copy must resolve inside the copy;
#   2. the mutant must still IMPORT — otherwise a SyntaxError fakes a kill, and a
#      harness that cannot tell "the behaviour is tested" from "the file no longer
#      parses" is not measuring anything. Reported as VOID, never as killed;
#   3. `git diff --quiet -- src/` before the first mutant and after the last.
# After every mutant the copy's file is restored from the read-only /app mount and
# `cmp`-checked, so one bad edit cannot silently pollute the rest of the run.
#
# THE INERT MUTANT IS NOT OPTIONAL. M0 below changes no behaviour (a docstring) and
# MUST SURVIVE. Without it, a selection that is red for any unrelated reason — a dead
# fixture, a migrated-away column, a leftover row — scores 9/9 and looks maximally
# sensitive when it is merely broken. It is listed FIRST so that failure is detected
# before any of the real mutants are believed.
#
# Usage:
#   TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_test \
#     ./scripts/mutate_cohorts.sh
#
# Overridable env:
#   TEST_DATABASE_URL   throwaway asyncpg DSN (REQUIRED — these suites commit)
#   MUTCOH_SERVICE      compose service to exec into (default: app)
#   MUTCOH_COPY_DIR     where the mutated tree lives inside the container
#   MUTCOH_LOGDIR       where per-mutant pytest logs are kept (default: a mktemp dir)
#   MUTCOH_KEEP_COPY    set to 1 to leave the mutated tree behind for inspection
#
# `RUNNER` is gone. It used to be a whole pytest invocation pasted in as a string,
# which cannot express "run in the container but with the copy as cwd" — the override
# and the isolation strategy were mutually exclusive. Use MUTCOH_SERVICE /
# MUTCOH_COPY_DIR instead.
#
# MEASURED 2026-08-04, after the conversion: 9/9 real mutants killed, inert control
# survived, src/ clean. Same 9/9 the in-place harness reported, so no mutant moved —
# but that agreement is now backed by asserted provenance rather than assumed, and by
# an inert control the old harness did not have. The unmutated selection is 239 passed,
# checked separately, which is the other half of why the 9/9 means something.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${TEST_DATABASE_URL:?set TEST_DATABASE_URL to a throwaway database}"

TESTS="tests/unit/test_cohort_isolation.py tests/integration/test_cohort_engine_live.py tests/integration/test_cohort_admin.py"
SVC="${MUTCOH_SERVICE:-app}"
COPY="${MUTCOH_COPY_DIR:-/tmp/mutcoh}"
LOGDIR="${MUTCOH_LOGDIR:-$(mktemp -d)}"
DC=(docker compose exec -T)

# mkdir, because MUTCOH_LOGDIR is documented as overridable and an absent directory
# makes every `>"$log"` redirect fail — which the shell scores as a nonzero exit, i.e.
# as a kill, for every mutant including the inert control. Measured on mutate_system.sh,
# which had this bug: 6/6 "killed" and 0/4 inert survived. Only the inert control
# distinguished that from a real result.
mkdir -p "$LOGDIR" || { echo "ERROR: cannot create log dir $LOGDIR" >&2; exit 1; }

# Deliberately NOT the live database, and asserted rather than assumed: the cohort
# engine and admin suites commit.
case "$TEST_DATABASE_URL" in
  */copi|*/copi\?*)
    echo "ERROR: TEST_DATABASE_URL points at the live 'copi' database. These suites" >&2
    echo "commit. Use a throwaway database." >&2
    exit 1 ;;
esac

# ---------------------------------------------------------------------------
# Guard 3a: the working tree is never touched. Checked here, and again at the end.
# ---------------------------------------------------------------------------
if ! git diff --quiet -- src/; then
  echo "ERROR: src/ has uncommitted changes." >&2
  echo "This script does not edit src/ — it mutates a copy inside the container — but a" >&2
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
"src/agent/message_log.py~~    if not entry.is_bot:~~    if entry.sender_agent_id is None:~~M4 the human bypass keys on a NULL agent_id, so an unattributable bot row leaks"
"src/agent/message_log.py~~    if entry.visibility == VISIBILITY_COLLAB_PRIVATE:~~    if False:~~M5 the private-channel exemption is dead"
# M6 was pinned to `visibility=self._resolve_channel_visibility(channel),` — the
# keyword argument inside the LogEntry(...) call. d311170 hoisted the resolution out of
# that call so the chunk loop could reuse one value, and the old target stopped existing.
# Re-pointed 2026-08-04 at the assignment, which is the same defect: every chunk of every
# outbound post is then stamped public. Nothing detected the drift for five days because
# nothing re-ran this script; when it was re-run it reported ERROR rather than a false
# kill, which is the one thing the old harness did get right.
"src/agent/simulation.py~~        visibility = self._resolve_channel_visibility(channel)~~        visibility = VISIBILITY_PUBLIC~~M6 outbound messages are never stamped collab_private (a REAL defect the suite missed)"
"src/agent/simulation.py~~            if thread.grandfathered:\n                continue~~            if False:\n                continue~~M7 a grandfathered thread keeps reactive priority"
"src/agent/simulation.py~~        if self._reactive_streak < settings.max_consecutive_reactive_turns:~~        if True:~~M8 the fairness valve never closes"
"src/agent/simulation.py~~            if target_id == agent.agent_id or target_id in allowed:~~            if True:~~M9 the outbound tag strip never strips"
)

# ---------------------------------------------------------------------------
# Build the mutable copy inside the container and PROVE it is what runs.
# ---------------------------------------------------------------------------
cleanup() {
  if [ "${MUTCOH_KEEP_COPY:-0}" = "1" ]; then
    echo "(left the mutated tree at ${SVC}:${COPY} — MUTCOH_KEEP_COPY=1)"
  else
    "${DC[@]}" "$SVC" rm -rf "$COPY" >/dev/null 2>&1
  fi
}
trap cleanup EXIT

echo "building a throwaway copy of the tree at ${SVC}:${COPY} (the repo is never written to)"
if ! "${DC[@]}" "$SVC" sh -c "
  rm -rf '$COPY' && mkdir -p '$COPY' &&
  tar -C /app \
      --exclude=./.git --exclude=./.venv-test --exclude=./mutants --exclude=./build \
      --exclude=./logs --exclude=./.hypothesis --exclude=./.pytest_cache \
      --exclude=./.ruff_cache --exclude=./.playwright-mcp --exclude=__pycache__ \
      -cf - . | tar -C '$COPY' -xf -
" 2>/dev/null; then
  echo "ERROR: could not copy /app into $COPY inside the '$SVC' container." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Guard 1: provenance.
# ---------------------------------------------------------------------------
prov=$("${DC[@]}" -w "$COPY" "$SVC" python -c "import src; print(src.__file__)" 2>/dev/null | tr -d '\r')
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
fail=0 killed=0 survived=0 void=0 broken_inert=0 inert_ok=0 n=0
declare -a SURVIVORS=()

for m in "${MUTANTS[@]}"; do
  file="${m%%~~*}"; rest="${m#*~~}"
  from="${rest%%~~*}"; rest="${rest#*~~}"
  to="${rest%%~~*}"; label="${rest#*~~}"
  n=$((n + 1))
  short="${label%% *}"

  inert=0; [[ "$label" == *INERT* ]] && inert=1

  # --- apply the mutation to the COPY --------------------------------------------------
  if ! "${DC[@]}" -e "FROM=$from" -e "TO=$to" "$SVC" python - "$COPY/$file" <<'PY' 2>&1
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
    fail=1
    "${DC[@]}" "$SVC" cp -- "/app/$file" "$COPY/$file" >/dev/null 2>&1
    continue
  fi

  # --- Guard 2: the mutant must still import ------------------------------------------
  # A SyntaxError makes every test in the selection error out, which is indistinguishable
  # from a kill unless it is checked for. Derived from the path so a new mutant in a new
  # file is covered without editing this line.
  mod=$(printf '%s' "${file%.py}" | tr '/' '.')
  if ! "${DC[@]}" -w "$COPY" "$SVC" python -c "import $mod" >/dev/null 2>&1; then
    echo "VOID      $label — the mutated module does not import, so a kill here would" >&2
    echo "          only mean 'the file no longer parses'. Fix the replacement text." >&2
    void=$((void + 1)); fail=1
    "${DC[@]}" "$SVC" cp -- "/app/$file" "$COPY/$file" >/dev/null 2>&1
    continue
  fi

  log="$LOGDIR/$(printf '%02d' "$n")-${short}.log"
  # -x: stop at the first failure. The killer's name is what the report needs, and the
  # inert control above is what makes attributing it sound.
  if "${DC[@]}" -e "TEST_DATABASE_URL=$TEST_DATABASE_URL" -w "$COPY" "$SVC" \
       sh -c "python -m pytest $TESTS -q -x -rf -m 'not real_llm' -p no:cacheprovider" \
       >"$log" 2>&1; then
    if [ "$inert" -eq 1 ]; then
      echo "survived (expected)  $label"
      inert_ok=$((inert_ok + 1))
    else
      echo "SURVIVED  $label"
      SURVIVORS+=("$label")
      survived=$((survived + 1)); fail=1
    fi
  else
    killer=$(grep -m1 '^FAILED ' "$log" | sed 's/^FAILED //')
    if [ "$inert" -eq 1 ]; then
      echo "KILLED AN INERT MUTANT  $label" >&2
      echo "          -> ${killer:-see $log}" >&2
      echo "          The selection is failing for a reason that is NOT the mutation, so" >&2
      echo "          every other number in this run is meaningless." >&2
      broken_inert=$((broken_inert + 1)); fail=1
    else
      echo "killed    $label"
      echo "          by ${killer:-<no FAILED line; see $log>}"
      killed=$((killed + 1))
    fi
  fi

  # --- restore the copy from the pristine mount, and verify it ------------------------
  "${DC[@]}" "$SVC" cp -- "/app/$file" "$COPY/$file" >/dev/null 2>&1
  if ! "${DC[@]}" "$SVC" cmp -s "/app/$file" "$COPY/$file"; then
    echo "ERROR: $COPY/$file no longer matches /app/$file; the copy is polluted and" >&2
    echo "every result after this point is unattributable. Stopping." >&2
    exit 1
  fi
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

echo
echo "killed ${killed}/$((killed + survived + void)) real mutants"
echo "inert controls: ${inert_ok}/$((inert_ok + broken_inert)) survived (all of them must)"
echo "src/ clean: yes"

if [ "$broken_inert" -gt 0 ]; then
  echo >&2
  echo "AN INERT MUTANT WAS KILLED. Read nothing else in this run as a score: the" >&2
  echo "selection is red for an unrelated reason, which makes a broken suite look" >&2
  echo "maximally sensitive. Fix that first, then re-run." >&2
fi
if [ "${#SURVIVORS[@]}" -gt 0 ]; then
  echo >&2
  echo "SURVIVING MUTANTS — each is a behaviour the suite does not protect:" >&2
  for s in "${SURVIVORS[@]}"; do echo "  - $s" >&2; done
  echo "Add the test that kills it. Do not weaken the mutant." >&2
fi
if [ "$fail" -eq 0 ]; then
  echo "all mutants killed and the inert control survived — the cohort suite has teeth"
fi
exit "$fail"
