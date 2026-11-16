"""S1, the deterministic filter at ingest (spec 2026-10-30 §7.2): §5.2 qualification, the
§5.3 window, and the §5.3 type, retraction and language rules.

S1 is not a judgment, so its result lives on the shared document row
(``landscape_documents.s1_status``), never on an epoch-scoped row (§7.1).
"""
from __future__ import annotations

from datetime import date, timedelta

from src.landscaping.sources import DocumentRecord

#: Document types that are never idea sources nor evidence (D4). PubMed publication types
#: arrive mapped into OpenAlex's vocabulary; OpenAlex's own list query admits only article,
#: preprint and review, so the rest guard the PubMed path and any type OpenAlex adds.
EXCLUDED_TYPES = frozenset({
    "erratum", "retraction", "editorial", "letter", "comment", "conference",
    "book-chapter", "book", "paratext", "peer-review",
})
#: Reviews are evidence only, never idea sources (D4).
_REVIEW_TYPES = frozenset({"review"})
#: Kinds whose window is the project period or fiscal year, not one publication date (§5.3).
_PERIOD_KINDS = frozenset({"grant"})


def _fiscal_year_period(fy: int) -> tuple[date, date]:
    """The US federal fiscal year ``fy``: 1 October of ``fy - 1`` to 30 September of ``fy``."""
    return date(fy - 1, 10, 1), date(fy, 9, 30)


def _iso(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _grant_in_window(rec: DocumentRecord, start: date, end: date) -> bool:
    """§5.3: any overlap of the project period with the window, or a fiscal year in it."""
    p_start = _iso(rec.extra.get("project_start")) or rec.published
    p_end = _iso(rec.extra.get("project_end")) or p_start
    if p_start is not None and p_end is not None and p_start <= end and p_end >= start:
        return True
    fy = rec.extra.get("fiscal_year")
    if isinstance(fy, int) or (isinstance(fy, str) and fy.isdigit()):
        fy_start, fy_end = _fiscal_year_period(int(fy))
        return fy_start <= end and fy_end >= start
    return False


def _in_window(rec: DocumentRecord, *, today: date, window_days: int) -> bool:
    """Whether a record counts toward the rolling window ending `today` (§5.3).

    Works, patents, JHTV listings and datasets go by publication date; a date after
    `today` (an issue dated ahead of release) still counts. A record with no date is
    outside the window, since nothing places it inside."""
    start = today - timedelta(days=window_days)
    if rec.kind in _PERIOD_KINDS:
        return _grant_in_window(rec, start, today)
    return rec.published is not None and rec.published >= start


def s1_status(rec: DocumentRecord, *, today: date, window_days: int) -> str:
    """``"eligible"`` or ``"excluded:<reason>"`` for one record (spec §7.2).

    Reasons, checked in this order: ``dataset_evidence_only`` (D3), ``apl`` (a patent whose
    inventors are all in Laurel or North Laurel, §6.1), ``not_qualified`` (no §5.2
    `role_basis`), ``retracted``, ``type_review_evidence_only``, ``type_<doc_type>`` (D4),
    ``language`` (a stated language other than English; an unstated one passes) and
    ``window``."""
    if rec.kind == "dataset":
        return "excluded:dataset_evidence_only"
    if rec.raw_qualifier.get("apl") is True:
        return "excluded:apl"
    if not rec.role_basis:
        return "excluded:not_qualified"
    if rec.retracted:
        return "excluded:retracted"
    doc_type = (rec.doc_type or "").strip().lower()
    if doc_type in _REVIEW_TYPES:
        return "excluded:type_review_evidence_only"
    if doc_type in EXCLUDED_TYPES:
        return f"excluded:type_{doc_type}"
    if rec.language and rec.language.strip().lower() not in ("en", "eng"):
        return "excluded:language"
    if not _in_window(rec, today=today, window_days=window_days):
        return "excluded:window"
    return "eligible"
