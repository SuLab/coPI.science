"""UI-audit seed: role users, PI with agent, interviews with adversarial text."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta

assert os.environ["DATABASE_URL"].endswith("/copi_e2e"), "scratch DB only"

XSS = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
EVIL_NAME = "O'Brien \"Q\" <b>bold</b> \\"
LONG = "https://example.org/" + "a" * 300
MD_EVIL = "[click](javascript:window.__xss=1) and <script>window.__xss=1</script> " + XSS


async def main():
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

    out = {}
    async with get_session_factory()() as s:
        admin = await factories.make_user(s, user_role=USER_ROLE_ADMIN, name="Audit Admin",
                                          orcid="0000-0002-1111-0001", email="admin@uiaudit.test")
        manager = await factories.make_user(s, user_role=USER_ROLE_MANAGER, name="Audit Manager",
                                            orcid="0000-0002-1111-0002", email="mgr@uiaudit.test")
        reviewer = await factories.make_user(s, user_role=USER_ROLE_REVIEWER, name="Audit Reviewer",
                                             orcid="0000-0002-1111-0003", email="rev@uiaudit.test")
        pi = await factories.make_user(s, user_role=USER_ROLE_PI, name=EVIL_NAME,
                                       orcid="0000-0002-1111-0004", email="pi@uiaudit.test",
                                       institution=XSS)
        pi2 = await factories.make_user(s, user_role=USER_ROLE_PI, name="Bert Vogelstein",
                                        orcid="0000-0002-1111-0005", email="bv@uiaudit.test")
        delegate = await factories.make_user(s, user_role=USER_ROLE_PI, name="Del Egate",
                                             orcid="0000-0002-1111-0006", email="del@uiaudit.test")
        pending = await factories.make_user(s, user_role=USER_ROLE_PI, name="Pending " + XSS,
                                            orcid="0000-0002-1111-0007", email="pend@uiaudit.test",
                                            access_status="pending", onboarding_complete=False)
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

        run = await factories.make_simulation_run(s, status="completed",
                                                  started_at=datetime.now(UTC) - timedelta(hours=3),
                                                  ended_at=datetime.now(UTC) - timedelta(hours=1))
        a1 = await seed_interview(s, run=run, subject="vogelstein", channel="interview-one")
        a2 = await seed_interview(
            s, run=run, subject="obrien", channel="interview-two",
            headline="EVIL " + XSS + " " + LONG,
            company_or_project=EVIL_NAME,
            rationale=MD_EVIL,
            red_flags=[XSS, LONG],
            strengths=[MD_EVIL],
            elevator_pitch=MD_EVIL + "\n" + LONG,
            key_points={"significance": [MD_EVIL, LONG], "innovation": [XSS]},
            recommended_next_experiment=MD_EVIL,
            recommendation="pass", band="pass", weighted_score=1.1,
        )
        a3 = await seed_interview(s, run=run, subject="vogelstein", channel="interview-three",
                                  with_messages=False, recommendation="advance", band="advance",
                                  weighted_score=4.6, headline="Strong one")
        for i in range(6):
            await factories.make_llm_call_log(s, run=run)
        for status in ("pending", "processing", "completed", "failed"):
            s.add(Job(type="generate_profile", status=status, user_id=(pi2.id if status == "processing" else pi.id),
                      payload={"note": XSS}, last_error=(XSS if status == "failed" else None)))
        await s.commit()
        out = dict(admin=str(admin.id), manager=str(manager.id), reviewer=str(reviewer.id),
                   pi=str(pi.id), pi2=str(pi2.id), delegate=str(delegate.id),
                   pending=str(pending.id), run=str(run.id),
                   assessments=[str(a1.assessment_id), str(a2.assessment_id),
                                str(a3.assessment_id)])
    await get_engine().dispose()
    json.dump(out, sys.stdout, indent=1)


asyncio.run(main())
