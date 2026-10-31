"""S6 deep analysis against Postgres (spec §7.7, §6.2, §12.2; D12, D28, D33, D41, L31, L64).

Every outside call is faked: the model (``deep.call_sync``), the full-text fetch, the research
tools, UniProt, HPA and openFDA. The full text is stored by the real
``fulltext.store_full_text``. Every session shares one connection whose outer transaction
rolls back. Every name below is invented. Each epoch stores a snapshot of the judging inputs,
which the runner reads (spec §7.11).
"""
import contextlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import epochs, fulltext, liveness, panel, scoring
from src.landscaping.evidence import hpa, openfda, opentargets, orphadata, uniprot
from src.landscaping.fulltext import FullText
from src.landscaping.llm_client import CallResult
from src.landscaping.schemas import DISEASE_PROFILE_KEYS, GATING_KEYS
from src.landscaping.stages import deep
from src.models import SimulationProcessStatus
from src.models.landscape import (
    LandscapeConsult,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeText,
)
from src.services import project_search, simulation_control
from src.services.advisory_locks import ENGINE_LOCK_KEY
from src.services.blackbird_rubric import load_rubric

pytestmark = pytest.mark.integration

INVENTED_PERSON = "Quillfeather"
SUMMARY_QUOTE = "knockdown of INV1 reduced tumour growth in mice"
FULL_TEXT_QUOTE = "INV1 knockout organoids lost marsh-cell identity"
ENSEMBL = "ENSG00000999999"
UNIPROT = "P99999"
MONDO = "MONDO:9999999"
DRUG = "INVENTAMAB"
APPLICATION = "NDA000001"
RETRIEVED = "pmid:111"
#: The rubric on disk's dimensions, which ``epochs.build_snapshot`` stores.
DIMENSION_KEYS = tuple(dim.key for dim in load_rubric().dimensions)


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


# --- fake model messages --------------------------------------------------------------------


class _Block:
    def __init__(self, type, **fields):
        self.type = type
        self.__dict__.update(fields)

    def model_dump(self):
        return dict(self.__dict__)


class _Message:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"


def _text_result(body: str) -> CallResult:
    message = _Message([_Block("text", text=body)], "end_turn")
    return CallResult(message, body, "end_turn", None, "claude-opus-5", {}, None)


def _tool_result(n: int) -> CallResult:
    block = _Block("tool_use", id=f"tool-{n}", name="search_pubmed",
                   input={"query": "INV1 invented carcinoma"})
    return CallResult(_Message([block], "tool_use"), "", "tool_use", None, "claude-opus-5", {}, None)


def _extraction() -> dict:
    return {
        "ideas": [{
            "evidence_summary": "INV1 loss changes marsh-cell identity.",
            "target_rubric": [],
            "key_results": [
                {"claim": "Identity lost.", "quote": FULL_TEXT_QUOTE, "section": "results"},
                {"claim": "Invented.", "quote": "a sentence the paper never wrote",
                 "section": "results"},
            ],
        }],
        "linked_publications": [],
    }


def _answer(doc_ref: str, **over) -> dict:
    """A valid deep answer citing a mix of real and unretrieved references."""
    profile = {
        "indication": {"value": "invented carcinoma", "support": [f"mondo:{MONDO}"]},
        "prevalence": {"value": "1 in 10,000 in Europe", "support": [RETRIEVED]},
        "standard_of_care": {"value": "surgery", "support": [RETRIEVED]},
        "approved_drugs": {"value": DRUG, "support": [
            f"openfda:{APPLICATION}", RETRIEVED, "openfda:NDA999999",
        ]},
        "clinical_pipeline": {"value": "two phase 2 trials", "support": ["trials:HGNC:99999"]},
        "competing_targets": {"value": "INV2", "support": "estimate"},
        "unmet_need": {"value": "no therapy", "support": [doc_ref, "pmid:424242"]},
    }
    answer = {
        "summary": "INV1 inhibition for an invented carcinoma.",
        "disease_profile": profile,
        "target_biology": [
            {"line": "INV1 is an invented kinase.", "support": [f"uniprot:{UNIPROT}"]},
            {"line": "Expressed in marsh tissue.", "support": [f"hpa:{ENSEMBL}"]},
            {"line": "Tractable by small molecules.", "support": [f"opentargets:{ENSEMBL}"]},
            {"line": "Recalled from memory.", "support": "estimate"},
            {"line": "Read in a paper.", "support": [RETRIEVED]},
        ],
        "outside_groups": [
            {"finding": "An outside lab saw the same.", "support": [RETRIEVED]},
            {"finding": "Never retrieved.", "support": ["pmid:555"]},
        ],
        "gating": {key: "met" for key in GATING_KEYS},
        "gating_rationales": {key: "fine" for key in GATING_KEYS},
        "flag_review": [{"flag": "target_essential", "status": "cleared", "reason": "tissue"}],
        "scores": {key: 3 for key in DIMENSION_KEYS},
        "dimension_rationales": {key: "middling" for key in DIMENSION_KEYS},
        "red_flags": [],
        "confidence": "Moderate",
    }
    answer.update(over)
    return answer


_OPINION = json.dumps({
    "verdict_signal": "adequate", "concerns": [], "questions_to_ask": [], "confidence": "high",
})


@pytest.fixture
def fakes(monkeypatch):
    """Scripted outside calls; ``state`` sets the behaviour and records the calls."""
    state = {
        "live": False, "tool_rounds": 1, "answer": None, "fetch_basis": "full_text",
        "calls": [], "fetched": [], "tools": [], "uniprot": [], "hpa": [], "fda": [],
    }

    async def fake_live(db):
        return state["live"]

    async def fake_fetch(doc):
        state["fetched"].append(doc.id)
        if state["fetch_basis"] != "full_text":
            return FullText(f"title: {doc.title}", [], state["fetch_basis"], None, None, False)
        body = f"Results: {FULL_TEXT_QUOTE}. Further invented text."
        return FullText(body, [("results", 0, len(body))], "full_text", "pmc", "CC BY", False)

    async def fake_call(stage, request, *, db_factory, item_ref, **kwargs):
        state["calls"].append((stage, request))
        if stage == "deep_extract":
            return _text_result(json.dumps(_extraction()))
        if "format" not in request["output_config"]:
            return _text_result(_OPINION)
        rounds = sum(1 for s, r in state["calls"] if s == "deep" and "tools" in r)
        if "tools" in request and rounds <= state["tool_rounds"]:
            return _tool_result(rounds)
        return _text_result(json.dumps(state["answer"]))

    async def fake_tool(name, tool_input):
        state["tools"].append((name, dict(tool_input)))
        return project_search.ToolResult(
            "<paper_abstract>An outside study.</paper_abstract>",
            ({"key": RETRIEVED, "kind": "pubmed", "title": "An outside study"},),
        )

    async def fake_uniprot(accession):
        state["uniprot"].append(accession)
        return {"accession": accession, "protein_name": "Invented kinase", "gene": "INV1",
                "function": ["Phosphorylates invented substrates."]}

    async def fake_hpa(ensembl_id):
        state["hpa"].append(ensembl_id)
        return {"ensembl_id": ensembl_id, "tissue_specificity": "Marsh enriched"}

    async def fake_fda(name):
        state["fda"].append(name)
        return [{"application_number": APPLICATION, "sponsor_name": "Invented Pharma",
                 "brand_names": ["Inventa"], "generic_names": [name.lower()],
                 "marketing_statuses": ["Prescription"], "approval_date": "20200101"}]

    async def unexpected(*args, **kwargs):
        raise AssertionError("assembled evidence should have been used")

    monkeypatch.setattr(liveness, "simulation_live", fake_live)
    monkeypatch.setattr(fulltext, "fetch_full_text", fake_fetch)
    monkeypatch.setattr(deep, "call_sync", fake_call)
    monkeypatch.setattr(project_search, "execute_research_tool", fake_tool)
    monkeypatch.setattr(uniprot, "uniprot_function", fake_uniprot)
    monkeypatch.setattr(hpa, "hpa_expression", fake_hpa)
    monkeypatch.setattr(openfda, "fda_approvals", fake_fda)
    monkeypatch.setattr(opentargets, "ot_target", unexpected)
    monkeypatch.setattr(orphadata, "orphadata_prevalence", unexpected)
    return state


def _ot_target() -> dict:
    return {
        "id": ENSEMBL, "approvedSymbol": "INV1", "tractability": [{"label": "High"}],
        "drugAndClinicalCandidates": {"count": 1, "rows": [{
            "id": "row-1", "maxClinicalStage": "APPROVAL",
            "drug": {"id": "CHEMBL000", "name": DRUG},
            "diseases": [{"diseaseFromSource": "invented carcinoma",
                          "disease": {"id": "MONDO_9999999", "name": "invented carcinoma"}}],
        }]},
    }


async def _idea(
    db_factory, *, idea_type: str = "biology", orpha: bool = False, queued: bool = True,
    epoch_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One idea with one paper; ``queued`` adds the pending S6 row a cut would have made.
    Returns ``(epoch_id, idea_id, document_id)``."""
    disease = {"as_written": "invented carcinoma", "label": "invented carcinoma",
               "mondo_id": MONDO}
    summary = {"opentargets_target": {"HGNC:99999": _ot_target()},
               "trials": {"HGNC:99999": {"total": 2, "by_phase": {"PHASE2": 2}}}}
    if orpha:
        disease["orpha_code"] = "ORPHA:99999"
        summary["orphadata"] = {"prevalence": {"orpha_code": "99999", "prevalence": [
            {"class": "1-9 / 100 000", "geography": "Europe"},
        ]}}
    async with db_factory() as db:
        if epoch_id is None:
            epoch = LandscapeEpoch(status="candidate", snapshot=epochs.build_snapshot())
            db.add(epoch)
            await db.flush()
            epoch_id = epoch.id
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
            title="INV1 in an invented carcinoma", summary_text=f"{SUMMARY_QUOTE}.",
            pmcid="PMC0000001", published_on=date(2026, 3, 1), document_type="article",
            qualifying_authorships=[{"name": f"Ada {INVENTED_PERSON}"}],
            qualifying_institution="Invented Institute of Marsh Studies",
            lab_key=f"lab:{INVENTED_PERSON.lower()}", s1_status="eligible",
        )
        db.add(doc)
        await db.flush()
        extraction = LandscapeExtraction(
            document_id=doc.id, epoch_id=epoch_id, basis="summary", status="done",
            raw={"ideas": [{"evidence_summary": SUMMARY_QUOTE}], "linked_publications": []},
        )
        agent = {"as_written": "QF-101", "name": "QF-101", "kind": "small_molecule"}
        idea = LandscapeIdea(
            epoch_id=epoch_id, idea_key=f"{idea_type};INV1;{uuid.uuid4().hex}",
            idea_type=idea_type,
            targets=[{"as_written": "INV1", "hgnc_id": "HGNC:99999", "symbol": "INV1",
                      "ensembl_id": ENSEMBL, "uniprot_id": UNIPROT}],
            direction="inhibit", agent=agent if idea_type == "chemistry" else None,
            disease=disease, stage="target_validation", evidence_grade="in_vivo",
            jhu_group_count=1, evidence_summary=summary,
            data_flags={"target_essential": {"raised": True, "evidence": "isEssential"}},
            version_hash=uuid.uuid4().hex, in_current_cut=queued,
        )
        db.add_all([extraction, idea])
        await db.flush()
        db.add(LandscapeIdeaEvidence(idea_id=idea.id, extraction_id=extraction.id,
                                     candidate_index=0))
        if queued:
            db.add(LandscapeDeep(idea_id=idea.id, idea_version=idea.version_hash,
                                 epoch_id=epoch_id))
        await db.commit()
        return epoch_id, idea.id, doc.id


async def _deep_row(db_factory, idea_id) -> LandscapeDeep:
    async with db_factory() as db:
        return (await db.execute(
            select(LandscapeDeep).where(LandscapeDeep.idea_id == idea_id)
            .execution_options(populate_existing=True)
        )).scalar_one()


async def _consult_domains(db_factory, deep_id) -> set[str]:
    async with db_factory() as db:
        return set((await db.scalars(
            select(LandscapeConsult.domain).where(LandscapeConsult.deep_id == deep_id)
        )).all())


def _deep_requests(state) -> list[dict]:
    return [r for s, r in state["calls"] if s == "deep" and "format" in r["output_config"]]


def _dossier(state) -> str:
    return _deep_requests(state)[0]["messages"][0]["content"]


# --- tests --------------------------------------------------------------------------------------


async def test_chemistry_idea_adds_technologic(db_factory, fakes):
    epoch_id, chem_id, doc_id = await _idea(db_factory, idea_type="chemistry")
    _, bio_id, _ = await _idea(db_factory, epoch_id=epoch_id)
    fakes["answer"] = _answer(f"doc:{doc_id}")

    await deep.run_deep(db_factory, chem_id, epoch_id=epoch_id)
    await deep.run_deep(db_factory, bio_id, epoch_id=epoch_id)

    chem, bio = await _deep_row(db_factory, chem_id), await _deep_row(db_factory, bio_id)
    assert chem.status == bio.status == "done"
    assert await _consult_domains(db_factory, chem.id) == set(panel.PANEL_CHEMISTRY)
    assert await _consult_domains(db_factory, bio.id) == set(panel.PANEL_BIOLOGY)
    consult_systems = [r["system"] for s, r in fakes["calls"]
                       if s == "deep" and "format" not in r["output_config"]]
    assert panel.compose_persona("technologic") in consult_systems
    async with db_factory() as db:
        opinion = await db.scalar(select(LandscapeConsult.opinion).where(
            LandscapeConsult.deep_id == chem.id, LandscapeConsult.domain == "legal"))
    assert opinion["verdict_signal"] == "adequate"


async def test_consults_and_scores_use_the_epochs_inputs(db_factory, fakes):
    """A consult's persona is the one the epoch stored, not the file on disk, and the
    provisional score is weighed with the epoch's rubric (spec §7.11)."""
    snapshot = epochs.build_snapshot()
    snapshot["personas"] = {
        domain: f"An invented stored persona for {domain}." for domain in snapshot["personas"]
    }
    async with db_factory() as db:
        epoch = LandscapeEpoch(status="candidate", snapshot=snapshot)
        db.add(epoch)
        await db.commit()
        epoch_id = epoch.id
    _, idea_id, doc_id = await _idea(db_factory, epoch_id=epoch_id)
    fakes["answer"] = _answer(f"doc:{doc_id}")

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    row = await _deep_row(db_factory, idea_id)
    assert row.status == "done"
    consult_systems = [r["system"] for s, r in fakes["calls"]
                       if s == "deep" and "format" not in r["output_config"]]
    assert sorted(consult_systems) == sorted(
        f"An invented stored persona for {domain}." for domain in panel.PANEL_BIOLOGY
    )
    async with db_factory() as db:
        rubric = (await epochs.epoch_inputs(db, epoch_id)).rubric
    assert row.provisional_weighted_score == scoring.epoch_weighted_score(
        rubric, fakes["answer"]["scores"]
    )


async def test_s6_validates_and_stores_in_the_epochs_rubric_terms(db_factory, fakes):
    """An epoch whose stored rubric renames a dimension and widens the scale: the S6 request's
    schema, its validation and the stored scores follow the stored rubric, not the file on
    disk (spec §7.11, D61)."""
    renamed, invented = DIMENSION_KEYS[-1], "invented_team_dimension"
    snapshot = epochs.build_snapshot()
    rubric_text = snapshot["rubric"]
    for old, new in ((f'key = "{renamed}"', f'key = "{invented}"'),
                     (f'source = "{renamed}"', f'source = "{invented}"'),
                     ("max = 5", "max = 7")):
        assert old in rubric_text
        rubric_text = rubric_text.replace(old, new)
    snapshot["rubric"] = rubric_text
    async with db_factory() as db:
        epoch = LandscapeEpoch(status="candidate", snapshot=snapshot)
        db.add(epoch)
        await db.commit()
        epoch_id = epoch.id
    stored_keys = (*DIMENSION_KEYS[:-1], invented)

    _, idea_id, doc_id = await _idea(db_factory, epoch_id=epoch_id)
    fakes["answer"] = _answer(
        f"doc:{doc_id}",
        scores={key: 6 if key == invented else 3 for key in stored_keys},
        dimension_rationales={key: "middling" for key in stored_keys},
    )
    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    schema = _deep_requests(fakes)[0]["output_config"]["format"]["schema"]
    scores_schema = schema["properties"]["scores"]
    assert scores_schema["required"] == list(stored_keys)
    assert scores_schema["properties"][invented]["enum"] == list(range(1, 8))
    row = await _deep_row(db_factory, idea_id)
    assert row.status == "done"
    assert row.provisional_scores == {key: 6 if key == invented else 3 for key in stored_keys}
    assert set(row.dimension_rationales) == set(stored_keys)
    async with db_factory() as db:
        rubric = (await epochs.epoch_inputs(db, epoch_id)).rubric
    assert row.provisional_weighted_score == scoring.epoch_weighted_score(
        rubric, row.provisional_scores
    )

    # An answer in the disk rubric's terms lacks the stored dimension: it is not valid.
    _, disk_idea_id, disk_doc_id = await _idea(db_factory, epoch_id=epoch_id)
    fakes["answer"] = _answer(f"doc:{disk_doc_id}")
    await deep.run_deep(db_factory, disk_idea_id, epoch_id=epoch_id)
    assert (await _deep_row(db_factory, disk_idea_id)).status == "failed:invalid"


async def test_disease_profile_every_field_has_support(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    answer = _answer(f"doc:{doc_id}")
    del answer["disease_profile"]["competing_targets"]
    fakes["answer"] = answer

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    profile = (await _deep_row(db_factory, idea_id)).disease_profile
    assert set(profile) == set(DISEASE_PROFILE_KEYS)
    for entry in profile.values():
        support = entry["support"]
        assert support == "estimate" or (
            isinstance(support, list) and support and all(isinstance(s, str) for s in support)
        )
    assert profile["competing_targets"]["support"] == "estimate"
    assert profile["indication"]["support"] == [f"mondo:{MONDO}"]
    assert profile["unmet_need"]["support"] == [f"doc:{doc_id}"]


async def test_prevalence_without_orphadata_is_estimate(db_factory, fakes):
    epoch_id, plain_id, doc_id = await _idea(db_factory)
    _, rare_id, _ = await _idea(db_factory, orpha=True, epoch_id=epoch_id)
    rare_profile = _answer(f"doc:{doc_id}")["disease_profile"]
    rare_profile["prevalence"]["support"] = ["orphadata:99999", RETRIEVED]

    fakes["answer"] = _answer(f"doc:{doc_id}")
    await deep.run_deep(db_factory, plain_id, epoch_id=epoch_id)
    fakes["answer"] = _answer(f"doc:{doc_id}", disease_profile=rare_profile)
    await deep.run_deep(db_factory, rare_id, epoch_id=epoch_id)

    plain = (await _deep_row(db_factory, plain_id)).disease_profile
    rare = (await _deep_row(db_factory, rare_id)).disease_profile
    assert plain["prevalence"]["support"] == "estimate"
    assert rare["prevalence"]["support"] == ["orphadata:99999"]


async def test_target_biology_lines_cite_uniprot_hpa_and_open_targets(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    fakes["answer"] = _answer(f"doc:{doc_id}")

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    assert fakes["uniprot"] == [UNIPROT]
    assert fakes["hpa"] == [ENSEMBL]
    dossier = _dossier(fakes)
    for ref in (f"uniprot:{UNIPROT}", f"hpa:{ENSEMBL}", f"opentargets:{ENSEMBL}"):
        assert f"`{ref}`" in dossier
    lines = (await _deep_row(db_factory, idea_id)).target_biology
    assert lines == [
        {"line": "INV1 is an invented kinase.", "support": [f"uniprot:{UNIPROT}"]},
        {"line": "Expressed in marsh tissue.", "support": [f"hpa:{ENSEMBL}"]},
        {"line": "Tractable by small molecules.", "support": [f"opentargets:{ENSEMBL}"]},
    ]


async def test_approved_drugs_come_from_openfda(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    fakes["answer"] = _answer(f"doc:{doc_id}")

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    assert fakes["fda"] == [DRUG]
    assert f"`openfda:{APPLICATION}`" in _dossier(fakes)
    profile = (await _deep_row(db_factory, idea_id)).disease_profile
    assert profile["approved_drugs"]["support"] == [f"openfda:{APPLICATION}"]


async def test_literature_loop_cites_only_retrieved(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    fakes["answer"] = _answer(f"doc:{doc_id}")

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    assert fakes["tools"] == [("search_pubmed", {"query": "INV1 invented carcinoma"})]
    second = _deep_requests(fakes)[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result"
    row = await _deep_row(db_factory, idea_id)
    assert row.search_transcript[0]["sources"] == [RETRIEVED]
    assert row.outside_group_count == 1
    assert row.disease_profile["standard_of_care"]["support"] == [RETRIEVED]
    assert row.disease_profile["unmet_need"]["support"] == [f"doc:{doc_id}"]
    async with db_factory() as db:
        assert await db.scalar(
            select(LandscapeIdea.outside_group_count).where(LandscapeIdea.id == idea_id)
        ) == 1


async def test_literature_loop_stops_after_eight_rounds(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    fakes["answer"] = _answer(f"doc:{doc_id}")
    fakes["tool_rounds"] = 50

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    requests = _deep_requests(fakes)
    assert len(requests) == deep.MAX_SEARCH_ROUNDS + 1
    assert "tools" not in requests[-1]
    assert (await _deep_row(db_factory, idea_id)).status == "done"


async def test_full_text_evidence_does_not_bump_version(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    fakes["answer"] = _answer(f"doc:{doc_id}")
    async with db_factory() as db:
        version = await db.scalar(
            select(LandscapeIdea.version_hash).where(LandscapeIdea.id == idea_id))
        links = await db.scalar(select(func.count()).select_from(LandscapeIdeaEvidence))

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    assert FULL_TEXT_QUOTE in _dossier(fakes)
    assert "<methods_section>" in _dossier(fakes)
    async with db_factory() as db:
        assert await db.scalar(
            select(LandscapeIdea.version_hash).where(LandscapeIdea.id == idea_id)) == version
        assert await db.scalar(select(func.count()).select_from(LandscapeIdeaEvidence)) == links
        full = (await db.execute(select(LandscapeExtraction).where(
            LandscapeExtraction.document_id == doc_id,
            LandscapeExtraction.basis == "full_text"))).scalar_one()
        stored = (await db.execute(select(LandscapeText).where(
            LandscapeText.document_id == doc_id))).scalar_one()
    assert full.status == "done"
    assert (full.quotes_verified, full.quotes_total) == (1, 2)
    assert stored.text_basis == "full_text"
    row = await _deep_row(db_factory, idea_id)
    assert row.idea_version == version
    [descriptor] = row.full_text_evidence["documents"]
    assert descriptor["basis"] == "full_text"
    assert descriptor["extraction_id"] == str(full.id)


async def test_transient_fetch_failure_retries_before_summary(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    fakes["answer"] = _answer(f"doc:{doc_id}")
    fakes["fetch_basis"] = "fetch_failed"

    for _ in range(deep.FULL_TEXT_MAX_FETCHES - 1):
        await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)
        assert (await _deep_row(db_factory, idea_id)).status == "pending"
    assert fakes["calls"] == []

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    assert len(fakes["fetched"]) == deep.FULL_TEXT_MAX_FETCHES
    assert not [s for s, _ in fakes["calls"] if s == "deep_extract"]
    row = await _deep_row(db_factory, idea_id)
    assert row.status == "done"
    assert row.full_text_evidence["documents"][0]["basis"] == "fetch_failed"


async def test_deep_gate_not_met_excludes(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    gating = {key: "met" for key in GATING_KEYS}
    gating[GATING_KEYS[0]] = "not_met"
    rationales = {key: "fine" for key in GATING_KEYS}
    rationales[GATING_KEYS[0]] = "An invented positive fact fails this gate."
    fakes["answer"] = _answer(f"doc:{doc_id}", gating=gating, gating_rationales=rationales)

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)

    row = await _deep_row(db_factory, idea_id)
    assert row.status == "excluded:deep_gate"
    assert row.gating[GATING_KEYS[0]] == "not_met"
    assert row.gating_rationales[GATING_KEYS[0]] == rationales[GATING_KEYS[0]]
    assert await _consult_domains(db_factory, row.id) == set()
    assert all("format" in r["output_config"] for s, r in fakes["calls"] if s == "deep")


async def test_waits_while_simulation_live(db_factory, fakes):
    epoch_id, idea_id, doc_id = await _idea(db_factory)
    _, probe_id, _ = await _idea(db_factory, queued=False, epoch_id=epoch_id)
    fakes["answer"] = _answer(f"doc:{doc_id}")
    fakes["live"] = True

    await deep.run_deep(db_factory, idea_id, epoch_id=epoch_id)
    await deep.run_deep(db_factory, probe_id, epoch_id=epoch_id)

    assert fakes["calls"] == [] and fakes["fetched"] == [] and fakes["tools"] == []
    assert (await _deep_row(db_factory, idea_id)).status == "pending"
    async with db_factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(LandscapeDeep)
            .where(LandscapeDeep.idea_id == probe_id)) == 0


async def test_live_means_engine_lock_held(db_factory):
    async with db_factory() as db:
        assert await liveness.simulation_live(db) == await simulation_control.engine_alive(db)
        # A transaction-level hold of the engine key shows in pg_locks exactly as the
        # engine's session-level lock does, and is released by the test's rollback.
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ENGINE_LOCK_KEY})
        await db.merge(SimulationProcessStatus(
            id=1, state="running", updated_at=datetime.now(UTC) - timedelta(hours=2)))
        await db.flush()

        assert await simulation_control.engine_alive(db) is True
        assert await liveness.simulation_live(db) is True


async def test_probe_idea_gets_full_text(db_factory, fakes):
    epoch_id, probe_id, doc_id = await _idea(db_factory, queued=False)
    fakes["answer"] = _answer(f"doc:{doc_id}")

    await deep.run_deep(db_factory, probe_id, epoch_id=epoch_id)

    assert fakes["fetched"] == [doc_id]
    assert [s for s, _ in fakes["calls"]].count("deep_extract") == 1
    row = await _deep_row(db_factory, probe_id)
    assert row.cut_id is None
    assert row.status == "done"
    assert row.full_text_evidence["documents"][0]["basis"] == "full_text"
