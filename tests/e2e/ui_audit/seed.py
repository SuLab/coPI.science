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

#: Phase 3 (spec 2026-10-02 §5.2, §6.1, §7.2, §7.4): names the journeys look for.
P3_CONFIRMED_COMPANY = "Helix Bio Inc"
P3_SUGGESTED_COMPANY = "Nimbus Therapeutics"
#: A second suggestion, rejected by journey_companies_card (the Reject button).
P3_REJECTED_COMPANY = "Orbit Labs"
P3_GATING = {"life_sciences_domain": "met", "credible_science": "not_met",
             "translational_potential": "unconfirmed"}
P3_GATING_RATIONALES = {
    "life_sciences_domain": "The pitch names a small-molecule therapeutic for fibrosis.",
    "credible_science": "The lab could not say how the in vivo efficacy claim was measured.",
    "translational_potential": "Never asked: the interview ended before the program came up.",
}
P3_RISK_BODY = "the lab's prior IP was licensed to a partner; the license terms would resolve it."
P3_COMPANIES_BODY = "a 2021 funding statement names Acme Ventures as a sponsor of one cohort."


async def _seed_phase3(s, *, run, now: datetime) -> dict:
    """Phase 3 rows: a PI with an agent, two live-rubric (3.5.0) verdicts on it (one with
    `gating_rationales` and labelled key-point extras, one without either), one confirmed
    and one suggested `PiCompany` for that PI, and one prompt suggestion. Returns the ids
    under ``p3_*`` keys."""
    import hashlib

    from src.models import USER_ROLE_PI
    from src.models.pi_company import PiCompany
    from src.models.review import PromptChangeSuggestion
    from src.services.pi_companies import normalize_company_name
    from tests import factories
    from tests.assessment_chat_support import seed_interview
    from tests.e2e.ui_audit.env import REPO_ROOT

    def sha12(rel: str) -> str:
        return hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()[:12]

    rubric_hash = sha12("prompts/rubric/blackbird-rubric.toml")
    founder = await factories.make_user(
        s, user_role=USER_ROLE_PI, name="Carla Founder", orcid="0000-0002-1111-0008",
        email="founder@uiaudit.test")
    await factories.make_profile(s, user=founder)
    await factories.make_agent(s, user=founder, agent_id="founder", bot_name="FounderBot",
                               pi_name="Carla Founder")
    key_points = {
        "indication_audience": ["Idiopathic pulmonary fibrosis patients failing standard care."],
        "lab_background": ["Ten years of fibroblast signalling work.",
                           "Companies: " + P3_COMPANIES_BODY],
        "proposal": ["Advance the lead compound into a two-species tox package."],
        "clinical_actionability": ["A lung-function endpoint is accepted by regulators."],
        "path_to_clinic": ["IND-enabling studies within eighteen months."],
        "commercial_opportunity": ["A first-in-class antifibrotic with orphan pricing.",
                                   "Risk: " + P3_RISK_BODY],
    }
    gated = await seed_interview(
        s, run=run, subject="founder", channel="interview-p3-gates", with_messages=False,
        headline="P3 gate reasons and key-point labels", company_or_project="Fibrosis Co",
        gating=dict(P3_GATING), gating_rationales=dict(P3_GATING_RATIONALES),
        key_points=key_points, rubric_version="3.5.0", rubric_content_hash=rubric_hash)
    ungated = await seed_interview(
        s, run=run, subject="founder", channel="interview-p3-definitions", with_messages=False,
        headline="P3 gate definitions only", company_or_project="Fibrosis Co",
        gating=dict(P3_GATING), gating_rationales=None,
        rubric_version="3.5.0", rubric_content_hash=rubric_hash)
    confirmed = PiCompany(
        user_id=founder.id, company_name=P3_CONFIRMED_COMPANY,
        normalized_name=normalize_company_name(P3_CONFIRMED_COMPANY), pi_role="founder",
        funding_usd=12_000_000, funding_as_of=(now - timedelta(days=30)).date(),
        source_url="https://example.org/helix-bio/about", status="confirmed", origin="manual",
        created_by_user_id=None, reviewed_by_user_id=None, reviewed_at=now)
    suggested = PiCompany(
        user_id=founder.id, company_name=P3_SUGGESTED_COMPANY,
        normalized_name=normalize_company_name(P3_SUGGESTED_COMPANY), pi_role="co_founder",
        funding_usd=4_500_000, funding_as_of=(now - timedelta(days=90)).date(),
        source_url="https://pubmed.ncbi.nlm.nih.gov/31234567/",
        funding_source_url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001234567",
        status="suggested", origin="discovered",
        evidence={
            "coi": [{"pmid": "31234567", "year": "2020",
                     "sentence": "C.F. is a co-founder of Nimbus Therapeutics.",
                     "former": False, "pi_role": "co_founder",
                     "company_name": P3_SUGGESTED_COMPANY,
                     "url": "https://pubmed.ncbi.nlm.nih.gov/31234567/"}],
            "wikidata": [],
            "form_d": {
                "status": "ok", "funding_usd": 4_500_000,
                "funding_as_of": (now - timedelta(days=90)).date().isoformat(),
                "filings_page": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001234567",
                "not_fetched": 0,
                "filings": [{
                    "accession": "0001234567-24-000001",
                    "filing_date": (now - timedelta(days=90)).date().isoformat(),
                    "form": "D", "cik": "0001234567", "file_num": "021-400001",
                    "issuer_name": "NIMBUS THERAPEUTICS, INC.", "is_amendment": False,
                    "previous_accession": None, "total_offering_amount": 10_000_000,
                    "total_offering_amount_raw": "10000000", "total_amount_sold": 4_500_000,
                    "total_amount_sold_raw": "4500000", "amount_note": None,
                    "pi_listed": True, "pi_relationships": ["Director"],
                    "url": "https://www.sec.gov/Archives/edgar/data/1234567/000123456724000001/",
                    "counted": True,
                }],
            },
            "former": False,
        })
    to_reject = PiCompany(
        user_id=founder.id, company_name=P3_REJECTED_COMPANY,
        normalized_name=normalize_company_name(P3_REJECTED_COMPANY), pi_role="founder",
        funding_usd=None, funding_as_of=None,
        source_url="https://pubmed.ncbi.nlm.nih.gov/31234568/",
        status="suggested", origin="discovered",
        evidence={
            "coi": [{"pmid": "31234568", "year": "2019",
                     "sentence": "C.F. founded Orbit Labs.", "former": False,
                     "pi_role": "founder", "company_name": P3_REJECTED_COMPANY,
                     "url": "https://pubmed.ncbi.nlm.nih.gov/31234568/"}],
            "wikidata": [],
            "form_d": {"status": "unavailable", "funding_usd": None, "funding_as_of": None,
                       "filings_page": None, "not_fetched": 0, "filings": [],
                       "note": "funding lookup unavailable", "reason": "seeded"},
            "former": False,
        })
    s.add_all([confirmed, suggested, to_reject])
    prompt_file = "prompts/roles/scout_hub/agent-system.md"
    suggestion = PromptChangeSuggestion(
        subject_label="Carla Founder — Fibrosis Co", feedback_snapshot=[], target="scout_hub",
        prompt_files=[{"path": prompt_file, "sha256_12": sha12(prompt_file)}],
        suggestion="Ask how each in vivo claim was measured.", transcript_available=False)
    s.add(suggestion)
    await s.flush()
    return {
        "p3_pi": str(founder.id),
        "p3_assessment_gated": str(gated.assessment_id),
        "p3_assessment_ungated": str(ungated.assessment_id),
        "p3_company_confirmed": str(confirmed.id),
        "p3_company_suggested": str(suggested.id),
        "p3_company_to_reject": str(to_reject.id),
        "p3_prompt_suggestion": str(suggestion.id),
    }


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
        phase3 = await _seed_phase3(s, run=run, now=now)
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
            # Phase 3 (hub 1.10.0) rows, under their own keys: `assessments` stays the
            # three rows crawl.routes unpacks.
            **phase3,
        }
    await get_engine().dispose()
    print(json.dumps(ids))


if __name__ == "__main__":
    asyncio.run(main())
