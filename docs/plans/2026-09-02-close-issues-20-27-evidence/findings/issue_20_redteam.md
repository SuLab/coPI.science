# Issue #20 — red-team pass over findings/issue_20.md (copi-prod @ 18ba52c)

Second, adversarial reviewer. Every row below was re-derived from the current tree with my own grep/sed/python; the first
agent's quotes were not used as a starting point. Line numbers are CURRENT (18ba52c). Written incrementally.

## 1. Summary table

| id | first-agent verdict | red-team result | one-line reason | evidence |
|---|---|---|---|---|
| COR-1a | FIXED (6af8207) | UPHELD | Only 4 `_post_message(` call sites exist in `src/` (grep), all four gate on the bool; ThreadNotFound path returns False before the LogEntry; pre-fix signature was `-> None` | simulation.py:1494-1498, 2370-2371, 2390-2394, 2432-2433; 3357-3367 (`except ThreadNotFound` → `return False`), LogEntry at 3416; `git show 6af8207^:src/agent/simulation.py` :3065-3071 `-> None`. Test test_simulation_logic.py:974-978 would fail pre-fix (posted None, entry appended) but covers only the empty-strip path; no test pins ThreadNotFound→False or the call-site gating |
| COR-1b | STILL PRESENT | UPHELD | `_post_one` → None on any other SlackApiError; wrapper → None; engine mints local id and returns True. `connect()` dropping the client (slack_client.py:447-457) only covers auth failure at connect time | slack_client.py:704-712, 815-816; simulation.py:3400, 3423, 3447. Ran: `_mirrored_messages(None,"hello",None) -> []` |
| COR-1c | STILL PRESENT | UPHELD | discard at 1818, no log purge; test pins it | simulation.py:1788-1825; tests/unit/test_thread_not_found.py:131,144. Ran the file: 9 passed |
| COR-1d | STILL PRESENT | QUALIFIED | Position confirmed (outside both `posted` branches) but for a flat private post no `return False` path can carry a ✅ today — latent, as the issue says; first agent implies live reachability | simulation.py:2455-2462; False sources 3300-3312 (empty strip), 3323-3329 (authorship, already passed on the same draft at 2319), 3357-3367 (needs thread_ts) |
| COR-3 | STILL PRESENT | UPHELD | first prior other-agent `:memo:` in reversed history, no recency check | simulation.py:1527-1531 vs 1687-1700 |
| COR-4 | STILL PRESENT | UPHELD | ✅ single-form at 1527; dual-form at 1563 and 1674 | simulation.py:1527, 1563, 1674 |
| COR-7 | STILL PRESENT | UPHELD | append dict has no thread_id/dedup; rebuild guarded by `_closed_thread_ids` | simulation.py:1583-1589; 4175-4185 |
| COR-6 | STILL PRESENT | UPHELD | wall-clock cursor vs `posted_at <= since`; rebuild uses inline `max(...)` not `latest_timestamp` (first agent right) | simulation.py:1033, 1071; message_log.py:199/311/339/435, 445-454; simulation.py:4401-4403 |
| COR-2 | STILL PRESENT | QUALIFIED | Code true (no offset on either Phase-3 path; close at ≥12) but reachability is narrower than "one @-mention closes a thread": Phase 3 skips `_closed_thread_ids`, and every participant's Phase 4 closes a ≥12 thread anyway, so the live window is the gap between the 12th message and the next participant turn (or a thread no roster agent tracks) | simulation.py:1196/1242 closed-skip; 1218-1225, 1259-1266 no offset; 1348, 1359-1365; `_close_thread` 1581 adds to closed set. Ran: `ThreadState(...).message_count_offset == 0` |
| COR-5 | STILL PRESENT | UPHELD | thread_id-only, any sender, all agents, nothing persisted; Slack site precedes PI loop; proposal poller gated on *any* PI; web writer free-form thread_ts + any public channel; `get_agent_with_access` checks ownership of the posting agent, not of the thread | simulation.py:2941-2954; 2797 vs 2800; 3232, 3250; 2913; agent_page.py:952, 990-998; pi_inbox.py:52-101; dependencies.py:114-140. `grep -n "ProposalReview(" src/agent/` → none |
| COR-13 | STILL PRESENT | UPHELD | rebuild `(td.id, aid)` vs tick `(agent_id, thread_id)`; reopened-set never seeded; synthetic PI row appended → persisted via callback | simulation.py:4264, 4297 vs 5007, 5054; state.py:50-58; 337/5075/5145; 5106-5117; persist callback 568 |
| COR-10(1) | STILL PRESENT | UPHELD | poll at 683 outside per-turn try 742-745; `start()` has no try; only `SlackApiError` is caught anywhere on the path; slack_sdk 3.43.0 re-raises transport errors (read installed file). Reachability condition: Slack-on AND `AgentRegistry.slack_user_id` set on an active agent | simulation.py:683, 742-745, 616; main.py:272-273; slack_client.py:310-341, 539-550, 826-833; .venv-test slack_sdk/web/base_client.py:484-515 `except Exception as err ... raise err` / `raise last_error` |
| COR-10(2) | N/A (low) | UPHELD | per-agent client, `_dm_channels` cache, idle backoff | slack_client.py:825-832; simulation.py:623-636 |
| COR-10(3) | STILL PRESENT | QUALIFIED | Ordering confirmed (cursor → dedup → append → unguarded handler). But the first agent's raise example `_hydrate_thread_from_db` is fully guarded; the real unguarded raise is `handle_channel_tag → _send_dm → client.send_dm` (Slack transport error). And the *row* is not lost (persisted by the web app, reloaded by the rebuild); the *side effects* are | simulation.py:2877-2898; 3800-3815 (hydrate guarded); pi_handler.py:343-344 → `_send_dm` 394-403 (`client.send_dm` at 403 is outside the `try` at 410); rebuild 3718-3736; cursor seed 3783-3785; 4401-4403 |
| COR-11 | STILL PRESENT | UPHELD | buffer cleared before write, except only logs; window rebuild reads `llm_call_logs` | simulation.py:4452-4453, 4474-4475; 4360-4380 |
| COR-9a | FIXED (d311170) | QUALIFIED | `_post_message` does stamp from channel (fixed, test would fail pre-fix). But two other engine writers still omit `visibility` and take the dataclass default `"public"`: the proposal-thread poller (which explicitly polls collab_private channels) and the web-reopen synthetic PI row. Human rows only → no live reader leaks today | simulation.py:3419, 3441; 3223 (no visibility; private channels explicitly routed at 3190); 5108-5117 (no visibility); message_log.py:30 default. Ran: `LogEntry(...).visibility == 'public'` |
| COR-9b | STILL PRESENT | QUALIFIED | Slack-off trace confirmed. Slack-on has three sub-branches, not one: root with `slack_ts` → ThreadNotFound/evict (first agent's case, Slack behaviour unverifiable offline); root WITHOUT `slack_ts` → `can_mirror=False` → the same persisted public row as Slack-off; root windowed out → as case 1 | simulation.py:2264, 2366-2368, 2390-2393, 3344-3352, 3463-3479, 3419, 5284-5288; slack_client.py:704-735 |
| COR-9c | CHANGED/hygiene | UPHELD | no `<slack_message>` request in the synthesis prompt; authorship strip present | simulation.py:5349 |
| COR-8 | STILL PRESENT (+2 regex copies) | UPHELD | four copies confirmed; case behaviour reproduced | message_log.py:407; funding_rules.py:154; simulation.py:2523, 2558. Ran: `<@U12345>`→None, `@SUBOT`→None, `@SuBot`/`@SUBot`/`@Subot`→su |
| E6(1) | STILL PRESENT | UPHELD | limiter consulted only in `_turn_eligible`; six booking sites incl. `on_retry`; one `gather` | simulation.py:891, 893; 1099, 1145, 1414, 1428, 2217, 5323; 1330-1334 |
| E6(2) | STILL PRESENT (+cursor reset) | UPHELD | fresh `Agent` → `AgentState()`; `_rebuild_agent_state` only called at startup; Phase 2 with `since=0.0` has no cap | simulation.py:4658, 578 (only call site besides the def); agent.py:87; 1070-1075. Ran: `AgentState().last_seen_cursor == 0.0` |
| E6(3) | STILL PRESENT (cosmetic) | UPHELD | sum over live roster | simulation.py:3939; main.py:293 |
| E7a | STILL PRESENT | UPHELD | cap return precedes `has_pi_priority` and the bypasses | simulation.py:2044-2046; 2061; 2070-2091 |
| E7b | STILL PRESENT (wording) | UPHELD | flag cleared unconditionally at end of `_run_turn`; read only at 1007; throttled agents never reach `_run_turn` | simulation.py:1007, 1030; 885-896 `_turn_eligible`; refs grep (no prompt reader) |
| E7c | STILL PRESENT | UPHELD (mis-cite) | never cleared; rebuild omits it; injected as authoritative. Rebuild ThreadState is at 4237-4243, not 4165-4171 | `grep pi_context` → set at 2826, 2931, 2988, 5130, 5247; agent.py:461-467; simulation.py:4237-4243 |
| E7d | STILL PRESENT | UPHELD | swap/restore before `try` | simulation.py:2134-2135, 2215, 2218 |
| NEW-1 | test pins the un-close | UPHELD | read + ran | tests/unit/test_thread_not_found.py:131, 144; 9 passed |
| NEW-2 | re-add resets `last_seen_cursor` → full rescan | UPHELD | see E6(2) | agent.py:87; state.py:70; simulation.py:1070-1075 |
| NEW-3 | `_seed_pi_inbox_cursor` seeds past a lost row | UPHELD (wording) | seeds to `max(created_at)`; but the row is re-loaded by the rebuild — only its side effects are lost | simulation.py:3764-3785, 3718-3736 |
| NEW-4 | four regex copies | UPHELD | grep | see COR-8 |
| NEW-5 | E7b "throttled" wording | UPHELD | see E7b | simulation.py:885-896 |
| NEW-6 | COR-9b Slack-on = collateral eviction only | QUALIFIED | one of three Slack-on sub-branches | see COR-9b |
| NEW-7 | COR-10(3) raise example `_hydrate_thread_from_db` | OVERTURNED (example only) | guarded by try/except → return | simulation.py:3801-3815 |

## 2. Detail — QUALIFIED / OVERTURNED rows

### COR-1a (FIXED) — upheld, with a coverage note
Every `_post_message(` call in `src/` (grep, four hits + the def) reads the return value and skips `message_count += 1`, the backoff resets and the `interesting_posts`→`active_threads` move when it is False (1494-1512, 2370-2386, 2390-2431, 2432-2440). The pre-fix tree (`git show 6af8207^:src/agent/simulation.py`, :3065-3071) had `-> None`. The cited regression test (tests/unit/test_simulation_logic.py:974-978) asserts `posted is False` and `_entries == []` — pre-fix it would get `None` and one appended entry, so it fails pre-fix. It exercises only the empty-strip branch; no unit test pins `ThreadNotFound → False` through `_post_message` (test_thread_not_found.py tests the client and `_evict_dead_thread` separately) nor the four call-site guards. Not an overturn — the fix is complete by reading — but the "definition of done" test for this PR does not yet exist.

One adjacent path checked and cleared: `BotNotInvitedToPrivateChannel` (slack_client.py:708-709) is not caught by `_post_message`; it propagates to Phase 5's `except Exception` (2464) / Phase 4's `gather(return_exceptions=True)` (1332). It is raised before any LogEntry, so it cannot mint a phantom.

### COR-1d — QUALIFIED (latent, not live)
```
2455            if (
2456                message_text
2457                and self._channel_visibility.get(channel) == VISIBILITY_COLLAB_PRIVATE
2458            ):
2459                await self._check_private_channel_outcome(agent, channel, message_text)
```
sits after both `if not posted` blocks and never reads `posted` — position confirmed. But for the private branch (flat post, `thread_ts=None`) `_post_message` returns False only from: (a) text empty after `<slack_message>` strip (3300-3312) — the strip removes only tags, so a draft that still carries `✅` cannot hit it; (b) the authorship chokepoint (3323-3329) — Phase 5 already ran the identical gate on the same draft at 2319 and returned, and the second pass runs on the tag-stripped text (fewer matches, never more); (c) `ThreadNotFound` (3357-3367) — requires `thread_ts`. `BotNotInvitedToPrivateChannel` raises instead of returning, which skips 2455-2459 entirely. `_finalize_private_proposal` is also idempotent per channel (1728-1751 DB existence check + `_finalized_private_channels`). So the issue's own label ("latent re-instance") is the right one; the first agent's "A suppressed private post carrying ✅ can still call `_finalize_private_proposal`" describes a path with no live trigger on this tree.

### COR-2 — QUALIFIED (reachability narrower than filed)
Code as claimed: both Phase-3 constructors (1218-1225 tag, 1259-1266 reply) omit `message_count_offset`; `_reply_to_thread` recomputes at 1348 and closes at 1359-1365 when `>= max_thread_messages` (config.py:327 = 12); reopen paths set it (2989, 3000, 5131, 5142). Ran: `ThreadState(...).message_count_offset == 0`. `_phase3_activate_threads` and `_phase4_reply_threads` run in the same `_run_turn` (relative lines 37-41 of the 955-1000 window), so activation → close happens within one turn.

What the issue and the first agent do not weigh: both Phase-3 paths skip `thread_id in self._closed_thread_ids` (1196, 1242); `_close_thread` adds to that set (1581); every roster participant's own Phase 4 closes the thread the moment its recomputed count reaches 12 (1359-1365); and the rebuild re-closes every ThreadDecision thread (4165, 4185). So an *open* ≥12-message thread exists only in three situations: (a) the race window between the 12th message (the one that tags C) and the next participant's Phase 4 — in which the thread would have been closed at the same count regardless; (b) a thread no roster agent tracks (≥12 messages from PI/humans/GrantBot/non-roster bots); (c) a thread `_evict_dead_thread` un-closed (COR-1c, 1818). "Multi-party funding threads cross 12 messages quickly" does not by itself make the thread reachable, because the participants close it at 12.

Marginal harm in case (a) is real but different from "a thread closed by one @-mention": a `ThreadDecision(timeout, agent_a=C, agent_b=B)` for a pair that never conversed, a DM to C's PI, memory events, and a `_prior_threads[(B,C)]` "you already tried this" entry that pollutes both agents' Phase-5 dedup context (1583-1589) — plus C's silence toward the tag. Also note the proposed fix (offset on activation) grants C a fresh 12-reply budget on a thread the other participants have just capped, which is a design decision the PR should make explicitly, not a pure bug fix.

### COR-10(3) — QUALIFIED (trigger example wrong; "message lost" overstated)
Ordering confirmed verbatim:
```
2877            if r.created_at and r.created_at > self._pi_inbox_cursor:
2878                self._pi_inbox_cursor = r.created_at
2879            if not r.message_ts or self.message_log.get_entry(r.message_ts):
2880                continue
...
2893            self.message_log.append(entry)
...
2898                await self._handle_pi_inbound_entry(entry)
```
with the only `try` (2855-2874) around the SELECT. Two corrections:
1. The first agent names `_reopen_thread`'s `_hydrate_thread_from_db` as an example raise. It cannot raise: 3800-3815 wraps its query in `try/except Exception → logger.warning; return`. `_check_pi_proposal_review` (2941-2954) and `_reopen_thread` (2956-3003) are pure in-memory after that. The one unguarded raise on the handler path is `handle_channel_tag` (pi_handler.py:300-346) → `_send_dm` (394-403): `result = client.send_dm(pi_slack_id, text)` at 403 is *outside* the `try` at 410; `send_dm` → `open_dm_channel` / `post_message` catch only `SlackApiError`, so a transport error (same class as COR-10(1)) propagates to the main loop. Trigger: a PI web message that @-tags a bot while Slack is on and the DM call fails at the socket level. Real, but narrower than "any raise in the handler".
2. "The PI message is silently lost" is too strong. The row was written by the web app and survives; the rebuild re-loads it (3718-3736) and it appears in every thread history. What is lost are the *triggers*: the in-memory review clear, the reopen, `pi_context`, `has_pi_directive`, and `handle_channel_tag`'s PI-priority PostRef. The tag is doubly lost because the rebuild sets `last_seen_cursor = max(posted_at)` (4401-4403), so Phase 3 never scans it either, and `_seed_pi_inbox_cursor` (3783-3785) keeps the poller from re-processing it — the first agent's NEW-3 claim holds with that wording.

### COR-9a (FIXED) — QUALIFIED: `_post_message` fixed, two other writers still default to public
`_post_message` stamps `visibility = self._resolve_channel_visibility(channel)` (3419) onto the LogEntry (3441); the cited integration test (tests/integration/test_cohort_engine_live.py:1196-1232) asserts three distinct persisted values and would fail pre-fix (all `public`). Could not run it (needs Docker); reasoned from the assertions.

Hunting for the same mislabel elsewhere: `LogEntry.visibility` defaults to `"public"` (message_log.py:30; ran: `LogEntry(...).visibility == 'public'`). Of the ten `LogEntry(` constructors in simulation.py, eight pass `visibility=`; two do not:
- **3223** in `_poll_proposal_threads_for_pi`. This poller explicitly supports collab_private proposal threads (3186-3193 routes a member bot via `_client_for_channel`), and `_finalize_private_proposal` puts the private channel into `pending_proposals` (1761-1768), so a PI's in-thread Slack reply to a memo inside a private refinement channel is appended and persisted with `visibility='public'`.
- **5108-5117** in `_sync_proposal_reviews_from_db`: the synthetic "PI (via web)" guidance row takes the proposal's channel, which is the private channel when the pending proposal came from `_finalize_private_proposal` and the review carries `refined_in_channel is None`.
Impact today is bounded because both are human rows: `_entry_allowed` returns True for `not entry.is_bot` before it reads `visibility` (message_log.py:73-76); the G2 memory filter keys on `sender_agent_id == agent.agent_id` (5284-5288); `conversation_feed.gate_clause` admits `is_bot False` (conversation_feed.py:68). So no reader leaks on them — but the "visibility mislabel" defect class is not closed, and any future reader that filters `agent_messages.visibility` will misclassify these rows. Recommend the E5 PR stamp both from `_resolve_channel_visibility(channel)`.

### COR-9b — QUALIFIED: Slack-on has three sub-branches, and one of them persists the public row
Slack-off trace (agree): `channel = action_data.get("channel", "general")` (2264) → `is_private_channel` derived from it (2366-2368) → threaded branch `_post_message(agent, "general", text, thread_ts=<private root>)` (2390-2393) → MOCK → LogEntry `channel="general"`, `visibility=public` (3419), `thread_ts=<private root>` → included in the public memory segment (5284-5288). The blocked-agent bypass (2288-2301) reads `target_entry.channel` only to set `is_private_reply`. `_post_message` never compares `channel` to the root's channel; `_slack_parent_ts` (3463-3479) only maps canonical→Slack ts.

Slack-on, by the state of the private root in the log:
1. Root has `slack_ts` → `chat.postMessage(channel=<general id>, thread_ts=<ts from another channel>)`. `_post_one` turns either a `thread_not_found` error or a silent `thread_ts` drop into `ThreadNotFound` (704-735) → `_evict_dead_thread(<live private root>)` + `return False`. This is the first agent's case. Slack's actual response to a cross-channel `thread_ts` cannot be verified offline.
2. Root has **no** `slack_ts` (persisted DB-only: posted before that bot was connected, or its own `chat.postMessage` failed and COR-1b minted a local id, or the channel never existed on Slack) → `_slack_parent_ts` → None → `can_mirror = False` (3344) → the warning branch (3347-3352) skips Slack and falls through to the *same* persisted public row as Slack-off.
3. Root windowed out of the log → `_slack_parent_ts` returns the canonical id → behaves as case 1.
So "the persisted leak is the Slack-off/DB-only path" (first agent, and the counts line item iv) is too narrow: it is the DB-only *root* path, which also occurs with Slack on.

### NEW-7 — OVERTURNED (example): `_hydrate_thread_from_db` as a raise source
See COR-10(3) item 1. The function is guarded end-to-end (3800-3815). The verdict on COR-10(3) is unaffected because `_send_dm` supplies a real unguarded raise.

## 3. First agent's mis-cited lines / symbols
- E7c: "rebuild ThreadState (4165-4171) has no `pi_context`" — 4164-4174 is the ThreadDecision loop's comment block; the rebuild `ThreadState(` is at **4237-4243** (and does omit `pi_context` and `message_count_offset`).
- COR-2: reply-path constructor cited as ":1264-1271" — it is **1259-1266**.
- COR-10(3): `_hydrate_thread_from_db` given as a raise example — guarded (3800-3815). Real unguarded raise: pi_handler.py:403 via `handle_channel_tag`.
- COR-1d: "A suppressed private post carrying ✅ can still call `_finalize_private_proposal`" — no `return False` path exists for a flat post that still carries ✅ (see detail).
- COR-9b counts-line item (iv): "the persisted leak is the Slack-off/DB-only path" — also the Slack-on path when the private root lacks `slack_ts`.
- COR-9a: "FIXED" is correct for `_post_message` but the report does not mention the two remaining unstamped writers (3223, 5108).
- COR-10(2): `_dm_channels` cache cited at "825-827" — the cache is declared at 283 and read/written at 825-832 (trivial).
All other cited lines checked (COR-1a/1b/1c, 3, 4, 5, 6, 7, 8, 11, 13, E6, E7a/b/d) point at the quoted code on 18ba52c.

## 4. Counts
26 first-agent table rows + 7 NEW claims = 33 items.
- UPHELD: 27 (COR-1a, 1b, 1c, 3, 4, 7, 6, 5, 13, 10(1), 10(2), 11, 9c, 8, E6(1), E6(2), E6(3), E7a, E7b, E7c, E7d, NEW-1..5 — NEW-3 with a wording note)
- QUALIFIED: 5 table rows (COR-1d, COR-2, COR-10(3), COR-9a, COR-9b) + NEW-6 = 6
- OVERTURNED: 0 verdicts; 1 supporting example (NEW-7, `_hydrate_thread_from_db`)
- UNVERIFIABLE: 0 rows; one sub-point (Slack's response to a cross-channel `thread_ts`, inside COR-9b) cannot be settled offline.

No verdict flips. Every "fixed in stack" item is fixed at the cited symbol; every "still present" item is present. The material corrections are: COR-9a's mislabel class survives at two other writers; COR-9b's persisted leak also occurs Slack-on; COR-2's live window is much narrower than the issue implies; COR-10(3)'s real trigger is the PI-tag DM, and the row itself is not lost; COR-1d is latent, not live.
