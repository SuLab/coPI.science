"""Ideas against Postgres (plan Task 4.3; spec §7.4 steps 5-7; D9, D27, D35, D36, D37, D64,
L21, L59).

Candidates are built as S3 hands them over after normalization; the evidence clients, the
evidence cache and normalization are faked. Every name and identifier is invented.
"""
import contextlib
import itertools
import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import epochs, flags, normalize
from src.landscaping.criteria import load_criteria
from src.landscaping.evidence import cache, chembl, comention, opentargets, orphadata, trials
from src.landscaping.ideas import (
    DOSSIER_CAP,
    EVIDENCE_SOURCES,
    apply_lab_unifications,
    assemble_evidence,
    dossier_documents,
    group_candidates,
    group_counts,
    idea_key,
    merge_candidate,
    resolve_aliases,
    version_hash,
)
from src.landscaping.stages import cut as cut_stage
from src.landscaping.stages import triage as triage_stage
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeCut,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaAlias,
    LandscapeIdeaEvidence,
    LandscapeLab,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeReview,
    LandscapeTriage,
)

pytestmark = pytest.mark.integration

_n = itertools.count()
LAB_A = "orcid:0000-0002-0000-0001"
LAB_B = "orcid:0000-0002-0000-0002"
NAME_LAB = "name:quillfeather|a"
TARGET = {"as_written": "KINX", "kind": "gene", "species": "human", "hgnc_id": "HGNC:90001",
          "ensembl_id": "ENSG00000900001", "symbol": "KINX"}
DISEASE = {"as_written": "invented fibrosis", "mondo_id": "MONDO:0900001",
           "mesh_id": "D900001", "label": "invented fibrosis", "orpha_code": "990001"}


def _bio(direction: str = "inhibit", *, target: dict = TARGET, disease: dict = DISEASE) -> dict:
    return {"idea_type": "biology", "targets": [dict(target)], "direction": direction,
            "agent": {"as_written": "", "kind": "other"}, "disease": dict(disease),
            "modality": {"as_written": "small molecule", "basis": "proposed"},
            "stage": "target_validation", "evidence_grade": "in_vivo"}


def _chem(agent: str = "compound QX-7", *, chembl_id: str | None = None) -> dict:
    agent_dict = {"as_written": agent, "kind": "small_molecule"}
    if chembl_id:
        agent_dict["chembl_id"] = chembl_id
    return {"idea_type": "chemistry", "targets": [dict(TARGET)], "direction": "inhibit",
            "agent": agent_dict, "disease": dict(DISEASE),
            "modality": {"as_written": "small molecule", "basis": "tested"},
            "stage": "lead", "evidence_grade": "in_vitro"}


async def _epoch(db: AsyncSession, status: str = "active") -> LandscapeEpoch:
    """An epoch with a stored snapshot: alias resolution normalizes with its criteria."""
    epoch = LandscapeEpoch(
        id=uuid.uuid4(), status=status, components={}, snapshot=epochs.build_snapshot()
    )
    db.add(epoch)
    await db.flush()
    return epoch


async def _doc(
    db: AsyncSession, *, kind: str = "work", s1: str = "eligible", lab: str | None = LAB_A,
    published: date | None = date(2026, 1, 15),
) -> LandscapeDocument:
    doc = LandscapeDocument(
        kind=kind, canonical_external_id=f"invented-ideas-{next(_n)}-{uuid.uuid4().hex[:8]}",
        s1_status=s1, lab_key=lab, published_on=published, title="An invented finding",
    )
    db.add(doc)
    await db.flush()
    return doc


async def _merge(
    db: AsyncSession, epoch: LandscapeEpoch, doc: LandscapeDocument, candidate: dict,
    *, index: int = 0, extraction: LandscapeExtraction | None = None,
) -> uuid.UUID:
    if extraction is None:
        extraction = await _extraction(db, epoch, doc)
    return await merge_candidate(
        db, epoch_id=epoch.id, extraction_id=extraction.id, index=index,
        candidate=candidate, document=doc,
    )


async def _extraction(db: AsyncSession, epoch: LandscapeEpoch, doc: LandscapeDocument):
    row = LandscapeExtraction(document_id=doc.id, epoch_id=epoch.id, basis="summary",
                              status="done")
    db.add(row)
    await db.flush()
    return row


async def _idea(db: AsyncSession, idea_id: uuid.UUID) -> LandscapeIdea | None:
    return (await db.execute(
        select(LandscapeIdea).where(LandscapeIdea.id == idea_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


@pytest_asyncio.fixture
async def db_factory(engine):
    """Sessions on one connection whose outer transaction rolls back; commits are savepoint
    releases, as in test_intake."""
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


# --- canonical keys and the L21 rule ------------------------------------------------------------


async def test_unknown_direction_joins_single_known(db_session):
    epoch = await _epoch(db_session)
    known = await _merge(db_session, epoch, await _doc(db_session), _bio("inhibit"))
    joined = await _merge(db_session, epoch, await _doc(db_session), _bio("unknown"))
    assert joined == known
    reasons = (await db_session.execute(
        select(LandscapeIdeaEvidence.merge_reason).where(LandscapeIdeaEvidence.idea_id == known)
    )).scalars().all()
    assert sorted(reasons) == ["key", "unknown_direction_joined"]


async def test_unknown_direction_stays_when_two_known(db_session):
    epoch = await _epoch(db_session)
    a = await _merge(db_session, epoch, await _doc(db_session), _bio("inhibit"))
    b = await _merge(db_session, epoch, await _doc(db_session), _bio("activate"))
    own = await _merge(db_session, epoch, await _doc(db_session), _bio("unknown"))
    assert own not in (a, b)
    idea = await _idea(db_session, own)
    assert idea.direction == "unknown"
    assert idea.idea_key.split(";")[2] == "unknown"


async def test_attachment_not_revisited(db_session):
    epoch = await _epoch(db_session)
    known = await _merge(db_session, epoch, await _doc(db_session), _bio("inhibit"))
    doc = await _doc(db_session)
    extraction = await _extraction(db_session, epoch, doc)
    joined = await _merge(db_session, epoch, doc, _bio("unknown"), extraction=extraction)
    assert joined == known
    # A second direction appears: the earlier attachment is not revisited (D27).
    await _merge(db_session, epoch, await _doc(db_session), _bio("activate"))
    again = await _merge(db_session, epoch, doc, _bio("unknown"), extraction=extraction)
    assert again == known
    rows = (await db_session.execute(
        select(LandscapeIdeaEvidence).where(LandscapeIdeaEvidence.extraction_id == extraction.id)
    )).scalars().all()
    assert [r.idea_id for r in rows] == [known]


async def test_rekeyed_candidate_refreshes_the_idea_it_leaves(db_session):
    epoch = await _epoch(db_session)
    doc = await _doc(db_session, lab=LAB_A)
    extraction = await _extraction(db_session, epoch, doc)
    left = await _merge(db_session, epoch, doc, _bio("inhibit"), extraction=extraction)
    other_doc = await _doc(db_session, lab=LAB_B)
    assert await _merge(db_session, epoch, other_doc, _bio("inhibit")) == left
    assert (await _idea(db_session, left)).jhu_group_count == 2

    # The document's summary changed: its candidate now keys to another idea.
    moved = await _merge(db_session, epoch, doc, _bio("activate"), extraction=extraction)

    assert moved != left
    idea = await _idea(db_session, left)
    assert idea.version_hash == version_hash([other_doc.id])
    assert idea.jhu_group_count == 1


async def test_idea_left_with_no_qualifying_document_has_no_version(db_session):
    """Re-keying an idea's only document leaves it with no version, not the hash of nothing,
    so S4 creates no row for it (an empty dossier for an idea the S5 pool never takes)."""
    epoch = await _epoch(db_session)
    doc = await _doc(db_session)
    extraction = await _extraction(db_session, epoch, doc)
    left = await _merge(db_session, epoch, doc, _bio("inhibit"), extraction=extraction)
    assert (await _idea(db_session, left)).version_hash == version_hash([doc.id])

    moved = await _merge(db_session, epoch, doc, _bio("activate"), extraction=extraction)

    assert moved != left
    idea = await _idea(db_session, left)
    assert idea.version_hash is None
    assert idea.jhu_group_count == 0
    idea.evidence_status = {source: {"status": "done"} for source in EVIDENCE_SOURCES}
    await db_session.flush()
    due = await triage_stage._due_rows(db_session, epoch.id)
    assert left not in {row.idea_id for row, _ in due}
    rows = (await db_session.execute(
        select(LandscapeTriage.id).where(LandscapeTriage.idea_id == left)
    )).scalars().all()
    assert rows == []


async def test_unregistered_agent_key_includes_lab(db_session):
    unregistered = idea_key(_chem(), senior_lab=LAB_A)
    assert f"@{LAB_A}" in unregistered
    assert idea_key(_chem(), senior_lab=LAB_B) != unregistered
    registered = idea_key(_chem(chembl_id="CHEMBL9000001"), senior_lab=LAB_A)
    assert registered == idea_key(_chem(chembl_id="CHEMBL9000001"), senior_lab=LAB_B)
    assert LAB_A not in registered

    # Merging reads the document's canonical lab key at merge time.
    db_session.add_all([LandscapeLab(lab_key=LAB_A), LandscapeLab(lab_key=NAME_LAB, alias_of=LAB_A)])
    await db_session.flush()
    epoch = await _epoch(db_session)
    idea_id = await _merge(db_session, epoch, await _doc(db_session, lab=NAME_LAB), _chem())
    idea = await _idea(db_session, idea_id)
    assert idea.idea_key == unregistered
    assert idea.agent["lab_key"] == LAB_A
    assert idea.provisional is True


async def test_group_candidates_matches_merge_rule(db_session):
    epoch = await _epoch(db_session)
    other_disease = {"as_written": "invented neuropathy", "mondo_id": "MONDO:0900002"}
    specs = [
        (LAB_A, _bio("inhibit")),
        (LAB_B, _bio("unknown")),
        (LAB_A, _bio("activate", disease=other_disease)),
        (LAB_B, _bio("inhibit", disease=other_disease)),
        (LAB_A, _bio("unknown", disease=other_disease)),
        (LAB_A, _chem()),
        (LAB_B, _chem()),
        (LAB_B, _chem(chembl_id="CHEMBL9000001")),
        (LAB_A, _chem(chembl_id="CHEMBL9000001")),
    ]
    candidates, senior_labs, merged = [], {}, {}
    for lab, candidate in specs:
        doc = await _doc(db_session, lab=lab)
        senior_labs[doc.id] = lab
        candidates.append({**candidate, "document_id": doc.id})
        idea_id = await _merge(db_session, epoch, doc, candidate)
        merged.setdefault(idea_id, set()).add(doc.id)

    groups = group_candidates(candidates, senior_labs=senior_labs)
    in_memory = {frozenset(c["document_id"] for c in members) for members in groups.values()}
    assert in_memory == {frozenset(docs) for docs in merged.values()}
    keys = (await db_session.execute(
        select(LandscapeIdea.idea_key).where(LandscapeIdea.epoch_id == epoch.id)
    )).scalars().all()
    assert set(groups) == set(keys)


# --- aliases ------------------------------------------------------------------------------------


async def test_alias_merge_keeps_reviews_visible(db_session, monkeypatch):
    epoch = await _epoch(db_session)
    normalized = await _merge(db_session, epoch, await _doc(db_session), _bio("inhibit"))
    unmatched = {"as_written": "Kinx-like", "kind": "gene", "species": "human",
                 "provisional_key": "prov:target:kinx-like"}
    prov_doc = await _doc(db_session, lab=LAB_B)
    provisional = await _merge(db_session, epoch, prov_doc, _bio("inhibit", target=unmatched))
    prov_idea = await _idea(db_session, provisional)
    assert prov_idea.provisional is True
    old_key, old_version = prov_idea.idea_key, prov_idea.version_hash

    # An opportunity, assessed and reviewed, built on the provisional idea.
    opportunity = LandscapeOpportunity(version_counter=1)
    db_session.add(opportunity)
    await db_session.flush()
    db_session.add(LandscapeOpportunityVersion(
        opportunity_id=opportunity.id, version=1, epoch_id=epoch.id,
        member_idea_keys=[old_key], status="done",
    ))
    assessment = LandscapeAssessment(
        opportunity_id=opportunity.id, opportunity_version=1, epoch_id=epoch.id, status="done",
    )
    db_session.add(assessment)
    await db_session.flush()
    db_session.add(LandscapeReview(
        opportunity_id=opportunity.id, reviewer_name="Invented Reviewer", score=4,
        landscape_assessment_id=assessment.id,
    ))
    db_session.add(LandscapeTriage(idea_id=provisional, idea_version=old_version,
                                   epoch_id=epoch.id, status="done"))
    await db_session.flush()

    seen_criteria = []

    async def targets(items, *, pubtator, criteria):
        seen_criteria.append(criteria)
        return [{**t, "hgnc_id": TARGET["hgnc_id"]} for t in items]

    async def disease(item, *, pubtator):
        return item

    monkeypatch.setattr(normalize, "normalize_targets", targets)
    monkeypatch.setattr(normalize, "normalize_disease", disease)

    assert await resolve_aliases(db_session, epoch_id=epoch.id) == 1
    # Normalization uses the epoch's stored criteria, not the file on disk.
    stored = (await epochs.epoch_inputs(db_session, epoch.id)).criteria
    assert seen_criteria and all(c is stored for c in seen_criteria)
    assert await _idea(db_session, provisional) is None
    survivor = await _idea(db_session, normalized)
    alias = (await db_session.execute(
        select(LandscapeIdeaAlias).where(
            LandscapeIdeaAlias.epoch_id == epoch.id, LandscapeIdeaAlias.old_key == old_key
        )
    )).scalar_one()
    assert alias.idea_id == normalized
    triage = (await db_session.execute(
        select(LandscapeTriage).where(LandscapeTriage.epoch_id == epoch.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert triage.idea_id == normalized
    # The provisional idea's document is now the survivor's: a new version (D37).
    assert prov_doc.id in {
        r.document_id for r in (await db_session.execute(
            select(LandscapeExtraction)
            .join(LandscapeIdeaEvidence, LandscapeIdeaEvidence.extraction_id == LandscapeExtraction.id)
            .where(LandscapeIdeaEvidence.idea_id == normalized)
        )).scalars()
    }
    assert survivor.jhu_group_count == 2

    # The review is still reachable: review -> opportunity version -> member key -> alias -> idea.
    reachable = (await db_session.execute(
        select(LandscapeReview.id, LandscapeIdea.id)
        .join(LandscapeOpportunityVersion,
              LandscapeOpportunityVersion.opportunity_id == LandscapeReview.opportunity_id)
        .join(LandscapeIdeaAlias, and_(
            LandscapeIdeaAlias.epoch_id == LandscapeOpportunityVersion.epoch_id,
            LandscapeOpportunityVersion.member_idea_keys.has_key(LandscapeIdeaAlias.old_key),
        ))
        .join(LandscapeIdea, LandscapeIdea.id == LandscapeIdeaAlias.idea_id)
        .where(LandscapeReview.landscape_assessment_id == assessment.id)
    )).all()
    assert [idea for _, idea in reachable] == [normalized]
    # Resolving again moves nothing.
    assert await resolve_aliases(db_session, epoch_id=epoch.id) == 0


async def test_lab_unification_rekeys_chemistry_idea_via_alias(db_session):
    db_session.add_all([LandscapeLab(lab_key=LAB_A), LandscapeLab(lab_key=NAME_LAB)])
    await db_session.flush()
    active = await _epoch(db_session, "active")
    candidate = await _epoch(db_session, "candidate")
    # Active: the canonical lab already has the idea, so the name-keyed one merges into it.
    canonical_active = await _merge(db_session, active, await _doc(db_session, lab=LAB_A), _chem())
    named_active = await _merge(db_session, active, await _doc(db_session, lab=NAME_LAB), _chem())
    # Candidate: only the name-keyed idea exists, so it is re-keyed in place.
    named_candidate = await _merge(
        db_session, candidate, await _doc(db_session, lab=NAME_LAB), _chem()
    )
    old_key = idea_key(_chem(), senior_lab=NAME_LAB)
    new_key = idea_key(_chem(), senior_lab=LAB_A)
    assert (await _idea(db_session, named_candidate)).idea_key == old_key

    await db_session.execute(
        update(LandscapeLab).where(LandscapeLab.lab_key == NAME_LAB).values(alias_of=LAB_A)
    )
    assert await apply_lab_unifications(db_session) == 2

    assert await _idea(db_session, named_active) is None
    rekeyed = await _idea(db_session, named_candidate)
    assert rekeyed.idea_key == new_key
    assert rekeyed.agent["lab_key"] == LAB_A
    aliases = {
        (a.epoch_id, a.old_key): a.idea_id
        for a in (await db_session.execute(select(LandscapeIdeaAlias))).scalars()
    }
    assert aliases[(active.id, old_key)] == canonical_active
    assert aliases[(candidate.id, old_key)] == named_candidate
    # Idempotent; and a later mention from the aliased lab lands on the canonical idea.
    assert await apply_lab_unifications(db_session) == 0
    later = await _merge(db_session, active, await _doc(db_session, lab=NAME_LAB), _chem())
    assert later == canonical_active


def _resolve_kinx(monkeypatch) -> None:
    """Normalization resolves every unmatched target to ``TARGET``'s HGNC id."""
    async def targets(items, *, pubtator, criteria):
        return [{**t, "hgnc_id": TARGET["hgnc_id"]} for t in items]

    async def disease(item, *, pubtator):
        return item

    monkeypatch.setattr(normalize, "normalize_targets", targets)
    monkeypatch.setattr(normalize, "normalize_disease", disease)


async def _survivor_and_merged(db: AsyncSession, epoch: LandscapeEpoch) -> tuple:
    """A normalized idea and a provisional one that resolves into it (ids, then versions)."""
    survivor = await _merge(db, epoch, await _doc(db), _bio("inhibit"))
    unmatched = {"as_written": "Kinx-like", "kind": "gene", "species": "human",
                 "provisional_key": "prov:target:kinx-like"}
    merged = await _merge(db, epoch, await _doc(db, lab=LAB_B),
                          _bio("inhibit", target=unmatched))
    versions = [(await _idea(db, i)).version_hash for i in (survivor, merged)]
    return survivor, merged, *versions


async def test_merge_drops_pending_cut_whose_tie_class_held_the_merged_idea(
    db_factory, monkeypatch,
):
    _resolve_kinx(monkeypatch)
    async with db_factory() as db:
        epoch = await _epoch(db)
        survivor, merged, v_survivor, v_merged = await _survivor_and_merged(db, epoch)
        ids = [str(survivor), str(merged)]
        pending = LandscapeCut(
            epoch_id=epoch.id, status="boundary_pending", n=2, k=1, ordered_idea_ids=ids,
            folds={"folded": {}, "tie_class": {
                "ids": ids, "versions": {ids[0]: v_survivor, ids[1]: v_merged},
            }},
            tie_class_size=2,
        )
        db.add(pending)
        await db.commit()
        epoch_id, pending_id = epoch.id, pending.id

    async with db_factory() as db:
        assert await resolve_aliases(db, epoch_id=epoch_id) == 1
        await db.commit()
    async with db_factory() as db:
        assert await db.get(LandscapeCut, pending_id) is None

    # The epoch is no longer held behind a tournament over the deleted idea.
    cut_id = await cut_stage.run_cut(db_factory, epoch_id, may_submit=False)
    assert cut_id is not None
    async with db_factory() as db:
        assert (await db.get(LandscapeCut, cut_id)).status == "complete"


async def test_merge_repoints_complete_cut_folds_to_the_survivor(db_session, monkeypatch):
    _resolve_kinx(monkeypatch)
    epoch = await _epoch(db_session)
    survivor, merged, _, _ = await _survivor_and_merged(db_session, epoch)
    keeper, folded_a, folded_b = (str(uuid.uuid4()) for _ in range(3))
    db_session.add(LandscapeCut(
        epoch_id=epoch.id, status="complete", n=2, k=1,
        ordered_idea_ids=[str(merged), keeper],
        # The survivor was folded under the merged idea, which it now replaces as keeper.
        folds={"folded": {str(merged): [str(survivor), folded_a], keeper: [folded_b]}},
    ))
    await db_session.flush()

    assert await resolve_aliases(db_session, epoch_id=epoch.id) == 1

    cut = (await db_session.execute(
        select(LandscapeCut).where(LandscapeCut.epoch_id == epoch.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert cut.ordered_idea_ids == [str(survivor), keeper]
    assert cut.folds["folded"] == {str(survivor): [folded_a], keeper: [folded_b]}


# --- versions, tombstones, dossiers -------------------------------------------------------------


async def test_db_refresh_does_not_bump_version(db_factory, monkeypatch):
    calls = _fake_evidence(monkeypatch)
    async with db_factory() as db:
        epoch = await _epoch(db)
        idea_id = await _merge(db, epoch, await _doc(db), _bio())
        version = (await _idea(db, idea_id)).version_hash
        # A new evidence-only document (a review) updates the dossier, not the version.
        review = await _doc(db, s1="excluded:type_review_evidence_only")
        assert await _merge(db, epoch, review, _bio()) == idea_id
        assert (await _idea(db, idea_id)).version_hash == version
        await db.commit()

    await assemble_evidence(db_factory, epoch_id=epoch.id, idea_id=idea_id)
    calls["essential"] = False
    async with db_factory() as db:
        idea = await _idea(db, idea_id)
        assert idea.evidence_summary["opentargets_target"][TARGET["hgnc_id"]]["isEssential"]
        # A refresh: every source back to pending.
        idea.evidence_status = None
        await db.commit()
    await assemble_evidence(db_factory, epoch_id=epoch.id, idea_id=idea_id)
    async with db_factory() as db:
        idea = await _idea(db, idea_id)
        assert idea.evidence_summary["opentargets_target"][TARGET["hgnc_id"]]["isEssential"] is False
        assert idea.data_flags["target_essential"]["raised"] is False
        assert idea.version_hash == version


async def test_tombstones_never_counted(db_session):
    epoch = await _epoch(db_session)
    kept = await _doc(db_session, lab=LAB_A)
    dropped = await _doc(db_session, lab=LAB_B)
    idea_id = await _merge(db_session, epoch, kept, _bio())
    await _merge(db_session, epoch, dropped, _bio())
    dropped.merged_into = kept.id
    dropped.s1_status = "excluded:merged"
    await db_session.flush()

    idea = await _idea(db_session, idea_id)
    documents = [kept, dropped]
    assert group_counts(idea, documents) == (1, 0)
    assert dropped not in dossier_documents(idea, documents)
    # The next attachment's hash counts the kept row once and never the tombstone.
    third = await _doc(db_session, lab=LAB_A)
    await _merge(db_session, epoch, third, _bio())
    idea = await _idea(db_session, idea_id)
    assert idea.version_hash == version_hash([kept.id, third.id])
    assert idea.version_hash == version_hash([third.id, kept.id, kept.id])
    assert idea.jhu_group_count == 1


async def test_tombstoning_keeps_version(db_session):
    epoch = await _epoch(db_session)
    kept, dropped = await _doc(db_session), await _doc(db_session, lab=LAB_B)
    idea_id = await _merge(db_session, epoch, kept, _bio())
    await _merge(db_session, epoch, dropped, _bio())
    before = (await _idea(db_session, idea_id)).version_hash
    assert before == version_hash([kept.id, dropped.id])
    await db_session.execute(
        update(LandscapeDocument).where(LandscapeDocument.id == dropped.id)
        .values(merged_into=kept.id, s1_status="excluded:merged")
    )
    # An evidence-only attachment afterwards still leaves the stored version alone.
    await _merge(db_session, epoch, await _doc(db_session, s1="excluded:dataset_evidence_only",
                                               kind="dataset"), _bio())
    assert (await _idea(db_session, idea_id)).version_hash == before


def test_dossier_cap_order():
    base = date(2026, 6, 1)

    def doc(kind, days, s1="eligible", merged_into=None):
        return {"id": uuid.uuid4(), "kind": kind, "s1_status": s1, "merged_into": merged_into,
                "published_on": base - timedelta(days=days), "lab_key": LAB_A}

    lead = [doc("grant", 300), doc("patent", 10), doc("jhtv", 50), doc("grant", 5)]
    papers = [doc("work", i) for i in range(1, 24)]
    review = doc("work", 0, s1="excluded:type_review_evidence_only")
    tomb = doc("patent", 0, s1="excluded:merged", merged_into=uuid.uuid4())
    picked = dossier_documents(None, [*papers, review, tomb, *lead])
    assert len(picked) == DOSSIER_CAP
    assert [d["kind"] for d in picked[:4]] == ["grant", "patent", "jhtv", "grant"]
    assert picked[:4] == sorted(lead, key=lambda d: d["published_on"], reverse=True)
    assert picked[4:] == papers[: DOSSIER_CAP - 4]
    assert review not in picked and tomb not in picked


# --- evidence assembly ---------------------------------------------------------------------------


def _fake_evidence(monkeypatch, failing: dict[str, int] | None = None) -> dict:
    """Fake clients and a pass-through cache. `failing` maps a client name to how many of its
    first calls raise (a large number: always). ``calls["releases"]`` records the release each
    cache source was looked up under."""
    failing = dict(failing or {})
    calls: dict = {"essential": True, "releases": {}}

    def tick(name):
        calls[name] = calls.get(name, 0) + 1
        if calls[name] <= failing.get(name, 0):
            raise RuntimeError(f"invented {name} outage")

    async def passthrough(db, source, release, key, fetch):
        calls["releases"][source] = release
        return await fetch()

    async def current_release(source):
        return {"opentargets": "invented-ot-26.09", "chembl": "invented-ChEMBL_99",
                "orphadata": "invented-orpha-2099-06-23"}.get(
            source, f"invented-week-of-{source}"
        )

    async def ot_target(ensembl_id):
        tick("ot_target")
        return {"isEssential": calls["essential"], "safetyLiabilities": [{"event": "invented"}],
                "drugAndClinicalCandidates": {"rows": [
                    {"drug": {"name": "INVENTOMAB"},
                     "diseases": [{"disease": {"id": "MONDO_0900001"}}]}]}}

    async def ot_association(ensembl_id, mondo_id):
        tick("ot_association")
        return {"overall": 0.4, "genetic": 0.1}

    async def mechanisms(target_chembl_id):
        tick("chembl")
        return []

    async def molecule(name):
        tick("chembl")
        return []

    async def trials_count(condition, intervention):
        tick("trials")
        return {"by_phase": {"phase2": 1}}

    async def comention_counts(target, disease_mesh, *, window):
        tick("comention")
        return {"count": 7}

    async def prevalence(orpha_code):
        tick("orphadata")
        return {"class": "1-9 / 100 000"}

    monkeypatch.setattr(cache, "cached", passthrough)
    monkeypatch.setattr(cache, "current_release", current_release)
    monkeypatch.setattr(opentargets, "ot_target", ot_target)
    monkeypatch.setattr(opentargets, "ot_association", ot_association)
    monkeypatch.setattr(chembl, "chembl_mechanisms", mechanisms)
    monkeypatch.setattr(chembl, "chembl_molecule", molecule)
    monkeypatch.setattr(trials, "trials_count", trials_count)
    monkeypatch.setattr(comention, "comention_counts", comention_counts)
    monkeypatch.setattr(orphadata, "orphadata_prevalence", prevalence)
    monkeypatch.setattr(flags, "is_ancestor", lambda a, b: False)
    return calls


async def _new_idea(db_factory) -> tuple[uuid.UUID, uuid.UUID, str]:
    async with db_factory() as db:
        epoch = await _epoch(db)
        idea_id = await _merge(db, epoch, await _doc(db), _bio())
        version = (await _idea(db, idea_id)).version_hash
        await db.commit()
    return epoch.id, idea_id, version


async def test_assemble_evidence_sets_flags_without_version_bump(db_factory, monkeypatch):
    _fake_evidence(monkeypatch)
    epoch_id, idea_id, version = await _new_idea(db_factory)
    await assemble_evidence(db_factory, epoch_id=epoch_id, idea_id=idea_id)
    async with db_factory() as db:
        idea = await _idea(db, idea_id)
        assert idea.evidence_assembled_at is not None
        assert {s: v["status"] for s, v in idea.evidence_status.items()} == {
            s: "done" for s in EVIDENCE_SOURCES
        }
        assert idea.data_flags["target_essential"]["raised"] is True
        assert idea.data_flags["safety_liabilities"]["raised"] is True
        assert idea.data_flags["same_target_in_clinic"]["raised"] is True
        assert idea.version_hash == version


async def test_evidence_cached_under_each_sources_release(db_factory, monkeypatch):
    """Open Targets, ChEMBL and Orphadata lookups key on the source's published release; the
    others on their own (weekly) release, never one shared ISO week (§11.2)."""
    calls = _fake_evidence(monkeypatch)
    epoch_id, idea_id, _ = await _new_idea(db_factory)
    await assemble_evidence(db_factory, epoch_id=epoch_id, idea_id=idea_id)
    # The biology idea has no ChEMBL target id and no agent, so ChEMBL is not looked up.
    assert calls["releases"] == {
        "opentargets_target": "invented-ot-26.09",
        "opentargets_association": "invented-ot-26.09",
        "trials": "invented-week-of-trials",
        "comention": "invented-week-of-comention",
        "orphadata": "invented-orpha-2099-06-23",
    }


async def test_evidence_failure_ends_after_three_attempts(db_factory, monkeypatch):
    # Trials and co-mentions fail on every call; Orphadata recovers on its third attempt.
    calls = _fake_evidence(monkeypatch, failing={"trials": 99, "comention": 99, "orphadata": 2})
    epoch_id, idea_id, version = await _new_idea(db_factory)

    for attempt in (1, 2):
        await assemble_evidence(db_factory, epoch_id=epoch_id, idea_id=idea_id)
        async with db_factory() as db:
            idea = await _idea(db, idea_id)
            assert idea.evidence_assembled_at is None
            assert idea.evidence_status["trials"] == {"status": "pending", "attempts": attempt}
            assert idea.evidence_status["comention"] == {"status": "pending", "attempts": attempt}
    await assemble_evidence(db_factory, epoch_id=epoch_id, idea_id=idea_id)

    async with db_factory() as db:
        idea = await _idea(db, idea_id)
        assert idea.evidence_assembled_at is not None
        for source in ("trials", "comention"):
            assert idea.evidence_status[source] == {"status": "failed", "attempts": 3}
            # The S4 dossier reads the summary: both sources show as unavailable.
            assert idea.evidence_summary[source]["unavailable"] is True
            assert "outage" in idea.evidence_summary[source]["error"]
        assert idea.evidence_status["orphadata"] == {"status": "done", "attempts": 3}
        assert idea.evidence_status["opentargets_target"] == {"status": "done", "attempts": 1}
        assert idea.version_hash == version
    # Done sources were fetched once; an ended assembly fetches nothing more.
    assert calls["ot_target"] == 1
    await assemble_evidence(db_factory, epoch_id=epoch_id, idea_id=idea_id)
    assert calls["trials"] == 3


async def test_target_carries_uniprot_and_resolves_chembl_target(db_factory, monkeypatch):
    """A normalized target takes HGNC's one UniProt accession, and evidence assembly resolves
    its ChEMBL target from it under the ChEMBL release before reading the mechanisms."""
    calls = _fake_evidence(monkeypatch)
    hgnc = {"KINX": [{"hgnc_id": "HGNC:90001", "symbol": "KINX",
                      "ensembl_gene_id": "ENSG00000900001", "uniprot_ids": ["Q9X001"]}],
            "KINY": [{"hgnc_id": "HGNC:90002", "symbol": "KINY",
                      "uniprot_ids": ["Q9X002", "Q9X003"]}]}

    async def hgnc_lookup(symbol):
        return hgnc.get(symbol, [])

    resolved, mechanism_ids = [], []

    async def target_for_uniprot(accession):
        resolved.append(accession)
        return {"Q9X001": "CHEMBL9900001"}.get(accession)

    async def mechanisms(target_chembl_id):
        mechanism_ids.append(target_chembl_id)
        return [{"molecule_chembl_id": "CHEMBL9900009", "max_phase": 2.0}]

    monkeypatch.setattr(normalize, "hgnc_lookup", hgnc_lookup)
    monkeypatch.setattr(chembl, "chembl_target_for_uniprot", target_for_uniprot)
    monkeypatch.setattr(chembl, "chembl_mechanisms", mechanisms)

    kinx, kiny = await normalize.normalize_targets(
        [{"as_written": "KINX", "kind": "gene", "species": "human"},
         {"as_written": "KINY", "kind": "gene", "species": "human"}],
        pubtator=[], criteria=load_criteria(),
    )
    assert kinx["uniprot_id"] == "Q9X001"
    assert "uniprot_id" not in kiny  # two accessions: left unset rather than guessed

    async with db_factory() as db:
        epoch = await _epoch(db)
        candidate = _bio()
        candidate["targets"] = [kinx]
        idea_id = await _merge(db, epoch, await _doc(db), candidate)
        await db.commit()
    await assemble_evidence(db_factory, epoch_id=epoch.id, idea_id=idea_id)

    assert resolved == ["Q9X001"]
    assert mechanism_ids == ["CHEMBL9900001"]
    assert calls["releases"]["chembl_target"] == "invented-ChEMBL_99"
    assert calls["releases"]["chembl_mechanisms"] == "invented-ChEMBL_99"
    async with db_factory() as db:
        idea = await _idea(db, idea_id)
        assert idea.evidence_summary["chembl"]["mechanisms"] == {
            "HGNC:90001": [{"molecule_chembl_id": "CHEMBL9900009", "max_phase": 2.0}]
        }
