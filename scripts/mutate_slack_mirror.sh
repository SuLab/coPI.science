#!/usr/bin/env bash
#
# Mutation check for the DB<->Slack mirror. Each mutant must be KILLED by the live
# Slack tier. A SURVIVING mutant means the live tests do not actually test that
# behaviour — which is the whole reason they exist, since the offline suite runs with
# NullTransport and cannot see the mirror at all (Rule S2).
#
# Needs the live workspace, including all three probe bot tokens: the lifecycle tests
# compare the bots against each other. Slower and more expensive than
# scripts/mutate_cohorts.sh — each mutant is a full live run against Slack.
#
#   SLACK_TEST_WORKSPACE=... SLACK_TEST_BOT_TOKEN_SU=... ./scripts/mutate_slack_mirror.sh
#
# Run it ON THE HOST, not over sshfs (CLAUDE.md), with the host's .venv-test: the images
# have no pytest. The host environment is inherited, so the SLACK_TEST_* variables (and
# ANTHROPIC_API_KEY / LIVE_API_TESTS if the live files need them) are passed by exporting
# them. With TEST_DATABASE_URL unset, tests/conftest.py starts its own testcontainers
# Postgres.
#
# Overridable env:
#   MUT_PYTHON           interpreter with pytest (default: ./.venv-test/bin/python)
#   TEST_DATABASE_URL    throwaway asyncpg DSN (default: unset, so conftest starts an
#                        ephemeral testcontainers Postgres); refused if it names `copi`
#   MUT_TIMEOUT          seconds per pytest run before it is killed and scored ERROR
#                        (default: 900)
#   MUTMIRROR_COPY_DIR   where the mutated tree lives (default: a fresh mktemp dir); must
#                        be absent or empty, outside the repository, not a symlink and
#                        owned by you; it is forced to 0700
#   MUTMIRROR_LOGDIR     where per-mutant pytest logs are kept (default: a mktemp dir);
#                        same symlink/owner checks, forced to 0700
#   MUTMIRROR_KEEP_COPY  set to 1 to leave the mutated tree behind for inspection
#
# ---------------------------------------------------------------------------------------
# CONVERTED 2026-08-04, when live workspace credentials first became available. Until then
# this script edited src/ IN PLACE via a .mutbak copy, and could not be run even once — so
# it was left alone deliberately, on the grounds that rewriting a measurement harness you
# cannot execute converts a known weakness into an unknown one. All four defects its own
# header listed are now fixed, and the result was run three times:
#
#   1. the tree is copied into a fresh 0700 mktemp directory outside the repository and
#      the COPY is mutated, with pytest run from the copy as its working directory;
#   2. provenance is asserted — `import src` from the copy must resolve to
#      "$COPY/src/__init__.py". If it resolved to the repository (an editable install) or
#      to a site-packages copy, a run would exercise unmutated code and report every
#      mutant as SURVIVED;
#   3. the mutated module must still import, so a SyntaxError cannot fake a kill;
#   4. `git diff --quiet -- src/` is asserted before the first mutant and after the last;
#   5. `\n` in the FROM/TO fields becomes a real newline, matching the other two harnesses;
#   6. per-mutant output is kept and a kill NAMES the test that killed it. Discarding
#      output made a mutant that "killed" because the workspace was unreachable
#      indistinguishable from a real kill. For the same reason a run that hangs past
#      MUT_TIMEOUT, a pytest exit other than 0 or 1, or an exit 1 whose log has no
#      `FAILED ` summary line (a setup ERROR, a failed `cd` or log redirect) is an ERROR,
#      never a kill.
#
# S4 is the inert control and MUST SURVIVE — a tier without one scores 100% precisely when
# it is broken. mutate_system.sh once printed "killed 6/6" beside "inert controls: 0/4
# survived" because its log directory did not exist and every redirect failed; the inert
# control was the only signal. Hence the mkdir -p below.
# ---------------------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT=$(pwd -P)
# Shared isolation, guard and scoring functions (also used by the other two harnesses).
source "$ROOT/scripts/lib/mutation_harness.sh"

: "${SLACK_TEST_WORKSPACE:?live workspace credentials required}"

PY="${MUT_PYTHON:-$PWD/.venv-test/bin/python}"
[ -x "$PY" ] || { echo "ERROR: $PY missing — create .venv-test ON THE HOST (CLAUDE.md); the images have no pytest" >&2; exit 1; }
case "$PY" in /*) ;; *) PY="$ROOT/$PY" ;; esac  # pytest runs from the copy, not from here
MUT_TIMEOUT="${MUT_TIMEOUT:-900}"

# The log dir is created, not assumed: an absent one makes every `>"$log"` redirect fail
# (see S4 in the header). A failed redirect no longer scores a kill regardless: a kill
# needs a FAILED line in the log.
if [ -n "${MUTMIRROR_LOGDIR:-}" ]; then
  LOGDIR="$MUTMIRROR_LOGDIR"
  secure_override_dir MUTMIRROR_LOGDIR "$LOGDIR" || exit 1
else
  LOGDIR=$(mktemp -d) || { echo "ERROR: mktemp failed for the log dir" >&2; exit 1; }
fi

# Deliberately NOT the live database: the live tiers commit. Unset or empty means
# conftest's own throwaway Postgres (tests/conftest.py treats an empty value as unset).
# The database name is parsed rather than pattern-matched, so `.../copi/`, `.../copi#x`
# and `.../copi?x` are all caught.
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

TESTS="tests/integration/test_slack_mirror_live.py tests/integration/test_slack_lifecycle_live.py"

if ! git diff --quiet -- src/; then
  echo "ERROR: src/ has uncommitted changes. Commit or stash first — a mutation run" >&2
  echo "against a dirty tree cannot be attributed to the mutants." >&2
  exit 1
fi

# file ~~ exact source substring ~~ replacement ~~ what it breaks
# `\n` in the FROM/TO fields is a newline (see the applier below).
# S1 is anchored on the outbound LogEntry's `visibility=` line too: `slack_ts=slack_ts,`
# alone occurs several times in slack_io.py, and the applier requires exactly one.
MUTANTS=(
"src/agent/engine/slack_io.py~~                visibility=visibility,\n                slack_ts=slack_ts,~~                visibility=visibility,\n                slack_ts=None,~~S1 the mirror mapping is never recorded on an outbound post"
"src/agent/engine/slack_io.py~~        return root.slack_ts~~        return thread_ts~~S2 a canonical id is handed to Slack (a93d136)"
"src/agent/slack_client.py~~            self._client = None~~            pass~~S3 a dead token still reports is_connected (the bug found by T11)"
"src/agent/slack_client.py~~        last_exc: SlackApiError | None = None~~        last_exc = None  # noqa~~S4 sanity: this edit is inert and MUST survive"
)

# ---------------------------------------------------------------------------
# Build the mutable copy OUTSIDE the repository and PROVE it is what runs.
# (copy_is_safe and the guards live in scripts/lib/mutation_harness.sh.)
# ---------------------------------------------------------------------------
if [ -n "${MUTMIRROR_COPY_DIR:-}" ]; then
  COPY="$MUTMIRROR_COPY_DIR"
  secure_override_dir MUTMIRROR_COPY_DIR "$COPY" empty || exit 1
else
  COPY=$(mktemp -d "${TMPDIR:-/tmp}/mutmirror.XXXXXX") || { echo "ERROR: mktemp failed" >&2; exit 1; }
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
  if [ "${MUTMIRROR_KEEP_COPY:-0}" = "1" ]; then
    echo "(left the mutated tree at ${COPY} — MUTMIRROR_KEEP_COPY=1)"
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

mh_check_provenance || exit 1

# -rfE: the summary lists `FAILED <node>` for failed tests (the kill evidence) and
# `ERROR <node>` for setup/collection errors (diagnostics only, never a kill).
run_selection() {  # $1 = log file; prints nothing, returns pytest's (or timeout's) exit
  local envargs=()
  # Inherited through the environment, never passed on `env`'s argv, where `ps` shows it.
  [ -z "${TEST_DATABASE_URL:-}" ] || export TEST_DATABASE_URL
  (cd "$COPY" && env ${envargs[@]+"${envargs[@]}"} timeout -k 30 "$MUT_TIMEOUT" \
     sh -c "exec \"$PY\" -m pytest $TESTS -q -rfE -m live_slack -p no:cacheprovider") > "$1" 2>&1
}

echo; echo "=== baseline (unmutated copy) ==="
if ! run_selection "$LOGDIR/baseline.log"; then
  echo "ERROR: the unmutated copy is RED (or timed out) — no mutant result below would mean anything." >&2
  grep -E "^FAILED|^ERROR|passed|failed" "$LOGDIR/baseline.log" | tail -5 >&2
  exit 1
fi
grep -E "passed|failed" "$LOGDIR/baseline.log" | tail -1
echo

fail=0 killed=0 survived=0 errors=0 broken_inert=0 inert_ok=0 void=0 i=0
declare -a SURVIVORS=()
for m in "${MUTANTS[@]}"; do
  i=$((i+1))
  file="${m%%~~*}"; rest="${m#*~~}"
  from="${rest%%~~*}"; rest="${rest#*~~}"
  to="${rest%%~~*}"; label="${rest#*~~}"
  inert=0; [[ "$label" == S4* ]] && inert=1

  if ! mh_apply_mutant "$file" "$from" "$to"
  then
    echo "ERROR   $label — target not found or not unique; the code moved" >&2
    cp -- "$file" "$COPY/$file" >/dev/null 2>&1
    fail=1; errors=$((errors + 1)); continue
  fi

  if ! mh_check_imports "$file"; then
    echo "VOID    $label — the mutated module does not import; result discarded" >&2
    cp -- "$file" "$COPY/$file" >/dev/null 2>&1
    void=$((void + 1)); fail=1; continue
  fi

  log="$LOGDIR/m$i.log"
  run_selection "$log"; rc=$?
  mh_score "$rc" "$log" "$label" "$inert"
  mh_restore "$file" || exit 1
done

echo
git diff --quiet -- src/ || { echo "FATAL: src/ was modified — results are void" >&2; exit 1; }
echo "repo clean check: src/ untouched"
mh_report slack_mirror
echo "${void} void"
echo "logs: $LOGDIR"
[ "$fail" -eq 0 ] && echo "the live Slack mirror tier has teeth" || echo "SURVIVING, VOID OR ERROR MUTANTS" >&2
exit "$fail"
