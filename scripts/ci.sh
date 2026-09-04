#!/usr/bin/env bash
#
# Local CI gate. Run manually, or automatically before every push once you have
# installed the hook (scripts/install-hooks.sh). There is NO server-side CI and no
# GitHub-side hooks by design — this script is the whole gate, and it runs on push.
# This is a deliberate stance, not an oversight — revisit only if the team decides it
# wants a redundant/remote runner (see issue #27 I1).
#
# Steps:
#   1. Alembic sanity: exactly one head, no duplicate revision ids. Cheap, offline,
#      and first because it catches the one class of breakage that a clean `git merge`
#      and a fully green test suite both miss. See .notes/cohort-system-v2.md §14.
#   2. Alembic round trip: upgrade -> downgrade -> upgrade against a THROWAWAY
#      Postgres that this step starts and destroys itself. On by default since
#      2026-08-04; set CI_MIGRATION_DB=none to skip.
#   3. ruff lint of the test suite. New test code is kept spotless — zero findings.
#   4. ruff lint of src/ against a CEILING (SRC_LINT_MAX) rather than zero. src/
#      carries pre-existing style debt, so this is a ratchet: it blocks NEW debt
#      without demanding the old debt be paid first.
#   5. requirements.lock freshness: regenerate with pip-compile and diff against the
#      committed lock (pins only — pip-compile's own header would never match
#      otherwise), so a pyproject.toml edit can't silently drift from what
#      actually gets installed (#27 I4). Set LOCKCHECK=none to skip.
#   6. mypy lint of src/ against a CEILING (MYPY_MAX), same ratchet shape as the ruff
#      ceiling above (#27 I1).
#   7. Full pytest run — unit + integration + characterization + contract — with
#      branch coverage over src/, failing under COV_MIN (a ratchet floor: raise it as
#      coverage grows, never lower it).
#
# The integration/characterization/contract suites spin an ephemeral Postgres via
# testcontainers, so a reachable Docker daemon is required.
#
# Overridable env: VENV_PY (python interpreter), COV_MIN (coverage floor %),
# SRC_LINT_MAX (src/ lint ceiling), CI_MIGRATION_DB (round-trip DSN, or `none` to
# skip the round trip), MIGCHECK_PORT (host port for the throwaway Postgres),
# MIGRATION_FLOOR (the revision the round trip downgrades to), LOCKCHECK (set to
# `none` to skip the requirements.lock freshness check — offline, or no Python 3.11
# interpreter available), LOCKCHECK_PYTHON (pin the interpreter/uv Python spec the
# check resolves with, instead of auto-detecting one).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Volume baseline for the reclaim in reclaim_test_volumes() below. Captured before any
# step can create one, so the reclaim can only ever consider volumes THIS run produced.
CI_VOLS_BEFORE="$(docker volume ls -q 2>/dev/null | sort)"

VENV_PY="${VENV_PY:-$REPO_ROOT/.venv-test/bin/python}"
# Coverage floor. Re-baselined 35 -> 60 on 2026-08-04. The old 35 was not a judgement
# about this suite; it was measured at 35.66% against a broken tracer. bd68fae added
# `concurrency = ["thread", "greenlet"]` to [tool.coverage.run] — without it coverage
# loses the frame across SQLAlchemy's greenlet switch and stops recording an async
# handler at its first `await db.execute(...)`. The true figure on the same suite and
# the same commit is 61.638% (1167 passed, 120 skipped). 60 leaves ~1.6 points of slack
# so the gate does not go red on ordering noise. Raise as coverage grows, never lower.
COV_MIN="${COV_MIN:-60}"

# Ceiling on ruff findings in src/, NOT a target. Measured 2026-08-04 with the same
# command the ratchet below runs, so the numbers are comparable: origin/main 292, this
# branch's pre-repair tip (8515f65) 308, HEAD 260.
#
# LOWER THIS AS DEBT IS PAID; NEVER RAISE IT. Raising it to make a push go through is
# precisely how those 16 findings got into admin.py in the first place — a ceiling that
# moves up to meet the code is not a gate, it is a logbook.
SRC_LINT_MAX="${SRC_LINT_MAX:-260}"

# Ceiling on mypy findings (`: error:` lines) in src/, same ratchet shape as
# SRC_LINT_MAX above: lower it as debt is paid, never raise it to force a push
# through (#27 I1). Measured 2026-09-04 with the exact command the ratchet below
# runs (`mypy src --ignore-missing-imports`, counting `: error:` lines): 143,
# against 27,618 LOC of largely un-annotated FastAPI/SQLAlchemy code — most of the
# debt is Optional/`| None` narrowing (SQLAlchemy relationship attributes, dict
# `.get()` results) rather than missing annotations outright.
MYPY_MAX="${MYPY_MAX:-143}"

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
  # tests/e2e was the one test directory the gate never linted. Added 2026-08-04,
  # when that tier was first run end to end; it was already at zero findings, so
  # this closes the hole without paying anything down. Two of its nine tests need
  # no server and run in the offline suite, so it is gate-relevant either way.
  tests/e2e
  # The production migration tooling. Not tests, but it is the code an operator runs
  # against a live database during an outage window, so it gets held to the same bar.
  # Verified at zero findings when added 2026-08-04.
  scripts/migrate
  # The backup tooling, same reasoning: it runs as root on the host against both
  # production databases and decides what gets deleted. Added 2026-08-18 at zero
  # findings. See docs/specs/2026-08-18-postgres-backup-verification-design.md.
  scripts/backup
)

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: test venv python not found at $VENV_PY" >&2
  echo "Create it with:" >&2
  echo "  uv venv .venv-test && uv pip install --python .venv-test/bin/python -e '.[dev]'" >&2
  exit 1
fi

if ! "$VENV_PY" -c 'import mypy' >/dev/null 2>&1; then
  echo "ERROR: mypy not installed in ${VENV_PY}'s environment (#27 I1)." >&2
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

# Round trip against a THROWAWAY database. ON BY DEFAULT since 2026-08-04.
#
# The unit tests already pin the migrations' static properties (single head, no
# duplicate ids, every drop guarded with if_exists). What this adds is the one thing
# static analysis cannot show: that upgrade -> downgrade -> upgrade actually RUNS
# clean. It was gated behind an unset CI_MIGRATION_DB for the entire life of the
# cohort branch, so 0022 and 0023 were never round-tripped by the gate at all.
#
# The step brings its own database SERVER — a throwaway postgres:15 container whose
# port is published on 127.0.0.1 and which is destroyed by the EXIT trap below. That
# is not gold-plating; it is the only DSN that actually works here. `ci.sh` runs on
# the HOST, and both obvious DSNs are wrong:
#
#   * `...@postgres:5432/...` is the compose-INTERNAL hostname. On the host it either
#     does not resolve, or — verified 2026-08-04 on this developer's machine — it
#     resolves to an unrelated real server (`postgres.int.hueb.org`) via the LAN's
#     search domain. A migration round trip that DROPs and re-CREATEs schema must
#     never be one DNS record away from someone else's database.
#   * `...@localhost:5432/...` is refused: docker-compose.yml publishes NO host port
#     for the postgres service (only app's 8001), so the dev database is simply not
#     reachable from the host. This is what the plan for this change assumed, and it
#     does not work.
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
#     the container, not its anonymous volume. (Verified 2026-08-18.)
#   * Anonymity alone is not enough either: copi-python-certbot-1's live volume is also
#     anonymous. (Verified 2026-08-18.)
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
# DOWN. Measured 2026-08-04: `chmod 000 src/routers/admin.py` takes the total from 260
# to 193, because that file's 68 findings disappear and one E902 replaces them. The
# ratchet would pass, and the next person would "helpfully" re-baseline the ceiling to
# 193 and lock the loss in. So refuse to produce a number at all.
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

echo "==> lockfile freshness (requirements.lock matches pyproject.toml)"
# pip-compile's own header defeats a raw file diff: it records the exact command it
# was invoked with, including the --output-file path, so a scratch regeneration can
# never byte-match the committed file even when every pin is identical. Fix: diff
# with comment lines stripped (`grep -v '^#'`) on both sides — verified: two
# lockfiles differing only in that header's --output-file path fail a raw `diff -q`
# but are identical once comment lines are stripped (see
# test_lockfile_comparison_ignores_pip_composes_own_header_but_not_real_pin_drift).
#
# pip-compile's resolution is Python-version/platform-sensitive, and the committed
# requirements.lock was generated (Task 27.4) against a throwaway Python 3.11.14
# venv (`uv venv --python 3.11.14`) to match the Dockerfile's `python:3.11-slim`
# base — NOT against $VENV_PY, which is this repo's 3.12 test venv. Re-resolving
# with $VENV_PY here would show spurious drift on every single run, for everyone,
# even when nothing actually changed. So this step ALWAYS resolves with a Python
# 3.11 interpreter, never $VENV_PY:
#   - LOCKCHECK_PYTHON=<path-or-uv-python-spec> pins the interpreter explicitly.
#   - otherwise, `uv python find 3.11 --no-python-downloads` locates an
#     already-installed 3.11 without downloading one.
#   - if neither uv nor a 3.11 interpreter is found, this step SKIPs with a clear
#     message rather than failing the gate over missing tooling — a spurious
#     failure here (from a 3.12-vs-3.11 mismatch) is worse than no check.
# pip-tools itself does not need to be pre-installed anywhere: `uv run --with
# pip-tools` supplies it in an ephemeral, isolated environment layered on top of
# whichever interpreter is chosen, so a full dev-dependency resolve can't perturb
# any persistent venv while running this check.
#
# Set LOCKCHECK=none to skip entirely (offline, or you deliberately don't want this).
if [ "${LOCKCHECK:-}" = "none" ]; then
  echo "    lockfile check skipped (LOCKCHECK=none)"
elif ! command -v uv >/dev/null 2>&1; then
  echo "    SKIP: uv not found on PATH (needed to run pip-compile against a matching"
  echo "    Python 3.11 interpreter — see Task 27.4's report). Install uv, or set"
  echo "    LOCKCHECK=none to silence this."
else
  LOCK_PYSPEC="${LOCKCHECK_PYTHON:-}"
  if [ -z "$LOCK_PYSPEC" ]; then
    LOCK_PYSPEC="$(uv python find 3.11 --no-python-downloads --no-project 2>/dev/null || true)"
  fi
  if [ -z "$LOCK_PYSPEC" ]; then
    echo "    SKIP: no Python 3.11 interpreter found (requirements.lock was cut against"
    echo "    3.11 to match the Dockerfile base — see Task 27.4's report). Install one"
    echo "    with 'uv python install 3.11', set LOCKCHECK_PYTHON=/path/to/python3.11,"
    echo "    or set LOCKCHECK=none to silence this."
  else
    LOCK_TMP="$(mktemp)"
    if ! uv run --isolated --no-project --python "$LOCK_PYSPEC" --with pip-tools -- \
          python -m piptools compile --generate-hashes --no-header \
          --output-file "$LOCK_TMP" pyproject.toml >/dev/null 2>&1; then
      echo "ERROR: pip-compile failed to resolve pyproject.toml — see above." >&2
      rm -f "$LOCK_TMP"
      exit 1
    fi
    if ! diff -q <(grep -v '^#' requirements.lock) <(grep -v '^#' "$LOCK_TMP") >/dev/null; then
      echo "ERROR: requirements.lock is stale — pyproject.toml changed without regenerating it." >&2
      echo "Run: uv run --isolated --no-project --python 3.11 --with pip-tools -- python -m piptools compile --generate-hashes --no-header -o requirements.lock pyproject.toml" >&2
      diff <(grep -v '^#' requirements.lock) <(grep -v '^#' "$LOCK_TMP") >&2 || true
      rm -f "$LOCK_TMP"
      exit 1
    fi
    rm -f "$LOCK_TMP"
    echo "    requirements.lock is current (resolved with $LOCK_PYSPEC)"
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

echo "==> CI passed."
