"""Render /admin/simulation for given run ids to stdout, as the first allowed
admin, without a browser session. Run INSIDE a one-off app container off the
current image with the working tree's src/ and templates/ mounted read-only:

  docker compose -f docker-compose.prod.yml run --rm -T \
    -v "$PWD/src:/app/src:ro" -v "$PWD/templates:/app/templates:ro" \
    -v "$PWD/scripts:/app/scripts:ro" blackbird-app \
    python scripts/render_admin_simulation.py <run-uuid> [<run-uuid> …] > /tmp/sim.html

GET only; nothing is written. Never `exec` this into the live app container.
"""
import asyncio
import sys

import httpx
from httpx import ASGITransport
from sqlalchemy import select


async def main(run_ids: list[str]) -> None:
    from src.database import get_session_factory
    from src.dependencies import get_current_user
    from src.main import create_app
    from src.models.user import User

    app = create_app()
    async with get_session_factory()() as s:
        admin = (await s.execute(
            select(User).where(User.user_role == "admin", User.access_status == "allowed").limit(1)
        )).scalars().first()

    async def _admin() -> User:
        return admin

    app.dependency_overrides[get_current_user] = _admin
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        for rid in run_ids:
            r = await c.get(f"/admin/simulation?run={rid}")
            sys.stdout.write(f"=====RUN {rid} STATUS {r.status_code}=====\n{r.text}\n")


asyncio.run(main(sys.argv[1:]))
