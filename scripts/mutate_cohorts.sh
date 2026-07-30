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
# Usage:
#   TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_test \
#     ./scripts/mutate_cohorts.sh
#
# Overridable env: RUNNER (how to invoke pytest), TEST_DATABASE_URL (required).
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${TEST_DATABASE_URL:?set TEST_DATABASE_URL to a throwaway database}"

TESTS="tests/unit/test_cohort_isolation.py tests/integration/test_cohort_engine_live.py tests/integration/test_cohort_admin.py"
RUNNER="${RUNNER:-docker compose exec -T -e TEST_DATABASE_URL=$TEST_DATABASE_URL app python}"

if ! git diff --quiet -- src/; then
  echo "ERROR: src/ has uncommitted changes. This script edits src/ in place and" >&2
  echo "restores from a backup; refusing to run with work that could be lost." >&2
  exit 1
fi

# file ~~ exact source substring ~~ replacement ~~ what it breaks
# The delimiter is ~~ and not | because one target contains a pipe
# (`gates[aid] = mates | unrestricted`) — the very line whose mutation is M2.
MUTANTS=(
"src/services/cohorts.py~~gates[aid] = set() if isolate_uncohorted else None~~gates[aid] = set()~~M1 open-policy uncohorted agent is silenced instead of unrestricted"
"src/services/cohorts.py~~gates[aid] = mates | unrestricted~~gates[aid] = mates~~M2 the open-policy asymmetry (a REAL defect the suite missed)"
"src/services/cohorts.py~~effective = cohort_count if live_members is None else live_members~~effective = cohort_count~~M3 preflight counts cohorts, not live members, so an empty cohort silences the roster"
"src/agent/message_log.py~~    if not entry.is_bot:~~    if entry.sender_agent_id is None:~~M4 the human bypass keys on a NULL agent_id, so an unattributable bot row leaks"
"src/agent/message_log.py~~    if entry.visibility == VISIBILITY_COLLAB_PRIVATE:~~    if False:~~M5 the private-channel exemption is dead"
"src/agent/simulation.py~~            visibility=self._resolve_channel_visibility(channel),~~            visibility=VISIBILITY_PUBLIC,~~M6 outbound messages are never stamped collab_private (a REAL defect the suite missed)"
"src/agent/simulation.py~~            if thread.grandfathered:\n                continue~~            if False:\n                continue~~M7 a grandfathered thread keeps reactive priority"
"src/agent/simulation.py~~        if self._reactive_streak < settings.max_consecutive_reactive_turns:~~        if True:~~M8 the fairness valve never closes"
"src/agent/simulation.py~~            if target_id == agent.agent_id or target_id in allowed:~~            if True:~~M9 the outbound tag strip never strips"
)

fail=0
killed=0

for m in "${MUTANTS[@]}"; do
  file="${m%%~~*}"; rest="${m#*~~}"
  from="${rest%%~~*}"; rest="${rest#*~~}"
  to="${rest%%~~*}"; label="${rest#*~~}"

  cp "$file" "$file.mutbak"
  if ! FROM="$from" TO="$to" python3 - "$file" <<'PY'
import os, pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
frm = os.environ["FROM"].replace("\\n", "\n")
to = os.environ["TO"].replace("\\n", "\n")
if frm not in s:
    sys.stderr.write(f"mutation target not found in {p}:\n{frm!r}\n")
    sys.exit(1)
p.write_text(s.replace(frm, to, 1))
PY
  then
    mv "$file.mutbak" "$file"
    echo "ERROR   $label — target string not found; the code moved, fix this script" >&2
    fail=1
    continue
  fi

  if $RUNNER -m pytest $TESTS -q -m 'not real_llm' >/dev/null 2>&1; then
    echo "SURVIVED  $label"
    fail=1
  else
    echo "killed    $label"
    killed=$((killed + 1))
  fi
  mv "$file.mutbak" "$file"
done

if ! git diff --quiet -- src/; then
  echo "ERROR: src/ was not restored cleanly. Inspect 'git diff -- src/' before doing" >&2
  echo "anything else." >&2
  exit 1
fi

echo
echo "killed ${killed}/${#MUTANTS[@]}"
if [ "$fail" -eq 0 ]; then
  echo "all mutants killed — the cohort suite has teeth"
else
  echo "SURVIVING MUTANTS — a behaviour above is untested. Add the test that kills it." >&2
fi
exit "$fail"
