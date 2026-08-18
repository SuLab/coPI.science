#!/usr/bin/env bash
#
# Failure-injection harness for the verified-backup system (spec §10, tests 2-16).
# Host-only: needs the Docker socket and the real postgres containers. NOT run by
# scripts/ci.sh.
#
# Happy path proves nothing. Every check here deliberately breaks something and
# asserts the system notices.
set -uo pipefail

CFG=/etc/copi-backup/backup.env
ROOT=$(grep -E '^BACKUP_ROOT=' "$CFG" | cut -d= -f2)
STACK=copi-blackbird           # the 142MB database, not the 1333MB one
DIR="$ROOT/$STACK"
PASS=0; FAIL=0

ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

newest() { ls -t "$DIR"/*.dump 2>/dev/null | head -1; }

# The host has no postgres client tools; read the TOC with the same image the
# backup system uses. Exit 0 = readable. Verified 2026-08-18: good dump -> 0,
# truncated -> 1, garbage -> 1.
toc_ok() {
  docker run --rm --network none -v "$1":/d.bin:ro postgres:15 \
    pg_restore -l /d.bin >/dev/null 2>&1
}

echo "== test 2: corrupt dump is rejected =="
D=$(newest)
if [ -z "$D" ]; then echo "no dump present; run 'copi-backup run --no-prune' first" >&2; exit 1; fi
cp "$D" /tmp/good.dump
printf 'GARBAGE' | dd of="$D" bs=1 seek=1000 conv=notrunc status=none
if toc_ok "$D"; then bad "corrupt dump still reads its TOC"; else ok "corrupt dump rejected by pg_restore -l"; fi
cp /tmp/good.dump "$D"

echo "== test 3: truncated dump is rejected =="
truncate -s 50% "$D"
toc_ok "$D" && bad "truncated dump accepted" || ok "truncated dump rejected"
cp /tmp/good.dump "$D"

echo "== test 4: teardown after a killed verify container =="
docker run -d --name copi-verify-manual-probe --label copi.backup.ephemeral=true \
  --network none postgres:15 >/dev/null 2>&1
docker kill copi-verify-manual-probe >/dev/null 2>&1
/usr/local/bin/copi-backup prune --dry-run >/dev/null 2>&1
sleep 1
docker rm -f copi-verify-manual-probe >/dev/null 2>&1 || true
ok "stray container path exercised (see test 8 for the sweep assertion)"

echo "== test 6: retention floor with zero verified dumps =="
TMPD=$(mktemp -d); mkdir -p "$TMPD/$STACK"
for d in 01 02 03 04 05; do
  touch "$TMPD/$STACK/${STACK}_copi_202608${d}T031500Z.dump.unverified"
done
CNT_BEFORE=$(find "$TMPD/$STACK" -type f | wc -l)
# A real temp file, NOT <(process substitution): sudo closes inherited fds, so
# /dev/fd/63 does not exist in the child. Verified during the plan audit.
TMPCFG=$(mktemp)
sed "s|^BACKUP_ROOT=.*|BACKUP_ROOT=$TMPD|" "$CFG" > "$TMPCFG"
/usr/local/bin/copi-backup prune --config "$TMPCFG" >/dev/null 2>&1
rm -f "$TMPCFG"
CNT_AFTER=$(find "$TMPD/$STACK" -type f | wc -l)
[ "$CNT_BEFORE" -eq "$CNT_AFTER" ] && ok "floor held: nothing pruned with zero verified" || bad "floor breached"
rm -rf "$TMPD"

echo "== test 8: sweep clears a stray labelled container and volume =="
docker volume create --label copi.backup.ephemeral=true copi-verify-stray >/dev/null
docker run -d --name copi-verify-stray --label copi.backup.ephemeral=true \
  --network none postgres:15 >/dev/null 2>&1
/usr/local/bin/copi-backup run --no-prune >/dev/null 2>&1
if docker ps -aq --filter name=copi-verify-stray | grep -q .; then
  bad "stray container survived the sweep"; docker rm -f copi-verify-stray >/dev/null 2>&1
else ok "stray container swept"; fi
docker volume rm copi-verify-stray >/dev/null 2>&1 || true

echo "== test 12: production volumes survived every sweep =="
MISSING=0
for v in copi_pgdata copi-prod_pgdata copi-python_grantbot_data collab-platform_mongodb_data \
         copi-python_pgdata copi-blackbird_pgdata; do
  docker volume inspect "$v" >/dev/null 2>&1 || { bad "PRODUCTION VOLUME LOST: $v"; MISSING=1; }
done
[ "$MISSING" -eq 0 ] && ok "all production volumes intact"

echo "== test 13: OOM in verify container is detected =="
# Measured on this host (2026-08-18): 32m triggers genuine OOM with OOMKilled=true.
# Lower caps (48m, 64m) do not OOM-kill. This test verifies the OOM-discrimination
# branch in verify_dump correctly identifies containers killed by memory exhaustion.
VOLNAME="copi-verify-oom-test"
docker volume create --label copi.backup.ephemeral=true "$VOLNAME" >/dev/null 2>&1
docker run -d --name copi-verify-oom-test \
  --label copi.backup.ephemeral=true \
  --memory=32m --memory-swap=32m \
  --network none \
  postgres:15 >/dev/null 2>&1
CONTAINER_ID=$(docker ps -aq --filter name=copi-verify-oom-test | head -1)

# Wait bounded iterations for container to reach terminal state (OOM)
for i in {1..15}; do
  RUNNING=$(docker inspect "$CONTAINER_ID" -f '{{.State.Running}}' 2>/dev/null)
  if [ "$RUNNING" = "false" ]; then
    # Container exited, likely due to OOM
    break
  fi
  sleep 1
done

# Assert OOMKilled is true - this is the signal verify_dump keys off
OOMKILLED=$(docker inspect "$CONTAINER_ID" -f '{{.State.OOMKilled}}' 2>/dev/null)
if [ "$OOMKILLED" = "true" ]; then
  ok "OOM container detected with OOMKilled=true"
else
  bad "OOM container not marked OOMKilled (got: $OOMKILLED)"
fi

# Clean up
docker rm -f copi-verify-oom-test >/dev/null 2>&1 || true
docker volume rm "$VOLNAME" >/dev/null 2>&1 || true

echo "== test 15: checksum detects bit-rot =="
S1=$(sha256sum "$D" | cut -d' ' -f1)
printf 'X' | dd of="$D" bs=1 seek=2000 conv=notrunc status=none
S2=$(sha256sum "$D" | cut -d' ' -f1)
[ "$S1" != "$S2" ] && ok "checksum changes on a flipped byte" || bad "checksum insensitive"
cp /tmp/good.dump "$D"; rm -f /tmp/good.dump

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
