"""Intake against Postgres (plan Task 3.5; spec §5.2, §6.1, §7.2, §12.4, §15; D3, D17, D38,
D67, D68, F10, K8).

Sources are faked: records are built as the source modules build them, and the network
calls intake makes (RePORTER publication links, OpenAlex and PubMed single lookups) are
monkeypatched. Every name and identifier is invented.
"""
import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import intake
from src.landscaping.intake import (
    SOURCES,
    ensure_sources,
    find_document,
    ingest_positive_controls,
    link_datasets,
    link_grants,
    merge_documents,
    run_slice,
    schedule_due,
    upsert_document,
)
from src.landscaping.labs import (
    COLLISION_PREFIX,
    resolve_lab_aliases,
    unify_grant_pi,
    unify_name_key,
)
from src.landscaping.sources import DocumentRecord, SliceResult
from src.models import AppSetting
from src.models.landscape import (
    LandscapeDocument,
    LandscapeDocumentLink,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeFetch,
    LandscapeLab,
    LandscapeScreen,
    LandscapeSource,
)
from src.services import nih_reporter, openalex, pubmed

pytestmark = pytest.mark.integration

RECENT = date.today() - timedelta(days=30)
ORCID = "0000-0002-1825-0097"
TITLE = "An invented kinase inhibitor slows tumour growth"


def _person(name: str, *, orcid=None, oa=None, nih=None) -> dict:
    return {"name": name, "orcid": orcid, "openalex_author_id": oa, "nih_pi_profile_id": nih}


def _pubmed(pmid: str, *, doi=None, summary="PubMed abstract.", name="Tamsin Marrowby",
            title=TITLE) -> DocumentRecord:
    ids = {"pmid": pmid}
    if doi:
        ids["doi"] = doi
    return DocumentRecord(
        kind="work", ids=ids, title=title, summary=summary, published=RECENT,
        doc_type="article", language="en", license=None, authorships=None,
        role_basis="pubmed_last_author",
        raw_qualifier={"last_author": {"last": name.split()[-1], "fore": name.split()[0]},
                       "affiliation": "Johns Hopkins University, Baltimore, MD"},
        summary_source="pubmed" if summary else None,
        senior_names=[_person(name)],
    )


def _openalex(work_id: str, *, doi=None, pmid=None, summary="OpenAlex text.", kind="work",
              title=TITLE, published=RECENT, name="Tamsin Marrowby", oa="A5000000001",
              orcid=None, role="last") -> DocumentRecord:
    ids = {"openalex": work_id}
    if doi:
        ids["doi"] = doi
    if pmid:
        ids["pmid"] = pmid
    authorship = {"author_position": role, "author": {"id": oa, "display_name": name},
                  "institutions": [{"id": "https://openalex.org/I145311948"}]}
    return DocumentRecord(
        kind=kind, ids=ids, title=title, summary=summary, published=published,
        doc_type="preprint" if kind == "preprint" else "article", language="en", license=None,
        authorships=[authorship], role_basis=role,
        raw_qualifier={"institution_ids": ["I145311948"], "raw_affiliations": []},
        summary_source="openalex" if summary else None,
        senior_names=[_person(name, orcid=orcid, oa=oa)],
    )


def _grant(core: str, *, summary="An invented grant summary.", name="Ada Quill",
           profile="9000001") -> DocumentRecord:
    return DocumentRecord(
        kind="grant", ids={"core_project": core}, title="Invented grant", summary=summary,
        published=RECENT, doc_type="grant", language=None, license=None, authorships=None,
        role_basis="grant_pi", raw_qualifier={"org_name": "JOHNS HOPKINS UNIVERSITY"},
        summary_source="reporter", senior_names=[_person(name, nih=profile)],
        extra={"project_start": RECENT.isoformat(), "project_end": None, "fiscal_year": None},
    )


async def _epoch(db: AsyncSession, status: str = "active") -> LandscapeEpoch:
    epoch = LandscapeEpoch(id=uuid.uuid4(), status=status, components={})
    db.add(epoch)
    await db.flush()
    return epoch


async def _doc(db: AsyncSession, doc_id: uuid.UUID) -> LandscapeDocument:
    doc = await db.get(LandscapeDocument, doc_id)
    await db.refresh(doc)
    return doc


async def _lab(db: AsyncSession, key: str) -> LandscapeLab | None:
    return (await db.execute(
        select(LandscapeLab).where(LandscapeLab.lab_key == key)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _set(db: AsyncSession, key: str, value: str) -> None:
    db.add(AppSetting(key=key, value=value))
    await db.flush()


@pytest_asyncio.fixture
async def db_factory(engine):
    """Sessions on one connection whose outer transaction rolls back; commits are savepoint
    releases, as in test_batches_db."""
    async with engine.connect() as conn:
        trans = await conn.begin()

        @contextlib.asynccontextmanager
        async def factory():
            session = AsyncSession(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                yield session
            finally:
                await session.close()

        try:
            yield factory
        finally:
            if trans.is_active:
                await trans.rollback()


# --- identity --------------------------------------------------------------------------------


async def test_pubmed_first_then_openalex_rekeys_in_place(db_session):
    db = db_session
    doc_id = await upsert_document(db, _pubmed("39100001"))
    await db.commit()
    doc = await _doc(db, doc_id)
    assert doc.canonical_external_id == "pmid:39100001"
    assert doc.role_basis == "pubmed_last_author"
    assert doc.s1_status == "eligible"
    epoch = await _epoch(db)
    db.add(LandscapeScreen(document_id=doc_id, epoch_id=epoch.id, status="done",
                           decision="yes", summary_hash=doc.summary_hash))
    await db.commit()

    again = await upsert_document(
        db, _openalex("W4400000001", doi="10.1234/inv.100", pmid="39100001"))
    await db.commit()

    assert again == doc_id
    count = (await db.execute(
        select(func.count()).select_from(LandscapeDocument)
        .where(LandscapeDocument.pmid == "39100001")
    )).scalar_one()
    assert count == 1
    doc = await _doc(db, doc_id)
    assert doc.canonical_external_id == "doi:10.1234/inv.100"
    assert doc.openalex_id == "W4400000001"
    assert doc.role_basis == "last"
    assert doc.summary_text == "PubMed abstract."
    assert doc.s1_status == "eligible"
    screen = (await db.execute(
        select(LandscapeScreen).where(LandscapeScreen.document_id == doc_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert (screen.status, screen.decision) == ("done", "yes")


async def test_summary_change_resets_s2_s3(db_session):
    db = db_session
    doc_id = await upsert_document(db, _grant("R01XX000010"))
    await db.commit()
    epoch = await _epoch(db)
    db.add_all([
        LandscapeScreen(document_id=doc_id, epoch_id=epoch.id, status="done", decision="yes"),
        LandscapeExtraction(document_id=doc_id, epoch_id=epoch.id, basis="summary",
                            status="done", raw={"ideas": []}),
        LandscapeExtraction(document_id=doc_id, epoch_id=epoch.id, basis="full_text",
                            status="done", raw={"ideas": []}),
    ])
    await db.commit()

    # The same text again changes nothing.
    await upsert_document(db, _grant("R01XX000010"))
    await db.commit()
    screen = (await db.execute(select(LandscapeScreen).where(
        LandscapeScreen.document_id == doc_id).execution_options(populate_existing=True)
    )).scalar_one()
    assert screen.status == "done"

    await upsert_document(db, _grant("R01XX000010", summary="A revised grant summary."))
    await db.commit()
    screen = (await db.execute(select(LandscapeScreen).where(
        LandscapeScreen.document_id == doc_id).execution_options(populate_existing=True)
    )).scalar_one()
    assert (screen.status, screen.decision) == ("pending", None)
    extractions = {
        e.basis: e for e in (await db.execute(select(LandscapeExtraction).where(
            LandscapeExtraction.document_id == doc_id
        ).execution_options(populate_existing=True))).scalars()
    }
    assert extractions["summary"].status == "pending"
    assert extractions["summary"].raw is None
    assert extractions["full_text"].status == "done"


async def test_pubmed_abstract_preferred_over_openalex(db_session):
    db = db_session
    doc_id = await upsert_document(
        db, _openalex("W4400000002", doi="10.1234/inv.200", pmid="39100002"))
    await db.commit()
    assert (await _doc(db, doc_id)).summary_source == "openalex"

    await upsert_document(db, _pubmed("39100002", doi="10.1234/INV.200"))
    await db.commit()
    doc = await _doc(db, doc_id)
    assert (doc.summary_text, doc.summary_source) == ("PubMed abstract.", "pubmed")
    # A later PubMed record never replaces OpenAlex's roles (D38).
    assert doc.role_basis == "last"

    await upsert_document(
        db, _openalex("W4400000002", doi="10.1234/inv.200", pmid="39100002",
                      summary="OpenAlex revised."))
    await db.commit()
    doc = await _doc(db, doc_id)
    assert doc.summary_text == "PubMed abstract."


async def _two_rows(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """A PubMed-only row and an OpenAlex row of one work that share no identifier."""
    drop = await upsert_document(db, _pubmed("39100003"))
    keep = await upsert_document(
        db, _openalex("W4400000003", doi="10.1234/inv.300", summary="OpenAlex text.",
                      title="A differently worded title"))
    await db.commit()
    return keep, drop


async def test_merge_two_existing_documents_tombstones_the_other(db_session):
    db = db_session
    keep, drop = await _two_rows(db)
    grant = await upsert_document(db, _grant("R01XX000030"))
    epoch = await _epoch(db)
    db.add_all([
        LandscapeScreen(document_id=drop, epoch_id=epoch.id, status="done", decision="yes"),
        LandscapeExtraction(document_id=drop, epoch_id=epoch.id, basis="summary", status="done"),
        LandscapeDocumentLink(doc_a=grant, doc_b=drop, link_kind="grant_cites"),
    ])
    await db.commit()

    await merge_documents(db, keep, drop)
    await db.commit()

    tomb = await _doc(db, drop)
    kept = await _doc(db, keep)
    assert tomb.merged_into == keep
    assert tomb.s1_status == "excluded:merged"
    assert kept.pmid == "39100003"
    assert kept.summary_text == "PubMed abstract."
    screens = (await db.execute(select(LandscapeScreen.document_id, LandscapeScreen.status)
                                .where(LandscapeScreen.epoch_id == epoch.id))).all()
    assert screens == [(drop, "done")]
    extraction = (await db.execute(select(LandscapeExtraction.document_id)
                                   .where(LandscapeExtraction.epoch_id == epoch.id))).scalar_one()
    assert extraction == drop
    links = (await db.execute(select(LandscapeDocumentLink.doc_a, LandscapeDocumentLink.doc_b)
                              .where(LandscapeDocumentLink.doc_a == grant))).all()
    assert links == [(grant, keep)]
    # Neither the tombstone nor its rows are selected again.
    eligible = set((await db.execute(select(LandscapeDocument.id).where(
        LandscapeDocument.s1_status == "eligible"))).scalars())
    assert drop not in eligible and keep in eligible
    assert await find_document(db, pmid="39100003") == keep


async def test_upsert_matching_a_tombstone_updates_the_kept_row(db_session):
    db = db_session
    keep, drop = await _two_rows(db)
    await merge_documents(db, keep, drop)
    await db.commit()

    found = await upsert_document(db, _pubmed("39100003", summary="Revised abstract."))
    await db.commit()

    assert found == keep
    assert (await _doc(db, keep)).summary_text == "Revised abstract."
    tomb = await _doc(db, drop)
    assert tomb.merged_into == keep
    assert tomb.summary_text == "PubMed abstract."


async def test_preprint_article_link(db_session):
    db = db_session
    preprint = await upsert_document(db, _openalex(
        "W4400000004", doi="10.1101/2025.01.01.000001", kind="preprint",
        title=TITLE + ".", published=RECENT - timedelta(days=200)))
    article = await upsert_document(db, _openalex(
        "W4400000005", doi="10.1234/inv.500", title="  " + TITLE.upper()))
    late = await upsert_document(db, _openalex(
        "W4400000006", doi="10.1234/inv.600", published=RECENT + timedelta(days=700)))
    await db.commit()

    links = (await db.execute(select(
        LandscapeDocumentLink.doc_a, LandscapeDocumentLink.doc_b, LandscapeDocumentLink.link_kind
    ))).all()
    assert (preprint, article, "preprint_of") in links
    assert all(late not in (a, b) for a, b, _ in links)


async def test_find_document_precedence_skips_tombstones(db_session):
    db = db_session
    first = await upsert_document(db, _openalex("W4400000007", doi="10.1234/inv.700",
                                                pmid="39100007", title="First invented title"))
    second = await upsert_document(db, _pubmed("39100008", title="Shared invented title"))
    preprint = await upsert_document(db, _openalex(
        "W4400000009", doi="10.1101/2025.02.02.000009", kind="preprint",
        title="Shared invented title", published=RECENT - timedelta(days=1500)))
    await db.commit()

    assert await find_document(db, doi="10.1234/INV.700", pmid="39100008") == first
    assert await find_document(db, doi="10.1234/absent", pmid="39100008") == second
    assert await find_document(db, title="shared  INVENTED title.") == second
    assert await find_document(db, title="No such title") is None

    await merge_documents(db, first, second)
    await db.commit()
    assert await find_document(db, pmid="39100008") is None
    assert await find_document(db, title="Shared invented title") == preprint


# --- labs ---------------------------------------------------------------------------------------


async def test_one_record_two_keys_resolve_to_orcid(db_session):
    db = db_session
    await upsert_document(db, _openalex("W4400000010", doi="10.1234/inv.1000",
                                        name="Cora Lindqvist", oa="A5000000010"))
    await db.commit()
    assert (await _lab(db, "oa:A5000000010")).alias_of is None

    doc_id = await upsert_document(db, _openalex(
        "W4400000011", doi="10.1234/inv.1100", name="Cora Lindqvist", oa="A5000000010",
        orcid=ORCID))
    await db.commit()

    assert (await _doc(db, doc_id)).lab_key == f"orcid:{ORCID}"
    assert (await _lab(db, "oa:A5000000010")).alias_of == f"orcid:{ORCID}"
    assert "oa:A5000000010" in (await _lab(db, f"orcid:{ORCID}")).aliases
    assert await resolve_lab_aliases(db, ["oa:A5000000010"]) == f"orcid:{ORCID}"


async def test_grant_pi_unified_through_publication_link(db_session):
    db = db_session
    await resolve_lab_aliases(db, ["nih:9000001"], display_name="Ada Quill")
    await resolve_lab_aliases(db, [f"orcid:{ORCID}"], display_name="Ada M. Quill")

    assert await unify_grant_pi(db, grant_key="nih:9000001", paper_key=f"orcid:{ORCID}",
                                grant_name="Ada Quill", paper_name="Quill, Ada M.") is True
    assert (await _lab(db, "nih:9000001")).alias_of == f"orcid:{ORCID}"

    await resolve_lab_aliases(db, ["nih:9000002"], display_name="Bea Quill")
    assert await unify_grant_pi(db, grant_key="nih:9000002", paper_key="oa:A5000000020",
                                grant_name="Bea Quill", paper_name="Ada Quill") is False
    assert (await _lab(db, "nih:9000002")).alias_of is None


async def test_name_key_unified_only_when_unique(db_session):
    db = db_session
    await resolve_lab_aliases(db, [f"orcid:{ORCID}"], display_name="Rowan Petrakis")
    assert await unify_name_key(db, "name:petrakis|r") == f"orcid:{ORCID}"
    assert (await _lab(db, "name:petrakis|r")).alias_of == f"orcid:{ORCID}"

    await resolve_lab_aliases(db, ["oa:A5000000031"], display_name="Hollis Brandt")
    await resolve_lab_aliases(db, ["oa:A5000000032"], display_name="Harper Brandt")
    assert await unify_name_key(db, "name:brandt|h") == "name:brandt|h"
    lab = await _lab(db, "name:brandt|h")
    assert lab.alias_of is None
    assert set(lab.aliases) == {COLLISION_PREFIX + "oa:A5000000031",
                                COLLISION_PREFIX + "oa:A5000000032"}

    # A patent's first inventor reaches the same rule through intake.
    patent = DocumentRecord(
        kind="patent", ids={"patent_application": "17000001"}, title="Invented device",
        summary="An invented method.", published=RECENT, doc_type="patent_application",
        language=None, license=None, authorships=None, role_basis="assignee",
        raw_qualifier={"applicants": ["The Johns Hopkins University"], "apl": False},
        summary_source="odp", senior_names=[_person("Rowan Petrakis")],
    )
    doc_id = await upsert_document(db, patent)
    assert (await _doc(db, doc_id)).lab_key == f"orcid:{ORCID}"


# --- fetching and scheduling -------------------------------------------------------------------


async def test_incomplete_slice_flagged_and_retried(db_factory, monkeypatch):
    monkeypatch.setattr(intake, "RETRY_DELAYS", (0.0, 0.0))
    async with db_factory() as db:
        await ensure_sources(db)
    calls = {"n": 0}

    async def short_fetch():
        calls["n"] += 1
        return SliceResult(rows=[_grant("R01XX000040")], expected=100, received=90)

    outcome = await run_slice(db_factory, "reporter", "fy:2026", short_fetch)
    assert (outcome.status, outcome.attempts, calls["n"]) == ("incomplete", 3, 3)
    async with db_factory() as db:
        row = await db.get(LandscapeFetch, outcome.fetch_id)
        assert (row.status, row.attempts, row.expected_count, row.received_count) == (
            "incomplete", 3, 100, 90)
        assert row.finished_at is not None
        count = (await db.execute(select(func.count()).select_from(LandscapeDocument).where(
            LandscapeDocument.canonical_external_id == "core_project:R01XX000040"))).scalar_one()
        assert count == 1
        source = (await db.execute(select(LandscapeSource).where(
            LandscapeSource.source == "reporter"))).scalar_one()
        assert source.last_completeness_ratio == pytest.approx(0.9)

    results = iter([
        SliceResult(rows=[], expected=100, received=50),
        SliceResult(rows=[], expected=100, received=100),
    ])

    async def recovering():
        return next(results)

    outcome = await run_slice(db_factory, "reporter", "fy:2025", recovering)
    assert (outcome.status, outcome.attempts) == ("complete", 2)

    async def broken():
        raise RuntimeError("invented outage")

    outcome = await run_slice(db_factory, "nsf", "month:2026-01", broken)
    assert outcome.status == "failed"
    async with db_factory() as db:
        row = await db.get(LandscapeFetch, outcome.fetch_id)
        assert row.status == "failed" and "invented outage" in row.error


async def _record_fetches(db: AsyncSession, pairs, now: datetime) -> None:
    db.add_all([LandscapeFetch(source=s, slice_key=k, started_at=now, status="complete")
                for s, k in pairs])
    await db.flush()


async def test_schedule_cadence(db_session):
    db = db_session
    await ensure_sources(db)
    await db.execute(update(LandscapeSource).values(enabled=True))
    await _set(db, "landscape_phase", "continuous")
    wednesday = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)

    due = await schedule_due(db, now=wednesday)
    for source in ("pubmed", "openalex", "jhtv"):
        assert (source, "daily:2026-11-04") in due
    assert ("openalex", "sweep:2026-11-02/2026-11") in due
    for source in ("nsf", "odp", "datacite", "geo"):
        assert (source, "week:2026-11-02") in due
    assert ("reporter", "fy:2027@2026-11") in due
    assert ("reporter", "fy:2026@2026-11") in due

    await _record_fetches(db, due, wednesday)
    assert await schedule_due(db, now=wednesday + timedelta(hours=2)) == []

    thursday = wednesday + timedelta(days=1)
    assert sorted(await schedule_due(db, now=thursday)) == sorted(
        (s, "daily:2026-11-05") for s in ("pubmed", "openalex", "jhtv"))

    monday = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
    due = await schedule_due(db, now=monday)
    assert ("geo", "week:2026-11-09") in due
    assert ("openalex", "sweep:2026-11-09/2026-11") in due
    assert not any(s == "reporter" for s, _ in due)

    december = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
    assert ("reporter", "fy:2027@2026-12") in await schedule_due(db, now=december)

    await db.execute(update(LandscapeSource).where(LandscapeSource.source == "geo")
                     .values(enabled=False))
    assert not any(s == "geo" for s, _ in await schedule_due(db, now=december))


async def test_backfill_ncbi_slices_only_off_peak(db_session):
    db = db_session
    await ensure_sources(db)
    await _set(db, "landscape_phase", "backfill")
    peak = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)          # Wednesday, 10:00 Eastern
    night = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)          # Wednesday, 22:00 Eastern
    saturday = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)      # Saturday, 10:00 Eastern

    due = await schedule_due(db, now=peak)
    assert not any(s in ("pubmed", "geo") for s, _ in due)
    months = [k for s, k in due if s == "openalex"]
    assert months == sorted(months) and len(months) >= 36
    assert not any(s == "jhtv" for s, _ in due)  # disabled until its terms are read

    for now in (night, saturday):
        due = await schedule_due(db, now=now)
        pubmed_months = [k for s, k in due if s == "pubmed"]
        assert pubmed_months == months
        assert any(s == "geo" for s, _ in due)


async def test_jhtv_starts_disabled(db_session):
    db = db_session
    await ensure_sources(db)
    rows = dict((await db.execute(select(LandscapeSource.source, LandscapeSource.enabled))).all())
    assert set(rows) == set(SOURCES)
    assert rows["jhtv"] is False
    assert all(enabled for source, enabled in rows.items() if source != "jhtv")


async def test_ensure_sources_never_changes_existing_rows(db_session):
    db = db_session
    await ensure_sources(db)
    await db.execute(update(LandscapeSource).where(LandscapeSource.source == "jhtv")
                     .values(enabled=True))
    await db.execute(update(LandscapeSource).where(LandscapeSource.source == "geo")
                     .values(enabled=False))
    await db.commit()

    await ensure_sources(db)
    rows = dict((await db.execute(select(LandscapeSource.source, LandscapeSource.enabled)
                                  .execution_options(populate_existing=True))).all())
    assert rows["jhtv"] is True
    assert rows["geo"] is False
    assert len(rows) == len(SOURCES)


# --- links and positive controls ----------------------------------------------------------------


async def test_link_grants_links_and_unifies(db_session, monkeypatch):
    db = db_session
    grant = await upsert_document(db, _grant("R01XX000050", name="Ada Quill", profile="9000005"))
    agreeing = await upsert_document(db, _openalex(
        "W4400000050", doi="10.1234/inv.5000", pmid="39100050", name="Ada Quill",
        oa="A5000000050", orcid=ORCID))
    other = await upsert_document(db, _openalex(
        "W4400000051", doi="10.1234/inv.5100", pmid="39100051", name="Bea Quill",
        oa="A5000000051", title="Another invented title"))
    await db.commit()
    calls = []

    async def fake_publications(cores):
        calls.append(list(cores))
        return {"R01XX000050": {"39100050", "39100051", "39199999"}}

    monkeypatch.setattr(nih_reporter, "publications_for_cores", fake_publications)

    assert await link_grants(db) == 2
    links = set((await db.execute(select(
        LandscapeDocumentLink.doc_a, LandscapeDocumentLink.doc_b
    ).where(LandscapeDocumentLink.link_kind == "grant_cites"))).all())
    assert links == {(grant, agreeing), (grant, other)}
    assert (await _lab(db, "nih:9000005")).alias_of == f"orcid:{ORCID}"
    assert (await _lab(db, "oa:A5000000051")).alias_of is None

    assert await link_grants(db) == 0
    assert calls == [["R01XX000050"]]


async def test_link_datasets(db_session):
    db = db_session
    by_doi = await upsert_document(db, _openalex("W4400000060", doi="10.1234/inv.6000"))
    by_pmid = await upsert_document(db, _pubmed("39100061", title="A second invented title"))
    datacite = DocumentRecord(
        kind="dataset", ids={"datacite_doi": "10.5061/dryad.inv0001"}, title="Invented data",
        summary=None, published=RECENT, doc_type="dataset", language=None, license=None,
        authorships=None, role_basis=None,
        related_ids={"doi": ["10.1234/INV.6000"], "pmid": ["39100061", "39199998"]},
    )
    geo_set = DocumentRecord(
        kind="dataset", ids={"geo_accession": "GSE900001"}, title="Invented series",
        summary=None, published=RECENT, doc_type="dataset", language=None, license=None,
        authorships=None, role_basis=None, related_ids={"pmid": ["39100061"]},
    )
    datacite_id = await upsert_document(db, datacite)
    geo_id = await upsert_document(db, geo_set)
    await db.commit()

    assert await link_datasets(db) == 3
    links = set((await db.execute(select(
        LandscapeDocumentLink.doc_a, LandscapeDocumentLink.doc_b
    ).where(LandscapeDocumentLink.link_kind == "dataset_of"))).all())
    assert links == {(datacite_id, by_doi), (datacite_id, by_pmid), (geo_id, by_pmid)}
    assert (await _doc(db, datacite_id)).s1_status == "excluded:dataset_evidence_only"
    assert await link_datasets(db) == 0


async def test_positive_controls_ingested_by_id(db_session, monkeypatch):
    db = db_session
    await _set(db, "landscape_positive_controls", '["10.1234/CTRL.1", "39200002"]')
    seen = {"openalex": [], "pubmed": []}
    work = {
        "id": "https://openalex.org/W4400000070", "type": "article", "title": "Control one",
        "ids": {"doi": "https://doi.org/10.1234/ctrl.1"},
        "publication_date": RECENT.isoformat(), "language": "en",
        "authorships": [{
            "author_position": "last", "is_corresponding": False,
            "author": {"id": "https://openalex.org/A5000000070", "display_name": "Ines Okafor"},
            "institutions": [{"id": "https://openalex.org/I145311948"}],
            "raw_affiliation_strings": ["Johns Hopkins University, Baltimore, MD"],
        }],
    }

    async def fake_iter_works(filter_, select_, *, per_page=200):
        seen["openalex"].append(filter_)
        yield ([work], 1) if filter_ == "doi:10.1234/ctrl.1" else ([], 0)

    async def fake_fetch_work(openalex_id, select_):
        return None

    async def fake_fetch_xml(pmids):
        seen["pubmed"].append(list(pmids))
        return "<PubmedArticleSet/>"

    extras = {"39200002": {
        "abstract": "Control two abstract.", "published": RECENT, "language": "en",
        "publication_types": ["Journal Article"],
        "last_author": {"last": "Okonkwo", "fore": "Pell", "orcid": None,
                        "affiliations": ["Kennedy Krieger Institute, Baltimore, MD"]},
    }}
    monkeypatch.setattr(openalex, "iter_works", fake_iter_works)
    monkeypatch.setattr(openalex, "fetch_work", fake_fetch_work)
    monkeypatch.setattr(pubmed, "fetch_pubmed_xml", fake_fetch_xml)
    monkeypatch.setattr(pubmed, "parse_pubmed_extras", lambda xml: extras)
    monkeypatch.setattr(pubmed, "_parse_pubmed_xml",
                        lambda xml: [{"pmid": "39200002", "title": "Control two"}])

    assert await ingest_positive_controls(db) == 2
    first = await find_document(db, doi="10.1234/ctrl.1")
    second = await find_document(db, pmid="39200002")
    assert first is not None and second is not None
    assert (await _doc(db, first)).s1_status == "eligible"
    assert (await _doc(db, second)).role_basis == "pubmed_last_author"
    assert seen["pubmed"] == [["39200002"]]

    # Stored controls are not fetched again.
    assert await ingest_positive_controls(db) == 2
    assert len(seen["openalex"]) == 2 and seen["pubmed"] == [["39200002"]]
