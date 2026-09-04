"""Golden master for the profile-generation pipeline (src/services/profile_pipeline.py).

Pins the end-to-end output of run_profile_pipeline for one researcher with every
external dependency faked deterministically:
  - ORCID/PubMed fetches are monkeypatched in the pipeline's namespace (they are
    imported there by name; reconcile_pub_doi is left REAL so DOI reconciliation
    is exercised for real).
  - The Anthropic client is replaced via the src.services.llm.get_anthropic_client
    seam, scripted to return a valid public-profile JSON then a private-profile
    markdown seed.

A future change to how the pipeline assembles/stores a profile (field mapping,
version bump, DOI handling, abstract hashing) breaks this snapshot loudly.
"""

import json

import pytest
from sqlalchemy import select

from src.models import Job, Publication, ResearcherProfile
from src.services import profile_pipeline
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

_PRIVATE_SEED = (
    "# Private Profile\n\n"
    "## Collaboration Preferences\n"
    "Prefers rigorous, mathematically grounded collaborators.\n\n"
    "## Communication Style\n"
    "Precise and formal; values worked examples.\n\n"
    "## Topic Priorities\n"
    "Programmable computation; symbolic manipulation.\n\n"
    "## Criteria to Always Explore\n"
    "Whether a method generalizes beyond numbers."
)


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

    async def fake_fetch_orcid_works(orcid_id):
        # Both works carry a PMID, so the DOI->PMID resolution branch is skipped.
        return [
            {"pmid": "1001", "doi": "10.1000/aaa", "title": "On the Analytical Engine", "year": 1843},
            {"pmid": "1002", "doi": "10.1000/bbb", "title": "Notes on Bernoulli", "year": 1842},
        ]

    async def fake_convert_dois_to_pmids(dois):
        return {}

    async def fake_fetch_pubmed_records(pmids):
        # Authoritative DOIs match the ORCID-assigned DOIs -> reconcile returns "ok".
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
            },
        ]

    async def fake_convert_pmids_to_pmcids(pmids):
        return {}

    async def fake_fetch_pmc_methods(pmcid):
        return ""

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", fake_fetch_orcid_profile)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_grants", fake_fetch_orcid_grants)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_works", fake_fetch_orcid_works)
    monkeypatch.setattr(profile_pipeline, "convert_dois_to_pmids", fake_convert_dois_to_pmids)
    monkeypatch.setattr(profile_pipeline, "fetch_pubmed_records", fake_fetch_pubmed_records)
    monkeypatch.setattr(profile_pipeline, "convert_pmids_to_pmcids", fake_convert_pmids_to_pmcids)
    monkeypatch.setattr(profile_pipeline, "fetch_pmc_methods", fake_fetch_pmc_methods)

    # LLM: synthesize_profile / synthesize_private_profile both call
    # src.services.llm.get_anthropic_client() at call time. First scripted
    # response is the public JSON, second is the private markdown seed.
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE), _PRIVATE_SEED])
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
        # Still onboarding (fix round: Step 9b's seed generation is now gated
        # on this), matching every other GM fixture in this file that expects
        # the private-seed call to fire on a first run.
        onboarding_complete=False,
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
    # Exactly two LLM calls on the happy path: public synthesis + private seed.
    assert len(fake_llm.calls) == 2


async def test_profile_pipeline_llm_failure_leaves_fields_unset(db_session, monkeypatch, snapshot):
    """Pin the resilience path: when the LLM call raises, the pipeline swallows
    it, stores no synthesized fields, and leaves version at 0 — but still
    records grant titles and the abstracts hash. `onboarding_complete=False`
    keeps Step 9b's seed generation gated "in" (it is gated on "still
    onboarding" since the COR-23 fix round), and the same raising client backs
    that call too, so this also exercises Step 9b's own except branch — not
    just the public-synthesis one — which is why `private_profile_seed` below
    still comes back None.

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
        db_session, name="Grace Hopper", orcid="0000-0002-1825-0098",
        onboarding_complete=False,  # still onboarding: Step 9b's seed call fires
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
            }
        ]

    async def fake_convert_pmids_to_pmcids(pmids):
        return {}

    async def fake_fetch_pmc_methods(pmcid):
        return ""

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", fake_fetch_orcid_profile)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_grants", fake_fetch_orcid_grants)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_works", fake_fetch_orcid_works)
    monkeypatch.setattr(profile_pipeline, "convert_dois_to_pmids", fake_convert_dois_to_pmids)
    monkeypatch.setattr(profile_pipeline, "fetch_pubmed_records", fake_fetch_pubmed_records)
    monkeypatch.setattr(profile_pipeline, "convert_pmids_to_pmcids", fake_convert_pmids_to_pmcids)
    monkeypatch.setattr(profile_pipeline, "fetch_pmc_methods", fake_fetch_pmc_methods)
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE), _PRIVATE_SEED])
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
    profile_version (1 -> 2), UPDATES the existing publications instead of
    duplicating them (count stays 2), and does NOT regenerate the private seed
    (that only happens when no seed exists yet). Three LLM calls total: public
    synthesis on each run + one private-seed generation on the first run only."""
    _install_fakes(monkeypatch)
    # Script the LLM for two runs: run 1 = public JSON + private seed; run 2 =
    # public JSON only (the seed step is skipped once a seed already exists).
    fake_llm = FakeAnthropic(
        [json.dumps(_VALID_PROFILE), _PRIVATE_SEED, json.dumps(_VALID_PROFILE)]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0100",
        onboarding_complete=False,  # still onboarding: Step 9b's seed call fires
    )

    first = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    first_version = first.profile_version  # capture the int before the second run mutates it
    first_seed = first.private_profile_seed

    second = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    pubs = (
        await db_session.execute(select(Publication).where(Publication.user_id == user.id))
    ).scalars().all()

    result = {
        "first_version": first_version,
        "second_version": second.profile_version,
        "same_profile_row": first.id == second.id,
        "pub_count_after_two_runs": len(pubs),
        "seed_set_after_first_run": first_seed is not None,
        "seed_unchanged_on_rerun": second.private_profile_seed == first_seed,
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
    the LLM is called twice rather than three times.
    """
    _install_fakes(monkeypatch)
    assert profile_pipeline._validate_profile(_INVALID_PROFILE) is False, (
        "_INVALID_PROFILE now passes validation, so this test no longer exercises "
        "the retry path it claims to"
    )
    # public #1 (rejected) -> public #2 (accepted) -> private seed
    fake_llm = FakeAnthropic(
        [json.dumps(_INVALID_PROFILE), json.dumps(_VALID_PROFILE), _PRIVATE_SEED]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0101",
        onboarding_complete=False,  # still onboarding: Step 9b's seed call fires
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
    assert len(fake_llm.calls) == 3, (
        f"{len(fake_llm.calls)} LLM calls; expected 3 (rejected public synthesis, "
        "retry, private seed). 2 means the retry never fired, i.e. validation "
        "accepted the invalid draft"
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
    absent, and only two LLM calls are made.
    """
    _install_fakes(monkeypatch)
    assert profile_pipeline._validate_profile(_INVALID_PROFILE) is False
    # Both public attempts return the same invalid draft, then the private seed.
    fake_llm = FakeAnthropic(
        [json.dumps(_INVALID_PROFILE), json.dumps(_INVALID_PROFILE), _PRIVATE_SEED]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0102",
        onboarding_complete=False,  # still onboarding: Step 9b's seed call fires
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
    assert len(fake_llm.calls) == 3


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
    # Run 1: valid public synthesis + private seed. Run 2: invalid twice (the seed
    # step is skipped because run 1 left a seed).
    fake_llm = FakeAnthropic([
        json.dumps(_VALID_PROFILE), _PRIVATE_SEED,
        json.dumps(_INVALID_PROFILE), json.dumps(_INVALID_PROFILE),
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0103",
        onboarding_complete=False,  # still onboarding: Step 9b's seed call fires
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


async def test_profile_pipeline_pubmed_outage_stores_a_profile_marked_evidence_lost(
    db_session, monkeypatch, snapshot
):
    """PubMed unreachable, ORCID and the LLM fine: the profile is fabricated from
    a name and a department, and now says so.

    Every ingredient of the defect is reproduced: ORCID lists two works with
    PMIDs, `fetch_pubmed_records` raises, so `pubs_for_synthesis` is empty, the
    synthesis context contains no publication at all, ZERO Publication rows are
    written — and the model still returns a profile that PASSES _validate_profile
    (the fixture is the same valid one the happy path uses, which is exactly what
    a real model does: it writes plausible prose from the name).

    Onboarding must still complete (asserted), so the discriminator cannot be a
    refusal to store. It is the pair of evidence counts: 2 identifiers in hand, 0
    abstracts in the prompt -> evidence_lost.
    """
    _install_fakes(monkeypatch)

    async def pubmed_is_down(pmids):
        raise ConnectionError("simulated PubMed outage")

    monkeypatch.setattr(profile_pipeline, "fetch_pubmed_records", pubmed_is_down)
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE), _PRIVATE_SEED])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    # Observe the prompt without replacing it: the claim "no publication reached
    # the model" is about the real context builder's output.
    contexts: list[str] = []
    real_ctx = profile_pipeline._build_synthesis_context

    def recording_ctx(**kwargs):
        out = real_ctx(**kwargs)
        contexts.append(out)
        return out

    monkeypatch.setattr(profile_pipeline, "_build_synthesis_context", recording_ctx)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0104",
    )
    job = await _make_job(db_session, user)
    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session, job=job)

    pubs = (
        await db_session.execute(select(Publication).where(Publication.user_id == user.id))
    ).scalars().all()

    result = {
        # Onboarding completed: there is a profile and it has a summary.
        "profile_version": profile.profile_version,
        "summary_is_set": bool(profile.research_summary),
        # ...and it passed the shape validator, which is the whole problem:
        # validation cannot see grounding.
        "synthesis_validated": profile.synthesis_validated,
        "validator_accepts_it": profile_pipeline._validate_profile(
            {
                "research_summary": profile.research_summary,
                "techniques": profile.techniques,
                "disease_areas": profile.disease_areas,
            }
        ),
        # The discriminator.
        "evidence_pmid_count": profile.evidence_pmid_count,
        "evidence_pub_count": profile.evidence_pub_count,
        "evidence_state": profile.evidence_state,
        "publication_rows": len(pubs),
        "context_has_publications_section": "## Publications" in contexts[0],
        "ungrounded_in_progress": "ungrounded" in _progress_steps(job),
    }
    assert result == snapshot
    # The crux: a fabricated profile is no longer indistinguishable from a real
    # one. Both explicit, because either alone can be satisfied by accident —
    # `evidence_pub_count == 0` also holds for a researcher with no papers, and
    # only the PMID count separates "we lost it" from "there was none".
    assert profile.evidence_pub_count == 0 and profile.evidence_pmid_count == 2
    assert profile.evidence_state == "evidence_lost"
    assert len(pubs) == 0, (
        "Publication rows were written while PubMed was unreachable — they came "
        "from somewhere other than PubMed and the count above means nothing"
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

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_works", no_works)
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE), _PRIVATE_SEED])
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


async def test_profile_pipeline_orcid_works_failure_is_not_reported_as_no_works(
    db_session, monkeypatch, snapshot
):
    """The inverse mistake: ORCID's works lookup FAILS, so the pipeline does not
    know how many publications exist. Recording 0 identifiers would read as "this
    researcher has no papers"; the count is left NULL and the state is
    evidence_lost, which is the honest answer and the actionable one."""
    _install_fakes(monkeypatch)

    async def orcid_works_down(orcid_id):
        raise ConnectionError("simulated ORCID outage")

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_works", orcid_works_down)
    fake_llm = FakeAnthropic([json.dumps(_VALID_PROFILE), _PRIVATE_SEED])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0106",
    )
    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    result = {
        "profile_version": profile.profile_version,
        "evidence_pmid_count": profile.evidence_pmid_count,
        "evidence_pub_count": profile.evidence_pub_count,
        "evidence_state": profile.evidence_state,
    }
    assert result == snapshot
    assert profile.evidence_pmid_count is None and profile.evidence_state == "evidence_lost"


async def test_profile_pipeline_pubmed_outage_on_rerun_keeps_the_grounded_profile(
    db_session, monkeypatch, snapshot
):
    """The refresh case of the same defect: a monthly refresh that runs during a
    PubMed outage must not replace a profile grounded in real abstracts with one
    invented from a name. Both syntheses pass validation, so only the evidence
    counts can tell the second one is worse — which is the reason they are
    persisted rather than merely logged."""
    _install_fakes(monkeypatch)
    fake_llm = FakeAnthropic(
        [json.dumps(_VALID_PROFILE), _PRIVATE_SEED, json.dumps(_VALID_PROFILE)]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0107",
        onboarding_complete=False,  # still onboarding: Step 9b's seed call fires
    )
    first = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    first_version = first.profile_version
    first_generated_at = first.profile_generated_at

    async def pubmed_is_down(pmids):
        raise ConnectionError("simulated PubMed outage")

    monkeypatch.setattr(profile_pipeline, "fetch_pubmed_records", pubmed_is_down)
    job = await _make_job(db_session, user)
    second = await profile_pipeline.run_profile_pipeline(user.id, db_session, job=job)

    result = {
        "first_version": first_version,
        "second_version": second.profile_version,
        "evidence_pub_count": second.evidence_pub_count,
        "evidence_state": second.evidence_state,
        "generated_at_untouched": second.profile_generated_at == first_generated_at,
        "rejected_in_progress": "validation_rejected" in _progress_steps(job),
    }
    assert result == snapshot
    assert second.evidence_pub_count == 2 and second.profile_version == 1, (
        "a refresh during a PubMed outage replaced a profile grounded in 2 "
        "abstracts with one grounded in none"
    )


async def test_a_pmid_listed_twice_by_orcid_inserts_exactly_one_publication(
    db_session, monkeypatch
):
    """V1-16b/c: one ORCID works listing naming the same PMID twice must not
    db.add() two Publication rows (post-0025 that is an IntegrityError that
    aborts the whole pipeline run), and must not feed the same publication
    into the synthesis context twice (fix-round Minor 3: `_dedup_pmids`
    upstream dedupes the PMID *list*, but `fetch_pubmed_records` can itself
    hand back the same record more than once for one requested PMID — that is
    what actually exercises the in-loop `existing_pubs[pmid] = pub` update,
    not `_dedup_pmids`)."""
    _install_fakes(monkeypatch)

    async def dupe_works(orcid_id):
        return [{"pmid": "1001", "doi": None}, {"pmid": "1001", "doi": None}]

    async def echo_records(pmids):
        # Return each requested PMID's record TWICE regardless of how many
        # times it appears in `pmids` (which is already deduped by the time it
        # gets here) — this is what makes the assertion depend on the in-loop
        # existing_pubs update rather than only on _dedup_pmids.
        records = []
        for p in pmids:
            rec = {
                "pmid": p, "doi": None, "title": "T", "abstract": "A", "journal": "J",
                "year": 1843, "pub_types": ["Journal Article"], "pmcid": None,
            }
            records.append(rec)
            records.append(rec)
        return records

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_works", dupe_works)
    monkeypatch.setattr(profile_pipeline, "fetch_pubmed_records", echo_records)

    # Observe the assembled synthesis context (data, not prompt text) without
    # replacing the real context builder.
    contexts: list[str] = []
    real_ctx = profile_pipeline._build_synthesis_context

    def recording_ctx(**kwargs):
        out = real_ctx(**kwargs)
        contexts.append(out)
        return out

    monkeypatch.setattr(profile_pipeline, "_build_synthesis_context", recording_ctx)

    user = await factories.make_user(db_session, name="Dupe Lovelace")
    await profile_pipeline.run_profile_pipeline(user.id, db_session)

    rows = (await db_session.execute(
        select(Publication).where(Publication.user_id == user.id, Publication.pmid == "1001")
    )).scalars().all()
    assert len(rows) == 1

    # The residual the duplicated-record case exposes: the SAME record must
    # not be appended twice to pubs_for_synthesis, which would duplicate its
    # heading in the assembled context.
    assert contexts[0].count("### T (J, 1843)") == 1


async def test_first_run_exports_the_private_seed_to_disk(db_session, monkeypatch, tmp_path):
    """COR-23: an admin-seeded lab whose PI never visits /onboarding/private-profile
    must still get agent instructions on disk from the pipeline's own seed write.

    This is the only test in this file with an AgentRegistry, so it is also the only
    one that reaches the export at all — hence the explicit export-dir patching (the
    rest of the file never writes, so it never needed it).

    Control for the fix-round gate on Step 9b's GENERATION (contrast with
    test_pi_who_cleared_their_private_profile_does_not_get_a_new_seed):
    onboarding_complete=False (an admin-seeded PI who never onboarded) still
    gets a seed generated and exported.
    """
    from src.services import profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    monkeypatch.setattr(profile_export, "PRIVATE_PROFILES_DIR", tmp_path / "private")
    _install_fakes(monkeypatch)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", onboarding_complete=False
    )
    agent = await factories.make_agent(
        db_session, user=user, agent_id="gmseed", bot_name="GmSeedBot"
    )
    await db_session.flush()

    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    assert profile.private_profile_md is None      # nothing promoted it yet
    assert profile.private_profile_seed            # the pipeline generated one
    written = (tmp_path / "private" / f"{agent.agent_id}.md").read_text(encoding="utf-8")
    assert written.strip() == _PRIVATE_SEED.strip()


async def test_pi_who_cleared_their_private_profile_does_not_get_a_new_seed(
    db_session, monkeypatch, tmp_path
):
    """Fix round (COR-23): a PI who has completed onboarding and then cleared
    both private_profile_md and private_profile_seed — POST
    /onboarding/private-profile with a blank form does exactly that, and also
    sets onboarding_complete=True (onboarding.py:save_private_profile) — must
    NOT have Step 9b synthesize a fresh seed on the next pipeline run (a
    regenerate, an admin re-enqueue, or a monthly refresh). Regenerating would
    silently push model-authored instructions back to the agent for a PI who
    deliberately turned them off. Contrast with
    test_first_run_exports_the_private_seed_to_disk (onboarding_complete=False):
    an admin-seeded PI who never onboarded still gets a seed.
    """
    from src.services import profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    monkeypatch.setattr(profile_export, "PRIVATE_PROFILES_DIR", tmp_path / "private")
    fake_llm = _install_fakes(monkeypatch)

    user = await factories.make_user(
        db_session, name="Ada Lovelace", onboarding_complete=True
    )
    agent = await factories.make_agent(
        db_session, user=user, agent_id="gmcleared", bot_name="GmClearedBot"
    )
    await db_session.flush()

    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    assert profile.private_profile_md is None
    assert profile.private_profile_seed is None
    # Only the public-profile synthesis call happened; the private-seed
    # response scripted into _install_fakes's FakeAnthropic was never reached.
    assert len(fake_llm.calls) == 1
    assert not (tmp_path / "private" / f"{agent.agent_id}.md").exists()


async def test_a_json_array_synthesis_response_does_not_crash_the_pipeline(
    db_session, monkeypatch
):
    """Minor 2 (fix round): extract_json's ```json fenced-block branch parses
    whatever valid JSON is inside the fence, so a malformed LLM response whose
    fence wraps a JSON ARRAY (not an object) makes synthesize_profile return a
    list. Before the isinstance guard added in this fix round, that list
    passed profile.get(...)-free through `_validate_profile`'s try/except as
    validated=False (truthy, non-empty), which then made `if not validated and
    synthesized:` retry, and once the retry's response ran out too (raising
    inside the second synthesize_profile call, leaving `synthesized` un-
    reassigned) the pipeline fell through to Step 9 with the raw list and
    crashed in `apply_synthesis` (`synthesized.get(...)`, list has no such
    method). The guard normalizes to {} right after the synthesis call, so the
    pipeline completes exactly as it does on any other unparseable synthesis:
    nothing stored, one LLM call, no retry, no crash.
    """
    _install_fakes(monkeypatch)
    array_response = "```json\n[1, 2, 3]\n```"
    fake_llm = FakeAnthropic([array_response])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)

    user = await factories.make_user(db_session, name="Array Lovelace")
    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    assert profile.research_summary is None
    assert profile.profile_version == 0
    assert profile.synthesis_validated is None
    assert len(fake_llm.calls) == 1
