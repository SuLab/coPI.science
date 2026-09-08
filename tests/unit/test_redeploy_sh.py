"""Bash-level tests for `scripts/redeploy.sh` (audit 2026-09-08 RC-6, #27 I2).

Root cause: `docker compose $C up -d --build app worker` on an ALREADY RUNNING stack does
not guarantee `migrate` reruns before the new app/worker containers start — `depends_on:
service_completed_successfully` only orders container *creation*, and an existing exited
`migrate` container can satisfy that condition without being re-run against the freshly
built image. The old code keeps serving requests against a schema `migrate` has not
applied yet for however long that window lasts.

`redeploy.sh` fixes the ordering explicitly: build migrate+app+worker, STOP the old
app/worker so they cannot serve during the window, run migrate to completion and check its
exit code, only then start the new app/worker, then reload nginx (the recreated app
container gets a new IP; nginx's static `upstream app { server app:8000; }` caches the old
one until reloaded -- see the nginx-stale-upstream-ip memory note).

No real Docker: a fake `docker` shim is placed first on PATH, mirroring
tests/unit/test_run_migration_sh.py's approach, so every `docker compose ...` invocation is
captured into a log file instead of executed.
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REDEPLOY_SH = REPO_ROOT / "scripts" / "redeploy.sh"

PROD_FILE = "docker-compose.prod.yml"
OVERRIDE_FILE = "docker-compose.override.yml"


def _shim(tmp_path: Path, *, migrate_exit: int = 0) -> tuple[Path, Path]:
    """A `docker` on PATH that logs every invocation and answers `docker compose wait
    migrate` with `migrate_exit` (the exit code check redeploy.sh must perform after
    starting `migrate`)."""
    log = tmp_path / "argv.log"
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        'case "$*" in\n'
        f'  *"wait migrate"*) exit {migrate_exit} ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim, log


def _run(tmp_path: Path, args: list[str], *, compose_file_env: str | None = None):
    shim, log = _shim(tmp_path)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    env.pop("COMPOSE_FILE", None)
    if compose_file_env is not None:
        env["COMPOSE_FILE"] = compose_file_env
    proc = subprocess.run(
        [str(REDEPLOY_SH), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc, log


def test_script_exists_and_is_executable():
    assert REDEPLOY_SH.is_file(), f"missing {REDEPLOY_SH}"
    mode = REDEPLOY_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/redeploy.sh must be executable"


def test_refuses_with_no_compose_file_information_at_all(tmp_path):
    proc, log = _run(tmp_path, [])
    assert proc.returncode != 0
    assert not log.exists() or log.read_text() == "", (
        "redeploy.sh must refuse BEFORE issuing any docker compose command when it "
        "cannot confirm the prod compose file set"
    )
    assert PROD_FILE in proc.stderr and OVERRIDE_FILE in proc.stderr


def test_refuses_against_the_dev_compose_file_only(tmp_path):
    proc, log = _run(tmp_path, ["-f", "docker-compose.yml"])
    assert proc.returncode != 0
    assert not log.exists() or log.read_text() == ""


def test_refuses_with_only_the_prod_file_and_not_the_override(tmp_path):
    # docker-compose.override.yml is required too (CLAUDE.md: without it every service
    # dies at start with AccessDeniedException under awslogs) -- half the pair is still
    # a refusal, not a best-effort attempt.
    proc, log = _run(tmp_path, ["-f", PROD_FILE])
    assert proc.returncode != 0
    assert not log.exists() or log.read_text() == ""


def test_accepts_both_prod_files_via_dash_f_flags(tmp_path):
    proc, log = _run(tmp_path, ["-f", PROD_FILE, "-f", OVERRIDE_FILE])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.exists() and log.read_text() != ""


def test_accepts_both_prod_files_via_compose_file_env_var(tmp_path):
    proc, log = _run(tmp_path, [], compose_file_env=f"{PROD_FILE}:{OVERRIDE_FILE}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.exists() and log.read_text() != ""


def test_step_order_builds_stops_migrates_then_starts_then_reloads(tmp_path):
    proc, log = _run(tmp_path, ["-f", PROD_FILE, "-f", OVERRIDE_FILE])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    argv = log.read_text()
    lines = [line for line in argv.splitlines() if line.strip()]

    def first_index(predicate):
        for i, line in enumerate(lines):
            if predicate(line):
                return i
        raise AssertionError(f"no matching docker compose call found in:\n{argv}")

    i_build = first_index(
        lambda line: "build" in line and "migrate" in line and "app" in line and "worker" in line
    )
    i_stop = first_index(lambda line: line.split()[0:1] == ["stop"] or " stop " in f" {line} ")
    i_up_migrate = first_index(
        lambda line: "up" in line and "-d" in line and "migrate" in line and "app" not in line
    )
    i_wait = first_index(lambda line: "wait" in line and "migrate" in line)
    i_up_app = first_index(
        lambda line: (
            "up" in line
            and "-d" in line
            and "app" in line
            and "worker" in line
            and "migrate" not in line
        )
    )
    i_reload = first_index(lambda line: "nginx" in line and "reload" in line)

    assert i_build < i_stop < i_up_migrate < i_wait < i_up_app < i_reload, (
        f"step order out of sequence:\n{argv}"
    )


def test_never_passes_remove_orphans():
    # --remove-orphans deletes the prod nginx/certbot containers (CLAUDE.md).
    text = REDEPLOY_SH.read_text()
    assert "--remove-orphans" not in text


def test_aborts_and_does_not_start_app_worker_when_migrate_fails(tmp_path):
    shim, log = _shim(tmp_path, migrate_exit=1)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    env.pop("COMPOSE_FILE", None)
    proc = subprocess.run(
        [str(REDEPLOY_SH), "-f", PROD_FILE, "-f", OVERRIDE_FILE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    argv = log.read_text()
    started_new_app_worker = any(
        "up" in line and "-d" in line and "app" in line and "worker" in line and "migrate" not in line
        for line in argv.splitlines()
    )
    assert not started_new_app_worker, (
        "redeploy.sh started app/worker on the new image after migrate failed:\n" + argv
    )
