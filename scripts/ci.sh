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
#   2. Alembic round trip: upgrade -> downgrade -> upgrade against a THROWAWAY
#      Postgres that this step starts and destroys itself. On by default since
#      2026-08-04; set CI_MIGRATION_DB=none to skip.
#   3. ruff lint of the test suite. New test code is kept spotless — zero findings.
#   4. ruff lint of src/ against a CEILING (SRC_LINT_MAX) rather than zero. src/
#      carries pre-existing style debt, so this is a ratchet: it blocks NEW debt
#      without demanding the old debt be paid first.
#   5. Full pytest run — unit + integration + characterization + contract — with
#      branch coverage over src/, failing under COV_MIN (a ratchet floor: raise it as
#      coverage grows, never lower it).
#
# The integration/characterization/contract suites spin an ephemeral Postgres via
# testcontainers, so a reachable Docker daemon is required.
#
# Overridable env: VENV_PY (python interpreter), COV_MIN (coverage floor %),
# SRC_LINT_MAX (src/ lint ceiling), CI_MIGRATION_DB (round-trip DSN, or `none` to
# skip the round trip), MIGCHECK_PORT (host port for the throwaway Postgres),
# MIGRATION_FLOOR (the revision the round trip downgrades to).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

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
# branch's pre-repair tip (8515f65) 308, HEAD 260. Re-measured 2026-08-12 (final audit
# wave, fix 8) with the same command: HEAD 249 — lowered from 260 to lock in the debt
# already paid down by this wave.
#
# LOWER THIS AS DEBT IS PAID; NEVER RAISE IT. Raising it to make a push go through is
# precisely how those 16 findings got into admin.py in the first place — a ceiling that
# moves up to meet the code is not a gate, it is a logbook.
SRC_LINT_MAX="${SRC_LINT_MAX:-249}"

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
    trap migcheck_cleanup EXIT INT TERM
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

echo "==> pytest (full suite + branch coverage, fail-under=${COV_MIN}%)"
"$VENV_PY" -m pytest tests/ \
  --cov=src --cov-report=term-missing \
  --cov-fail-under="${COV_MIN}"

echo "==> CI passed."
