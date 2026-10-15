"""Phase B of spec 2026-10-09 (§5): a PubMed-thin PI's non-PubMed works are stored without a
PMID and ground the profile."""
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from src.models import Publication
from src.services import non_pubmed_works, profile_pipeline
from src.services.corpus import CorpusResult, CorpusStageError
from src.services.job_queue import JobDeferred
from src.services.profile_pipeline import run_profile_pipeline
from tests.unit.test_pipeline_corpus_integration import (  # noqa: F401
    _make_pi,
    _rec,
    wired,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]


def _oa(doi, title, year=2024, abstract="Robots stay safe."):
    # An IEEE venue: profile_export links a 10.1109/ DOI only when the venue names IEEE.
    return {"pmid": None, "doi": doi, "year": year, "title": title, "type": "conference-paper",
            "venue": "IEEE Robotics and Automation Letters", "abstract": abstract,
            "is_retracted": False}


def _orcid(doi, title="ORCID title"):
    return {"pmid": None, "doi": doi, "title": title, "year": 2023,
            "type": "conference-paper", "journal": None}


def _corpus(kept=(), openalex=(), orcid=()):
    return CorpusResult(kept=list(kept), flagged=[], ranked=list(kept),
                        orcid_works=list(orcid), openalex_works=list(openalex))


HOPKINS = {"institutions": ["Johns Hopkins University"], "raw_affiliations": [],
           "orcid_in_metadata": False}


@pytest.fixture(autouse=True)
def hopkins_authorships(monkeypatch, wired):  # noqa: F811
    """Every OpenAlex work the corpus lists carries a Hopkins authorship for the PI, unless a
    test patches ``fetch_orcid_authorships`` itself."""
    async def lookup(orcid):
        return {w["doi"]: HOPKINS for w in wired.corpus.openalex_works if w.get("doi")}

    monkeypatch.setattr(non_pubmed_works, "fetch_orcid_authorships", lookup)


@pytest.fixture
def no_lookup(monkeypatch):
    async def lookup(doi):
        raise AssertionError(f"unexpected OpenAlex lookup of {doi}")

    monkeypatch.setattr(non_pubmed_works, "fetch_work_by_doi", lookup)


async def _pubs(db_session, user_id):
    return (await db_session.execute(
        select(Publication).where(Publication.user_id == user_id))).scalars().all()


@pytest.mark.usefixtures("no_lookup")
async def test_a_pi_with_no_pubmed_paper_is_grounded_on_openalex_works(db_session, wired):  # noqa: F811
    user, agent, job = await _make_pi(db_session)
    wired.corpus = _corpus(openalex=[_oa("10.1109/lra.2022.1", "SHARP shielding planning"),
                                     _oa("10.1109/tac.2021.2", "FaSTrack tracking", year=2021)])

    profile = await run_profile_pipeline(user.id, db_session, job.id)

    assert {(r.doi, r.pmid, r.provenance) for r in await _pubs(db_session, user.id)} == {
        ("10.1109/lra.2022.1", None, "openalex"), ("10.1109/tac.2021.2", None, "openalex")}
    assert "SHARP shielding planning" in wired.contexts[0]
    assert "Robots stay safe." in wired.contexts[0]
    assert profile.evidence_state == "grounded"
    persona = (wired.export_dir / f"{agent.agent_id}.md").read_text()
    assert "https://doi.org/10.1109/lra.2022.1" in persona
    await db_session.refresh(job)
    assert "non_pubmed_works" in [p["step"] for p in job.payload["progress"]]


@pytest.mark.usefixtures("no_lookup")
async def test_a_pi_with_eleven_pubmed_papers_gets_no_non_pubmed_rows(db_session, wired):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    kept = [_rec(100 + i, 2020, f"PubMed paper {i}") for i in range(11)]
    wired.corpus = _corpus(kept=kept, openalex=[_oa("10.1/conf", "Conference paper")])
    await run_profile_pipeline(user.id, db_session, job.id)
    assert all(r.pmid for r in await _pubs(db_session, user.id))


async def test_an_orcid_doi_openalex_lacks_is_looked_up_and_stored_as_s1(
    db_session, wired, monkeypatch,  # noqa: F811
):
    async def lookup(doi):
        return _oa(doi.lower(), "Looked up paper")

    monkeypatch.setattr(non_pubmed_works, "fetch_work_by_doi", lookup)
    user, _agent, job = await _make_pi(db_session)
    wired.corpus = _corpus(orcid=[_orcid("10.1/ORCID")])
    await run_profile_pipeline(user.id, db_session, job.id)
    [row] = await _pubs(db_session, user.id)
    assert (row.doi, row.title, row.provenance) == ("10.1/ORCID", "Looked up paper", "s1")


async def test_a_lookup_failure_fails_the_run_before_any_publication_write(
    db_session, wired, monkeypatch,  # noqa: F811
):
    async def lookup(doi):
        raise RuntimeError("OpenAlex 503")

    monkeypatch.setattr(non_pubmed_works, "fetch_work_by_doi", lookup)
    user, _agent, job = await _make_pi(db_session)
    wired.corpus = _corpus(orcid=[_orcid("10.1/x")])
    with pytest.raises(CorpusStageError, match="non_pubmed_lookup"):
        await run_profile_pipeline(user.id, db_session, job.id)
    assert await _pubs(db_session, user.id) == []


@pytest.mark.usefixtures("no_lookup")
async def test_non_pubmed_jhu_era_works_do_not_end_pubmed_thinness(db_session, wired):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    wired.profile["employments"] = [
        {"organization": "Johns Hopkins University", "start_year": 2020, "current": True}
    ]
    first = [_oa(f"10.1/{i}", f"Paper {i}", year=2021) for i in range(11)]
    wired.corpus = _corpus(openalex=first)
    await run_profile_pipeline(user.id, db_session, job.id)
    wired.corpus = _corpus(openalex=[*first, _oa("10.1/new", "Newest paper", year=2025)])
    await run_profile_pipeline(user.id, db_session, job.id)
    assert "10.1/new" in {r.doi for r in await _pubs(db_session, user.id)}


@pytest.mark.usefixtures("no_lookup")
async def test_a_new_hire_with_only_earlier_non_pubmed_work_gets_the_full_career(
    db_session, wired,  # noqa: F811
):
    user, _agent, job = await _make_pi(db_session)
    wired.profile["employments"] = [
        {"organization": "Johns Hopkins University", "start_year": 2026, "current": True}
    ]
    wired.corpus = _corpus(openalex=[_oa("10.1/a", "Postdoc work", year=2024)])
    profile = await run_profile_pipeline(user.id, db_session, job.id)
    assert profile.full_career_fallback is True and "Postdoc work" in wired.contexts[0]
    assert profile.evidence_state == "grounded"


@pytest.mark.usefixtures("no_lookup")
async def test_a_pubmed_rich_pi_makes_no_openalex_lookup(db_session, wired):  # noqa: F811
    # The lookups run only when the PI may be PubMed-thin (security and semantic review
    # 2026-10-09): a lookup failure must not fail a PubMed-rich PI's regeneration.
    user, _agent, job = await _make_pi(db_session)
    kept = [_rec(200 + i, 2020, f"PubMed paper {i}") for i in range(11)]
    wired.corpus = _corpus(kept=kept, orcid=[_orcid("10.1/conf")])
    await run_profile_pipeline(user.id, db_session, job.id)
    assert all(r.pmid for r in await _pubs(db_session, user.id))


@pytest.mark.usefixtures("no_lookup")
async def test_the_pre_lock_check_uses_the_paper_derived_tenure_year(db_session, wired):  # noqa: F811
    # No recorded year and no Hopkins employment: like _derive_tenure_start, the check
    # dates tenure from the earliest Hopkins paper (2021), so 10 earlier PubMed papers do
    # not make the PI look PubMed-rich (re-audit 2026-10-09).
    user, _agent, job = await _make_pi(db_session)
    kept = [_rec(300 + i, 2005, f"Earlier paper {i}") for i in range(10)]
    kept.append(_rec(400, 2021, "Hopkins paper", hopkins_pi=True))
    wired.corpus = _corpus(kept=kept, openalex=[_oa("10.1/conf", "Conference paper")])
    await run_profile_pipeline(user.id, db_session, job.id)
    assert "10.1/conf" in {r.doi for r in await _pubs(db_session, user.id)}


@pytest.mark.usefixtures("no_lookup")
async def test_an_openalex_work_nothing_ties_to_the_pi_is_not_stored(
    db_session, wired, monkeypatch,  # noqa: F811
):
    # 2026-10-09 rollout: OpenAlex credited one PI's ORCID with 25 papers by namesakes.
    async def lookup(orcid):
        assert orcid == user.orcid
        return {"10.1/own": {"institutions": [], "raw_affiliations": [
                    "Dept. of ECE, Princeton University, Princeton, NJ"],
                    "orcid_in_metadata": False},
                "10.1/namesake": {"institutions": ["State Grid Shanghai Municipal Electric "
                                                   "Power Company"], "raw_affiliations": [],
                                  "orcid_in_metadata": False}}

    monkeypatch.setattr(non_pubmed_works, "fetch_orcid_authorships", lookup)
    user, _agent, job = await _make_pi(db_session)
    wired.profile["organizations"] = ["Princeton University", "Johns Hopkins University"]
    wired.corpus = _corpus(openalex=[_oa("10.1/own", "Safety filters for robots"),
                                     _oa("10.1/namesake", "Cable termination defects")])
    await run_profile_pipeline(user.id, db_session, job.id)
    assert {r.doi for r in await _pubs(db_session, user.id)} == {"10.1/own"}
    await db_session.refresh(job)
    [note] = [p for p in job.payload["progress"] if p["step"] == "non_pubmed_unattributed"]
    assert "Skipped 1 works only OpenAlex attributes to the PI" in note["detail"]


async def test_an_authorship_lookup_failure_fails_the_run_before_any_publication_write(
    db_session, wired, monkeypatch,  # noqa: F811
):
    async def lookup(orcid):
        raise RuntimeError("OpenAlex 503")

    monkeypatch.setattr(non_pubmed_works, "fetch_orcid_authorships", lookup)
    user, _agent, job = await _make_pi(db_session)
    wired.corpus = _corpus(openalex=[_oa("10.1/x", "Conference paper")])
    with pytest.raises(CorpusStageError, match="non_pubmed_lookup"):
        await run_profile_pipeline(user.id, db_session, job.id)
    assert await _pubs(db_session, user.id) == []


async def test_a_budget_deferral_from_the_authorship_list_defers_the_job(
    db_session, wired, monkeypatch,  # noqa: F811
):
    # The list request is charged, so openalex_budget can defer the job; that must not
    # become a stage failure that spends a retry attempt (review 2026-10-09).
    from datetime import UTC, datetime

    async def lookup(orcid):
        raise JobDeferred(datetime.now(UTC), "OpenAlex free budget low")

    monkeypatch.setattr(non_pubmed_works, "fetch_orcid_authorships", lookup)
    user, _agent, job = await _make_pi(db_session)
    wired.corpus = _corpus(openalex=[_oa("10.1/x", "Conference paper")])
    with pytest.raises(JobDeferred):
        await run_profile_pipeline(user.id, db_session, job.id)
    assert await _pubs(db_session, user.id) == []


ELSEWHERE = {"institutions": ["University of Elsewhere"], "raw_affiliations": [],
             "orcid_in_metadata": False}


def _counted(monkeypatch, answer):
    calls = []

    async def fetch(orcid):
        calls.append(orcid)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(non_pubmed_works, "fetch_orcid_authorships", fetch)
    return calls


async def _openalex_row(db_session, user, doi="10.1/b"):
    row = Publication(user_id=user.id, doi=doi, title="An OpenAlex-only work", year=2023,
                      provenance="openalex")
    db_session.add(row)
    await db_session.flush()
    return row


def _not_thin():
    return _corpus(kept=[_rec(100 + i, 2020, f"PubMed paper {i}") for i in range(25)])


async def _rechecked(db_session, job):
    await db_session.refresh(job)
    [detail] = [p["detail"] for p in job.payload["progress"] if p["step"] == "corpus_rechecked"]
    return detail


async def test_a_non_thin_pi_with_openalex_rows_fetches_authorships_once(
    db_session, wired, monkeypatch,  # noqa: F811
):
    user, _agent, job = await _make_pi(db_session)
    row = await _openalex_row(db_session, user)
    calls = _counted(monkeypatch, {"10.1/b": ELSEWHERE})

    async def never(*_a, **_k):
        raise AssertionError("a non-thin PI collects no non-PubMed works")

    monkeypatch.setattr(profile_pipeline, "collect_non_pubmed_works", never)
    wired.corpus = _not_thin()
    # F36: an ORCID profile with no organisations skips the check, so name one here.
    wired.profile["organizations"] = ["Johns Hopkins University"]
    await run_profile_pipeline(user.id, db_session, job.id)
    await db_session.refresh(row)
    assert calls == [user.orcid] and row.excluded_reason == "openalex_affiliation"


async def test_a_failed_authorship_fetch_skips_the_check(db_session, wired, monkeypatch):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    row = await _openalex_row(db_session, user)
    _counted(monkeypatch, httpx.TransportError("connection reset"))
    wired.corpus = _not_thin()
    assert await run_profile_pipeline(user.id, db_session, job.id) is not None
    await db_session.refresh(row)
    assert row.excluded_at is None
    assert ("OpenAlex attribution not checked: the OpenAlex authorship list could not be "
            "fetched.") in await _rechecked(db_session, job)


async def test_a_budget_deferral_defers_the_job(db_session, wired, monkeypatch):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    row = await _openalex_row(db_session, user)
    _counted(monkeypatch, JobDeferred(datetime(2031, 1, 1, tzinfo=UTC), "OpenAlex budget"))
    wired.corpus = _not_thin()
    with pytest.raises(JobDeferred):
        await run_profile_pipeline(user.id, db_session, job.id)
    await db_session.refresh(row)
    assert [p.doi for p in await _pubs(db_session, user.id)] == ["10.1/b"]
    assert row.excluded_at is None


async def test_step1_failure_skips_the_openalex_check(db_session, wired, monkeypatch):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    row = await _openalex_row(db_session, user)
    calls = _counted(monkeypatch, {"10.1/b": ELSEWHERE})

    async def orcid_down(orcid):
        raise httpx.TransportError("ORCID unreachable")

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", orcid_down)
    wired.corpus = _not_thin()
    await run_profile_pipeline(user.id, db_session, job.id)
    await db_session.refresh(row)
    assert calls == [] and row.excluded_at is None
    assert ("OpenAlex attribution not checked: the ORCID profile was unavailable."
            in await _rechecked(db_session, job))


@pytest.mark.usefixtures("no_lookup")
async def test_a_thin_pi_fetches_authorships_once(db_session, wired, monkeypatch):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    await _openalex_row(db_session, user, doi="10.1/old")
    wired.corpus = _corpus(openalex=[_oa("10.1/new", "A new conference paper")])
    calls = _counted(monkeypatch, {"10.1/old": HOPKINS, "10.1/new": HOPKINS})
    await run_profile_pipeline(user.id, db_session, job.id)
    assert calls == [user.orcid]
    assert "10.1/new" in {r.doi for r in await _pubs(db_session, user.id)}


@pytest.mark.usefixtures("no_lookup")
async def test_no_openalex_rows_no_fetch_on_a_non_thin_run(db_session, wired, monkeypatch):  # noqa: F811
    user, _agent, job = await _make_pi(db_session)
    wired.corpus = _corpus(kept=[_rec(100 + i, 2020, f"PubMed paper {i}") for i in range(11)])
    calls = _counted(monkeypatch, {})
    await run_profile_pipeline(user.id, db_session, job.id)
    assert calls == []
