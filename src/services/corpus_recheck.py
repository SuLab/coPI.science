"""Re-check of a PI's stored publications on every completed profile run (contract C4 of the
2026-10-15 publication-handling design, as amended by I2; owner decisions Q7, Q11, F12, F26,
F27, F28; I8, I12).

``corpus_additions.apply_corpus_result`` stores and refreshes the records a resolve returned.
This module judges every other in-use stored row, using only records fetched in this run:

- a row with a PMID whose record now carries an ``EXCLUDED_TYPES`` type, mixed types
  included (F12), is excluded with reason ``publication_type``;
- a row with a PMID whose record this run fails the identity gate is relabelled
  ``unanchored`` (Q7), on every completed resolve; no candidate is made, the row exists;
- a row with a PMID that no anchor stage returned is relabelled ``unanchored`` only after
  complete lookups (``corpus_additions.incomplete_lookups_reason``); both relabels apply to
  stage-list and NULL provenance only (F26);
- a PMID-less row whose OpenAlex record in this run is retracted is excluded with reason
  ``publication_type`` (F28);
- an ``openalex`` row whose authorship in this run's OpenAlex data is ``"unattributed"``
  (``non_pubmed_works.attribution_verdict``; Q11, F11, I8) is excluded with reason
  ``openalex_affiliation``. ``"unknown"`` (no authorship for its DOI) leaves it alone, and
  the check is skipped for the run when the authorship list or the ORCID profile could not
  be read (F27, I12).

``manual`` rows and excluded rows are never touched; nothing is deleted or restored. An
``unanchored`` row the resolve returned anchored is re-anchored by ``apply_corpus_result``
(``CorpusStoreOutcome.reanchored``)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.publication import (
    EXCLUDED_REASON_OPENALEX_AFFILIATION,
    EXCLUDED_REASON_PUBLICATION_TYPE,
    PROVENANCE_MANUAL,
    PROVENANCE_OPENALEX,
    PROVENANCE_UNANCHORED,
    Publication,
)
from src.services.corpus_additions import (
    ANCHOR_STAGES,
    exclude_by_system,
    incomplete_lookups_reason,
    superseded_pairs,
)
from src.services.non_pubmed_works import attribution_verdict
from src.services.pubmed import _doi_fold

if TYPE_CHECKING:
    from src.services.corpus import CorpusResult
    from src.services.corpus_additions import CorpusStoreOutcome

#: A stored stage-list provenance: the pattern of the ``ck_publications_provenance`` CHECK.
_STAGE_LIST = re.compile(r"^(s[1-4],)*s[13](,s[1-4])*$")
#: How many identifiers the progress line lists.
_SAMPLE = 10


@dataclass
class RecheckOutcome:
    """What one re-check did. The lists hold PMIDs, except the two of PMID-less rows, which
    hold the stored DOIs."""

    excluded_type: list[str] = field(default_factory=list)
    excluded_retracted: list[str] = field(default_factory=list)
    unanchored_gate: list[str] = field(default_factory=list)
    unanchored_absent: list[str] = field(default_factory=list)
    excluded_openalex: list[str] = field(default_factory=list)
    #: incomplete_lookups_reason when the absence relabel was not run, else None
    absence_skipped: str | None = None
    #: why the OpenAlex attribution check was skipped, else None
    openalex_skipped: str | None = None
    #: in-use "openalex" rows that reached the attribution check
    openalex_rows: int = 0


async def recheck_stored_publications(
    db: AsyncSession, user_id: uuid.UUID, result: CorpusResult, *,
    authorships: Mapping[str, Mapping[str, Any] | None] | None,
    organizations: Sequence[str],
    retracted_dois: Collection[str] = frozenset(),
    openalex_skip_reason: str | None = None,
) -> RecheckOutcome:
    """Apply contract C4 to the PI's in-use, non-``manual`` rows (see the module docstring).

    ``authorships`` is the folded-DOI map ``non_pubmed_works.orcid_authorships_by_doi`` fetched
    once in this run, or None when it was not fetched (``openalex_skip_reason`` then says why,
    or is None when the PI has no ``openalex`` row). ``organizations`` are the ORCID profile's.
    ``retracted_dois`` are folded DOIs the thin-path singleton lookups found retracted. Rows the
    resolve returned, title duplicates and earlier versions of ``superseded_pairs`` were
    already handled by ``apply_corpus_result`` and are skipped. Runs in the caller's
    transaction under the ``lock_corpus`` the caller holds; flushes, never commits."""
    outcome = RecheckOutcome()
    outcome.absence_skipped = incomplete_lookups_reason(result)
    handled = {str(r["pmid"]) for r in result.ranked if r.get("pmid")}
    handled |= {dup for dup, _keeper in result.title_duplicates}
    handled |= {old for old, _new in superseded_pairs(result)}
    type_excluded = {str(e["pmid"]) for e in result.type_excluded if e.get("pmid")}
    gate_failed = {str(e["pmid"]) for e in result.flagged if e.get("pmid")}
    anchored = {
        pmid for pmid, stages in (result.stage_map or {}).items() if set(stages) & ANCHOR_STAGES
    }
    retracted = {
        _doi_fold(w["doi"]) for w in result.openalex_works
        if w.get("is_retracted") and w.get("doi")
    } | set(retracted_dois)
    rows = (await db.execute(
        select(Publication)
        .where(Publication.user_id == user_id, Publication.excluded_at.is_(None))
        .order_by(Publication.created_at, Publication.id)
    )).scalars().all()
    for row in rows:
        if row.provenance == PROVENANCE_MANUAL:
            continue
        if row.pmid is not None:
            if row.pmid not in handled:
                _recheck_pubmed_row(row, type_excluded, gate_failed, anchored, outcome)
        else:
            _recheck_pmid_less_row(
                row, retracted, authorships, organizations, openalex_skip_reason, outcome
            )
    await db.flush()
    return outcome


def _recheck_pubmed_row(
    row: Publication, type_excluded: set[str], gate_failed: set[str], anchored: set[str],
    outcome: RecheckOutcome,
) -> None:
    """C4 for a row with a PMID the resolve did not hand back; first match wins."""
    if row.pmid in type_excluded:
        if exclude_by_system(row, EXCLUDED_REASON_PUBLICATION_TYPE):
            outcome.excluded_type.append(row.pmid)
        return
    if row.provenance is not None and not _STAGE_LIST.match(row.provenance):
        return  # "unanchored" or "openalex": nothing more to relabel (F26)
    if row.pmid in gate_failed:
        # I13: after Phase 3 a record an anchor stage returned can only be flagged
        # no_individual_author_match; every other flag means it lost its anchor this run, so
        # an incomplete lookup must not turn that into an identity failure.
        if row.pmid in anchored or outcome.absence_skipped is None:
            row.provenance = PROVENANCE_UNANCHORED
            outcome.unanchored_gate.append(row.pmid)
    elif row.pmid not in anchored and outcome.absence_skipped is None:
        # Consortium-only records are identity-correct, so they fall here, not to the gate.
        row.provenance = PROVENANCE_UNANCHORED
        outcome.unanchored_absent.append(row.pmid)


def _recheck_pmid_less_row(
    row: Publication, retracted: set[str],
    authorships: Mapping[str, Mapping[str, Any] | None] | None,
    organizations: Sequence[str], openalex_skip_reason: str | None, outcome: RecheckOutcome,
) -> None:
    """C4 for a PMID-less row: retraction (F28), then OpenAlex attribution (Q11, F27, I12)."""
    fold = _doi_fold(row.doi) if row.doi else None
    if fold is not None and fold in retracted:
        if exclude_by_system(row, EXCLUDED_REASON_PUBLICATION_TYPE):
            outcome.excluded_retracted.append(row.doi)
        return
    if row.provenance != PROVENANCE_OPENALEX:
        return
    outcome.openalex_rows += 1
    if authorships is None:
        outcome.openalex_skipped = openalex_skip_reason
        return
    if not organizations:
        # F36: an ORCID profile with no organisations is "organisations unavailable" (I12).
        outcome.openalex_skipped = "the ORCID profile lists no organisations"
        return
    authorship = authorships.get(fold) if fold is not None else None
    if attribution_verdict(authorship, organizations) == "unattributed" and exclude_by_system(
        row, EXCLUDED_REASON_OPENALEX_AFFILIATION
    ):
        outcome.excluded_openalex.append(row.doi)


def recheck_progress_detail(store: CorpusStoreOutcome, recheck: RecheckOutcome) -> str | None:
    """The ``corpus_rechecked`` progress text, or None when there is nothing to say: every count
    is 0 and nothing was skipped (an OpenAlex skip with no ``openalex`` row says nothing)."""
    counts = (
        len(recheck.excluded_type), len(recheck.excluded_retracted),
        len(recheck.unanchored_gate), len(recheck.unanchored_absent),
        len(store.reanchored), len(recheck.excluded_openalex),
    )
    openalex_skip = recheck.openalex_skipped is not None and recheck.openalex_rows > 0
    if not any(counts) and recheck.absence_skipped is None and not openalex_skip:
        return None
    detail = (
        f"Stored publications re-checked: {counts[0]} excluded (PubMed lists a publication "
        f"type Blackbird leaves out), {counts[1]} excluded (OpenAlex marks the work "
        f"retracted), {counts[2]} unanchored (the PubMed author list does not name the PI), "
        f"{counts[3]} unanchored (no ORCID anchor in this run), {counts[4]} anchored again, "
        f"{counts[5]} excluded (OpenAlex's authorship no longer ties the work to the PI)."
    )
    if recheck.absence_skipped is not None:
        detail += f" Absence not checked: {recheck.absence_skipped}."
    if openalex_skip:
        detail += f" OpenAlex attribution not checked: {recheck.openalex_skipped}."
    ids = [
        *recheck.excluded_type, *recheck.excluded_retracted, *recheck.unanchored_gate,
        *recheck.unanchored_absent, *store.reanchored, *recheck.excluded_openalex,
    ]
    if ids:
        detail += " " + ", ".join(ids[:_SAMPLE])
    return detail
