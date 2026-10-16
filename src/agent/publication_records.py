"""A lab's own papers: the ground truth behind own-paper recognition (C12 of the
2026-10-15 publication-handling design; main SPEC AGT-4 to AGT-6, adapted).

A lab's own papers are its stored in-use publications plus the identifiers in its exported
persona, minus any persona identifier held only by rows excluded as not the PI's (F9:
excluded with reason ``staff``, ``openalex_affiliation`` or ``repair_script``). Rows excluded
for another reason (``superseded``, ``pubmed_twin``, ``duplicate_title``,
``publication_type``) neither count nor subtract, and an identifier one of them holds stays
the lab's own even when a not-theirs row holds it too. Rows excluded before migration 0068
(reason NULL, F34) are ignored. Tenure scope does not apply, and ``unanchored`` rows are in use (D17,
D45). The persona half is spec 2026-10-05 D49 unchanged: every DOI anywhere in the persona,
plus the PMIDs of its Recent Publications section.

The engine loads the rows (``load_publication_ids``) at the roster-sync cadence; everything
else here is pure. Every identifier is in C2 canonical form (``src.agent.dois``). This
module never imports ``src.agent.agent``.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.dois import Citation, canonical_doi, canonical_pmid, extract_dois
from src.agent.persona_sections import recent_publication_pmids
from src.models import AgentRegistry, Publication
from src.services.tenure_scope import publication_in_use

#: The C3 exclusion reasons that mean "not the PI's paper" (F9).
NOT_THEIRS_REASONS: frozenset[str] = frozenset({"staff", "openalex_affiliation", "repair_script"})


@dataclass(frozen=True)
class PublicationIds:
    """One agent's loaded identifiers, all canonical. An identifier any in-use row holds is
    never in a ``not_theirs`` set. ``PublicationIds()`` is the empty value."""

    in_use_dois: frozenset[str] = frozenset()
    in_use_pmids: frozenset[str] = frozenset()
    not_theirs_dois: frozenset[str] = frozenset()
    not_theirs_pmids: frozenset[str] = frozenset()
    #: The number of in-use rows.
    row_count: int = 0


@dataclass(frozen=True)
class LabPublicationRecord:
    """The lab's own papers by kind (main GATE-1's field order)."""

    dois: frozenset[str] = frozenset()
    has_records: bool = False
    pmids: frozenset[str] = frozenset()

    def holds(self, citation: Citation) -> bool:
        """Whether ``citation`` (C2) names one of the lab's own papers."""
        return citation.value in (self.dois if citation.kind == "doi" else self.pmids)

    @property
    def ids(self) -> frozenset[str]:
        """Both kinds together; a DOI starts with ``10.``, so the two never collide."""
        return self.dois | self.pmids


def profile_identifiers(persona: str | None) -> tuple[frozenset[str], frozenset[str]]:
    """``(dois, pmids)`` of an exported persona: every DOI in it, and the PMIDs of its
    Recent Publications section (D49). ``None`` gives two empty sets."""
    if persona is None:
        return frozenset(), frozenset()
    return frozenset(extract_dois(persona)), frozenset(recent_publication_pmids(persona))


def build_record(persona: str | None, ids: PublicationIds | None) -> LabPublicationRecord:
    """The lab's own papers: per kind, (persona identifiers − not-theirs) ∪ in-use rows.
    ``ids=None`` means not loaded yet, and the record is the persona alone."""
    p_dois, p_pmids = profile_identifiers(persona)
    if ids is None:
        return LabPublicationRecord(dois=p_dois, has_records=bool(p_dois or p_pmids), pmids=p_pmids)
    kept_dois = p_dois - ids.not_theirs_dois          # F9
    kept_pmids = p_pmids - ids.not_theirs_pmids
    return LabPublicationRecord(
        dois=kept_dois | ids.in_use_dois,
        has_records=ids.row_count > 0 or bool(kept_dois or kept_pmids),
        pmids=kept_pmids | ids.in_use_pmids,
    )


def records_from_rows(
    agent_ids: Iterable[str],
    rows: Iterable[tuple[str, str | None, str | None, datetime | None, str | None]],
) -> dict[str, PublicationIds]:
    """``PublicationIds`` per agent id from ``(agent_id, doi, pmid, excluded_at,
    excluded_reason)`` rows. An in-use row adds its canonical identifiers to the in-use sets
    and counts; an excluded row with a ``NOT_THEIRS_REASONS`` reason adds them to the
    not-theirs sets unless an in-use row or a row excluded for another reason holds them too;
    a row excluded with reason NULL is ignored. Every id in ``agent_ids`` gets an entry; a
    row for any other agent is ignored. Pure."""
    found: dict[str, dict[str, set[str]]] = {
        aid: {kind: set() for kind in (
            "in_dois", "in_pmids", "nt_dois", "nt_pmids", "kept_dois", "kept_pmids",
        )}
        for aid in agent_ids
    }
    counts = dict.fromkeys(found, 0)
    for agent_id, doi, pmid, excluded_at, reason in rows:
        sets = found.get(agent_id)
        if sets is None:
            continue
        if excluded_at is None:
            kind = "in"
            counts[agent_id] += 1
        elif reason in NOT_THEIRS_REASONS:
            kind = "nt"
        elif reason is not None:
            kind = "kept"
        else:
            continue
        doi_value = canonical_doi(doi) if doi else None
        pmid_value = canonical_pmid(pmid) if pmid else None
        if doi_value is not None:
            sets[f"{kind}_dois"].add(doi_value)
        if pmid_value is not None:
            sets[f"{kind}_pmids"].add(pmid_value)
    return {
        aid: PublicationIds(
            in_use_dois=frozenset(sets["in_dois"]),
            in_use_pmids=frozenset(sets["in_pmids"]),
            not_theirs_dois=frozenset(sets["nt_dois"] - sets["in_dois"] - sets["kept_dois"]),
            not_theirs_pmids=frozenset(sets["nt_pmids"] - sets["in_pmids"] - sets["kept_pmids"]),
            row_count=counts[aid],
        )
        for aid, sets in found.items()
    }


async def load_publication_ids(
    db: AsyncSession, agent_ids: Collection[str],
) -> dict[str, PublicationIds]:
    """``records_from_rows`` over one statement: every in-use row, and every row excluded
    with a reason (C3), of the users who own ``agent_ids``. ``{}`` without a
    query when ``agent_ids`` is empty. Reads only, does not commit, and lets errors
    propagate."""
    if not agent_ids:
        return {}
    rows = (await db.execute(
        select(
            AgentRegistry.agent_id, Publication.doi, Publication.pmid,
            Publication.excluded_at, Publication.excluded_reason,
        )
        .join(Publication, Publication.user_id == AgentRegistry.user_id)
        .where(
            AgentRegistry.agent_id.in_(list(agent_ids)),
            or_(publication_in_use(), Publication.excluded_reason.is_not(None)),
        )
    )).tuples().all()
    return records_from_rows(agent_ids, rows)
