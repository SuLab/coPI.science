"""Adversarial seed for the harness database. Run as
``python -m tests.e2e.ui_audit.seed`` inside the harness environment; prints the
seed ids as JSON on stdout.

The content is hostile on purpose (names that break JS strings, raw HTML, a form
that would promote a user, long unbroken URLs) and benign in effect: the database is
a throwaway container and no outbound path is enabled.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from tests.e2e.ui_audit.env import DB_NAME

XSS = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
EVIL_NAME = "O'Brien \"Q\" <b>bold</b> \\"
LONG = "https://example.org/" + "a" * 300
MD_EVIL = "[click](javascript:window.__xss=1) and <script>window.__xss=1</script> " + XSS
SCRIPT_NAME = "x'+(window.__xss=1)+'"
APOSTROPHE_NAME = "Mary O'Brien"


async def main() -> None:
    if not os.environ.get("DATABASE_URL", "").endswith("/" + DB_NAME):
        sys.exit(f"refusing to seed: DATABASE_URL must name {DB_NAME}")

    from src.database import get_engine, get_session_factory
    from src.models import (
        USER_ROLE_ADMIN,
        USER_ROLE_MANAGER,
        USER_ROLE_PI,
        USER_ROLE_REVIEWER,
        AgentDelegate,
        Job,
    )
    from tests import factories
    from tests.assessment_chat_support import seed_interview

    async with get_session_factory()() as s:
        mk = factories.make_user
        admin = await mk(s, user_role=USER_ROLE_ADMIN, name="Audit Admin",
                         orcid="0000-0002-1111-0001", email="admin@uiaudit.test")
        manager = await mk(s, user_role=USER_ROLE_MANAGER, name="Audit Manager",
                           orcid="0000-0002-1111-0002", email="mgr@uiaudit.test")
        reviewer = await mk(s, user_role=USER_ROLE_REVIEWER, name="Audit Reviewer",
                            orcid="0000-0002-1111-0003", email="rev@uiaudit.test")
        pi = await mk(s, user_role=USER_ROLE_PI, name=EVIL_NAME, orcid="0000-0002-1111-0004",
                      email="pi@uiaudit.test", institution=XSS)
        pi2 = await mk(s, user_role=USER_ROLE_PI, name="Bert Vogelstein",
                       orcid="0000-0002-1111-0005", email="bv@uiaudit.test")
        delegate = await mk(s, user_role=USER_ROLE_PI, name="Del Egate",
                            orcid="0000-0002-1111-0006", email="del@uiaudit.test")
        pending = await mk(s, user_role=USER_ROLE_PI, name="Pending " + XSS,
                           orcid="0000-0002-1111-0007", email="pend@uiaudit.test",
                           access_status="pending", onboarding_complete=False)
        script_user = await mk(s, name=SCRIPT_NAME, orcid="0000-0002-1111-0091",
                               email="x1@uiaudit.test")
        apostrophe_user = await mk(s, name=APOSTROPHE_NAME, orcid="0000-0002-1111-0092",
                                   email="x2@uiaudit.test")
        await factories.make_profile(s, user=pi, research_summary=MD_EVIL + "\n\n" + LONG,
                                     techniques=[XSS, "CRISPR"], keywords=[LONG, "k2"])
        await factories.make_profile(s, user=pi2)
        evil_agent = await factories.make_agent(s, user=pi, agent_id="obrien",
                                                bot_name="OBrienBot", pi_name=EVIL_NAME)
        await factories.make_agent(s, user=pi2, agent_id="vogelstein",
                                   bot_name="VogelsteinBot", pi_name="Bert Vogelstein")
        await factories.make_agent(s, agent_id="blackbird", bot_name="BlackbirdBot",
                                   pi_name="Blackbird", role="scout_hub")
        s.add(AgentDelegate(agent_registry_id=evil_agent.id, user_id=delegate.id))

        now = datetime.now(UTC)
        run = await factories.make_simulation_run(
            s, status="completed", started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=1))
        a1 = await seed_interview(s, run=run, subject="vogelstein", channel="interview-one")
        a2 = await seed_interview(
            s, run=run, subject="obrien", channel="interview-two",
            headline="EVIL " + XSS + " " + LONG, company_or_project=EVIL_NAME,
            rationale=MD_EVIL, red_flags=[XSS, LONG], strengths=[MD_EVIL],
            elevator_pitch=MD_EVIL + "\n" + LONG,
            key_points={"significance": [MD_EVIL, LONG], "innovation": [XSS]},
            recommended_next_experiment=MD_EVIL,
            recommendation="pass", band="pass", weighted_score=1.1,
        )
        # A stopped, never-finalized run: the Finalize journeys (Phase 1) need one.
        stopped = await factories.make_simulation_run(
            s, status="stopped", started_at=now - timedelta(hours=6),
            ended_at=now - timedelta(hours=5))
        a3 = await seed_interview(s, run=run, subject="vogelstein", channel="interview-three",
                                  with_messages=False, recommendation="advance",
                                  band="advance", weighted_score=4.6, headline="Strong one")
        # A long hub reply (timeline clamp) and a raw-HTML form that would promote pi2.
        long_reply = ("Paragraph of a very long hub reply. " * 40 + "\n\n") * 6
        injected = (
            "Thanks for the question.\n\n"
            f'<form method="post" action="/admin/users/{pi2.id}/role">'
            '<input type="hidden" name="user_role" value="admin">'
            '<button id="injected-btn" style="position:fixed;inset:0;opacity:0.01;'
            'z-index:9999">x</button></form>\n\n'
            "Inline <b>bold</b> and a lone line<br>break."
        )
        for agent_id, content in (("blackbird", long_reply), ("obrien", injected)):
            await factories.make_agent_message(
                s, run=run, agent_id=agent_id, channel_name="interview-two",
                message_ts=f"{time.time():.6f}", thread_ts=a2.root_ts,
                phase="thread_reply", content=content, posted_at=time.time())
        for _ in range(6):
            await factories.make_llm_call_log(s, run=run)
        for status, owner in (("pending", pi), ("processing", pi2), ("completed", pi),
                              ("failed", pi)):
            s.add(Job(type="generate_profile", status=status, user_id=owner.id,
                      payload={"note": XSS},
                      last_error=XSS if status == "failed" else None))
        await s.commit()
        ids = {
            "admin": str(admin.id), "manager": str(manager.id), "reviewer": str(reviewer.id),
            "pi": str(pi.id), "pi2": str(pi2.id), "delegate": str(delegate.id),
            "pending": str(pending.id), "script_user": str(script_user.id),
            "apostrophe_user": str(apostrophe_user.id), "run": str(run.id),
            "stopped_run": str(stopped.id),
            "agent_obrien": str(evil_agent.id),
            "assessments": [str(a1.assessment_id), str(a2.assessment_id),
                            str(a3.assessment_id)],
            "a2_root_ts": a2.root_ts,
            "bad_uuid": str(uuid.UUID(int=0)),
        }
    await get_engine().dispose()
    print(json.dumps(ids))


if __name__ == "__main__":
    asyncio.run(main())
