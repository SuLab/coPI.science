"""S4 triage submission against Postgres (spec §7.4 step 5, §7.5, D20, L24).

The batch layer is faked: ``batches.submit`` records the items it is given, so each test
reads the exact user message S4 would send. Every session shares one connection whose outer
transaction rolls back. Every name below is invented. Each epoch stores a snapshot of the
judging inputs, which the runner reads (spec §7.11).
"""
import contextlib
import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import batches, epochs
from src.landscaping.schemas import stage_schema
from src.landscaping.stages import triage
from src.models.landscape import (
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeTriage,
)

pytestmark = pytest.mark.integration

INVENTED_PERSON = "Quillfeather"
INVENTED_JOURNAL = "Journal of Invented Results"
INVENTED_INSTITUTE = "Invented Institute of Marsh Studies"
QUOTE = "knockdown of INV1 reduced tumour growth in mice"


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


@pytest.fixture
def submitted(monkeypatch):
    """The items of every faked ``batches.submit`` call, in order."""
    calls: list[list[tuple[uuid.UUID, dict]]] = []

    async def fake_submit(db_factory, *, stage, row_model, items):
        assert stage == "triage"
        assert row_model is LandscapeTriage
        calls.append(list(items))
        return uuid.uuid4()

    monkeypatch.setattr(batches, "submit", fake_submit)
    return calls


def _candidate() -> dict:
    return {
        "idea_type": "biology",
        "targets": [{"as_written": "INV1", "kind": "gene", "species": "human"}],
        "direction": "inhibit",
        "agent": {"as_written": "", "kind": "other"},
        "disease": {"as_written": "invented carcinoma"},
        "stage": "target_validation",
        "evidence_grade": "in_vivo",
        "evidence_summary": "INV1 loss slows an invented carcinoma in mice.",
        "mechanism_hypothesis": "INV1 drives proliferation through an invented pathway.",
        "target_rubric": [
            {"item": 1, "status": "present", "grade": "in_vivo", "quote": QUOTE,
             "section": "abstract"},
            {"item": 2, "status": "unclear", "grade": None, "quote": "", "section": ""},
        ],
        "key_results": [{"claim": "Tumour growth fell.", "quote": QUOTE, "section": "abstract"}],
        "model_systems": ["invented xenograft"],
        "funding_and_ip_mentions": [f"R01 awarded to {INVENTED_PERSON}"],
    }


async def _idea(
    db_factory, *, evidence_status: dict | None, data_flags: dict | None = None,
    epoch_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """One idea with one attached document; returns ``(epoch_id, idea_id)``."""
    async with db_factory() as db:
        if epoch_id is None:
            epoch = LandscapeEpoch(status="candidate", snapshot=epochs.build_snapshot())
            db.add(epoch)
            await db.flush()
            epoch_id = epoch.id
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
            title=f"INV1 in carcinoma, from the {INVENTED_PERSON} lab",
            summary_text=f"{QUOTE}.", published_on=date(2026, 3, 1), document_type="article",
            details={"journal": INVENTED_JOURNAL},
            qualifying_authorships=[{"name": f"Ada {INVENTED_PERSON}"}],
            qualifying_institution=INVENTED_INSTITUTE,
            lab_key=f"lab:{INVENTED_PERSON.lower()}", s1_status="eligible",
        )
        db.add(doc)
        await db.flush()
        extraction = LandscapeExtraction(
            document_id=doc.id, epoch_id=epoch_id, basis="summary", status="done",
            raw={"ideas": [_candidate()], "linked_publications": []},
        )
        idea = LandscapeIdea(
            epoch_id=epoch_id, idea_key=f"biology|INV1|inhibit|{uuid.uuid4().hex}",
            idea_type="biology",
            targets=[{"as_written": "INV1", "hgnc_id": "HGNC:99999", "label": "INV1"}],
            direction="inhibit",
            agent={"as_written": "QF-101", "kind": "small_molecule",
                   "lab_key": f"lab:{INVENTED_PERSON.lower()}"},
            disease={"as_written": "invented carcinoma", "mondo_id": "MONDO:9999999"},
            stage="target_validation", evidence_grade="in_vivo", jhu_group_count=1,
            evidence_summary={"open_targets": {"association": 0.42}},
            data_flags=data_flags, evidence_status=evidence_status,
            version_hash=uuid.uuid4().hex,
        )
        db.add_all([extraction, idea])
        await db.flush()
        db.add(LandscapeIdeaEvidence(
            idea_id=idea.id, extraction_id=extraction.id, candidate_index=0,
        ))
        await db.commit()
        return epoch_id, idea.id


def _done(*sources: str) -> dict:
    return {source: {"status": "done", "attempts": 1} for source in sources}


def _user_message(request: dict) -> str:
    return request["messages"][0]["content"]


async def test_blind_dossier_has_no_identities(db_factory, submitted):
    epoch_id, _ = await _idea(db_factory, evidence_status=_done("open_targets", "chembl"))

    assert await triage.submit_triage_batch(db_factory, epoch_id) == 1

    [[(_, request)]] = submitted
    message = _user_message(request)
    for identity in (INVENTED_PERSON, INVENTED_PERSON.lower(), INVENTED_JOURNAL,
                     INVENTED_INSTITUTE, "R01"):
        assert identity not in message
    assert "INV1" in message
    assert QUOTE in message
    assert "<publication_abstract>" in message
    assert triage.triage_dossier(
        {"idea_type": "biology", "agent": {"as_written": "QF-101", "lab_key": "lab:x-lab"}},
        {}, [{"kind": "work", "document_type": "article", "date": "2026-03-01"}],
    ).count("lab:x-lab") == 0


async def test_s4_input_carries_data_flags(db_factory, submitted):
    flags = {
        "target_essential": {"raised": True, "evidence": "isEssential is true"},
        "safety_liabilities": {"raised": False, "evidence": "no safetyLiabilities"},
        "same_target_in_clinic": {"raised": False, "evidence": "no clinical candidate"},
    }
    epoch_id, _ = await _idea(
        db_factory, evidence_status=_done("open_targets"), data_flags=flags,
    )

    await triage.submit_triage_batch(db_factory, epoch_id)

    [[(_, request)]] = submitted
    message = _user_message(request)
    for key in flags:
        assert key in message
    assert "isEssential is true" in message
    assert "association" in message


async def test_failed_evidence_does_not_block_triage(db_factory, submitted):
    failed = {
        "open_targets": {"status": "failed", "attempts": 3},
        "chembl": {"status": "failed", "attempts": 3},
        "trials": {"status": "done", "attempts": 1},
    }
    epoch_id, ended_id = await _idea(db_factory, evidence_status=failed)
    await _idea(
        db_factory, epoch_id=epoch_id,
        evidence_status={"open_targets": {"status": "pending", "attempts": 1}},
    )
    await _idea(db_factory, epoch_id=epoch_id, evidence_status=None)

    assert await triage.submit_triage_batch(db_factory, epoch_id) == 1

    [[(row_id, request)]] = submitted
    message = _user_message(request)
    assert "Unavailable" in message
    assert "chembl" in message and "open_targets" in message
    async with db_factory() as db:
        rows = (
            await db.scalars(select(LandscapeTriage).where(LandscapeTriage.epoch_id == epoch_id))
        ).all()
    assert [(r.id, r.idea_id) for r in rows] == [(row_id, ended_id)]
    assert rows[0].raw == {"dossier": message}

    # The row exists now, so a second pass sends nothing new for that version.
    async with db_factory() as db:
        row = await db.get(LandscapeTriage, row_id)
        row.status = "submitted"
        await db.commit()
    assert await triage.submit_triage_batch(db_factory, epoch_id) == 0
    assert len(submitted) == 1


async def test_triage_scores_and_requests_with_the_epochs_inputs(
    db_factory, submitted, monkeypatch
):
    """The request's model, system prompt and schema, the fallback rebuild and the computed
    score all come from the epoch's snapshot, not the settings or files at the time of the
    call."""
    from src.config import get_settings

    stored_model = get_settings().landscape_triage_model
    epoch_id, _ = await _idea(db_factory, evidence_status=_done("open_targets"))
    monkeypatch.setattr(get_settings(), "landscape_triage_model", "claude-invented-later")

    assert await triage.submit_triage_batch(db_factory, epoch_id) == 1
    [[(row_id, request)]] = submitted
    async with db_factory() as db:
        inputs = await epochs.epoch_inputs(db, epoch_id)
        row = await db.get(LandscapeTriage, row_id)
        rebuilt = await triage.FALLBACKS[0].build(db, row)
    assert request["model"] == stored_model == rebuilt["model"]
    assert request["system"] == inputs.system("triage") == rebuilt["system"]
    schema = stage_schema("triage", inputs.rubric)
    assert request["output_config"]["format"]["schema"] == schema
    assert rebuilt["output_config"]["format"]["schema"] == schema

    scores = {"differentiation_unmet_need": 3, "scientific_credibility": 4,
              "translational_path": 2}
    values = triage._result_values({"scores": scores, "target_rubric": []}, inputs)
    assert values["triage_score"] == triage.triage_score(
        scores, [], rubric=inputs.rubric, criteria=inputs.criteria
    )
