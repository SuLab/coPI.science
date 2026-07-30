#!/usr/bin/env bash
#
# Local CI gate. Run manually, or automatically before every push once you have
# installed the hook (scripts/install-hooks.sh). There is NO server-side CI and no
# GitHub-side hooks by design — this script is the whole gate, and it runs on push.
#
# Steps:
#   1. Alembic sanity: exactly one head, no duplicate revision ids. Cheap, offline,
#      and first because it catches the one class of breakage that a clean `git merge`
#      and a fully green test suite both miss. See .notes/cohort-system-v2.md §14.
#   2. ruff lint of the test suite. (New test code is kept clean. Legacy src/ carries
#      pre-existing style debt — out of scope for this behavior-pinning gate; lint it
#      separately with `ruff check src` when you're ready to pay that down.)
#   3. Full pytest run — unit + integration + characterization + contract — with
#      branch coverage over src/, failing under COV_MIN (a ratchet floor: raise it as
#      coverage grows, never lower it).
#
# The integration/characterization/contract suites spin an ephemeral Postgres via
# testcontainers, so a reachable Docker daemon is required.
#
# Overridable env: VENV_PY (python interpreter), COV_MIN (coverage floor %).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="${VENV_PY:-$REPO_ROOT/.venv-test/bin/python}"
COV_MIN="${COV_MIN:-35}"  # measured 35.66% on this suite; raise as coverage grows, never lower

LINT_TARGETS=(
  tests/conftest.py tests/factories.py tests/fakes.py
  tests/unit tests/integration tests/characterization tests/contract
)

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: test venv python not found at $VENV_PY" >&2
  echo "Create it with:" >&2
  echo "  uv venv .venv-test && uv pip install --python .venv-test/bin/python -e '.[dev]'" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon not reachable — the integration/characterization/contract" >&2
  echo "suites need it (testcontainers spins an ephemeral Postgres). Start Docker and retry." >&2
  exit 1
fi

echo "==> alembic (single head, no duplicate revision ids)"
# Two migrations sharing a revision id is invisible to git and to pytest: the merge
# is clean, every test passes, and Alembic only warns. The damage shows up at deploy
# — `alembic upgrade head` dies on multiple heads, and a targeted `upgrade <rev>`
# silently applies whichever duplicate sorts last while stamping the DB as fully
# migrated. Assign revision ids at merge, never at branch.
dupes="$(grep -h '^revision' alembic/versions/*.py | sort | uniq -d || true)"
if [ -n "$dupes" ]; then
  echo "ERROR: duplicate alembic revision ids:" >&2
  echo "$dupes" >&2
  grep -l "^revision" alembic/versions/*.py | while read -r f; do
    printf '  %s -> %s\n' "$f" "$(grep -m1 '^revision' "$f")" >&2
  done
  exit 1
fi
# `alembic heads` reads only the script directory — no database needed.
heads_out="$("$VENV_PY" -m alembic heads 2>/dev/null || true)"
heads_n="$(printf '%s\n' "$heads_out" | grep -c '[^[:space:]]' || true)"
if [ "$heads_n" -ne 1 ]; then
  echo "ERROR: expected exactly 1 alembic head, found ${heads_n}:" >&2
  printf '%s\n' "$heads_out" >&2
  echo "Renumber the newer migration onto the current head before merging." >&2
  exit 1
fi
echo "    single head: $(printf '%s\n' "$heads_out" | tr -d '\n')"

echo "==> ruff (test-suite lint)"
"$VENV_PY" -m ruff check "${LINT_TARGETS[@]}"

echo "==> pytest (full suite + branch coverage, fail-under=${COV_MIN}%)"
"$VENV_PY" -m pytest tests/ \
  --cov=src --cov-report=term-missing \
  --cov-fail-under="${COV_MIN}"

echo "==> CI passed."
