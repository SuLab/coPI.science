"""run_profile_pipeline drives resolve_corpus + the tenure window (T7).

Pins the audited behaviors end to end against a real database:

* a NEW PI's corpus is stored full-career (top-50 by recency — matching the 62
  existing PIs' storage semantics) while synthesis and export are
  tenure-filtered;
* a corpus-stage failure raises (job retry) and persists NO tenure entry;
* an EXISTING PI's stored rows are never deleted; additions come only from
  ORCID-anchored stages (S4-only candidates are flagged for review, not
  stored); the cap is respected; and the EXPORT is tenure-filtered against the
  full-career store — the exact regression that put pre-tenure papers into 9
  agents' prompts on 2026-08-14 (audit H3);
* tenure derivation: ORCID employment year first, else earliest paper the PI
  herself wrote at Hopkins, else a loud ``tenure_unknown`` progress flag.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.models import Job, Publication
from src.services import profile_export, profile_pipeline
from src.services.corpus import CorpusResult, CorpusStageError
from src.services.jhu_rules import get_tenure_start, set_tenure_start
from src.services.profile_pipeline import run_profile_pipeline
from tests import factories

pytestmark = pytest.mark.integration

_SUMMARY = " ".join(["word"] * 180)
_SYNTH = {
    "research_summary": _SUMMARY,
    "techniques": ["CRISPR", "RNA-seq", "cryo-EM"],
    "experimental_models": ["mouse"],
    "disease_areas": ["cancer"],
    "key_targets": ["KRAS"],
    "keywords": ["biology"],
}


def _rec(pmid, year, title, *, hopkins_pi=False, stages=("s1",)):
    return {
        "pmid": str(pmid),
        "title": title,
        "abstract": f"Abstract of {title}.",
        "journal": "J",
        "year": year,
        "pub_types": ["Journal Article"],
        "authors": [],
        "authorship": "individual",
        "pi_affiliations": (
            ["Johns Hopkins University"] if hopkins_pi else ["Elsewhere U"]
        ),
        "stages": list(stages),
    }


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Patch every network seam; return a namespace to tweak per-test."""
    ns = SimpleNamespace(
        profile={
            "orcid": "0000-0001-2345-6789",
            "name": "Rachel Green",
            "institution": "Johns Hopkins University",
            "department": "MolBio",
            "employments": [],
        },
        corpus=CorpusResult(kept=[], flagged=[]),
        contexts=[],
    )

    async def fake_profile(orcid):
        return dict(ns.profile)

    async def fake_grants(orcid):
        return []

    async def fake_corpus(orcid, name, institution, *, cap=50):
        if isinstance(ns.corpus, Exception):
            raise ns.corpus
        return ns.corpus

    async def fake_pmcids(pmids):
        return {}

    async def fake_synth(context, name):
        ns.contexts.append(context)
        return dict(_SYNTH)

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", fake_profile)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_grants", fake_grants)
    monkeypatch.setattr(profile_pipeline, "resolve_corpus", fake_corpus)
    monkeypatch.setattr(profile_pipeline, "convert_pmids_to_pmcids", fake_pmcids)
    monkeypatch.setattr(profile_pipeline, "synthesize_profile", fake_synth)
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    ns.export_dir = tmp_path
    return ns


async def _make_pi(db_session, *, with_agent=True):
    user = await factories.make_user(
        db_session,
        orcid="0000-0001-2345-6789",
        name="Rachel Green",
        institution="Johns Hopkins University",
    )
    agent = None
    if with_agent:
        agent = await factories.make_agent(
            db_session, user=user, agent_id="green", bot_name="GreenBot"
        )
    job = Job(
        type="generate_profile",
        user_id=user.id,
        payload={"user_id": str(user.id)},
    )
    db_session.add(job)
    await db_session.flush()
    return user, agent, job


async def test_new_pi_stores_full_career_but_synthesizes_and_exports_in_tenure(
    db_session, wired
):
    user, agent, job = await _make_pi(db_session)
    wired.profile["employments"] = [
        {"organization": "Johns Hopkins University", "start_year": 2018,
         "current": True},
    ]
    wired.corpus = CorpusResult(
        kept=[
            _rec(1, 2010, "Pre-tenure paper"),
            _rec(2, 2019, "In-tenure paper A"),
            _rec(3, 2020, "In-tenure paper B"),
        ],
        flagged=[],
    )

    profile = await run_profile_pipeline(user.id, db_session, job)

    stored = (
        (await db_session.execute(
            select(Publication).where(Publication.user_id == user.id)
        )).scalars().all()
    )
    assert sorted(p.pmid for p in stored) == ["1", "2", "3"], (
        "storage is full-career (cohort-consistent with the existing 62 PIs)"
    )

    context = wired.contexts[0]
    assert "In-tenure paper A" in context and "In-tenure paper B" in context
    assert "Pre-tenure paper" not in context, (
        "synthesis must be tenure-scoped (JHU R2)"
    )

    assert await get_tenure_start(db_session, user.id) == 2018

    exported = (wired.export_dir / "green.md").read_text()
    assert "In-tenure paper A" in exported
    assert "Pre-tenure paper" not in exported, (
        "the export top-20 must be tenure-filtered (audit H3)"
    )

    assert profile.evidence_pub_count == 2


async def test_paper_tier_dates_tenure_when_orcid_has_no_hopkins_employment(
    db_session, wired
):
    user, agent, job = await _make_pi(db_session)
    wired.corpus = CorpusResult(
        kept=[
            _rec(1, 2005, "Elsewhere paper"),
            _rec(2, 2015, "First Hopkins paper", hopkins_pi=True),
            _rec(3, 2021, "Later Hopkins paper", hopkins_pi=True),
        ],
        flagged=[],
    )

    await run_profile_pipeline(user.id, db_session, job)

    assert await get_tenure_start(db_session, user.id) == 2015
    context = wired.contexts[0]
    assert "Elsewhere paper" not in context


async def test_no_derivable_tenure_flags_loudly_and_stays_full_career(
    db_session, wired
):
    user, agent, job = await _make_pi(db_session)
    wired.corpus = CorpusResult(
        kept=[_rec(1, 2005, "Old paper"), _rec(2, 2020, "New paper")],
        flagged=[],
    )

    await run_profile_pipeline(user.id, db_session, job)

    assert await get_tenure_start(db_session, user.id) is None
    steps = [p["step"] for p in job.payload.get("progress", [])]
    assert "tenure_unknown" in steps
    context = wired.contexts[0]
    assert "Old paper" in context and "New paper" in context


async def test_a_corpus_stage_failure_raises_and_persists_no_tenure_entry(
    db_session, wired
):
    user, agent, job = await _make_pi(db_session)
    wired.profile["employments"] = [
        {"organization": "Johns Hopkins University", "start_year": 2018,
         "current": True},
    ]
    wired.corpus = CorpusStageError("corpus stage s3_pubmed_auid failed")

    with pytest.raises(CorpusStageError):
        await run_profile_pipeline(user.id, db_session, job)

    assert await get_tenure_start(db_session, user.id) is None


async def test_existing_pi_rows_are_never_deleted_and_s4_only_adds_are_flagged(
    db_session, wired
):
    user, agent, job = await _make_pi(db_session)
    for pmid, year, title in [(10, 2005, "Pre-tenure stored"),
                              (11, 2019, "In-tenure stored")]:
        db_session.add(
            Publication(user_id=user.id, pmid=str(pmid), title=title,
                        abstract=f"Abstract of {title}.", year=year)
        )
    await set_tenure_start(user.id, 2018, "manual", db=db_session)
    await db_session.flush()

    wired.corpus = CorpusResult(
        kept=[
            _rec(11, 2019, "In-tenure stored", stages=("s1",)),
            _rec(12, 2021, "New anchored paper", stages=("s3",)),
            _rec(13, 2022, "S4-only candidate", stages=("s4",)),
        ],
        flagged=[],
    )

    await run_profile_pipeline(user.id, db_session, job)

    stored = sorted(
        p.pmid for p in (await db_session.execute(
            select(Publication).where(Publication.user_id == user.id)
        )).scalars().all()
    )
    assert stored == ["10", "11", "12"], (
        "never delete; add ORCID-anchored only; S4-only goes to review"
    )
    steps = {p["step"] for p in job.payload.get("progress", [])}
    assert "corpus_addition_review" in steps

    context = wired.contexts[0]
    assert "In-tenure stored" in context and "New anchored paper" in context
    assert "Pre-tenure stored" not in context

    exported = (wired.export_dir / "green.md").read_text()
    assert "Pre-tenure stored" not in exported, (
        "export must tenure-filter the FULL-CAREER store (audit H3): this is "
        "the 2026-08-14 nine-agent regression"
    )


async def test_the_cap_is_respected_for_an_existing_pi(db_session, wired, monkeypatch):
    monkeypatch.setattr(profile_pipeline, "CORPUS_CAP", 2)
    user, agent, job = await _make_pi(db_session)
    for pmid, year in [(10, 2019), (11, 2020)]:
        db_session.add(
            Publication(user_id=user.id, pmid=str(pmid), title=f"P{pmid}",
                        abstract="A.", year=year)
        )
    await db_session.flush()

    wired.corpus = CorpusResult(
        kept=[_rec(12, 2021, "Would exceed the cap", stages=("s3",))],
        flagged=[],
    )

    await run_profile_pipeline(user.id, db_session, job)

    stored = (await db_session.execute(
        select(Publication).where(Publication.user_id == user.id)
    )).scalars().all()
    assert sorted(p.pmid for p in stored) == ["10", "11"]
    steps = {p["step"] for p in job.payload.get("progress", [])}
    assert "corpus_cap_reached" in steps
