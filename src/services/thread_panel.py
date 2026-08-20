"""Specialist-panel cards for the discussions pages.

The discussions pages list every interview thread, including the ones that ended
without an assessment — a timeout, a decline, a conversation that simply stopped.
Those are exactly the threads the assessment detail page cannot show, because
there is no assessment to open. Without this the panel's work on them was
invisible everywhere in the app.

Two bounds, both deliberate, both narrower than the assessment detail page's:

* **``specialist_consults`` ONLY.** No read-time parse of
  ``llm_call_logs.messages_json`` here. The table is forward-only (Ruling R5:
  no backfill), so an interview that predates it shows no panel on this page at
  all — the retroactive reconstruction lives on the assessment detail page
  (``src/services/assessment_detail.py``), which is admin-only precisely because
  everything derived from ``llm_call_logs`` is LLM drill-down.
* **No tool chips.** ``search_prior_art``/``retrieve_abstract`` calls and their
  results are drill-down too, and this page is read by managers (Ruling R4).
  Tool activity stays on the assessment detail page's timeline.

``raw_opinion`` is dropped for a non-admin render HERE, not merely left
unrendered by the template: a value that reaches the template context reaches
anyone who can read the page source the moment a later template edit prints it.
Same reasoning, and same split, as ``assessment_detail._load_consults``.

One grouped query per page render, keyed on the threads the page is actually
showing. Per-thread queries would be one round trip per row of an unpaginated
table, and a run-wide query would load (and, for an admin, ship) the verbatim
opinions of every thread the current filters excluded.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import SpecialistConsult

# Hard bounds. Everything below is unbounded production text: `question` and
# `raw_opinion` are `Text`, `concerns`/`questions_to_ask` are JSONB arrays
# written from a model's reply, and NONE of it is length-limited on the write
# path (src/agent/simulation.py::_record_consult). This page renders every
# consult of every thread it lists at once, with no pagination, so the clipping
# happens here rather than being left to the browser.
CONSULT_ROW_LIMIT = 500
QUESTION_CHARS = 400
ITEM_CHARS = 240
ITEMS_MAX = 6
RAW_OPINION_CHARS = 4000


def _clip(text: object, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _items(value: object) -> list[str]:
    """A JSONB array as at most ``ITEMS_MAX`` clipped strings.

    Non-list JSON (a bare string, a dict, null) yields nothing: the column is
    written from a parsed model reply, so a wrong shape is possible, and a
    per-character bullet list is worse than no bullet list.
    """
    if not isinstance(value, list):
        return []
    return [_clip(item, ITEM_CHARS) for item in value[:ITEMS_MAX] if item]


async def panel_cards_by_thread(
    db: AsyncSession,
    run_id: uuid.UUID | str | None,
    thread_ids: Iterable[str | None],
    *,
    admin_view: bool,
) -> dict[str, list[dict[str, Any]]]:
    """``{thread_id: [card, ...]}`` for the threads on one discussions page.

    ``run_id`` may be a UUID (one run), the string ``"all"`` (every run), or
    None (no run selected — nothing to show). ``thread_ids`` are the page's
    thread keys, which on the discussions pages is each root post's
    ``message_ts``: that is the same value ``specialist_consults.thread_id``
    carries (``src/agent/simulation.py`` passes the root ts), and getting that
    join wrong renders nothing at all rather than failing loudly.

    Cards are ordered by ``created_at`` within a thread, so a domain consulted
    twice reads in the order it was asked and both entries are kept — "asked
    twice" is itself a fact about the interview. Each card carries ``domain``
    and ``verdict_signal``, which is all the compact per-row panel indicator in
    ``templates/admin/_discussions_threads.html`` needs, so the indicator and
    the expanded cards are one query and can never disagree.

    ``raw_opinion`` is present only when ``admin_view`` (Ruling R4) — it is not
    even SELECTed otherwise.
    """
    if run_id is None:
        return {}
    keys = {key for key in thread_ids if key}
    if not keys:
        return {}

    columns = [
        SpecialistConsult.thread_id,
        SpecialistConsult.domain,
        SpecialistConsult.verdict_signal,
        SpecialistConsult.confidence,
        SpecialistConsult.question,
        SpecialistConsult.concerns,
        SpecialistConsult.questions_to_ask,
        SpecialistConsult.created_at,
    ]
    if admin_view:
        columns.append(SpecialistConsult.raw_opinion)

    query = select(*columns).where(SpecialistConsult.thread_id.in_(keys))
    if run_id != "all":
        query = query.where(SpecialistConsult.simulation_run_id == run_id)
    # Oldest first, then capped: a truncated read shows the beginning of the
    # panel's work rather than an arbitrary slice of it.
    rows = await db.execute(
        query.order_by(SpecialistConsult.created_at).limit(CONSULT_ROW_LIMIT)
    )

    cards: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw_opinion = row.raw_opinion if admin_view else None
        cards.setdefault(row.thread_id, []).append({
            "domain": row.domain,
            "verdict_signal": row.verdict_signal,
            "confidence": row.confidence,
            "question": _clip(row.question, QUESTION_CHARS),
            "concerns": _items(row.concerns),
            "questions_to_ask": _items(row.questions_to_ask),
            "created_at": row.created_at,
            "raw_opinion": _clip(raw_opinion, RAW_OPINION_CHARS) if raw_opinion else None,
            "raw_opinion_truncated": bool(
                raw_opinion and len(raw_opinion) > RAW_OPINION_CHARS
            ),
        })
    return cards
