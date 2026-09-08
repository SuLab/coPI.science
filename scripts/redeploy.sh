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
#   2. stop the OLD app/worker (they must not serve while migrate runs)
#   3. start migrate and wait for it to exit; abort on nonzero (old containers stay
#      stopped -- refusing is safer than guessing)
#   4. start the NEW app/worker only once migrate is verified to have exited 0
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

echo "==> [1/5] building migrate, app, worker"
compose build migrate app worker

echo "==> [2/5] stopping app, worker (must not serve while migrate applies the new schema)"
compose stop app worker

echo "==> [3/5] running migrate"
compose up -d migrate
set +e
compose wait migrate
MIGRATE_EXIT=$?
set -e
if [ "$MIGRATE_EXIT" -ne 0 ]; then
  die "migrate exited $MIGRATE_EXIT -- aborting. app/worker remain stopped (old code is
  not serving, but neither is the new code). Fix the migration, then re-run this script.
  See docs/production-migration.md section 10.6."
fi
echo "    migrate exited 0"

echo "==> [4/5] starting app, worker on the new image"
compose up -d app worker

echo "==> [5/5] reloading nginx (picks up the recreated app container's new IP)"
compose exec -T nginx nginx -s reload

echo "==> redeploy complete"
