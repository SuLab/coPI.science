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
