"""Projects in the MessageLog (spec §5.6–§5.8): BlackbirdBot's posting and everything in its
thread are hidden from every agent read; a lab's reply roots the project thread, is visible
to the hub and is not one of the lab's own pitches."""
import inspect

from src.agent.message_log import (
    PHASE_HUB_PROJECT,
    PHASE_PROJECT_REPLY,
    LogEntry,
    MessageLog,
)

CH = "general"
HUB = ("blackbird", "BlackbirdBot")
LAB = ("wang", "WangBot")


def _entry(ts, sender, content, *, thread_ts=None, phase=None):
    agent_id, name = sender
    return LogEntry(ts=ts, channel=CH, sender_agent_id=agent_id, sender_name=name,
                    content=content, thread_ts=thread_ts, posted_at=float(ts), phase=phase)


def _log(*entries):
    log = MessageLog()
    log.set_bot_name_map({"blackbirdbot": "blackbird", "wangbot": "wang"})
    for e in entries:
        log.append(e)
    return log


def test_hub_posting_and_its_thread_are_hidden_from_every_list_read():
    posting = "100.000000"
    log = _log(
        _entry(posting, HUB, "Blackbird project for the Wang lab", phase=PHASE_HUB_PROJECT),
        _entry("101.000000", LAB, "@BlackbirdBot thanks, we will reply", thread_ts=posting),
        _entry("102.000000", HUB, "@WangBot please reply with a post", thread_ts=posting),
    )
    for agent_id, bot_name in (HUB, LAB):
        assert log.get_new_top_level_posts(0.0, {CH}, exclude_agent_id="nobody") == []
        assert log.get_thread_history(posting) == []
        assert log.get_thread_message_count(posting) == 0
        assert log.get_agent_top_level_posts(agent_id) == []
        assert log.get_replies_to_agent_posts(agent_id, 0.0) == []
        assert log.get_tags_for_agent(bot_name, 0.0) == []
        assert log.has_new_reply_from_other(posting, agent_id, 0.0) is False
    # Still in the log for the dedup and the cursor.
    assert log.get_entry(posting) is not None
    assert log.latest_timestamp == 102.0


def test_project_reply_root_is_visible_to_the_hub_and_its_replies_to_the_lab():
    root = "200.000000"
    log = _log(
        _entry(root, LAB, "Blackbird project: organoids", phase=PHASE_PROJECT_REPLY),
        _entry("201.000000", HUB, "Thanks — which readout?", thread_ts=root),
    )
    tops = log.get_new_top_level_posts(0.0, {CH}, exclude_agent_id="blackbird")
    assert [e.ts for e in tops] == [root]
    replies = log.get_replies_to_agent_posts("wang", 0.0)
    assert [e.ts for e in replies] == ["201.000000"]
    assert [e.ts for e in log.get_thread_history(root)] == [root, "201.000000"]
    assert log.get_thread_message_count(root) == 2
    assert log.has_new_reply_from_other(root, "wang", 0.0) is True


def test_project_reply_is_left_out_of_agent_top_level_posts():
    log = _log(
        _entry("300.000000", LAB, "An ordinary pitch"),
        _entry("301.000000", LAB, "Blackbird project: organoids", phase=PHASE_PROJECT_REPLY),
    )
    assert [e.ts for e in log.get_agent_top_level_posts("wang")] == ["300.000000"]


def test_is_project_thread_and_entry():
    posting, reply_root, plain = "400.000000", "401.000000", "402.000000"
    log = _log(
        _entry(posting, HUB, "posting", phase=PHASE_HUB_PROJECT),
        _entry("400.500000", LAB, "under the posting", thread_ts=posting),
        _entry(reply_root, LAB, "reply", phase=PHASE_PROJECT_REPLY),
        _entry("401.500000", HUB, "in the project thread", thread_ts=reply_root),
        _entry(plain, LAB, "a pitch"),
        _entry("402.500000", HUB, "an interview reply", thread_ts=plain),
    )
    assert log.is_project_thread(reply_root) is True
    assert log.is_project_thread(posting) is False
    assert log.is_project_thread(plain) is False
    assert log.is_project_thread("999.000000") is False
    for ts in (posting, "400.500000", reply_root, "401.500000"):
        assert log.is_project_entry(log.get_entry(ts)) is True, ts
    for ts in (plain, "402.500000"):
        assert log.is_project_entry(log.get_entry(ts)) is False, ts
    assert log.is_hidden_entry(log.get_entry(posting)) is True
    assert log.is_hidden_entry(log.get_entry("400.500000")) is True
    assert log.is_hidden_entry(log.get_entry(reply_root)) is False
    assert log.is_hidden_entry(log.get_entry("401.500000")) is False


def test_new_methods_are_classified():
    for name in ("is_hidden_entry", "is_project_thread", "is_project_entry"):
        doc = inspect.getdoc(getattr(MessageLog, name)) or ""
        assert "COHORT-GATE: UNGATED" in doc, name
