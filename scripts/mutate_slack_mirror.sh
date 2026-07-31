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
