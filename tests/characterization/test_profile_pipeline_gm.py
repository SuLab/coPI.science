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

from src.models import Publication
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
        "publications": pub_view,
    }

    assert result == snapshot
    # Exactly two LLM calls on the happy path: public synthesis + private seed.
    assert len(fake_llm.calls) == 2


async def test_profile_pipeline_llm_failure_leaves_fields_unset(db_session, monkeypatch, snapshot):
    """Pin the resilience path: when the public-synthesis LLM call raises, the
    pipeline swallows it, stores no synthesized fields, and leaves version at 0 —
    but still records grant titles and the abstracts hash and attempts the seed."""
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
    }
    assert result == snapshot
