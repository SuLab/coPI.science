#!/usr/bin/env bash
#
# Scoped mutation testing — a periodic quality probe, NOT part of the push gate
# (whole-repo mutation is far too slow to run on every push). Mutates one small,
# pure, security-relevant module and runs only the matching pure unit tests, to
# check whether those tests actually kill the mutants.
#
# Uses mutmut 2.x: 3.x's instrumentation trampoline asserts module names must not
# start with "src.", which every import in this repo does, so 3.x cannot run here.
#
# Usage:
#   scripts/mutation.sh            # run the scoped mutation campaign, then print results
#   scripts/mutation.sh results    # re-print the last run's results
#   scripts/mutation.sh show <id>  # show the diff for one surviving mutant
#   scripts/mutation.sh html       # write an HTML report to html/
#
# Override scope with env: PATHS_TO_MUTATE, TEST_FILE.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="${VENV_PY:-$REPO_ROOT/.venv-test/bin/python}"
PATHS_TO_MUTATE="${PATHS_TO_MUTATE:-src/services/validators.py}"
TEST_FILE="${TEST_FILE:-tests/unit/test_validators.py}"

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: test venv python not found at $VENV_PY" >&2
  exit 1
fi

if [ "${1:-run}" = "run" ]; then
  # mutmut exits nonzero when mutants survive; that is data, not a script error,
  # so don't let `set -e` abort before we print the results table.
  "$VENV_PY" -m mutmut run \
    --paths-to-mutate "$PATHS_TO_MUTATE" \
    --runner "$VENV_PY -m pytest -x -q $TEST_FILE" \
    --simple-output || true
  echo "---- mutation results ----"
  "$VENV_PY" -m mutmut results
else
  "$VENV_PY" -m mutmut "$@"
fi
