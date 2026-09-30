"""Why a simulation run ended, and what that decides (spec P0-04; B15, B19, B24).

- TODAY: the operator's default Stop (``operator``) and SIGTERM/SIGINT
  (``signal``, including ``docker stop``) — the normal end of a production run
  (C28). The shutdown sweep announces every owed headline, open interviews
  included, exactly as before this vocabulary existed.
- FINALIZE: a natural end (``time_limit``, ``target_drained``) — unreachable with
  the production defaults. Same sweep, then ``simulation_runs.finalized_at`` is
  set and a resume is refused.
- HOLD: "Stop — hold open interviews" (``operator_hold``), a terminal stall
  (``stall``), an exception escaping the loop (``exception``), a ``start()``
  failure (``start_failed``) and, from Phase 2, a lost engine lock
  (``lock_lost``). Only ended interviews are announced; the rest wait for a
  resume or a finalize, and ``simulation_runs.held_at`` is set.

A later reason can only RAISE the class (HOLD > FINALIZE > TODAY); within a
class the first reason recorded stays.
"""

TODAY = "TODAY"
FINALIZE = "FINALIZE"
HOLD = "HOLD"

END_REASON_CLASS: dict[str, str] = {
    "operator": TODAY,
    "signal": TODAY,
    "time_limit": FINALIZE,
    "target_drained": FINALIZE,
    "operator_hold": HOLD,
    "stall": HOLD,
    "exception": HOLD,
    "start_failed": HOLD,
    "lock_lost": HOLD,
}

_RANK = {TODAY: 0, FINALIZE: 1, HOLD: 2}


def end_reason_class(reason: str) -> str:
    """The class of ``reason``; ``ValueError`` for anything outside the vocabulary."""
    try:
        return END_REASON_CLASS[reason]
    except KeyError:
        raise ValueError(
            f"unknown end reason {reason!r}; expected one of {sorted(END_REASON_CLASS)}"
        ) from None


def stronger_reason(current: str | None, new: str) -> str:
    """The reason to keep when ``new`` arrives after ``current``: a strictly
    higher class replaces it; the same or a lower class keeps ``current``."""
    new_rank = _RANK[end_reason_class(new)]
    if current is None:
        return new
    return new if new_rank > _RANK[end_reason_class(current)] else current
