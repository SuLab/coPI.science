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


MIGRATE_CID = "fake-migrate-cid"
APP_CID = "fake-app-cid"


def _shim(
    tmp_path: Path, *, migrate_exit: int = 0, app_health_status: str = "healthy"
) -> tuple[Path, Path]:
    """A `docker` on PATH that logs every invocation and fakes just enough of the real
    CLI for redeploy.sh's post-RC-6-review flow:

    - `docker compose ... ps -aq migrate` -> a fake container id (so the script has
      something to pass to `docker wait`, mirroring the real one-shot's lifecycle).
    - `docker wait <that id>` -> prints `migrate_exit` to stdout, exits 0 itself (this
      is the real command's contract: the container's exit code is the OUTPUT, not the
      process's own exit status -- unlike the `docker compose wait` this replaced,
      which returned the exit code as ITS OWN exit status and had a "no containers for
      project" race against an already-exited one-shot).
    - `docker compose ... ps -q app` -> a fake app container id.
    - `docker inspect -f '{{.State.Health.Status}}' <that id>` -> `app_health_status`.
    """
    log = tmp_path / "argv.log"
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        'case "$*" in\n'
        f'  *"ps -aq migrate"*) echo {MIGRATE_CID} ;;\n'
        f'  "wait {MIGRATE_CID}") echo {migrate_exit} ;;\n'
        f'  *"ps -q app"*) echo {APP_CID} ;;\n'
        f'  *"inspect -f "*"{APP_CID}"*) echo {app_health_status} ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim, log


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    compose_file_env: str | None = None,
    migrate_exit: int = 0,
    app_health_status: str = "healthy",
    extra_env: dict | None = None,
):
    shim, log = _shim(tmp_path, migrate_exit=migrate_exit, app_health_status=app_health_status)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        # Fast and deterministic: the happy path answers "healthy" on the first poll
        # regardless of these, and the timeout test wants a short bound.
        "APP_HEALTH_TIMEOUT_SECONDS": "1",
        "APP_HEALTH_POLL_INTERVAL_SECONDS": "0",
        **(extra_env or {}),
    }
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
    proc, log = _run(tmp_path, ["-f", PROD_FILE, "-f", OVERRIDE_FILE], migrate_exit=1)
    assert proc.returncode != 0
    argv = log.read_text()
    started_new_app_worker = any(
        "up" in line and "-d" in line and "app" in line and "worker" in line and "migrate" not in line
        for line in argv.splitlines()
    )
    assert not started_new_app_worker, (
        "redeploy.sh started app/worker on the new image after migrate failed:\n" + argv
    )


# --- Opus review of RC-6 (2026-09-08) ----------------------------------------------
# `docker compose wait migrate` returns 1 with "no containers for project" (measured
# by the reviewer at >=0.3s) when the one-shot has ALREADY exited by the time `wait`
# runs -- a real race, not a hypothetical, since `up -d migrate` can return before or
# after the container finishes. That would abort the deploy with app/worker stopped
# even though migrate actually succeeded. Fixed by reading the exit code via `docker
# wait <container id>` (talks to the Engine API directly, no project-state race).


def test_migrate_exit_code_is_read_via_docker_wait_on_the_container_id_not_compose_wait(tmp_path):
    proc, log = _run(tmp_path, ["-f", PROD_FILE, "-f", OVERRIDE_FILE])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    argv = log.read_text()
    lines = [line for line in argv.splitlines() if line.strip()]

    # The `ps -aq migrate` lookup happened (redeploy.sh resolves the container id).
    assert any("ps -aq migrate" in line for line in lines), argv
    # And the exit code came from a BARE `docker wait <id>` call -- not routed through
    # `docker compose ... wait migrate` (compose-level `wait`), which is the buggy
    # command this replaces.
    assert any(line.strip() == f"wait {MIGRATE_CID}" for line in lines), (
        f"expected a bare 'docker wait {MIGRATE_CID}' call; got:\n{argv}"
    )
    assert not any("compose" in line and "wait" in line and "migrate" in line for line in lines), (
        f"redeploy.sh must not call 'docker compose ... wait migrate':\n{argv}"
    )


def test_aborts_when_migrate_container_id_cannot_be_resolved(tmp_path):
    # `ps -aq migrate` prints nothing (e.g. the compose project name doesn't match) --
    # redeploy.sh must refuse rather than call `docker wait ""`.
    log = tmp_path / "argv.log"
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        'case "$*" in\n'
        '  *"ps -aq migrate"*) ;;\n'  # prints nothing
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
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
    assert "wait \"\"" not in log.read_text()


def test_app_worker_are_stopped_gracefully_with_a_30s_timeout(tmp_path):
    proc, log = _run(tmp_path, ["-f", PROD_FILE, "-f", OVERRIDE_FILE])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    stop_lines = [line for line in lines if " stop " in f" {line} "]
    assert stop_lines, log.read_text()
    assert any("-t 30" in line for line in stop_lines), (
        f"stop must use '-t 30' (matches CLAUDE.md's agent-restart runbook), got: {stop_lines}"
    )


def test_waits_for_the_new_app_container_to_report_healthy_before_reloading_nginx(tmp_path):
    proc, log = _run(tmp_path, ["-f", PROD_FILE, "-f", OVERRIDE_FILE], app_health_status="healthy")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [line for line in log.read_text().splitlines() if line.strip()]

    def first_index(predicate):
        for i, line in enumerate(lines):
            if predicate(line):
                return i
        raise AssertionError(f"no matching call found in:\n{lines}")

    i_up_app = first_index(
        lambda line: "up" in line and "-d" in line and "app" in line and "worker" in line
        and "migrate" not in line
    )
    i_health = first_index(lambda line: "inspect" in line and APP_CID in line)
    i_reload = first_index(lambda line: "nginx" in line and "reload" in line)
    assert i_up_app < i_health < i_reload, f"order out of sequence:\n{lines}"


def test_aborts_if_the_app_container_never_becomes_healthy_within_the_bound(tmp_path):
    # Always "starting" -- a container that never passes its healthcheck. Must abort
    # within the bounded timeout (env-overridden to 1s / 0s poll by _run) rather than
    # hang or declare success anyway, and must never reach the nginx reload step.
    proc, log = _run(
        tmp_path, ["-f", PROD_FILE, "-f", OVERRIDE_FILE], app_health_status="starting"
    )
    assert proc.returncode != 0
    argv = log.read_text()
    assert not any("nginx" in line and "reload" in line for line in argv.splitlines()), (
        f"nginx was reloaded even though the app container never reported healthy:\n{argv}"
    )
