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
#   5. requirements.lock consistency: every direct dependency in pyproject.toml is in
#      the lock, pinned at a version its specifier allows (scripts/check_lockfile.py) —
#      offline and deterministic, so a pyproject.toml edit can't silently drift from
#      what actually gets installed (#27 I4). It deliberately does NOT compare against
#      a fresh pip-compile: that made the gate red whenever any upstream package
#      published, with no repo change (see the step's own comment). LOCKCHECK=none
#      skips it; LOCKCHECK=strict adds the fresh-resolve comparison as a NOTE.
#   6. Lock smoke test — OPT-IN, OFF BY DEFAULT. Step 5 proves the lock MATCHES
#      pyproject.toml; this proves it actually INSTALLS and IMPORTS on the same
#      Python 3.11 requirements.lock was cut against (#27 I6). Set LOCK_SMOKE=1 to
#      run it; it costs minutes (a full throwaway-venv dependency install), which is
#      why it does not run on every push.
#   7. mypy lint of src/ against a CEILING (MYPY_MAX), same ratchet shape as the ruff
#      ceiling above (#27 I1).
#   8. Full pytest run — unit + integration + characterization + contract — with
#      branch coverage over src/, failing under COV_MIN (a ratchet floor: raise it as
#      coverage grows, never lower it).
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
# SRC_LINT_MAX above: a MEASURED COUNT, not a target — lower it as debt is paid.
# mypy itself is capped in pyproject.toml's dev extra (mypy>=2.3,<2.4, #27 I7)
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
#   measured : 145 findings ("Found 145 errors in 25 files (checked 81 source files)")
#   at commit: 9cbfc00 — measured on a CLEAN `git archive 9cbfc00 src pyproject.toml`
#              export, NEVER the working tree (see the correction below)
#   with     : mypy 2.3.1, python_version = "3.11" from pyproject's [tool.mypy]
#   command  : mypy src --ignore-missing-imports  (identical to the step below)
#   on       : 2026-09-04
#   ceiling  : 150 findings (slack 5)
#
# The 145 are ~30,656 LOC of largely un-annotated FastAPI/SQLAlchemy code; most
# of the debt is Optional/`| None` narrowing (SQLAlchemy relationship attributes,
# dict `.get()` results) rather than missing annotations outright.
#
# WHY FIVE OF SLACK, and not one. (a) The mypy cap is a RANGE, so a rebuilt
# .venv-test may resolve a 2.3.x patch that adds a check; requirements.lock is
# runtime-only and does not pin dev extras, so `<2.4` is the whole bound — a
# separate dev lockfile would be a much larger change than this ceiling needs.
# (b) Roughly fifteen further fix rounds of this branch's backlog still have to
# land against these 145. Five is 3.4 % of 145 — the same proportional headroom
# SRC_LINT_MAX carries (260 over a measured 251, 3.6 %). Tighten it to 146 and
# ordinary work goes red for a reason no reviewer can act on, which is how a
# ratchet gets deleted instead of obeyed. Lower it when the debt is paid.
#
# MEASURE A CLEAN EXPORT, NOT THE WORKING TREE — the previous version of this
# comment is the cautionary tale. It read "(commit 2170efb) ... 145 findings";
# 2170efb re-measures at 147. Nothing drifted: the two extra findings there are
# http_retry.py's `Exception must be derived from BaseException`, fixed three
# minutes later by c1429e2. The 145 was read off a working tree that already
# carried that uncommitted fix and was then labelled with the sha at HEAD.
# Bisected 2026-09-04 on clean exports, one venv, the command above:
#   2170efb 147 | f9541ba 145 | 42f03f4 147 | f29e295 147 | 5090322 147 |
#   79cee44 145 | d7ce1a5 145 | 962aa6c 145 | 9cbfc00 145
# The 145 -> 147 step is 42f03f4 adding two `tuple[Publication | None, bool]`
# [return-value] findings in profile_pipeline.py; 79cee44 fixed that annotation
# and the count returned to 145. Every move this ceiling has made was caused by
# this repository's own code, never by mypy and never by PyPI.
#
# The test does NOT re-run mypy and demand equality with 145. That would be
# #27 I4-e's mistake in a new place: a gate that goes red because a later commit
# legitimately paid off two findings, or because a 2.3.x patch found one more,
# is a gate that gets deleted. The ceiling is the gate; the block above is the
# audit trail for it, and 145 is what it measured on the date it says.
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

# Locate a Python 3.11 interpreter for the steps that must resolve or install against
# the same version requirements.lock was cut against (the Dockerfile's python:3.11-slim
# base), never $VENV_PY's 3.12: an explicit LOCKCHECK_PYTHON pin, else
# `uv python find 3.11` with no downloads, else empty so the caller SKIPs.
#
# Restored here after f9541ba deleted the definition while leaving BOTH call sites
# (the LOCKCHECK=strict branch and the LOCK_SMOKE step). Under `set -euo pipefail` an
# undefined function is exit 127, so `LOCK_SMOKE=1 ./scripts/ci.sh` died at the smoke
# step -- before mypy and before the entire test suite -- while a default run passed.
# Found by the over-implementation audit.
find_lock_python() {
  local spec="${LOCKCHECK_PYTHON:-}"
  if [ -z "$spec" ]; then
    spec="$(uv python find 3.11 --no-python-downloads --no-project 2>/dev/null || true)"
  fi
  printf '%s' "$spec"
}

echo "==> lockfile consistency (requirements.lock matches pyproject.toml)"
# What this does NOT do: compare the lock against a fresh `pip-compile`. That was the
# first implementation, and it cannot be a gate — it compares the committed lock against
# whatever PyPI holds at this instant, so any of ~200 transitive packages publishing a
# release turns the gate red with no change to this repository. Measured 2026-09-04:
# `alembic 1.19.2` was published at 17:10:12Z and the gate went red within the hour,
# reporting "pyproject.toml changed without regenerating it" when nobody had touched it.
# It was not even reproducible on one machine — two back-to-back runs of the same command
# disagreed (alembic 1.19.1 vs 1.19.2, rich 14.3.4 vs 15.0.0) depending on which HTTP
# cache the ephemeral environment saw. Since this script IS the whole gate and the
# pre-push hook runs it (D17), such a check does not protect the lock: it just blocks
# pushes on PyPI's schedule and teaches everyone to set LOCKCHECK=none.
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
# Dockerfile installs requirements.lock on Python 3.11 — measured drift on
# 2026-09-04: anthropic 0.117.0 vs 0.125.0, fastapi 0.139.2 vs 0.141.1, alembic
# 1.18.5 vs 1.19.1, sqlalchemy 2.0.51 vs 2.0.52, slack-sdk 3.43.0 vs 3.44.1,
# uvicorn 0.51.0 vs 0.52.4, boto3 1.43.51 vs 1.43.88 (#27 I6). With no server-side
# CI (D17) the prod image is the first thing that ever executes those exact pins.
#
# OFF BY DEFAULT — set LOCK_SMOKE=1 to run it. This installs requirements.lock
# (with --require-hashes, the same as the Dockerfile) into a throwaway Python 3.11
# venv and imports src.main, src.worker.main and src.agent.main, then discards the
# venv. It is not on by default because a cold `uv` cache means downloading and
# verifying the hashes of every one of the ~80 pinned wheels, which is real network
# time this gate should not add to every push (measured 2026-09-04 with a WARM uv
# cache: 5.9s wall, all three modules imported cleanly with exit 0 — see this
# task's report for the cold-cache caveat). Reuses find_lock_python() (defined
# above, for the freshness check) rather than duplicating the interpreter-discovery
# order.
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

echo "==> CI passed."
