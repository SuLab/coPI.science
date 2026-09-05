"""Targeted repair of publication text truncated by the pre-`itertext()` parser.

Until `2c1d504` the PubMed parser read ElementTree's `.text`, which stops at the
first child element, so `<ArticleTitle>Role of <i>TP53</i> in cancer.` was stored
as `"Role of "` and an `<AbstractText>` starting with markup was stored as `""`.
`35cc010` repairs such a row on the pipeline's existing-row branch, but only for a
PMID that is still listed on the owner's ORCID record. Measured on the production
copy (`copi_verify`, 4,508 rows): 512 rows match the corruption predicate and 84 of
them belong to PIs whose ORCID works list contains neither that PMID nor its DOI --
including twelve PIs whose ORCID works list is empty outright, which is why
`scripts/backfill_publications.py` inserted their rows in the first place. Those
rows are what this script exists for.
"""

import uuid

import pytest

from scripts.repair_publication_text import repair
from src.models import Publication, User

pytestmark = pytest.mark.integration


async def _seed_user(db, orcid: str | None = None) -> User:
    user = User(
        id=uuid.uuid4(), name="PI", orcid=orcid or f"0000-{uuid.uuid4().hex[:12]}"
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_pub(db, user: User, **kwargs) -> Publication:
    pub = Publication(user_id=user.id, **kwargs)
    db.add(pub)
    await db.flush()
    return pub


def _fake_fetch(records: list[dict]):
    """Async fetch stub that records which PMIDs were requested."""
    requested: list[list[str]] = []

    async def fetch(pmids: list[str]) -> list[dict]:
        requested.append(list(pmids))
        return [r for r in records if r["pmid"] in pmids]

    fetch.requested = requested  # type: ignore[attr-defined]
    return fetch


GOOD = {
    "pmid": "111",
    "title": "Role of TP53 in cancer.",
    "abstract": "A" * 400,
    "journal": "J. Repair",
    "year": 2020,
}


async def test_dry_run_reports_the_repair_but_writes_nothing(db_session):
    user = await _seed_user(db_session)
    pub = await _seed_pub(
        db_session, user, pmid="111", title="Role of ", abstract="A" * 400
    )

    report = await repair(db_session, fetch=_fake_fetch([GOOD]), apply=False)

    assert ("111", "would-repair", "title,journal,year", user.orcid) in report
    await db_session.refresh(pub)
    assert pub.title == "Role of "


async def test_apply_repairs_a_title_truncated_at_the_first_inline_tag(db_session):
    user = await _seed_user(db_session)
    pub = await _seed_pub(
        db_session, user, pmid="111", title="Role of ", abstract="A" * 400
    )

    report = await repair(db_session, fetch=_fake_fetch([GOOD]), apply=True)

    assert ("111", "repair", "title,journal,year", user.orcid) in report
    await db_session.refresh(pub)
    assert pub.title == "Role of TP53 in cancer."
    assert pub.journal == "J. Repair"
    assert pub.year == 2020


async def test_apply_repairs_an_abstract_blanked_by_the_old_parser(db_session):
    user = await _seed_user(db_session)
    pub = await _seed_pub(
        db_session, user, pmid="111", title="Role of TP53 in cancer.", abstract=""
    )

    await repair(db_session, fetch=_fake_fetch([GOOD]), apply=True)

    await db_session.refresh(pub)
    assert pub.abstract == "A" * 400


async def test_a_record_pubmed_has_no_abstract_for_is_never_blanked_or_shortened(
    db_session,
):
    """The write guard: a PubMed hiccup, or a record that genuinely carries no
    abstract, must not destroy the text already on the row."""
    user = await _seed_user(db_session)
    pub = await _seed_pub(
        db_session,
        user,
        pmid="111",
        title="Role of ",
        abstract="An abstract already on the row that PubMed is not returning now.",
    )
    thin = {"pmid": "111", "title": "Role of TP53 in cancer.", "abstract": ""}

    await repair(db_session, fetch=_fake_fetch([thin]), apply=True)

    await db_session.refresh(pub)
    assert pub.title == "Role of TP53 in cancer."
    assert pub.abstract == (
        "An abstract already on the row that PubMed is not returning now."
    )


async def test_the_repair_is_re_runnable(db_session):
    """A repaired row stops matching; a row PubMed has no abstract for keeps
    matching and reports `no-change` forever rather than rewriting anything."""
    user = await _seed_user(db_session)
    await _seed_pub(
        db_session, user, pmid="111", title="Role of ", abstract="A" * 400
    )
    await _seed_pub(
        db_session, user, pmid="222", title="A commentary with no abstract.", abstract=""
    )
    records = [GOOD, {"pmid": "222", "title": "A commentary with no abstract."}]

    first = await repair(db_session, fetch=_fake_fetch(records), apply=True)
    assert ("111", "repair", "title,journal,year", user.orcid) in first
    assert ("222", "no-change", "", user.orcid) in first

    second = await repair(db_session, fetch=_fake_fetch(records), apply=True)
    assert second == [("222", "no-change", "", user.orcid)]


async def test_a_healthy_row_is_never_selected(db_session):
    user = await _seed_user(db_session)
    await _seed_pub(
        db_session, user, pmid="111", title="Role of TP53 in cancer.", abstract="A" * 400
    )

    fetch = _fake_fetch([GOOD])
    assert await repair(db_session, fetch=fetch, apply=True) == []
    assert fetch.requested == []


async def test_one_pmid_stored_for_two_pis_costs_one_fetch_and_repairs_both(db_session):
    """One efetch slot, two rows repaired -- and each entry carries its own owner,
    because the follow-up half of the deploy step is "re-run the pipeline for the
    PIs whose evidence just changed"."""
    one = await _seed_user(db_session, orcid="0000-0000-0000-0001")
    two = await _seed_user(db_session, orcid="0000-0000-0000-0002")
    a = await _seed_pub(db_session, one, pmid="111", title="Role of ", abstract="A" * 400)
    b = await _seed_pub(db_session, two, pmid="111", title="Role of ", abstract="A" * 400)

    fetch = _fake_fetch([GOOD])
    report = await repair(db_session, fetch=fetch, apply=True)

    assert fetch.requested == [["111"]]
    assert sorted(orcid for _, _, _, orcid in report) == [
        "0000-0000-0000-0001",
        "0000-0000-0000-0002",
    ]
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.title == b.title == "Role of TP53 in cancer."


async def test_the_doi_is_never_touched(db_session):
    """DOI assignment has its own reconciliation gate (the fix for the bad-links
    incident); a text repair must not re-decide it."""
    user = await _seed_user(db_session)
    pub = await _seed_pub(
        db_session,
        user,
        pmid="111",
        title="Role of ",
        abstract="A" * 400,
        doi="10.1000/curated",
    )

    await repair(
        db_session, fetch=_fake_fetch([{**GOOD, "doi": "10.1000/other"}]), apply=True
    )

    await db_session.refresh(pub)
    assert pub.doi == "10.1000/curated"


async def test_a_pmid_pubmed_cannot_resolve_is_reported_not_silently_dropped(db_session):
    user = await _seed_user(db_session)
    await _seed_pub(db_session, user, pmid="999", title="Role of ", abstract="A" * 400)

    report = await repair(db_session, fetch=_fake_fetch([GOOD]), apply=True)

    assert report == [("999", "error-no-record", "", user.orcid)]


async def test_limit_bounds_the_rows_considered(db_session):
    user = await _seed_user(db_session)
    await _seed_pub(db_session, user, pmid="111", title="Role of ", abstract="A" * 400)
    await _seed_pub(db_session, user, pmid="222", title="Study of ", abstract="A" * 400)

    report = await repair(db_session, fetch=_fake_fetch([GOOD]), apply=False, limit=1)

    assert [pmid for pmid, _, _, _ in report] == ["111"]


async def test_all_sweeps_rows_the_corruption_signature_misses(db_session):
    """The predicate is a signature, not an oracle: a title cut at markup that is
    neither empty nor followed by a space is invisible to it. Measured on the
    production copy, the predicate finds 248 genuinely-wrong rows and a full sweep
    finds 896."""
    user = await _seed_user(db_session)
    pub = await _seed_pub(
        db_session,
        user,
        pmid="111",
        title="Inhibition of STEP",  # cut at <sub>61</sub>; no trailing space
        abstract="A" * 400,
    )
    fresh = {**GOOD, "title": "Inhibition of STEP61 ameliorates deficits."}

    assert await repair(db_session, fetch=_fake_fetch([fresh]), apply=True) == []
    await db_session.refresh(pub)
    assert pub.title == "Inhibition of STEP"

    report = await repair(
        db_session, fetch=_fake_fetch([fresh]), apply=True, all_rows=True
    )

    assert ("111", "repair", "title,journal,year", user.orcid) in report
    await db_session.refresh(pub)
    assert pub.title == "Inhibition of STEP61 ameliorates deficits."


async def test_all_still_skips_a_row_with_no_pmid(db_session):
    user = await _seed_user(db_session)
    await _seed_pub(db_session, user, pmid=None, title="Role of ", abstract="A" * 400)

    fetch = _fake_fetch([GOOD])
    assert await repair(db_session, fetch=fetch, apply=True, all_rows=True) == []
    assert fetch.requested == []
