"""Differential contract test: the indexed MessageLog must return EXACTLY
what the linear-scan implementation returned — same elements, same ORDER —
for every read method, over a randomized log with out-of-order posted_at,
threads, panel notes, human rows and cohort gates. The reference class below
is a verbatim port of the pre-index method bodies (2026-08-21 tree)."""
import random

from src.agent.message_log import (
    PHASE_PANEL_NOTE,
    LogEntry,
    MessageLog,
    _entry_allowed,
    is_panel_note,
)


class LinearReference:
    """The pre-index read algorithms, over a plain entry list."""

    def __init__(self, entries):
        self._entries = entries
        self._by_ts = {e.ts: e for e in entries}
        self._bot_name_to_id = {}

    def get_new_top_level_posts(self, since, channels, exclude_agent_id,
                                allowed_sender_ids=None):
        results = []
        for entry in self._entries:
            if is_panel_note(entry):
                continue
            if entry.posted_at <= since:
                continue
            if entry.thread_ts is not None:
                continue
            if entry.channel not in channels:
                continue
            if entry.sender_agent_id == exclude_agent_id:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            results.append(entry)
        return results

    def get_thread_history(self, thread_ts):
        root = self._by_ts.get(thread_ts)
        replies = sorted(
            (e for e in self._entries
             if e.thread_ts == thread_ts and not is_panel_note(e)),
            key=lambda e: e.posted_at,
        )
        result = []
        if root and not is_panel_note(root):
            result.append(root)
        result.extend(replies)
        return result

    def get_thread_message_count(self, thread_ts):
        count = 1 if thread_ts in self._by_ts else 0
        count += sum(1 for e in self._entries
                     if e.thread_ts == thread_ts and not is_panel_note(e))
        return count

    def get_agent_top_level_posts(self, agent_id, limit=10):
        posts = sorted(
            (e for e in self._entries
             if e.sender_agent_id == agent_id and e.thread_ts is None
             and not is_panel_note(e)),
            key=lambda e: e.posted_at,
        )
        return posts[-limit:]

    def get_last_bot_sender_in_channel(self, channel_name):
        best = None
        for entry in self._entries:
            if is_panel_note(entry):
                continue
            if entry.channel != channel_name:
                continue
            if not entry.is_bot or not entry.sender_agent_id:
                continue
            if best is None or entry.posted_at >= best.posted_at:
                best = entry
        return best.sender_agent_id if best else None

    def get_replies_to_agent_posts(self, agent_id, since,
                                   allowed_sender_ids=None):
        agent_post_ts = {
            e.ts for e in self._entries
            if e.sender_agent_id == agent_id and e.thread_ts is None
        }
        results = []
        for entry in self._entries:
            if is_panel_note(entry):
                continue
            if entry.posted_at <= since:
                continue
            if entry.thread_ts not in agent_post_ts:
                continue
            if entry.sender_agent_id == agent_id:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            results.append(entry)
        return results

    def get_tags_for_agent(self, agent_bot_name, since,
                           allowed_sender_ids=None):
        tag = f"@{agent_bot_name}".lower()
        results = []
        for entry in self._entries:
            if is_panel_note(entry):
                continue
            if entry.posted_at <= since:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            if tag in entry.content.lower():
                results.append(entry)
        return results

    def has_new_reply_from_other(self, thread_ts, agent_id, since,
                                 allowed_sender_ids=None):
        for entry in self._entries:
            if entry.thread_ts != thread_ts:
                continue
            if is_panel_note(entry):
                continue
            if entry.posted_at <= since:
                continue
            if entry.sender_agent_id == agent_id:
                continue
            if not entry.is_bot:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            return True
        return False


def _random_log(seed, n=3000):
    rng = random.Random(seed)
    agents = ["blackbird", "su", "wang", "wu", None]
    channels = ["general", "chemical-biology", "collab-x"]
    log = MessageLog()
    log.set_bot_name_map({"subot": "su", "wangbot": "wang"})
    entries = []
    for i in range(n):
        sender = rng.choice(agents)
        is_bot = sender is not None and rng.random() > 0.1
        thread = rng.choice([None, f"root-{rng.randrange(60)}"])
        entry = LogEntry(
            ts=f"{1000 + i}.0001",
            channel=rng.choice(channels),
            sender_agent_id=sender if is_bot else None,
            sender_name=str(sender),
            content=rng.choice(["hello", "ping @SuBot", "note", "@WangBot hi"]),
            thread_ts=thread,
            # Deliberately out-of-order and colliding timestamps
            posted_at=float(rng.randrange(0, n // 2)),
            is_bot=is_bot,
            visibility=rng.choice(["public", "collab_private"]),
            phase=PHASE_PANEL_NOTE if rng.random() < 0.05 else None,
        )
        log.load_entry(entry)
        entries.append(entry)
    return log, entries


def test_indexed_reads_match_the_linear_reference_exactly():
    for seed in range(5):
        log, entries = _random_log(seed)
        ref = LinearReference(entries)
        gates = [None, {"su", "wang"}, set()]
        sinces = [0.0, 100.0, 1e9]
        for since in sinces:
            for gate in gates:
                assert log.get_new_top_level_posts(
                    since, {"general", "collab-x"}, "su", gate
                ) == ref.get_new_top_level_posts(
                    since, {"general", "collab-x"}, "su", gate
                )
                assert log.get_replies_to_agent_posts("su", since, gate) == \
                    ref.get_replies_to_agent_posts("su", since, gate)
                assert log.get_tags_for_agent("SuBot", since, gate) == \
                    ref.get_tags_for_agent("SuBot", since, gate)
        for t in [f"root-{i}" for i in range(60)] + ["missing"]:
            assert log.get_thread_history(t) == ref.get_thread_history(t)
            assert log.get_thread_message_count(t) == ref.get_thread_message_count(t)
            for gate in gates:
                assert log.has_new_reply_from_other(t, "su", 50.0, gate) == \
                    ref.has_new_reply_from_other(t, "su", 50.0, gate)
        for a in ["blackbird", "su", "wang", "wu"]:
            assert log.get_agent_top_level_posts(a, 10) == \
                ref.get_agent_top_level_posts(a, 10)
        for ch in ["general", "chemical-biology", "collab-x"]:
            assert log.get_last_bot_sender_in_channel(ch) == \
                ref.get_last_bot_sender_in_channel(ch)
