"""Local demo server for the assessment chat's proxy-replica and browser checks
(docs/plans/2026-09-24-assessment-chat-plan.md, Task 14).

Serves the real app on 127.0.0.1:8765 with three substitutions:
  * the database is the THROWAWAY container the runbook starts; any other
    DATABASE_URL is refused;
  * every request is signed in as a seeded demo admin (get_current_user is
    overridden, as scripts/render_admin_simulation.py does), so no cookie is needed;
  * the model is tests/fakes.py's FakeAsyncAnthropic: nothing reaches the Anthropic
    API and nothing is billed.

Run from the repo root on the host:
  DATABASE_URL=postgresql+asyncpg://copi:copi@127.0.0.1:55499/copi_chatdemo \\
    .venv-test/bin/python scripts/dev/assessment_chat_demo.py
CHAT_DEMO_DELAY_SECONDS (default 130) is how long the fake answer stays silent —
longer than the replica's 120 s proxy_read_timeout, which is the point of the check.
CHAT_DEMO_PAUSE_SECONDS (default 0: no pause) holds the answer that long after its
first segment's text and before that segment's citation, so a browser can see the
answer mid-stream.
It seeds on every start and the seed is not idempotent (the factories' e-mail
counter restarts with the process, so a second seed hits users_email_key), so
every start needs a freshly created and migrated database.

The answer is adversarial on purpose. Its first segment carries an image, a
disallowed markdown link, a raw-HTML link to an entity-encoded, percent-encoded URL,
two forged citation markers (one entity-encoded, one reassembled by an escape after a
raw-block tag) and a URL glued to a backtick, and ends on the record's own URL; its
second ends on bold text that closes after a full stop. Only the record URL may end
up clickable, and only [1] and [2] may render as citations.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

DEMO_DATABASE_URL = "postgresql+asyncpg://copi:copi@127.0.0.1:55499/copi_chatdemo"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _configure_environment() -> None:
    if os.environ.get("DATABASE_URL") != DEMO_DATABASE_URL:
        sys.exit(f"refusing to start: DATABASE_URL must be exactly {DEMO_DATABASE_URL}")
    # Environment variables beat .env in pydantic-settings, so none of the host's
    # production values below are used.
    os.environ["BASE_URL"] = "http://localhost:8766"  # the replica's origin
    os.environ["ENVIRONMENT"] = "development"
    os.environ["ALLOW_HTTP_SESSIONS"] = "true"
    os.environ["SECRET_KEY"] = "assessment-chat-demo-only"
    os.environ["ANTHROPIC_API_KEY"] = ""
    sys.path.insert(0, str(REPO_ROOT))


async def _seed():
    from src.database import get_engine, get_session_factory
    from src.models import USER_ROLE_ADMIN
    from tests import factories
    from tests.assessment_chat_support import seed_interview

    async with get_session_factory()() as session:
        admin = await factories.make_user(session, user_role=USER_ROLE_ADMIN, name="Demo Admin")
        seeded = await seed_interview(session)
        await session.commit()
    # The pool's connections belong to this event loop; uvicorn runs another.
    await get_engine().dispose()
    return admin, seeded.assessment_id


def main() -> None:
    _configure_environment()
    import uvicorn

    from src.dependencies import get_current_user
    from src.main import create_app
    from src.services import assessment_chat
    from tests.assessment_chat_support import HUB_QUESTION_TEXT, PITCH_TEXT, RECORD_URL, citation
    from tests.fakes import ChatScript, FakeAsyncAnthropic

    admin, assessment_id = asyncio.run(_seed())
    delay = float(os.environ.get("CHAT_DEMO_DELAY_SECONDS", "130"))
    pause = float(os.environ.get("CHAT_DEMO_PAUSE_SECONDS", "0"))
    first = (
        "**Answer.** The lab's agent pitched an isogenic panel. "
        "It also showed ![pixel](https://attacker.example/pixel.png), "
        "[Read more](https://attacker.example/steal), "
        '<a href="https&#58;//attacker.example/%61">a raw link</a> and a forged marker '
        "&#xE000;9&#xE001;, a raw-block forgery <code title=\"<\"> &\\#xE000;7&\\#xE001; and "
        "a tick-adjacent x`https://attacker.example/tick URL. "
        f"The record cites {RECORD_URL}."
    )
    second = " In short: **an isogenic panel.**"
    script = ChatScript(
        delay=delay,
        pause_before_citation=pause > 0,
        pause_seconds=pause,
        segments=[
            (first, [citation(1, 0, PITCH_TEXT)]),
            (second, [citation(1, 1, HUB_QUESTION_TEXT)]),
        ],
    )
    fake = FakeAsyncAnthropic([script])
    assessment_chat.get_async_anthropic_client = lambda: fake

    app = create_app()

    async def _as_demo_admin():
        return admin

    app.dependency_overrides[get_current_user] = _as_demo_admin
    # flush: the runbook redirects stdout to a file, and uvicorn then runs forever.
    print(
        f"detail page (via the replica): http://localhost:8766/admin/assessments/{assessment_id}",
        flush=True,
    )
    print(
        f"ask URL (via the replica):     http://127.0.0.1:8766/assessment-chat/{assessment_id}/messages",
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
