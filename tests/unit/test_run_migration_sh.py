"""Bash-level test for `scripts/migrate/run_migration.sh --via-run`.

No database and no real Docker: a fake `docker` shim is placed first on PATH so the
script's `docker compose ...` calls are captured instead of executed. The shim answers
`NOT-app` to the running-services query (Step 1's "service must be running" check),
which would BLOCK the script if that check ran — so a clean exit here proves `--via-run`
skips it: the image, not a running container, carries the
new source once `app` is stopped for the migration window.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

from scripts.migrate import preflight as pf

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_MIGRATION_SH = REPO_ROOT / "scripts" / "migrate" / "run_migration.sh"

#: The revision the script must migrate to when no --target is given. Read from
#: preflight, which `test_revision_order_ends_at_the_repo_head` already pins to the
#: alembic tree's own head -- so this file never restates a revision literal that can
#: go stale independently of the tree. A pinned TARGET in run_migration.sh and the two
#: fake-psql shims below pinning the same number went stale together when a new
#: revision landed and neither test noticed, which is why this file derives the
#: revision instead of hardcoding it.
HEAD_REVISION = pf.DEFAULT_TARGET


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
        f'  *"alembic_version"*) echo {HEAD_REVISION} ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://copi:x@postgres:5432/copi",
        "MIGRATE_BACKUP_DIR": str(tmp_path / "backups"),
        # Explicit, matching --backup-dir: the BACKUP_HOST_DIR == SNAP_HOST_DIR
        # guard (run_migration.sh Step 3) needs the dump's directory and the
        # snapshot's directory to agree. Since #27 Minor 12/18 defaulted the
        # snapshot to data/ (not backups/) under --via-run to route around UID
        # 10001's permission denial there, this test names an explicit
        # MIGRATE_SNAPSHOT so it keeps exercising the "same directory" case
        # rather than the (now default) "must state --backup-verified-elsewhere
        # or align the directories" case covered separately below.
        "MIGRATE_SNAPSHOT": str(tmp_path / "backups" / "preflight_snapshot.json"),
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


def test_via_run_default_snapshot_lands_under_data_not_backups(tmp_path):
    # #27 Critical 1 / Minor 12: UID 10001 (the image's runtime user) cannot write
    # into backups/ on the prod host, only into data/ (already chowned per Part
    # R.5). Without an explicit MIGRATE_SNAPSHOT, --via-run must default the
    # snapshot under <repo>/data/, not <repo>/backups/ (BACKUP_DIR's default).
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
        # Deliberately NOT set: MIGRATE_SNAPSHOT. MIGRATE_BACKUP_DIR is set to
        # prove the snapshot default does not simply follow BACKUP_DIR under
        # --via-run any more.
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
    assert f"-v {REPO_ROOT}/data:/migrate-state" in argv
    assert str(tmp_path / "backups") not in argv


def test_explicit_migrate_snapshot_still_wins_under_via_run(tmp_path):
    # An operator who names MIGRATE_SNAPSHOT explicitly must still be obeyed —
    # the data/ default above is a fallback, not an override.
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
    custom_snap = tmp_path / "custom" / "snap.json"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://copi:x@postgres:5432/copi",
        "MIGRATE_SNAPSHOT": str(custom_snap),
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
    assert f"-v {tmp_path}/custom:/migrate-state" in argv
    assert "--snapshot /migrate-state/snap.json" in argv


def test_backup_dump_honours_postgres_user(tmp_path):
    # #27 Minor 18: Step 3 hardcoded `pg_dump -U copi`. R.1/R.10 use the same
    # literal, but the tool itself should read POSTGRES_USER like the rest of the
    # compose stack does, defaulting to copi.
    log = tmp_path / "argv.log"
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        'case "$*" in\n'
        '  *"ps --status running --services"*) echo app ;;\n'
        '  *"import src; print(src.__file__)"*) echo /app/src/__init__.py ;;\n'
        '  *"compose cp "*) head -c 2048 /dev/zero > "$4" ;;\n'
        f'  *"alembic_version"*) echo {HEAD_REVISION} ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://copi:x@postgres:5432/copi",
        "MIGRATE_BACKUP_DIR": str(tmp_path / "backups"),
        "POSTGRES_USER": "custom_pg_user",
    }
    # Deliberately NO --via-run and NO --backup-verified-elsewhere: this is the
    # plain --apply path that takes a real (faked-through-the-shim) dump.
    proc = subprocess.run(
        ["./scripts/migrate/run_migration.sh", "--apply"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    argv = log.read_text()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pg_dump -U custom_pg_user" in argv
    assert "pg_dump -U copi" not in argv


def _shim(tmp_path, log):
    """A fake `docker` that captures argv and answers the four probes the script makes."""
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        'case "$*" in\n'
        '  *"ps --status running --services"*) echo app ;;\n'
        '  *"import src; print(src.__file__)"*) echo /app/src/__init__.py ;;\n'
        f'  *"alembic_version"*) echo {HEAD_REVISION} ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


def _run(tmp_path, *args):
    log = tmp_path / "argv.log"
    _shim(tmp_path, log)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://copi:x@postgres:5432/copi",
        "MIGRATE_BACKUP_DIR": str(tmp_path / "backups"),
    }
    proc = subprocess.run(
        ["./scripts/migrate/run_migration.sh", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, (log.read_text() if log.exists() else "")


def test_the_default_target_is_derived_from_the_alembic_tree_not_a_pinned_literal(tmp_path):
    """A bare `--apply` must migrate to the tree's head.

    `TARGET="0028"` was pinned in the script while the tree's head moved to 0029, so a
    bare `--apply` migrated to 0028, stamped it, verified it, and reported success --
    leaving the app to start against a schema with no `thread_decisions.pi_engaged_at`.
    preflight only WARNs on a target that is not the head, and run_migration.sh does not
    stop on a preflight warning in --apply mode, so nothing blocked it.
    """
    proc, argv = _run(tmp_path, "--apply", "--backup-verified-elsewhere", "test")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f" coPI production migration -> {HEAD_REVISION}" in proc.stdout, proc.stdout
    assert f"--target {HEAD_REVISION}" in argv, argv
    assert f"upgrade {HEAD_REVISION}" in argv, argv


def test_no_revision_literal_is_pinned_in_the_script():
    """The defect was a constant, so the fix is pinned as 'there is no constant'.

    Bumping 0028 to 0029 would have made the test above pass and left the next head
    bump to fail exactly the same way -- this is the third stale-constant defect on
    this branch.
    """
    source = RUN_MIGRATION_SH.read_text()
    pinned = re.findall(r'^\s*TARGET=(["\']?)(\d{4})\1\s*$', source, re.M)
    assert pinned == [], (
        f"run_migration.sh assigns a hard-coded revision to TARGET ({pinned}); it must "
        f"derive the head from alembic/versions/ instead"
    )


def test_an_explicit_target_still_overrides_the_derived_head(tmp_path):
    """--target is how you migrate to something other than the head, deliberately."""
    proc, argv = _run(
        tmp_path, "--target", "0026", "--backup-verified-elsewhere", "test"
    )
    assert proc.returncode in (0, 2), proc.stdout + proc.stderr
    assert " coPI production migration -> 0026" in proc.stdout, proc.stdout
    assert "--target 0026" in argv, argv


def test_the_banner_says_where_the_target_came_from(tmp_path):
    """An operator reading the log must be able to tell a derived target from one they
    typed -- otherwise a wrong `--target` and a wrong tree look identical afterwards."""
    derived, _ = _run(tmp_path, "--backup-verified-elsewhere", "test")
    typed, _ = _run(tmp_path, "--target", "0026", "--backup-verified-elsewhere", "test")
    assert (
        f" coPI production migration -> {HEAD_REVISION} (derived from alembic/versions/)"
        in derived.stdout
    ), derived.stdout
    assert " coPI production migration -> 0026 (--target)" in typed.stdout, typed.stdout
