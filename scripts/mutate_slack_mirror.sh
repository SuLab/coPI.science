#!/usr/bin/env bash
#
# Mutation check for the DB<->Slack mirror. Each mutant must be KILLED by the live
# Slack tier. A SURVIVING mutant means the live tests do not actually test that
# behaviour — which is the whole reason they exist, since the offline suite runs with
# NullTransport and cannot see the mirror at all (Rule S2).
#
# Needs the live workspace. Slower and more expensive than scripts/mutate_cohorts.sh:
# each mutant is a full live run against Slack.
#
#   source <live env> && ./scripts/mutate_slack_mirror.sh
#
# ---------------------------------------------------------------------------------------
# WARNING, 2026-08-04: THIS SCRIPT STILL EDITS src/ IN PLACE. It is the last of the three
# mutation harnesses to do so. scripts/mutate_system.sh's header documents that exact
# strategy as having been auto-reverted mid-run by a repo guard, silently corrupting three
# earlier agents' results — mutants reported as SURVIVING would in fact have been killed —
# and scripts/mutate_cohorts.sh was converted away from it on 2026-08-04. Use
# mutate_system.sh as the pattern when converting this one:
#
#   1. copy the tree into the container's /tmp and mutate the COPY, running pytest with
#      the copy as its working directory;
#   2. assert provenance — `import src` from the copy must resolve to
#      "$COPY/src/__init__.py". `src` is ALSO installed into site-packages in this image,
#      so without this a run can exercise unmutated code and report every mutant as
#      SURVIVED. That is not hypothetical: it is what produced 8515f65's false "all four
#      chokepoint mutants survived the offline selection", re-measured 2026-08-04 as 4/4
#      killed;
#   3. assert the mutated module still imports, so a SyntaxError cannot fake a kill;
#   4. assert `git diff --quiet -- src/` before the first mutant and after the last.
#
# NOT CONVERTED HERE ON PURPOSE. Every mutant below is judged by the live Slack tier, and
# SLACK_TEST_WORKSPACE was not available, so a rewrite could not be run even once before
# being committed. Rewriting a measurement harness you cannot execute converts a known
# weakness into an unknown one. Two further defects found by reading, also left alone for
# the same reason — fix them in the same pass as the conversion, then run it three times:
#
#   a. the applier does NOT convert `\n` in the FROM/TO fields to a real newline, unlike
#      the other two harnesses (`frm, to = os.environ["FROM"], os.environ["TO"]`). No
#      mutant below currently spans a line, so nothing is broken today, but the first
#      multi-line mutant added here will substitute a literal backslash-n, and the result
#      will mean nothing.
#   b. `eval "$RUN" >/dev/null 2>&1` discards all output, so a kill cannot name the test
#      that killed it, and a mutant that "killed" because the workspace was unreachable is
#      indistinguishable from a real kill. S4 is the only thing standing between this
#      script and that failure mode; keep it, and add per-mutant logs.
#
# S4 is the inert control and MUST SURVIVE — see mutate_system.sh on why a tier without
# one scores 100% precisely when it is broken.
# ---------------------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${SLACK_TEST_WORKSPACE:?live workspace credentials required}"
: "${TEST_DATABASE_URL:?set TEST_DATABASE_URL}"

ENVARGS=""
for v in SLACK_TEST_WORKSPACE SLACK_TEST_PI_USER_ID SLACK_TEST_TEAM_ID \
         SLACK_TEST_BOT_TOKEN_SU SLACK_TEST_BOT_TOKEN_CRAVATT SLACK_TEST_BOT_TOKEN_WISEMAN \
         TEST_DATABASE_URL; do
  ENVARGS="$ENVARGS -e $v=${!v}"
done
TESTS="tests/integration/test_slack_mirror_live.py tests/integration/test_slack_lifecycle_live.py"
RUN="docker compose exec -T $ENVARGS app python -m pytest $TESTS -q -m live_slack"

if ! git diff --quiet -- src/; then
  echo "ERROR: src/ has uncommitted changes; refusing to edit it in place." >&2
  exit 1
fi

# file ~~ exact source substring ~~ replacement ~~ what it breaks
MUTANTS=(
"src/agent/simulation.py~~            slack_ts=slack_ts,~~            slack_ts=None,~~S1 the mirror mapping is never recorded on an outbound post"
"src/agent/simulation.py~~        return root.slack_ts~~        return thread_ts~~S2 a canonical id is handed to Slack (a93d136)"
"src/agent/slack_client.py~~            self._client = None~~            pass~~S3 a dead token still reports is_connected (the bug found by T11)"
"src/agent/slack_client.py~~        last_exc: SlackApiError | None = None~~        last_exc = None  # noqa~~S4 sanity: this edit is inert and MUST survive"
)

fail=0; killed=0
for m in "${MUTANTS[@]}"; do
  file="${m%%~~*}"; rest="${m#*~~}"
  from="${rest%%~~*}"; rest="${rest#*~~}"
  to="${rest%%~~*}"; label="${rest#*~~}"
  inert=0; [[ "$label" == S4* ]] && inert=1

  cp "$file" "$file.mutbak"
  if ! FROM="$from" TO="$to" python3 - "$file" <<'PY'
import os, pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
frm, to = os.environ["FROM"], os.environ["TO"]
if frm not in s:
    sys.stderr.write(f"target not found in {p}: {frm!r}\n"); sys.exit(1)
p.write_text(s.replace(frm, to, 1))
PY
  then
    mv "$file.mutbak" "$file"
    echo "ERROR   $label — target string not found; the code moved" >&2; fail=1; continue
  fi

  if eval "$RUN" >/dev/null 2>&1; then
    if [ "$inert" -eq 1 ]; then echo "survived (expected)  $label"; killed=$((killed+1))
    else echo "SURVIVED  $label"; fail=1; fi
  else
    if [ "$inert" -eq 1 ]; then
      echo "KILLED AN INERT MUTANT  $label — the suite is flaky, not sensitive" >&2; fail=1
    else echo "killed    $label"; killed=$((killed+1)); fi
  fi
  mv "$file.mutbak" "$file"
done

git diff --quiet -- src/ || { echo "ERROR: src/ not restored" >&2; exit 1; }
echo; echo "killed ${killed}/${#MUTANTS[@]}"
[ "$fail" -eq 0 ] && echo "the live Slack tier has teeth" || echo "SURVIVING MUTANTS" >&2
exit "$fail"
