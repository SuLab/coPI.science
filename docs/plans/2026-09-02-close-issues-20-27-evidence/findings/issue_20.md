# Issue #20 — Agent engine: turn & thread state-machine correctness — verification against `copi-prod` @ 18ba52c

Tree: /home/a/scripps/coPI.science, branch `copi-prod`, HEAD 18ba52c, clean. All line numbers below are CURRENT (they differ from the issue's `b1d54da` numbers by roughly +40..+110 in `simulation.py`). Snippets were run with `.venv-test/bin/python` against the real modules; outputs are quoted verbatim.

## 1. Summary table

| id | claim (one line) | verdict | key evidence (current file:line) | conf |
|---|---|---|---|---|
| COR-1a | `_post_message -> bool`; 4 call sites gate on it; ThreadNotFound path returns False before any LogEntry | FIXED (commit 6af8207) | simulation.py:3280-3286 signature; 1494-1498, 2370-2371, 2390-2394, 2432-2433 guards; 3356-3366 ThreadNotFound `return False` | high |
| COR-1b | swallowed non-`thread_not_found` `SlackApiError` → local id minted, row persisted, returns **True** | STILL PRESENT | slack_client.py:704-712 `_post_one` returns None; :815-816 `post_message` returns None; simulation.py:3400 `_mirrored_messages(None)`→`[]`; 3423 `enumerate(mirrored or [None])`; 3447 `return True`. Ran: `_mirrored_messages(None,"hello",None) -> []`, loop iterates `[None]` | high |
| COR-1c | `_evict_dead_thread` never purges the message log and `discard`s `_closed_thread_ids` (un-closes) | STILL PRESENT — and pinned by a test | simulation.py:1788-1825; :1818 `self._closed_thread_ids.discard(thread_id)`; tests/unit/test_thread_not_found.py:144 asserts `dead_ts not in engine._closed_thread_ids` | high |
| COR-1d | `_check_private_channel_outcome` runs outside the `posted` guard | STILL PRESENT | simulation.py:2455-2462 — gated on `message_text` + channel visibility only, after both `if not posted` branches | high |
| COR-3 | public ✅ finalizes against the first prior other-agent `:memo:` in reversed history, no recency check | STILL PRESENT | simulation.py:1527-1531; private sibling 1687-1700 skips only the handover post | high |
| COR-4 | public path matches raw ✅ only; private accepts ✅ and `:white_check_mark:`; pause accepts both forms | STILL PRESENT | :1527 `if "✅" in latest_reply`; :1563 `"⏸️" ... or ":pause_button:"`; :1674 `"✅" not in ... and ":white_check_mark:" not in ...` | high |
| COR-7 | in-process `_prior_threads` append has no dedup and no `thread_id`; DB rebuild is guarded + tested | STILL PRESENT (in-process); rebuild FIXED (02f5749) | :1583-1589 append dict {channel,outcome,summary}; :4166-4185 rebuild guard on `_closed_thread_ids`; tests/integration/test_state_rebuild.py:208-231 | high |
| COR-6 | `last_seen_cursor = time.time()` vs `posted_at <= since` filters; `latest_timestamp` exists; Phase 4 comment concedes | STILL PRESENT (issue detail about rebuild is stale) | :1033 `time.time()`; message_log.py:199/311/339/435 `<= since`; :445-455 `latest_timestamp` property; simulation.py:1302 + 1311-1315 comment; rebuild :4400-4403 computes `max(e.posted_at...)` inline, does NOT call `latest_timestamp` | high |
| COR-2 | Phase-3 activation builds `ThreadState` without `message_count_offset`; reopen paths set it; funding threads open-to-all | STILL PRESENT | :1218-1225 (tag), :1264-1271 (reply) no offset; :1348 recompute; :1359-1365 close "timeout" at `>= max_thread_messages` (=12, config.py:327); offsets set at :2989/:3000/:5131/:5142; message_log.py:363-364 funding→None | high |
| COR-5 | `_check_pi_proposal_review` matches thread_id only, any sender, flips all agents, persists nothing; Slack site outside PI loop; web thread_ts free-form; any PI clears another lab's block | STILL PRESENT | :2941-2954; Slack call :2797 precedes `for pi_agent_id in pi_agent_ids` :2800; DB path :2913; agent_page.py:952 `thread_ts: str = Form("")`, :991-998; pi_inbox.py:52-101 allows any non-private/unknown channel; dashboard agent_page.py:251-268 + email_notifications.py:171-173,763-764 read `ProposalReview`; rebuild :4297 `(td.id, aid) in reviewed_set` | high |
| COR-13 | rebuild keys `(thread_decision_id, agent_id)`, tick keys `(agent_id, thread_id)`; `ProposalRef` lacks `thread_decision_id`; `_db_reopened_thread_ids` never seeded; reopen persists a synthetic PI row per restart; Slack `_reopen_thread` writes nothing durable; rebuild re-closes | STILL PRESENT | :4264/:4297 vs :5007/:5054; state.py:50-58; :337 init, :5075 check, :5145 add (only 3 refs); :5106-5117 mint+append; :5064-5066 "independent of the reviewed flag"; :2956-3003 no DB write; :4160-4165,4186 | high |
| COR-10(1) | `_poll_pi_dms` calls `poll_dm_messages` unguarded; `_call_with_retry` catches only `SlackApiError`; poller outside turn try → transient socket error kills the sim | STILL PRESENT | :3028 unguarded; slack_client.py:310-341 `except SlackApiError` only; :505-550 `poll_channel_messages` catches `SlackListingIncomplete`/`SlackApiError` only; main loop :683 vs per-turn try :742-745; main.py:272-273 logs and falls to `finally`. slack_sdk 3.43.0 `base_client.py:483-515` re-raises the raw `err` | high |
| COR-10(2) | no DM-poll throttle — rated low | N/A (agree: low) | :3005-3046 per-agent token; slack_client.py:_dm_channels cache :825-827; idle backoff :629-640 | high |
| COR-10(3) | `_poll_inbound_from_db` advances cursor + appends before the unguarded handler; lookback dedups on the log entry → crash + permanent loss | STILL PRESENT (also lost across restart) | :2877-2878 cursor, :2879 dedup, :2893 append, :2898 `await self._handle_pi_inbound_entry` unguarded; try/except :2855-2874 covers query only; `_seed_pi_inbox_cursor` :3764-3785 seeds to max(created_at) on restart | high |
| COR-11 | `_flush_llm_logs` clears buffer before write; except only logs; under-counts the sliding window after restart | STILL PRESENT | :4452-4453 `batch = buf[:]; buf.clear()` before `try`; :4474-4475 warning only; contrast `_flush_persisted` re-queue :3946-3950; `call_times` rebuilt from `llm_call_logs` :4352-4393 | high |
| COR-9a | visibility mislabel fixed: `_post_message` resolves from channel and persists | FIXED (commit d311170) | :3419 `visibility = self._resolve_channel_visibility(channel)`; :3441 stamped on LogEntry | high |
| COR-9b | reply channel is `action_data.get("channel","general")`, never reconciled with the target post; private→public path | STILL PRESENT (nuanced: persisted leak on Slack-off; collateral eviction on Slack-on) | :2264 channel from LLM; :2366-2368 `is_private_channel` from that channel; :2390-2393 posts to `channel` with `thread_ts=target_post_id`; :2286-2294 `is_private_reply` reads `target_entry.channel` but only to unblock; no cross-check in `_post_message` :3280-3448 / `_slack_parent_ts` :3463-3479; acknowledged in tests/integration/test_full_run_live.py:41-44 | high |
| COR-9c | memory-synthesis strip downgraded to hygiene | CHANGED / agree (no live trigger) | :5262-5396 prompt never asks for `<slack_message>`; :5349 `strip_ungrounded_authorship_lines` (b6c2de4); no tag strip on that path | high |
| COR-8 | `<@Uxxx>` undetected; regex `@(\w+[Bb]ot)\b` case-blind above `[Bb]`; literal PI check; `bot_uid_to_agent` only for attribution; web UI synthesizes literal | STILL PRESENT (plus 2 more regex copies the issue missed) | message_log.py:407; funding_rules.py:154; **simulation.py:2523 and :2558** (same pattern); PI literal :2831-2832; `_bot_uid_map` :4004-4019 used at :4036/:4067/:4123; agent_page.py:975-976; `grep -rn '<@' src/` → 0 hits. Ran regex: `<@U12345>`→None, `@subot`→su, `@SUBOT`→None, `@SuBot`→su | high |
| E6(1) | no mid-turn rate check; Phase 4 fans out in one `gather` with per-retry booking | STILL PRESENT | `_within_rate_limit` called only at :891/:893 (`_turn_eligible`); `record_api_call()` at :1099,:1145,:1414,:2217,:5323 + `on_retry=agent.record_api_call` :1428; `asyncio.gather(*tasks, return_exceptions=True)` :1330-1334 | high |
| E6(2) | roster re-add builds a fresh `Agent` → pending_proposals/threads/cursors/call_times/throttled lost; no rebuild follows | STILL PRESENT (also resets `last_seen_cursor` to 0.0 → full rescan) | :4658 `agent = Agent(agent_id=aid, ...)`; agent.py:87 `self.state = AgentState()`; after-add only `set_bot_name_map`/`_load_pi_mappings`/`_recompute_allowed_sender_ids` :4665-4675; tests/unit/test_roster_sync.py:127 asserts presence only | high |
| E6(3) | `total_api_calls` recomputed from live roster, non-monotonic | STILL PRESENT (cosmetic, as labelled) | :3939; main.py:293; comment :186-191 | high |
| E7a | daily-cap gate returns before PI-priority/private/funding bypasses are computed | STILL PRESENT | :2043-2046 return; `has_pi_priority` :2061; bypasses :2070-2091 | high |
| E7b | `has_pi_directive` cleared unconditionally at `_run_turn` scope | STILL PRESENT (wording: "throttled" turns never reach `_run_turn`; "capped/skipped/blocked" is the real path) | :1030 (origin 713aa20); consumed only at :1007 as a Phase-5 trigger; never read by any prompt builder (no consumers outside simulation.py/state.py); Phase 5 bails at :2044 (cap), :2063 (random skip — `has_pi_priority` is PostRef-based), :2107 (blocked) | high |
| E7c | `thread.pi_context` set at four sites, never cleared in-process; rebuild omits it; re-injected as authoritative | STILL PRESENT | set :2826,:2931,:2988,:5130; `grep "pi_context = None"` → 0; rebuild ThreadState :4165-4171 omits; agent.py:461-467 injects | high |
| E7d | `interesting_posts` swap/restore both precede the `try` | STILL PRESENT | swap :2134-2135; restore :2215; `try` :2218; `_run_turn` is inside loop try :742 so a raise leaves state narrowed | high |

## 2. Per-item detail

### PR E1

**COR-1a (FIXED, 6af8207 "fix(sched): suppress a post that strips to nothing, and tell the caller").**
```
3280    async def _post_message(self, agent_id, channel, text, thread_ts=None) -> bool:
3356            except ThreadNotFound:
3361                if thread_ts:
3362                    self._evict_dead_thread(thread_ts)
3366                return False
```
Callers: Phase 4 `posted = await self._post_message(...)` / `if not posted:` (1494-1498), Phase 5 private (2370-2371), Phase 5 threaded reply (2390-2394), Phase 5 top-level (2432-2433). All four skip `message_count += 1`, backoff resets and the `interesting_posts`→`active_threads` move when `posted` is False.

**COR-1b (STILL PRESENT).** `_post_one` (slack_client.py:704-712):
```
        except SlackApiError as exc:
            err = exc.response.get("error")
            if err == "thread_not_found" and thread_ts and may_raise_thread_not_found:
                raise ThreadNotFound(...)
            if err in ("channel_not_found", "not_in_channel") and self._is_private_channel(channel_id):
                raise BotNotInvitedToPrivateChannel(...)
            logger.error("[%s] Failed to post to #%s: %s", ...)
            return None
```
`post_message` (815-816): `if not posted: return None`. Back in `_post_message`: `result = client.post_message(...)` → None; `mirrored = self._mirrored_messages(result, text, slack_parent)` (3400) → `[]`; `for index, message in enumerate(mirrored or [None]):` (3423) → one iteration with `message=None` → `ts = slack_ts or self.mint_ts()` → `LogEntry(... slack_ts=None ...)` appended → `return True` (3447). Ran:
```
mirrored for result=None -> [] | loop iterates over: [None]
```
So a connected client whose `chat.postMessage` failed with e.g. `msg_too_long`, `is_archived`, `not_in_channel` (public), `invalid_auth` is indistinguishable from the Slack-off mock path: turn counted, `_check_thread_outcome` runs (can close the thread / mint a ProposalRef / DM the PI) for a message that is not on Slack. No test asserts either behaviour (`tests/unit/test_slack_web.py`, `test_transport.py` cover other aspects of `_post_message`).

**COR-1c (STILL PRESENT).** `_evict_dead_thread` (1788-1825) pops active_threads / interesting_posts / pending_proposals per agent, pops the proposal-thread poll cursor, then `self._closed_thread_ids.discard(thread_id)` (1818). No `message_log` purge. `tests/unit/test_thread_not_found.py:131` adds the ts to `_closed_thread_ids` in the fixture and `:144` asserts it is gone afterwards — the test pins the un-close. Low severity as the issue says (no phantom entry any more), but a fix that stops discarding will break that test.

**COR-1d (STILL PRESENT).** 2455-2462:
```
            if (
                message_text
                and self._channel_visibility.get(channel) == VISIBILITY_COLLAB_PRIVATE
            ):
                await self._check_private_channel_outcome(agent, channel, message_text)
```
sits after both `posted` if/else blocks and does not read `posted`. A suppressed private post carrying ✅ can still call `_finalize_private_proposal` (writes a `ThreadDecision`, blocks both bots, DMs the PI).

**COR-3 (STILL PRESENT).** 1527-1531:
```
        if "✅" in latest_reply:
            history = self.message_log.get_thread_history(thread.thread_id)
            for entry in reversed(history):
                if entry.sender_agent_id == thread.other_agent_id and ":memo:" in entry.content:
```
First match wins — nothing requires that memo to be newer than this agent's last message or to be the latest other-agent message. The private analogue (1687-1700) also takes the most-recent other-member memo but adds the handover skip ("without this a casual ✅ could finalize the un-revised proposal"). `tests/integration/test_proposal_review.py:237` drives only the happy path (`✅` immediately after the memo).

**COR-4 (STILL PRESENT).** Three marker checks, two dual-form:
- 1527 `if "✅" in latest_reply:` (public, single form)
- 1563 `if "⏸️" in latest_reply or ":pause_button:" in latest_reply:` (pause, dual)
- 1674 `if "✅" not in message_text and ":white_check_mark:" not in message_text:` (private, dual — introduced by af94bf8)
Mitigation check: the prompts instruct the unicode form (agent.py:48-56, prompts/phase4-thread-reply.md:126, prompts/agent-system.md:178), and both checks run on the LLM's raw output (pre-Slack), so exposure is the model writing the shortcode on its own. Consequence remains "missed finalization → timeout close", medium.

**COR-7 (STILL PRESENT in-process).** 1583-1589:
```
        pair_key = tuple(sorted([agent.agent_id, thread.other_agent_id]))
        self._prior_threads.setdefault(pair_key, []).append({
            "channel": thread.channel, "outcome": outcome,
            "summary": (summary_text or "")[:400] or None,
        })
```
No `thread_id`, no dedup. The `pending_proposals` sibling at 1601-1612 filters by `p.thread_id`. The rebuild path (4166-4185, commit 02f5749) is guarded by `_closed_thread_ids` and tested (`test_a_second_rebuild_does_not_duplicate_prior_thread_context`, test_state_rebuild.py:208-231). The rebuild comment explicitly says a thread with several decision rows "still contributes each of them on the first pass", so duplicates after propose→reopen→re-propose reappear on restart as the issue states.

### PR E2

**COR-6 (STILL PRESENT; one stale detail).** `_run_turn` ends with `agent.state.last_seen_cursor = time.time()` (1033, present since 497ec82). Filters: message_log.py:199, 311, 339, 435 all `if entry.posted_at <= since: continue`. `latest_timestamp` exists (445-455) backed by `_max_posted_at` (baa5583). Phase 4 uses the cursor at 1302 and its comment (1311-1315) reads "The cursor advances unconditionally each turn, so has_new can't be relied on for retry". External writers stamp `posted_at` from their own minter: `record_pi_message` pi_inbox.py:120-134 `posted_at=float(ts)` with `ts = mint_local_ts()`; grantbot.py:174-179 same. message_log.py:222-223 flags "a writer's clock can run behind — see PI_INBOX_LOOKBACK_S". Loss window as described: a row committed with `posted_at < cursor` but ingested by `_poll_inbound_from_db` after the turn ends is filtered forever; PI rows get direct side effects via `_handle_pi_inbound_entry` (mitigation), bot rows (GrantBot) do not. Partial mitigation not in the issue: `_rewind_cursors_for_private_channels` (1922-1987) rewinds member-bot cursors for private channels only.
*Issue text stale:* the rebuild (4400-4403) does `latest_ts = max(e.posted_at for e in self.message_log._entries)` inline — it does not call `latest_timestamp`. Same value, different mechanism.

**COR-2 (STILL PRESENT).** Phase 3 tag path (1218-1225) and reply path (1264-1271) construct `ThreadState(thread_id, channel, other_agent_id, message_count=self.message_log.get_thread_message_count(thread_id), has_pending_reply=True, foa_number=...)` — no `message_count_offset`. `_reply_to_thread`: `thread.message_count = len(history_entries) - thread.message_count_offset` (1348); `if thread.message_count >= settings.max_thread_messages: ... await self._close_thread(agent, thread, "timeout"); return` (1359-1365). `max_thread_messages: int = 12` (config.py:327). Reopen paths set `message_count_offset=existing_count` at 2989, 3000, 5131, 5142. `get_thread_allowed_agents` returns None for funding roots (message_log.py:363-364) so the Phase-3 `allowed` guard passes. Net effect confirmed: ThreadDecision(timeout) + PI DM + two memory updates, zero replies. No test references `message_count_offset`.

### PR E3

**COR-5 (STILL PRESENT).** 2941-2954:
```
    def _check_pi_proposal_review(self, entry: LogEntry) -> None:
        thread_ts = entry.thread_ts
        if not thread_ts: return
        for agent in self.agents.values():
            for proposal in agent.state.pending_proposals:
                if proposal.thread_id == thread_ts and not proposal.reviewed:
                    proposal.reviewed = True
```
Callers: Slack channel poller 2797 (before the `for pi_agent_id in pi_agent_ids` loop at 2800 — any human), proposal-thread poller 3250 (PI-gated by `user_id not in pi_user_ids` at 3232, but not owner-gated), DB path 2913. Web writer `post_agent_message` (agent_page.py:948-1032): `thread_ts: str = Form("")` passed through as `thread_ts.strip() or None`; `pi_may_post_to_channel` (pi_inbox.py:52-101) returns True for any channel whose visibility is not `collab_private`, and True for unknown names. Nothing writes a `ProposalReview`; dashboard (agent_page.py:251-268), email (email_notifications.py:171-173, 763-764) and the rebuild (4297) all read `ProposalReview`, so the in-memory flip is invisible to them and is undone on restart. No test references `_check_pi_proposal_review`.

**COR-13 (STILL PRESENT).** Rebuild: `reviewed_set = {(r.thread_decision_id, r.agent_id) ...}` (4264), `is_reviewed = (td.id, aid) in reviewed_set` (4297), keyed on the latest ThreadDecision per `(aid, thread_id)` (4268-4283). Tick: `reviewed_set = {(r.agent_id, r.thread_id) for r in rows}` (5007), `(agent.agent_id, proposal.thread_id) in reviewed_set` (5054). `ProposalRef` (state.py:50-58) has `thread_id, channel, other_agent_id, summary_text, proposed_at, reviewed` — no decision id. `_db_reopened_thread_ids`: `set()` at 337, checked 5075, added 5145 — never loaded from DB. Reopen loop is "independent of the reviewed flag" (5064-5066), mints `minted = self.mint_ts()` and appends a `LogEntry(sender_name="PI (via web)", content=guidance, ...)` (5106-5117) which the persist callback writes to `agent_messages`, then builds both ThreadStates with a fresh `message_count_offset` (5125-5143). So every restart with a rating-0 review and a still-pending proposal re-appends one PI-guidance row and re-grants a reply budget. Slack `_reopen_thread` (2956-3003) mutates memory only. Rebuild adds every `ThreadDecision.thread_id` to `closed_thread_ids`/`_closed_thread_ids` (4160-4165, 4186).

### PR E4

**COR-10(1) (STILL PRESENT).** 3024-3028:
```
                oldest = self._dm_poll_cursors.get(agent_id, default_cursor)
                messages = client.poll_dm_messages(pi_slack_id, oldest=oldest)
```
The only try/except in the method (3034-3043) wraps `record_pi_dm`. `poll_dm_messages` (slack_client.py:846-857) → `open_dm_channel` (`except SlackApiError` only, 826-833) → `poll_channel_messages` (`except SlackListingIncomplete` / `except SlackApiError` only, 539-550). `_call_with_retry` (310-341) `except SlackApiError`. slack_sdk 3.43.0 `web/base_client.py:483-515`: non-HTTP failures hit `except Exception as err:` and end in `raise err` / `raise last_error` — the raw `URLError`/`socket.timeout`/`ssl.SSLError` propagates. The client is built as `WebClient(token=self.bot_token)` (slack_client.py:437) with default retry handlers, so the SDK retries a connection error once and then re-raises it unchanged; nothing converts it to `SlackApiError`. Main loop (`_run_main_loop`): `await self._poll_pi_dms()` at 683; the per-turn `try: did_work = await self._run_turn(agent) except Exception:` is at 742-745. `start()` has no try around `_run_main_loop`; main.py:272-273 `except Exception: logger.exception("Simulation engine raised an exception")` then `finally` flushes and marks the run "stopped". Sibling pollers use per-item `except Exception` (2839-2840, 3203-3208). `tests/unit/test_hub_budget_scheduler.py:57-70` stubs `_poll_pi_dms` out of the loop tests, so no test exercises the raise.

**COR-10(2).** Agree with the low re-rating: per-agent client, `_dm_channels` cache (slack_client.py:825-827), tick cadence floored by `_idle_backoff` (629-640). No change needed beyond noting it.

**COR-10(3) (STILL PRESENT, worse across restart).** 2876-2898:
```
        for r in rows:
            if r.created_at and r.created_at > self._pi_inbox_cursor:
                self._pi_inbox_cursor = r.created_at              # cursor first
            if not r.message_ts or self.message_log.get_entry(r.message_ts):
                continue                                          # dedup on log
            entry = LogEntry(...)
            self.message_log.append(entry)                        # log second
            if r.is_bot: ...
            else:
                await self._handle_pi_inbound_entry(entry)        # handler last, unguarded
```
The `try/except` (2855-2874) covers only the SELECT. A raise inside `_handle_pi_inbound_entry` (e.g. `_reopen_thread`'s `_hydrate_thread_from_db`, `handle_channel_tag`) propagates to the main loop and ends the run; the entry is already in the log so the in-process lookback re-scan skips it, and on restart `_seed_pi_inbox_cursor` (3764-3785) seeds `_pi_inbox_cursor = max(created_at)` so the row is never re-polled. No test references `_handle_pi_inbound_entry`.

**COR-11 (STILL PRESENT).** 4448-4475:
```
        batch = self._llm_log_buffer[:]
        self._llm_log_buffer.clear()
        try:
            ... db.add(record) ... await db.commit()
        except Exception as exc:
            logger.warning("Failed to flush LLM call logs: %s", exc)
```
Contrast `_flush_persisted` (3946-3950) which re-queues. The sliding-window rebuild reads `LlmCallLog.created_at >= cutoff` (4364-4375) into `call_times`, so dropped rows under-count the window after a restart exactly as the issue says. Only test reference is the no-op stub (test_hub_budget_scheduler.py:69).

### PR E5

**COR-9a (FIXED, d311170).** 3419 `visibility = self._resolve_channel_visibility(channel)`; 3441 `visibility=visibility` on the LogEntry; `_resolve_channel_visibility` (1989-1997) maps from `_channel_visibility`, default public.

**COR-9b (STILL PRESENT, with a Slack-on nuance).** `channel = action_data.get("channel", "general").lstrip("#")` (2264). `is_private_channel` is derived from that `channel` (2366-2368), so a reply targeting a collab_private post while declaring `"general"` takes the threaded-reply branch and calls `self._post_message(agent.agent_id, channel, message_text, thread_ts=target_post_id)` (2390-2393). The blocked-agent bypass (2286-2294) looks up `target_entry.channel` but only to set `is_private_reply=True`; it never overwrites `channel`. `_post_message` never compares `channel` to the root's channel (`_slack_parent_ts` 3463-3479 only maps canonical→Slack ts).
- Slack-off / DB-only: entry persisted with `channel="general"`, `visibility=public` (3419), `thread_ts=<private root>` → feeds the public memory-synthesis segment (5284-5288 filters by `e.visibility == visibility`). Leak as described.
- Slack-on: `chat.postMessage(channel=<general id>, thread_ts=<private ts>)` either errors `thread_not_found` or silently drops `thread_ts` (slack_client.py:718-735); either way `_post_one` raises `ThreadNotFound`, `_post_message` calls `_evict_dead_thread(target_post_id)` on a *live* private post (removing it from every agent's interesting_posts/pending_proposals) and returns False. In the silent-drop case the text is briefly top-level in #general before `chat_delete`. So on Slack-on the failure mode is collateral eviction plus a transient public post, not a persisted row.
- Acknowledged in the codebase: tests/integration/test_full_run_live.py:41-44 documents that Phase 5 "posts there without checking it against the target post's channel" and guards it externally in the live test.

**COR-9c (CHANGED — hygiene, agree).** `_update_agent_memory` (5262-5396): the user prompt asks for working-memory text, no `<slack_message>` tags; `strip_ungrounded_authorship_lines` at 5349 (b6c2de4). There is no `<slack_message>` strip on this path; no live trigger.

**COR-8 (STILL PRESENT).** Four copies of the pattern, not two: message_log.py:407 `re.search(r"@(\w+[Bb]ot)\b", content)`, funding_rules.py:154 `_TAG_RE = re.compile(r"@(\w+[Bb]ot)\b")`, simulation.py:2523 (`_strip_disallowed_tags`) and :2558 (tag enumeration). PI literal check at 2831-2832 `if bot_name and f"@{bot_name.lower()}" in msg.get("text", "").lower():`. Ran (`scratchpad/20/cor8.py`, bot map `{"subot": "su"}`):
```
raw regex r'@(\w+[Bb]ot)\b':          MessageLog._extract_tagged_agent:   PI literal (lower):   funding_rules._TAG_RE:
  '<@U12345>'          -> None          -> None                             -> False              -> None
  '@subot'             -> subot         -> su                               -> True               -> subot
  '@SUBOT'             -> None          -> None                             -> True               -> None
  '@SuBot'             -> SuBot         -> su                               -> True               -> SuBot
  '@SUBot'             -> SUBot         -> su                               -> True               -> SUBot
  'hey <@U12345> look' -> None          -> None                             -> False              -> None
```
Precisely: the regex is case-insensitive over `\w+` and `[Bb]`, case-sensitive on the trailing `ot`; the literal check is fully case-insensitive but only matches the `@Name` form. `grep -rn '<@' src/` → no output. `_bot_uid_map` (4004-4019) is consulted only at 4036/4067/4123 (Slack reconcile attribution). Web UI synthesizes the literal: agent_page.py:975-976 `text = f"@{agent.bot_name} {text}"`. Test coverage: tests/unit/test_message_log.py:99-133 covers the service-bot exemption only; no case or `<@U…>` assertions.

### PR E6

**E6(1) (STILL PRESENT).** `_within_rate_limit` (492-515) is called only from `_turn_eligible` (891 side-effect call, 893 gate). Booking sites: Phase 2 1099 and 1145, Phase 4 1414 plus `on_retry=agent.record_api_call` (1428), Phase 5 2217, memory 5323 — none consult the limiter. Phase 4 `tasks = [self._reply_to_thread(agent, thread) for thread in threads_to_reply]; await asyncio.gather(*tasks, return_exceptions=True)` (1330-1334).

**E6(2) (STILL PRESENT; one extra consequence).** 4658 `agent = Agent(agent_id=aid, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)`; `Agent.__init__` sets `self.api_call_count = 0` and `self.state = AgentState()` (agent.py:85-87). Post-add steps (4665-4675) are `set_bot_name_map`, `_pi_slack_id_to_agent_ids.clear()` + `_load_pi_mappings()`, `_recompute_allowed_sender_ids()` — no `_rebuild_agent_state`. Lost on an inactive→active flip: `pending_proposals` (block evaporates), `active_threads`, `interesting_posts`, `call_times`/`throttled` (limiter reset), `api_call_count` (legacy cap reset), and `last_seen_cursor` → 0.0, so the re-added agent's first Phase 2 rescans every top-level post since epoch (not in the issue). Engine-level `_closed_thread_ids`/`_prior_threads` survive; working memory is on disk. `tests/unit/test_roster_sync.py:127 test_adds_newly_active_agent` asserts membership only.

**E6(3) (STILL PRESENT, cosmetic).** 3939 `run.total_api_calls = sum(a.api_call_count for a in self.agents.values())`; main.py:293 same over the boot-time `agents` list; comment 186-191 labels the counters cosmetic.

### PR E7

**E7a (STILL PRESENT).** 2043-2046 `if today_posts >= settings.daily_post_cap: ... return` precedes `has_pi_priority` (2061) and the funding/private/pi_priority bypasses (2070-2091). `_count_today_posts` (2005-2023) already excludes collab_private posts, so the private case is partially mitigated; PI-priority and funding replies are not.

**E7b (STILL PRESENT; wording nuance).** 1030 `agent.state.has_pi_directive = False` (713aa20) is unconditional at the end of `_run_turn`. The flag's only consumer is 1007 `has_pi = agent.state.has_pi_directive` → `has_new_work` (1020) → whether `_phase5_new_post` is called. No prompt builder reads it (grep: no references outside simulation.py/state.py; the DM *content* reaches prompts through PIHandler's profile/instruction writes, not this flag). Phase 5 can still return without an LLM call at 2044 (daily cap), 2063 (random skip — `has_pi_priority` is `PostRef.pi_priority`, not the directive flag), 2107 (blocked, nothing available); the flag is then cleared. A *throttled* agent is not selected (`_turn_eligible`) so `_run_turn` never runs and the flag is preserved — the issue's "throttled" wording is inaccurate; "capped, randomly skipped or blocked" is the real path.

**E7c (STILL PRESENT).** `thread.pi_context = entry.content` at 2826 (Slack active thread), 2931 (DB active thread), `pi_context=pi_entry.content` 2988 (`_reopen_thread`), `pi_context=guidance` 5130 (web reopen). `grep -n "pi_context = None\|pi_context=None" src/agent/simulation.py` → none. Rebuild ThreadState (4165-4171) has no `pi_context`. agent.py:461-467 injects "**Your PI has posted in this thread.** Their message is authoritative" on every Phase 4 build while set.

**E7d (STILL PRESENT).** 2134-2135 `original_posts = agent.state.interesting_posts; agent.state.interesting_posts = available_posts`; 2215 `agent.state.interesting_posts = original_posts`; `try:` at 2218. Between them: `get_agent_top_level_posts` (2138), `format_foa_for_prompt`/`summarize_funding_thread` (2147-2156), `_get_prior_threads_for_agent` (2189-2191), `build_phase5_prompt` (2203-2212). `_phase5_new_post` is awaited from `_run_turn` (1023) inside the loop's `try` (742), so a raise is logged and the agent keeps the narrowed list.

## 3. Counts

26 sub-claims: **22 still present** (COR-1b, 1c, 1d, 3, 4, 7, 6, 2, 5, 13, 10(1), 10(3), 11, 9b, 8, E6-1, E6-2, E6-3, E7a-d), **2 fixed** (COR-1a, COR-9a), **0 partial** at sub-claim level (COR-1 as a whole = PARTIALLY FIXED, matching the issue), **1 changed** (COR-9c, hygiene only), **1 N/A** (COR-10(2), agreed low), **0 not reproducible**.

Contradictions with the issue text: none at verdict level — every "fixed in stack" item is fixed, every "still present" item is present. Stale/inaccurate details: (i) COR-6 says the rebuild "already uses" `latest_timestamp` — it computes `max(posted_at)` inline; (ii) E7b's "throttled" turn does not reach `_run_turn`, so the flag survives it; the real consumers are the daily cap, the random skip and the blocked-return; (iii) COR-8 lists two regex copies — there are four (add simulation.py:2523 and :2558); (iv) COR-9b on Slack-on produces collateral eviction of a live private thread plus a transient orphan in #general, not a persisted public row (the persisted leak is the Slack-off/DB-only path); (v) all line numbers have drifted (~+40..+110 in simulation.py).

Surprises: `tests/unit/test_thread_not_found.py:144` pins the COR-1c un-close (`assert dead_ts not in engine._closed_thread_ids`) — fixing COR-1c must update that test. E6(2) additionally resets `last_seen_cursor` to 0.0 (full rescan). slack_sdk 3.43.0 confirmed to re-raise raw transport errors (`base_client.py:483-515`), so COR-10(1) is not hypothetical.

## 4. What I could not verify and why

- Live Slack behaviour for a `chat.postMessage` with a `thread_ts` from another channel (COR-9b Slack-on branch): reasoned from `_post_one`'s two handled cases (`thread_not_found` error vs silent `thread_ts` drop, slack_client.py:704-735); no network calls were made.
- Whether the LLM ever emits `:white_check_mark:` in practice (COR-4 exposure): prompts ask for ✅; no run logs were inspected.
- Whether `_reopen_thread`/`handle_channel_tag` actually raise in production (COR-10(3) trigger): verified the control flow, not a live failure.
- Integration/contract tests were not executed (Docker required per PREAMBLE); test coverage claims rest on grep + reading the assertions.
