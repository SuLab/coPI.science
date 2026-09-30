"""Pure engine helpers: the verdict record, the in-doubt headline sentinel and the
small functions the units share."""

from typing import NamedTuple

from src.agent.thread_guidance import CONCLUDE
from src.models import AgentMessage
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC
from src.services.llm import is_truncated_stop


def _thread_phase_label(thread_phase: str) -> str:
    """Map a ``thread_guidance`` phase constant to the ``llm_call_logs``
    enum value (``explore`` / ``decide`` / ``conclude``).

    ``EXPLORE``/``DECIDE`` lowercase directly; ``CONCLUDE``'s actual value is
    ``"MUST CONCLUDE"`` (see thread_guidance.py), so it is special-cased
    rather than blindly lowered.
    """
    return "conclude" if thread_phase == CONCLUDE else thread_phase.lower()


def _was_truncated(stop_reasons: list[str]) -> bool:
    """Did the reply this turn is holding stop BEFORE the model finished it?

    ``src/services/llm.py`` reports the terminating ``stop_reason`` through an
    ``on_stop_reason`` callback and still RETURNS the partial text, because
    whether a partial answer may be posted, persisted or credited differs per
    call site. Every engine call site therefore collects the reason into a list
    (``on_stop_reason=stop_reasons.append``, the idiom ``src/agent/tools.py``
    already uses) and asks this.

    ``is_truncated_stop`` rather than a ``refusal``-only test, and deliberately
    not re-derived here: ``refusal`` is the classifier cutting the generation and
    ``max_tokens`` is the ceiling doing it, the text in hand is equally partial,
    and a reply that truncated, retried and truncated again reports
    ``max_tokens`` — as does a fallthrough from the retry path whose first pass
    was refused. Until 2026-08-22 ``on_stop_reason`` had NO reader in this module
    at all, so a truncated hub reply was posted to Slack as complete and a
    truncated synthesis overwrote a good working memory.

    ``any`` rather than "the last one": the contract says the callback fires
    exactly once, and a guard about incomplete text should not become a no-op if
    that ever changes.
    """
    return any(is_truncated_stop(reason) for reason in stop_reasons)


class _HeadlineInDoubt:
    """What `_post_assessment_summary` returns when the post raised with no Slack
    response (spec P0-08): the headline may or may not be in the channel. Falsy,
    so every `if not posted:` still reads it as "not known to be posted"; the
    claim-aware caller tests `is HEADLINE_IN_DOUBT` and keeps its claim."""

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "HEADLINE_IN_DOUBT"


HEADLINE_IN_DOUBT = _HeadlineInDoubt()


def _visibility_permits(origin: str, current: str) -> bool:
    """True iff an origin-visibility record may appear in a current-visibility context.

    Implements the ordering `public < collab_private` from G3:
    - public origins are visible in any context.
    - collab_private origins are visible only in a collab_private context.

    See specs/privacy-and-channel-visibility.md §G3.
    """
    if origin == VISIBILITY_PUBLIC:
        return True
    return current == VISIBILITY_COLLAB_PRIVATE


def _restored_slack_ts(row: AgentMessage) -> str | None:
    """Slack ts for a restored ``agent_messages`` row, or None if it has none.

    Restoring this mapping is what lets ``_slack_parent_ts`` tell a Slack-backed
    thread from a DB-origin one after a restart. The column is the only evidence:
    a NULL means the message is not on Slack.

    This used to *infer* a missing mapping — "a row stored against a real Slack
    ``channel_id`` was born on Slack, so its canonical id is its Slack ts" — to
    cover pre-Stage-6 rows written before the mapping was recorded. That
    inference is unsound, because a DB-origin message can also carry a real Slack
    channel id: a PI message written through the web inbox resolves ``channel_id``
    from the ``agent_channels`` row (Slack's id when Slack is on), and so does an
    agent post whose Slack mirror failed. Both mint a *local* canonical id, and
    inferring turns that id into a Slack ts Slack never issued — which
    ``_slack_parent_ts`` then hands to ``chat.postMessage`` as a ``thread_ts``,
    producing an orphan post, a ``ThreadNotFound`` and an evicted thread. Nothing
    in the row distinguishes the two cases, so the guess is now refused.

    Legacy rows written before the mapping was recorded stay NULL: the one-off
    repair script for them was retired.
    """
    return row.slack_ts


class _HeldVerdict(NamedTuple):
    """The verdict an interview thread already holds, and the turn it came from.

    Recorded per thread in ``SimulationEngine._assessed_threads`` so a second
    ``<assessment_json>`` sidecar on the same thread can be JUDGED rather than
    merely counted: a re-capture of the same turn is a duplicate and is refused,
    while a strictly later reply that concludes or closes the interview is the
    better-informed verdict and supersedes this one. See ``_sidecar_refusal``.

    ``ordinal`` is the message ordinal of the reply that carried it
    (``thread.message_count + 1`` as read at capture time). ``final`` means that
    reply CLOSED the thread, so no later turn exists and nothing may supersede
    it. ``slack_ts`` is the stored row's own link back to that reply, kept for
    logging; since §8.1 the row is found by (run, thread). ``revision`` is the
    row's ``verdict_revision`` (1 for a first verdict or a pre-0054 row).

    Whether an interview's headline is public lives in the headlines ledger
    (``headlines.is_announced``, spec §8.2), not here. A CONCLUDE-ordinal reply
    is terminal enough to ANNOUNCE but not to freeze the thread (``final``),
    because ``thread_guidance`` renders CONCLUDE for every ordinal above 11.
    """

    ordinal: int
    final: bool
    slack_ts: str | None
    revision: int = 1


__all__ = [
    "HEADLINE_IN_DOUBT", "_HeadlineInDoubt", "_HeldVerdict", "_restored_slack_ts",
    "_thread_phase_label", "_visibility_permits", "_was_truncated",
]
