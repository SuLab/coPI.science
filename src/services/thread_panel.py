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
from typing import Any, NamedTuple

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


class PanelRead(NamedTuple):
    """One page's panel read: the cards, and whether the row cap bit.

    ``truncated`` exists because this cap is the one bound here whose effect a
    reader CANNOT see in what is rendered. A clipped opinion says "…"; a missing
    card says nothing at all, and the per-row indicator now feeds from this same
    query — so a silent cap would turn "no consults recorded for this thread"
    and "we stopped reading" into the same page. The callers pass it to the
    template, which says so.
    """

    by_thread: dict[str, list[dict[str, Any]]]
    truncated: bool
    limit: int


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
) -> PanelRead:
    """The panel cards for the threads on one discussions page, keyed by thread.

    ``run_id`` may be a UUID (one run), the string ``"all"`` (every run), or
    None (no run selected — nothing to show). ``thread_ids`` are the page's
    thread keys, which on the discussions pages is each root post's
    ``message_ts``: that is the same value ``specialist_consults.thread_id``
    carries (``src/agent/simulation.py`` passes the root ts), and getting that
    join wrong renders nothing at all rather than failing loudly.

    **The cap keeps the NEWEST consults.** The query orders ``created_at``
    DESC and the per-thread lists are reversed afterwards, so display order is
    still oldest-first — a domain consulted twice reads in the order it was
    asked, and both entries are kept, because "asked twice" is itself a fact
    about the interview. Reading ASC and capping would have dropped the newest
    consults on an overflowing page, which is precisely backwards: the
    discussions table is sorted with the newest threads at the bottom but it is
    the LIVE interviews whose panel a reader is looking for, and those rows
    would have lost both their cards and — since the compact per-row indicator
    now feeds from this same query — their indicator, reading as "no specialist
    was ever consulted here". Overflow is also reported rather than swallowed;
    see ``PanelRead.truncated``.

    Each card carries ``domain``, ``verdict_signal`` and ``reply_truncated``,
    which is all the indicator needs, so the indicator and the expanded cards
    are one query and can never disagree — including about whether a signal is
    an opinion or the parser's default on a reply that stopped early. It also
    carries ``read_state`` and ``concern_count``, which the indicator does not
    use: both exist because a signal alone overstates itself. ``adequate``
    (2026-08-28) means "meets the bar for THIS STAGE", not "no concerns", and
    the count is what stops a reader taking it for the latter.

    ``raw_opinion`` is present only when ``admin_view`` (Ruling R4) — it is not
    even SELECTed otherwise.
    """
    if run_id is None:
        return PanelRead({}, False, CONSULT_ROW_LIMIT)
    keys = {key for key in thread_ids if key}
    if not keys:
        return PanelRead({}, False, CONSULT_ROW_LIMIT)

    columns = [
        SpecialistConsult.thread_id,
        SpecialistConsult.domain,
        SpecialistConsult.verdict_signal,
        SpecialistConsult.confidence,
        SpecialistConsult.question,
        SpecialistConsult.concerns,
        SpecialistConsult.questions_to_ask,
        SpecialistConsult.created_at,
        # 0036's "this reply was cut off". SELECTed for every reader, admin or
        # manager: `verdict_signal` is a PARSE DEFAULT (`gap`,
        # src/agent/specialists.py) when the reply never finished, so without
        # this column both surfaces below present a sentence that stopped
        # mid-word as an opinion a specialist gave. Not drill-down — it
        # qualifies the signal itself, and a manager gets no other way to tell.
        SpecialistConsult.truncated,
        # 0038's read-state. Generalises `truncated`: a reply that arrived
        # COMPLETE and failed to parse is not truncated and is just as unread,
        # and `read_state` is the one field that says which of the two (or
        # neither) happened. NULL on every pre-0038 row — "never recorded",
        # not "parsed" — so the template must not read a missing value as a
        # clean read.
        SpecialistConsult.read_state,
    ]
    if admin_view:
        columns.append(SpecialistConsult.raw_opinion)

    query = select(*columns).where(SpecialistConsult.thread_id.in_(keys))
    if run_id != "all":
        query = query.where(SpecialistConsult.simulation_run_id == run_id)
    # NEWEST first, so the cap sheds the oldest consults (see the docstring).
    # `id` breaks ties: `created_at` is not unique — consults written inside one
    # transaction share Postgres' transaction-scoped now() — and without a
    # unique final term the rows either side of the cap boundary are chosen
    # arbitrarily, so the same page could gain and lose a card between renders.
    #
    # LIMIT is `+ 1`: fetching one row past the cap is how overflow is DETECTED.
    # `len(rows) == limit` would also fire on an exact fit and cry wolf.
    limit = CONSULT_ROW_LIMIT
    rows = list(
        await db.execute(
            query.order_by(
                SpecialistConsult.created_at.desc(), SpecialistConsult.id.desc()
            ).limit(limit + 1)
        )
    )
    truncated = len(rows) > limit
    rows = rows[:limit]

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
            # THE THIRD "truncated" on this page, and the only one about the
            # consult rather than about the rendering: `PanelRead.truncated` is
            # the row cap biting, `raw_opinion_truncated` is this page clipping
            # a long reply for display, and this one is the SPECIALIST's reply
            # having stopped before it finished. Spelled `reply_truncated` so
            # the three are never confused in a template, and matching the key
            # `assessment_detail._load_consults` projects so the two card
            # renderers read alike.
            #
            # `bool(...)`: NULL means "written before 0036" and the column's
            # comment says to read it as not truncated.
            "reply_truncated": bool(row.truncated),
            # 0038's `read_state`, carried verbatim INCLUDING None — None means
            # "written before 0038", which is a third state and not "parsed".
            # Deciding that here would put the guess in the service; the
            # template shows it only when it has one.
            "read_state": row.read_state,
            # How many concerns this specialist filed, alongside its signal.
            # The point of the 2026-08-28 rename: `adequate` means "meets the
            # bar for THIS STAGE", NOT "no concerns" — every `clear` opinion
            # ever emitted carried 4-9 of them, and the label's predecessor read
            # as a clean bill of health because nothing beside it said
            # otherwise. Staff-only, like the lists it counts: `format_panel_note`
            # still cannot carry it to Slack (spec D7).
            "concern_count": len(_items(row.concerns)),
        })
    # Back to chronological within each thread. The rows arrived newest-first
    # (that is what makes the cap shed the OLDEST consults), and each thread's
    # sublist inherits that order, so one reverse per thread restores the
    # asked-in-this-order reading the cards are meant to have.
    for thread_cards in cards.values():
        thread_cards.reverse()
    return PanelRead(cards, truncated, limit)
