#!/usr/bin/env bash
#
# Install the verified-backup system onto this host. Idempotent: safe to re-run
# after a code change. Does NOT enable the timers — see the go-live checklist in
# docs/specs/2026-08-18-postgres-backup-verification-design.md §10. Enabling before
# the failure-injection harness has passed ships an unproven backup system.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root (installs to /usr/local/bin and /etc)." >&2
  exit 1
fi

install -m 0755 "$HERE/copi_backup.py" /usr/local/bin/copi-backup

install -d -m 0700 /etc/copi-backup
if [ ! -f /etc/copi-backup/backup.env ]; then
  install -m 0600 "$HERE/backup.env.example" /etc/copi-backup/backup.env
  echo "    wrote /etc/copi-backup/backup.env — EDIT SES_SENDER_EMAIL before enabling"
else
  echo "    /etc/copi-backup/backup.env exists, left untouched"
fi

install -d -m 0700 /var/backups/copi

# copi-backup-failure@.service is the OnFailure= target. copi-backup.service
# references it, so installing one without the other leaves systemd unable to run
# the failure handler — the independent alert path that exists precisely for runs
# that die without executing their own error handling.
# docker-builder-prune.* is not part of the backup system, but the backup system
# cannot run without it. The 2026-09-15/16 outage was Docker build cache filling
# /dev/root until the free-space guard could no longer be satisfied; nothing on this
# host reclaims that cache, and it regrows with every rebuild. Shipped here so the
# precondition is versioned and reviewable rather than a hand-made unit on one host.
for unit in copi-backup.service copi-backup.timer \
            copi-backup-report.service copi-backup-report.timer \
            copi-backup-failure@.service \
            docker-builder-prune.service docker-builder-prune.timer; do
  install -m 0644 "$HERE/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

echo "installed. Timers are NOT enabled yet. To enable after the harness passes:"
echo "  systemctl enable --now copi-backup.timer copi-backup-report.timer"
echo "  systemctl enable --now docker-builder-prune.timer   # build-cache reclaim"
