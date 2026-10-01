"""Write a usable Slack app-config ACCESS token to data/.slack_config_token (0600)
for scripts/provision_slack_bots.py, which runs on the host without database access.

The single-use REFRESH token is read and rotated only inside the database
(AppSetting, under a row lock, admin_provisioning._config_token); it is never
written to a file or to .env (PS-17). Run from a one-off container with data/
mounted, as the roster export does, as your own uid so the file is yours:
  docker compose -f docker-compose.prod.yml run --rm --no-deps -T \\
    --user "$(id -u):$(id -g)" -v "$PWD/data:/app/data" \\
    blackbird-app python scripts/slack_config_token.py
Delete data/.slack_config_token when provisioning is done (the host script does)."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT = Path("data/.slack_config_token")


async def _main() -> int:
    from src.database import get_session_factory
    from src.services.admin_provisioning import _config_token

    async with get_session_factory()() as db:
        token = await _config_token(db)
        await db.commit()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    print(f"wrote {OUT} (0600); the refresh token stays in the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
