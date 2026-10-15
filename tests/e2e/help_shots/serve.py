"""Serve the app for the Help screenshots, with a benign fake model and stubbed outbound
fetchers (spec docs/specs/2026-10-14-help-and-onboarding-design.md §8.2). Run as
``python -m tests.e2e.help_shots.serve --port N`` inside the harness environment;
capture.py starts it with an outbound-recording proxy in its environment.

The stubs are the primary defence against a real outbound call; the proxy only makes a
missed one visible. Outbound calls reachable from a web route, read from the code:

* ``src.services.staff_accounts.fetch_orcid_profile``: the Staff page's Preview for an iD
  with no account (the ``staff-preview`` shot). Stubbed.
* ``src.routers.workspace.pi_profile.fetch_pubmed_records``: accepting a candidate paper.
  No shot posts it; stubbed anyway.
* ``src.routers.workspace.pi_evidence.profile_id_exists`` (NIH RePORTER): pinning a
  RePORTER profile. No shot posts it; stubbed anyway.
* ``src.routers.auth.fetch_orcid_profile``: the ORCID sign-in callback, which needs an
  ORCID token exchange first; no shot signs in (sessions are forged). Not stubbed.

OpenAlex, ORCID fundings, company discovery and profile generation run in worker jobs,
not in a web request; the harness runs no worker.
"""

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
    from src.routers.workspace import pi_evidence, pi_profile
    from src.services import assessment_chat, staff_accounts
    from tests.assessment_chat_support import citation
    from tests.e2e.help_shots.seed import (
        CHAT_ANSWER,
        CHAT_PITCH,
        STAFF_PREVIEW_INSTITUTION,
        STAFF_PREVIEW_NAME,
    )
    from tests.fakes import ChatScript, FakeAsyncAnthropic

    # Document 1 is the interview transcript; block 0 is the lab's pitch
    # (src/services/assessment_chat_record.py build_chat_record).
    fake = FakeAsyncAnthropic(
        [ChatScript(segments=[(CHAT_ANSWER, [citation(1, 0, CHAT_PITCH)])]) for _ in range(50)]
    )
    assessment_chat.get_async_anthropic_client = lambda: fake

    async def fake_orcid_profile(orcid_id: str) -> dict:
        return {
            "orcid": orcid_id,
            "name": STAFF_PREVIEW_NAME,
            "institution": STAFF_PREVIEW_INSTITUTION,
            "department": "Clinical Research Office",
            "email": None,
            "employments": [{"organization": STAFF_PREVIEW_INSTITUTION, "start_year": 2019,
                             "current": True}],
        }

    async def fake_pubmed_records(pmids, *, strict=False):
        return [
            {"pmid": str(pmid), "title": "A candidate paper found by name",
             "abstract": "Demo abstract.", "journal": "Example Journal", "year": 2025,
             "pmcid": None, "doi": None}
            for pmid in pmids
        ]

    async def fake_profile_id_exists(profile_id) -> bool:
        return True

    staff_accounts.fetch_orcid_profile = fake_orcid_profile
    pi_profile.fetch_pubmed_records = fake_pubmed_records
    pi_evidence.profile_id_exists = fake_profile_id_exists
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
