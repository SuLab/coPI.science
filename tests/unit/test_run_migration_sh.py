"""Bash-level test for `scripts/migrate/run_migration.sh --via-run`.

No database and no real Docker: a fake `docker` shim is placed first on PATH so the
script's `docker compose ...` calls are captured instead of executed. The shim answers
`NOT-app` to the running-services query (Step 1's "service must be running" check),
which would BLOCK the script if that check ran — so a clean exit here proves `--via-run`
skips it, per the brief for issue #27 I2 (the image, not a running container, carries the
new source once `app` is stopped for the migration window).
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_via_run_uses_compose_run_not_exec_and_skips_the_running_check(tmp_path):
    log = tmp_path / "argv.log"
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        'case "$*" in\n'
        '  *"ps --status running --services"*) echo NOT-app ;;\n'
        '  *"import src; print(src.__file__)"*) echo /app/src/__init__.py ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://copi:x@postgres:5432/copi",
        "MIGRATE_BACKUP_DIR": str(tmp_path / "backups"),
    }
    proc = subprocess.run(
        ["./scripts/migrate/run_migration.sh", "--via-run", "--backup-verified-elsewhere", "test"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    argv = log.read_text()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert " run --rm --no-deps -T " in argv
    assert "/migrate-state" in argv, "the snapshot must be on a bind mount (see Step 3)"
    assert " exec " not in argv.replace("exec -T postgres", "")
    # Pin the exact translated snapshot path, not just "/migrate-state" appearing
    # somewhere (the -v mount source alone would satisfy the assertion above even
    # if SNAP_IN_CONTAINER were reverted to the host path everywhere else).
    assert "--snapshot /migrate-state/preflight_snapshot.json" in argv


def test_via_run_apply_translates_the_backup_path_into_the_bind_mount(tmp_path):
    # Regression test for a real bug: Step 3 hands preflight the HOST dump path
    # via --backup-path. Under --via-run that path is meaningless inside the
    # ephemeral container (prod bakes the repo into the image, so there is no
    # repo bind mount to fall back on) -- preflight's find_backup() would return
    # None and evaluate_backup() would BLOCK a rehearsal-free `--via-run --apply`
    # run right after it took a good dump. The fix translates --backup-path into
    # the /migrate-state bind mount the same way the snapshot path already is.
    log = tmp_path / "argv.log"
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        'case "$*" in\n'
        '  *"ps --status running --services"*) echo NOT-app ;;\n'
        '  *"import src; print(src.__file__)"*) echo /app/src/__init__.py ;;\n'
        '  *"compose cp "*) head -c 2048 /dev/zero > "$4" ;;\n'
        '  *"alembic_version"*) echo 0028 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://copi:x@postgres:5432/copi",
        "MIGRATE_BACKUP_DIR": str(tmp_path / "backups"),
    }
    # Deliberately NO --backup-verified-elsewhere: this is the path that takes a
    # real (here, faked-through-the-shim) dump and must still clear preflight.
    proc = subprocess.run(
        ["./scripts/migrate/run_migration.sh", "--via-run", "--apply"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    argv = log.read_text()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--backup-path /migrate-state/" in argv
    assert f"--backup-path {tmp_path}" not in argv
