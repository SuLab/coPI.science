#!/usr/bin/env bash
#
# Redeploy app/worker/grantbot against a migrated schema, without ever serving
# requests against a schema `migrate` has not applied yet.
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
# `grantbot` has the exact same `depends_on: migrate: condition:
# service_completed_successfully` shape as app/worker in docker-compose.prod.yml, so
# it is exposed to the identical race and is built/stopped/started alongside them.
# `agent` is deliberately excluded: it is a one-off (`docker compose run`, not a
# long-running service in this compose file's default service set) with its own
# restart runbook in CLAUDE.md, not something this script starts.
#
# THE FIX IS ORDERING, ENFORCED EXPLICITLY, NOT LEFT TO depends_on:
#   1. build migrate, app, worker, grantbot (new images)
#   2. stop the OLD app/worker/grantbot, GRACEFULLY (`-t 30`, matching the
#      agent-restart runbook in CLAUDE.md -- an in-flight request gets a chance to
#      finish rather than being SIGKILLed at 0s); they must not serve while migrate
#      runs
#   3. start migrate and wait for it to exit; abort on nonzero (old containers stay
#      stopped -- refusing is safer than guessing). The exit code is read via `docker
#      wait <container id>`, NOT `docker compose wait migrate`: the latter looks up
#      the service by PROJECT state, and against a `migrate` that has already exited
#      by the time `wait` runs, it fails with "no containers for project" instead of
#      reporting the exit code -- which would abort this script with app/worker left
#      stopped even though migrate actually succeeded. `docker wait` on the
#      container's own id talks to the Engine API directly and has no such race.
#   4. start the NEW app/worker/grantbot only once migrate is verified to have
#      exited 0, and wait for the new app container to report Docker-healthy
#      (bounded; see APP_HEALTH_TIMEOUT_SECONDS) before declaring success -- `up -d`
#      returning just means the container was created, not that it is serving.
#      grantbot has no healthcheck in docker-compose.prod.yml, so there is nothing
#      to wait on for it beyond `up -d` reporting it created.
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

# Every compose file this invocation would ACTUALLY use once `compose()` runs.
#
# `docker compose` ignores $COMPOSE_FILE entirely the moment ANY `-f`/`--file` flag is
# passed (documented `docker compose` behaviour) -- unioning $COMPOSE_FILE with any -f
# args before checking for both prod files would let a caller who passes only
# `-f docker-compose.prod.yml` pass the guard on the strength of an unrelated
# $COMPOSE_FILE, while the real invocation never sees the override file and every
# service comes up on the prod file's bare `logging.driver: awslogs`, dying immediately
# with AccessDeniedException (see CLAUDE.md "Compose file set"). Only fall back to
# $COMPOSE_FILE when NO -f/--file was given at all; once any -f is present, it is the
# exhaustive list and $COMPOSE_FILE is ignored, matching `docker compose`'s own
# precedence.
_known_files() {
  local files=""
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
  if [ -z "$files" ]; then
    files="${COMPOSE_FILE:-}"
  fi
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

echo "==> [1/6] building migrate, app, worker, grantbot"
compose build migrate app worker grantbot

echo "==> [2/6] stopping app, worker, grantbot (must not serve while migrate applies the new schema)"
compose stop -t 30 app worker grantbot

echo "==> [3/6] running migrate"
compose up -d migrate
# `compose ps -aq migrate` can return SEVERAL ids -- a stale one-off `migrate`
# container from an earlier `docker compose run` left alongside the one `up -d
# migrate` just created. Passing the whole newline-joined blob to `docker wait
# "$MIGRATE_CID"` as a single argument is exactly what real `docker wait` rejects (no
# parseable exit code, non-zero exit), tripping the "failed to report an exit code"
# abort below even after a SUCCESSFUL migration.
#
# Selecting by list POSITION (`tail -n1`, on the assumption compose always lists them
# oldest-first) is not reliable either -- a stale one-off `<proj>-migrate-run-<hash>`
# container can sort AFTER the persistent `<proj>-migrate-1` service container this
# run actually cares about. Select by the `com.docker.compose.oneoff` label instead
# (`False` on the service container `up -d` creates, `True` on every `docker compose
# run`/`exec`-style one-off) -- that is what actually distinguishes them, not creation
# order. Kept dependency-free (no jq, no project-name derivation): `compose ps -aq
# migrate` already scopes the id list to this project's `migrate` service, so only the
# oneoff label needs checking, via `docker inspect` (already used below for the app
# healthcheck).
MIGRATE_CIDS="$(compose ps -aq migrate)"
if [ -z "$MIGRATE_CIDS" ]; then
  die "no migrate container found after \`up -d migrate\` -- cannot verify its exit code.
  app/worker/grantbot remain stopped. See docs/production-migration.md section 10.6."
fi
MIGRATE_CID=""
while IFS= read -r cid; do
  [ -z "$cid" ] && continue
  ONEOFF="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.oneoff"}}' "$cid" 2>/dev/null || echo "")"
  # Accept only an explicit "False": an empty/unknown label (inspect failed, or the
  # container vanished between `ps` and `inspect`) must not elect a dead id, or
  # `docker wait` on it aborts the deploy after a migration that succeeded.
  if [ "$ONEOFF" = "False" ]; then
    MIGRATE_CID="$cid"
  fi
done <<EOF
$MIGRATE_CIDS
EOF
if [ -z "$MIGRATE_CID" ]; then
  # No candidate carried an explicit oneoff=False. With a SINGLE candidate that is
  # the container `up -d migrate` just made, so use it. With several, guessing by
  # list position could elect a stale one-off whose old exit code (usually 0) would
  # green-light app/worker against an un-migrated schema -- fail closed instead.
  if [ "$(printf '%s\n' "$MIGRATE_CIDS" | grep -c .)" -gt 1 ]; then
    die "several migrate containers exist and none carries com.docker.compose.oneoff=False
  (docker inspect could not read the label). Refusing to guess which one this deploy ran.
  app/worker/grantbot remain stopped. Remove stale one-off migrate containers and re-run."
  fi
  MIGRATE_CID="$(printf '%s\n' "$MIGRATE_CIDS" | tail -n1)"
fi
if [ -z "$MIGRATE_CID" ]; then
  die "no migrate container found after \`up -d migrate\` -- cannot verify its exit code.
  app/worker/grantbot remain stopped. See docs/production-migration.md section 10.6."
fi
set +e
# `tail -n1` here too: `docker wait` on a single valid id always prints exactly one
# line, but this stays defensive against the same multi-line hazard above.
MIGRATE_EXIT="$(docker wait "$MIGRATE_CID" | tail -n1)"
WAIT_RC=$?
set -e
if [ "$WAIT_RC" -ne 0 ] || [ -z "$MIGRATE_EXIT" ]; then
  die "\`docker wait $MIGRATE_CID\` failed to report an exit code (rc=$WAIT_RC) --
  cannot confirm migrate succeeded. app/worker/grantbot remain stopped."
fi
# `[ "$MIGRATE_EXIT" -ne 0 ]` on a non-numeric value (e.g. `docker wait` printing
# garbage) makes `[` print "integer expression expected" to stderr and return exit
# status 2 -- which `if` treats identically to a clean "false", falling through as
# though migrate had exited 0. Reject anything that is not a plain non-negative
# integer before the numeric comparison ever runs.
case "$MIGRATE_EXIT" in
  ''|*[!0-9]*)
    die "\`docker wait $MIGRATE_CID\` reported a non-numeric exit code
  ('$MIGRATE_EXIT') -- cannot confirm migrate succeeded. app/worker/grantbot
  remain stopped."
    ;;
esac
if [ "$MIGRATE_EXIT" -ne 0 ]; then
  die "migrate exited $MIGRATE_EXIT -- aborting. app/worker/grantbot remain stopped (old code is
  not serving, but neither is the new code). Fix the migration, then re-run this script.
  See docs/production-migration.md section 10.6."
fi
echo "    migrate exited 0"

echo "==> [4/6] starting app, worker, grantbot on the new image"
compose up -d app worker grantbot

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
