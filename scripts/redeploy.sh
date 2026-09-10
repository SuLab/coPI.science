#!/usr/bin/env bash
#
# Redeploy app/worker against a migrated schema, without ever serving requests
# against a schema `migrate` has not applied yet (audit 2026-09-08 RC-6, #27 I2).
#
# ROOT CAUSE THIS SCRIPT FIXES: `docker compose $C up -d --build app worker` on an
# ALREADY RUNNING stack does not guarantee `migrate` reruns before the new app/worker
# containers start. `depends_on: migrate: condition: service_completed_successfully`
# only orders container *creation* -- on a cold start that is enough, because
# `migrate` has to be created (and therefore run) before app/worker can be created.
# On a running stack, an existing exited `migrate` container from the LAST deploy can
# already satisfy that condition without being re-run against the freshly built
# image, so the old app/worker containers (or worse, new ones started against the old
# schema) can end up serving requests during the window the new migration was
# supposed to cover.
#
# THE FIX IS ORDERING, ENFORCED EXPLICITLY, NOT LEFT TO depends_on:
#   1. build migrate, app, worker (new images)
#   2. stop the OLD app/worker, GRACEFULLY (`-t 30`, matching the agent-restart runbook
#      in CLAUDE.md -- an in-flight request gets a chance to finish rather than being
#      SIGKILLed at 0s); they must not serve while migrate runs
#   3. start migrate and wait for it to exit; abort on nonzero (old containers stay
#      stopped -- refusing is safer than guessing). The exit code is read via `docker
#      wait <container id>`, NOT `docker compose wait migrate` (opus review,
#      2026-09-08): the latter looks up the service by PROJECT state, and measured
#      against a `migrate` that has already exited by the time `wait` runs, it fails
#      with "no containers for project" in >=0.3 s instead of reporting the exit code
#      -- which would abort this script with app/worker left stopped even though
#      migrate actually succeeded. `docker wait` on the container's own id talks to
#      the Engine API directly and has no such race.
#   4. start the NEW app/worker only once migrate is verified to have exited 0, and
#      wait for the new app container to report Docker-healthy (bounded; see
#      APP_HEALTH_TIMEOUT_SECONDS) before declaring success -- `up -d` returning just
#      means the container was created, not that it is serving.
#   5. reload nginx -- the recreated app container gets a new IP, and nginx's static
#      `upstream app { server app:8000; }` caches the old one until reloaded (see the
#      nginx-stale-upstream-ip-after-app-recreate memory note; it self-heals in <=6h
#      via the container's own reload loop, but there is no reason to wait)
#
# USAGE
#   Export COMPOSE_FILE once per shell (see CLAUDE.md "Compose file set"):
#     export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml
#     ./scripts/redeploy.sh
#   ...or pass the files explicitly:
#     ./scripts/redeploy.sh -f docker-compose.prod.yml -f docker-compose.override.yml
#
# This script REFUSES to run unless BOTH prod compose files
# (docker-compose.prod.yml and docker-compose.override.yml) are identifiable, either
# in $COMPOSE_FILE or passed as -f arguments -- running it against the bare dev
# compose file (docker-compose.yml) would recreate prod services with `restart: no`
# and no awslogs override (see CLAUDE.md).
#
# This script never asks `docker compose` to remove "orphan" containers: that
# deletes the prod nginx/certbot containers, which are not part of this compose
# file's service list but ARE part of the same project (see CLAUDE.md).
set -euo pipefail

PROD_FILE="docker-compose.prod.yml"
OVERRIDE_FILE="docker-compose.override.yml"

COMPOSE_ARGS=("$@")

die() {
  echo "ERROR: $*" >&2
  exit 1
}

# Every compose file this invocation would actually use: $COMPOSE_FILE (colon-
# separated, matching docker compose's own env var) plus every argument following
# a `-f`/`--file` flag.
_known_files() {
  local files="${COMPOSE_FILE:-}"
  local i=0
  local n=${#COMPOSE_ARGS[@]}
  while [ "$i" -lt "$n" ]; do
    case "${COMPOSE_ARGS[$i]}" in
      -f|--file)
        i=$((i + 1))
        files="${files}:${COMPOSE_ARGS[$i]:-}"
        ;;
    esac
    i=$((i + 1))
  done
  printf '%s' "$files"
}

KNOWN_FILES="$(_known_files)"
case "$KNOWN_FILES" in
  *"$PROD_FILE"*) HAS_PROD=1 ;;
  *) HAS_PROD=0 ;;
esac
case "$KNOWN_FILES" in
  *"$OVERRIDE_FILE"*) HAS_OVERRIDE=1 ;;
  *) HAS_OVERRIDE=0 ;;
esac

if [ "$HAS_PROD" -ne 1 ] || [ "$HAS_OVERRIDE" -ne 1 ]; then
  die "refusing to run: both $PROD_FILE and $OVERRIDE_FILE must be in \$COMPOSE_FILE
  or passed via -f. A bare \`docker compose\` (or this script without them) reads only
  docker-compose.yml -- the DEV file -- and recreating prod services from it leaves them
  with 'restart: no' and no awslogs override (see CLAUDE.md 'Compose file set').
  Fix:
    export COMPOSE_FILE=${PROD_FILE}:${OVERRIDE_FILE}
    $0
  or:
    $0 -f $PROD_FILE -f $OVERRIDE_FILE"
fi

compose() {
  docker compose "${COMPOSE_ARGS[@]}" "$@"
}

# How long to wait for the new app container to report Docker-healthy before giving up
# (its healthcheck in docker-compose.prod.yml polls every 30s with a 15s start_period,
# so 120s gives it roughly three tries). Overridable for testing.
APP_HEALTH_TIMEOUT_SECONDS="${APP_HEALTH_TIMEOUT_SECONDS:-120}"
APP_HEALTH_POLL_INTERVAL_SECONDS="${APP_HEALTH_POLL_INTERVAL_SECONDS:-3}"

echo "==> [1/6] building migrate, app, worker"
compose build migrate app worker

echo "==> [2/6] stopping app, worker (must not serve while migrate applies the new schema)"
compose stop -t 30 app worker

echo "==> [3/6] running migrate"
compose up -d migrate
# REV3-5 (opus review, audit 2026-09-08): `compose ps -aq migrate` can return SEVERAL
# ids -- a stale one-off `migrate` container from an earlier `docker compose run`
# left alongside the one `up -d migrate` just created. Passing the whole
# newline-joined blob to `docker wait "$MIGRATE_CID"` as a single argument is exactly
# what real `docker wait` rejects (no parseable exit code, non-zero exit), tripping
# the "failed to report an exit code" abort below even after a SUCCESSFUL migration.
#
# REV4-2 (audit 2026-09-08): selecting by list POSITION (`tail -n1`, on the assumption
# compose always lists them oldest-first) is not reliable -- a stale one-off
# `<proj>-migrate-run-<hash>` container can sort AFTER the persistent
# `<proj>-migrate-1` service container this run actually cares about. Select by the
# `com.docker.compose.oneoff` label instead (`False` on the service container `up -d`
# creates, `True` on every `docker compose run`/`exec`-style one-off) -- that is what
# actually distinguishes them, not creation order. Kept dependency-free (no jq, no
# project-name derivation): `compose ps -aq migrate` already scopes the id list to
# this project's `migrate` service, so only the oneoff label needs checking, via
# `docker inspect` (already used below for the app healthcheck).
MIGRATE_CIDS="$(compose ps -aq migrate)"
if [ -z "$MIGRATE_CIDS" ]; then
  die "no migrate container found after \`up -d migrate\` -- cannot verify its exit code.
  app/worker remain stopped. See docs/production-migration.md section 10.6."
fi
MIGRATE_CID=""
while IFS= read -r cid; do
  [ -z "$cid" ] && continue
  ONEOFF="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.oneoff"}}' "$cid" 2>/dev/null || echo "")"
  if [ "$ONEOFF" != "True" ]; then
    MIGRATE_CID="$cid"
  fi
done <<EOF
$MIGRATE_CIDS
EOF
if [ -z "$MIGRATE_CID" ]; then
  # Nothing was recognizably non-oneoff (e.g. every candidate was itself a one-off, or
  # `docker inspect` could not resolve the label) -- fall back to the last id in list
  # order, matching REV3-5's original behaviour, rather than refusing outright.
  MIGRATE_CID="$(printf '%s\n' "$MIGRATE_CIDS" | tail -n1)"
fi
if [ -z "$MIGRATE_CID" ]; then
  die "no migrate container found after \`up -d migrate\` -- cannot verify its exit code.
  app/worker remain stopped. See docs/production-migration.md section 10.6."
fi
set +e
# `tail -n1` here too: `docker wait` on a single valid id always prints exactly one
# line, but this stays defensive against the same multi-line hazard above.
MIGRATE_EXIT="$(docker wait "$MIGRATE_CID" | tail -n1)"
WAIT_RC=$?
set -e
if [ "$WAIT_RC" -ne 0 ] || [ -z "$MIGRATE_EXIT" ]; then
  die "\`docker wait $MIGRATE_CID\` failed to report an exit code (rc=$WAIT_RC) --
  cannot confirm migrate succeeded. app/worker remain stopped."
fi
if [ "$MIGRATE_EXIT" -ne 0 ]; then
  die "migrate exited $MIGRATE_EXIT -- aborting. app/worker remain stopped (old code is
  not serving, but neither is the new code). Fix the migration, then re-run this script.
  See docs/production-migration.md section 10.6."
fi
echo "    migrate exited 0"

echo "==> [4/6] starting app, worker on the new image"
compose up -d app worker

echo "==> [5/6] waiting for the new app container to report healthy (up to ${APP_HEALTH_TIMEOUT_SECONDS}s)"
APP_CID="$(compose ps -q app)"
if [ -z "$APP_CID" ]; then
  die "no app container found after \`up -d app worker\` -- cannot confirm it is serving."
fi
DEADLINE=$(( $(date +%s) + APP_HEALTH_TIMEOUT_SECONDS ))
HEALTH="unknown"
while :; do
  HEALTH="$(docker inspect -f '{{.State.Health.Status}}' "$APP_CID" 2>/dev/null || echo "unknown")"
  [ "$HEALTH" = "healthy" ] && break
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    die "app container did not report healthy within ${APP_HEALTH_TIMEOUT_SECONDS}s
  (last status: $HEALTH). It is running the new image but may not be serving correctly --
  check \`docker compose logs app\` before assuming the deploy succeeded."
  fi
  sleep "$APP_HEALTH_POLL_INTERVAL_SECONDS"
done
echo "    app is healthy"

echo "==> [6/6] reloading nginx (picks up the recreated app container's new IP)"
compose exec -T nginx nginx -s reload

echo "==> redeploy complete"
