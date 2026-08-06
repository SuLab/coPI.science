#!/usr/bin/env bash
#
# Guided production migration to alembic head 0023 (branch cohort-db-conversations).
# Supported starting points: 0018 (main before PR19), 0019, 0020 and 0021.
# 0021 is origin/main's own alembic head, so that is where a deployment tracking main is.
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
# EXIT CODES
#   0  rehearsal clear / migration applied and verified
#   1  BLOCKED — a check failed. Nothing was written. Fix and re-run.
#   2  rehearsal only: preflight raised warnings you should read. Nothing written.
#      (In --apply mode warnings do not stop the run — you already chose to proceed —
#      so a successful apply is still 0.)
#   3  operational failure (no DSN, unreachable DB, backup failed, lock timeout)
#  64  usage error
#
# WHAT THIS DOES NOT DO, on purpose:
#   * It does not stop or start the agent/worker/web containers. Deciding when your
#     traffic can pause is not a script's call, and a half-stopped deployment is
#     worse than a refused one. It checks that nothing is holding a lock and tells
#     you what to stop.
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
TARGET="0025"
DSN="${DATABASE_URL:-}"
BACKUP_DIR="${MIGRATE_BACKUP_DIR:-backups}"
SVC="${MIGRATE_SERVICE:-app}"
PG_SVC="${MIGRATE_PG_SERVICE:-postgres}"
LOCK_TIMEOUT_MS="${ALEMBIC_LOCK_TIMEOUT_MS:-10000}"
BACKUP_VERIFIED_REASON=""
EXTRA_PREFLIGHT=()

die_usage() { echo "ERROR: $*" >&2; echo "See docs/production-migration.md" >&2; exit "$EX_USAGE"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --target) TARGET="${2:?--target needs a revision}"; shift 2 ;;
    --database-url) DSN="${2:?--database-url needs a DSN}"; shift 2 ;;
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

MODE="REHEARSAL (nothing will be written)"
[ "$APPLY" -eq 1 ] && MODE="APPLY (this will back up and migrate)"

echo "=============================================================="
echo " coPI production migration -> $TARGET"
echo " mode: $MODE"
echo "=============================================================="

# --------------------------------------------------------------------------
# Step 1. The image must contain current code.
#
# Dockerfile does `pip install .`, which BAKES a copy of src/ into
# site-packages. For `python scripts/X.py`, CPython sets sys.path[0] to the
# script's own directory, so /app is NOT on the path and `import src` resolves to
# the baked copy — which in a stale container is days old. Every python step below
# therefore passes PYTHONPATH=/app, and this check proves it worked.
# --------------------------------------------------------------------------
echo
echo "--- Step 1: the container is running current code ---"
if ! docker compose ps --status running --services 2>/dev/null | grep -qx "$SVC"; then
  echo "BLOCKED: compose service '$SVC' is not running." >&2
  echo "  docker compose up -d --build $SVC" >&2
  exit "$EX_OPERATIONAL"
fi
SRC_PATH="$(docker compose exec -T -e PYTHONPATH=/app "$SVC" \
  python -c 'import src; print(src.__file__)' 2>/dev/null | tr -d '\r')"
case "$SRC_PATH" in
  /app/src/__init__.py) echo "    PASS  import src -> $SRC_PATH" ;;
  *) echo "BLOCKED: with PYTHONPATH=/app, 'import src' resolved to '${SRC_PATH:-<nothing>}'," >&2
     echo "  not /app/src/__init__.py. The container would run stale code." >&2
     echo "  docker compose up -d --build $SVC" >&2
     exit "$EX_BLOCKED" ;;
esac
if ! docker compose exec -T -e PYTHONPATH=/app "$SVC" \
     python -c 'from src.models import Cohort' >/dev/null 2>&1; then
  echo "BLOCKED: /app/src has no Cohort model — the mounted source predates 0022." >&2
  exit "$EX_BLOCKED"
fi
echo "    PASS  /app/src carries the cohort models"

# --------------------------------------------------------------------------
# Step 2. Resolve the DSN, and say it out loud.
#
# alembic.ini defaults sqlalchemy.url to
# postgresql+asyncpg://copi:copi@localhost:5432/copi and env.py only overrides it
# when DATABASE_URL is set. A migration run with no DSN therefore targets whatever
# answers on localhost:5432. Measured on this machine: compose does not publish
# Postgres to the host, so a host-side run fails closed — but the bare hostname
# `postgres` resolves from the host to a PUBLIC IP (195.35.25.84) through a LAN
# search domain, so a DSN meant for in-container use points somewhere else entirely
# when it escapes. Never let it default, and always resolve it inside the container.
# --------------------------------------------------------------------------
echo
echo "--- Step 2: target database ---"
if [ -z "$DSN" ]; then
  echo "BLOCKED: no DSN. Pass --database-url or export DATABASE_URL." >&2
  echo "  Refusing to let alembic.ini's localhost default choose the target." >&2
  exit "$EX_USAGE"
fi
echo "    target: $(printf '%s' "$DSN" | sed -E 's#(//[^:]+):[^@]*@#\1:***@#')"

run_py() {  # run a repo python script inside the container with current code
  docker compose exec -T -e PYTHONPATH=/app -e DATABASE_URL="$DSN" "$SVC" python "$@"
}

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
  echo "    (rehearsal) would write a custom-format dump into $BACKUP_DIR/"
  EXTRA_PREFLIGHT+=(--backup-verified-elsewhere "rehearsal mode — no dump taken")
else
  mkdir -p "$BACKUP_DIR"
  DBNAME="$(printf '%s' "$DSN" | sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')"
  BACKUP_FILE="$BACKUP_DIR/${DBNAME}_pre${TARGET}_$(date +%Y%m%dT%H%M%S).dump"
  echo "    dumping $DBNAME -> $BACKUP_FILE"
  # Dump to a file INSIDE the container, verify it there, then copy it out.
  #
  # Not `pg_dump … > host_file` piped back through `pg_restore -l /dev/stdin`:
  # a custom-format archive needs random access to read its table of contents, and
  # a pipe is not seekable, so that verification fails on a perfectly good dump.
  # Caught by rehearsing this script end to end — it would have blocked every real
  # migration at the backup step.
  CTMP="/tmp/copi_migrate_$$.dump"
  if ! docker compose exec -T "$PG_SVC" pg_dump -U copi -Fc -f "$CTMP" "$DBNAME"; then
    echo "BLOCKED: pg_dump failed. Not migrating without a backup." >&2
    docker compose exec -T "$PG_SVC" rm -f "$CTMP" >/dev/null 2>&1 || true
    exit "$EX_OPERATIONAL"
  fi
  # A dump whose table of contents cannot be read cannot be restored. This is the
  # difference between having a backup and having a file.
  if ! docker compose exec -T "$PG_SVC" pg_restore -l "$CTMP" >/dev/null 2>&1; then
    echo "BLOCKED: pg_restore -l cannot read the dump — it is not restorable." >&2
    docker compose exec -T "$PG_SVC" rm -f "$CTMP" >/dev/null 2>&1 || true
    exit "$EX_OPERATIONAL"
  fi
  TOC_N=$(docker compose exec -T "$PG_SVC" pg_restore -l "$CTMP" 2>/dev/null | grep -c '^[0-9]' || true)
  docker compose cp "$PG_SVC:$CTMP" "$BACKUP_FILE" >/dev/null
  docker compose exec -T "$PG_SVC" rm -f "$CTMP" >/dev/null 2>&1 || true
  SZ=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || echo 0)
  if [ "$SZ" -lt 1024 ]; then
    echo "BLOCKED: dump is only ${SZ} bytes — that is not a backup." >&2
    exit "$EX_OPERATIONAL"
  fi
  echo "    PASS  ${SZ} bytes on the host, ${TOC_N} restorable objects in the TOC"
  EXTRA_PREFLIGHT+=(--backup-path "$BACKUP_FILE")
fi

# --------------------------------------------------------------------------
# Step 4. Preflight. Exit 1 here means STOP.
# --------------------------------------------------------------------------
echo
echo "--- Step 4: preflight ---"
SNAP="${MIGRATE_SNAPSHOT:-$BACKUP_DIR/preflight_snapshot.json}"
mkdir -p "$(dirname "$SNAP")"
set +e
run_py scripts/migrate/preflight.py --target "$TARGET" --snapshot "$SNAP" \
  "${EXTRA_PREFLIGHT[@]}"
PF=$?
set -e
case "$PF" in
  0) echo "    PASS  preflight clear" ;;
  2) echo "    WARN  preflight raised warnings — read them above before continuing" ;;
  *) echo "BLOCKED: preflight exited $PF. Nothing was written." >&2
     echo "  If it reported duplicate (simulation_run_id, message_ts) rows:" >&2
     echo "    docker compose exec -T -e PYTHONPATH=/app -e DATABASE_URL=\"\$DSN\" $SVC \\" >&2
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
  if [ "$PF" -eq 2 ]; then
    echo
    echo " EXIT 2: preflight raised warnings. Scroll up and read them."
    echo "=============================================================="
    exit "$EX_WARN"
  fi
  echo "=============================================================="
  exit "$EX_OK"
fi

# --------------------------------------------------------------------------
# Step 5. Migrate. One alembic command, so the whole chain is ONE transaction:
# a killed migration cannot leave a half-applied schema.
# --------------------------------------------------------------------------
echo
echo "--- Step 5: alembic upgrade $TARGET (lock_timeout ${LOCK_TIMEOUT_MS}ms) ---"
set +e
docker compose exec -T -e PYTHONPATH=/app -e DATABASE_URL="$DSN" \
  -e ALEMBIC_LOCK_TIMEOUT_MS="$LOCK_TIMEOUT_MS" "$SVC" \
  python -m alembic upgrade "$TARGET"
MIG=$?
set -e
if [ "$MIG" -ne 0 ]; then
  echo "BLOCKED: alembic exited $MIG." >&2
  echo "  The chain runs in one transaction, so the database is unchanged — verify:" >&2
  echo "    docker compose exec -T $PG_SVC psql -U copi -d <db> -c 'select * from alembic_version'" >&2
  echo "  A LockNotAvailableError means something held a lock on agent_messages." >&2
  echo "  Stop the writers (docker stop -t 30 agent-run) and re-run." >&2
  exit "$EX_BLOCKED"
fi

# --------------------------------------------------------------------------
# Step 6. Confirm the commit by READING THE DATABASE.
#
# This step exists because of a real mistake made while building this tooling: a
# bad env.py change made every migration log "Running upgrade" and then silently
# roll the whole chain back, leaving no alembic_version table at all — and the
# log lines were mistaken for success. Alembic's own output is not evidence.
# --------------------------------------------------------------------------
echo
echo "--- Step 6: confirm the commit landed ---"
STAMP="$(run_py -c "
import asyncio, os, sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
async def m():
    e = create_async_engine(os.environ['DATABASE_URL'])
    async with e.connect() as c:
        r = await c.execute(sa.text('select version_num from alembic_version'))
        print((r.scalar() or 'NONE'))
    await e.dispose()
asyncio.run(m())" 2>/dev/null | tr -d '\r')"
if [ "$STAMP" != "$TARGET" ]; then
  echo "BLOCKED: alembic reported success but alembic_version is '${STAMP:-MISSING}', not $TARGET." >&2
  echo "  Treat this as a silent rollback. Do NOT deploy code. Investigate env.py." >&2
  exit "$EX_BLOCKED"
fi
echo "    PASS  alembic_version = $STAMP (read back from the database)"

# --------------------------------------------------------------------------
# Step 7. Postflight: the schema, not the stamp.
# --------------------------------------------------------------------------
echo
echo "--- Step 7: postflight ---"
set +e
run_py scripts/migrate/postflight.py --target "$TARGET" --snapshot "$SNAP"
POST=$?
set -e
if [ "$POST" -ne 0 ]; then
  echo "BLOCKED: postflight exited $POST — the schema does not match $TARGET." >&2
  echo "  Do NOT deploy application code. Restore path:" >&2
  echo "    docs/production-migration.md, section 'If postflight fails'" >&2
  exit "$EX_BLOCKED"
fi
echo "    PASS  postflight verified"

echo
echo "=============================================================="
echo " MIGRATION COMPLETE AND VERIFIED -> $TARGET"
[ -n "$BACKUP_FILE" ] && echo " backup: $BACKUP_FILE"
echo
echo " STILL TO DO, in this order (docs/production-migration.md steps 8-10):"
echo "   8. Repair the Slack mirror mapping on legacy rows:"
echo "        docker compose exec -T -e PYTHONPATH=/app $SVC \\"
echo "          python scripts/backfill_slack_ts.py            # report first"
echo "        docker compose exec -T -e PYTHONPATH=/app $SVC \\"
echo "          python scripts/backfill_slack_ts.py --apply"
echo "      Read its output. Exit 2 means rows were UNVERIFIED, not absent."
echo "   9. Deploy the application code, then restart app + worker."
echo "  10. Start agent-run last."
echo "=============================================================="
