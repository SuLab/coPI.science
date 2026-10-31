"""Ideas: canonical keys, merge, aliases, versions, dossiers and database evidence (spec
2026-10-30 §7.4 steps 5-7; D9, D27, D35, D36, D37, D64, L21, L59).

Ideas exist per epoch (§7.1); the ``idea_key`` string is what links one idea across epochs.

**Keys** (D9). Segments are joined by ``;`` and ids within a segment by ``,`` (both are
replaced inside ids, so a key always splits into the same four segments):

- biology: ``biology;<sorted target ids>;<direction>;<disease id>``;
- chemistry: ``chemistry;<agent id>;<sorted target ids>;<disease id>``, where the agent id is
  the ChEMBL id, else ``agent:<folded name>@<senior lab key>`` (L59).

A target id is its HGNC id, else its provisional key; a disease id its Mondo id, else its
provisional key, else empty. An idea whose key holds any unmatched facet is ``provisional``.

**Versions** (D37). An idea's ``version_hash`` covers its attached qualifying idea-source
documents (S1 ``eligible``, not a tombstone). It is recomputed only when a qualifying document
that was not yet attached attaches, or when a re-extracted candidate takes its document away
from the idea, so tombstoning a document, a database-evidence refresh and an evidence-only
document never change a stored version.

**Aliases** (D36, D64). When a key changes (a provisional facet resolves, or a chemistry key's
lab is unified under another lab), the idea is either re-keyed in place or, when an idea with
the new key already exists, merged into it. Either way an alias row records the old key, and a
merge re-points every row that references the merged idea, so foreign keys stay valid and
opportunities, whose versions list member ``idea_key`` strings, resolve through the alias.

Nothing here commits except `assemble_evidence`, which owns its sessions; callers own the
transaction of everything else.
"""
from __future__ import annotations

import hashlib
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Text, cast, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import epochs, normalize
from src.landscaping.evidence import cache, chembl, comention, opentargets, orphadata, trials
from src.landscaping.flags import combine_flags, data_flags
from src.models.landscape import (
    LandscapeComparison,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaAlias,
    LandscapeIdeaEvidence,
    LandscapeIdeaLink,
    LandscapeLab,
    LandscapeRanking,
    LandscapeRejectAudit,
    LandscapeRescue,
    LandscapeSimilarity,
    LandscapeTriage,
)
from src.services.job_queue import job_error_text

#: ``landscape_ideas.idea_key`` is String(500); a longer key keeps a prefix and a digest.
_KEY_MAX = 500
_SEP = ";"
#: A dossier carries at most this many documents (D35).
DOSSIER_CAP = 20
#: Document kinds that lead the dossier, before the newest qualifying papers (D35).
_LEAD_KINDS = frozenset({"grant", "patent", "jhtv"})
_PAPER_KINDS = frozenset({"work", "preprint"})
#: The database-evidence sources of §7.4 step 5, each tracked on the idea on its own.
EVIDENCE_SOURCES = (
    "opentargets_target", "opentargets_association", "chembl", "trials", "comention",
    "orphadata",
)
#: A source still failing after this many attempts is ``failed`` and shown as unavailable.
MAX_EVIDENCE_ATTEMPTS = 3
#: ``alias_of`` chains are flattened by `labs`; a longer walk means a hand-written cycle.
_MAX_LAB_HOPS = 10


# --- keys ------------------------------------------------------------------------------------


def _fold(text: object) -> str:
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(folded.split())


def _seg(value: object) -> str:
    return str(value).replace(_SEP, "_").replace(",", "_")


def _target_id(target: dict) -> str | None:
    found = target.get("hgnc_id") or target.get("provisional_key")
    if not found and (name := _fold(target.get("as_written"))):
        found = f"unmatched:{name}"
    return _seg(found) if found else None


def _target_ids(targets: object) -> str:
    ids = {_target_id(t) for t in targets or [] if isinstance(t, dict)}
    return ",".join(sorted(i for i in ids if i))


def _disease_id(disease: object) -> str:
    if not isinstance(disease, dict):
        return ""
    found = disease.get("mondo_id") or disease.get("provisional_key")
    if not found and (name := _fold(disease.get("as_written"))):
        found = f"unmatched:{name}"
    return _seg(found) if found else ""


def _agent_id(agent: object, senior_lab: str | None) -> str:
    agent = agent if isinstance(agent, dict) else {}
    if agent.get("chembl_id"):
        return _seg(agent["chembl_id"])
    return _seg(f"agent:{_fold(agent.get('as_written'))}@{senior_lab or ''}")


def _bounded(key: str) -> str:
    if len(key) <= _KEY_MAX:
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()
    return key[: _KEY_MAX - len(digest) - 1] + "#" + digest


def _direction(candidate: dict) -> str:
    return _seg(candidate.get("direction") or "unknown")


def idea_key(candidate: dict, *, senior_lab: str | None) -> str:
    """The canonical key of one normalized extraction candidate (D9, L59).

    `senior_lab` is the canonical lab key of the candidate's document at merge time; only a
    chemistry idea without a ChEMBL id carries it."""
    targets = _target_ids(candidate.get("targets"))
    disease = _disease_id(candidate.get("disease"))
    if candidate.get("idea_type") == "chemistry":
        agent = _agent_id(candidate.get("agent"), senior_lab)
        return _bounded(_SEP.join(("chemistry", agent, targets, disease)))
    return _bounded(_SEP.join(("biology", targets, _direction(candidate), disease)))


def _unknown_direction(candidate: dict) -> bool:
    return candidate.get("idea_type") != "chemistry" and _direction(candidate) == "unknown"


def _known_match(key: str, keys: Iterable[str]) -> str | None:
    """L21: the one biology key with `key`'s targets and disease and a known direction, or
    None when there are none or several."""
    parts = key.split(_SEP)
    found = {
        other for other in keys
        if (o := other.split(_SEP)) and len(o) == 4 and o[0] == "biology"
        and o[1] == parts[1] and o[3] == parts[3] and o[2] != "unknown"
    }
    return found.pop() if len(found) == 1 else None


def _is_provisional(candidate: dict) -> bool:
    targets = [t for t in candidate.get("targets") or [] if isinstance(t, dict)]
    if any(not t.get("hgnc_id") for t in targets):
        return True
    disease = candidate.get("disease")
    if isinstance(disease, dict) and _fold(disease.get("as_written")) and not disease.get(
        "mondo_id"
    ):
        return True
    agent = candidate.get("agent")
    return (
        candidate.get("idea_type") == "chemistry" and isinstance(agent, dict)
        and not agent.get("chembl_id")
    )


def group_candidates(
    candidates: list[dict], *, senior_labs: dict[uuid.UUID, str | None],
) -> dict[str, list[dict]]:
    """`merge_candidate`'s rule in memory, with no database writes (the bake-off's grouping).

    Candidates are taken in list order, each carrying its ``document_id``; `senior_labs` maps
    a document id to its canonical lab key. A ``direction: unknown`` biology candidate joins
    the one group already formed with its targets and disease and a known direction (L21);
    that choice is made once, when it is taken (D27). Returns ``{idea_key: [candidates]}``."""
    groups: dict[str, list[dict]] = {}
    for candidate in candidates:
        key = idea_key(candidate, senior_lab=senior_labs.get(candidate.get("document_id")))
        if _unknown_direction(candidate):
            key = _known_match(key, groups) or key
        groups.setdefault(key, []).append(candidate)
    return groups


# --- versions, dossiers, group counts ---------------------------------------------------------


def _attr(item: object, name: str) -> object:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _qualifying(document: object) -> bool:
    """A qualifying idea-source document: S1 eligible and not a tombstone (§6.1, D4)."""
    return _attr(document, "merged_into") is None and _attr(document, "s1_status") == "eligible"


def version_hash(qualifying_document_ids: Iterable[uuid.UUID]) -> str:
    """D37: sha256 over the sorted, de-duplicated ids of the idea's qualifying idea-source
    documents. Callers pass only qualifying documents, never tombstones."""
    ids = sorted({str(i) for i in qualifying_document_ids})
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def _newest_first(document: object) -> tuple:
    day = _attr(document, "published_on")
    return (day is None, -day.toordinal() if isinstance(day, date) else 0, str(_attr(document, "id")))


def dossier_documents(idea, documents) -> list:
    """The ≤ 20 documents an idea's dossier carries (D35): every grant, patent and JHTV listing
    first, then the most recent qualifying papers, each group newest first. Tombstones never
    appear; the caller lists the rest by id. `documents` are document rows or dicts with the
    same fields; `idea` is not read."""
    live = [d for d in documents if _attr(d, "merged_into") is None]
    lead = sorted((d for d in live if _attr(d, "kind") in _LEAD_KINDS), key=_newest_first)
    papers = sorted(
        (d for d in live if _attr(d, "kind") in _PAPER_KINDS and _qualifying(d)),
        key=_newest_first,
    )
    return (lead + papers)[:DOSSIER_CAP]


def group_counts(idea, documents) -> tuple[int, int]:
    """``(jhu_labs, outside_groups)``: the distinct lab keys of the idea's qualifying documents
    (tombstones never count, §7.4 step 6), and the outside groups S6 filled (0 before S6)."""
    labs = {_attr(d, "lab_key") for d in documents if _qualifying(d) and _attr(d, "lab_key")}
    return len(labs), int(_attr(idea, "outside_group_count") or 0)


# --- merge -------------------------------------------------------------------------------------


async def _canonical_lab(db: AsyncSession, key: str | None) -> str | None:
    current = key
    for _ in range(_MAX_LAB_HOPS):
        if not current:
            return current
        target = (await db.execute(
            select(LandscapeLab.alias_of).where(LandscapeLab.lab_key == current)
        )).scalar_one_or_none()
        if not target:
            return current
        current = target
    return current


async def _idea_documents(db: AsyncSession, idea_id: uuid.UUID) -> list[LandscapeDocument]:
    rows = await db.execute(
        select(LandscapeDocument)
        .join(LandscapeExtraction, LandscapeExtraction.document_id == LandscapeDocument.id)
        .join(LandscapeIdeaEvidence, LandscapeIdeaEvidence.extraction_id == LandscapeExtraction.id)
        .where(LandscapeIdeaEvidence.idea_id == idea_id)
        .distinct()
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


async def _qualifying_ids(db: AsyncSession, idea_id: uuid.UUID) -> set[uuid.UUID]:
    return {d.id for d in await _idea_documents(db, idea_id) if _qualifying(d)}


async def _refresh_version(db: AsyncSession, idea: LandscapeIdea) -> None:
    """Recompute the version and the JHU group count; called only when a qualifying document
    that was not yet attached has attached (D37), or one attached has been taken away."""
    await db.flush()
    documents = await _idea_documents(db, idea.id)
    idea.version_hash = version_hash(d.id for d in documents if _qualifying(d))
    idea.jhu_group_count = group_counts(idea, documents)[0]


async def _by_key(db: AsyncSession, epoch_id: uuid.UUID, key: str) -> LandscapeIdea | None:
    """The idea holding `key` in the epoch, directly or through an alias row (D36)."""
    idea = (await db.execute(
        select(LandscapeIdea).where(LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.idea_key == key)
    )).scalar_one_or_none()
    if idea is not None:
        return idea
    return (await db.execute(
        select(LandscapeIdea)
        .join(LandscapeIdeaAlias, LandscapeIdeaAlias.idea_id == LandscapeIdea.id)
        .where(LandscapeIdeaAlias.epoch_id == epoch_id, LandscapeIdeaAlias.old_key == key)
    )).scalar_one_or_none()


async def _known_match_db(db: AsyncSession, epoch_id: uuid.UUID, key: str) -> str | None:
    prefix = _SEP.join(key.split(_SEP)[:2]) + _SEP
    keys = (await db.execute(
        select(LandscapeIdea.idea_key).where(
            LandscapeIdea.epoch_id == epoch_id,
            LandscapeIdea.idea_type == "biology",
            LandscapeIdea.direction != "unknown",
            LandscapeIdea.idea_key.startswith(prefix, autoescape=True),
        )
    )).scalars().all()
    return _known_match(key, keys)


def _modalities(variants: list | None, candidate: dict) -> list:
    out = list(variants or [])
    modality = candidate.get("modality")
    text = modality.get("as_written") if isinstance(modality, dict) else None
    if text and text not in out:
        out.append(text)
    return out


def _stored_agent(candidate: dict, senior_lab: str | None) -> dict | None:
    agent = candidate.get("agent")
    if not isinstance(agent, dict) or not _fold(agent.get("as_written")):
        return None
    if candidate.get("idea_type") == "chemistry" and not agent.get("chembl_id"):
        return {**agent, "lab_key": senior_lab}
    return agent


def _normalization(candidate: dict) -> dict:
    disease = candidate.get("disease") if isinstance(candidate.get("disease"), dict) else {}
    agent = candidate.get("agent") if isinstance(candidate.get("agent"), dict) else {}
    return {
        "targets": [bool(t.get("hgnc_id")) for t in candidate.get("targets") or []
                    if isinstance(t, dict)],
        "disease": bool(disease.get("mondo_id")),
        "agent": bool(agent.get("chembl_id")),
    }


def _pending_status() -> dict:
    return {s: {"status": "pending", "attempts": 0} for s in EVIDENCE_SOURCES}


async def _get_or_create(
    db: AsyncSession, epoch_id: uuid.UUID, key: str, candidate: dict, senior_lab: str | None,
) -> LandscapeIdea:
    disease = candidate.get("disease")
    await db.execute(
        pg_insert(LandscapeIdea).values(
            id=uuid.uuid4(), epoch_id=epoch_id, idea_key=key,
            idea_type=candidate.get("idea_type") or "biology",
            provisional=_is_provisional(candidate),
            targets=[t for t in candidate.get("targets") or [] if isinstance(t, dict)] or None,
            direction=_direction(candidate) if candidate.get("idea_type") != "chemistry"
            else candidate.get("direction"),
            agent=_stored_agent(candidate, senior_lab),
            disease=disease if isinstance(disease, dict) and _disease_id(disease) else None,
            normalization=_normalization(candidate),
            modality_variants=_modalities(None, candidate) or None,
            stage=candidate.get("stage"), evidence_grade=candidate.get("evidence_grade"),
            evidence_status=_pending_status(),
        ).on_conflict_do_nothing(index_elements=["epoch_id", "idea_key"])
    )
    return (await db.execute(
        select(LandscapeIdea)
        .where(LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.idea_key == key)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def _link_types(
    db: AsyncSession, epoch_id: uuid.UUID, extraction_id: uuid.UUID, idea: LandscapeIdea,
    document_id: uuid.UUID,
) -> None:
    """Link this idea with the other-type ideas the same extraction produced (§7.4 step 7)."""
    others = (await db.execute(
        select(LandscapeIdea.id)
        .join(LandscapeIdeaEvidence, LandscapeIdeaEvidence.idea_id == LandscapeIdea.id)
        .where(
            LandscapeIdeaEvidence.extraction_id == extraction_id,
            LandscapeIdea.idea_type != idea.idea_type,
        )
    )).scalars().all()
    for other in set(others):
        bio, chem = (idea.id, other) if idea.idea_type == "biology" else (other, idea.id)
        await db.execute(
            pg_insert(LandscapeIdeaLink)
            .values(epoch_id=epoch_id, idea_a=bio, idea_b=chem, document_id=document_id)
            .on_conflict_do_nothing(index_elements=["epoch_id", "idea_a", "idea_b"])
        )


async def _prior_attachment(
    db: AsyncSession, epoch_id: uuid.UUID, extraction_id: uuid.UUID, index: int, key: str,
) -> uuid.UUID | None:
    """The idea this candidate already merged into, when it still describes that idea.

    A replayed candidate returns its first decision (D27); a candidate re-extracted with other
    facets (the document's summary changed) drops its stale attachment and merges afresh. The
    idea it leaves gets a fresh version and JHU group count when that took a qualifying
    document away from it, so S4 and S6 see the idea without the document."""
    row = (await db.execute(
        select(LandscapeIdeaEvidence, LandscapeIdea.idea_key)
        .join(LandscapeIdea, LandscapeIdea.id == LandscapeIdeaEvidence.idea_id)
        .where(
            LandscapeIdeaEvidence.extraction_id == extraction_id,
            LandscapeIdeaEvidence.candidate_index == index,
        )
    )).first()
    if row is None:
        return None
    evidence, prior_key = row
    if prior_key == key or (
        evidence.merge_reason == "unknown_direction_joined" and _known_match(key, [prior_key])
    ):
        return evidence.idea_id
    holder = await _by_key(db, epoch_id, key)  # the idea was re-keyed since (D36)
    if holder is not None and holder.id == evidence.idea_id:
        return evidence.idea_id
    left = await db.get(LandscapeIdea, evidence.idea_id)
    before = await _qualifying_ids(db, evidence.idea_id)
    await db.delete(evidence)
    await db.flush()
    if left is not None and before - await _qualifying_ids(db, left.id):
        await _refresh_version(db, left)
        await db.flush()
    return None


async def merge_candidate(
    db: AsyncSession, *, epoch_id: uuid.UUID, extraction_id: uuid.UUID, index: int,
    candidate: dict, document,
) -> uuid.UUID:
    """Merge one normalized S3 candidate (`index` within extraction `extraction_id`) into its
    idea in the epoch, creating the idea when its key is new, and return the idea's id.

    - The key is `idea_key` with the document's canonical lab key at merge time (D64), read
      through the epoch's alias rows (D36).
    - A ``direction: unknown`` biology candidate joins the one idea with its targets and
      disease and a known direction, when exactly one exists (L21); a candidate already
      merged keeps that decision (D27).
    - Ideas the same extraction produced of the other type are linked (biology ↔ chemistry).
    - The version and JHU group count are recomputed only when `document` is a qualifying
      idea-source document not yet attached (D37), and on the idea a stale attachment of
      this candidate leaves.

    `document` is the candidate's ``LandscapeDocument``. Flushes; does not commit."""
    senior = await _canonical_lab(db, document.lab_key)
    key = idea_key(candidate, senior_lab=senior)
    prior = await _prior_attachment(db, epoch_id, extraction_id, index, key)
    if prior is not None:
        return prior
    reason = "key"
    if _unknown_direction(candidate):
        joined = await _known_match_db(db, epoch_id, key)
        key, reason = (joined, "unknown_direction_joined") if joined else (
            key, "unknown_direction_own"
        )
    existing = await _by_key(db, epoch_id, key)
    idea = existing or await _get_or_create(db, epoch_id, key, candidate, senior)
    if existing is not None:
        idea.modality_variants = _modalities(idea.modality_variants, candidate) or None
    attached = await _qualifying_ids(db, idea.id)
    await db.execute(
        pg_insert(LandscapeIdeaEvidence)
        .values(idea_id=idea.id, extraction_id=extraction_id, candidate_index=index,
                merge_reason=reason)
        .on_conflict_do_nothing(index_elements=["idea_id", "extraction_id", "candidate_index"])
    )
    await _link_types(db, epoch_id, extraction_id, idea, document.id)
    if _qualifying(document) and document.id not in attached:
        await _refresh_version(db, idea)
    await db.flush()
    return idea.id


# --- aliases ----------------------------------------------------------------------------------

#: Every column that references an idea id, with the other columns of its unique key. A row
#: that would collide with one the surviving idea already has stays on the merged idea and
#: goes with it (``ON DELETE CASCADE``): the survivor already holds that result.
_REPOINT: tuple[tuple[type, str, tuple[str, ...]], ...] = (
    (LandscapeIdeaEvidence, "idea_id", ("extraction_id", "candidate_index")),
    (LandscapeIdeaLink, "idea_a", ("epoch_id", "idea_b")),
    (LandscapeIdeaLink, "idea_b", ("epoch_id", "idea_a")),
    (LandscapeIdeaAlias, "idea_id", ()),
    (LandscapeTriage, "idea_id", ("idea_version", "epoch_id")),
    (LandscapeDeep, "idea_id", ("idea_version", "epoch_id")),
    (LandscapeComparison, "idea_a",
     ("version_a", "idea_b", "version_b", "epoch_id", "basis", "ask_order")),
    (LandscapeComparison, "idea_b",
     ("idea_a", "version_a", "version_b", "epoch_id", "basis", "ask_order")),
    (LandscapeComparison, "winner_idea_id", ()),
    (LandscapeRanking, "idea_id", ("epoch_id",)),
    (LandscapeSimilarity, "idea_a", ("version_a", "idea_b", "version_b", "epoch_id")),
    (LandscapeSimilarity, "idea_b", ("idea_a", "version_a", "version_b", "epoch_id")),
    # Audits and rescues name their item by a plain id column, not a foreign key.
    (LandscapeRejectAudit, "item_id", ("item_kind", "epoch_id")),
    (LandscapeRescue, "item_id", ()),
)


async def _repoint(db: AsyncSession, src: uuid.UUID, dst: uuid.UUID) -> None:
    for model, column, unique in _REPOINT:
        table = model.__table__
        where = [table.c[column] == src]
        if unique:
            other = table.alias("other")
            clash = select(other.c.id).where(
                other.c[column] == dst, *(other.c[u] == table.c[u] for u in unique)
            ).correlate(table)
            where.append(~clash.exists())
        await db.execute(update(table).where(*where).values({column: dst}))
    # Collisions left on the merged idea without a cascading foreign key.
    await db.execute(
        LandscapeRejectAudit.__table__.delete().where(LandscapeRejectAudit.item_id == src)
    )


def _repointed(ids: list | None, old: str, new: str) -> list:
    """``ids`` with `old` read as `new`, each id kept once at its first place."""
    out: list = []
    for item in ids or []:
        item = new if item == old else item
        if item not in out:
            out.append(item)
    return out


def _repointed_folds(folded: dict, kept: list, old: str, new: str) -> dict:
    """``folds["folded"]`` with `old` read as `new`: a keeper's lists join, and an id is
    folded once, never under itself and never while it is a kept (ranked) idea."""
    out: dict[str, list[str]] = {}
    seen = set(kept)
    for keeper, members in folded.items():
        keeper = new if keeper == old else keeper
        for item in members:
            item = new if item == old else item
            if item != keeper and item not in seen:
                seen.add(item)
                out.setdefault(keeper, []).append(item)
    return out


async def _repoint_cuts(
    db: AsyncSession, epoch_id: uuid.UUID, src: uuid.UUID, dst: uuid.UUID,
) -> None:
    """Re-point the epoch's cuts, whose lists hold idea ids as JSON, from `src` to `dst`.

    In a complete cut, ``ordered_idea_ids`` and ``folds["folded"]`` name the survivor instead
    (`_repointed`, `_repointed_folds`); its ``tie_class`` stays the record of what was compared.
    A ``boundary_pending`` cut naming `src` is deleted: its order and tie class were drawn over
    idea versions that no longer exist, and finishing it would redraw pairs naming the deleted
    idea. Nothing attaches to a pending cut (S6 rows, groupings and opportunity versions take
    complete ones), the previous complete cut stays current, and the next ``run_cut`` takes a
    fresh cut from the pool (spec §7.6)."""
    old, new = str(src), str(dst)
    cuts = (await db.execute(
        select(LandscapeCut).where(
            LandscapeCut.epoch_id == epoch_id,
            or_(LandscapeCut.ordered_idea_ids.contains([old]),
                cast(LandscapeCut.folds, Text).contains(old)),
        )
    )).scalars().all()
    for cut in cuts:
        if cut.status == "boundary_pending":
            await db.delete(cut)
            continue
        cut.ordered_idea_ids = _repointed(cut.ordered_idea_ids, old, new)
        folds = cut.folds or {}
        if isinstance(folds.get("folded"), dict):
            cut.folds = {**folds, "folded": _repointed_folds(
                folds["folded"], cut.ordered_idea_ids, old, new
            )}


async def _record_alias(
    db: AsyncSession, epoch_id: uuid.UUID, old_key: str, idea_id: uuid.UUID,
) -> None:
    await db.execute(
        pg_insert(LandscapeIdeaAlias)
        .values(epoch_id=epoch_id, old_key=old_key, idea_id=idea_id)
        .on_conflict_do_update(index_elements=["epoch_id", "old_key"], set_={"idea_id": idea_id})
    )


async def _merge_into(db: AsyncSession, src: LandscapeIdea, dst: LandscapeIdea) -> None:
    """Merge `src` into `dst` in their epoch (D36): re-point, record the alias, delete `src`."""
    before = await _qualifying_ids(db, dst.id)
    incoming = await _qualifying_ids(db, src.id)
    await _repoint(db, src.id, dst.id)
    await _repoint_cuts(db, src.epoch_id, src.id, dst.id)
    await _record_alias(db, src.epoch_id, src.idea_key, dst.id)
    variants = list(dst.modality_variants or [])
    variants += [v for v in src.modality_variants or [] if v not in variants]
    dst.modality_variants = variants or None
    dst.in_current_cut = dst.in_current_cut or src.in_current_cut
    await db.delete(src)
    await db.flush()
    if incoming - before:
        await _refresh_version(db, dst)
    await db.flush()


async def _move(db: AsyncSession, idea: LandscapeIdea, new_key: str, fields: dict) -> None:
    """Give `idea` the key `new_key`: merge into the idea already holding it, else re-key in
    place. Both record the old key as an alias (D36, D64)."""
    target = await _by_key(db, idea.epoch_id, new_key)
    if target is not None and target.id != idea.id:
        await _merge_into(db, idea, target)
        return
    await _record_alias(db, idea.epoch_id, idea.idea_key, idea.id)
    idea.idea_key = new_key
    for name, value in fields.items():
        setattr(idea, name, value)
    await db.flush()


def _facets(idea: LandscapeIdea, **changes) -> dict:
    return {
        "idea_type": idea.idea_type, "targets": idea.targets, "direction": idea.direction,
        "agent": idea.agent, "disease": idea.disease, **changes,
    }


def _strip(item: dict, *names: str) -> dict:
    return {k: v for k, v in item.items() if k not in ("provisional_key", *names)}


async def _renormalized(idea: LandscapeIdea, criteria) -> dict:
    """The idea's facets with every unmatched one normalized again (no PubTator input)."""
    targets = [dict(t) for t in idea.targets or []]
    todo = [i for i, t in enumerate(targets) if not t.get("hgnc_id")]
    if todo:
        fresh = await normalize.normalize_targets(
            [_strip(targets[i]) for i in todo], pubtator=[], criteria=criteria
        )
        if len(fresh) == len(todo):
            for i, target in zip(todo, fresh, strict=True):
                targets[i] = target
    disease = idea.disease
    if isinstance(disease, dict) and not disease.get("mondo_id"):
        disease = await normalize.normalize_disease(_strip(disease), pubtator=[])
    agent = idea.agent
    if idea.idea_type == "chemistry" and isinstance(agent, dict) and not agent.get("chembl_id"):
        fresh_agent = await normalize.normalize_agent(_strip(agent, "lab_key"))
        agent = fresh_agent if fresh_agent.get("chembl_id") else {
            **fresh_agent, "lab_key": agent.get("lab_key")
        }
    return {"targets": targets or None, "disease": disease, "agent": agent}


async def resolve_aliases(db: AsyncSession, *, epoch_id: uuid.UUID) -> int:
    """Normalize every provisional idea of the epoch again and move the ones whose key
    changed: into the idea already holding the new key, through an alias row that keeps
    every foreign key valid, else re-keyed in place with an alias row (D36).

    Each idea is retried at most once a UTC day (``normalization["checked_on"]``), since
    normalization calls external lookups. Normalization uses the epoch's own criteria
    (``epochs.epoch_inputs``), so raises ``LookupError`` for an epoch without a snapshot.
    Returns how many ideas moved. Flushes only."""
    criteria = (await epochs.epoch_inputs(db, epoch_id)).criteria
    today = datetime.now(UTC).date().isoformat()
    ids = (await db.execute(
        select(LandscapeIdea.id)
        .where(LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.provisional.is_(True))
        .order_by(LandscapeIdea.created_at, LandscapeIdea.id)
    )).scalars().all()
    moved = 0
    for idea_id in ids:
        idea = await db.get(LandscapeIdea, idea_id)
        if idea is None or (idea.normalization or {}).get("checked_on") == today:
            continue
        fields = await _renormalized(idea, criteria)
        facets = _facets(idea, **fields)
        fields["provisional"] = _is_provisional(facets)
        fields["normalization"] = {**_normalization(facets), "checked_on": today}
        new_key = idea_key(facets, senior_lab=(fields["agent"] or {}).get("lab_key"))
        if new_key == idea.idea_key:
            idea.normalization = fields["normalization"]
            continue
        await _move(db, idea, new_key, fields)
        moved += 1
    await db.flush()
    return moved


async def apply_lab_unifications(db: AsyncSession) -> int:
    """D64: in every non-retired epoch, re-key each chemistry idea whose key carries a lab key
    that has since become an alias onto its lab's canonical key, merging into the idea already
    holding that key through an alias row (D36). Idempotent: a re-keyed idea carries the
    canonical key. Returns how many ideas moved. Runs in the caller's transaction; flushes."""
    live_epochs = select(LandscapeEpoch.id).where(LandscapeEpoch.status != "retired")
    aliased = select(LandscapeLab.lab_key).where(LandscapeLab.alias_of.is_not(None))
    ids = (await db.execute(
        select(LandscapeIdea.id)
        .where(
            LandscapeIdea.epoch_id.in_(live_epochs),
            LandscapeIdea.idea_type == "chemistry",
            LandscapeIdea.agent["lab_key"].astext.in_(aliased),
        )
        .order_by(LandscapeIdea.epoch_id, LandscapeIdea.created_at, LandscapeIdea.id)
    )).scalars().all()
    moved = 0
    for idea_id in ids:
        idea = await db.get(LandscapeIdea, idea_id)
        if idea is None:
            continue
        lab = (idea.agent or {}).get("lab_key")
        canonical = await _canonical_lab(db, lab)
        if canonical == lab:
            continue
        agent = {**idea.agent, "lab_key": canonical}
        new_key = idea_key(_facets(idea, agent=agent), senior_lab=canonical)
        await _move(db, idea, new_key, {"agent": agent})
        moved += 1
    await db.flush()
    return moved


# --- database evidence -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    id: str
    ensembl: str | None
    symbol: str | None
    chembl_target: str | None
    uniprot: str | None


@dataclass(frozen=True)
class _Context:
    targets: tuple[_Target, ...]
    mondo: str | None
    mesh: str | None
    orpha: str | None
    disease_label: str | None
    agent_name: str | None


def _context(targets: list[dict], disease: dict | None, agent: dict | None) -> _Context:
    """Lookup ids of the matched targets (HGNC) and of the disease and agent."""
    disease = disease or {}
    agent = agent or {}
    matched = tuple(
        _Target(
            id=str(t["hgnc_id"]),
            ensembl=t.get("ensembl_id") or t.get("ensembl_gene_id"),
            symbol=t.get("symbol") or t.get("as_written"),
            chembl_target=t.get("chembl_target_id"),
            uniprot=t.get("uniprot_id"),
        )
        for t in targets or [] if isinstance(t, dict) and t.get("hgnc_id")
    )
    return _Context(
        targets=matched,
        mondo=disease.get("mondo_id"),
        mesh=disease.get("mesh_id"),
        orpha=disease.get("orpha_code"),
        disease_label=disease.get("label") or disease.get("as_written") or None,
        agent_name=agent.get("name") or agent.get("as_written") or None,
    )


def _call(fn: Callable[..., Awaitable], *args, wrap: str | None = None, **kwargs):
    """A cache fetch for ``fn(*args, **kwargs)``; a non-dict result is stored under `wrap`."""
    async def fetch() -> dict:
        result = await fn(*args, **kwargs)
        return {wrap: result} if wrap else result
    return fetch


#: Cache source -> the source whose published release keys it (``cache.RELEASE_RESOLVERS``).
#: A source not listed is keyed by its own name, which for a release-less source gives the
#: ISO week of the fetch (§11.2).
_RELEASE_SOURCE = {
    "opentargets_target": "opentargets",
    "opentargets_association": "opentargets",
    "chembl_mechanisms": "chembl",
    "chembl_molecule": "chembl",
    "chembl_target": "chembl",
}


async def _cached(db: AsyncSession, source: str, key: str, fetch) -> dict:
    """``fetch`` through the evidence cache under the source's current release."""
    release = await cache.current_release(_RELEASE_SOURCE.get(source, source))
    return await cache.cached(db, source, release, key, fetch)


async def _ot_target(db: AsyncSession, ctx: _Context) -> dict:
    return {
        t.id: await _cached(db, "opentargets_target", t.ensembl,
                            _call(opentargets.ot_target, t.ensembl))
        for t in ctx.targets if t.ensembl
    }


async def _ot_association(db: AsyncSession, ctx: _Context) -> dict:
    if not ctx.mondo:
        return {}
    return {
        t.id: await _cached(db, "opentargets_association", f"{t.ensembl}|{ctx.mondo}",
                            _call(opentargets.ot_association, t.ensembl, ctx.mondo))
        for t in ctx.targets if t.ensembl
    }


async def _chembl_target(db: AsyncSession, target: _Target) -> str | None:
    """The target's ChEMBL target id: as stored, else resolved from its UniProt accession."""
    if target.chembl_target or not target.uniprot:
        return target.chembl_target
    found = await _cached(db, "chembl_target", target.uniprot,
                          _call(chembl.chembl_target_for_uniprot, target.uniprot,
                                wrap="target_chembl_id"))
    return found.get("target_chembl_id")


async def _chembl(db: AsyncSession, ctx: _Context) -> dict:
    mechanisms: dict = {}
    for t in ctx.targets:
        chembl_target = await _chembl_target(db, t)
        if chembl_target:
            mechanisms[t.id] = (await _cached(
                db, "chembl_mechanisms", chembl_target,
                _call(chembl.chembl_mechanisms, chembl_target, wrap="rows")))["rows"]
    out: dict = {"mechanisms": mechanisms}
    if ctx.agent_name:
        found = await _cached(db, "chembl_molecule", _fold(ctx.agent_name),
                              _call(chembl.chembl_molecule, ctx.agent_name, wrap="rows"))
        out["molecule"] = found["rows"]
    return out


async def _trials(db: AsyncSession, ctx: _Context) -> dict:
    if not ctx.disease_label:
        return {}
    interventions = {t.id: t.symbol for t in ctx.targets if t.symbol}
    if ctx.agent_name:
        interventions["agent"] = ctx.agent_name
    return {
        name: await _cached(db, "trials", f"{_fold(ctx.disease_label)}|{_fold(term)}",
                            _call(trials.trials_count, ctx.disease_label, term))
        for name, term in interventions.items()
    }


async def _comention(db: AsyncSession, ctx: _Context) -> dict:
    if not ctx.mesh:
        return {}
    today = datetime.now(UTC).date()
    window = (today - timedelta(days=get_settings().landscape_window_days), today)
    return {
        t.id: await _cached(db, "comention", f"{t.symbol}|{ctx.mesh}|{window[0].isoformat()}",
                            _call(comention.comention_counts, t.symbol, ctx.mesh, window=window))
        for t in ctx.targets if t.symbol
    }


async def _orphadata(db: AsyncSession, ctx: _Context) -> dict:
    if not ctx.orpha:
        return {}
    return await _cached(db, "orphadata", str(ctx.orpha),
                         _call(orphadata.orphadata_prevalence, ctx.orpha, wrap="prevalence"))


_FETCHERS: dict[str, Callable[[AsyncSession, _Context], Awaitable[dict]]] = {
    "opentargets_target": _ot_target,
    "opentargets_association": _ot_association,
    "chembl": _chembl,
    "trials": _trials,
    "comention": _comention,
    "orphadata": _orphadata,
}


async def _fetch_sources(
    db: AsyncSession, ctx: _Context, only: Iterable[str],
) -> tuple[dict, dict]:
    """One attempt at each source in `only`: ``(payload by source, error text by source)``.
    A source that needs an id the idea lacks succeeds with an empty payload."""
    payloads: dict = {}
    errors: dict = {}
    for source in EVIDENCE_SOURCES:
        if source not in only:
            continue
        try:
            async with db.begin_nested():
                payloads[source] = await _FETCHERS[source](db, ctx)
        except Exception as exc:  # each source fails on its own (§7.4 step 5)
            errors[source] = job_error_text(exc)
    return payloads, errors


def _flags(summary: dict, ctx: _Context) -> dict:
    """The data flags from the assembled summary; an unavailable source raises nothing."""
    ot = summary.get("opentargets_target") or {}
    trial_counts = summary.get("trials") or {}
    per_target = {}
    for t in ctx.targets:
        target = ot.get(t.id) if isinstance(ot.get(t.id), dict) else {}
        per_target[t.id] = data_flags(
            target, target.get("drugAndClinicalCandidates") or {},
            trial_counts.get(t.id) if isinstance(trial_counts.get(t.id), dict) else {},
            disease=ctx.mondo or "",
        )
    return combine_flags(per_target)


async def evidence_payload(
    db: AsyncSession, *, targets: list[dict], disease: dict | None, agent: dict | None,
) -> dict:
    """§7.4 step 5 for one set of facets, through the evidence cache, writing no idea:
    ``{"evidence_summary": {source: payload}, "data_flags": {...}, "errors": {source: text}}``.

    One attempt per source; a failed source is absent from the summary and named in
    ``errors``. Cache rows are written in the caller's transaction."""
    ctx = _context(targets, disease, agent)
    payloads, errors = await _fetch_sources(db, ctx, EVIDENCE_SOURCES)
    return {"evidence_summary": payloads, "data_flags": _flags(payloads, ctx), "errors": errors}


async def assemble_evidence(db_factory, *, epoch_id: uuid.UUID, idea_id: uuid.UUID) -> None:
    """One assembly pass for an idea (§7.4 step 5): one attempt at each source still pending.

    Each source is tracked in ``evidence_status`` on its own as ``{"status", "attempts"}``. A
    source still failing after `MAX_EVIDENCE_ATTEMPTS` attempts becomes ``failed`` and its
    summary entry ``{"unavailable": True, "error": ...}``, which the S4 dossier shows. Once
    every source is done or failed, the data flags are computed from the summary and
    ``evidence_assembled_at`` is stamped. A refresh is requested by setting sources back to
    pending (or clearing ``evidence_status``). Never changes the idea's version (D37).
    Commits; does nothing for an idea not in the epoch or with no pending source."""
    async with db_factory() as db:
        idea = (await db.execute(
            select(LandscapeIdea).where(
                LandscapeIdea.id == idea_id, LandscapeIdea.epoch_id == epoch_id
            )
        )).scalar_one_or_none()
        if idea is None:
            return
        stored = idea.evidence_status or {}
        status = {
            s: dict(stored.get(s) or {"status": "pending", "attempts": 0})
            for s in EVIDENCE_SOURCES
        }
        todo = {s for s, st in status.items() if st.get("status") == "pending"}
        if not todo:
            return
        ctx = _context(idea.targets or [], idea.disease, idea.agent)
        payloads, errors = await _fetch_sources(db, ctx, todo)
        summary = dict(idea.evidence_summary or {})
        for source in todo:
            entry = status[source]
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            if source in payloads:
                entry["status"] = "done"
                summary[source] = payloads[source]
            elif entry["attempts"] >= MAX_EVIDENCE_ATTEMPTS:
                entry["status"] = "failed"
                summary[source] = {"unavailable": True, "error": errors.get(source)}
        idea.evidence_status = status
        idea.evidence_summary = summary
        if all(st["status"] in ("done", "failed") for st in status.values()):
            idea.data_flags = _flags(summary, ctx)
            idea.evidence_assembled_at = datetime.now(UTC)
        await db.commit()
