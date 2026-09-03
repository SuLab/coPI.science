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


EXPECTED_MEM = {
    "postgres": "512m", "app": "384m", "worker": "512m", "agent": "512m",
    "grantbot": "256m", "nginx": "64m", "certbot": "32m",
}


def test_every_prod_service_has_a_memory_and_cpu_ceiling():
    services = _prod_compose()["services"]
    for name, mem in EXPECTED_MEM.items():
        assert services[name].get("mem_limit") == mem, name
        assert services[name].get("cpus"), f"{name} has no cpus limit"


def test_the_copi_python_mem_limits_sum_comfortably_under_the_host_total():
    # Part R.1: the prod host has ~3.7 GB RAM and also runs the separate
    # blackbird stack. A sum near or over the physical total defeats the
    # purpose of a per-container ceiling — the kernel OOM killer fires
    # first, before any cgroup limit does.
    services = _prod_compose()["services"]
    names = (*EXPECTED_MEM, "migrate")
    total_mib = sum(int(services[name]["mem_limit"].rstrip("mg")) for name in names)
    assert total_mib <= 3072, f"copi-python mem_limits sum to {total_mib}m, want <= 3072m"


def test_worker_mounts_prompts_like_app_and_agent_do():
    worker_volumes = _prod_compose()["services"]["worker"]["volumes"]
    assert "./prompts:/app/prompts" in worker_volumes
