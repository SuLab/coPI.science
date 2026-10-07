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
    from src.routers.workspace import pi_profile
    from src.services import assessment_chat, profile_publish
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

    async def fake_pubmed_records(pmids, *, strict=False):
        """The review-card candidate route has no outbound dependency in the harness."""
        return [
            {"pmid": str(pmid), "title": "P3 accepted candidate paper",
             "abstract": "A fake PubMed abstract for the isolated UI audit.",
             "journal": "Audit Journal", "year": 2024, "pmcid": None, "doi": None}
            for pmid in pmids
        ]

    pi_profile.fetch_pubmed_records = fake_pubmed_records
    real_export = profile_publish.export_profile_to_markdown
    failed_slugs: set[str] = set()

    def fail_renamed_pi_c_export(user, profile, agent_id, *args, **kwargs):
        # Operator choice for J4-5: lifecycle export is attempted before activation, but its
        # isolated writer failure leaves the renamed pending lab behind the hard file gate.
        if agent_id == "p4-refusal-renamed" and agent_id not in failed_slugs:
            failed_slugs.add(agent_id)
            return None
        return real_export(user, profile, agent_id, *args, **kwargs)

    profile_publish.export_profile_to_markdown = fail_renamed_pi_c_export
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
