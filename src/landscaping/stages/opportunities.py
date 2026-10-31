"""S8 opportunities: joining edges, related links, labels, identity and probes (spec §7.9;
L42, L57, L60, D14, D29, D55, D59, D62, D64).

An opportunity is a connected component of the S6-passed ideas of a cut's top K over the
**joining** edges only (L57):

- ``lab_target`` / ``lab_disease``: the same canonical lab key (D49) and a shared target (HGNC
  id, else provisional key) or a shared Mondo disease;
- ``document_link``: a biology–chemistry link from one document (``landscape_idea_links``,
  §7.4 step 7).

**Related** links (``related_edges``) are shown on cards and never merge them: the same HGNC
target or gene group, the same Mondo disease or a shared direct parent, a shared source
document, and a ``same_program`` similarity judgment (``judge_similarity``, D14).

**Who writes what** (D55). Only the active epoch's S8 and an epoch's activation create, version
or retire opportunities. A candidate (or held) epoch's S8 writes ``landscape_candidate_groupings``
only; ``activate_groupings`` matches them at activation (D59). ``make_probe`` is the one
exception (D62): a probe opportunity takes no part in grouping, matching or retirement.

**Identity** (L60). ``match_groups`` pairs groups with live opportunities one-to-one, by
``idea_key`` after resolving both epochs' idea aliases (D36, D64). A member's version is the
idea version its S6 record judged; a matched opportunity gets a new version when its members
change or a member's S6 record changed after its current version was made (the version row
stores member keys only, so the S6 record's ``created_at``/``updated_at`` is what is compared).

**Labels** (stage ``label``) go through a Message Batch for groups of two or more; a single-idea
group is labelled by its structured line (D29) with no call. Every request takes its system
prompt and model from the row's epoch's own inputs (``epochs.epoch_inputs``, spec §7.11).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import batches, epochs
from src.landscaping.batches import BatchOutcome, FallbackAdapter
from src.landscaping.comparator import deep_text
from src.landscaping.epochs import EpochInputs
from src.landscaping.evidence import mondo
from src.landscaping.llm_client import CallResult, build_request, call_sync
from src.landscaping.schemas import SCHEMAS
from src.landscaping.stages.screen import message_text, parse_output, served_by, submit_items
from src.landscaping.structured_lines import structured_line
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeBatch,
    LandscapeCandidateGrouping,
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
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeSimilarity,
)
from src.services.job_queue import job_error_text

logger = logging.getLogger(__name__)

LABEL_STAGE = "label"
SIMILARITY_STAGE = "similarity"
#: Output bounds the prompts state and structured output cannot enforce (§7.9).
LABEL_MAX_CHARS = 60
THEME_MAX_CHARS = 200
REASON_MAX_CHARS = 200
#: ``landscape_similarity.verdict`` values. ``refused``, ``truncated`` and ``invalid`` are
#: stored too, so a pair that cannot be judged is not billed again at the same versions.
SAME_PROGRAM = "same"
DIFFERENT_PROGRAM = "different"

#: An S6 record whose idea may join an opportunity.
_PASSED = ("done", "rescued")
_IN_FLIGHT = ("pending", "submitting", "submitted", "running")
_GROUPING_EPOCHS = ("candidate", "held")
_CHUNK = 1000
_MAX_HOPS = 10


def _chunks(values: Sequence, size: int = _CHUNK) -> Iterable[Sequence]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _clip(text: Any, limit: int) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _follow(key: str, mapping: dict[str, str]) -> str:
    """``key`` followed through ``mapping`` (an alias chain) for at most ``_MAX_HOPS`` hops."""
    for _ in range(_MAX_HOPS):
        nxt = mapping.get(key)
        if nxt is None or nxt == key:
            return key
        key = nxt
    return key


def _version(idea: LandscapeIdea) -> str:
    return idea.version_hash or ""


# --- what the edges read -------------------------------------------------------------------


@dataclass(frozen=True)
class IdeaFacts:
    """The facets of one idea in one epoch that S8's edges compare (``idea_facts``)."""

    id: uuid.UUID
    idea_key: str
    version: str
    #: HGNC id, else provisional key, case-folded: what a same-lab join compares.
    targets: frozenset[str] = frozenset()
    #: HGNC ids only: the "same target" related link.
    hgnc: frozenset[str] = frozenset()
    #: HGNC gene groups, read from the target records when they carry ``gene_group``.
    gene_groups: frozenset[str] = frozenset()
    #: The Mondo id; None when the disease is absent or unmatched.
    disease: str | None = None
    disease_parents: frozenset[str] = frozenset()
    #: Canonical lab keys of the idea's qualifying documents (D49, D64).
    labs: frozenset[str] = frozenset()
    documents: frozenset[uuid.UUID] = frozenset()
    #: Ideas linked to this one by one document (§7.4 step 7).
    linked: frozenset[uuid.UUID] = frozenset()
    #: Ideas judged the same program at both ideas' current versions (D14).
    similar: frozenset[uuid.UUID] = frozenset()
    #: The ``LandscapeIdea`` row, for structured lines and messages.
    idea: Any = field(default=None, compare=False, repr=False)


def _target_facets(targets: Any) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    joined: set[str] = set()
    hgnc: set[str] = set()
    groups: set[str] = set()
    for target in targets or []:
        if not isinstance(target, dict):
            continue
        if target.get("hgnc_id"):
            hgnc.add(str(target["hgnc_id"]))
        key = target.get("hgnc_id") or target.get("provisional_key")
        if key:
            joined.add(str(key).strip().casefold())
        for group in target.get("gene_group") or []:
            groups.add(str(group))
    return frozenset(joined), frozenset(hgnc), frozenset(groups)


def _parents(mondo_id: str | None) -> frozenset[str]:
    if not mondo_id:
        return frozenset()
    try:
        return frozenset(mondo.mondo_parents(mondo_id))
    except mondo.MondoNotLoaded:
        return frozenset()


async def _idea_documents(
    db: AsyncSession, idea_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str | None]]]:
    """Per idea, its qualifying documents (S1 eligible, not a tombstone) and their lab keys."""
    D, E, X = LandscapeDocument, LandscapeIdeaEvidence, LandscapeExtraction
    out: dict[uuid.UUID, list[tuple[uuid.UUID, str | None]]] = defaultdict(list)
    for chunk in _chunks(list(idea_ids)):
        rows = (
            await db.execute(
                select(E.idea_id, D.id, D.lab_key)
                .join(X, X.id == E.extraction_id)
                .join(D, D.id == X.document_id)
                .where(E.idea_id.in_(chunk), D.s1_status == "eligible", D.merged_into.is_(None))
                .distinct()
            )
        ).all()
        for idea_id, doc_id, lab_key in rows:
            out[idea_id].append((doc_id, lab_key))
    return out


async def _lab_aliases(db: AsyncSession) -> dict[str, str]:
    rows = (
        await db.execute(
            select(LandscapeLab.lab_key, LandscapeLab.alias_of)
            .where(LandscapeLab.alias_of.is_not(None))
        )
    ).all()
    return {key: alias_of for key, alias_of in rows}


async def _linked(
    db: AsyncSession, epoch_id: uuid.UUID, idea_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    L = LandscapeIdeaLink
    out: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for chunk in _chunks(list(idea_ids)):
        rows = (
            await db.execute(
                select(L.idea_a, L.idea_b)
                .where(L.epoch_id == epoch_id, or_(L.idea_a.in_(chunk), L.idea_b.in_(chunk)))
            )
        ).all()
        for a, b in rows:
            out[a].add(b)
            out[b].add(a)
    return out


async def _similar(
    db: AsyncSession, epoch_id: uuid.UUID, versions: dict[uuid.UUID, str]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    S = LandscapeSimilarity
    out: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for chunk in _chunks(list(versions)):
        rows = (
            await db.execute(
                select(S.idea_a, S.version_a, S.idea_b, S.version_b)
                .where(S.epoch_id == epoch_id, S.verdict == SAME_PROGRAM, S.idea_a.in_(chunk))
            )
        ).all()
        for a, va, b, vb in rows:
            if versions.get(a) == va and versions.get(b) == vb:
                out[a].add(b)
                out[b].add(a)
    return out


async def idea_facts(
    db: AsyncSession, *, epoch_id: uuid.UUID, idea_ids: Iterable[uuid.UUID]
) -> list[IdeaFacts]:
    """``IdeaFacts`` for the given ideas of the epoch, in the given order; ids not in the
    epoch are left out. Reads only; Mondo parents are empty while no Mondo release is
    installed."""
    ids = list(dict.fromkeys(idea_ids))
    ideas: list[LandscapeIdea] = []
    for chunk in _chunks(ids):
        ideas.extend(
            (
                await db.scalars(
                    select(LandscapeIdea)
                    .where(LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.id.in_(chunk))
                )
            ).all()
        )
    order = {idea_id: n for n, idea_id in enumerate(ids)}
    ideas.sort(key=lambda idea: order[idea.id])
    present = [idea.id for idea in ideas]
    documents = await _idea_documents(db, present)
    labs = await _lab_aliases(db)
    linked = await _linked(db, epoch_id, present)
    similar = await _similar(db, epoch_id, {idea.id: _version(idea) for idea in ideas})
    out = []
    for idea in ideas:
        targets, hgnc, groups = _target_facets(idea.targets)
        disease = idea.disease.get("mondo_id") if isinstance(idea.disease, dict) else None
        docs = documents.get(idea.id, [])
        out.append(IdeaFacts(
            id=idea.id, idea_key=idea.idea_key, version=_version(idea),
            targets=targets, hgnc=hgnc, gene_groups=groups,
            disease=disease or None, disease_parents=_parents(disease),
            labs=frozenset(_follow(lab, labs) for _, lab in docs if lab),
            documents=frozenset(doc_id for doc_id, _ in docs),
            linked=frozenset(linked.get(idea.id, ())),
            similar=frozenset(similar.get(idea.id, ())),
            idea=idea,
        ))
    return out


# --- edges and components -------------------------------------------------------------------


def _shared_pairs(
    ideas: Sequence[IdeaFacts], values: Callable[[IdeaFacts], Iterable]
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Every pair of ideas (lower id first) that share a value of ``values``."""
    index: dict[Any, set[uuid.UUID]] = defaultdict(set)
    for idea in ideas:
        for value in values(idea):
            index[value].add(idea.id)
    pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for members in index.values():
        pairs.update(combinations(sorted(members), 2))
    return pairs


def _listed_pairs(ideas: Sequence[IdeaFacts], attr: str) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Pairs within ``ideas`` named by each idea's ``attr`` set (lower id first)."""
    present = {idea.id for idea in ideas}
    return {
        (min(idea.id, other), max(idea.id, other))
        for idea in ideas for other in getattr(idea, attr)
        if other in present and other != idea.id
    }


def opportunity_edges(ideas) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """The joining edges among ``ideas`` (L57), each ``(lower id, higher id, kind)``:
    ``lab_target`` and ``lab_disease`` for a shared lab key plus a shared target or Mondo
    disease, ``document_link`` for a biology–chemistry link from one document. A pair may
    carry more than one kind. ``ideas`` are ``IdeaFacts``."""
    ideas = list(ideas)
    by_id = {idea.id: idea for idea in ideas}
    edges: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for a, b in sorted(_shared_pairs(ideas, lambda idea: idea.labs)):
        x, y = by_id[a], by_id[b]
        if x.targets & y.targets:
            edges.append((a, b, "lab_target"))
        if x.disease and x.disease == y.disease:
            edges.append((a, b, "lab_disease"))
    edges.extend((a, b, "document_link") for a, b in sorted(_listed_pairs(ideas, "linked")))
    return edges


_RELATED: tuple[tuple[str, Callable[[IdeaFacts], Iterable]], ...] = (
    ("same_target", lambda idea: idea.hgnc),
    ("gene_group", lambda idea: idea.gene_groups),
    ("same_disease", lambda idea: (idea.disease,) if idea.disease else ()),
    ("disease_parent", lambda idea: idea.disease_parents),
    ("shared_document", lambda idea: idea.documents),
)


def related_edges(ideas) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """The "related" links among ``ideas`` (spec §7.9), each ``(lower id, higher id, kind)``,
    sorted: ``same_target``, ``gene_group``, ``same_disease``, ``disease_parent`` (a shared
    direct Mondo parent), ``shared_document`` and ``similarity`` (a stored ``same_program``
    verdict at both current versions). They never merge cards; pairs inside one opportunity
    are listed too, and callers drop the ones they do not show. Every pair carrying a kind
    other than ``similarity`` is a candidate for ``judge_similarity``."""
    ideas = list(ideas)
    edges = {
        (a, b, kind) for kind, values in _RELATED for a, b in _shared_pairs(ideas, values)
    }
    edges.update((a, b, "similarity") for a, b in _listed_pairs(ideas, "similar"))
    return sorted(edges)


def _components(
    ideas: Sequence[IdeaFacts], edges: Iterable[tuple[uuid.UUID, uuid.UUID, str]]
) -> list[tuple[list[IdeaFacts], list[str]]]:
    """Connected components of ``ideas`` over ``edges``, each with its sorted edge kinds,
    ordered by their smallest ``idea_key``."""
    parent = {idea.id: idea.id for idea in ideas}

    def find(node: uuid.UUID) -> uuid.UUID:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    edges = list(edges)
    for a, b, _ in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    members: dict[uuid.UUID, list[IdeaFacts]] = defaultdict(list)
    kinds: dict[uuid.UUID, set[str]] = defaultdict(set)
    for idea in ideas:
        members[find(idea.id)].append(idea)
    for a, _, kind in edges:
        kinds[find(a)].add(kind)
    roots = sorted(members, key=lambda root: min(idea.idea_key for idea in members[root]))
    return [(members[root], sorted(kinds[root])) for root in roots]


# --- identity -------------------------------------------------------------------------------


def match_groups(
    groups: list[frozenset[str]],
    current: dict[uuid.UUID, frozenset[str]],
    ages: dict[uuid.UUID, datetime],
) -> dict[int, uuid.UUID]:
    """One-to-one matching of new groups to existing opportunities (spec §7.9, L60).

    ``groups`` and ``current`` (each opportunity's current members) hold ``idea_key``s the
    caller has already resolved through both epochs' aliases (D64); ``ages`` is each
    opportunity's creation time. A pair qualifies when the group holds at least half of the
    opportunity's members. Pairs are taken by overlap (larger first), then opportunity age
    (older first), then opportunity id, then group index, each group and each opportunity at
    most once. Returns ``{group index: opportunity id}``.
    """
    by_key: dict[str, set[uuid.UUID]] = defaultdict(set)
    for opportunity_id, members in current.items():
        for key in members:
            by_key[key].add(opportunity_id)
    pairs = []
    for index, group in enumerate(groups):
        for opportunity_id in set().union(*(by_key.get(key, set()) for key in group)):
            members = current[opportunity_id]
            overlap = len(group & members)
            if 2 * overlap >= len(members):
                pairs.append((-overlap, ages[opportunity_id], opportunity_id, index))
    pairs.sort()
    matched: dict[int, uuid.UUID] = {}
    used: set[uuid.UUID] = set()
    for _, _, opportunity_id, index in pairs:
        if index in matched or opportunity_id in used:
            continue
        matched[index] = opportunity_id
        used.add(opportunity_id)
    return matched


async def _alias_map(db: AsyncSession, epoch_ids: Iterable[uuid.UUID]) -> dict[str, str]:
    """Old key -> the key of the idea it merged into, over the given epochs' alias rows."""
    A, Idea = LandscapeIdeaAlias, LandscapeIdea
    rows = (
        await db.execute(
            select(A.old_key, Idea.idea_key)
            .join(Idea, Idea.id == A.idea_id)
            .where(A.epoch_id.in_(list(set(epoch_ids))))
        )
    ).all()
    return {old: new for old, new in rows if old != new}


def _resolved(keys: Iterable[str], aliases: dict[str, str]) -> frozenset[str]:
    return frozenset(_follow(key, aliases) for key in keys)


async def _current_versions(
    db: AsyncSession,
) -> dict[uuid.UUID, tuple[LandscapeOpportunity, LandscapeOpportunityVersion]]:
    """Every live, non-probe opportunity with its current version (``version_counter``)."""
    Opp, V = LandscapeOpportunity, LandscapeOpportunityVersion
    rows = (
        await db.execute(
            select(Opp, V)
            .join(V, (V.opportunity_id == Opp.id) & (V.version == Opp.version_counter))
            .where(Opp.retired_at.is_(None), V.is_probe.is_(False))
        )
    ).all()
    return {opportunity.id: (opportunity, version) for opportunity, version in rows}


async def _match_current(
    db: AsyncSession, groups: list[list[str]], epoch_ids: set[uuid.UUID],
) -> tuple[dict[int, uuid.UUID], dict, dict[str, str]]:
    """``(match, current versions, aliases)`` for groups of member keys against every live
    opportunity, keys resolved through the aliases of ``epoch_ids`` and of each current
    version's epoch."""
    current = await _current_versions(db)
    aliases = await _alias_map(db, epoch_ids | {v.epoch_id for _, v in current.values()})
    match = match_groups(
        [_resolved(keys, aliases) for keys in groups],
        {oid: _resolved(v.member_idea_keys or [], aliases) for oid, (_, v) in current.items()},
        {oid: opportunity.created_at for oid, (opportunity, _) in current.items()},
    )
    return match, current, aliases


async def _new_opportunity(db: AsyncSession) -> LandscapeOpportunity:
    opportunity = LandscapeOpportunity(id=uuid.uuid4(), version_counter=0)
    db.add(opportunity)
    # No relationship is declared, so the row is flushed before any version references it.
    await db.flush()
    return opportunity


def _mint(
    db: AsyncSession, opportunity: LandscapeOpportunity, *, epoch_id: uuid.UUID,
    cut_id: uuid.UUID | None, members: list[str], edge_kinds: Iterable[str], **label: Any,
) -> LandscapeOpportunityVersion:
    """Add the opportunity's next version; the counter only moves forward (D37, D50)."""
    opportunity.version_counter = (opportunity.version_counter or 0) + 1
    opportunity.updated_at = func.now()
    version = LandscapeOpportunityVersion(
        id=uuid.uuid4(), opportunity_id=opportunity.id, version=opportunity.version_counter,
        epoch_id=epoch_id, cut_id=cut_id, member_idea_keys=list(members),
        edge_kinds=list(edge_kinds), **label,
    )
    db.add(version)
    return version


def _retire(opportunities: Iterable[LandscapeOpportunity]) -> None:
    for opportunity in opportunities:
        opportunity.retired_at = func.now()
        opportunity.updated_at = func.now()


# --- S8 -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Member:
    facts: IdeaFacts
    #: The idea version its S6 record judged.
    version: str
    #: When that S6 record last changed.
    stamp: datetime


@dataclass(frozen=True)
class _Group:
    members: tuple[_Member, ...]
    edge_kinds: tuple[str, ...]

    def keys(self) -> list[str]:
        return sorted(member.facts.idea_key for member in self.members)

    def grouping_key(self) -> str:
        """sha256 of the sorted (``idea_key``, idea version) pairs (spec §11.1)."""
        pairs = sorted((m.facts.idea_key, m.version) for m in self.members)
        return hashlib.sha256("\n".join(f"{k}\t{v}" for k, v in pairs).encode()).hexdigest()

    def label_values(self) -> dict:
        """A single idea is labelled by its structured line with no call (D29); a larger
        group waits for the label batch."""
        if len(self.members) == 1:
            return {"label": structured_line(self.members[0].facts.idea), "status": "done"}
        return {"label": None, "theme": None, "platform_signal": None, "status": "pending"}


async def _passed_deep(
    db: AsyncSession, epoch_id: uuid.UUID, versions: dict[uuid.UUID, str]
) -> dict[uuid.UUID, LandscapeDeep]:
    """Per idea, the S6 record it is judged by: its current version's when passed (done or
    rescued), else its newest passed one. Ideas with none are absent."""
    best: dict[uuid.UUID, LandscapeDeep] = {}
    for chunk in _chunks(list(versions)):
        records = (
            await db.scalars(
                select(LandscapeDeep).where(
                    LandscapeDeep.epoch_id == epoch_id,
                    LandscapeDeep.idea_id.in_(chunk),
                    LandscapeDeep.status.in_(_PASSED),
                )
            )
        ).all()
        for record in records:
            rank = (record.idea_version == versions.get(record.idea_id), record.created_at)
            held = best.get(record.idea_id)
            if held is None or rank > (held.idea_version == versions.get(held.idea_id),
                                       held.created_at):
                best[record.idea_id] = record
    return best


async def _members(db: AsyncSession, cut: LandscapeCut) -> list[_Member]:
    """The S6-passed ideas of the cut's top K."""
    top = [uuid.UUID(str(i)) for i in (cut.ordered_idea_ids or [])[: cut.k]]
    facts = await idea_facts(db, epoch_id=cut.epoch_id, idea_ids=top)
    deep = await _passed_deep(db, cut.epoch_id, {f.id: f.version for f in facts})
    return [
        _Member(f, deep[f.id].idea_version, max(deep[f.id].created_at, deep[f.id].updated_at))
        for f in facts if f.id in deep
    ]


def _groups(members: list[_Member]) -> list[_Group]:
    by_id = {member.facts.id: member for member in members}
    facts = [member.facts for member in members]
    return [
        _Group(tuple(by_id[f.id] for f in component), tuple(kinds))
        for component, kinds in _components(facts, opportunity_edges(facts))
    ]


def _changed(group: _Group, version: LandscapeOpportunityVersion, aliases: dict[str, str]) -> bool:
    if _resolved(group.keys(), aliases) != _resolved(version.member_idea_keys or [], aliases):
        return True
    return any(member.stamp > version.created_at for member in group.members)


async def _version_groups(
    db: AsyncSession, epoch_id: uuid.UUID, cut_id: uuid.UUID, groups: list[_Group]
) -> None:
    """The active epoch's S8: version matched opportunities that changed, create the
    unmatched groups, retire the unmatched opportunities (spec §7.9)."""
    match, current, aliases = await _match_current(db, [g.keys() for g in groups], {epoch_id})
    for index, group in enumerate(groups):
        opportunity_id = match.get(index)
        if opportunity_id is None:
            opportunity = await _new_opportunity(db)
        else:
            opportunity, version = current[opportunity_id]
            if not _changed(group, version, aliases):
                version.cut_id = cut_id
                continue
        _mint(
            db, opportunity, epoch_id=epoch_id, cut_id=cut_id, members=group.keys(),
            edge_kinds=group.edge_kinds, **group.label_values(),
        )
    matched = set(match.values())
    _retire(opportunity for oid, (opportunity, _) in current.items() if oid not in matched)


async def _write_groupings(
    db: AsyncSession, epoch_id: uuid.UUID, cut_id: uuid.UUID, groups: list[_Group]
) -> None:
    """A candidate epoch's S8 (F1): provisional groupings only. A grouping produced again
    keeps its label and takes the new ``cut_id``; one no longer produced loses ``current``."""
    G = LandscapeCandidateGrouping
    keys: list[str] = []
    for group in groups:
        key = group.grouping_key()
        keys.append(key)
        stmt = pg_insert(G).values(
            id=uuid.uuid4(), epoch_id=epoch_id, cut_id=cut_id, grouping_key=key,
            member_idea_keys=group.keys(), edge_kinds=list(group.edge_kinds), current=True,
            **group.label_values(),
        )
        await db.execute(stmt.on_conflict_do_update(
            constraint="uq_landscape_candidate_groupings_epoch_key",
            set_={
                "current": True, "cut_id": cut_id, "edge_kinds": stmt.excluded.edge_kinds,
                "updated_at": func.now(),
            },
        ))
    await db.execute(
        update(G)
        .where(G.epoch_id == epoch_id, G.current.is_(True), G.grouping_key.not_in(keys))
        .values(current=False, updated_at=func.now())
    )


async def build_opportunities(db_factory, *, epoch_id: uuid.UUID, cut_id: uuid.UUID) -> None:
    """S8 over the cut's S6-passed top-K ideas (spec §7.9); commits.

    Groups are the components over ``opportunity_edges``. In the active epoch, groups are
    matched to the live opportunities' current versions (``match_groups``): a matched group
    becomes a new version when its members or a member's S6 record changed, an unmatched
    group a new opportunity, and an unmatched opportunity is retired; probes are left alone.
    In a candidate or held epoch only ``landscape_candidate_groupings`` are written, and no
    stable row is touched (F1, D55). Every version and grouping written records ``cut_id``.
    A retired epoch is skipped. ``ValueError`` when the cut is missing, not complete or of
    another epoch.
    """
    async with db_factory() as db:
        epoch = await db.get(LandscapeEpoch, epoch_id)
        cut = await db.get(LandscapeCut, cut_id)
        if epoch is None or cut is None or cut.epoch_id != epoch_id or cut.status != "complete":
            raise ValueError(f"cut {cut_id} is not a complete cut of epoch {epoch_id}")
        if epoch.status not in ("active", *_GROUPING_EPOCHS):
            logger.info("S8 skipped: epoch %s is %s", epoch_id, epoch.status)
            return
        groups = _groups(await _members(db, cut))
        if epoch.status == "active":
            await _version_groups(db, epoch_id, cut_id, groups)
        else:
            await _write_groupings(db, epoch_id, cut_id, groups)
        await db.commit()
    logger.info("S8 epoch %s cut %s: %d group(s)", epoch_id, cut_id, len(groups))


# --- activation -----------------------------------------------------------------------------


def _carried_label(grouping: LandscapeCandidateGrouping) -> dict:
    """The grouping's label state for the version minted from it. A label still owed goes
    back to ``pending`` on the version, since its batch item writes to the grouping row."""
    if grouping.status in _IN_FLIGHT:
        return {"status": "pending", "label": None, "theme": None, "platform_signal": None}
    return {
        "status": grouping.status, "label": grouping.label, "theme": grouping.theme,
        "platform_signal": grouping.platform_signal,
        "served_by_model": grouping.served_by_model,
        "refusal_category": grouping.refusal_category, "error": grouping.error,
    }


async def activate_groupings(
    db: AsyncSession, old_epoch_id: uuid.UUID | None, new_epoch_id: uuid.UUID
) -> None:
    """The ``epochs.activate`` hook (spec §7.9, D59), run inside the activation transaction;
    never commits.

    Matches the new epoch's current groupings against the live opportunities' current
    versions (keys resolved through both epochs' aliases, D64), then mints one version per
    grouping, tagged with the new epoch (a matched opportunity keeps its identity, an
    unmatched grouping becomes a new opportunity), retires the unmatched opportunities and
    re-points each current grouping's assessment to the version minted for it. Assessments of
    groupings no longer current keep their grouping key, as history. Probes are untouched.
    """
    G = LandscapeCandidateGrouping
    groupings = (
        await db.scalars(
            select(G).where(G.epoch_id == new_epoch_id, G.current.is_(True))
            .order_by(G.grouping_key)
        )
    ).all()
    epoch_ids = {new_epoch_id} | ({old_epoch_id} if old_epoch_id is not None else set())
    match, current, _ = await _match_current(
        db, [list(g.member_idea_keys or []) for g in groupings], epoch_ids
    )
    for index, grouping in enumerate(groupings):
        opportunity_id = match.get(index)
        opportunity = (
            current[opportunity_id][0] if opportunity_id is not None
            else await _new_opportunity(db)
        )
        version = _mint(
            db, opportunity, epoch_id=new_epoch_id, cut_id=grouping.cut_id,
            members=list(grouping.member_idea_keys or []), edge_kinds=grouping.edge_kinds or [],
            **_carried_label(grouping),
        )
        await db.flush()
        await db.execute(
            update(LandscapeAssessment)
            .where(
                LandscapeAssessment.epoch_id == new_epoch_id,
                LandscapeAssessment.grouping_id == grouping.id,
            )
            .values(
                opportunity_id=opportunity.id, opportunity_version=version.version,
                grouping_id=None, updated_at=func.now(),
            )
        )
    matched = set(match.values())
    _retire(opportunity for oid, (opportunity, _) in current.items() if oid not in matched)
    await db.flush()


# --- probes ---------------------------------------------------------------------------------


async def make_probe(db_factory, *, epoch_id: uuid.UUID, idea_id: uuid.UUID) -> uuid.UUID:
    """A single-idea probe opportunity for a blind batch (spec §7.9, §10.6, D62); commits and
    returns its id. Its one version is flagged ``is_probe``, has no cut and is labelled by the
    idea's structured line. A probe never joins S8 groups and takes no part in matching or
    retirement. ``LookupError`` when the idea is not in the epoch."""
    async with db_factory() as db:
        idea = await db.scalar(
            select(LandscapeIdea)
            .where(LandscapeIdea.id == idea_id, LandscapeIdea.epoch_id == epoch_id)
        )
        if idea is None:
            raise LookupError(f"idea {idea_id} is not in epoch {epoch_id}")
        opportunity = await _new_opportunity(db)
        _mint(
            db, opportunity, epoch_id=epoch_id, cut_id=None, members=[idea.idea_key],
            edge_kinds=(), label=structured_line(idea), is_probe=True, status="done",
        )
        await db.commit()
        return opportunity.id


# --- texts the label and similarity calls read ------------------------------------------------


async def _idea_texts(
    db: AsyncSession, epoch_id: uuid.UUID, ideas: Sequence[LandscapeIdea]
) -> dict[uuid.UUID, str]:
    """Per idea: its structured line, then its S6 record (``comparator.deep_text``) when it
    has a passed one. No lab, author or institution is shown."""
    deep = await _passed_deep(db, epoch_id, {idea.id: _version(idea) for idea in ideas})
    texts = {}
    for idea in ideas:
        text = f"Structured line: {structured_line(idea)}"
        if idea.id in deep:
            text += f"\n\nRecord:\n{deep_text(deep[idea.id])}"
        texts[idea.id] = text
    return texts


async def _ideas_by_key(
    db: AsyncSession, epoch_id: uuid.UUID, keys: Sequence[str]
) -> list[LandscapeIdea]:
    """The epoch's ideas holding ``keys``, directly or through an alias (D36), in key order,
    each once."""
    Idea, A = LandscapeIdea, LandscapeIdeaAlias
    keys = list(keys)
    found = {
        idea.idea_key: idea
        for idea in (
            await db.scalars(
                select(Idea).where(Idea.epoch_id == epoch_id, Idea.idea_key.in_(keys))
            )
        ).all()
    }
    missing = [key for key in keys if key not in found]
    if missing:
        rows = (
            await db.execute(
                select(A.old_key, Idea)
                .join(Idea, Idea.id == A.idea_id)
                .where(A.epoch_id == epoch_id, A.old_key.in_(missing))
            )
        ).all()
        found.update({old_key: idea for old_key, idea in rows})
    ideas: dict[uuid.UUID, LandscapeIdea] = {}
    for key in keys:
        if key in found:
            ideas.setdefault(found[key].id, found[key])
    return list(ideas.values())


# --- similarity (synchronous) -------------------------------------------------------------------


async def _similarity_row(
    db: AsyncSession, a: uuid.UUID, va: str, b: uuid.UUID, vb: str, epoch_id: uuid.UUID
) -> LandscapeSimilarity | None:
    S = LandscapeSimilarity
    return await db.scalar(
        select(S).where(
            S.idea_a == a, S.version_a == va, S.idea_b == b, S.version_b == vb,
            S.epoch_id == epoch_id,
        )
    )


def _similarity_answer(result: CallResult) -> tuple[str, str | None]:
    if result.stop_reason == "refusal":
        return "refused", None
    if result.stop_reason == "max_tokens":
        return "truncated", None
    parsed = parse_output(result.text)
    same = parsed.get("same_program") if parsed is not None else None
    if not isinstance(same, bool):
        return "invalid", None
    verdict = SAME_PROGRAM if same else DIFFERENT_PROGRAM
    return verdict, _clip(parsed.get("reason"), REASON_MAX_CHARS)


def _similarity_result(row: LandscapeSimilarity, *, cached: bool) -> dict:
    same = {SAME_PROGRAM: True, DIFFERENT_PROGRAM: False}.get(row.verdict)
    return {"same_program": same, "verdict": row.verdict, "reason": row.reason, "cached": cached}


async def judge_similarity(db_factory, a, b, *, epoch_id) -> dict:
    """Whether ideas ``a`` and ``b`` (ids in ``epoch_id``) pursue the same program (spec §7.9,
    D14): one synchronous call (``call_sync``, stage ``similarity``, the client's refusal chain
    included), cached per (a, version a, b, version b, epoch), the pair stored lower id first.

    Returns ``{"same_program": bool | None, "verdict", "reason", "cached"}``. A refused,
    truncated or unparseable answer is stored with ``same_program`` None, so it is not asked
    again at the same versions; an API error is not stored (``verdict`` None, ``reason`` the
    error text) and the next call asks again. ``ValueError`` unless both are distinct ideas
    of the epoch. The caller paces these calls and yields to a live simulation run.
    """
    if a == b:
        raise ValueError("judge_similarity needs two distinct ideas")
    a, b = (a, b) if a < b else (b, a)
    async with db_factory() as db:
        ideas = {
            idea.id: idea
            for idea in (
                await db.scalars(
                    select(LandscapeIdea)
                    .where(LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.id.in_((a, b)))
                )
            ).all()
        }
        if len(ideas) != 2:
            raise ValueError(f"ideas {a} and {b} are not both in epoch {epoch_id}")
        va, vb = _version(ideas[a]), _version(ideas[b])
        cached = await _similarity_row(db, a, va, b, vb, epoch_id)
        if cached is not None:
            return _similarity_result(cached, cached=True)
        texts = await _idea_texts(db, epoch_id, [ideas[a], ideas[b]])
        inputs = await epochs.epoch_inputs(db, epoch_id)
        request = build_request(
            SIMILARITY_STAGE, model=inputs.model(SIMILARITY_STAGE),
            system=inputs.system(SIMILARITY_STAGE),
            user=f"# Idea A\n\n{texts[a]}\n\n# Idea B\n\n{texts[b]}",
            schema=SCHEMAS[SIMILARITY_STAGE],
        )
    result = await call_sync(
        SIMILARITY_STAGE, request, db_factory=db_factory,
        item_ref=f"{LandscapeSimilarity.__tablename__}:{a}:{b}",
    )
    if result.error is not None:
        return {"same_program": None, "verdict": None, "reason": result.error, "cached": False}
    verdict, reason = _similarity_answer(result)
    async with db_factory() as db:
        await db.execute(
            pg_insert(LandscapeSimilarity)
            .values(
                id=uuid.uuid4(), idea_a=a, version_a=va, idea_b=b, version_b=vb,
                epoch_id=epoch_id, verdict=verdict, reason=reason,
                served_by_model=result.served_by,
            )
            .on_conflict_do_nothing(constraint="uq_landscape_similarity_pair")
        )
        row = await _similarity_row(db, a, va, b, vb, epoch_id)
        await db.commit()
    return _similarity_result(row, cached=False)


# --- labels (batched) -----------------------------------------------------------------------


_LABEL_ROW_MODELS: dict[str, type] = {
    model.__tablename__: model
    for model in (LandscapeOpportunityVersion, LandscapeCandidateGrouping)
}


async def _label_message(db: AsyncSession, epoch_id: uuid.UUID, keys: Sequence[str]) -> str:
    ideas = await _ideas_by_key(db, epoch_id, keys)
    texts = await _idea_texts(db, epoch_id, ideas)
    parts = [f"This opportunity joins {len(ideas)} ideas."]
    parts.extend(f"# Idea {n}\n\n{texts[idea.id]}" for n, idea in enumerate(ideas, start=1))
    return "\n\n".join(parts)


def _label_request(user: str, inputs: EpochInputs, *, retry: bool) -> dict:
    return build_request(
        LABEL_STAGE, model=inputs.model(LABEL_STAGE), system=inputs.system(LABEL_STAGE),
        user=user, schema=SCHEMAS[LABEL_STAGE], retry=retry,
    )


async def _label_rows(db: AsyncSession, epoch: LandscapeEpoch) -> tuple[type, list]:
    """The epoch's label row model and its rows of two or more members owed a label: current
    versions of live opportunities in the active epoch, current groupings in a candidate."""
    if epoch.status == "active":
        model: type = LandscapeOpportunityVersion
        Opp = LandscapeOpportunity
        query = (
            select(model)
            .join(Opp, Opp.id == model.opportunity_id)
            .where(model.version == Opp.version_counter, Opp.retired_at.is_(None))
        )
    else:
        model = LandscapeCandidateGrouping
        query = select(model).where(model.current.is_(True))
    rows = (
        await db.scalars(
            query.where(
                model.epoch_id == epoch.id, model.status == "pending",
                model.fallback_from.is_(None),
            )
            .order_by(model.created_at, model.id)
        )
    ).all()
    return model, [row for row in rows if len(row.member_idea_keys or []) >= 2]


async def submit_label_batch(db_factory, *, epoch_id) -> int:
    """Batch the S8 label (stage ``label``) for the epoch's rows of two or more members that
    are ``pending`` and not waiting for the fallback chain: opportunity versions in the active
    epoch, current candidate groupings in a candidate or held one. Requests use the epoch's
    own prompt and model (§7.11); a row truncated once is sent with the higher limit. Returns
    the number submitted (0 for a retired epoch or when nothing is owed)."""
    async with db_factory() as db:
        epoch = await db.get(LandscapeEpoch, epoch_id)
        if epoch is None or epoch.status not in ("active", *_GROUPING_EPOCHS):
            return 0
        model, rows = await _label_rows(db, epoch)
        if not rows:
            return 0
        inputs = await epochs.epoch_inputs(db, epoch_id)
        items = []
        for row in rows:
            user = await _label_message(db, epoch_id, row.member_idea_keys)
            items.append((row.id, _label_request(user, inputs, retry=row.truncated_once)))
    return await submit_items(db_factory, stage=LABEL_STAGE, row_model=model, items=items)


def _label_values(parsed: dict | None, model: str) -> dict:
    """Row values for a label answer, clipped to the prompt's bounds; ``failed:invalid``
    without a usable label."""
    label = _clip(parsed.get("label"), LABEL_MAX_CHARS) if parsed is not None else None
    if label is None:
        return {
            "status": "failed:invalid", "served_by_model": model,
            "error": job_error_text("label output is not valid"),
        }
    signal = parsed.get("platform_signal")
    present = signal.get("present") if isinstance(signal, dict) else None
    return {
        "status": "done",
        "label": label,
        "theme": _clip(parsed.get("theme"), THEME_MAX_CHARS),
        "platform_signal": (
            {"present": present, "reason": _clip(signal.get("reason"), REASON_MAX_CHARS)}
            if isinstance(present, bool) else None
        ),
        "served_by_model": model,
        "error": None,
    }


async def ingest_label_outcomes(db_factory, outcomes: list[BatchOutcome]) -> None:
    """Apply label batch results, each a compare-and-set in its own transaction (spec §7.1
    step 5). The row's table is the batch's ``row_table``. ``succeeded`` stores the label,
    theme and platform signal; any other kind takes ``batches.transition`` (a refusal leaves
    the row for ``FALLBACKS``). A replayed chunk changes nothing."""
    for outcome in outcomes:
        async with db_factory() as db:
            batch = await db.get(LandscapeBatch, outcome.batch_row_id)
            model = _LABEL_ROW_MODELS.get(batch.row_table) if batch is not None else None
            row = await db.get(model, outcome.row_id) if model is not None else None
            if row is None:
                continue
            if outcome.kind == "succeeded":
                values = _label_values(
                    parse_output(message_text(outcome.message)), served_by(outcome)
                )
            else:
                values = batches.transition(
                    outcome.kind, attempts=row.attempts, truncated_once=row.truncated_once,
                    model=outcome.model,
                )
            await batches.apply_result(
                db, model, row_id=outcome.row_id, batch_item_id=outcome.batch_item_id,
                values=values,
            )
            await db.commit()


async def _fallback_request(db: AsyncSession, row: Any) -> dict:
    """The row's label request rebuilt from its members, in its epoch's inputs (D42)."""
    inputs = await epochs.epoch_inputs(db, row.epoch_id)
    user = await _label_message(db, row.epoch_id, row.member_idea_keys or [])
    return _label_request(user, inputs, retry=row.truncated_once)


async def _fallback_success(db: AsyncSession, row: Any, result: CallResult) -> None:
    for column, value in _label_values(parse_output(result.text), result.served_by).items():
        setattr(row, column, value)


FALLBACKS: tuple[FallbackAdapter, ...] = (
    FallbackAdapter(LABEL_STAGE, LandscapeOpportunityVersion, _fallback_request, _fallback_success),
    FallbackAdapter(LABEL_STAGE, LandscapeCandidateGrouping, _fallback_request, _fallback_success),
)
