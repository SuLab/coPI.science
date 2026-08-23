"""A self-parented `MessageLog` entry must be treated as the root it is.

`get_thread_history` pins `root = _by_ts[thread_ts]` and then extends with
`_by_thread[thread_ts]`. An entry whose `thread_ts == ts` is in BOTH, so it is
returned twice and `get_thread_message_count` answers 2 for one message. That
count IS the interview's turn budget (`_reply_to_thread` assigns it to
`thread.message_count`, which drives the EXPLORE/DECIDE/CONCLUDE ordinal and the
`max_thread_messages` close), so it would burn at 2x and conclude early.

`normalize_inbound_message` guards the Slack ingest path — Slack sets
`thread_ts == ts` on a parent once it has replies — but `_rebuild_state_from_db`
and `_hydrate_thread_from_db` copy `thread_ts` verbatim out of the database.

Guarding only the `_by_thread` insertion is NOT the fix, and
`test_a_self_parented_entry_is_still_a_top_level_post` is what says so: `_record`
uses `thread_ts is None` — not that insertion — to decide the TOP-LEVEL indexes,
so a guard-only change leaves such an entry in NEITHER index. No longer
double-counted, and invisible to `get_new_top_level_posts` and therefore to
Phase 3.
"""
import logging

from src.agent.message_log import LogEntry, MessageLog


def _entry(ts, *, thread_ts=None, channel="general", sender="su", content="x"):
    return LogEntry(
        ts=ts, channel=channel, sender_agent_id=sender, sender_name=f"{sender}bot",
        content=content, thread_ts=thread_ts, posted_at=float(ts or 0.0),
    )


def test_a_self_parented_entry_is_not_counted_twice():
    log = MessageLog()
    log.append(_entry("100.000001", thread_ts="100.000001"))

    assert log.get_thread_message_count("100.000001") == 1, (
        "one message counted as two — the interview's turn budget burns at 2x "
        "and it concludes early"
    )
    history = log.get_thread_history("100.000001")
    assert [e.ts for e in history] == ["100.000001"], (
        f"the root was returned twice: {[e.ts for e in history]}"
    )


def test_a_self_parented_entry_is_still_a_top_level_post():
    """The trap in the naive fix: guarding only `_by_thread` hides it entirely."""
    log = MessageLog()
    log.append(_entry("100.000001", thread_ts="100.000001"))

    tops = log.get_new_top_level_posts(
        since=0.0, channels={"general"}, exclude_agent_id="other",
    )
    assert [e.ts for e in tops] == ["100.000001"], (
        "the self-parented root vanished from get_new_top_level_posts, so "
        "Phase 3 can never activate a thread on it"
    )
    assert log.get_agent_top_level_posts("su") == tops


def test_a_self_parented_entry_persists_as_a_root():
    """The persist callback must see the normalised entry, not the raw one.

    `_flush_persisted` derives `phase` from `thread_ts` when the entry carries
    none of its own, so an un-normalised entry stores `phase='thread_reply'`
    with `thread_ts` pointing at itself — and `_rebuild_state_from_db` then
    reads it straight back in on the next restart.
    """
    log = MessageLog()
    seen = []
    log.set_persist_callback(seen.append)
    log.append(_entry("100.000001", thread_ts="100.000001"))

    assert len(seen) == 1
    assert seen[0].thread_ts is None, (
        "the persisted row still parents the message to itself"
    )


def test_a_genuine_reply_is_untouched():
    """The other direction — normalisation must not flatten real threads."""
    log = MessageLog()
    log.append(_entry("100.000001"))
    log.append(_entry("100.000002", thread_ts="100.000001", sender="wiseman"))

    assert log.get_thread_message_count("100.000001") == 2
    assert [e.ts for e in log.get_thread_history("100.000001")] == [
        "100.000001", "100.000002",
    ]
    tops = log.get_new_top_level_posts(
        since=0.0, channels={"general"}, exclude_agent_id="other",
    )
    assert [e.ts for e in tops] == ["100.000001"]


def test_an_entry_with_no_ts_is_rejected_loudly(caplog):
    """A `ts=""` entry collapses every ts-less message into one, then vanishes.

    `_by_ts[""]` is a single slot, so the second such entry is deduped away; and
    `_flush_persisted` skips `if not e.ts`, so it never reaches the DB either.
    It is in neither store — the worst of both.
    """
    log = MessageLog()
    with caplog.at_level(logging.WARNING, logger="src.agent.message_log"):
        added = log.append(_entry("", content="first"))
        log.load_entry(_entry("", content="second"))

    assert added is False
    assert len(log) == 0, (
        f"a ts-less entry entered the log: {[e.content for e in log._entries]}"
    )
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text.count("ts") >= 2 and len(caplog.records) == 2, (
        f"append and load_entry must each warn: {caplog.records}"
    )


def test_a_ts_less_entry_never_reaches_the_persist_callback(caplog):
    log = MessageLog()
    seen = []
    log.set_persist_callback(seen.append)
    with caplog.at_level(logging.WARNING, logger="src.agent.message_log"):
        log.append(_entry(""))
    assert seen == []
