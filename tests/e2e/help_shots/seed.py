"""Demo seed for the Help screenshots (spec
docs/specs/2026-10-14-help-and-onboarding-design.md §8.1). Run as
``python -m tests.e2e.help_shots.seed`` inside the harness environment (capture.py does);
prints the seed ids as JSON on the last stdout line.

Every person, lab and company is invented. ORCID iDs carry a deliberately WRONG ISO 7064
MOD 11-2 check digit, so none can ever be issued to anyone; the seed can store them because
the factories insert directly and ``validate_orcid`` checks the format only. Emails are
``@example.org``; PMIDs, RePORTER ids and grant numbers are made up.

Importing this module touches nothing in ``src``: tests/unit/test_help_shots.py and
shots.py read its constants (``ACCOUNT_ROLES``, the chat texts), and the database guard
lives in ``main()``.
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

#: Seed key -> ``users.user_role`` of every account a shot may be captured as. Shots of
#: audience ``all`` use a ``reviewer`` account and ``staff`` shots a ``manager`` one
#: (tests/unit/test_help_shots.py checks it against this dict).
ACCOUNT_ROLES: dict[str, str] = {
    "admin": "admin",
    "manager": "manager",
    "reviewer": "reviewer",
    # No help_intro_dismissals row: the only account that shows the first-sign-in prompt.
    "new_reviewer": "reviewer",
}

#: Seed key -> (invented name, the number the key's ORCID iD is built from).
PEOPLE: dict[str, tuple[str, int]] = {
    "admin": ("Avery Quillfeather", 101),
    "manager": ("Morgan Ellery", 102),
    "reviewer": ("Riley Marchetti", 103),
    "new_reviewer": ("Noa Brightwater", 104),
    "staff_pending": ("Parker Lindqvist", 105),
    "staff_revoked": ("Sasha Orrin", 106),
    "pi_featured": ("Dana Whitcombe", 201),
    "pi_pending_token": ("Lee Farrow", 202),
    "pi_pending": ("Jordan Halvorsen", 203),
    "pi_generating": ("Robin Ashdown", 204),
    "pi_no_profile": ("Casey Wrenfield", 205),
    "pi_muted": ("Elliot Fenwick", 206),
    "pi_draft": ("Kai Morrow", 207),
}

#: The person the stubbed ORCID lookup describes for the Staff page's Preview
#: (tests/e2e/help_shots/serve.py). No account holds this iD.
STAFF_PREVIEW_NAME = "Quinn Harrowgate"
STAFF_PREVIEW_INSTITUTION = "Example Institute of Health"

#: The assessment-chat demo (serve.py scripts the answer; shots.py asks the question).
CHAT_QUESTION = "What would the first experiment need to show?"
#: Plain text inside CHAT_ANSWER that markdown leaves unchanged; shots.py waits for it.
CHAT_ANSWER_MARKER = "a blinded mouse study"
CHAT_ANSWER = (
    "**In short.** The lab proposes " + CHAT_ANSWER_MARKER + " in which the two lead "
    "compounds reverse fibroblast activation faster than the standard of care. The pitch "
    "describes the imaging assay that would read it out."
)
#: The interview's first message; the scripted answer cites it (document 1, block 0).
CHAT_PITCH = (
    "Our lab images fibroblast activation in living lung slices and has ranked forty "
    "approved drugs by how fast they reverse it; two outperform the standard of care in "
    "mouse models."
)


def _check_digit(base15: str) -> str:
    """The ISO 7064 MOD 11-2 check character for 15 digits: the algorithm
    ``src.services.pubmed._normalize_author_orcid`` validates with."""
    total = 0
    for ch in base15:
        total = (total + int(ch)) * 2
    result = (12 - total % 11) % 11
    return "X" if result == 10 else str(result)


def _bad_checksum_orcid(n: int) -> str:
    """``0000-0002-…`` with a check digit that is never the right one."""
    base = f"00000002{n:07d}"
    right = _check_digit(base)
    wrong = "0" if right == "X" else str((int(right) + 1) % 10)
    digits = base + wrong
    return "-".join(digits[i:i + 4] for i in range(0, 16, 4))


SEED_ORCIDS: dict[str, str] = {key: _bad_checksum_orcid(n) for key, (_, n) in PEOPLE.items()}
STAFF_PREVIEW_ORCID = _bad_checksum_orcid(901)

assert all(
    _check_digit(orcid.replace("-", "")[:15]) != orcid[-1]
    for orcid in (*SEED_ORCIDS.values(), STAFF_PREVIEW_ORCID)
), "every seeded ORCID iD must fail the MOD 11-2 check"


def _email(key: str) -> str:
    return PEOPLE[key][0].lower().replace(" ", ".") + "@example.org"


_KEY_POINTS = {
    "indication_audience": ["Adults with idiopathic pulmonary fibrosis whose lung function "
                            "keeps falling on standard care."],
    "lab_background": ["Ten years of live lung-slice imaging and a published screen of "
                       "approved drugs."],
    "proposal": ["Validate the two strongest compounds in a second fibrosis model, then "
                 "start a dose-finding study."],
    "clinical_actionability": ["Both compounds are already approved for other uses, so "
                               "safety data exist."],
    "path_to_clinic": ["A repurposing route could reach a first patient study in about "
                       "three years."],
    "commercial_opportunity": ["A new formulation could carry its own patent protection."],
}


async def _interview(s, *, run, subject: str, channel: str, pitch: str, reply: str,
                     consults: tuple[tuple[str, str, str], ...], **verdict):
    """One interview thread (pitch, hub question, lab reply, hub verdict), the specialist
    consults, and the stored verdict; ``verdict`` sets assessment columns. Modelled on
    tests/assessment_chat_support.py::seed_interview with demo text."""
    from src.models import OpportunityAssessment, SpecialistConsult
    from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
    from tests import factories

    base = time.time() - 7200
    root_ts = f"{base:.6f}"
    verdict_ts = f"{base + 180:.6f}"
    for agent_id, ts, thread_ts, phase, content, at in (
        (subject, root_ts, None, "new_post", ":bulb: @BlackbirdBot — " + pitch, base),
        ("blackbird", f"{base + 60:.6f}", root_ts, "thread_reply",
         "Which measurement would convince a sceptical clinician first?", base + 60),
        (subject, f"{base + 120:.6f}", root_ts, "thread_reply", reply, base + 120),
        ("blackbird", verdict_ts, root_ts, "thread_reply",
         "Thank you. I have written up my assessment of this proposal.", base + 180),
    ):
        await factories.make_agent_message(
            s, run=run, agent_id=agent_id, channel_name=channel, message_ts=ts,
            thread_ts=thread_ts, phase=phase, content=content, posted_at=at)
    for i, (domain, signal, concern) in enumerate(consults):
        s.add(SpecialistConsult(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id=subject,
            thread_id=root_ts, channel_name=channel, domain=domain,
            question=f"What would a {domain} reviewer check first?",
            verdict_signal=signal, confidence="moderate", concerns=[concern],
            questions_to_ask=["Which result would change your mind?"],
            raw_opinion="Demo specialist opinion.", established=["The assay is reproducible."],
            read_state="parsed", created_at=datetime.fromtimestamp(base + 90 + i, UTC)))
    fields = dict(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id=subject,
        channel_name=channel, slack_ts=verdict_ts, thread_id=root_ts, panel_owed=True,
        rubric_version=RUBRIC_VERSION, rubric_content_hash=RUBRIC_CONTENT_HASH,
        raw_verdict={"note": "demo verdict"})
    fields.update(verdict)
    assessment = OpportunityAssessment(**fields)
    s.add(assessment)
    await s.flush()
    return assessment


def _scored(scores: dict[str, int]) -> dict:
    """The score columns for ``scores``: the weighted score and band the app computes."""
    from src.services.blackbird_rubric import band, weighted_score

    value = weighted_score(scores)
    banded = band(value)
    return {"scores": scores, "weighted_score": value, "band": banded,
            "recommendation": banded}


async def _seed_pis(s, users: dict, *, now: datetime) -> dict:
    """Every profile and agent state, and the fully populated PI. Returns ids."""
    from src.models import Job, PiIndustryEvidence, PiIndustryScore, Publication
    from src.models.enrichment import PiGrant, PiGrantIdentity, PiOrcidFunding
    from src.models.pi_company import PiCompany
    from src.models.publication import PublicationCandidate
    from src.services.industry_score import SCORER_VERSION
    from src.services.jhu_rules import set_tenure_start
    from src.services.pi_companies import normalize_company_name
    from tests import factories

    def profile_fields(summary: str, **extra) -> dict:
        return dict(research_summary=summary, profile_version=2, evidence_pub_count=3,
                    evidence_pmid_count=3, **extra)

    featured = users["pi_featured"]
    await factories.make_profile(
        s, user=featured, **profile_fields(
            "Studies how lung fibroblasts switch into scar-forming cells, using live imaging "
            "of lung slices to test drugs that reverse the switch.",
            techniques=["Live lung-slice imaging", "Single-cell RNA sequencing"],
            disease_areas=["Pulmonary fibrosis"], experimental_models=["Mouse", "Human lung slices"],
            key_targets=["Fibroblast activation"], keywords=["fibrosis", "imaging", "repurposing"],
            pending_profile={
                "fields": {"research_summary": "Adds the lab's new work on scar resolution.",
                           "techniques": ["Live lung-slice imaging"], "experimental_models": None,
                           "disease_areas": None, "key_targets": None, "keywords": None},
                "synthesis_validated": True, "evidence_pmid_count": 3, "evidence_pub_count": 3,
                "evidence_flagged_count": 0, "base_profile_version": 2, "job_id": None,
            },
            pending_profile_created_at=now - timedelta(days=1)))
    await factories.make_agent(s, user=featured, agent_id="whitcombe", bot_name="WhitcombeBot",
                               pi_name=PEOPLE["pi_featured"][0], status="active")
    await set_tenure_start(featured.id, 2016, "manual", db=s)

    farrow = users["pi_pending_token"]
    await factories.make_profile(s, user=farrow, **profile_fields(
        "Maps how airway cells repair after viral injury.", techniques=["Organoids"]))
    await factories.make_agent(s, user=farrow, agent_id="farrow", bot_name="FarrowBot",
                               pi_name=PEOPLE["pi_pending_token"][0], status="pending",
                               slack_bot_token="xoxb-helpdemo-not-a-real-token")

    halvorsen = users["pi_pending"]
    await factories.make_profile(s, user=halvorsen, **profile_fields(
        "Builds imaging probes for inflamed tissue.", techniques=["Probe chemistry"]))
    await factories.make_agent(s, user=halvorsen, agent_id="halvorsen", bot_name="HalvorsenBot",
                               pi_name=PEOPLE["pi_pending"][0], status="pending")

    ashdown = users["pi_generating"]
    await factories.make_agent(s, user=ashdown, agent_id="ashdown", bot_name="AshdownBot",
                               pi_name=PEOPLE["pi_generating"][0], status="pending")
    s.add(Job(type="generate_profile", status="processing", user_id=ashdown.id,
              payload={"note": "demo"}))

    fenwick = users["pi_muted"]
    await factories.make_profile(s, user=fenwick, **profile_fields(
        "Studies immune cells that keep scars from resolving.", techniques=["Flow cytometry"]))
    await factories.make_agent(s, user=fenwick, agent_id="fenwick", bot_name="FenwickBot",
                               pi_name=PEOPLE["pi_muted"][0], status="inactive",
                               muted_at=now - timedelta(days=2), muted_by=users["manager"].id)

    morrow = users["pi_draft"]
    await factories.make_profile(s, user=morrow, **profile_fields(
        "Designs wearable sensors for breathing.", techniques=["Sensor design"],
        pending_profile={
            "fields": {"research_summary": "Adds the lab's sleep-study sensors.",
                       "techniques": None, "experimental_models": None, "disease_areas": None,
                       "key_targets": None, "keywords": None},
            "synthesis_validated": True, "evidence_pmid_count": 3, "evidence_pub_count": 3,
            "evidence_flagged_count": 0, "base_profile_version": 2, "job_id": None,
        }, pending_profile_created_at=now - timedelta(hours=5)))
    await factories.make_agent(s, user=morrow, agent_id="morrow", bot_name="MorrowBot",
                               pi_name=PEOPLE["pi_draft"][0], status="active")

    # The fully populated PI: papers in every review state, grants, fundings, industry
    # evidence, companies and jobs.
    s.add_all([
        Publication(user_id=featured.id, pmid="90000011", title="Live imaging of fibroblast "
                    "activation in lung slices", abstract="Demo abstract.", journal="Example "
                    "Journal of Lung Biology", year=2022, provenance="s1"),
        Publication(user_id=featured.id, pmid="90000012", title="A drug screen for scar "
                    "reversal", abstract="Demo abstract.", journal="Example Fibrosis Reports",
                    year=2024, provenance="s1"),
        Publication(user_id=featured.id, pmid="90000013", title="A paper with no ORCID link "
                    "to this PI", abstract="Demo abstract.", journal="Example Journal",
                    year=2023, provenance="unanchored"),
        Publication(user_id=featured.id, pmid="90000014", title="A paper by a different "
                    "D. Whitcombe", abstract="Demo abstract.", journal="Example Journal",
                    year=2019, provenance="s1", excluded_at=now - timedelta(days=3),
                    excluded_by_user_id=users["manager"].id),
        PublicationCandidate(user_id=featured.id, pmid="90000015", title="A candidate paper "
                             "found by name", year=2025, stages="s4", reason="no_orcid_anchor",
                             status="pending"),
    ])
    s.add(PiGrantIdentity(
        user_id=featured.id, status="resolved", accepted_profile_ids=[90000001],
        candidates=[{"id": 90000001, "name_on_award": "WHITCOMBE, DANA", "linked": True,
                     "linking_pmids": ["90000011"]}],
        evaluated_at=now - timedelta(days=4)))
    evidence = {"rule": "pmid_link", "matched_profile_id": 90000001,
                "name_on_award": "WHITCOMBE, DANA", "linking_pmids": ["90000011"]}
    s.add_all([
        PiGrant(user_id=featured.id, core_project_num="R01ZZ900001", reporter_profile_id=90000001,
                title="Imaging the switch from repair to scarring in the lung",
                activity_code="R01", agency_ic="ZZ", org_name="Example University",
                first_fy=2022, last_fy=now.year + 1, total_award_in_tenure=1_800_000,
                is_contact_pi=True, tenure_filter_mode="org_and_year",
                identity_evidence=evidence),
        PiGrant(user_id=featured.id, core_project_num="R21ZZ900002", reporter_profile_id=90000001,
                title="A pilot screen of approved drugs in lung slices", activity_code="R21",
                agency_ic="ZZ", org_name="Example University", first_fy=2018, last_fy=2020,
                total_award_in_tenure=400_000, is_contact_pi=True,
                tenure_filter_mode="org_and_year", identity_evidence=evidence),
        PiOrcidFunding(user_id=featured.id, group_key="demo-funding-1",
                       title="Lung repair fellowship", funder_name="Example Lung Foundation",
                       funding_type="grant", start_year=2021, end_year=2024),
        PiIndustryEvidence(user_id=featured.id, source="openalex", kind="coauthor_company",
                           external_id="W900000001", company_name="Northwind Biologics",
                           company_class="pharma_biotech", year=2023, pi_role="last",
                           in_tenure=True, evidence={"title": "A co-authored imaging study"}),
        PiIndustryEvidence(user_id=featured.id, source="uspto", kind="patent_assigned",
                           external_id="US90000001", company_name="Lumen Fibrosis Inc",
                           company_class="pharma_biotech", year=2024, pi_role="inventor",
                           in_tenure=True, evidence={"title": "Formulations for scar reversal"}),
        PiCompany(user_id=featured.id, company_name="Lumen Fibrosis Inc",
                  normalized_name=normalize_company_name("Lumen Fibrosis Inc"),
                  pi_role="co_founder", funding_usd=3_000_000,
                  funding_as_of=(now - timedelta(days=60)).date(),
                  source_url="https://example.org/lumen-fibrosis/team", status="confirmed",
                  origin="manual", reviewed_by_user_id=users["manager"].id, reviewed_at=now),
        PiCompany(user_id=featured.id, company_name="Northwind Biologics",
                  normalized_name=normalize_company_name("Northwind Biologics"),
                  pi_role="advisor", source_url="https://example.org/papers/90000016",
                  status="suggested", origin="discovered",
                  evidence={
                      "coi": [{"pmid": "90000016", "year": "2023",
                               "sentence": "D.W. is a scientific advisor to Northwind Biologics.",
                               "former": False, "pi_role": "advisor",
                               "company_name": "Northwind Biologics",
                               "url": "https://example.org/papers/90000016"}],
                      "wikidata": [],
                      "form_d": {"status": "no_filings", "funding_usd": None,
                                 "funding_as_of": None, "filings_page": None, "not_fetched": 0,
                                 "filings": [], "note": None},
                      "former": False,
                  }),
    ])
    for job_type in ("generate_profile", "enrich_grants", "industry_evidence"):
        s.add(Job(type=job_type, status="completed", user_id=featured.id,
                  payload={"note": "demo"}))
    # Five scored PIs, so the industry percentile renders instead of "unscored".
    for user, raw in ((featured, 6.5), (farrow, 1.0), (halvorsen, 2.5), (fenwick, 3.0),
                      (morrow, 0.5)):
        s.add(PiIndustryScore(
            user_id=user.id, raw_sum=raw, evidence_count=2, tenure_start_used=2016,
            scorer_version=SCORER_VERSION,
            components={"coauthor_company": {"distinct_companies": 1, "weighted": raw / 2},
                        "patent_assigned": {"distinct_companies": 1, "weighted": raw / 2}},
            coverage={"openalex": "ok", "uspto": "ok"}))
    await s.flush()
    return {"pi_featured": str(featured.id), "pi_pending": str(halvorsen.id),
            "pi_with_profiles": [str(u.id) for u in (featured, farrow, halvorsen, fenwick,
                                                     morrow)]}


async def _seed_run(s, users: dict, *, now: datetime) -> dict:
    """A completed run: three assessments (every detail section filled on the first),
    feedback in both modes, an assignment, discussion threads and one prompt suggestion."""
    import hashlib
    import tomllib
    from pathlib import Path

    from sqlalchemy import func, select

    from src.models import AgentMessage
    from src.models.review import (
        AssessmentReview,
        AssessmentReviewAssignment,
        PromptChangeSuggestion,
    )
    from src.services.assessment_detail import KEY_POINT_GROUPS
    from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION, RUBRIC_WEIGHTS
    from tests import factories

    gates = list(tomllib.loads(Path("prompts/rubric/blackbird-rubric.toml").read_text())["gating"])
    dims = list(RUBRIC_WEIGHTS)

    def scores(*values: int) -> dict[str, int]:
        return {key: values[i % len(values)] for i, key in enumerate(dims)}

    run = await factories.make_simulation_run(
        s, status="completed", started_at=now - timedelta(hours=4),
        ended_at=now - timedelta(hours=1),
        config={"rubric_version": RUBRIC_VERSION, "max_runtime": 180, "budget_cap": 0})
    await factories.make_agent(s, agent_id="blackbird", bot_name="BlackbirdBot",
                               pi_name="Blackbird", role="scout_hub")

    main = await _interview(
        s, run=run, subject="whitcombe", channel="interview-whitcombe", pitch=CHAT_PITCH,
        reply="A blinded study in a second mouse model, read out by our imaging assay.",
        consults=(("clinical", "adequate", "The patient group is well defined."),
                  ("commercial", "gap", "Both compounds are licensed to other companies."),
                  ("scientific", "adequate", "The assay has been reproduced in two labs.")),
        company_or_project="Lumen Fibrosis Program",
        headline="A live-imaging assay that ranks approved drugs by how well they reverse "
                 "lung scarring",
        confidence="Moderate", elevator_pitch=CHAT_PITCH,
        key_points={key: _KEY_POINTS.get(key, [f"{label}: summarised from the interview."])
                    for key, label in KEY_POINT_GROUPS},
        score_rationale="Strong science and a short path to patients; the licensing question "
                        "is open.",
        dimension_rationales={key: "Supported by the interview and the lab's papers."
                              for key in dims},
        gating={key: "met" for key in gates},
        gating_rationales={key: "Established in the interview." for key in gates},
        strengths=["A validated assay with published results."],
        risks=["Both lead compounds are licensed to other companies for other uses."],
        competitive_landscape=["Two companies screen antifibrotics in cell culture only."],
        evidence_maturity=["Mouse data in one model; a second model is planned."],
        red_flags=["Freedom to operate for the lead compounds has not been checked."],
        rationale="The lab has a working assay and a ranked list of candidates.\n\n"
                  "The main open question is whether the lead compounds can be licensed.",
        recommended_next_experiment="A blinded study of the two lead compounds in a second "
                                    "mouse model of lung fibrosis.",
        **_scored(scores(4, 5, 4, 3, 4, 4)))
    second = await _interview(
        s, run=run, subject="fenwick", channel="interview-fenwick",
        pitch="We can tell which immune cells keep a scar from resolving.",
        reply="We would sort those cells from patient samples first.",
        consults=(("clinical", "adequate", "Patient samples are available."),),
        company_or_project="Scar Immunity Panel",
        headline="An immune-cell panel that predicts which lung scars will not resolve",
        confidence="Low", elevator_pitch="A panel of immune markers for scar resolution.",
        gating={**{key: "met" for key in gates}, gates[-1]: "unconfirmed"},
        red_flags=[], rationale="Promising but early.",
        **_scored(scores(3, 3, 2, 3, 3, 3)))
    third = await _interview(
        s, run=run, subject="morrow", channel="interview-morrow",
        pitch="Our wearable sensor tracks breathing during sleep.",
        reply="We have tested it on ten volunteers.",
        consults=(("commercial", "gap", "Several similar devices are on sale."),),
        company_or_project="Night Breath Sensor",
        headline="A wearable breathing sensor for sleep studies",
        confidence="Moderate", elevator_pitch="A wearable sensor for breathing at night.",
        gating={**{key: "met" for key in gates}, gates[1]: "not_met"},
        red_flags=["Similar devices already exist."], rationale="Not distinct enough.",
        **_scored(scores(2, 2, 1, 2, 2, 2)))

    reviewer, manager = users["reviewer"], users["manager"]
    learn = AssessmentReview(
        assessment_id=main.id, reviewer_user_id=manager.id, reviewer_name=manager.name,
        score=4, dimension_scores={dims[0]: 4, dims[1]: 5}, rubric_version=RUBRIC_VERSION,
        rubric_content_hash=RUBRIC_CONTENT_HASH,
        comment="Convincing assay. Ask how the compounds would be licensed.",
        feedback_mode="learn")
    log_only = AssessmentReview(
        assessment_id=second.id, reviewer_user_id=reviewer.id, reviewer_name=reviewer.name,
        score=3, rubric_version=RUBRIC_VERSION, rubric_content_hash=RUBRIC_CONTENT_HASH,
        comment="Interesting, but the patient samples are not described.",
        feedback_mode="log_only")
    s.add_all([learn, log_only, AssessmentReviewAssignment(
        assessment_id=main.id, assignee_user_id=reviewer.id, assignee_name=reviewer.name,
        assigned_by_user_id=manager.id, assigned_by_name=manager.name)])
    await s.flush()

    # Discussions: two threads between labs, one ending in a proposal.
    base = time.time() - 9000
    threads = (
        ("whitcombe", "morrow", "lung-imaging", "proposal",
         "Combine the lung-slice assay with wearable breathing data in one pilot.",
         ("We are looking for a partner with patient breathing data.",
          "We have that data and would like to compare notes.",
          "Let's write up a joint pilot.")),
        ("fenwick", "farrow", "airway-repair", "no_proposal", None,
         ("Has anyone sorted immune cells from airway samples?",
          "Only from organoids so far, not from patients.",
          "Then our questions do not overlap yet; thanks.")),
    )
    for i, (poster, replier, channel, outcome, summary, texts) in enumerate(threads):
        root = f"{base + i * 600:.6f}"
        for j, text in enumerate(texts):
            await factories.make_agent_message(
                s, run=run, agent_id=replier if j == 1 else poster, channel_name=channel,
                message_ts=root if j == 0 else f"{base + i * 600 + 60 * j:.6f}",
                thread_ts=None if j == 0 else root,
                phase="new_post" if j == 0 else "thread_reply", content=text,
                posted_at=base + i * 600 + 60 * j)
        await factories.make_thread_decision(
            s, run=run, thread_id=root, channel=channel, agent_a=poster, agent_b=replier,
            outcome=outcome, summary_text=summary)
    for _ in range(3):
        await factories.make_llm_call_log(s, run=run, agent_id="blackbird")

    prompt_file = "prompts/roles/scout_hub/agent-system.md"
    suggestion = PromptChangeSuggestion(
        assessment_id=main.id, subject_label="Dana Whitcombe — Lumen Fibrosis Program",
        rubric_version=RUBRIC_VERSION,
        feedback_snapshot=[{"review_id": str(learn.id), "reviewer_name": manager.name, "score": 4,
                            "feedback_mode": "learn", "comment": learn.comment,
                            "created_at": now.isoformat(timespec="minutes")}],
        target="scout_hub",
        prompt_files=[{"path": prompt_file,
                       "sha256_12": hashlib.sha256(Path(prompt_file).read_bytes()).hexdigest()[:12]}],
        suggestion="When a proposal repurposes approved drugs, ask who holds the licence "
                   "for the new use.",
        model="demo-model", transcript_available=True)
    s.add(suggestion)
    # The Activity page's cards read the run's own totals and the channel rows: keep them
    # consistent with the messages seeded above, so its screenshot shows a plausible run.
    for name, kind, creator in (("lung-imaging", "thematic", "whitcombe"),
                                ("airway-repair", "thematic", "fenwick"),
                                ("interview-whitcombe", "collaboration", "whitcombe"),
                                ("interview-fenwick", "collaboration", "fenwick"),
                                ("interview-morrow", "collaboration", "morrow")):
        await factories.make_agent_channel(s, run=run, channel_name=name, channel_type=kind,
                                           created_by_agent=creator)
    await s.flush()
    run.total_messages = await s.scalar(
        select(func.count(AgentMessage.id)).where(AgentMessage.simulation_run_id == run.id))
    run.total_api_calls = run.total_messages * 3
    await s.flush()
    return {"run": str(run.id), "assessment_main": str(main.id),
            "assessment_second": str(second.id), "assessment_third": str(third.id),
            "lab_main": "whitcombe", "prompt_suggestion": str(suggestion.id)}


async def main() -> None:
    if not os.environ.get("DATABASE_URL", "").endswith("/" + DB_NAME):
        sys.exit(f"refusing to seed: DATABASE_URL must name {DB_NAME}")

    from src.database import get_engine, get_session_factory
    from src.models import USER_ROLE_PI
    from src.models.help import VIA_DISMISS, HelpIntroDismissal
    from src.services.profile_publish import write_persona_files
    from tests import factories

    now = datetime.now(UTC)
    async with get_session_factory()() as s:
        users = {}
        for key, (name, _) in PEOPLE.items():
            role = ACCOUNT_ROLES.get(key) or ("reviewer" if key.startswith("staff_") else USER_ROLE_PI)
            access = {"staff_pending": "pending", "staff_revoked": "denied"}.get(key, "allowed")
            users[key] = await factories.make_user(
                s, name=name, orcid=SEED_ORCIDS[key], email=_email(key), user_role=role,
                institution="Example University", access_status=access)
        # The manager and the reviewer answered the first-sign-in prompt, so it stays out
        # of their shots; new_reviewer did not (the intro-prompt shot).
        s.add_all([HelpIntroDismissal(user_id=users[key].id, via=VIA_DISMISS)
                   for key in ("manager", "reviewer")])
        pis = await _seed_pis(s, users, now=now)
        run = await _seed_run(s, users, now=now)
        await s.commit()
        for user_id in pis.pop("pi_with_profiles"):
            await write_persona_files(s, uuid.UUID(user_id))
        ids = {
            **{key: str(users[key].id) for key in ACCOUNT_ROLES},
            **pis, **run,
            "staff_preview_orcid": STAFF_PREVIEW_ORCID,
            # tests/e2e/ui_audit/harness.py classifies 404s against this key.
            "bad_uuid": str(uuid.UUID(int=0)),
        }
    await get_engine().dispose()
    print(json.dumps(ids))


if __name__ == "__main__":
    asyncio.run(main())
