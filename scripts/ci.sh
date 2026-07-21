#!/usr/bin/env bash
#
# Local CI gate. Run manually, or automatically before every push once you have
# installed the hook (scripts/install-hooks.sh). There is NO server-side CI and no
# GitHub-side hooks by design — this script is the whole gate, and it runs on push.
#
# Steps:
#   1. ruff lint of the test suite. (New test code is kept clean. Legacy src/ carries
#      pre-existing style debt — out of scope for this behavior-pinning gate; lint it
#      separately with `ruff check src` when you're ready to pay that down.)
#   2. Full pytest run — unit + integration + characterization + contract — with
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

echo "==> ruff (test-suite lint)"
"$VENV_PY" -m ruff check "${LINT_TARGETS[@]}"

echo "==> pytest (full suite + branch coverage, fail-under=${COV_MIN}%)"
"$VENV_PY" -m pytest tests/ \
  --cov=src --cov-report=term-missing \
  --cov-fail-under="${COV_MIN}"

echo "==> CI passed."
