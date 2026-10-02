"""Serve the app for the harness with the fake model. Run as
``python -m tests.e2e.ui_audit.serve --port N`` inside the harness environment."""

from __future__ import annotations

import argparse

from tests.e2e.ui_audit.env import DB_NAME


def main() -> None:
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL", "").endswith("/" + DB_NAME):
        raise SystemExit(f"refusing to serve: DATABASE_URL must name {DB_NAME}")

    import uvicorn

    from src.main import create_app
    from src.services import assessment_chat
    from tests.assessment_chat_support import PITCH_TEXT, RECORD_URL, citation
    from tests.fakes import ChatScript, FakeAsyncAnthropic

    answer = (
        "**Answer.** <img src=x onerror=\"window.__xss=1\"> [js](javascript:window.__xss=1) "
        "![pixel](https://attacker.example/p.png) " + "x" * 200
        + f" The record cites {RECORD_URL}."
    )
    fake = FakeAsyncAnthropic(
        [ChatScript(delay=1.0, segments=[(answer, [citation(1, 0, PITCH_TEXT)])])
         for _ in range(200)]
    )
    assessment_chat.get_async_anthropic_client = lambda: fake
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
