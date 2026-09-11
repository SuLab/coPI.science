#!/usr/bin/env bash
#
# Local CI gate. Run manually, or automatically before every push once you have
# installed the hook (scripts/install-hooks.sh). CI is deliberately local-only:
# this script, run by the pre-push hook, is the whole gate; there is no
# server-side CI.
#
# Steps:
#   1. Alembic sanity: exactly one head, no duplicate revision ids. Cheap, offline,
#      and first because it catches the one class of breakage that a clean `git merge`
#      and a fully green test suite both miss.
#   2. Alembic round trip: upgrade -> downgrade -> upgrade against a THROWAWAY
#      Postgres that this step starts and destroys itself. On by default; set
#      CI_MIGRATION_DB=none to skip.
#   3. ruff lint of the test suite. New test code is kept spotless — zero findings.
#   4. ruff lint of src/ against a CEILING (SRC_LINT_MAX) rather than zero. src/
#      carries pre-existing style debt, so this is a ratchet: it blocks NEW debt
#      without demanding the old debt be paid first.
#   5. requirements.lock consistency: every direct dependency in pyproject.toml is in
#      the lock, pinned at a version its specifier allows (scripts/check_lockfile.py) —
#      offline and deterministic, so a pyproject.toml edit can't silently drift from
#      what actually gets installed. It deliberately does NOT compare against
#      a fresh pip-compile: that made the gate red whenever any upstream package
#      published, with no repo change (see the step's own comment). LOCKCHECK=none
#      skips it; LOCKCHECK=strict adds the fresh-resolve comparison as a NOTE.
#   6. Lock smoke test — OPT-IN, OFF BY DEFAULT. Step 5 proves the lock MATCHES
#      pyproject.toml; this proves it actually INSTALLS and IMPORTS on the same
#      Python 3.11 requirements.lock was cut against. Set LOCK_SMOKE=1 to
#      run it; it costs minutes (a full throwaway-venv dependency install), which is
#      why it does not run on every push.
#   7. mypy lint of src/ against a CEILING (MYPY_MAX), same ratchet shape as the ruff
#      ceiling above.
#   8. Full pytest run — unit + integration + characterization + contract — with
#      branch coverage over src/, failing under COV_MIN (a ratchet floor: raise it as
#      coverage grows, never lower it).
#   9. A notice, printed last, naming every test tier this gate did NOT run — the
#      live_slack and live_api tiers, which step 8 collects and conftest.py then skips.
#      Counted with `--collect-only`, never executed; informational, never fatal.
#
# The integration/characterization/contract suites spin an ephemeral Postgres via
# testcontainers, so a reachable Docker daemon is required.
#
# Overridable env: VENV_PY (python interpreter), COV_MIN (coverage floor %),
# SRC_LINT_MAX (src/ lint ceiling), CI_MIGRATION_DB (round-trip DSN, or `none` to
# skip the round trip), MIGCHECK_PORT (host port for the throwaway Postgres),
# MIGRATION_FLOOR (the revision the round trip downgrades to), LOCKCHECK (`none` to
# skip the requirements.lock check, `strict` to also report whether a newer resolve is
# available), LOCKCHECK_PYTHON (pin the interpreter/uv Python spec strict mode
# resolves with, instead of auto-detecting one), LOCK_SMOKE (set to `1` to run
# step 6's opt-in requirements.lock install-and-import smoke test — off by default).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Volume baseline for the reclaim in reclaim_test_volumes() below. Captured before any
# step can create one, so the reclaim can only ever consider volumes THIS run produced.
CI_VOLS_BEFORE="$(docker volume ls -q 2>/dev/null | sort)"

VENV_PY="${VENV_PY:-$REPO_ROOT/.venv-test/bin/python}"
# Coverage floor, not a target. `concurrency = ["thread", "greenlet"]` in
# [tool.coverage.run] is required for this to mean anything — without it coverage
# loses the frame across SQLAlchemy's greenlet switch and stops recording an async
# handler at its first `await db.execute(...)`, understating the true figure. The
# floor leaves a couple of points of slack so the gate does not go red on ordering
# noise. Raise as coverage grows, never lower.
COV_MIN="${COV_MIN:-60}"

# Ceiling on ruff findings in src/, NOT a target: src/ carries pre-existing style
# debt that this gate does not demand be paid off all at once, but it blocks NEW
# debt from getting in.
#
# LOWER THIS AS DEBT IS PAID; NEVER RAISE IT. Raising it to make a push go through
# turns a gate into a logbook that moves up to meet whatever the code already does.
SRC_LINT_MAX="${SRC_LINT_MAX:-260}"

# Ceiling on mypy findings (`: error:` lines) in src/, same ratchet shape as
# SRC_LINT_MAX above: a MEASURED COUNT, not a target — lower it as debt is paid.
# mypy itself is capped to a narrow range in pyproject.toml's dev extra
# precisely so this number does not move out from under an unrelated push when
# a new mypy release adds a check. If you deliberately bump the mypy cap,
# re-measure with the exact command this step runs (`mypy src
# --ignore-missing-imports`, counting `: error:` lines) and update this default
# in its OWN commit, with the old and new numbers in the message — do not just
# raise it to make a red push pass; that is how ratchets rot.
#
# PROVENANCE. A bare ceiling with no record of what it was measured against is
# not a ratchet, it is a number nobody can audit — so state what was measured,
# at which commit, with which mypy, and what the ceiling costs on top; and
# restate all of it whenever this default moves. tests/unit/test_ci_gate.py checks
# that the three numbers below stay arithmetically consistent with the default,
# so a raise that does not re-measure fails the gate rather than passing quietly.
# MEASURE A CLEAN EXPORT (`git archive <commit> src pyproject.toml`), NEVER THE
# WORKING TREE — a working-tree measurement can carry an uncommitted fix and get
# mislabelled with the wrong commit.
#   measured : 138 findings ("Found 138 errors in 25 files (checked 81 source files)")
#   at commit: 45d1198
#   with     : mypy 2.3.1, python_version = "3.11" from pyproject's [tool.mypy]
#   command  : mypy src --ignore-missing-imports  (identical to the step below)
#   ceiling  : 150 findings (slack 12)
#
# The 138 findings are largely un-annotated FastAPI/SQLAlchemy code; most of the
# debt is Optional/`| None` narrowing (SQLAlchemy relationship attributes, dict
# `.get()` results) rather than missing annotations outright.
#
# The slack exists because the mypy cap is a RANGE, so a rebuilt .venv-test may
# resolve a later patch that adds a check; requirements.lock is runtime-only and
# does not pin dev extras, so the cap's upper bound is the whole guard. Slack
# should widen only as debt is genuinely paid down (the measured count falling
# while the ceiling default stays put), never because a ceiling was relaxed.
#
# 145 findings (slack 7) is the right ceiling once this branch merges, so the
# current slack of 12 is a temporary allowance, not the steady-state target.
#
# The test does NOT re-run mypy and demand equality with the measured count:
# a gate that goes red because a later commit legitimately paid off findings, or
# because a point-release mypy found one more, is a gate that gets deleted. The
# ceiling is the gate; the block above is the audit trail for it.
MYPY_MAX="${MYPY_MAX:-150}"

# Throwaway-Postgres settings for the migration round trip (step 2). The port is
# published on 127.0.0.1 only. MIGRATION_FLOOR is how far down the round trip goes;
# lowering it widens the round trip, which is always safe on a throwaway database.
MIGCHECK_PORT="${MIGCHECK_PORT:-55432}"
MIGCHECK_CONTAINER="copi-ci-migcheck"
# 0018, not 0021. At 0021 the round trip never executed the 0019/0020/0021
# DOWNGRADES — and those are the ones with teeth: 0019's downgrade drops the
# content columns and puts agent_id back to NOT NULL, and 0019/0020/0021 lack the
# if_exists guards that 0022/0023 have. The gate runs against an empty throwaway
# database, so the NOT NULL step cannot fail here; that is precisely why it is safe
# to exercise, and why the gate still cannot catch the data-dependent failure that
# same step produces on a populated production database. Raise this only to skip
# work deliberately.
MIGRATION_FLOOR="${MIGRATION_FLOOR:-0018}"

LINT_TARGETS=(
  tests/conftest.py tests/factories.py tests/fakes.py
  tests/unit tests/integration tests/characterization tests/contract
  # Two of tests/e2e's nine tests need no server and run in the offline suite, so
  # it is gate-relevant either way.
  tests/e2e
  # The production migration tooling. Not tests, but it is the code an operator runs
  # against a live database during an outage window, so it gets held to the same bar.
  scripts/migrate
  # The backup tooling, same reasoning: it runs as root on the host against both
  # production databases and decides what gets deleted.
  scripts/backup
)

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: test venv python not found at $VENV_PY" >&2
  echo "Create it with:" >&2
  echo "  uv venv .venv-test && uv pip install --python .venv-test/bin/python -e '.[dev]'" >&2
  exit 1
fi

if ! "$VENV_PY" -c 'import mypy' >/dev/null 2>&1; then
  echo "ERROR: mypy not installed in ${VENV_PY}'s environment." >&2
  echo "Install it with: uv pip install --python $VENV_PY mypy" >&2
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

# Round trip against a THROWAWAY database. ON BY DEFAULT.
#
# The unit tests already pin the migrations' static properties (single head, no
# duplicate ids, every drop guarded with if_exists). What this adds is the one thing
# static analysis cannot show: that upgrade -> downgrade -> upgrade actually RUNS
# clean.
#
# The step brings its own database SERVER — a throwaway postgres:15 container whose
# port is published on 127.0.0.1 and which is destroyed by the EXIT trap below. That
# is not gold-plating; it is the only DSN that actually works here. `ci.sh` runs on
# the HOST, and both obvious DSNs are wrong:
#
#   * `...@postgres:5432/...` is the compose-INTERNAL hostname. On the host it either
#     does not resolve, or resolves to an unrelated real server via the LAN's search
#     domain. A migration round trip that DROPs and re-CREATEs schema must never be
#     one DNS record away from someone else's database.
#   * `...@localhost:5432/...` is refused: docker-compose.yml publishes NO host port
#     for the postgres service (only app's 8001), so the dev database is simply not
#     reachable from the host.
#
# Publishing our own port makes `localhost` true by construction, and owning the
# server means this step cannot touch the dev database even in principle.
#
# CI_MIGRATION_DB=none skips the step. CI_MIGRATION_DB=<dsn> runs it against a
# database you supply instead of the throwaway container — NEVER point that at a
# database with data you want.
migcheck_cleanup() { docker rm -f "$MIGCHECK_CONTAINER" >/dev/null 2>&1 || true; }

# The pytest suite spins throwaway Postgres containers via testcontainers. The library
# removes the CONTAINERS but leaves their anonymous data volumes behind — roughly 50MB
# per run, forever, on a host that also serves production. Ryuk (the testcontainers
# reaper) is not running here, so nothing else collects them.
#
# This is deliberately NOT `docker volume prune`, and not a label filter either:
#   * A bare prune would delete copi_pgdata, copi-prod_pgdata, copi-python_grantbot_data
#     and collab-platform_mongodb_data — real, unreferenced, un-backed-up production data.
#   * `--filter label=org.testcontainers=true` matches ZERO volumes: testcontainers labels
#     the container, not its anonymous volume.
#   * Anonymity alone is not enough either: copi-python-certbot-1's live volume is also
#     anonymous.
#
# So the reclaim is triple-guarded. A volume is removed only if it (a) did not exist when
# this script started, (b) is referenced by no container at all, and (c) is anonymous.
# A volume failing any one of those is left alone.
reclaim_test_volumes() {
  local after new anon
  after="$(docker volume ls -q 2>/dev/null | sort)" || return 0
  new="$(comm -13 <(printf '%s\n' "$CI_VOLS_BEFORE") <(printf '%s\n' "$after"))"
  [ -n "$new" ] || return 0
  while IFS= read -r v; do
    [ -n "$v" ] || continue
    [ -z "$(docker ps -aq --filter volume="$v" 2>/dev/null)" ] || continue
    anon="$(docker volume inspect "$v" --format '{{json .Labels}}' 2>/dev/null || echo '{}')"
    case "$anon" in *com.docker.volume.anonymous*) ;; *) continue ;; esac
    if docker volume rm "$v" >/dev/null 2>&1; then
      echo "    reclaimed leaked test volume ${v:0:12}"
    fi
  done <<< "$new"
}

if [ "${CI_MIGRATION_DB:-}" = "none" ]; then
  echo "==> alembic round trip SKIPPED (CI_MIGRATION_DB=none)"
else
  if [ -n "${CI_MIGRATION_DB:-}" ]; then
    migration_dsn="$CI_MIGRATION_DB"
    echo "==> alembic round trip against caller-supplied $migration_dsn"
  else
    migration_dsn="postgresql+asyncpg://copi:copi@127.0.0.1:${MIGCHECK_PORT}/copi_migcheck"
    echo "==> alembic round trip against a throwaway postgres:15 on 127.0.0.1:${MIGCHECK_PORT}"
    # Fixed container name, removed up front as well as on exit, so a run that was
    # killed mid-flight cannot wedge the next one. ci.sh is a serial pre-push gate;
    # two concurrent runs would collide on the port regardless of the name.
    # INT and TERM as well as EXIT: this gate runs for ~6 minutes, so Ctrl-C
    # partway through is the likely case, and a leaked container keeps
    # MIGCHECK_PORT bound — the next run would then fail its readiness wait and
    # look like a broken migration rather than a stale container.
    trap 'migcheck_cleanup; reclaim_test_volumes' EXIT INT TERM
    migcheck_cleanup
    docker run -d --name "$MIGCHECK_CONTAINER" \
      -e POSTGRES_USER=copi -e POSTGRES_PASSWORD=copi -e POSTGRES_DB=copi_migcheck \
      -p "127.0.0.1:${MIGCHECK_PORT}:5432" postgres:15 >/dev/null
    migcheck_ready=0
    for _ in $(seq 1 60); do
      if docker exec "$MIGCHECK_CONTAINER" pg_isready -U copi -q >/dev/null 2>&1; then
        migcheck_ready=1
        break
      fi
      sleep 1
    done
    if [ "$migcheck_ready" -ne 1 ]; then
      echo "ERROR: throwaway postgres on 127.0.0.1:${MIGCHECK_PORT} never became ready." >&2
      echo "Is that port already in use? Override with MIGCHECK_PORT=<n>." >&2
      docker logs "$MIGCHECK_CONTAINER" 2>&1 | tail -20 >&2
      exit 1
    fi
    echo "    throwaway postgres ready"
  fi
  DATABASE_URL="$migration_dsn" "$VENV_PY" -m alembic upgrade head
  DATABASE_URL="$migration_dsn" "$VENV_PY" -m alembic downgrade "$MIGRATION_FLOOR"
  DATABASE_URL="$migration_dsn" "$VENV_PY" -m alembic upgrade head
  echo "    round trip clean (upgrade head -> downgrade ${MIGRATION_FLOOR} -> upgrade head)"
  # Tear down now rather than at exit: pytest below spins its own Postgres via
  # testcontainers and takes minutes, and there is no reason to hold a second server
  # and a bound port for all of it. The EXIT trap stays armed as the failure path.
  if [ -z "${CI_MIGRATION_DB:-}" ]; then
    migcheck_cleanup
    echo "    throwaway postgres destroyed"
  fi
fi

echo "==> ruff (test-suite lint)"
"$VENV_PY" -m ruff check "${LINT_TARGETS[@]}"

echo "==> ruff (src/ ratchet, ceiling ${SRC_LINT_MAX})"
# A ceiling, not zero: src/ carries pre-existing style debt, and demanding it all be
# paid before the next push would just get this gate deleted. What the ceiling buys is
# that NEW debt cannot get in — the cohort branch put 16 findings into admin.py past a
# gate that only ever linted tests/.
#
# Three details that are easy to get wrong here, all of them load-bearing:
#   * --quiet suppresses ruff's trailing "Found N errors." / "[*] N fixable" summary.
#     Without it those two lines are counted as findings and every number is +2.
#   * ruff exits 1 when it finds anything, and this script runs under `set -o pipefail`,
#     so a bare `ruff ... | wc -l` command substitution aborts the whole script at the
#     assignment, silently and with no message. Hence the explicit rc capture.
#   * exit >1 means ruff itself failed (a malformed config, for instance — verified to
#     exit 2). Treat that as a gate failure, never as "zero findings"; a ratchet that
#     fails open is worse than no ratchet.
set +e
src_lint_out="$("$VENV_PY" -m ruff check src --output-format=concise --quiet 2>&1)"
src_lint_rc=$?
set -e
if [ "$src_lint_rc" -gt 1 ]; then
  echo "ERROR: ruff failed to run over src/ (exit ${src_lint_rc}):" >&2
  printf '%s\n' "$src_lint_out" >&2
  exit 1
fi
# The rc check above is not enough on its own. A missing or unreadable path is NOT an
# error exit: ruff emits a single E902 diagnostic and still exits 1, so an I/O problem
# is indistinguishable from "that file has one finding" — and it makes the count go
# DOWN (an unreadable file's own findings disappear and one E902 replaces them). A
# ratchet that would pass on that basis invites re-baselining the ceiling down to lock
# the loss in. So refuse to produce a number at all.
if printf '%s' "$src_lint_out" | grep -q 'E902'; then
  echo "ERROR: ruff could not read part of src/ (E902), so the finding count is not a" >&2
  echo "measurement. Fix the path or the permissions. Do NOT re-baseline SRC_LINT_MAX" >&2
  echo "off a run that reported this." >&2
  printf '%s\n' "$src_lint_out" | grep 'E902' >&2
  exit 1
fi
src_findings="$(printf '%s' "$src_lint_out" | grep -c . || true)"
if [ "$src_findings" -gt "$SRC_LINT_MAX" ]; then
  echo "ERROR: ruff findings in src/ rose to ${src_findings}; the ceiling is ${SRC_LINT_MAX}." >&2
  echo "Fix what you added. Do not raise SRC_LINT_MAX in scripts/ci.sh to make this pass." >&2
  printf '%s\n' "$src_lint_out" >&2
  exit 1
fi
echo "    ${src_findings} findings (ceiling ${SRC_LINT_MAX})"

# Locate a Python 3.11 interpreter for the steps that must resolve or install against
# the same version requirements.lock was cut against (the Dockerfile's python:3.11-slim
# base), never $VENV_PY's 3.12: an explicit LOCKCHECK_PYTHON pin, else
# `uv python find 3.11` with no downloads, else empty so the caller SKIPs.
#
# Both the LOCKCHECK=strict branch and the LOCK_SMOKE step call this. Under
# `set -euo pipefail` an undefined function exits 127, so if this definition is ever
# dropped while its call sites remain, `LOCK_SMOKE=1 ./scripts/ci.sh` dies at the
# smoke step — before mypy and before the entire test suite — while a default run
# still passes.
find_lock_python() {
  local spec="${LOCKCHECK_PYTHON:-}"
  if [ -z "$spec" ]; then
    spec="$(uv python find 3.11 --no-python-downloads --no-project 2>/dev/null || true)"
  fi
  printf '%s' "$spec"
}

echo "==> lockfile consistency (requirements.lock matches pyproject.toml)"
# What this does NOT do: compare the lock against a fresh `pip-compile`. That cannot be
# a gate — it compares the committed lock against whatever PyPI holds at this instant,
# so any of ~200 transitive packages publishing a release turns the gate red with no
# change to this repository, and the result is not even reproducible on one machine
# (a resolve can disagree with itself run to run depending on which HTTP cache the
# ephemeral environment sees). Since this script IS the whole gate and the pre-push
# hook runs it, such a check does not protect the lock: it just blocks pushes on
# PyPI's schedule and teaches everyone to set LOCKCHECK=none.
#
# The drift that matters is inside the repo — a dependency edited in pyproject.toml
# without regenerating the lock — and that is decided by the two files alone. So it is
# checked from the two files, deterministically and offline:
#   * every direct dependency appears in the lock, and
#   * the pinned version satisfies the specifier pyproject declares.
# Adding, removing or re-constraining a dependency without regenerating fails, naming the
# package. LOCK_SMOKE=1 (next step) proves the lock actually installs and imports.
#
#   LOCKCHECK=none    skip entirely
#   LOCKCHECK=strict  ALSO run the old fresh-resolve comparison (needs network + a 3.11
#                     interpreter). Use it deliberately when asking "could this lock be
#                     newer?", not as a merge gate.
if [ "${LOCKCHECK:-}" = "none" ]; then
  echo "    lockfile check skipped (LOCKCHECK=none)"
else
  "$VENV_PY" scripts/check_lockfile.py || exit 1

  if [ "${LOCKCHECK:-}" = "strict" ]; then
    echo "==> lockfile freshness (LOCKCHECK=strict: is a newer resolve available?)"
    if ! command -v uv >/dev/null 2>&1; then
      echo "    SKIP: uv not found on PATH; strict mode needs it to re-resolve."
    else
      LOCK_PYSPEC="$(find_lock_python)"
      if [ -z "$LOCK_PYSPEC" ]; then
        echo "    SKIP: no Python 3.11 interpreter found (the lock is cut against 3.11 to"
        echo "    match the Dockerfile base). 'uv python install 3.11', or set LOCKCHECK_PYTHON."
      else
        LOCK_TMP="$(mktemp)"
        LOCK_COMPILE_LOG="$(mktemp)"
        if ! uv run --isolated --no-project --python "$LOCK_PYSPEC" --with pip-tools -- \
              python -m piptools compile --generate-hashes --no-header \
              --output-file "$LOCK_TMP" pyproject.toml >"$LOCK_COMPILE_LOG" 2>&1; then
          echo "    SKIP: pip-compile could not resolve pyproject.toml (usually PyPI being"
          echo "    unreachable). Its output:"
          sed 's/^/    /' "$LOCK_COMPILE_LOG" >&2
        elif ! diff -q <(grep -v '^#' requirements.lock) <(grep -v '^#' "$LOCK_TMP") >/dev/null; then
          echo "    NOTE: a fresh resolve differs from the committed lock — upstream has"
          echo "    published something newer, or a pin moved. This is informational; it"
          echo "    does not fail the gate. Regenerate when you want the newer versions:"
          echo "      uv run --isolated --no-project --python 3.11 --with pip-tools -- python -m piptools compile --generate-hashes --no-header -o requirements.lock pyproject.toml"
          diff <(grep -v '^#' requirements.lock) <(grep -v '^#' "$LOCK_TMP") | grep '^[<>][a-z]' | head -20
        else
          echo "    requirements.lock equals a fresh resolve (resolved with $LOCK_PYSPEC)"
        fi
        rm -f "$LOCK_TMP" "$LOCK_COMPILE_LOG"
      fi
    fi
  fi
fi

echo "==> lock smoke test (opt-in: LOCK_SMOKE=1)"
# The freshness check above proves requirements.lock MATCHES pyproject.toml; it
# proves nothing about whether the lock actually WORKS. .venv-test (this whole
# script's $VENV_PY) is Python 3.12 resolved fresh from pyproject.toml, while the
# Dockerfile installs requirements.lock on Python 3.11, so several direct
# dependencies resolve to different versions between the two. With no server-side
# CI the prod image is the first thing that ever executes those exact pins.
#
# OFF BY DEFAULT — set LOCK_SMOKE=1 to run it. This installs requirements.lock
# (with --require-hashes, the same as the Dockerfile) into a throwaway Python 3.11
# venv and imports src.main, src.worker.main and src.agent.main, then discards the
# venv. It is not on by default because a cold `uv` cache means downloading and
# verifying the hashes of every one of the ~80 pinned wheels, which is real network
# time this gate should not add to every push (a warm cache costs only a few seconds).
# Reuses find_lock_python() (defined above, for the freshness check) rather than
# duplicating the interpreter-discovery order.
if [ "${LOCK_SMOKE:-}" != "1" ]; then
  echo "    skipped (opt-in — set LOCK_SMOKE=1 to prove requirements.lock actually installs and imports)"
elif ! command -v uv >/dev/null 2>&1; then
  echo "    SKIP: uv not found on PATH (needed for the throwaway 3.11 venv). Install uv."
else
  SMOKE_PYSPEC="$(find_lock_python)"
  if [ -z "$SMOKE_PYSPEC" ]; then
    echo "    SKIP: no Python 3.11 interpreter found (same requirement as the lockfile"
    echo "    freshness check above). Install one with 'uv python install 3.11', or set"
    echo "    LOCKCHECK_PYTHON=/path/to/python3.11."
  else
    SMOKE_VENV="$(mktemp -d)"
    SMOKE_LOG="$(mktemp)"
    if ! uv venv --python "$SMOKE_PYSPEC" "$SMOKE_VENV" >"$SMOKE_LOG" 2>&1 \
        || ! uv pip install --python "$SMOKE_VENV/bin/python" --require-hashes \
             -r requirements.lock >>"$SMOKE_LOG" 2>&1; then
      echo "ERROR: LOCK_SMOKE could not install requirements.lock into a throwaway" >&2
      echo "3.11 venv:" >&2
      cat "$SMOKE_LOG" >&2
      rm -rf "$SMOKE_VENV"
      rm -f "$SMOKE_LOG"
      exit 1
    fi
    if ! "$SMOKE_VENV/bin/python" -c \
        'import src.main, src.worker.main, src.agent.main' >>"$SMOKE_LOG" 2>&1; then
      echo "ERROR: LOCK_SMOKE installed requirements.lock but importing src.main," >&2
      echo "src.worker.main or src.agent.main failed:" >&2
      cat "$SMOKE_LOG" >&2
      rm -rf "$SMOKE_VENV"
      rm -f "$SMOKE_LOG"
      exit 1
    fi
    rm -rf "$SMOKE_VENV"
    rm -f "$SMOKE_LOG"
    echo "    PASS  requirements.lock installs on Python 3.11 and src.main/src.worker.main/src.agent.main import cleanly"
  fi
fi

echo "==> mypy (src/ ratchet, ceiling ${MYPY_MAX})"
# Same shape as the ruff src/ ratchet above: a ceiling, not zero — src/ is largely
# un-annotated FastAPI/SQLAlchemy code, so demanding it all be fixed before the next
# push would just get this stage deleted. rc>1 means mypy itself failed to run (a
# malformed config, an internal error), which is a gate failure, never "zero
# findings" — mirrors the ruff step's E902 handling.
set +e
mypy_out="$("$VENV_PY" -m mypy src --ignore-missing-imports 2>&1)"
mypy_rc=$?
set -e
if [ "$mypy_rc" -gt 1 ]; then
  echo "ERROR: mypy failed to run over src/ (exit ${mypy_rc}):" >&2
  printf '%s\n' "$mypy_out" >&2
  exit 1
fi
mypy_findings="$(printf '%s' "$mypy_out" | grep -c ': error:' || true)"
if [ "$mypy_findings" -gt "$MYPY_MAX" ]; then
  echo "ERROR: mypy findings in src/ rose to ${mypy_findings}; the ceiling is ${MYPY_MAX}." >&2
  echo "Fix what you added. Do not raise MYPY_MAX in scripts/ci.sh to make this pass." >&2
  printf '%s\n' "$mypy_out" >&2
  exit 1
fi
echo "    ${mypy_findings} findings (ceiling ${MYPY_MAX})"

echo "==> pytest (full suite + branch coverage, fail-under=${COV_MIN}%)"
"$VENV_PY" -m pytest tests/ \
  --cov=src --cov-report=term-missing \
  --cov-fail-under="${COV_MIN}"

echo "==> reclaiming leaked testcontainers volumes"
reclaim_test_volumes
echo "    done"

echo "==> gated tiers this gate did NOT run"
# Said LAST, on purpose: it is the one line an operator reading a green gate needs and
# would otherwise never see.
#
# The two live tiers are SKIPPED, not deselected. The pytest step above runs `pytest
# tests/` with no `-m` expression at all; tests/conftest.py's
# pytest_collection_modifyitems adds a `skip` marker to every `live_slack` test when the
# workspace credentials are absent, and to every `live_api` test when LIVE_API_TESTS is
# unset. That is deliberate — a skip reports "skipped" where a collection filter would
# report the ambiguous "no tests ran" — but its cost is that both tiers land inside the
# "N skipped" tally above, indistinguishable from an ordinary skip. That silence is how
# a data-loss regression once reached HEAD while the test that catches it sat green
# and unrun in the live Slack tier.
#
# The counts are MEASURED here, never written down. A literal count in this file would
# be the same defect as a stale provenance comment: right on the day it is typed,
# silently wrong the first time somebody adds a test to a tier, and wrong in the
# direction that under-reports what was skipped. `--collect-only` imports the test
# modules and stops — no credentials, no fixtures, no test body; nothing is posted to
# any Slack workspace and no third-party API is called, and it costs only a few
# seconds per tier (the whole suite is collected either way) against an ~8-minute
# gate. tests/unit/test_ci_gate.py runs the same two collections and fails if what this
# step prints differs from what they find, so the notice cannot drift.
#
# INFORMATIONAL: this step never fails the gate, and it never runs either tier. The
# gate's environment has no Slack credentials by design, and the tier writes to a real
# workspace — scripts/run_live_slack.sh is the only supported way in, because it proves
# the production credentials are blanked before pytest is reached.
tier_pytest="${VENV_PY%/*}/pytest"
for gated_tier in \
    "live_slack|run them with: scripts/run_live_slack.sh" \
    "live_api|run them with: LIVE_API_TESTS=1 ${tier_pytest} tests/ -m live_api"; do
  tier_marker="${gated_tier%%|*}"
  tier_howto="${gated_tier#*|}"
  # PYTEST_ADDOPTS is emptied for this command alone (not unset globally — the pytest
  # step above is entitled to the operator's options). It is prepended to pytest's
  # argv, so an exported `-q` makes this run quiet LEVEL TWO, which prints one
  # `path: n` line per file instead of node ids and no total at all — the count then
  # reads 0 and the notice under-reports in exactly the silence it exists to break.
  # Found by tests/unit/test_ci_gate.py, which sets PYTEST_ADDOPTS to stub the pytest
  # step; a count that depends on what the caller exported is not a count.
  set +e
  tier_collect="$(PYTEST_ADDOPTS= "$VENV_PY" -m pytest tests/ -m "$tier_marker" \
    --collect-only -q -p no:cacheprovider 2>&1)"
  tier_rc=$?
  set -e
  if [ "$tier_rc" -ne 0 ]; then
    # Not fatal, but not silent either: a marker that has been renamed or a tier that
    # has been deleted must read as "we do not know", never as a count.
    echo "    ${tier_marker}: COUNT UNAVAILABLE — \`pytest --collect-only\` exited ${tier_rc};"
    echo "        the marker may have been renamed or the tier removed. Otherwise, ${tier_howto}"
    continue
  fi
  # One node id per collected test, and only node ids carry `::` — the -q summary line
  # and any warnings do not.
  tier_n="$(printf '%s\n' "$tier_collect" | grep -c '::' || true)"
  echo "    ${tier_marker}: ${tier_n} tests skipped at collection and NOT run here — ${tier_howto}"
done

echo "==> CI passed."
