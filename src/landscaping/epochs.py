"""Judgment epochs (spec §7.11, D61, D65): which judging inputs an epoch stands for, when a
candidate appears, and how it is held, overridden, built and activated.

An epoch is identified only by its *components* (``current_components``): the eight judging
prompt hashes, the criteria file's judging hash, the rubric and BlackbirdBot extract hashes, the
hash of S6's composed specialist personas, and the eight stage model settings. A change marks the stages that read the changed component, plus
every stage downstream of them (``changed_stages``); the other stages are copied forward from the
epoch the candidate was built from (``copy_forward``). Nothing here writes a row of the active
epoch: a candidate only ever inserts its own rows (§7.11 step 3).

Each epoch also stores the inputs themselves (``snapshot``, from ``build_snapshot``), and its
stage runners judge with those (``epoch_inputs``), never with the files on disk or the current
settings: the active epoch keeps serving unchanged while a candidate is built (§7.11).

Every function that writes commits its own transaction.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, Table, and_, exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import panel
from src.landscaping.criteria import (
    CRITERIA_PATH,
    Criteria,
    CriteriaError,
    load_criteria,
    parse_criteria,
)
from src.landscaping.llm_client import STAGE_SPECS
from src.landscaping.prompt_set import (
    JUDGING_FILES,
    BlackbirdExtracts,
    PromptSet,
    load_blackbird_extracts,
    load_prompt_set,
    prompt_set_from_texts,
    system_prompt,
)
from src.models import AppSetting
from src.models.landscape import (
    LandscapeCandidateGrouping,
    LandscapeComparison,
    LandscapeConsult,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeExtractionLink,
    LandscapeIdea,
    LandscapeIdeaAlias,
    LandscapeIdeaEvidence,
    LandscapeIdeaLink,
    LandscapeRanking,
    LandscapeScreen,
    LandscapeSimilarity,
    LandscapeTriage,
)
from src.services.blackbird_rubric import (
    Rubric,
    load_rubric,
    loaded_rubric_bytes,
    parse_rubric_bytes,
)

# The judged stages in pipeline order; ``changed_stages`` returns them in this order.
_STAGES = ("screen", "extract", "triage", "cut", "deep", "rerank", "similarity", "label", "final")

#: Component -> the stages that read it (spec §7.11, §8.1, §8.2). The rerank model also judges
#: the S5 tie-break, so it marks ``cut``. S6's full-text re-extraction reads ``extract.md`` and
#: runs on the deep model.
STAGE_INPUTS: dict[str, tuple[str, ...]] = {
    "screen.md": ("screen",),
    "extract.md": ("extract", "deep"),
    "triage.md": ("triage",),
    "rerank.md": ("cut", "rerank"),
    "deep.md": ("deep",),
    "similarity.md": ("similarity",),
    "cluster_label.md": ("label",),
    "final.md": ("final",),
    "criteria": ("screen", "extract", "triage", "deep", "final"),
    "rubric": ("extract", "triage", "deep", "final"),
    "extracts": ("triage", "deep", "final"),
    "personas": ("deep",),
    "landscape_screen_model": ("screen",),
    "landscape_extract_model": ("extract",),
    "landscape_triage_model": ("triage",),
    "landscape_deep_model": ("deep",),
    "landscape_rerank_model": ("cut", "rerank"),
    "landscape_similarity_model": ("similarity",),
    "landscape_label_model": ("label",),
    "landscape_final_model": ("final",),
}

#: Stage -> the stages that consume its output directly. Similarity only adds "related" links,
#: which never change a grouping (spec §7.9), so nothing is downstream of it.
DOWNSTREAM: dict[str, tuple[str, ...]] = {
    "screen": ("extract",),
    "extract": ("triage",),
    "triage": ("cut",),
    "cut": ("deep",),
    "deep": ("rerank", "similarity", "label", "final"),
    "label": ("final",),
}

# The eight stage model settings that identify an epoch. The fallback chain, the audit model and
# the chat model are deliberately absent (D61).
_MODEL_SETTINGS = tuple(k for k in STAGE_INPUTS if k.startswith("landscape_"))

# Stage-row statuses that mean work is still owed; every other status is terminal.
_IN_FLIGHT = ("pending", "submitting", "submitted", "running")
_UNACTIVATED = ("candidate", "held")
# Columns a copied row takes fresh from the server defaults.
_FRESH_COLUMNS = frozenset({"created_at", "updated_at"})
_INSERT_CHUNK = 1000


def current_components() -> dict:
    """The identifying components of the inputs on disk and in settings (D61).

    Keys are ``STAGE_INPUTS``'s. The set and criteria versions are not components: they are
    recorded on the epoch for people to read and never compared.
    """
    settings = get_settings()
    prompt_set = load_prompt_set()
    components: dict = {name: prompt_set.judging_hashes[name] for name in JUDGING_FILES}
    components["criteria"] = load_criteria().judging_hash
    components["rubric"] = load_rubric().content_hash
    components["extracts"] = load_blackbird_extracts().content_hash
    components["personas"] = _personas_hash(_composed_personas())
    for name in _MODEL_SETTINGS:
        components[name] = getattr(settings, name)
    return components


def _composed_personas() -> dict[str, str]:
    """Each panel domain's persona as S6 consults it (``panel.compose_persona``), keyed in
    ``panel.PANEL_CHEMISTRY`` order (the biology panel is a subset)."""
    return {domain: panel.compose_persona(domain) for domain in panel.PANEL_CHEMISTRY}


def _personas_hash(personas: dict[str, str]) -> str:
    """sha256 over the composed personas in ``panel.PANEL_CHEMISTRY`` order, each preceded by
    its domain, every part NUL-terminated."""
    digest = hashlib.sha256()
    for domain in panel.PANEL_CHEMISTRY:
        for part in (domain, personas[domain]):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


#: The stages whose model an epoch stores (``build_snapshot``); S6's full-text re-extraction
#: (``deep_extract``) runs on the deep model.
_SNAPSHOT_MODEL_STAGES = (
    "screen", "extract", "triage", "deep", "rerank", "similarity", "label", "final",
)
_MODEL_ALIASES = {"deep_extract": "deep"}


def build_snapshot() -> dict:
    """The judging inputs on disk and in settings, as stored on a new epoch (spec §7.11).

    Keys: ``prompt_set_version``; ``prompt_files`` (each ``JUDGING_FILES`` name -> text);
    ``criteria`` (the criteria file's text); ``extracts`` (the ``BlackbirdExtracts`` fields);
    ``rubric`` (the loaded rubric document's text); ``personas`` (each ``panel.PANEL_CHEMISTRY``
    domain -> its composed persona, ``panel.compose_persona``); ``models`` (stage -> model, for
    the eight judged model stages). Raises what the loaders raise (``CriteriaError`` for an
    unreadable criteria file).
    """
    settings = get_settings()
    prompt_set = load_prompt_set()
    try:
        criteria_text = CRITERIA_PATH.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CriteriaError(f"criteria file unreadable at {CRITERIA_PATH}: {exc}") from exc
    return {
        "prompt_set_version": prompt_set.version,
        "prompt_files": {name: prompt_set.files[name] for name in JUDGING_FILES},
        "criteria": criteria_text,
        "extracts": asdict(load_blackbird_extracts()),
        "rubric": loaded_rubric_bytes().decode("utf-8"),
        "personas": _composed_personas(),
        "models": {
            stage: getattr(settings, STAGE_SPECS[stage].model_setting)
            for stage in _SNAPSHOT_MODEL_STAGES
        },
    }


@dataclass(frozen=True)
class EpochInputs:
    """One epoch's judging inputs, rebuilt from its stored snapshot (``epoch_inputs``)."""

    epoch_id: uuid.UUID
    ps: PromptSet
    ex: BlackbirdExtracts
    criteria: Criteria
    rubric: Rubric
    #: Stage -> model, the ``build_snapshot`` keys.
    models: dict[str, str]
    #: Panel domain -> composed persona, as ``build_snapshot`` stored it.
    personas: dict[str, str]

    def system(self, stage: str) -> str:
        """``stage``'s system prompt from these inputs (``prompt_set.system_prompt``)."""
        return system_prompt(
            stage, ps=self.ps, ex=self.ex, criteria=self.criteria, rubric=self.rubric
        )

    def model(self, stage: str) -> str:
        """``stage``'s model; ``deep_extract`` takes the deep model. ``KeyError`` for a stage
        the snapshot holds no model for."""
        return self.models[_MODEL_ALIASES.get(stage, stage)]

    def persona(self, domain: str) -> str:
        """``domain``'s composed specialist persona, the S6 consult's system prompt. ``KeyError``
        for a domain outside ``panel.PANEL_CHEMISTRY``."""
        return self.personas[domain]


# Snapshots never change once written, so an epoch's inputs are built once per process.
_INPUTS_CACHE: dict[uuid.UUID, EpochInputs] = {}


async def epoch_inputs(db: AsyncSession, epoch_id: uuid.UUID) -> EpochInputs:
    """The judging inputs stored on ``epoch_id`` (spec §7.11), cached per epoch in-process.

    Raises ``LookupError`` when the epoch does not exist or has no snapshot (``KeyError``, a
    ``LookupError``, when the snapshot lacks an input such as ``personas``), and the parsers'
    errors (``CriteriaError``, ``RubricError``, ``PromptSetError``) for a defective one.
    """
    cached = _INPUTS_CACHE.get(epoch_id)
    if cached is not None:
        return cached
    snapshot = await db.scalar(
        select(LandscapeEpoch.snapshot).where(LandscapeEpoch.id == epoch_id)
    )
    if not snapshot:
        raise LookupError(
            f"epoch {epoch_id} has no stored snapshot of its judging inputs; "
            "its stage runners cannot judge with it"
        )
    inputs = EpochInputs(
        epoch_id=epoch_id,
        ps=prompt_set_from_texts(snapshot["prompt_set_version"], snapshot["prompt_files"]),
        ex=BlackbirdExtracts(**snapshot["extracts"]),
        criteria=parse_criteria(snapshot["criteria"], f"epoch {epoch_id} snapshot"),
        rubric=parse_rubric_bytes(snapshot["rubric"].encode("utf-8"), "epoch snapshot"),
        models=dict(snapshot["models"]),
        personas=dict(snapshot["personas"]),
    )
    _INPUTS_CACHE[epoch_id] = inputs
    return inputs


def changed_stages(old: dict, new: dict) -> tuple[str, ...]:
    """The stages to re-judge going from components ``old`` to ``new``: those reading a changed
    component, closed over ``DOWNSTREAM``, in pipeline order. A changed key that
    ``STAGE_INPUTS`` does not know marks every stage."""
    marked: set[str] = set()
    for key in old.keys() | new.keys():
        if old.get(key) != new.get(key):
            marked.update(STAGE_INPUTS.get(key, _STAGES))
    frontier = list(marked)
    while frontier:
        for stage in DOWNSTREAM.get(frontier.pop(), ()):
            if stage not in marked:
                marked.add(stage)
                frontier.append(stage)
    return _ordered(marked)


def _ordered(stages: Iterable[str]) -> tuple[str, ...]:
    chosen = set(stages)
    return tuple(s for s in _STAGES if s in chosen)


def _upstream_closed(stages: set[str]) -> bool:
    return all(
        down not in stages or up in stages
        for up, downs in DOWNSTREAM.items()
        for down in downs
    )


async def active_epoch(db: AsyncSession) -> LandscapeEpoch | None:
    """The one active epoch, or None before the first activation."""
    result = await db.execute(select(LandscapeEpoch).where(LandscapeEpoch.status == "active"))
    return result.scalar_one_or_none()


async def _unactivated(db: AsyncSession) -> list[LandscapeEpoch]:
    """Candidate and held epochs, newest first."""
    result = await db.execute(
        select(LandscapeEpoch)
        .where(LandscapeEpoch.status.in_(_UNACTIVATED))
        .order_by(LandscapeEpoch.created_at.desc(), LandscapeEpoch.id)
    )
    return list(result.scalars())


async def _build_source(
    db: AsyncSession, active: LandscapeEpoch | None, replaced: list[LandscapeEpoch], new: dict
) -> tuple[LandscapeEpoch | None, tuple[str, ...]]:
    """The epoch a new candidate copies from, and the stages it re-judges (D65).

    With an active epoch, that epoch. Otherwise the newest replaced candidate, whose rows hold the
    finished work. One that never began its re-judge holds no rows, so the walk goes on to the
    epoch it was built from; with no such epoch left, nothing is judged yet and every stage runs.
    Stages are compared with the source's own components, whose rows are the ones copied.
    """
    if active is not None:
        return active, changed_stages(active.components or {}, new)
    source = replaced[0] if replaced else None
    while source is not None and source.rejudge_started_at is None:
        parent = source.built_from_epoch_id
        source = await db.get(LandscapeEpoch, parent) if parent is not None else None
    if source is None:
        return None, _STAGES
    return source, changed_stages(source.components or {}, new)


async def _retire(db: AsyncSession, epochs: Iterable[LandscapeEpoch]) -> None:
    ids = [e.id for e in epochs]
    if ids:
        await db.execute(
            update(LandscapeEpoch)
            .where(LandscapeEpoch.id.in_(ids))
            .values(status="retired", retired_at=func.now(), updated_at=func.now())
        )


async def ensure_candidate(db: AsyncSession) -> LandscapeEpoch | None:
    """Make sure a candidate stands for the current judging inputs (spec §7.11, D65).

    Returns None when the active epoch already matches them; then any unactivated candidate is
    stale and is retired. Returns the existing unactivated epoch (candidate or held) whose
    components match, retiring any other. Otherwise retires every unactivated epoch and creates a
    candidate built from the active epoch, or, while none is active, from the replaced candidate's
    unchanged stages; with nothing to build from it re-judges every stage. Every epoch it creates
    stores ``build_snapshot()``. Never writes the active epoch.
    """
    components = current_components()
    active = await active_epoch(db)
    pending = await _unactivated(db)
    if active is not None and active.components == components:
        await _retire(db, pending)
        await db.commit()
        return None
    matching = [e for e in pending if e.components == components]
    if matching:
        await _retire(db, [e for e in pending if e.id != matching[0].id])
        await db.commit()
        return matching[0]
    source, stages = await _build_source(db, active, pending, components)
    await _retire(db, pending)
    snapshot = build_snapshot()
    criteria_version = parse_criteria(snapshot["criteria"], str(CRITERIA_PATH)).version
    candidate = LandscapeEpoch(
        status="candidate",
        components=components,
        snapshot=snapshot,
        prompt_set_version=snapshot["prompt_set_version"],
        criteria_version=criteria_version,
        changed_stages=list(stages),
        built_from_epoch_id=source.id if source is not None else None,
    )
    db.add(candidate)
    await db.commit()
    return candidate


async def hold(db: AsyncSession, epoch_id: uuid.UUID, reason: str) -> None:
    """Hold an unactivated epoch, recording why (§7.11 step 1); raises ValueError otherwise."""
    await _set_unactivated(db, epoch_id, status="held", hold_reason=reason)


async def override(
    db: AsyncSession, epoch_id: uuid.UUID, *, actor_id: uuid.UUID, reason: str
) -> None:
    """Record an admin's override of a held (or not yet regressed) epoch and return it to
    ``candidate`` so its re-judge may proceed (L28); raises ValueError for an active or retired
    epoch."""
    await _set_unactivated(
        db, epoch_id, status="candidate", override_actor_id=actor_id,
        override_reason=reason, override_at=func.now(),
    )


async def _set_unactivated(db: AsyncSession, epoch_id: uuid.UUID, **values) -> None:
    result = await db.execute(
        update(LandscapeEpoch)
        .where(LandscapeEpoch.id == epoch_id, LandscapeEpoch.status.in_(_UNACTIVATED))
        .values(**values, updated_at=func.now())
        .returning(LandscapeEpoch.id)
    )
    if result.scalar_one_or_none() is None:
        raise ValueError(f"epoch {epoch_id} is not an unactivated candidate")
    await db.commit()


async def activate(
    db: AsyncSession,
    epoch_id: uuid.UUID,
    *,
    on_activate: Callable[[AsyncSession, uuid.UUID | None, uuid.UUID], Awaitable[None]],
) -> None:
    """Make the candidate ``epoch_id`` active and retire the old active epoch, in one transaction
    (§7.11 step 4, D59).

    ``on_activate(db, old_active_id, epoch_id)`` runs first, inside the same savepoint, so the
    groupings it matches and the versions it mints commit with the switch. If it raises, the
    whole switch rolls back and the exception propagates. Raises ValueError when ``epoch_id`` is
    not a ``candidate`` (a held epoch needs ``override`` first). Readiness (§7.11 step 4) is the
    caller's check.
    """
    async with db.begin_nested():
        epoch = await db.get(
            LandscapeEpoch, epoch_id, with_for_update=True, populate_existing=True
        )
        if epoch is None or epoch.status != "candidate":
            raise ValueError(f"epoch {epoch_id} is not a candidate")
        old = await active_epoch(db)
        old_id = old.id if old is not None else None
        await on_activate(db, old_id, epoch_id)
        if old_id is not None:
            # Retire first: the one-active index is checked per statement.
            await db.execute(
                update(LandscapeEpoch)
                .where(LandscapeEpoch.id == old_id)
                .values(status="retired", retired_at=func.now(), updated_at=func.now())
            )
        await db.execute(
            update(LandscapeEpoch)
            .where(LandscapeEpoch.id == epoch_id)
            .values(status="active", activated_at=func.now(), updated_at=func.now())
        )
    await db.commit()


@dataclass
class _IdMaps:
    """Source row id -> copied row id, per referenced table."""

    ideas: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    extractions: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    cuts: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    deep: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)


def _reset_in_flight(values: dict) -> None:
    """A row still owed work in the source is owed it afresh in the copy: its batch item belongs
    to the source's submission, whose results would never reach the copy."""
    values["status"] = "pending"
    for column, fresh in (
        ("batch_item_id", None), ("attempts", 0), ("truncated_once", False),
        ("fallback_from", None), ("error", None),
    ):
        if column in values:
            values[column] = fresh


async def _copy_rows(
    db: AsyncSession,
    table: Table,
    where: ColumnElement[bool],
    to_epoch: uuid.UUID,
    *,
    refs: dict[str, tuple[dict[uuid.UUID, uuid.UUID], bool]] | None = None,
    adjust: Callable[[dict], None] | None = None,
    ids_from: dict[uuid.UUID, uuid.UUID] | None = None,
) -> dict[uuid.UUID, uuid.UUID]:
    """Insert a copy of every ``table`` row matching ``where`` into ``to_epoch``; returns source
    id -> new id.

    ``refs`` maps a column to (id map, required): a reference outside its map skips the row when
    required and becomes NULL otherwise. JSON contents are copied verbatim. A copy's id is fresh,
    unless the source row's id is a key of ``ids_from``: it then takes the mapped id (for a row
    whose id is another table's row id). Only inserts.
    """
    rows = (await db.execute(select(table).where(where))).mappings().all()
    ids: dict[uuid.UUID, uuid.UUID] = {}
    out: list[dict] = []
    for row in rows:
        values = {k: v for k, v in row.items() if k not in _FRESH_COLUMNS}
        if not _remap(values, refs or {}):
            continue
        new_id = (ids_from or {}).get(row["id"]) or uuid.uuid4()
        values["id"] = ids[row["id"]] = new_id
        if "epoch_id" in values:
            values["epoch_id"] = to_epoch
        if values.get("status") in _IN_FLIGHT:
            _reset_in_flight(values)
        if adjust is not None:
            adjust(values)
        out.append(values)
    for start in range(0, len(out), _INSERT_CHUNK):
        await db.execute(insert(table), out[start:start + _INSERT_CHUNK])
    return ids


def _remap(values: dict, refs: dict[str, tuple[dict[uuid.UUID, uuid.UUID], bool]]) -> bool:
    """Point ``values``' references at the copies; False when a required one has no copy."""
    for column, (mapping, required) in refs.items():
        old = values[column]
        if old is None:
            continue
        new = mapping.get(old)
        if new is None and required:
            return False
        values[column] = new
    return True


async def _copy_extract(db, src, dst, maps: _IdMaps, stages: set[str]) -> None:
    """S3: summary extractions and everything derived from them (ideas, links, aliases)."""
    ext, idea = LandscapeExtraction.__table__, LandscapeIdea.__table__
    maps.extractions.update(await _copy_rows(
        db, ext, and_(ext.c.epoch_id == src, ext.c.basis == "summary"), dst
    ))

    def _idea(values: dict) -> None:
        # Cut membership and S6's count belong to stages this copy may not carry.
        if "cut" not in stages:
            values["in_current_cut"] = False
        if "deep" not in stages:
            values["outside_group_count"] = None

    maps.ideas.update(await _copy_rows(db, idea, idea.c.epoch_id == src, dst, adjust=_idea))
    links = LandscapeIdeaLink.__table__
    await _copy_rows(db, links, links.c.epoch_id == src, dst, refs={
        "idea_a": (maps.ideas, True), "idea_b": (maps.ideas, True),
    })
    ext_links = LandscapeExtractionLink.__table__
    await _copy_rows(db, ext_links, ext_links.c.epoch_id == src, dst, refs={
        "extraction_id": (maps.extractions, False),
    })
    aliases = LandscapeIdeaAlias.__table__
    await _copy_rows(db, aliases, aliases.c.epoch_id == src, dst, refs={
        "idea_id": (maps.ideas, True),
    })


async def _copy_idea_evidence(db, src, dst, maps: _IdMaps) -> None:
    """Idea evidence, after every copied extraction is mapped (S6's full-text ones included)."""
    evidence, idea = LandscapeIdeaEvidence.__table__, LandscapeIdea.__table__
    source_ideas = exists().where(idea.c.id == evidence.c.idea_id, idea.c.epoch_id == src)
    await _copy_rows(db, evidence, source_ideas, dst, refs={
        "idea_id": (maps.ideas, True), "extraction_id": (maps.extractions, True),
    })


async def _copy_triage(db, src, dst, maps: _IdMaps, stages: set[str]) -> None:
    triage = LandscapeTriage.__table__
    await _copy_rows(db, triage, triage.c.epoch_id == src, dst, refs={
        "idea_id": (maps.ideas, True),
    })


async def _copy_comparisons(db, src, dst, maps: _IdMaps, basis: str) -> None:
    comp = LandscapeComparison.__table__
    await _copy_rows(db, comp, and_(comp.c.epoch_id == src, comp.c.basis == basis), dst, refs={
        "idea_a": (maps.ideas, True), "idea_b": (maps.ideas, True),
        "winner_idea_id": (maps.ideas, False),
    })


async def _copy_cut(db, src, dst, maps: _IdMaps, stages: set[str]) -> None:
    """S5: complete cuts (a ``boundary_pending`` one waits on the source's own batch) and the
    tie-break comparisons. A cut's idea ids (``ordered_idea_ids``, and in ``folds`` the
    keepers, the folded ids and the tie class's ``ids`` and ``versions`` keys) are repointed at
    the copied ideas."""
    cut = LandscapeCut.__table__
    by_str = {str(old): str(new) for old, new in maps.ideas.items()}

    def _id(value):
        return by_str.get(str(value), value)

    def _cut(values: dict) -> None:
        if values.get("ordered_idea_ids"):
            values["ordered_idea_ids"] = [_id(i) for i in values["ordered_idea_ids"]]
        if values.get("folds"):
            values["folds"] = _remap_folds(values["folds"], _id)

    maps.cuts.update(await _copy_rows(
        db, cut, and_(cut.c.epoch_id == src, cut.c.status == "complete"), dst, adjust=_cut
    ))
    await _copy_comparisons(db, src, dst, maps, "triage")


def _remap_folds(folds: dict, remap: Callable) -> dict:
    """``folds`` (``stages.cut``'s shape) with every idea id passed through ``remap``."""
    out = dict(folds)
    if isinstance(folds.get("folded"), dict):
        out["folded"] = {
            remap(keeper): [remap(i) for i in folded]
            for keeper, folded in folds["folded"].items()
        }
    tie = folds.get("tie_class")
    if isinstance(tie, dict):
        tie = dict(tie)
        if isinstance(tie.get("ids"), list):
            tie["ids"] = [remap(i) for i in tie["ids"]]
        if isinstance(tie.get("versions"), dict):
            tie["versions"] = {remap(i): v for i, v in tie["versions"].items()}
        out["tie_class"] = tie
    return out


async def _copy_deep(db, src, dst, maps: _IdMaps, stages: set[str]) -> None:
    """S6: its full-text extractions, deep records and consults."""
    ext, deep, consult = (
        LandscapeExtraction.__table__, LandscapeDeep.__table__, LandscapeConsult.__table__
    )
    maps.extractions.update(await _copy_rows(
        db, ext, and_(ext.c.epoch_id == src, ext.c.basis == "full_text"), dst
    ))
    maps.deep.update(await _copy_rows(db, deep, deep.c.epoch_id == src, dst, refs={
        "idea_id": (maps.ideas, True), "cut_id": (maps.cuts, False),
    }))
    source_deep = exists().where(deep.c.id == consult.c.deep_id, deep.c.epoch_id == src)
    await _copy_rows(db, consult, source_deep, dst, refs={"deep_id": (maps.deep, True)})


async def _copy_rerank(db, src, dst, maps: _IdMaps, stages: set[str]) -> None:
    """S7: its comparisons and the ranked list. A ranking row's id is the id of the S6 record it
    was placed on (``stages.rerank``), which is how a re-run S6 version is detected, so a copied
    row takes its S6 record's copy's id; one whose record had no copy keeps a fresh id."""
    await _copy_comparisons(db, src, dst, maps, "deep")
    ranking = LandscapeRanking.__table__
    await _copy_rows(db, ranking, ranking.c.epoch_id == src, dst, refs={
        "idea_id": (maps.ideas, True),
    }, ids_from=maps.deep)


async def _copy_similarity(db, src, dst, maps: _IdMaps, stages: set[str]) -> None:
    sim = LandscapeSimilarity.__table__
    await _copy_rows(db, sim, sim.c.epoch_id == src, dst, refs={
        "idea_a": (maps.ideas, True), "idea_b": (maps.ideas, True),
    })


async def _copy_label(db, src, dst, maps: _IdMaps, stages: set[str]) -> None:
    """S8: a candidate source's provisional groupings. An active source has none: its S8 writes
    opportunity versions, which only its own S8 and an activation create (D55)."""
    grouping = LandscapeCandidateGrouping.__table__
    await _copy_rows(db, grouping, grouping.c.epoch_id == src, dst, refs={
        "cut_id": (maps.cuts, True),
    })


async def _copy_screen(db, src, dst, maps: _IdMaps, stages: set[str]) -> None:
    screen = LandscapeScreen.__table__
    await _copy_rows(db, screen, screen.c.epoch_id == src, dst)


# S9 is never copied: its assessments key on groupings and versions the candidate makes anew.
_COPIERS = {
    "screen": _copy_screen,
    "extract": _copy_extract,
    "triage": _copy_triage,
    "cut": _copy_cut,
    "deep": _copy_deep,
    "rerank": _copy_rerank,
    "similarity": _copy_similarity,
    "label": _copy_label,
}


def _epoch_id(epoch: LandscapeEpoch | uuid.UUID) -> uuid.UUID:
    return epoch.id if isinstance(epoch, LandscapeEpoch) else epoch


async def copy_forward(
    db: AsyncSession,
    *,
    from_epoch: LandscapeEpoch | uuid.UUID | None,
    to_epoch: LandscapeEpoch | uuid.UUID,
    stages: tuple[str, ...],
) -> None:
    """Begin the candidate ``to_epoch``'s re-judge (§7.11 step 2): stamp its
    ``rejudge_started_at`` and copy ``from_epoch``'s rows of the unchanged ``stages`` into it.

    ``stages`` must be closed upstream (the complement of ``changed_stages``); ``final`` copies
    nothing. Copied rows get new ids (an S7 ranking row takes its copied S6 record's id), with
    their references to copied ideas, extractions, cuts and deep records repointed (a cut's
    idea ids inside its JSON too); a row still owed work is copied as ``pending``. Other JSON
    contents, and references to shared inputs (documents, batches), are copied verbatim. Only
    inserts: ``from_epoch``'s rows are never written. Raises ValueError for an unknown or not
    upstream-closed stage set, or when ``to_epoch`` is not a ``candidate``.

    A candidate built from nothing (the first epoch) passes ``from_epoch=None`` and no stages:
    the call then only stamps the start of its re-judge.
    """
    src = _epoch_id(from_epoch) if from_epoch is not None else None
    dst = _epoch_id(to_epoch)
    chosen = set(stages)
    if src is None and chosen:
        raise ValueError("copying stages needs an epoch to copy from")
    if chosen - set(_STAGES):
        raise ValueError(f"unknown stages {sorted(chosen - set(_STAGES))}")
    if not _upstream_closed(chosen):
        raise ValueError(f"stages {sorted(chosen)} omit a stage upstream of one they copy")
    target = await db.get(LandscapeEpoch, dst, populate_existing=True)
    if src == dst or target is None or target.status != "candidate":
        raise ValueError(f"epoch {dst} is not a candidate to copy into")
    # clock_timestamp, not now(): rows created earlier in this transaction count as before it.
    await db.execute(
        update(LandscapeEpoch)
        .where(LandscapeEpoch.id == dst)
        .values(rejudge_started_at=func.clock_timestamp(), updated_at=func.now())
    )
    maps = _IdMaps()
    for stage in _STAGES:
        if stage in chosen and stage in _COPIERS:
            await _COPIERS[stage](db, src, dst, maps, chosen)
    if "extract" in chosen:
        await _copy_idea_evidence(db, src, dst, maps)
    await db.commit()
    await db.refresh(target)


async def _phase(db: AsyncSession) -> str | None:
    result = await db.execute(select(AppSetting.value).where(AppSetting.key == "landscape_phase"))
    value = result.scalar_one_or_none()
    return value.strip().strip('"') if value is not None else None


def _old_document(document_id, cutoff) -> ColumnElement[bool]:
    doc = LandscapeDocument.__table__
    return exists().where(doc.c.id == document_id, doc.c.created_at < cutoff)


def _old_idea(idea_id, cutoff) -> ColumnElement[bool]:
    """The idea came from a document the candidate had when its re-judge began."""
    evidence, ext = LandscapeIdeaEvidence.__table__, LandscapeExtraction.__table__
    return exists().where(
        evidence.c.idea_id == idea_id,
        ext.c.id == evidence.c.extraction_id,
        _old_document(ext.c.document_id, cutoff),
    )


async def funnel_work_pending(
    db: AsyncSession, epoch_id: uuid.UUID, *, cutoff: datetime | None = None
) -> bool:
    """Whether S2, S3, S4 or evidence-assembly work is owed in ``epoch_id``: a row in flight, or
    an item its stage has not enqueued yet (an eligible document with no S2 row, a screened-in
    (or rescued, L44) document with no S3 row, an idea whose evidence is not assembled, an idea
    version with no S4 row). S4 rows count only at the idea's current version: a stale-version
    row is never sent.

    With ``cutoff``, only the items the epoch had then count: documents created before it, and
    ideas with evidence from such a document (§7.11 step 4).
    """
    D, S, E = LandscapeDocument, LandscapeScreen, LandscapeExtraction
    T, Idea = LandscapeTriage, LandscapeIdea

    def old_doc(document_id) -> tuple:
        return () if cutoff is None else (_old_document(document_id, cutoff),)

    def old_idea(idea_id) -> tuple:
        return () if cutoff is None else (_old_idea(idea_id, cutoff),)

    # Where D is in the outer FROM, compare its own column: an EXISTS over the same table
    # would auto-correlate to nothing.
    created_before = () if cutoff is None else (D.created_at < cutoff,)
    screened = select(S.id).where(S.document_id == D.id, S.epoch_id == epoch_id).exists()
    extracted = select(E.id).where(
        E.document_id == S.document_id, E.epoch_id == epoch_id, E.basis == "summary"
    ).exists()
    triaged = select(T.id).where(
        T.idea_id == Idea.id, T.epoch_id == epoch_id, T.idea_version == Idea.version_hash
    ).exists()
    checks = (
        select(S.id).where(
            S.epoch_id == epoch_id, S.status.in_(_IN_FLIGHT), *old_doc(S.document_id),
        ).exists(),
        select(E.id).where(
            E.epoch_id == epoch_id, E.basis == "summary", E.status.in_(_IN_FLIGHT),
            *old_doc(E.document_id),
        ).exists(),
        select(T.id).join(Idea, Idea.id == T.idea_id).where(
            T.epoch_id == epoch_id, T.status.in_(_IN_FLIGHT), T.idea_version == Idea.version_hash,
            *old_idea(T.idea_id),
        ).exists(),
        select(D.id).where(
            D.s1_status == "eligible", D.merged_into.is_(None), ~screened, *created_before,
        ).exists(),
        select(S.id).join(D, D.id == S.document_id).where(
            S.epoch_id == epoch_id,
            or_(and_(S.status == "done", S.decision.in_(("yes", "unsure"))),
                S.status == "rescued"),
            D.merged_into.is_(None), ~extracted, *created_before,
        ).exists(),
        select(Idea.id).where(
            Idea.epoch_id == epoch_id, Idea.evidence_assembled_at.is_(None),
            *old_idea(Idea.id),
        ).exists(),
        select(Idea.id).where(
            Idea.epoch_id == epoch_id, Idea.version_hash.is_not(None), ~triaged,
            *old_idea(Idea.id),
        ).exists(),
    )
    for check in checks:
        if await db.scalar(select(check)):
            return True
    return False


async def mark_drained(db: AsyncSession, *, epoch_id: uuid.UUID) -> bool:
    """Set ``drained_at`` once no S2, S3, S4 or evidence-assembly work is pending for any item
    (document, or idea from one) created before the epoch's ``rejudge_started_at`` (§7.11 step 4),
    whether its stage row is in flight or not yet enqueued (``funnel_work_pending``).

    Acts only in phase ``continuous`` and only after the re-judge has begun. Returns whether the
    epoch is drained; an earlier ``drained_at`` is kept (the D69 flip overwrites it in the worker).
    """
    epoch = await db.get(LandscapeEpoch, epoch_id, populate_existing=True)
    if epoch is None or epoch.rejudge_started_at is None:
        return False
    if epoch.drained_at is not None:
        return True
    if await _phase(db) != "continuous":
        return False
    if await funnel_work_pending(db, epoch_id, cutoff=epoch.rejudge_started_at):
        return False
    await db.execute(
        update(LandscapeEpoch)
        .where(LandscapeEpoch.id == epoch_id, LandscapeEpoch.drained_at.is_(None))
        .values(drained_at=func.now(), updated_at=func.now())
    )
    await db.commit()
    await db.refresh(epoch)
    return True
