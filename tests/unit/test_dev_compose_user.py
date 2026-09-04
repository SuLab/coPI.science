"""Static assertions over docker-compose.yml (dev) and docker-compose.prod.yml.

No compose CLI, no Docker daemon — parsed as plain YAML.

Since Task 27.8 (commit 5f8aa2b) the image runs as fixed UID 10001
(Dockerfile). The dev compose file bind-mounts the whole checkout at
`.:/app`, which is owned by the host user (typically UID 1000/1001, not
10001) — so dev containers built from the same image can no longer write
into the bind-mounted tree. The fix is dev-only: every service in
docker-compose.yml that runs the app image gets `user: "0:0"` so it runs
as root inside the container. Prod keeps the image's UID 10001 unchanged —
docker-compose.prod.yml must have no `user:` key on any of the services that
run the app image (app/worker/agent/grantbot/migrate). nginx, certbot and
postgres run their own upstream images and are out of scope for this
assertion — it is not this test's job to constrain them.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# Services in docker-compose.yml that build/run the app image (Dockerfile).
# `postgres` uses the upstream postgres:15 image and is excluded.
APP_IMAGE_SERVICES = ("app", "worker", "agent", "grantbot")

# Services in docker-compose.prod.yml that build/run the app image
# (Dockerfile). Prod also has a one-shot `migrate` service that dev does not
# (dev migrates via the app container's entrypoint). nginx/certbot/postgres
# run upstream images, not this Dockerfile, so they're excluded here.
PROD_APP_IMAGE_SERVICES = ("app", "worker", "agent", "grantbot", "migrate")


def _dev_compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _prod_compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text())


def test_every_app_image_service_in_dev_compose_runs_as_root():
    services = _dev_compose()["services"]
    for name in APP_IMAGE_SERVICES:
        assert services[name].get("user") == "0:0", (
            f"{name} must set user: \"0:0\" in docker-compose.yml so the "
            "host-owned bind mount stays writable under the UID 10001 image"
        )


def test_postgres_in_dev_compose_has_no_user_override():
    services = _dev_compose()["services"]
    assert "user" not in services["postgres"]


def test_prod_compose_has_no_user_key_on_any_service():
    services = _prod_compose()["services"]
    for name in PROD_APP_IMAGE_SERVICES:
        assert "user" not in services[name], (
            f"{name} must not set user: in docker-compose.prod.yml — "
            "prod keeps the image's fixed UID 10001"
        )
