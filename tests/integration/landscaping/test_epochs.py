"""Judgment epochs (spec §7.11, D61, D65): candidates, changed stages, hold and override, the
atomic activation, copy-forward and the drain mark. Components are invented hashes unless a test
reads the shipped inputs; the full re-judge test is Task 6.5's (X1)."""
import itertools
import shutil
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import get_settings
from src.landscaping import epochs, panel
from src.landscaping.criteria import load_criteria
from src.landscaping.epochs import (
    STAGE_INPUTS,
    activate,
    changed_stages,
    copy_forward,
    ensure_candidate,
    hold,
    mark_drained,
    override,
)
from src.landscaping.prompt_set import PROMPT_DIR, load_prompt_set, stamp
from src.models import AppSetting
from src.models.landscape import (
    LandscapeConsult,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeOpportunity,
    LandscapeRanking,
    LandscapeScreen,
    LandscapeTriage,
)
from tests import factories

pytestmark = pytest.mark.integration

ALL_STAGES = ("screen", "extract", "triage", "cut", "deep", "rerank", "similarity", "label", "final")
BASE = {key: f"invented-{key}" for key in STAGE_INPUTS}
_doc_counter = itertools.count()


def _components(**changes) -> dict:
    return {**BASE, **changes}


@pytest.fixture
def components(monkeypatch):
    """The components ``ensure_candidate`` sees; tests set ``state["value"]``."""
    state = {"value": dict(BASE)}
    monkeypatch.setattr(epochs, "current_components", lambda: dict(state["value"]))
    return state


async def _epoch(db, status: str, components: dict | None = None, **kw) -> LandscapeEpoch:
    epoch = LandscapeEpoch(
        status=status, components=dict(BASE) if components is None else components, **kw
    )
    db.add(epoch)
    await db.flush()
    return epoch


async def _document(db, created_at: datetime | None = None) -> LandscapeDocument:
    extra = {"created_at": created_at} if created_at is not None else {}
    doc = LandscapeDocument(
        kind="work",
        canonical_external_id=f"doi:10.1000/invented-epoch-{next(_doc_counter)}-{uuid.uuid4().hex[:8]}",
        s1_status="eligible",
        **extra,
    )
    db.add(doc)
    await db.flush()
    return doc


async def _count(db, model, *where) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _set_phase(db, value: str) -> None:
    stmt = pg_insert(AppSetting).values(key="landscape_phase", value=value)
    await db.execute(stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value}))
    await db.flush()


async def _judged_rows(db, epoch: LandscapeEpoch) -> dict:
    """One document through S2-S7 in ``epoch``, plus a second whose S2 is still in flight."""
    doc, doc_in_flight = await _document(db), await _document(db)
    screen = LandscapeScreen(
        document_id=doc.id, epoch_id=epoch.id, status="done", decision="yes",
        reason="An invented screening reason.",
    )
    in_flight = LandscapeScreen(document_id=doc_in_flight.id, epoch_id=epoch.id, status="submitted")
    extraction = LandscapeExtraction(
        document_id=doc.id, epoch_id=epoch.id, basis="summary", status="done",
        raw={"ideas": [{"idea_type": "biology"}]},
    )
    idea = LandscapeIdea(
        epoch_id=epoch.id, idea_key="bio:HGNC:0001|inhibit|MONDO:0001", idea_type="biology",
        version_hash="v1", in_current_cut=True,
    )
    db.add_all([screen, in_flight, extraction, idea])
    await db.flush()
    triage = LandscapeTriage(
        idea_id=idea.id, idea_version="v1", epoch_id=epoch.id, status="done", triage_score=0.5
    )
    evidence = LandscapeIdeaEvidence(idea_id=idea.id, extraction_id=extraction.id, candidate_index=0)
    cut = LandscapeCut(
        epoch_id=epoch.id, status="complete", n=1, k=1, ordered_idea_ids=[str(idea.id)]
    )
    db.add_all([triage, evidence, cut])
    await db.flush()
    deep = LandscapeDeep(
        idea_id=idea.id, idea_version="v1", epoch_id=epoch.id, cut_id=cut.id, status="done"
    )
    ranking = LandscapeRanking(epoch_id=epoch.id, idea_id=idea.id, position=1)
    db.add_all([deep, ranking])
    await db.flush()
    consult = LandscapeConsult(deep_id=deep.id, domain="chemistry", opinion={"view": "invented"})
    db.add(consult)
    await db.flush()
    return {"doc": doc, "doc_in_flight": doc_in_flight, "idea": idea, "extraction": extraction}


async def _snapshot(db, epoch_id: uuid.UUID) -> dict:
    """Every row of ``epoch_id``: its epoch row, its epoch-scoped rows, and their children."""
    out = {}
    epoch_table = LandscapeEpoch.__table__
    out["epoch"] = [dict(r) for r in (await db.execute(
        select(epoch_table).where(epoch_table.c.id == epoch_id)
    )).mappings()]
    for table in LandscapeEpoch.metadata.sorted_tables:
        if table.name.startswith("landscape_") and "epoch_id" in table.c:
            rows = await db.execute(
                select(table).where(table.c.epoch_id == epoch_id).order_by(table.c.id)
            )
            out[table.name] = [dict(r) for r in rows.mappings()]
    evidence, idea = LandscapeIdeaEvidence.__table__, LandscapeIdea.__table__
    out["evidence"] = [dict(r) for r in (await db.execute(
        select(evidence).join(idea, idea.c.id == evidence.c.idea_id)
        .where(idea.c.epoch_id == epoch_id).order_by(evidence.c.id)
    )).mappings()]
    consult, deep = LandscapeConsult.__table__, LandscapeDeep.__table__
    out["consults"] = [dict(r) for r in (await db.execute(
        select(consult).join(deep, deep.c.id == consult.c.deep_id)
        .where(deep.c.epoch_id == epoch_id).order_by(consult.c.id)
    )).mappings()]
    return out


async def test_first_epoch_is_candidate(db_session, components):
    epoch = await ensure_candidate(db_session)

    assert epoch is not None
    assert epoch.status == "candidate"
    assert epoch.components == BASE
    assert tuple(epoch.changed_stages) == ALL_STAGES
    assert epoch.built_from_epoch_id is None
    assert epoch.prompt_set_version and epoch.criteria_version
    # The first epoch stores its judging inputs too.
    assert epoch.snapshot == epochs.build_snapshot()
    assert epoch.prompt_set_version == epoch.snapshot["prompt_set_version"]


async def test_epoch_inputs_use_the_snapshot_not_the_files(
    db_session, components, tmp_path, monkeypatch
):
    work = tmp_path / "landscaping"
    shutil.copytree(PROMPT_DIR, work)
    monkeypatch.setattr(epochs, "load_prompt_set", lambda: load_prompt_set(work))
    stored_model = get_settings().landscape_screen_model
    epoch = await ensure_candidate(db_session)
    original = load_prompt_set(work).files["screen.md"]

    # The judging prompt and the stage model change after the epoch was created.
    screen_md = work / "screen.md"
    screen_md.write_text(original + "\nAn invented later instruction.\n", encoding="utf-8")
    stamp("invented-later-version", work)
    assert "An invented later instruction." in load_prompt_set(work).files["screen.md"]
    monkeypatch.setattr(get_settings(), "landscape_screen_model", "claude-invented-later")

    inputs = await epochs.epoch_inputs(db_session, epoch.id)
    assert inputs.ps.files["screen.md"] == original
    assert inputs.ps.judging_hashes == load_prompt_set(PROMPT_DIR).judging_hashes
    assert "An invented later instruction." not in inputs.system("screen")
    assert inputs.model("screen") == stored_model
    assert inputs.model("deep_extract") == inputs.model("deep")
    assert inputs.criteria.judging_hash == load_criteria().judging_hash
    assert inputs.rubric.content_hash == epochs.load_rubric().content_hash
    assert await epochs.epoch_inputs(db_session, epoch.id) is inputs

    bare = await _epoch(db_session, "candidate", _components(**{"final.md": "invented-2"}))
    with pytest.raises(LookupError):
        await epochs.epoch_inputs(db_session, bare.id)


async def test_personas_are_an_epoch_input(db_session, monkeypatch):
    assert changed_stages(BASE, _components(personas="invented-2")) == (
        "deep", "rerank", "similarity", "label", "final"
    )
    base = epochs.current_components()
    epoch = await ensure_candidate(db_session)
    composed = {domain: panel.compose_persona(domain) for domain in panel.PANEL_CHEMISTRY}
    assert epoch.snapshot["personas"] == composed

    # A persona edit after the epoch was created changes only the personas component, and the
    # epoch keeps judging with the personas it stored.
    original = panel.compose_persona
    monkeypatch.setattr(
        panel, "compose_persona", lambda domain: original(domain) + "\nAn invented later line.\n"
    )
    later = epochs.current_components()
    assert {key for key in base if base[key] != later[key]} == {"personas"}
    inputs = await epochs.epoch_inputs(db_session, epoch.id)
    for domain in panel.PANEL_CHEMISTRY:
        assert inputs.persona(domain) == composed[domain]
    with pytest.raises(KeyError):
        inputs.persona("invented-domain")


async def test_no_change_no_candidate(db_session, components):
    await _epoch(db_session, "active")
    stale = await _epoch(db_session, "candidate", _components(**{"final.md": "invented-old"}))

    assert await ensure_candidate(db_session) is None

    await db_session.refresh(stale)
    assert stale.status == "retired"
    assert await _count(db_session, LandscapeEpoch, LandscapeEpoch.status != "retired") == 1


async def test_chat_md_and_reason_tags_create_no_candidate(db_session, tmp_path, monkeypatch):
    work = tmp_path / "landscaping"
    shutil.copytree(PROMPT_DIR, work)
    monkeypatch.setattr(epochs, "load_prompt_set", lambda: load_prompt_set(work))
    monkeypatch.setattr(epochs, "load_criteria", lambda: load_criteria(work / "criteria.toml"))
    before = epochs.current_components()
    await _epoch(db_session, "active", before)

    chat = work / "chat.md"
    chat.write_text(chat.read_text(encoding="utf-8") + "\nAn invented closing line.\n", encoding="utf-8")
    criteria = work / "criteria.toml"
    text = criteria.read_text(encoding="utf-8")
    marker = "[reason_tags]\napproved = false\ntags = [\n"
    assert marker in text
    criteria.write_text(
        text.replace(marker, '[reason_tags]\napproved = true\ntags = [\n  "An invented tag",\n'),
        encoding="utf-8",
    )
    stamp("invented-test-version", work)
    assert load_criteria(work / "criteria.toml").reason_tags_approved is True

    assert epochs.current_components() == before
    assert await ensure_candidate(db_session) is None
    assert await _count(db_session, LandscapeEpoch) == 1


async def test_model_change_marks_stage_and_downstream(db_session, components):
    expected = ("triage", "cut", "deep", "rerank", "similarity", "label", "final")
    new = _components(landscape_triage_model="claude-sonnet-5-5")
    assert changed_stages(BASE, new) == expected

    active = await _epoch(db_session, "active")
    components["value"] = new
    candidate = await ensure_candidate(db_session)

    assert tuple(candidate.changed_stages) == expected
    assert candidate.built_from_epoch_id == active.id


async def test_second_change_supersedes_candidate(db_session, components):
    active = await _epoch(db_session, "active")
    components["value"] = _components(**{"final.md": "invented-2"})
    first = await ensure_candidate(db_session)
    components["value"] = _components(**{"final.md": "invented-2", "cluster_label.md": "invented-2"})
    second = await ensure_candidate(db_session)

    await db_session.refresh(first)
    assert first.status == "retired" and first.retired_at is not None
    assert second.id != first.id
    assert second.built_from_epoch_id == active.id
    assert tuple(second.changed_stages) == ("label", "final")
    assert await _count(db_session, LandscapeEpoch, LandscapeEpoch.status == "candidate") == 1
    # The same inputs again keep the same candidate.
    assert (await ensure_candidate(db_session)).id == second.id


async def test_second_change_without_active_epoch_keeps_unchanged_stages(db_session, components):
    first = await ensure_candidate(db_session)
    await copy_forward(db_session, from_epoch=None, to_epoch=first.id, stages=())
    doc = await _document(db_session)
    db_session.add(LandscapeScreen(
        document_id=doc.id, epoch_id=first.id, status="done", decision="yes"
    ))
    await db_session.flush()

    components["value"] = _components(landscape_final_model="claude-opus-4-8")
    second = await ensure_candidate(db_session)

    await db_session.refresh(first)
    assert first.status == "retired"
    assert second.built_from_epoch_id == first.id
    assert tuple(second.changed_stages) == ("final",)

    unchanged = tuple(s for s in ALL_STAGES if s not in second.changed_stages)
    await copy_forward(db_session, from_epoch=first, to_epoch=second, stages=unchanged)
    copied = (await db_session.execute(
        select(LandscapeScreen).where(LandscapeScreen.epoch_id == second.id)
    )).scalars().all()
    assert [(s.document_id, s.status, s.decision) for s in copied] == [(doc.id, "done", "yes")]


async def test_mark_drained_ignores_items_after_rejudge_start(db_session):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    candidate = await _epoch(db_session, "candidate", rejudge_started_at=start)
    await _set_phase(db_session, "backfill")
    assert await mark_drained(db_session, epoch_id=candidate.id) is False

    await _set_phase(db_session, "continuous")
    old_doc = await _document(db_session, created_at=start - timedelta(days=1))
    new_doc = await _document(db_session, created_at=start + timedelta(hours=1))
    old_screen = LandscapeScreen(document_id=old_doc.id, epoch_id=candidate.id, status="pending")
    new_screen = LandscapeScreen(document_id=new_doc.id, epoch_id=candidate.id, status="pending")
    db_session.add_all([old_screen, new_screen])
    await db_session.flush()
    assert await mark_drained(db_session, epoch_id=candidate.id) is False

    old_screen.status = "done"
    await db_session.flush()
    assert await mark_drained(db_session, epoch_id=candidate.id) is True
    await db_session.refresh(candidate)
    assert candidate.drained_at is not None
    assert new_screen.status == "pending"


async def test_mark_drained_waits_for_screened_in_document_without_extraction(db_session):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    candidate = await _epoch(db_session, "candidate", rejudge_started_at=start)
    await _set_phase(db_session, "continuous")
    doc = await _document(db_session, created_at=start - timedelta(days=1))
    db_session.add(LandscapeScreen(
        document_id=doc.id, epoch_id=candidate.id, status="done", decision="yes",
    ))
    await db_session.flush()
    assert await mark_drained(db_session, epoch_id=candidate.id) is False

    db_session.add(LandscapeExtraction(
        document_id=doc.id, epoch_id=candidate.id, basis="summary", status="done",
        raw={"ideas": []},
    ))
    await db_session.flush()
    assert await mark_drained(db_session, epoch_id=candidate.id) is True


async def test_mark_drained_waits_for_old_document_without_screen(db_session):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    candidate = await _epoch(db_session, "candidate", rejudge_started_at=start)
    await _set_phase(db_session, "continuous")
    old_doc = await _document(db_session, created_at=start - timedelta(days=1))
    await _document(db_session, created_at=start + timedelta(hours=1))
    assert await mark_drained(db_session, epoch_id=candidate.id) is False

    db_session.add(LandscapeScreen(
        document_id=old_doc.id, epoch_id=candidate.id, status="done", decision="no",
    ))
    await db_session.flush()
    assert await mark_drained(db_session, epoch_id=candidate.id) is True


async def test_fallback_audit_and_chat_models_are_not_components(monkeypatch):
    base = epochs.current_components()
    assert set(base) == set(STAGE_INPUTS)
    for key in ("landscape_fallback_models", "landscape_audit_model", "llm_landscape_chat_model"):
        assert key not in base

    unrelated = get_settings().model_copy(update={
        "landscape_fallback_models": ["claude-opus-4-8"],
        "landscape_audit_model": "claude-sonnet-5",
        "llm_landscape_chat_model": "claude-sonnet-5",
    })
    monkeypatch.setattr(epochs, "get_settings", lambda: unrelated)
    assert epochs.current_components() == base

    stage_model = get_settings().model_copy(update={"landscape_triage_model": "claude-sonnet-5-5"})
    monkeypatch.setattr(epochs, "get_settings", lambda: stage_model)
    assert epochs.current_components() != base


async def test_activate_is_atomic_and_single_active(db_session):
    old = await _epoch(db_session, "active")
    candidate = await _epoch(db_session, "candidate", _components(**{"final.md": "invented-2"}))
    calls = []

    async def on_activate(session, old_id, new_id):
        calls.append((session, old_id, new_id))
        session.add(LandscapeOpportunity())
        await session.flush()

    await activate(db_session, candidate.id, on_activate=on_activate)

    assert calls == [(db_session, old.id, candidate.id)]
    await db_session.refresh(old)
    await db_session.refresh(candidate)
    assert candidate.status == "active" and candidate.activated_at is not None
    assert old.status == "retired" and old.retired_at is not None
    assert await _count(db_session, LandscapeEpoch, LandscapeEpoch.status == "active") == 1
    assert await _count(db_session, LandscapeOpportunity) == 1
    with pytest.raises(ValueError):
        await activate(db_session, old.id, on_activate=on_activate)


async def test_activate_hook_runs_in_same_transaction(db_session):
    old = await _epoch(db_session, "active")
    candidate = await _epoch(db_session, "candidate", _components(**{"final.md": "invented-2"}))

    async def failing(session, old_id, new_id):
        session.add(LandscapeOpportunity())
        await session.flush()
        raise RuntimeError("invented grouping failure")

    with pytest.raises(RuntimeError):
        await activate(db_session, candidate.id, on_activate=failing)

    await db_session.refresh(old)
    await db_session.refresh(candidate)
    assert candidate.status == "candidate" and candidate.activated_at is None
    assert old.status == "active" and old.retired_at is None
    assert await _count(db_session, LandscapeOpportunity) == 0


async def test_override_records_actor_and_reason(db_session):
    actor = await factories.make_user(db_session)
    candidate = await _epoch(db_session, "candidate")

    await hold(db_session, candidate.id, "regression failed on the screen items")
    await db_session.refresh(candidate)
    assert candidate.status == "held"
    assert candidate.hold_reason == "regression failed on the screen items"

    await override(db_session, candidate.id, actor_id=actor.id, reason="checked by hand")
    await db_session.refresh(candidate)
    assert candidate.status == "candidate"
    assert candidate.override_actor_id == actor.id
    assert candidate.override_reason == "checked by hand"
    assert candidate.override_at is not None

    active = await _epoch(db_session, "active", _components(**{"final.md": "invented-2"}))
    with pytest.raises(ValueError):
        await hold(db_session, active.id, "an active epoch is never held")


async def test_unchanged_upstream_rows_copied_forward(db_session, components):
    active = await _epoch(db_session, "active")
    rows = await _judged_rows(db_session, active)
    components["value"] = _components(**{"deep.md": "invented-2"})
    candidate = await ensure_candidate(db_session)
    unchanged = tuple(s for s in ALL_STAGES if s not in candidate.changed_stages)
    assert unchanged == ("screen", "extract", "triage", "cut")

    await copy_forward(db_session, from_epoch=active.id, to_epoch=candidate.id, stages=unchanged)

    await db_session.refresh(candidate)
    assert candidate.rejudge_started_at is not None
    screens = {
        s.document_id: s for s in (await db_session.execute(
            select(LandscapeScreen).where(LandscapeScreen.epoch_id == candidate.id)
        )).scalars()
    }
    assert screens[rows["doc"].id].status == "done"
    assert screens[rows["doc"].id].decision == "yes"
    assert screens[rows["doc_in_flight"].id].status == "pending"
    assert screens[rows["doc_in_flight"].id].batch_item_id is None

    idea = (await db_session.execute(
        select(LandscapeIdea).where(LandscapeIdea.epoch_id == candidate.id)
    )).scalar_one()
    assert idea.id != rows["idea"].id
    assert idea.idea_key == rows["idea"].idea_key
    extraction = (await db_session.execute(
        select(LandscapeExtraction).where(LandscapeExtraction.epoch_id == candidate.id)
    )).scalar_one()
    evidence = (await db_session.execute(
        select(LandscapeIdeaEvidence).where(LandscapeIdeaEvidence.idea_id == idea.id)
    )).scalar_one()
    assert evidence.extraction_id == extraction.id
    triage = (await db_session.execute(
        select(LandscapeTriage).where(LandscapeTriage.epoch_id == candidate.id)
    )).scalar_one()
    assert triage.idea_id == idea.id and triage.triage_score == 0.5
    cut = (await db_session.execute(
        select(LandscapeCut).where(LandscapeCut.epoch_id == candidate.id)
    )).scalar_one()
    assert cut.ordered_idea_ids == [str(idea.id)]
    assert await _count(db_session, LandscapeDeep, LandscapeDeep.epoch_id == candidate.id) == 0
    assert await _count(db_session, LandscapeRanking, LandscapeRanking.epoch_id == candidate.id) == 0


async def test_copy_forward_remaps_fold_ids(db_session, components):
    active = await _epoch(db_session, "active")
    rows = await _judged_rows(db_session, active)
    keeper = rows["idea"]
    folded = LandscapeIdea(
        epoch_id=active.id, idea_key="bio:HGNC:0002|inhibit|MONDO:0001", idea_type="biology",
        version_hash="v1",
    )
    db_session.add(folded)
    await db_session.flush()
    cut = (await db_session.execute(
        select(LandscapeCut).where(LandscapeCut.epoch_id == active.id)
    )).scalar_one()
    cut.folds = {
        "folded": {str(keeper.id): [str(folded.id)]},
        "tie_class": {
            "ids": [str(keeper.id), str(folded.id)],
            "versions": {str(keeper.id): "v1", str(folded.id): "v1"},
        },
    }
    await db_session.flush()
    source_folds = dict(cut.folds)

    components["value"] = _components(**{"deep.md": "invented-2"})
    candidate = await ensure_candidate(db_session)
    unchanged = tuple(s for s in ALL_STAGES if s not in candidate.changed_stages)
    await copy_forward(db_session, from_epoch=active.id, to_epoch=candidate.id, stages=unchanged)

    new_ids = {
        idea.idea_key: str(idea.id) for idea in (await db_session.execute(
            select(LandscapeIdea).where(LandscapeIdea.epoch_id == candidate.id)
        )).scalars()
    }
    new_keeper, new_folded = new_ids[keeper.idea_key], new_ids[folded.idea_key]
    copied = (await db_session.execute(
        select(LandscapeCut).where(LandscapeCut.epoch_id == candidate.id)
    )).scalar_one()
    assert copied.folds == {
        "folded": {new_keeper: [new_folded]},
        "tie_class": {
            "ids": [new_keeper, new_folded],
            "versions": {new_keeper: "v1", new_folded: "v1"},
        },
    }
    await db_session.refresh(cut)
    assert cut.folds == source_folds


async def test_rerank_model_change_marks_cut():
    new = _components(landscape_rerank_model="claude-opus-4-8")
    assert changed_stages(BASE, new) == (
        "cut", "deep", "rerank", "similarity", "label", "final"
    )


async def test_copy_forward_never_writes_active_rows(db_session, components):
    active = await _epoch(db_session, "active")
    await _judged_rows(db_session, active)
    before = await _snapshot(db_session, active.id)

    components["value"] = _components(**{"final.md": "invented-2"})
    candidate = await ensure_candidate(db_session)
    unchanged = tuple(s for s in ALL_STAGES if s not in candidate.changed_stages)
    await copy_forward(db_session, from_epoch=active.id, to_epoch=candidate.id, stages=unchanged)

    assert await _snapshot(db_session, active.id) == before
    copied = await _snapshot(db_session, candidate.id)
    for table in ("landscape_screens", "landscape_ideas", "landscape_triage", "landscape_cuts",
                  "landscape_deep", "landscape_rankings"):
        assert len(copied[table]) == len(before[table]) > 0, table
    assert len(copied["consults"]) == len(before["consults"]) == 1


async def test_copied_ranking_takes_copied_deep_id(db_session, components):
    active = await _epoch(db_session, "active")
    await _judged_rows(db_session, active)
    source_deep = (await db_session.execute(
        select(LandscapeDeep).where(LandscapeDeep.epoch_id == active.id)
    )).scalar_one()
    # S7 places a ranking row on its S6 record: the row's id is the record's id.
    await db_session.execute(
        update(LandscapeRanking).where(LandscapeRanking.epoch_id == active.id)
        .values(id=source_deep.id)
    )
    await db_session.flush()

    components["value"] = _components(**{"final.md": "invented-2"})
    candidate = await ensure_candidate(db_session)
    unchanged = tuple(s for s in ALL_STAGES if s not in candidate.changed_stages)
    assert {"deep", "rerank"} <= set(unchanged)
    await copy_forward(db_session, from_epoch=active.id, to_epoch=candidate.id, stages=unchanged)

    deep_ids = {
        deep.idea_id: deep.id for deep in (await db_session.execute(
            select(LandscapeDeep).where(LandscapeDeep.epoch_id == candidate.id)
        )).scalars()
    }
    rankings = (await db_session.execute(
        select(LandscapeRanking).where(LandscapeRanking.epoch_id == candidate.id)
    )).scalars().all()
    assert rankings
    for ranking in rankings:
        assert ranking.id == deep_ids[ranking.idea_id] != source_deep.id
