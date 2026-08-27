"""Golden master for the profile-generation pipeline (src/services/profile_pipeline.py).

Pins the end-to-end output of run_profile_pipeline for one researcher with every
external dependency faked deterministically:
  - ORCID/PubMed fetches are monkeypatched in the pipeline's namespace (they are
    imported there by name; reconcile_pub_doi is left REAL so DOI reconciliation
    is exercised for real).
  - The Anthropic client is replaced via the src.services.llm.get_anthropic_client
    seam, scripted to return a valid public-profile JSON (retries on a failed
    validation consume additional scripted responses in order; the removal
    cycle deleted the follow-up private-profile-seed LLM call, so a happy-path
    run makes exactly one).

A future change to how the pipeline assembles/stores a profile (field mapping,
version bump, DOI handling, abstract hashing) breaks this snapshot loudly.
"""

import json

import pytest
from sqlalchemy import select

from src.models import Job, Publication, ResearcherProfile
from src.services import corpus as corpus_module
from src.services import profile_pipeline
from src.services.corpus import CorpusStageError
from tests import factories
from tests.fakes import FakeAnthropic

pytestmark = pytest.mark.characterization


# A public-profile JSON that passes _validate_profile (summary 100-350 words,
# >=3 techniques, >=1 disease area) so the happy path runs without the retry.
_VALID_PROFILE = {
    "research_summary": (
        "This laboratory investigates programmable mechanical computation and the "
        "mathematical foundations that make general-purpose calculation possible. "
        "The central line of work formalizes how a sequence of operations can be "
        "encoded on punched cards and executed by an analytical engine, turning an "
        "abstract algorithm into a repeatable physical process. A recurring theme is "
        "the computation of Bernoulli numbers, used as a demanding proving ground for "
        "loop control, intermediate storage, and the reuse of partial results. The "
        "group connects nineteenth-century engine architecture to modern notions of "
        "symbolic manipulation, arguing that the machine could act on entities other "
        "than numbers when those entities obey formal rules. Methodologically the work "
        "spans analytical derivation, stepwise numerical verification, and the careful "
        "design of operation tables that other researchers can follow and reproduce."
    ),
    "techniques": [
        "mechanical computation",
        "algorithm design",
        "numerical analysis",
        "punch-card programming",
    ],
    "experimental_models": ["analytical engine", "difference engine"],
    "disease_areas": ["computational theory"],
    "key_targets": ["Bernoulli numbers"],
    "keywords": ["computing", "mathematics", "engines"],
}

# A public-profile JSON that FAILS _validate_profile on all three of its rules:
# an 18-word summary (min 100), two techniques (min 3) and no disease areas. Every
# test that uses it re-asserts that it really is invalid, so the fixture cannot
# drift into validity and turn its test into a tautology.
_INVALID_PROFILE = {
    "research_summary": (
        "The lab studies engines. Work continues on several fronts and results "
        "will be reported in due course elsewhere."
    ),
    "techniques": ["mechanical computation", "algorithm design"],
    "experimental_models": ["analytical engine"],
    "disease_areas": [],
    "key_targets": ["Bernoulli numbers"],
    "keywords": ["computing"],
}

def _install_fakes(monkeypatch):
    """Patch every external boundary the pipeline reaches through, deterministically."""

    async def fake_fetch_orcid_profile(orcid_id):
        return {
            "name": "Ada Lovelace",
            "institution": "Analytical Engine Institute",
            "department": "Computing",
            "orcid": orcid_id,
            "lab_website": "https://example.org/lab",
        }

    async def fake_fetch_orcid_grants(orcid_id):
        return ["Difference Engine Program", "Analytical Engine Grant"]

    _ADA = [{
        "last": "Lovelace", "fore": "Ada", "initials": "A",
        "collective": None, "affiliations": ["Analytical Engine Institute"],
    }]

    async def fake_fetch_orcid_works(orcid_id):
        # Both works carry a PMID, so the DOI->PMID resolution branch is skipped.
        return [
            {"pmid": "1001", "doi": "10.1000/aaa", "title": "On the Analytical Engine", "year": 1843},
            {"pmid": "1002", "doi": "10.1000/bbb", "title": "Notes on Bernoulli", "year": 1842},
        ]

    async def fake_fetch_works_by_orcid(orcid_id):
        return []  # S2 (OpenAlex) finds nothing new

    async def fake_search_pmids(term, retmax=200):
        return []  # S3/S4 find nothing new

    async def fake_convert_dois_to_pmids(dois):
        return {}

    async def fake_fetch_pubmed_records(pmids):
        # Authoritative DOIs match the ORCID-assigned DOIs -> reconcile returns "ok".
        # Authors are present so the REAL resolve_corpus keeps both records.
        return [
            {
                "pmid": "1001",
                "doi": "10.1000/aaa",
                "title": "On the Analytical Engine",
                "abstract": "We describe the analytical engine and its operation on Bernoulli numbers.",
                "journal": "Taylor's Scientific Memoirs",
                "year": 1843,
                "pub_types": ["Journal Article"],
                "pmcid": None,
                "authors": list(_ADA),
            },
            {
                "pmid": "1002",
                "doi": "10.1000/bbb",
                "title": "Notes on Bernoulli",
                "abstract": "A method for computing Bernoulli numbers with the engine.",
                "journal": "Memoirs",
                "year": 1842,
                "pub_types": ["Journal Article"],
                "pmcid": None,
                "authors": list(_ADA),
            },
        ]

    async def fake_convert_pmids_to_pmcids(pmids):
        return {}

    async def fake_fetch_pmc_methods(pmcid):
        return ""

    # Retrieval seams live in the CORPUS module now; resolve_corpus itself is
    # left REAL so its gates/ranking/cap run in every characterization test.
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", fake_fetch_orcid_profile)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_grants", fake_fetch_orcid_grants)
    monkeypatch.setattr(corpus_module, "fetch_orcid_works", fake_fetch_orcid_works)
    monkeypatch.setattr(corpus_module, "fetch_works_by_orcid", fake_fetch_works_by_orcid)
    monkeypatch.setattr(corpus_module, "search_pmids", fake_search_pmids)
    monkeypatch.setattr(corpus_module, "convert_dois_to_pmids", fake_convert_dois_to_pmids)
    monkeypatch.setattr(corpus_module, "fetch_pubmed_records", fake_fetch_pubmed_records)
    monkeypatch.setattr(profile_pipeline, "convert_pmids_to_pmcids", fake_convert_pmids_to_pmcids)
    monkeypatch.setattr(profile_pipeline, "fetch_pmc_methods", fake_fetch_pmc_methods)

    # LLM: synthesize_profile calls src.services.llm.get_anthropic_client() at
    # call time. The removal cycle deleted the second, private-profile-seed
    # call this pipeline used to make (synthesize_private_profile no longer
    # exists), so a happy-path run consumes exactly one scripted response.
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE)])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)
    return fake_llm


async def test_profile_pipeline_golden_master(db_session, monkeypatch, snapshot):
    fake_llm = _install_fakes(monkeypatch)
    user = await factories.make_user(
        db_session,
        name="Ada Lovelace",
        orcid="0000-0002-1825-0097",
        institution=None,
        department=None,
    )

    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    pubs = (
        await db_session.execute(select(Publication).where(Publication.user_id == user.id))
    ).scalars().all()
    pub_view = sorted(
        [
            {
                "pmid": p.pmid,
                "doi": p.doi,
                "title": p.title,
                "journal": p.journal,
                "year": p.year,
                "abstract": p.abstract,
                "pmcid": p.pmcid,
            }
            for p in pubs
        ],
        key=lambda d: d["pmid"] or "",
    )

    # Deterministic view of what the pipeline produced (ids/timestamps excluded).
    result = {
        "research_summary": profile.research_summary,
        "techniques": profile.techniques,
        "experimental_models": profile.experimental_models,
        "disease_areas": profile.disease_areas,
        "key_targets": profile.key_targets,
        "keywords": profile.keywords,
        "grant_titles": profile.grant_titles,
        "profile_version": profile.profile_version,
        "private_profile_md": profile.private_profile_md,
        "private_profile_seed": profile.private_profile_seed,
        "raw_abstracts_hash": profile.raw_abstracts_hash,
        # Provenance of the synthesis (migration 0023). On the happy path the
        # stored fields passed validation and both works carried an abstract, so
        # the profile is grounded in the two publications below.
        "synthesis_validated": profile.synthesis_validated,
        "evidence_pmid_count": profile.evidence_pmid_count,
        "evidence_pub_count": profile.evidence_pub_count,
        "evidence_state": profile.evidence_state,
        "publications": pub_view,
    }

    assert result == snapshot
    # Exactly one LLM call on the happy path: public synthesis only. The
    # removal cycle deleted the follow-up private-profile-seed call, so
    # private_profile_md/private_profile_seed above are always None now —
    # the columns are kept (decision 5) but nothing in the pipeline writes them.
    assert len(fake_llm.calls) == 1


async def test_profile_pipeline_llm_failure_leaves_fields_unset(db_session, monkeypatch, snapshot):
    """Pin the resilience path: when the public-synthesis LLM call raises, the
    pipeline swallows it, stores no synthesized fields, and leaves version at 0 —
    but still records grant titles and the abstracts hash and attempts the seed.

    The provenance columns stay NULL here, which is the third state they need: no
    synthesis was stored, so there is nothing to say about its validation or its
    evidence. `evidence_state` reads "unknown" rather than claiming the profile
    had no evidence — it had no profile."""
    _install_fakes(monkeypatch)

    # Replace the LLM with one that always raises on create().
    class _BoomMessages:
        def create(self, **kwargs):
            raise RuntimeError("synthesis boom")

    class _BoomClient:
        def __init__(self):
            self.messages = _BoomMessages()

    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: _BoomClient())

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0098",
    )
    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    result = {
        "research_summary": profile.research_summary,
        "techniques": profile.techniques,
        "disease_areas": profile.disease_areas,
        "grant_titles": profile.grant_titles,
        "profile_version": profile.profile_version,
        "private_profile_seed": profile.private_profile_seed,
        "raw_abstracts_hash_is_set": profile.raw_abstracts_hash is not None,
        "synthesis_validated": profile.synthesis_validated,
        "evidence_pmid_count": profile.evidence_pmid_count,
        "evidence_pub_count": profile.evidence_pub_count,
        "evidence_state": profile.evidence_state,
    }
    assert result == snapshot


async def test_profile_pipeline_doi_correction_stores_authoritative(
    db_session, monkeypatch, snapshot
):
    """DOI-gate 'corrected' branch, end-to-end. When the ORCID-curated DOI for a
    PMID disagrees with the DOI PubMed has on record for that SAME PMID,
    reconcile_pub_doi returns the authoritative (PubMed) DOI, and the pipeline must
    store that — not the ORCID candidate. Pins the bad-link-incident guard through
    the pipeline (reconcile_pub_doi is left REAL). The happy-path GM only exercises
    the matching 'ok' branch, so this covers the security-relevant mismatch path."""

    async def fake_fetch_orcid_profile(orcid_id):
        return {"name": "Ada Lovelace", "orcid": orcid_id}

    async def fake_fetch_orcid_grants(orcid_id):
        return []

    async def fake_fetch_orcid_works(orcid_id):
        # ORCID lists a DOI that points at the WRONG paper for this PMID.
        return [{"pmid": "2001", "doi": "10.1000/orcid-wrong", "title": "T", "year": 1843}]

    async def fake_fetch_works_by_orcid(orcid_id):
        return []

    async def fake_search_pmids(term, retmax=200):
        return []

    async def fake_convert_dois_to_pmids(dois):
        return {}

    async def fake_fetch_pubmed_records(pmids):
        # PubMed's record for the SAME PMID carries the authoritative DOI.
        return [
            {
                "pmid": "2001",
                "doi": "10.1000/pubmed-authoritative",
                "title": "On the Analytical Engine",
                "abstract": "We describe the analytical engine.",
                "journal": "Memoirs",
                "year": 1843,
                "pub_types": ["Journal Article"],
                "pmcid": None,
                "authors": [{
                    "last": "Lovelace", "fore": "Ada", "initials": "A",
                    "collective": None, "affiliations": ["Analytical Engine Institute"],
                }],
            }
        ]

    async def fake_convert_pmids_to_pmcids(pmids):
        return {}

    async def fake_fetch_pmc_methods(pmcid):
        return ""

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", fake_fetch_orcid_profile)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_grants", fake_fetch_orcid_grants)
    monkeypatch.setattr(corpus_module, "fetch_orcid_works", fake_fetch_orcid_works)
    monkeypatch.setattr(corpus_module, "fetch_works_by_orcid", fake_fetch_works_by_orcid)
    monkeypatch.setattr(corpus_module, "search_pmids", fake_search_pmids)
    monkeypatch.setattr(corpus_module, "convert_dois_to_pmids", fake_convert_dois_to_pmids)
    monkeypatch.setattr(corpus_module, "fetch_pubmed_records", fake_fetch_pubmed_records)
    monkeypatch.setattr(profile_pipeline, "convert_pmids_to_pmcids", fake_convert_pmids_to_pmcids)
    monkeypatch.setattr(profile_pipeline, "fetch_pmc_methods", fake_fetch_pmc_methods)
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE)])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0099",
    )
    await profile_pipeline.run_profile_pipeline(user.id, db_session)

    pub = (
        await db_session.execute(select(Publication).where(Publication.user_id == user.id))
    ).scalar_one()

    result = {
        "orcid_candidate_doi": "10.1000/orcid-wrong",
        "pubmed_authoritative_doi": "10.1000/pubmed-authoritative",
        "stored_doi": pub.doi,
        "stored_is_authoritative": pub.doi == "10.1000/pubmed-authoritative",
        "stored_is_not_orcid_candidate": pub.doi != "10.1000/orcid-wrong",
    }
    assert result == snapshot
    # Crux, asserted explicitly so a careless --snapshot-update cannot silently
    # bless a regression that starts persisting the wrong (ORCID) DOI again.
    assert pub.doi == "10.1000/pubmed-authoritative"


async def test_profile_pipeline_rerun_increments_version_and_updates_pubs(
    db_session, monkeypatch, snapshot
):
    """Re-run / idempotency. A second run for the same user increments
    profile_version (1 -> 2) and UPDATES the existing publications instead of
    duplicating them (count stays 2). Two LLM calls total: one public synthesis
    per run (the removal cycle deleted the private-seed follow-up call this
    test used to also pin)."""
    _install_fakes(monkeypatch)
    # Script the LLM for two runs: one public-synthesis call each.
    fake_llm = FakeAnthropic(
        [json.dumps(_VALID_PROFILE), json.dumps(_VALID_PROFILE)]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0100",
    )

    first = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    first_version = first.profile_version  # capture the int before the second run mutates it

    second = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    pubs = (
        await db_session.execute(select(Publication).where(Publication.user_id == user.id))
    ).scalars().all()

    result = {
        "first_version": first_version,
        "second_version": second.profile_version,
        "same_profile_row": first.id == second.id,
        "pub_count_after_two_runs": len(pubs),
        "llm_calls_total": len(fake_llm.calls),
        # The provenance columns are rewritten each run, not accumulated: a second
        # valid, grounded run over the same two publications leaves the same 2/2.
        "synthesis_validated": second.synthesis_validated,
        "evidence_pmid_count": second.evidence_pmid_count,
        "evidence_pub_count": second.evidence_pub_count,
    }
    assert result == snapshot


# ===========================================================================
# Step 8/9: what the pipeline records about HOW a profile was produced.
#
# Until migration 0023 it recorded nothing, and two defects lived in that gap:
#
#   * step 9 stored on `if synthesized:` alone, so the validation result — both
#     the first one and the retry's — was computed and discarded. A profile that
#     failed _validate_profile twice was persisted exactly like one that passed.
#   * with PubMed unreachable, ORCID works never reach the prompt (they enter it
#     only via their PubMed records), so the model invents a profile from a name
#     and a department, it passes validation, profile_version is bumped, and zero
#     Publication rows are written.
#
# Both were invisible to a black-box test because the outcome was byte-identical
# to the good path. The tests below are the ones that fail if _validate_profile
# is hardwired to `return True`, and the ones that tell a grounded profile from
# a fabricated one.
# ===========================================================================


def _progress_steps(job: Job) -> list[str]:
    return [p["step"] for p in (job.payload or {}).get("progress", [])]


async def _make_job(db_session, user) -> Job:
    """A real generate_profile Job, so update_progress writes where the worker and
    the /onboarding page read it from (job.payload['progress'])."""
    job = Job(
        type="generate_profile",
        user_id=user.id,
        payload={"user_id": str(user.id), "orcid": user.orcid},
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def test_profile_pipeline_stores_the_retry_not_the_rejected_first_synthesis(
    db_session, monkeypatch, snapshot
):
    """Validation fails on the first attempt and passes on the retry -> the RETRY
    is what gets stored, and the profile is marked validated.

    This is the first of the three tests that die if `_validate_profile` is
    hardwired to `return True`: with a validator that never says no, the retry
    below never fires, the 18-word draft is stored instead of the good one, and
    the LLM is called once rather than twice.
    """
    _install_fakes(monkeypatch)
    assert profile_pipeline._validate_profile(_INVALID_PROFILE) is False, (
        "_INVALID_PROFILE now passes validation, so this test no longer exercises "
        "the retry path it claims to"
    )
    # public #1 (rejected) -> public #2 (accepted, retry)
    fake_llm = FakeAnthropic(
        [json.dumps(_INVALID_PROFILE), json.dumps(_VALID_PROFILE)]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0101",
    )
    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    result = {
        "stored_the_retry": profile.research_summary == _VALID_PROFILE["research_summary"],
        "stored_the_rejected_draft": (
            profile.research_summary == _INVALID_PROFILE["research_summary"]
        ),
        "techniques": profile.techniques,
        "disease_areas": profile.disease_areas,
        "profile_version": profile.profile_version,
        "synthesis_validated": profile.synthesis_validated,
        "evidence_state": profile.evidence_state,
        "llm_calls_total": len(fake_llm.calls),
    }
    assert result == snapshot
    # Cruxes, asserted explicitly so a careless --snapshot-update cannot bless a
    # regression back to storing the rejected draft.
    assert profile.research_summary == _VALID_PROFILE["research_summary"]
    assert profile.synthesis_validated is True
    assert len(fake_llm.calls) == 2, (
        f"{len(fake_llm.calls)} LLM calls; expected 2 (rejected public synthesis, "
        "retry). 1 means the retry never fired, i.e. validation accepted the "
        "invalid draft"
    )


async def test_profile_pipeline_marks_a_profile_that_fails_validation_twice(
    db_session, monkeypatch, snapshot
):
    """Validation fails BOTH times -> the draft is stored, and it is stored
    *marked*: synthesis_validated=False, plus an 'unvalidated' entry in the job
    progress the /onboarding page renders.

    Storing rather than discarding is the deliberate choice (see the step 9
    comment in profile_pipeline.py): the PI gets something to edit instead of an
    unexplained empty form, and the mark is what makes the state distinguishable
    and recoverable. What must never happen is what happened before 0023 — the
    row looking exactly like a profile that passed.

    Second of the three mutation-killing tests: with `_validate_profile` hardwired
    to `return True`, synthesis_validated comes out True, the progress entry is
    absent, and only one LLM call is made.
    """
    _install_fakes(monkeypatch)
    assert profile_pipeline._validate_profile(_INVALID_PROFILE) is False
    # Both public attempts return the same invalid draft.
    fake_llm = FakeAnthropic(
        [json.dumps(_INVALID_PROFILE), json.dumps(_INVALID_PROFILE)]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0102",
    )
    job = await _make_job(db_session, user)
    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session, job=job)

    result = {
        "research_summary": profile.research_summary,
        "techniques": profile.techniques,
        "disease_areas": profile.disease_areas,
        # Still 1: the draft IS the stored profile, and the PI's onboarding page
        # needs a profile to render.
        "profile_version": profile.profile_version,
        "synthesis_validated": profile.synthesis_validated,
        # The draft is thin, but it is thin about real publications.
        "evidence_pmid_count": profile.evidence_pmid_count,
        "evidence_pub_count": profile.evidence_pub_count,
        "evidence_state": profile.evidence_state,
        "unvalidated_in_progress": "unvalidated" in _progress_steps(job),
        "llm_calls_total": len(fake_llm.calls),
    }
    assert result == snapshot
    # THE crux of fix 2. `is False`, not falsey: None means "nothing was ever
    # synthesized here", which is a different state (see the LLM-failure GM).
    assert profile.synthesis_validated is False, (
        "a profile that failed _validate_profile on both attempts was stored with "
        f"synthesis_validated={profile.synthesis_validated!r}. If it is True the "
        "validator is not being consulted; if it is None the store path did not "
        "record the decision at all — either way step 9's gate is gone and a "
        "below-standard profile is again indistinguishable from a good one"
    )
    assert "unvalidated" in _progress_steps(job)
    assert len(fake_llm.calls) == 2


async def test_profile_pipeline_rerun_that_fails_validation_keeps_the_stored_profile(
    db_session, monkeypatch, snapshot
):
    """A monthly refresh whose synthesis fails validation must NOT overwrite the
    good profile that is already stored.

    This is the case that makes "store the draft" safe: storing a marked draft is
    right when there is nothing better, and wrong when there is. Before 0023 the
    pipeline had no way to tell the difference, so the refresh clobbered.

    Third mutation-killing test: with `_validate_profile` hardwired to `return
    True` the second run replaces the summary and bumps the version to 2.
    """
    _install_fakes(monkeypatch)
    assert profile_pipeline._validate_profile(_INVALID_PROFILE) is False
    # Run 1: valid public synthesis (single call). Run 2: invalid twice (the
    # retry also fails validation).
    fake_llm = FakeAnthropic([
        json.dumps(_VALID_PROFILE),
        json.dumps(_INVALID_PROFILE), json.dumps(_INVALID_PROFILE),
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0103",
    )
    first = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    first_version = first.profile_version
    job = await _make_job(db_session, user)
    second = await profile_pipeline.run_profile_pipeline(user.id, db_session, job=job)

    rows = (
        await db_session.execute(
            select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
        )
    ).scalars().all()

    result = {
        "first_version": first_version,
        "second_version": second.profile_version,
        "same_profile_row": first.id == second.id,
        "profile_row_count": len(rows),
        "kept_the_validated_summary": (
            second.research_summary == _VALID_PROFILE["research_summary"]
        ),
        "took_the_rejected_draft": (
            second.research_summary == _INVALID_PROFILE["research_summary"]
        ),
        "synthesis_validated": second.synthesis_validated,
        "evidence_pub_count": second.evidence_pub_count,
        "rejected_in_progress": "validation_rejected" in _progress_steps(job),
        "llm_calls_total": len(fake_llm.calls),
    }
    assert result == snapshot
    assert second.research_summary == _VALID_PROFILE["research_summary"], (
        "a synthesis that failed validation twice overwrote a profile that had "
        "passed it — the monthly refresh now degrades profiles it cannot improve"
    )
    assert second.profile_version == 1, (
        f"profile_version went to {second.profile_version} on a run that stored "
        "nothing; the version must track the stored content, not the attempt"
    )
    assert second.synthesis_validated is True


async def test_profile_pipeline_pubmed_outage_raises_instead_of_fabricating(
    db_session, monkeypatch
):
    """PubMed unreachable, ORCID and the LLM fine: the run RAISES.

    This test used to pin the old policy — proceed, store a profile invented
    from a name and a department, and mark it evidence_lost. The 2026-08-24
    corpus change (audit M5) replaced that: a corpus-stage failure raises
    CorpusStageError, process_job retries up to 3 times, and a dead job is
    visible on /admin/jobs and the manager PI page — because a "profile"
    with zero abstracts behind it is exactly the Kavran-class fabrication
    the coverage design exists to prevent, and the activation gate now
    refuses to activate an agent whose newest job died.
    """
    _install_fakes(monkeypatch)

    async def pubmed_is_down(pmids):
        raise ConnectionError("simulated PubMed outage")

    monkeypatch.setattr(corpus_module, "fetch_pubmed_records", pubmed_is_down)
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE)])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0104",
    )
    with pytest.raises(CorpusStageError):
        await profile_pipeline.run_profile_pipeline(user.id, db_session)

    pubs = (
        await db_session.execute(select(Publication).where(Publication.user_id == user.id))
    ).scalars().all()
    profiles = (
        await db_session.execute(
            select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
        )
    ).scalars().all()
    assert pubs == [] and profiles == [], (
        "an outage run must leave nothing behind for the retry to trip over"
    )
    assert len(fake_llm.calls) == 0, (
        "the LLM was called during a PubMed outage — the fabrication path is back"
    )


async def test_profile_pipeline_researcher_with_no_works_is_not_reported_as_evidence_lost(
    db_session, monkeypatch, snapshot
):
    """A genuinely publication-less researcher onboards, and is NOT confused with
    an outage.

    Same observable surface as the test above — 0 abstracts in the prompt, 0
    Publication rows, a profile written from name and department — but nothing was
    lost: ORCID was reachable and reported no works. An operator triaging
    ungrounded profiles must not be sent to regenerate this one, because
    regenerating cannot help.
    """
    _install_fakes(monkeypatch)

    async def no_works(orcid_id):
        return []

    monkeypatch.setattr(corpus_module, "fetch_orcid_works", no_works)
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE)])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Josiah Carberry", orcid="0000-0002-1825-0105",
    )
    job = await _make_job(db_session, user)
    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session, job=job)

    result = {
        "profile_version": profile.profile_version,
        "summary_is_set": bool(profile.research_summary),
        "synthesis_validated": profile.synthesis_validated,
        "evidence_pmid_count": profile.evidence_pmid_count,
        "evidence_pub_count": profile.evidence_pub_count,
        "evidence_state": profile.evidence_state,
        "ungrounded_in_progress": "ungrounded" in _progress_steps(job),
    }
    assert result == snapshot
    assert profile.profile_version == 1, (
        "a researcher with no publications did not get a profile — a real, "
        "publication-less PI must still be able to onboard"
    )
    assert profile.evidence_state == "no_evidence_available", (
        f"reported {profile.evidence_state!r} for a researcher whose ORCID record "
        "is simply empty; nothing was lost and regeneration cannot help, so this "
        "must not be triaged as an outage"
    )


async def test_profile_pipeline_orcid_works_failure_raises(
    db_session, monkeypatch
):
    """ORCID's works lookup FAILS: the run raises (audit M5) rather than
    recording anything. The old behavior (proceed with evidence_pmid_count
    NULL, state evidence_lost) is gone with the works_lookup_failed flag —
    the honest answer is now a failed job the operator can see and retry."""
    _install_fakes(monkeypatch)

    async def orcid_works_down(orcid_id):
        raise ConnectionError("simulated ORCID outage")

    monkeypatch.setattr(corpus_module, "fetch_orcid_works", orcid_works_down)
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE)])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0106",
    )
    with pytest.raises(CorpusStageError):
        await profile_pipeline.run_profile_pipeline(user.id, db_session)
    assert len(fake_llm.calls) == 0


async def test_profile_pipeline_pubmed_outage_on_rerun_keeps_the_grounded_profile(
    db_session, monkeypatch
):
    """A monthly refresh that runs during a PubMed outage must not replace a
    profile grounded in real abstracts. Under the 2026-08-24 corpus policy the
    refresh RAISES before synthesis, so the stored profile — version, summary,
    evidence counts, generated_at — is untouched."""
    _install_fakes(monkeypatch)
    fake_llm = FakeAnthropic(
        [json.dumps(_VALID_PROFILE), json.dumps(_VALID_PROFILE)]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0107",
    )
    first = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    first_version = first.profile_version
    first_generated_at = first.profile_generated_at

    async def pubmed_is_down(pmids):
        raise ConnectionError("simulated PubMed outage")

    monkeypatch.setattr(corpus_module, "fetch_pubmed_records", pubmed_is_down)
    with pytest.raises(CorpusStageError):
        await profile_pipeline.run_profile_pipeline(user.id, db_session)

    await db_session.refresh(first)
    assert first.profile_version == first_version == 1
    assert first.research_summary == _VALID_PROFILE["research_summary"]
    assert first.profile_generated_at == first_generated_at
    assert first.evidence_pub_count == 2, (
        "a refresh during a PubMed outage degraded a profile grounded in 2 abstracts"
    )
    assert len(fake_llm.calls) == 1
