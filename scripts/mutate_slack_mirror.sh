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
#   TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a6 \
#     ./scripts/mutate_slack_mirror.sh
#
# ---------------------------------------------------------------------------------------
# CONVERTED 2026-08-04, when live workspace credentials first became available. Until then
# this script edited src/ IN PLACE via a .mutbak copy, and could not be run even once — so
# it was left alone deliberately, on the grounds that rewriting a measurement harness you
# cannot execute converts a known weakness into an unknown one. All four defects its own
# header listed are now fixed, and the result was run three times:
#
#   1. the tree is copied into the container's /tmp and the COPY is mutated, with pytest
#      run from the copy as its working directory;
#   2. provenance is asserted — `import src` from the copy must resolve to
#      "$COPY/src/__init__.py". `src` is ALSO installed into site-packages in this image,
#      so without this a run can exercise unmutated code and report every mutant as
#      SURVIVED;
#   3. the mutated module must still import, so a SyntaxError cannot fake a kill;
#   4. `git diff --quiet -- src/` is asserted before the first mutant and after the last;
#   5. `\n` in the FROM/TO fields becomes a real newline, matching the other two harnesses;
#   6. per-mutant output is kept and a kill NAMES the test that killed it. Discarding
#      output made a mutant that "killed" because the workspace was unreachable
#      indistinguishable from a real kill.
#
# S4 is the inert control and MUST SURVIVE — a tier without one scores 100% precisely when
# it is broken. mutate_system.sh once printed "killed 6/6" beside "inert controls: 0/4
# survived" because its log directory did not exist and every redirect failed; the inert
# control was the only signal. Hence the mkdir -p below.
# ---------------------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${SLACK_TEST_WORKSPACE:?live workspace credentials required}"
: "${TEST_DATABASE_URL:?set TEST_DATABASE_URL to a throwaway database}"

SVC="${MUTMIRROR_SERVICE:-app}"
COPY="${MUTMIRROR_COPY_DIR:-/tmp/mutmirror}"
LOGDIR="${MUTMIRROR_LOGDIR:-$(mktemp -d)}"
mkdir -p "$LOGDIR"

ENVARGS=()
for v in SLACK_TEST_WORKSPACE SLACK_TEST_PI_USER_ID SLACK_TEST_TEAM_ID \
         SLACK_TEST_BOT_TOKEN_SU SLACK_TEST_BOT_TOKEN_CRAVATT SLACK_TEST_BOT_TOKEN_WISEMAN \
         ANTHROPIC_API_KEY LIVE_API_TESTS TEST_DATABASE_URL; do
  [ -n "${!v:-}" ] && ENVARGS+=(-e "$v=${!v}")
done
TESTS="tests/integration/test_slack_mirror_live.py tests/integration/test_slack_lifecycle_live.py"

if ! git diff --quiet -- src/; then
  echo "ERROR: src/ has uncommitted changes. Commit or stash first — a mutation run" >&2
  echo "against a dirty tree cannot be attributed to the mutants." >&2
  exit 1
fi

# file ~~ exact source substring ~~ replacement ~~ what it breaks
MUTANTS=(
"src/agent/simulation.py~~            slack_ts=slack_ts,~~            slack_ts=None,~~S1 the mirror mapping is never recorded on an outbound post"
"src/agent/simulation.py~~        return root.slack_ts~~        return thread_ts~~S2 a canonical id is handed to Slack (a93d136)"
"src/agent/slack_client.py~~            self._client = None~~            pass~~S3 a dead token still reports is_connected (the bug found by T11)"
"src/agent/slack_client.py~~        last_exc: SlackApiError | None = None~~        last_exc = None  # noqa~~S4 sanity: this edit is inert and MUST survive"
)

echo "building a throwaway copy of the tree at ${SVC}:${COPY} (the repo is never written to)"
docker compose exec -T "$SVC" bash -c "
  rm -rf '$COPY' && mkdir -p '$COPY' &&
  cd /app && tar --exclude=./.git --exclude=./.venv-test -cf - . | tar -C '$COPY' -xf -
" >/dev/null 2>&1 || { echo "ERROR: could not copy /app into $COPY" >&2; exit 1; }

prov=$(docker compose exec -T -w "$COPY" "$SVC" python -c "import src; print(src.__file__)" 2>/dev/null | tr -d '\r')
if [ "$prov" != "$COPY/src/__init__.py" ]; then
  echo "ERROR: from $COPY, 'import src' resolves to '${prov:-<nothing>}', not" >&2
  echo "$COPY/src/__init__.py. The mutants would not be under test. Refusing to run." >&2
  docker compose exec -T "$SVC" rm -rf "$COPY" >/dev/null 2>&1
  exit 1
fi
echo "provenance OK: pytest will import $prov"

cleanup() {
  if [ "${MUTMIRROR_KEEP_COPY:-0}" = "1" ]; then
    echo "(left the mutated tree at ${SVC}:${COPY} — MUTMIRROR_KEEP_COPY=1)"
  else
    docker compose exec -T "$SVC" rm -rf "$COPY" >/dev/null 2>&1
  fi
}
trap cleanup EXIT INT TERM

run_selection() {  # $1 = log file
  docker compose exec -T -w "$COPY" "${ENVARGS[@]}" "$SVC" \
    python -m pytest $TESTS -q -m live_slack -p no:cacheprovider > "$1" 2>&1
}

echo; echo "=== baseline (unmutated copy) ==="
if ! run_selection "$LOGDIR/baseline.log"; then
  echo "ERROR: the unmutated copy is RED — no mutant result below would mean anything." >&2
  grep -E "^FAILED|^ERROR|passed|failed" "$LOGDIR/baseline.log" | tail -5 >&2
  exit 1
fi
grep -E "passed|failed" "$LOGDIR/baseline.log" | tail -1
echo

fail=0; killed=0; i=0
for m in "${MUTANTS[@]}"; do
  i=$((i+1))
  file="${m%%~~*}"; rest="${m#*~~}"
  from="${rest%%~~*}"; rest="${rest#*~~}"
  to="${rest%%~~*}"; label="${rest#*~~}"
  inert=0; [[ "$label" == S4* ]] && inert=1

  if ! docker compose exec -T -e "FROM=$from" -e "TO=$to" "$SVC" python - "$COPY/$file" <<'PY'
import os, pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
frm = os.environ["FROM"].replace("\\n", "\n")
to = os.environ["TO"].replace("\\n", "\n")
n = s.count(frm)
if n != 1:
    sys.stderr.write(f"expected exactly 1 occurrence in {p.name}, found {n}: {frm!r}\n")
    sys.exit(1)
p.write_text(s.replace(frm, to, 1))
PY
  then
    echo "ERROR   $label — target not found or not unique; the code moved" >&2
    docker compose exec -T "$SVC" cp "/app/$file" "$COPY/$file" >/dev/null 2>&1
    fail=1; continue
  fi

  mod="${file#src/}"; mod="src.${mod%.py}"; mod="${mod//\//.}"
  if ! docker compose exec -T -w "$COPY" "$SVC" python -c "import $mod" >/dev/null 2>&1; then
    echo "VOID    $label — the mutated module does not import; result discarded" >&2
    docker compose exec -T "$SVC" cp "/app/$file" "$COPY/$file" >/dev/null 2>&1
    fail=1; continue
  fi

  log="$LOGDIR/m$i.log"
  if run_selection "$log"; then
    if [ "$inert" -eq 1 ]; then
      echo "survived (expected)  $label   [$(grep -oE '[0-9]+ passed' "$log" | tail -1)]"
      killed=$((killed+1))
    else
      echo "SURVIVED  $label   [$(grep -oE '[0-9]+ passed' "$log" | tail -1)]"; fail=1
    fi
  else
    if [ "$inert" -eq 1 ]; then
      echo "KILLED AN INERT MUTANT  $label — the tier is flaky or broken, not sensitive" >&2
      grep -E "^FAILED|^ERROR" "$log" | head -3 >&2
      fail=1
    else
      killers=$(grep -oE "^FAILED [^ ]+" "$log" | sed 's/^FAILED //' | head -3 | tr '\n' ' ')
      echo "killed    $label"
      echo "          by: ${killers:-no FAILED line — inspect $log}"
      killed=$((killed+1))
    fi
  fi
  docker compose exec -T "$SVC" cp "/app/$file" "$COPY/$file" >/dev/null 2>&1
done

echo
git diff --quiet -- src/ || { echo "FATAL: src/ was modified — results are void" >&2; exit 1; }
echo "repo clean check: src/ untouched"
echo "killed ${killed}/${#MUTANTS[@]}"
echo "logs: $LOGDIR"
[ "$fail" -eq 0 ] && echo "the live Slack mirror tier has teeth" || echo "SURVIVING OR VOID MUTANTS" >&2
exit "$fail"
