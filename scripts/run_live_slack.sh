#!/usr/bin/env bash
#
# Run the `live_slack` test tier against the `copi-test` workspace — and nothing else.
#
# This script NEVER sources .env. That file holds live production bot tokens and a live
# app-config token pair, and `src/config.Settings` reads it by default, so sourcing it
# (or letting its values through) is precisely how the tier would end up posting into
# the production workspace. Instead:
#
#   1. every production Slack credential var is exported EMPTY. Empty, not unset: an
#      absent var falls through to .env and the live value wins, while a present-but-
#      empty var overrides the file (measured with a scratch .env — see
#      scripts/live_slack_preflight.py's docstring). The key list is derived from
#      Settings' own fields by `--print-blank-keys`, so an agent added to config.py
#      later is blanked too. Names only; no value is ever read or printed.
#   2. scripts/live_slack_preflight.py must exit 0. It re-proves the blanking through
#      the real settings object, asks Slack's own auth.test which workspace each
#      fixture token belongs to (check 3), and — audit 2026-09-08 RC-13 — confirms
#      profiles/{public,private,memory} under `profiles_dir` (env COPI_PROFILES_DIR,
#      default "profiles") exist and are writable, creating them if missing (check 6).
#      An unwritable profiles dir is exactly RC-7's silent-clobber trigger, so this
#      tier refuses up front rather than discovering it mid-run. A non-zero exit from
#      any check ABORTS — pytest is never reached.
#   3. only then does pytest run, with the tier's own SLACK_TEST_* environment.
#
# The tier's credentials come from YOUR environment, never from this repo. Put them in
# a file that holds nothing else and export it before calling this script:
#
#   set -a; . ~/.copi-test.env; set +a     # SLACK_TEST_* only — never .env
#   scripts/run_live_slack.sh
#
# Extra arguments are passed through to pytest (e.g. -k, -x, --lf).
#
# Overridable env: VENV_PY (interpreter; default .venv-test/bin/python),
# COPI_PROFILES_DIR (where agent profiles live on disk; default "profiles" — point
# this at a writable directory if the repo's own profiles/ is not, e.g. root-owned).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="${VENV_PY:-$REPO_ROOT/.venv-test/bin/python}"
if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: interpreter not found: $VENV_PY (set VENV_PY)" >&2
  exit 1
fi

# `python scripts/x.py` puts scripts/ on sys.path, not the repo root, so `import src`
# needs this. Prepended, so the checkout's own src/ wins over anything installed.
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "==> blanking every production Slack credential in this process's environment"
blank_keys="$("$VENV_PY" scripts/live_slack_preflight.py --print-blank-keys)"
blank_count=0
while IFS= read -r key; do
  [ -n "$key" ] || continue
  export "${key}="
  blank_count=$((blank_count + 1))
done <<< "$blank_keys"
echo "    ${blank_count} credential vars exported empty (names only; no value read)"

echo "==> live-Slack isolation preflight"
if ! "$VENV_PY" scripts/live_slack_preflight.py; then
  echo "REFUSING to run the live Slack tier: the preflight did not prove isolation." >&2
  echo "Nothing was sent to Slack and pytest was not started." >&2
  exit 1
fi

echo "==> pytest -m live_slack"
exec "$VENV_PY" -m pytest tests/ -m live_slack -p no:cacheprovider "$@"
