"""Static assertions over docker-compose.prod.yml and docker-compose.override.yml.
No compose CLI, no Docker daemon — parsed as plain YAML; ${VAR:-default}
interpolation syntax is just a string to PyYAML, so this needs no substitution."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _prod_compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text())


def _override_compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.override.yml").read_text())


def test_migrate_service_runs_alembic_upgrade_head_and_does_not_restart():
    svc = _prod_compose()["services"]["migrate"]
    assert svc["command"] == ["python", "-m", "alembic", "upgrade", "head"]
    assert svc["restart"] == "no"
    assert svc["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_app_worker_grantbot_wait_for_migrate_to_complete():
    services = _prod_compose()["services"]
    for name in ("app", "worker", "grantbot"):
        dep = services[name]["depends_on"]
        assert dep["migrate"]["condition"] == "service_completed_successfully", name
        assert dep["postgres"]["condition"] == "service_healthy", name


def test_every_prod_service_including_migrate_has_the_json_file_log_override():
    # CLAUDE.md "Compose file set": docker-compose.override.yml forces json-file
    # logging because the EC2 role lacks logs:CreateLogStream; a service missing
    # from it dies at start with AccessDeniedException. A new prod-only service
    # (like this task's `migrate`) is invisible to that protection unless it is
    # added to the override in the SAME commit.
    prod = set(_prod_compose()["services"])
    override = set(_override_compose()["services"])
    assert prod <= override, f"no json-file logging override for {sorted(prod - override)}"


def test_every_service_json_file_log_has_rotation_options():
    # json-file logging with no options grows unbounded --
    # every service's override must cap it (max-size/max-file), same as
    # every other json-file logging block in this repo.
    override = _override_compose()["services"]
    for name, svc in override.items():
        logging_cfg = svc.get("logging", {})
        assert logging_cfg.get("driver") == "json-file", name
        options = logging_cfg.get("options", {})
        assert options.get("max-size") == "50m", (
            f"{name}'s json-file logging is missing max-size: 50m"
        )
        assert options.get("max-file") == "5", (
            f"{name}'s json-file logging is missing max-file: 5"
        )


EXPECTED_MEM = {
    "migrate": "256m", "app": "384m", "worker": "512m", "agent": "768m",
    "grantbot": "256m", "nginx": "128m", "certbot": "128m",
}

EXPECTED_CPUS = {
    "migrate": 0.5, "app": 1.0, "worker": 0.5, "agent": 1.0,
    "grantbot": 0.5, "nginx": 1.0, "certbot": 0.1,
}


def test_every_prod_service_has_a_memory_and_cpu_ceiling():
    services = _prod_compose()["services"]
    for name, mem in EXPECTED_MEM.items():
        assert services[name].get("mem_limit") == mem, name
    for name, cpus in EXPECTED_CPUS.items():
        assert services[name].get("cpus") == cpus, name


def test_postgres_has_no_resource_limit():
    # Decision D24: postgres stays uncapped. A cgroup mem_limit on a ~2.3 GB
    # database also caps its page cache, and an OOM-killed backend restarts
    # the whole cluster -- worse than the unbounded-growth failure mode the
    # other services' limits guard against.
    postgres = _prod_compose()["services"]["postgres"]
    assert "mem_limit" not in postgres
    assert "cpus" not in postgres


def test_the_copi_python_mem_limits_sum_comfortably_under_the_host_total():
    # Part R.1: the prod host has ~3.7 GB RAM and also runs the separate
    # blackbird stack. A sum near or over the physical total defeats the
    # purpose of a per-container ceiling — the kernel OOM killer fires
    # first, before any cgroup limit does. postgres is deliberately excluded
    # (D24, uncapped). The nightly verified-backup container
    # (scripts/backup/copi_backup.py, --memory=768m) and the blackbird stack
    # share whatever headroom remains under 3072m.
    services = _prod_compose()["services"]

    def _mib(v: str) -> int:
        return int(v[:-1]) * (1024 if v[-1] in "gG" else 1)

    total_mib = sum(_mib(services[name]["mem_limit"]) for name in EXPECTED_MEM)
    assert total_mib <= 3072, f"copi-python mem_limits sum to {total_mib}m, want <= 3072m"


def test_agent_has_a_stop_grace_period_for_clean_shutdown_flush():
    # An OOM kill is SIGKILL and skips the shutdown flush that persists the
    # in-flight turn; the runbook's `docker stop -t 30` relies on this being
    # set (dev's docker-compose.yml already has it).
    agent = _prod_compose()["services"]["agent"]
    assert agent.get("stop_grace_period") == "30s"


# `./prompts:/app/prompts` is a bind mount over prompt text the image already
# bakes (`COPY . .`), so it silently shadows the image's prompts on app, agent
# and grantbot. Files under prompts/ are never edited on a live host, so the
# mount serves no live purpose; it remains a stated residual (removing it
# changes deployed behaviour and belongs with the deploy runbook, not here),
# and it must not spread to a fourth service (worker), which would open a new
# un-gated write path into model-facing text.
PROMPTS_MOUNT = "./prompts:/app/prompts"
SERVICES_THAT_STILL_SHADOW_PROMPTS = {"app", "agent", "grantbot"}


def test_the_prompts_bind_mount_did_not_spread_to_a_fourth_service():
    services = _prod_compose()["services"]
    shadowing = {
        name for name, svc in services.items()
        if PROMPTS_MOUNT in (svc.get("volumes") or [])
    }
    assert shadowing == SERVICES_THAT_STILL_SHADOW_PROMPTS, (
        f"services bind-mounting {PROMPTS_MOUNT} are {sorted(shadowing)}; "
        f"expected {sorted(SERVICES_THAT_STILL_SHADOW_PROMPTS)}. Adding one is a new "
        "un-gated write path into model-facing text; removing one of the three "
        "is progress, but update this set to match."
    )
