#!/usr/bin/env bash
#
# Guided production migration: dump -> preflight -> apply -> read-back -> postflight.
# Every in-image step runs in a ONE-OFF container off the image you just built
# (`docker compose -f docker-compose.prod.yml run --rm --no-deps ...`), never an exec
# into the running service, which under migrate-before-serve is still the OLD image.
# The default --target is the image's own preflight.DEFAULT_TARGET, which CI pins to the
# single alembic head. Supported starting points: SUPPORTED_START_REVISIONS in
# scripts/migrate/preflight.py.
#
# Run it as CLAUDE.md's deploy step 4, after the build and before `up -d`, in place of a
# bare `alembic upgrade head`. Rehearse first (no --apply).
#
# READ docs/production-migration.md BEFORE RUNNING THIS. This script is the
# executable half of that runbook; the runbook explains *why* each step is where
# it is, which matters when a step fails.
#
# DEFAULT IS A REHEARSAL. Without --apply nothing is written: it runs the checks,
# prints the exact commands it *would* run, and tells you whether you are clear to
# proceed. That is deliberate — every other tool in this directory is dry-run by
# default and an operator who learns the convention from one must not be caught by
# another.
#
#   ./scripts/migrate/run_migration.sh                      # rehearse, write nothing
#   ./scripts/migrate/run_migration.sh --apply              # back up, migrate, verify
#   ./scripts/migrate/run_migration.sh --apply \
#       --backup-verified-elsewhere "nightly base backup + WAL, restore tested 2026-08-04"
#
# --backup-verified-elsewhere is the ONLY way to skip taking a dump, and it makes
# you write down what you are asserting instead. There is deliberately no bare
# "skip the backup check" flag: a safety tool must not accept a flag that quietly
# does nothing, and an operator must not be able to turn the check off without
# stating a reason that ends up in the log.
#
# MIGRATE_DATABASE_URL (preferred) or --database-url is the only way to point it at a
# database other than the service's own DATABASE_URL. A DATABASE_URL exported in the
# host shell is IGNORED: the service's DSN is read inside the container and only its
# redacted form reaches this shell. A DSN you supply is exported as DATABASE_URL into
# this script's environment and passed to each container BY NAME (`-e DATABASE_URL`),
# so it never appears on a docker argv. --database-url itself still puts the value in
# THIS script's argv (visible to `ps` and /proc for the whole run); prefer
#   MIGRATE_DATABASE_URL='postgresql+asyncpg://…' ./scripts/migrate/run_migration.sh
# The dump is always taken from MIGRATE_PG_SERVICE, so --apply against a DSN whose
# host is anything else is refused unless --backup-verified-elsewhere is given.
#
# ENVIRONMENT
#   COMPOSE_FILE              compose file (default docker-compose.prod.yml)
#   MIGRATE_DATABASE_URL      target DSN (default: the service's own DATABASE_URL)
#   MIGRATE_SERVICE           service whose image runs the python steps (default blackbird-app)
#   MIGRATE_PG_SERVICE        postgres service (default postgres)
#   MIGRATE_BACKUP_DIR        host backup directory, mounted at /app/backups (default backups)
#   ALEMBIC_LOCK_TIMEOUT_MS   lock wait passed into the upgrade container (default 10000)
#
# EXIT CODES
#   0  rehearsal clear / migration applied and verified
#   1  BLOCKED — a check failed. Fix and re-run.
#   2  rehearsal only: warnings you should read. Nothing written.
#      (In --apply mode warnings do not stop the run — you already chose to proceed —
#      so a successful apply is still 0.)
#   3  operational failure (unreachable DB, backup failed)
#  64  usage error
#
# WHAT THIS DOES NOT DO, on purpose:
#   * It does not stop or start the agent/worker/web services. Deciding when your
#     traffic can pause is not a script's call, and a half-stopped deployment is
#     worse than a refused one. It checks that nothing is holding a lock and tells
#     you what to stop. (The containers it starts are one-off and --rm.)
#   * It does not run scripts/backfill_slack_ts.py. That one talks to Slack, needs a
#     valid bot token in every affected channel, and its output needs a human to
#     read. It is step 8 of the runbook, after this script.
#   * It does not resolve duplicate (simulation_run_id, message_ts) rows. That is
#     scripts/migrate/remediate_duplicates.py, which refuses the ambiguous cases on
#     purpose. This script tells you to run it and stops.
set -euo pipefail

EX_OK=0; EX_BLOCKED=1; EX_WARN=2; EX_OPERATIONAL=3; EX_USAGE=64

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

APPLY=0
TARGET=""           # resolved in Step 1 unless --target is given
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
SVC="${MIGRATE_SERVICE:-blackbird-app}"
PG_SVC="${MIGRATE_PG_SERVICE:-postgres}"
# Never add --use-aliases to a `run` below: the web service is on the shared copi-edge
# network, and an alias there collides with the other deployment's services.
DC=(docker compose -f "$COMPOSE_FILE")
BACKUP_DIR="${MIGRATE_BACKUP_DIR:-backups}"
BACKUP_MOUNT=/app/backups
SNAP_CTR="$BACKUP_MOUNT/preflight_snapshot.json"
LOCK_TIMEOUT_MS="${ALEMBIC_LOCK_TIMEOUT_MS:-10000}"   # alembic/env.py's own default
DSN="${MIGRATE_DATABASE_URL:-}"   # or --database-url; a host DATABASE_URL is ignored
DSN_SRC="MIGRATE_DATABASE_URL"
DSN_ENV=()          # (-e DATABASE_URL), by name, only when a DSN was supplied
RUN_ENV=()          # per-call container environment for run_img
BACKUP_VERIFIED_REASON=""
EXTRA_PREFLIGHT=()
SCRIPT_WARN=0

die_usage() { echo "ERROR: $*" >&2; echo "See docs/production-migration.md" >&2; exit "$EX_USAGE"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --target) TARGET="${2:?--target needs a revision}"; shift 2 ;;
    --database-url)
      CLI_DSN="${2:?--database-url needs a DSN}"; shift 2
      if [ -n "$DSN" ] && [ "$DSN" != "$CLI_DSN" ]; then
        die_usage "MIGRATE_DATABASE_URL and --database-url name different DSNs. Pass one."
      fi
      DSN="$CLI_DSN"; DSN_SRC="--database-url" ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir needs a path}"; shift 2 ;;
    --backup-verified-elsewhere)
      BACKUP_VERIFIED_REASON="${2:?--backup-verified-elsewhere needs a reason}"; shift 2 ;;
    # This flag used to be accepted and then silently ignored. Fail loudly rather
    # than let anyone believe they turned the backup check off.
    --skip-backup-check)
      die_usage "--skip-backup-check no longer exists (it never did anything).
  To proceed without letting this script take the dump, state what you are relying on:
    --backup-verified-elsewhere \"nightly base backup + WAL, restore tested <date>\"" ;;
    # Print the header block by matching where it ENDS, not a hardcoded line number:
    # the previous version said `2,45p` and had drifted into printing shell source,
    # because editing the header silently invalidates a line count.
    -h|--help) sed -n '2,/^set -euo pipefail/p' "$0" \
                 | grep '^#' | sed 's/^# \{0,1\}//'; exit "$EX_OK" ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

# By name only: `-e DATABASE_URL=<dsn>` would put the password on docker's argv, where
# `ps` and /proc/<pid>/cmdline show it to every user on the host.
if [ -n "$DSN" ]; then
  export DATABASE_URL="$DSN"
  DSN_ENV=(-e DATABASE_URL)
else
  unset DATABASE_URL   # a host-shell value must not reach docker compose at all
fi

# The host backup directory is bind-mounted into every one-off container, so the dump
# this script takes is the file preflight's backup check reads, and the snapshot
# preflight writes is the file postflight reads.
mkdir -p "$BACKUP_DIR"
BACKUP_DIR_ABS="$(cd "$BACKUP_DIR" && pwd)"

ERR_FILE="$(mktemp)"
POST_OUT="$(mktemp)"   # postflight report, parsed to tell a row-count FAIL from a schema one
trap 'rm -f "$ERR_FILE" "$POST_OUT"' EXIT

# Run a command in a one-off container off the service's BUILT image.
# `-e` options must precede the service name: an environment prefix on the host
# (`VAR=... run_img`) would reach only the docker CLI. The `${a[@]+"${a[@]}"}` form
# keeps empty arrays safe under `set -u` on any bash.
run_img() {
  "${DC[@]}" run --rm --no-deps -T -v "$BACKUP_DIR_ABS:$BACKUP_MOUNT" \
    ${DSN_ENV[@]+"${DSN_ENV[@]}"} ${RUN_ENV[@]+"${RUN_ENV[@]}"} "$SVC" "$@"
}

# Every in-image read is `if ! out="$(run_img ... 2>"$ERR_FILE")"; then ... exit; fi`.
# Under `set -euo pipefail` a bare `VAR="$(failing command)"` aborts the script with
# the command's own status and no message — and argparse's usage error is status 2,
# which this script's callers read as "warnings only".
show_err() { sed 's/^/    | /' "$ERR_FILE" >&2; }

# Print alembic_version from the target database: the revision, NONE for an empty
# table, and a non-zero exit when the query fails (e.g. the table does not exist).
read_stamp() {
  run_img python -c "
import asyncio, os, sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
async def m():
    e = create_async_engine(os.environ['DATABASE_URL'])
    async with e.connect() as c:
        r = await c.execute(sa.text('select version_num from alembic_version'))
        print((r.scalar() or 'NONE'))
    await e.dispose()
asyncio.run(m())" 2>"$ERR_FILE" | tr -d '\r' | tail -n 1
}

REBUILD="${DC[*]} build $SVC worker"
MODE="REHEARSAL (nothing will be written)"
[ "$APPLY" -eq 1 ] && MODE="APPLY (this will back up and migrate)"

# --------------------------------------------------------------------------
# Step 1. The image is the one you just built.
#
# src/ and alembic/ are baked into the image, so the image — not the host tree —
# decides which revisions exist and what preflight checks. Its .build_info.json
# (scripts/write_build_info.py, run at build time) names the commit it was built
# from; a different commit from the host HEAD means the build step was skipped.
# The dirty count only WARNs: docker-compose.prod.yml is a deliberate uncommitted
# host edit, so a correct image reports one dirty file.
# --------------------------------------------------------------------------
echo
echo "--- Step 1: the image is the one you just built ---"
if ! BI="$(run_img python -c 'import json; d = json.load(open("/app/.build_info.json")); print(d["commit"], d["dirty_files"])' \
           2>"$ERR_FILE" | tr -d '\r' | tail -n 1)" || [ -z "$BI" ]; then
  echo "BLOCKED: could not read /app/.build_info.json from the $SVC image." >&2
  show_err
  echo "  The image predates build-info stamping, or was never built. Rebuild:" >&2
  echo "    $REBUILD" >&2
  exit "$EX_BLOCKED"
fi
IMG_COMMIT="${BI%% *}"
IMG_DIRTY="${BI##* }"
if ! HOST_COMMIT="$(git rev-parse HEAD 2>"$ERR_FILE")"; then
  echo "BLOCKED: git rev-parse HEAD failed in $REPO_ROOT." >&2
  show_err
  exit "$EX_BLOCKED"
fi
HOST_DIRTY="$(git status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' ')" \
  || HOST_DIRTY="unknown"
if [ "$IMG_COMMIT" != "$HOST_COMMIT" ]; then
  echo "BLOCKED: the $SVC image's /app/.build_info.json says commit $IMG_COMMIT," >&2
  echo "  but the host HEAD is $HOST_COMMIT. The image is not the one this tree builds." >&2
  echo "  Rebuild, then re-run:" >&2
  echo "    $REBUILD" >&2
  exit "$EX_BLOCKED"
fi
echo "    PASS  image commit $IMG_COMMIT = host HEAD"
if [ "$IMG_DIRTY" != "$HOST_DIRTY" ]; then
  echo "    WARN  image was built with $IMG_DIRTY dirty file(s); the host tree has $HOST_DIRTY now."
  echo "          Uncommitted edits since the build are not in the image. Rebuild if they matter:"
  echo "            $REBUILD"
  SCRIPT_WARN=1
else
  echo "    PASS  dirty file count $IMG_DIRTY matches the host tree"
fi

if [ -z "$TARGET" ]; then
  if ! TARGET="$(run_img python scripts/migrate/preflight.py --print-default-target \
                   2>"$ERR_FILE" | tr -d '\r' | tail -n 1)" \
     || ! printf '%s' "$TARGET" | grep -Eqx '[0-9]{4}'; then
    echo "BLOCKED: could not read the default target from the image (got '${TARGET:-<nothing>}')." >&2
    show_err
    echo "  image predates --print-default-target; rebuild:" >&2
    echo "    $REBUILD" >&2
    exit "$EX_BLOCKED"
  fi
  echo "    PASS  default target $TARGET (the image's preflight.DEFAULT_TARGET)"
fi
if ! run_img sh -c 'ls /app/alembic/versions/"$1"_*.py' _ "$TARGET" >/dev/null 2>"$ERR_FILE"; then
  echo "BLOCKED: the $SVC image has no alembic/versions/${TARGET}_*.py." >&2
  show_err
  echo "  alembic cannot upgrade to a revision the image does not carry. Rebuild:" >&2
  echo "    $REBUILD" >&2
  exit "$EX_BLOCKED"
fi
echo "    PASS  the image carries revision $TARGET"

echo
echo "=============================================================="
echo " coPI production migration -> $TARGET"
echo " mode: $MODE"
echo "=============================================================="

# --------------------------------------------------------------------------
# Step 2. Resolve the target database, and say it out loud.
#
# alembic.ini defaults sqlalchemy.url to a localhost DSN, and on this host the bare
# hostname `postgres` resolves from the host to a PUBLIC IP through a LAN search
# domain, so a DSN is only meaningful inside the compose network. The service's own
# DATABASE_URL is therefore parsed INSIDE the container, and only the database name,
# the password-redacted URL and the host come back to this shell.
# --------------------------------------------------------------------------
echo
echo "--- Step 2: target database ---"
if ! DSN_INFO="$(run_img python -c 'import os; from sqlalchemy.engine import make_url as m; u = m(os.environ["DATABASE_URL"]); print(u.database or ""); print(u.render_as_string(hide_password=True)); print(u.host or "")' \
                 2>"$ERR_FILE" | tr -d '\r' | tail -n 3)"; then
  echo "BLOCKED: could not read DATABASE_URL inside the $SVC container." >&2
  show_err
  exit "$EX_BLOCKED"
fi
DBNAME="$(printf '%s\n' "$DSN_INFO" | sed -n 1p)"
DSN_SHOWN="$(printf '%s\n' "$DSN_INFO" | sed -n 2p)"
DSN_HOST="$(printf '%s\n' "$DSN_INFO" | sed -n 3p)"
if [ -z "$DBNAME" ]; then
  echo "BLOCKED: the DSN names no database ('${DSN_SHOWN:-<nothing>}')." >&2
  echo "  Refusing to let a default choose the target. Pass --database-url." >&2
  exit "$EX_USAGE"
fi
if [ -n "$DSN" ]; then
  echo "    target: $DSN_SHOWN   (from $DSN_SRC)"
else
  echo "    target: $DSN_SHOWN   (the $SVC service's own DATABASE_URL)"
fi
# Step 3's pg_dump always dumps $DBNAME on the $PG_SVC service, whatever host the DSN
# names. Against any other server that dump is of the WRONG database, and preflight's
# backup check would pass on it.
if [ -n "$DSN" ] && [ "$DSN_HOST" != "$PG_SVC" ] && [ -z "$BACKUP_VERIFIED_REASON" ]; then
  if [ "$APPLY" -eq 1 ]; then
    echo "BLOCKED: the DSN's host is '${DSN_HOST:-<none>}', not the $PG_SVC service." >&2
    echo "  This script can only dump $PG_SVC, so its backup would be of a different server." >&2
    echo "  Back that server up yourself, then re-run with" >&2
    echo "    --backup-verified-elsewhere \"<what you took, and when you tested its restore>\"" >&2
    exit "$EX_USAGE"
  fi
  echo "    WARN  the DSN's host is '${DSN_HOST:-<none>}', not $PG_SVC: --apply will refuse"
  echo "          without --backup-verified-elsewhere, because the dump would be of $PG_SVC."
  SCRIPT_WARN=1
fi

# --------------------------------------------------------------------------
# Step 3. Backup. Ordered BEFORE preflight so a blocked preflight still leaves you
# with a dump — and because migration 0019 is a one-way door: its downgrade drops
# agent_messages.content, i.e. every message body, silently and with exit 0.
# --------------------------------------------------------------------------
echo
echo "--- Step 3: backup ---"
BACKUP_FILE=""
if [ -n "$BACKUP_VERIFIED_REASON" ]; then
  echo "    WARN  backup asserted elsewhere: $BACKUP_VERIFIED_REASON"
  EXTRA_PREFLIGHT+=(--backup-verified-elsewhere "$BACKUP_VERIFIED_REASON")
elif [ "$APPLY" -eq 0 ]; then
  echo "    (rehearsal) would write a custom-format dump of $DBNAME into $BACKUP_DIR_ABS/"
  EXTRA_PREFLIGHT+=(--backup-verified-elsewhere "rehearsal mode — no dump taken")
else
  BACKUP_FILE="$BACKUP_DIR_ABS/${DBNAME}_pre${TARGET}_$(date +%Y%m%dT%H%M%S).dump"
  echo "    dumping $DBNAME -> $BACKUP_FILE"
  # Dump to a file INSIDE the container, verify it there, then copy it out.
  #
  # Not `pg_dump … > host_file` piped back through `pg_restore -l /dev/stdin`:
  # a custom-format archive needs random access to read its table of contents, and
  # a pipe is not seekable, so that verification fails on a perfectly good dump.
  # Caught by rehearsing this script end to end — it would have blocked every real
  # migration at the backup step.
  CTMP="/tmp/copi_migrate_$$.dump"
  if ! "${DC[@]}" exec -T "$PG_SVC" pg_dump -U copi -Fc -f "$CTMP" "$DBNAME"; then
    echo "BLOCKED: pg_dump failed. Not migrating without a backup." >&2
    "${DC[@]}" exec -T "$PG_SVC" rm -f "$CTMP" >/dev/null 2>&1 || true
    exit "$EX_OPERATIONAL"
  fi
  # A dump whose table of contents cannot be read cannot be restored. This is the
  # difference between having a backup and having a file.
  if ! "${DC[@]}" exec -T "$PG_SVC" pg_restore -l "$CTMP" >/dev/null 2>&1; then
    echo "BLOCKED: pg_restore -l cannot read the dump — it is not restorable." >&2
    "${DC[@]}" exec -T "$PG_SVC" rm -f "$CTMP" >/dev/null 2>&1 || true
    exit "$EX_OPERATIONAL"
  fi
  TOC_N=$("${DC[@]}" exec -T "$PG_SVC" pg_restore -l "$CTMP" 2>/dev/null | grep -c '^[0-9]' || true)
  # Wrapped: a bare failing `cp` under `set -e` would exit 1 (BLOCKED) with no message.
  # The in-container dump is deliberately left in place — it is the only good copy.
  if ! "${DC[@]}" cp "$PG_SVC:$CTMP" "$BACKUP_FILE" >/dev/null; then
    echo "BLOCKED: copying the dump out of the $PG_SVC container failed. Nothing was migrated." >&2
    echo "  The verified dump is still inside the container at $PG_SVC:$CTMP." >&2
    echo "  Copy it out by hand, or remove it:" >&2
    echo "    ${DC[*]} cp $PG_SVC:$CTMP $BACKUP_FILE" >&2
    echo "    ${DC[*]} exec -T $PG_SVC rm -f $CTMP" >&2
    exit "$EX_OPERATIONAL"
  fi
  "${DC[@]}" exec -T "$PG_SVC" rm -f "$CTMP" >/dev/null 2>&1 || true
  SZ=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || echo 0)
  if [ "$SZ" -lt 1024 ]; then
    echo "BLOCKED: dump is only ${SZ} bytes — that is not a backup." >&2
    exit "$EX_OPERATIONAL"
  fi
  echo "    PASS  ${SZ} bytes on the host, ${TOC_N} restorable objects in the TOC"
  # Preflight runs in the container, so it gets the dump's path under the mount.
  EXTRA_PREFLIGHT+=(--backup-path "$BACKUP_MOUNT/$(basename "$BACKUP_FILE")")
fi

# --------------------------------------------------------------------------
# Step 4. Preflight. Exit 1 here means STOP.
# --------------------------------------------------------------------------
echo
echo "--- Step 4: preflight ---"
set +e
run_img python scripts/migrate/preflight.py --target "$TARGET" --snapshot "$SNAP_CTR" \
  ${EXTRA_PREFLIGHT[@]+"${EXTRA_PREFLIGHT[@]}"}
PF=$?
set -e
case "$PF" in
  0) echo "    PASS  preflight clear" ;;
  2) echo "    WARN  preflight raised warnings — read them above before continuing" ;;
  *) echo "BLOCKED: preflight exited $PF. Nothing was written." >&2
     echo "  If it reported duplicate (simulation_run_id, message_ts) rows:" >&2
     echo "    ${DC[*]} run --rm --no-deps -T -e PYTHONPATH=/app $SVC \\" >&2
     echo "      python scripts/migrate/remediate_duplicates.py            # dry run first" >&2
     exit "$EX_BLOCKED" ;;
esac

if [ "$APPLY" -eq 0 ]; then
  echo
  echo "=============================================================="
  echo " REHEARSAL COMPLETE — nothing was written."
  echo " Re-run with --apply when your window is open."
  # A rehearsal that raised warnings must not be indistinguishable, to a caller
  # reading only the exit code, from one that was clear. Warnings here are things
  # like "these rows will end up with content = ''" — real, unfixable, and worth an
  # operator reading before the window rather than discovering after it.
  if [ "$PF" -eq 2 ] || [ "$SCRIPT_WARN" -eq 1 ]; then
    echo
    echo " EXIT 2: warnings were raised. Scroll up and read them."
    echo "=============================================================="
    exit "$EX_WARN"
  fi
  echo "=============================================================="
  exit "$EX_OK"
fi

# The stamp before the upgrade, so Step 6 can tell "applied nothing" from "rolled back".
if ! PRE_STAMP="$(read_stamp)" || [ -z "$PRE_STAMP" ]; then
  echo "BLOCKED: could not read alembic_version before the upgrade. Nothing was written." >&2
  show_err
  exit "$EX_BLOCKED"
fi

# --------------------------------------------------------------------------
# Step 5. Migrate. One alembic command, so the whole chain is ONE transaction:
# a killed migration cannot leave a half-applied schema.
# --------------------------------------------------------------------------
echo
echo "--- Step 5: alembic upgrade $PRE_STAMP -> $TARGET (lock_timeout ${LOCK_TIMEOUT_MS}ms) ---"
RUN_ENV=(-e "ALEMBIC_LOCK_TIMEOUT_MS=$LOCK_TIMEOUT_MS")
set +e
run_img python -m alembic upgrade "$TARGET"
MIG=$?
set -e
RUN_ENV=()
if [ "$MIG" -ne 0 ]; then
  echo "BLOCKED: alembic exited $MIG." >&2
  echo "  The chain runs in one transaction, so the database is unchanged — verify:" >&2
  echo "    ${DC[*]} exec -T $PG_SVC psql -U copi -d $DBNAME -c 'select * from alembic_version'" >&2
  echo "  A LockNotAvailableError means something held a lock on agent_messages." >&2
  # -t 420, not -t 30: shutdown is cooperative (request_stop() only flips a flag; the
  # durable flush in main.py's finally-block needs the main loop to RETURN), and a
  # 16000-token thread_reply final call can run ~4-5 minutes uninterrupted. `docker
  # stop` returns as soon as the container exits, so a generous -t is free insurance.
  echo "  Stop the writers: Stop on /admin/simulation, then" >&2
  echo "    ${DC[*]} --profile agent stop -t 420 agent" >&2
  echo "    docker stop -t 420 blackbird-agent-run   # ONLY if an emergency CLI run is live" >&2
  echo "  Re-run." >&2
  exit "$EX_BLOCKED"
fi

# --------------------------------------------------------------------------
# Step 6. Confirm the commit by READING THE DATABASE.
#
# This step exists because of a real mistake made while building this tooling: a
# bad env.py change made every migration log "Running upgrade" and then silently
# roll the whole chain back, leaving no alembic_version table at all — and the
# log lines were mistaken for success. Alembic's own output is not evidence.
# An unchanged stamp is a different failure: `alembic upgrade X` to a revision at or
# behind the stamp is a no-op that exits 0.
# --------------------------------------------------------------------------
echo
echo "--- Step 6: confirm the commit landed ---"
STAMP="$(read_stamp)" || STAMP=""
if [ "$STAMP" = "$TARGET" ]; then
  echo "    PASS  alembic_version = $STAMP (read back from the database)"
elif [ -z "$STAMP" ] || [ "$STAMP" = "NONE" ]; then
  echo "BLOCKED: alembic reported success but alembic_version is '${STAMP:-MISSING}', not $TARGET." >&2
  show_err
  echo "  Treat this as a silent rollback. Do NOT deploy code. Investigate env.py." >&2
  exit "$EX_BLOCKED"
elif [ "$STAMP" = "$PRE_STAMP" ]; then
  echo "BLOCKED: alembic exited 0 but applied nothing: alembic_version is still $STAMP, not $TARGET." >&2
  echo "  The database is unchanged. Check that $TARGET is ahead of $PRE_STAMP on the image's chain." >&2
  echo "  Do NOT deploy code." >&2
  exit "$EX_BLOCKED"
else
  echo "BLOCKED: alembic_version moved $PRE_STAMP -> $STAMP, not $TARGET." >&2
  echo "  Do NOT deploy code. Investigate the chain before going further." >&2
  exit "$EX_BLOCKED"
fi

# --------------------------------------------------------------------------
# Step 7. Postflight: the schema, not the stamp.
#
# Migrate-before-serve keeps the old web tier and worker serving through the
# migration, so their inserts grow tables between the snapshot and now. With a live
# writer, growth is tolerated; loss and a missing table still FAIL. A service stuck in
# `restarting` counts as live (it writes between crashes), and so does an emergency
# CLI one-off, which is not a compose service and is only visible to `docker ps` under
# its exact name. Never match the unprefixed `agent-run`: that is org1's container.
# --------------------------------------------------------------------------
echo
echo "--- Step 7: postflight ---"
POST_ARGS=()
if ! RUNNING="$("${DC[@]}" --profile agent ps --status running --status restarting --services 2>"$ERR_FILE")" \
   || ! ONE_OFF="$(docker ps --filter 'name=^blackbird-agent-run$' --filter status=running --format '{{.Names}}' 2>"$ERR_FILE")"; then
  echo "BLOCKED: could not list running services. The migration IS committed ($STAMP)." >&2
  show_err
  echo "  Run postflight yourself before deploying code:" >&2
  echo "    ${DC[*]} run --rm --no-deps -T -v \"$BACKUP_DIR_ABS:$BACKUP_MOUNT\" $SVC \\" >&2
  echo "      python scripts/migrate/postflight.py --target $TARGET --snapshot $SNAP_CTR" >&2
  exit "$EX_BLOCKED"
fi
WRITERS="$( { printf '%s\n' "$RUNNING" | tr -d '\r' | grep -Ex "$SVC|worker|agent" | sort -u
              printf '%s\n' "$ONE_OFF" | tr -d '\r' | grep -x 'blackbird-agent-run'; } \
            | paste -sd' ' - || true)"
if [ -n "$WRITERS" ]; then
  POST_ARGS+=(--allow-row-growth)
  echo "    WARN  row growth tolerated: live writers: $WRITERS"
fi
# tee, so the operator still sees the report; with pipefail the status is postflight's.
set +e
run_img python scripts/migrate/postflight.py --target "$TARGET" --snapshot "$SNAP_CTR" \
  ${POST_ARGS[@]+"${POST_ARGS[@]}"} | tee "$POST_OUT"
POST=$?
set -e
# Failing checks, from Report.render_text() (scripts/migrate/preflight.py, shared by
# postflight): one `NN. [STATUS] title` line per check, and FAIL is the BLOCK token.
ROW_COUNT_TITLE="Row counts match the preflight snapshot"
FAILED_CHECKS="$(tr -d '\r' < "$POST_OUT" \
                 | sed -n 's/^ *[0-9][0-9]*\. \[BLOCK *\] //p' || true)"
if [ "$POST" -eq 1 ] && [ "$FAILED_CHECKS" = "$ROW_COUNT_TITLE" ]; then
  # A restore here would throw away every write made since the dump — on a schema
  # that postflight has just verified in every other respect.
  echo "BLOCKED: postflight FAILED on row counts only; every schema check passed." >&2
  echo "  Row counts differ from the preflight snapshot. Investigate before deploying code:" >&2
  echo "  read the \"$ROW_COUNT_TITLE\" item above for the tables that moved." >&2
  echo "  Do NOT restore the dump on this alone: a restore loses every write made since it" >&2
  echo "  was taken. Loss usually means a writer deleted rows during the window; growth" >&2
  echo "  means a writer this script did not see was live. The migration IS committed ($STAMP)." >&2
  echo "  Once explained, re-run postflight:" >&2
  echo "    ${DC[*]} run --rm --no-deps -T -v \"$BACKUP_DIR_ABS:$BACKUP_MOUNT\" $SVC \\" >&2
  echo "      python scripts/migrate/postflight.py --target $TARGET --snapshot $SNAP_CTR ${POST_ARGS[*]-}" >&2
  exit "$EX_BLOCKED"
elif [ "$POST" -eq 1 ]; then
  echo "BLOCKED: postflight FAILED — the schema does not match $TARGET." >&2
  if [ -n "$FAILED_CHECKS" ]; then
    printf '%s\n' "$FAILED_CHECKS" | sed 's/^/    FAILED: /' >&2
  else
    echo "  (the failing checks could not be read from its output; see the report above)" >&2
  fi
  echo "  Do NOT deploy application code. The chain committed as one transaction, so this" >&2
  echo "  is a schema to investigate, not a partial state to repair. To go back to $PRE_STAMP," >&2
  echo "  restore a backup. Stop the run from /admin/simulation (Stop drains and flushes" >&2
  echo "  in-process), then:" >&2
  echo "    ${DC[*]} --profile agent stop -t 420 agent" >&2
  echo "    ${DC[*]} stop $SVC worker" >&2
  if [ -n "$BACKUP_FILE" ]; then
    echo "    ${DC[*]} cp $BACKUP_FILE $PG_SVC:/tmp/restore.dump" >&2
    echo "    ${DC[*]} exec -T $PG_SVC psql -U copi -d postgres -c 'ALTER DATABASE $DBNAME RENAME TO ${DBNAME}_failed_migration'" >&2
    echo "    ${DC[*]} exec -T $PG_SVC psql -U copi -d postgres -c 'CREATE DATABASE $DBNAME'" >&2
    echo "    ${DC[*]} exec -T $PG_SVC pg_restore -U copi -d $DBNAME --exit-on-error /tmp/restore.dump" >&2
    echo "    ${DC[*]} exec -T $PG_SVC psql -U copi -d $DBNAME -c 'select * from alembic_version'" >&2
    echo "  Rename rather than drop: keep the failed database until the restore is confirmed." >&2
    echo "  --exit-on-error is not optional: without it pg_restore reports success after a" >&2
    echo "  partial restore. The revision should read $PRE_STAMP again." >&2
  else
    # No dump means no recipe: pasting a pg_restore of a file that was never written
    # would leave the operator on an empty, freshly created database.
    echo "  NO DUMP WAS TAKEN BY THIS RUN, so there is nothing here to restore from." >&2
    echo "  Restore from the backup you asserted with --backup-verified-elsewhere:" >&2
    echo "    $BACKUP_VERIFIED_REASON" >&2
    echo "  using that backup's own restore procedure. Keep the failed database until the" >&2
    echo "  restore is confirmed; the revision should read $PRE_STAMP again." >&2
  fi
  exit "$EX_BLOCKED"
elif [ "$POST" -ne 0 ]; then
  echo "BLOCKED: postflight did not complete (exit $POST). The migration IS committed ($STAMP)." >&2
  echo "  Do NOT deploy application code until postflight passes. Re-run it:" >&2
  echo "    ${DC[*]} run --rm --no-deps -T -v \"$BACKUP_DIR_ABS:$BACKUP_MOUNT\" $SVC \\" >&2
  echo "      python scripts/migrate/postflight.py --target $TARGET --snapshot $SNAP_CTR ${POST_ARGS[*]-}" >&2
  exit "$EX_BLOCKED"
fi
echo "    PASS  postflight verified"

# The committed docker-compose.prod.yml runs `src.agent.main` for the agent service,
# which RESUMES the latest simulation run on start; only the host's uncommitted edit
# runs `src.agent.supervisor`, which comes back IDLE. So `up -d agent` is printed only
# when the resolved compose config says the supervisor is what it would start.
AGENT_IS_SUPERVISOR=0
if AGENT_CFG="$("${DC[@]}" --profile agent config 2>/dev/null)" \
   && printf '%s\n' "$AGENT_CFG" | tr -d '\r' | awk '
        /^[^ ]/          { in_agent = 0; in_cmd = 0; next }
        /^  [^ ]/        { in_agent = ($0 ~ /^  agent:[[:space:]]*$/); in_cmd = 0; next }
        !in_agent        { next }
        /^    [^ -]/     { in_cmd = ($0 ~ /^    command:/) }
        in_cmd           { print }' | grep -q 'src\.agent\.supervisor'; then
  AGENT_IS_SUPERVISOR=1
fi

echo
echo "=============================================================="
echo " MIGRATION COMPLETE AND VERIFIED $PRE_STAMP -> $TARGET"
[ -n "$BACKUP_FILE" ] && echo " backup: $BACKUP_FILE"
echo
echo " STILL TO DO, in this order (docs/production-migration.md steps 8-10):"
echo "   8. Repair the Slack mirror mapping on legacy rows (only needed if this chain"
echo "      created agent_messages.content; a later start can skip it):"
echo "        ${DC[*]} run --rm --no-deps -T $SVC python scripts/backfill_slack_ts.py          # report first"
echo "        ${DC[*]} run --rm --no-deps -T $SVC python scripts/backfill_slack_ts.py --apply"
echo "      Read its output. Exit 2 means rows were UNVERIFIED, not absent."
echo "   9. Serve the new code on the migrated schema:"
echo "        ${DC[*]} up -d $SVC worker"
if [ "$AGENT_IS_SUPERVISOR" -eq 1 ]; then
  echo "  10. Bring the agent supervisor back, only when /admin/simulation shows no live run:"
  echo "        ${DC[*]} --profile agent up -d agent   # supervisor returns IDLE; start a run from /admin/simulation only when intended (NOT agent-run, which is org1's)"
else
  echo "  10. !!! DO NOT RUN \`${DC[*]} --profile agent up -d agent\` !!!"
  echo "      $COMPOSE_FILE's agent service does not run src.agent.supervisor (or its"
  echo "      config could not be read), so that command would START A SIMULATION RUN,"
  echo "      resuming the latest one. Restore the host's agent-service edit (command"
  echo "      src.agent.supervisor) first, and confirm with:"
  echo "        ${DC[*]} --profile agent config | grep src.agent.supervisor"
fi
echo "=============================================================="
