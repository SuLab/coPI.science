"""S2 screen and S3 extract runners against Postgres (plan Task 4.4; spec §7.1, §7.3, §7.4; D42,
D44, D67, N12).

The Message Batches API is a fake ``messages.batches`` resource whose batches end at once, each
result chosen from the request; the real ``batches.submit`` and ``batches.poll`` run against it.
Normalization's outside lookups are faked. Every name and identifier is invented. Each epoch
stores a snapshot of the judging inputs, which the runners read (spec §7.11).
"""
# ``FakeBatches`` defines a method named ``list``; postponed annotations keep ``list[dict]``
# below from resolving to it.
from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.prompt_safety import FENCE_TAGS
from src.config import get_settings
from src.landscaping import batches, epochs
from src.landscaping.stages import extract, screen
from src.models.landscape import (
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeExtractionLink,
    LandscapeIdea,
    LandscapeScreen,
)
from src.services import llm

pytestmark = pytest.mark.integration

REGISTERED_TAGS = {"publication_abstract", "methods_section", "patent", "trial_record"}
QUOTE = "Knockdown of INVK1 reduced invented fibrosis in mice"


def _message(text: str = "", stop_reason: str = "end_turn", *, model: str | None = None):
    return SimpleNamespace(
        model=model or get_settings().landscape_screen_model, stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="bio") if stop_reason == "refusal" else None,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _succeeded(payload: dict):
    return SimpleNamespace(type="succeeded", message=_message(json.dumps(payload)))


def _user(params: dict) -> str:
    return params["messages"][0]["content"]


class FakeBatches:
    """``client.messages.batches``: every batch has ended by the time it is retrieved, and
    ``reply(params)`` gives each request's result."""

    def __init__(self) -> None:
        self.reply = lambda params: _succeeded({})
        self.remote: dict[str, SimpleNamespace] = {}
        self.requests: dict[str, list[dict]] = {}

    def create(self, *, requests):
        api_id = f"msgbatch_{len(self.remote) + 1:04d}"
        now = datetime.now(UTC)
        self.remote[api_id] = SimpleNamespace(
            id=api_id, created_at=now, processing_status="ended", ended_at=now
        )
        self.requests[api_id] = list(requests)
        return self.remote[api_id]

    def retrieve(self, api_id):
        return self.remote[api_id]

    def list(self, *, limit=20):
        return sorted(self.remote.values(), key=lambda b: b.created_at, reverse=True)

    def results(self, api_id):
        return iter([
            SimpleNamespace(custom_id=r["custom_id"], result=self.reply(r["params"]))
            for r in self.requests[api_id]
        ])

    def params(self) -> list[dict]:
        return [r["params"] for requests in self.requests.values() for r in requests]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(batches, "_RECONCILER", batches._Reconciler())


@pytest.fixture
def fake(monkeypatch):
    client = FakeBatches()
    monkeypatch.setattr(batches, "_batches_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _fake_normalization(monkeypatch):
    """Normalization matches every name, with invented ids, and makes no outside call."""

    async def targets(items, *, pubtator, criteria):
        return [{**t, "hgnc_id": "HGNC:900001"} for t in items]

    async def disease(item, *, pubtator):
        return {**item, "mondo_id": "MONDO:9000001"}

    async def agent(item):
        return {**item, "chembl_id": None}

    async def annotations(pmids):
        return {}

    monkeypatch.setattr(extract.normalize, "normalize_targets", targets)
    monkeypatch.setattr(extract.normalize, "normalize_disease", disease)
    monkeypatch.setattr(extract.normalize, "normalize_agent", agent)
    monkeypatch.setattr(extract.pubtator, "pubtator_annotations", annotations)


@pytest_asyncio.fixture
async def db_factory(engine):
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


async def _epoch(db_factory, status: str = "candidate") -> uuid.UUID:
    async with db_factory() as db:
        epoch = LandscapeEpoch(status=status, snapshot=epochs.build_snapshot())
        db.add(epoch)
        await db.commit()
        return epoch.id


async def _doc(
    db_factory, *, title: str, summary: str, kind: str = "work", doi: str | None = None,
    details: dict | None = None, institution: str | None = None,
) -> uuid.UUID:
    async with db_factory() as db:
        doc = LandscapeDocument(
            kind=kind, canonical_external_id=f"invented:{uuid.uuid4().hex}", title=title,
            summary_text=summary, summary_hash=uuid.uuid4().hex, doi=doi, details=details,
            qualifying_institution=institution, lab_key="orcid:0000-0002-0000-0001",
            s1_status="eligible",
        )
        db.add(doc)
        await db.commit()
        return doc.id


async def _screened(db_factory, epoch_id: uuid.UUID, doc_id: uuid.UUID, decision="yes") -> None:
    async with db_factory() as db:
        db.add(LandscapeScreen(
            document_id=doc_id, epoch_id=epoch_id, status="done", decision=decision
        ))
        await db.commit()


async def _poll(db_factory) -> list:
    outcomes = []
    async for batch_id, chunk, is_last in batches.poll(db_factory):
        outcomes.extend(chunk)
        if is_last:
            await batches.mark_ingested(db_factory, batch_id)
    return outcomes


async def _screen_row(db_factory, epoch_id, doc_id) -> LandscapeScreen:
    async with db_factory() as db:
        return await db.scalar(
            select(LandscapeScreen).where(
                LandscapeScreen.epoch_id == epoch_id, LandscapeScreen.document_id == doc_id
            )
        )


async def _extraction(db_factory, epoch_id, doc_id) -> LandscapeExtraction | None:
    async with db_factory() as db:
        return await db.scalar(
            select(LandscapeExtraction).where(
                LandscapeExtraction.epoch_id == epoch_id,
                LandscapeExtraction.document_id == doc_id,
                LandscapeExtraction.basis == "summary",
            )
        )


async def _count(db_factory, model, *where) -> int:
    async with db_factory() as db:
        return await db.scalar(select(func.count()).select_from(model).where(*where))


def _screen_answer(in_scope: str) -> dict:
    return {
        "in_scope": in_scope, "research_kind": ["target_validation"],
        "targets_mentioned": ["INVK1"], "diseases_mentioned": ["invented fibrosis"],
        "modality_mentioned": None, "reason": "An invented reason.",
    }


def _idea(quote: str) -> dict:
    return {
        "idea_type": "biology",
        "targets": [{"as_written": "INVK1", "kind": "gene", "species": "human"}],
        "direction": "inhibit",
        "agent": {"as_written": "", "kind": "other"},
        "disease": {"as_written": "invented fibrosis"},
        "modality": {"as_written": "", "basis": "none"},
        "stage": "target_validation",
        "evidence_grade": "in_vivo",
        "evidence_summary": "Knockdown reduced fibrosis in an invented mouse model.",
        "mechanism_hypothesis": "INVK1 drives an invented fibrotic programme.",
        "target_rubric": [
            {"item": 1, "status": "present", "grade": "in_vivo", "quote": quote,
             "section": "abstract"},
        ],
        "key_results": [
            {"claim": "Knockdown reduced fibrosis.", "quote": quote, "section": "abstract"},
            {"claim": "Never said.", "quote": "a sentence the text never contains",
             "section": "abstract"},
        ],
        "model_systems": ["mouse"], "patient_population": "", "biomarkers": [],
        "novelty_claims": [], "limitations": [], "unmet_need_statements": [],
        "funding_and_ip_mentions": [],
    }


async def test_screen_yes_and_unsure_go_to_extract(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    docs = {
        answer: await _doc(
            db_factory, title=f"Invented study {answer}", summary=f"An invented summary {answer}."
        )
        for answer in ("yes", "unsure", "no")
    }
    fake.reply = lambda params: _succeeded(_screen_answer(
        next(a for a in docs if f"Invented study {a}" in _user(params))
    ))

    assert await screen.submit_screen_batch(db_factory, epoch_id) == 3
    await screen.ingest_screen_outcomes(db_factory, await _poll(db_factory))

    rows = {a: await _screen_row(db_factory, epoch_id, d) for a, d in docs.items()}
    assert rows["yes"].status == "done" and rows["yes"].decision == "yes"
    assert rows["unsure"].status == "done" and rows["unsure"].decision == "unsure"
    assert rows["no"].status == "excluded:screen"

    assert await extract.submit_extract_batch(db_factory, epoch_id) == 2
    assert await _extraction(db_factory, epoch_id, docs["yes"]) is not None
    assert await _extraction(db_factory, epoch_id, docs["unsure"]) is not None
    assert await _extraction(db_factory, epoch_id, docs["no"]) is None


async def test_screen_input_has_no_names_or_journal(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    await _doc(
        db_factory, title="An invented kinase study", summary="An invented abstract.",
        institution="Invented University",
        details={
            "senior_names": [{"name": "Ada Inventedson", "orcid": None}],
            "raw_qualifier": {"org_name": "Invented Institute of Health"},
            "extra": {"journal": "Journal of Invented Results", "authors": ["Bo Madeupson"]},
        },
    )

    assert await screen.submit_screen_batch(db_factory, epoch_id) == 1
    (params,) = fake.params()
    user = _user(params)
    assert "An invented kinase study" in user and "An invented abstract." in user
    for name in ("Ada Inventedson", "Invented University", "Invented Institute of Health",
                 "Journal of Invented Results", "Bo Madeupson"):
        assert name not in user
        assert name not in params["system"]


async def test_refused_item_goes_down_chain_then_refused(db_factory, fake, monkeypatch):
    epoch_id = await _epoch(db_factory)
    doc_id = await _doc(db_factory, title="An invented title", summary="An invented summary.")
    fake.reply = lambda params: SimpleNamespace(
        type="succeeded", message=_message(stop_reason="refusal")
    )
    batch_model = get_settings().landscape_screen_model

    assert await screen.submit_screen_batch(db_factory, epoch_id) == 1
    await screen.ingest_screen_outcomes(db_factory, await _poll(db_factory))
    row = await _screen_row(db_factory, epoch_id, doc_id)
    assert row.status == "pending" and row.fallback_from == batch_model
    # A row waiting for the chain is never batched again.
    assert await screen.submit_screen_batch(db_factory, epoch_id) == 0

    called: list[str] = []

    async def refuse(client, **request):
        called.append(request["model"])
        return _message(stop_reason="refusal", model=request["model"])

    monkeypatch.setattr(llm, "get_anthropic_client", lambda: object())
    monkeypatch.setattr(llm, "abeta_create", refuse)
    assert await batches.run_fallbacks(db_factory, screen.FALLBACKS[0]) == 0

    expected = [m for m in dict.fromkeys(get_settings().landscape_fallback_models)
                if m != batch_model]
    assert called == expected
    assert batch_model not in called
    row = await _screen_row(db_factory, epoch_id, doc_id)
    assert row.status == "refused"
    assert row.refusal_category == "bio"


async def test_active_epoch_runner_uses_its_own_model(db_factory, fake, monkeypatch):
    """A settings change after the epoch was created reaches neither its batch nor its
    fallback request: the epoch judges with the model it stored (§7.11)."""
    stored_model = get_settings().landscape_screen_model
    epoch_id = await _epoch(db_factory, status="active")
    doc_id = await _doc(db_factory, title="An invented title", summary="An invented summary.")
    monkeypatch.setattr(get_settings(), "landscape_screen_model", "claude-invented-later")

    assert await screen.submit_screen_batch(db_factory, epoch_id) == 1
    (params,) = fake.params()
    assert params["model"] == stored_model
    async with db_factory() as db:
        inputs = await epochs.epoch_inputs(db, epoch_id)
        row = await db.scalar(select(LandscapeScreen).where(LandscapeScreen.document_id == doc_id))
        rebuilt = await screen.FALLBACKS[0].build(db, row)
    assert params["system"] == inputs.system("screen")
    assert rebuilt["model"] == stored_model
    assert rebuilt["system"] == params["system"]


async def test_errored_resubmitted_once_then_failed(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    doc_id = await _doc(db_factory, title="An invented title", summary="An invented summary.")
    fake.reply = lambda params: SimpleNamespace(type="errored")

    assert await screen.submit_screen_batch(db_factory, epoch_id) == 1
    await screen.ingest_screen_outcomes(db_factory, await _poll(db_factory))
    row = await _screen_row(db_factory, epoch_id, doc_id)
    assert (row.status, row.attempts) == ("pending", 1)

    assert await screen.submit_screen_batch(db_factory, epoch_id) == 1
    await screen.ingest_screen_outcomes(db_factory, await _poll(db_factory))
    row = await _screen_row(db_factory, epoch_id, doc_id)
    assert row.status == "failed:errored"
    assert await screen.submit_screen_batch(db_factory, epoch_id) == 0


async def test_extract_creates_ideas_with_epoch(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    other_epoch = await _epoch(db_factory)
    doc_id = await _doc(
        db_factory, title="An invented fibrosis study", summary=f"{QUOTE}, in an invented model."
    )
    await _screened(db_factory, epoch_id, doc_id)
    fake.reply = lambda params: _succeeded({"ideas": [_idea(QUOTE)], "linked_publications": []})

    assert await extract.submit_extract_batch(db_factory, epoch_id) == 1
    await extract.ingest_extract_outcomes(db_factory, await _poll(db_factory))

    row = await _extraction(db_factory, epoch_id, doc_id)
    assert row.status == "done"
    assert row.quotes_verified >= 1
    assert row.quotes_total > row.quotes_verified
    assert row.raw is not None
    assert await _count(db_factory, LandscapeIdea, LandscapeIdea.epoch_id == epoch_id) >= 1
    assert await _count(db_factory, LandscapeIdea, LandscapeIdea.epoch_id == other_epoch) == 0


async def test_derived_rows_written_once_on_replay(db_factory, fake, monkeypatch):
    epoch_id = await _epoch(db_factory)
    paper_doi = "10.1234/invented.0101"
    await _doc(db_factory, title="An invented paper", summary="An invented summary.",
               doi=paper_doi)
    patent_id = await _doc(
        db_factory, kind="patent", title="An invented compound patent",
        summary=f"{QUOTE}. Supporting work: doi {paper_doi}.",
    )
    await _screened(db_factory, epoch_id, patent_id)
    fake.reply = lambda params: _succeeded({
        "ideas": [_idea(QUOTE)],
        "linked_publications": [{"doi": paper_doi, "pmid": "", "title": ""}],
    })
    merges: list[int] = []
    real_merge = extract.merge_candidate

    async def counting_merge(db, **kwargs):
        merges.append(kwargs["index"])
        return await real_merge(db, **kwargs)

    monkeypatch.setattr(extract, "merge_candidate", counting_merge)

    assert await extract.submit_extract_batch(db_factory, epoch_id) == 1
    outcomes = await _poll(db_factory)
    await extract.ingest_extract_outcomes(db_factory, outcomes)
    ideas = await _count(db_factory, LandscapeIdea, LandscapeIdea.epoch_id == epoch_id)
    links = await _count(
        db_factory, LandscapeExtractionLink, LandscapeExtractionLink.epoch_id == epoch_id
    )

    await extract.ingest_extract_outcomes(db_factory, outcomes)

    assert merges == [0]
    assert ideas >= 1 and links == 1
    assert await _count(db_factory, LandscapeIdea, LandscapeIdea.epoch_id == epoch_id) == ideas
    assert await _count(
        db_factory, LandscapeExtractionLink, LandscapeExtractionLink.epoch_id == epoch_id
    ) == links


async def test_extraction_link_only_when_verbatim_and_resolved(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    cited, absent_from_text, unstored = (
        "10.1234/invented.0001", "10.1234/invented.0002", "10.1234/invented.0003"
    )
    cited_id = await _doc(db_factory, title="An invented cited paper", summary="Invented.",
                          doi=cited)
    await _doc(db_factory, title="An invented uncited paper", summary="Invented.",
               doi=absent_from_text)
    text = f"An invented listing. Supporting work: doi {cited} and doi {unstored}."
    listing_id = await _doc(db_factory, kind="jhtv", title="An invented listing", summary=text)
    work_id = await _doc(db_factory, title="An invented article", summary=text)
    for doc_id in (listing_id, work_id):
        await _screened(db_factory, epoch_id, doc_id)
    fake.reply = lambda params: _succeeded({
        "ideas": [],
        "linked_publications": [
            {"doi": doi, "pmid": "", "title": ""} for doi in (cited, absent_from_text, unstored)
        ],
    })

    assert await extract.submit_extract_batch(db_factory, epoch_id) == 2
    await extract.ingest_extract_outcomes(db_factory, await _poll(db_factory))

    async with db_factory() as db:
        links = (await db.scalars(select(LandscapeExtractionLink))).all()
    extraction = await _extraction(db_factory, epoch_id, listing_id)
    assert [(link.epoch_id, link.doc_a, link.doc_b, link.extraction_id) for link in links] == [
        (epoch_id, listing_id, cited_id, extraction.id)
    ]


async def test_registered_fence_tags_only(monkeypatch):
    assert REGISTERED_TAGS <= FENCE_TAGS
    tags: list[str] = []
    real_delimit = screen.delimit

    def recording(content, tag="untrusted_content"):
        tags.append(tag)
        return real_delimit(content, tag)

    monkeypatch.setattr(screen, "delimit", recording)
    if hasattr(extract, "delimit"):
        monkeypatch.setattr(extract, "delimit", recording)
    kinds = ("work", "preprint", "grant", "patent", "jhtv", "dataset")
    for kind in kinds:
        doc = LandscapeDocument(
            kind=kind, canonical_external_id=f"invented:{kind}", title="An invented title",
            summary_text="An invented summary.", s1_status="eligible",
            details={"extra": {"phr_text": "An invented relevance.",
                               "claims": ["1. An invented compound."]}},
        )
        screen.user_message(doc)
    assert len(tags) == len(kinds)
    assert set(tags) <= REGISTERED_TAGS
