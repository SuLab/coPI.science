# Red-team audit — MASTER_PLAN.md Part 20 (agent engine, tasks 20.1–20.22)

Scope: MASTER_PLAN.md lines 70–4650, plus lines 1–69 (constraints + reconciliation).
Tree: `/home/a/scripps/coPI.science` @ `copi-prod` 18ba52c, clean, read-only.
Everything below was re-derived with `sed -n`/`grep`/`awk` against the real files and, where
cheap, by RUNNING the scenario with `.venv-test/bin/python` (outputs pasted).
Method note: I copied `src/` into a scratch dir and applied Task 20.14's diff there to run its
Step-4 assertion post-fix; the repo itself was never modified.

---

## 1. Task → verdict

| task | verdict | one line |
|---|---|---|
| 20.1 | needs-fix | Fix + anchors + pre-fix failure all verified correct; the Step-1 test has a real ruff `I001` that fails the gate. |
| 20.2 | needs-fix | Anchors exact; test-file line anchor wrong (134-144, not 141-152); `purge_thread` leaves `_max_posted_at` stale; no-longer-closed evicted threads can be resurrected by 20.18's new per-agent rebuild. |
| 20.3 | needs-fix | Behaviour + BEFORE verified live (`check_private_channel_outcome calls: 1` with `posted=False`); Step-1 test has ruff `I001` + `F401`. |
| 20.4 | **BLOCKER** | `break` replaces `return` → after finalizing, control falls through to the `:memo:`/`⏸️` checks: an un-close and a possible SECOND `_close_thread` (duplicate ThreadDecision + PI DM). Also both tests drive `_close_thread` → 2 LIVE Anthropic API calls from `tests/unit/`. |
| 20.5 | needs-fix | Fix correct, anchors exact; second test drives `_close_thread` → live LLM calls. |
| 20.6 | needs-fix | Fix + BEFORE exact and pre-fix failure correct; both tests drive `_close_thread` → live LLM calls. |
| 20.7 | OK (minor) | Anchors exact, pre-fix failure real; note the fix narrows but does not eliminate COR-6 (a late row whose `posted_at` is below the log max is still filtered). |
| 20.8 | OK | Anchors exact (1218 / 1259); ran the scenario: `message_count=12, message_count_offset=0` pre-fix, exactly as claimed. |
| 20.9 | needs-fix | Authorization fix is right and the unique-constraint case IS handled; but the `rating=-1` row silences the nav badge, the review-request email and the dashboard form — three readers in files Part 20 does not own and does not flag. |
| 20.10 | **BLOCKER** | Migration/model/`ProposalRef`/key-unification all correct; but seeding `_db_reopened_thread_ids` from `reopened_at` makes the rebuild the sole reconstructor of reopened threads — and it sets NO `message_count_offset` and NO `pi_context`, so a reopened thread is re-closed as `timeout` on the first post-restart Phase 4 and the PI's guidance is never delivered. Also unbounded `_prior_threads` growth per rebuild. Two BEFORE snippets silently delete existing comments. |
| 20.11 | **BLOCKER** | Fix + BEFORE exact; the test's `_RaisingDmClient`/`_WorkingDmClient` have no `bot_user_id`, so once Task 20.16 lands (earlier phase) `SimulationEngine(...)` raises `AttributeError` in `__init__`. |
| 20.12 | OK (minor) | Both fixes correct; `_poll_inbound_from_db` BEFORE range is 2893-2899 (plan says 2894-2899); `_send_dm` spans 394-420 (plan says 394-410). |
| 20.13 | OK | BEFORE at 4474-4475 exact; `batch` in scope; test valid and hermetic. |
| 20.14 | **BLOCKER** | The fix is right, but its own Step-4 assertion cannot pass: the corrected channel routes the reply down the private FLAT branch, so the entry's `thread_ts` is `None` and `[e for e ... if e.thread_ts == "100.0"]` is empty. Ran it post-fix: `PLAN ASSERTION len(replies)==1 -> 0`. |
| 20.15 | **BLOCKER** | Both fixes correct and both pre-fix `visibility='public'` values reproduced; test 1's `_StubReplyClient` has no `bot_user_id` (breaks after 20.16); test 2 makes a live LLM call (`_update_agent_memory` await_count 1, measured); the first BEFORE snippet deletes two comment lines. |
| 20.16 | **BLOCKER ×3** | (a) `set_bot_uid_map(self._bot_uid_map())` in `__init__` raises `AttributeError` on any client double without `bot_user_id` — breaks 8 EXISTING tests in `test_roster_sync.py`; (b) `_bot_name_to_id.get(mentions[0], mentions[0])` makes an unknown bot name resolve to itself, so `get_thread_allowed_agents` locks the thread to a phantom agent (was `None` before); (c) the plan's claim that `re` stays used in `message_log.py` is false — line 407 is its only use, so the fix leaves an unused import. Plus four placeholder tests. |
| 20.17 | OK (minor) | Anchors exact (`record_api_call` 2217, `try` 2218); no deadlock (window ages out, `has_pending_reply` persists); note the Phase-5 gate also drops a PI-directive turn, which only works because 20.20 lands after it. |
| 20.18 | needs-fix | Correctly rejects calling `_rebuild_agent_state()` (it takes no agent arg and its step 5 clobbers every cursor). But the `LlmCallLog` query is unscoped by `simulation_run_id` AND unfiltered by `cutoff` → lifetime-inflated `api_call_count` and an unbounded row load. |
| 20.19 | needs-fix | Cap bypass computed correctly, but unlike `blocked_for_regular` there is no downstream re-check against the action the LLM actually chose → one funding/PI-priority candidate lets an agent post unlimited ordinary top-level posts. |
| 20.20 | OK (minor) | BEFORE exact; `phase5_acted` is a sound signal. Note the flag can now never clear for a permanently-blocked agent (cheap busy-loop, no LLM cost). |
| 20.21 | needs-fix | Anchor exact (1396-1406), pre-fix failure real. Clearing before `generate_with_tools` runs means a transient LLM error (swallowed by Phase 4's `gather(return_exceptions=True)`) silently loses the PI's guidance. |
| 20.22 | OK | Swap 2133-2135 / restore 2215 exact; `_get_prior_threads_for_agent` really is inside the window, so the test's monkeypatch works. |

---

## 2. Findings, by severity

### B1 — BLOCKER — Task 20.4: `break` instead of `return` un-closes the thread and can double-close it

Step 3's AFTER ends the ✅ branch with `break` at the loop level instead of the `return` it replaced:

```
                if ":memo:" in entry.content:
                    ...
                    await self._close_thread(agent, thread, "proposal", summary_text)
                break
```

`_close_thread` sets `thread.status = "closed"` (simulation.py:1580). Control then falls out of the
loop into the two checks that follow in `_check_thread_outcome` (:1554-1570):

* `if ":memo:" in latest_reply: thread.status = "active"` — a draft carrying BOTH `✅` and `:memo:`
  (a confirm-plus-revised-summary reply, which the prompts positively encourage) is closed and then
  immediately re-marked `active`, plus a misleading "posted :memo: Summary, waiting for ✅" log line.
* `if "⏸️" in latest_reply or ":pause_button:" in latest_reply: await self._close_thread(...)` — a
  draft carrying `✅` and `⏸️` calls `_close_thread` TWICE: two `ThreadDecision` rows, two PI DMs,
  two `_prior_threads` entries, four memory syntheses. (Task 20.6's idempotency guard, if it has
  landed, suppresses the second — but 20.4 is sequenced BEFORE 20.6 in this part's own ordering
  note, so there is a window where it does not.)

**Corrected Step 3 AFTER (loop body only):**

```python
            history = self.message_log.get_thread_history(thread.thread_id)
            for entry in reversed(history):
                if entry.sender_agent_id != thread.other_agent_id:
                    continue
                if ":memo:" in entry.content:
                    # Proposal confirmed!
                    logger.info(
                        "[%s] Thread %s: proposal confirmed with ✅",
                        agent.agent_id, thread.thread_id,
                    )
                    memo_idx = entry.content.find(":memo:")
                    summary_text = entry.content[memo_idx:].strip() if memo_idx >= 0 else entry.content
                    agent.state.pending_proposals = [
                        p for p in agent.state.pending_proposals
                        if p.thread_id != thread.thread_id
                    ]
                    agent.state.pending_proposals.append(ProposalRef(
                        thread_id=thread.thread_id,
                        channel=thread.channel,
                        other_agent_id=thread.other_agent_id,
                        summary_text=summary_text,
                        proposed_at=time.time(),
                    ))
                    await self._close_thread(agent, thread, "proposal", summary_text)
                    return
                # The other agent's latest message is NOT a memo — this ✅ is
                # not confirming anything. Stop before an older memo. (COR-3)
                break
```

Also add a third test so this cannot regress:

```python
    @pytest.mark.asyncio
    async def test_a_reply_carrying_both_a_tick_and_a_memo_stays_closed(self, monkeypatch, tmp_path):
        import src.agent.agent as agent_mod
        from unittest.mock import AsyncMock
        monkeypatch.setattr(agent_mod, "PROFILES_DIR", tmp_path)
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response", AsyncMock(return_value="mem"),
        )
        engine, a, thread = self._engine_with_thread()

        await engine._check_thread_outcome(a, thread, "Agreed ✅ — :memo: Summary: v2")

        assert thread.status == "closed"
        assert len(engine._prior_threads[tuple(sorted(["a", "b"]))]) == 1
```

### B2 — BLOCKER — Tasks 20.4, 20.5, 20.6, 20.15: unit tests make LIVE Anthropic API calls

`_close_thread` ends in `await self._update_agent_memory(agent, event)` for BOTH agents
(simulation.py:1650-1656), and `_update_agent_memory` calls the real
`generate_agent_response` (simulation.py:5322-5333). `_sync_proposal_reviews_from_db` does the same
at :5085-5088. `get_settings()` reads `.env`, and `.env` on this host carries a non-empty
`ANTHROPIC_API_KEY` (`grep -c '^ANTHROPIC_API_KEY=.' .env` → 1), so nothing short-circuits.

Measured: driving 20.15's second test with `_update_agent_memory` instrumented printed
`t215-2 memory calls (would be LIVE llm): 1`. The plan's own Task 20.4 Step-1 note quotes
`[a] Failed to update working memory: [Errno 13] Permission denied` — that string only exists at
`src/agent/agent.py:728`, which is reached *after* `generate_agent_response` returned, i.e. the
drafter's own verification billed a real API call.

Affected: 20.4 (control test), 20.5 (`test_public_thread_path_accepts_the_shortcode_form`),
20.6 (both tests), 20.15 (`test_web_reopen_synthetic_row_stamps_private_visibility`).
Cost/flakiness aside, `agent.update_working_memory_file` also writes real
`profiles/memory/<agent_id>/public.md` files into the working tree.

The repo's own convention is to stub — `tests/integration/test_proposal_review.py:161`
(`monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake)`) and
`tests/unit/test_memory_authorship_guard.py:11,27` (PROFILES_DIR → `tmp_path` + the same stub).

**Corrected text — add to every test in 20.4/20.5/20.6 that reaches `_close_thread`, and to
20.15's second test:**

```python
    @pytest.fixture(autouse=True)
    def _no_live_llm_or_disk(self, monkeypatch, tmp_path):
        """_close_thread / _sync_proposal_reviews_from_db end in
        _update_agent_memory, which calls the real Anthropic API and writes
        profiles/memory/<id>/public.md. Stub both: this is tests/unit."""
        from unittest.mock import AsyncMock

        import src.agent.agent as agent_mod
        monkeypatch.setattr(agent_mod, "PROFILES_DIR", tmp_path)
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value="## Working Memory\n1. nothing.\n"),
        )
```

(For 20.15's second test, which is a bare async function rather than a class, the equivalent is
`engine._update_agent_memory = AsyncMock()` — cheaper and equally hermetic.)

### B3 — BLOCKER — Task 20.16: `_bot_uid_map()` in `__init__` breaks 8 existing tests

Step 3c adds, at `simulation.py:301`:

```python
        self.message_log.set_bot_uid_map(self._bot_uid_map())
```

`_bot_uid_map` (:4004-4019) does `if c and c.bot_user_id` over every entry of `self.slack_clients`.
Test doubles that only implement the slice they need have no such attribute. Verified:

```
$ .venv-test/bin/python rt20/t216.py
RAISED: AttributeError '_RaisingDmClient' object has no attribute 'bot_user_id'
```

`tests/unit/test_roster_sync.py:98-106`'s `_FakeSlackClient` defines only `agent_id`, `bot_token`
and `connect()`. `_make_engine` passes one per `existing_agents`, used at lines
129, 137, 148, 167, 186, 196, 206, 231 — **eight existing tests would error at construction.**
The plan's Step-5 neighbour list for 20.16 does not include `test_roster_sync.py`, so this
surfaces only at the phase GATE. It also breaks the plan's OWN new doubles in 20.11
(`_RaisingDmClient`, `_WorkingDmClient`) and 20.15 (`_StubReplyClient`), all of which are written
in later phases and would then be dead on arrival.

**Corrected text (add to Task 20.16, Step 3c, as a third hunk):**

```python
# src/agent/simulation.py, _bot_uid_map (:4013-4016) — before/after
# BEFORE:
        uid_map = {
            c.bot_user_id: aid
            for aid, c in self.slack_clients.items()
            if c and c.bot_user_id
        }
# AFTER:
        # getattr, not attribute access: this is now called from __init__ (see
        # set_bot_uid_map below), and `slack_clients` legitimately holds
        # partial doubles in the unit suite as well as NullTransport and
        # AgentSlackClient in production. A transport that cannot name its bot
        # user simply contributes no uid mapping.
        uid_map = {
            uid: aid
            for aid, c in self.slack_clients.items()
            if c is not None and (uid := getattr(c, "bot_user_id", None))
        }
```

and to Step 5's neighbour run add `tests/unit/test_roster_sync.py tests/unit/test_transport.py`.

### B4 — BLOCKER — Task 20.16: an unknown bot name now resolves to itself and locks the thread

Step 3b's AFTER:

```python
        agent_id = self._bot_name_to_id.get(mentions[0], mentions[0])
        return None if agent_id in SERVICE_AGENT_IDS else agent_id
```

The old body was `self._bot_name_to_id.get(bot_name)` → `None` for an unrecognised name. The new
default makes `_extract_tagged_agent("ping @GhostBot")` return `"ghostbot"`. Its only real consumer
is `MessageLog.get_thread_allowed_agents` (:370-372):

```python
        tagged_id = self._extract_tagged_agent(root.content)
        if tagged_id and tagged_id != poster_id:
            return {poster_id, tagged_id} if poster_id else {tagged_id}
```

so a root post naming a hallucinated or misspelled bot now locks the thread to
`{poster, "ghostbot"}` — permanently excluding every real agent from Phase 3 and Phase 5
(`if allowed and agent.agent_id not in allowed: continue`). `tests/unit/test_cohort_isolation.py`
already proves the LLM emits unknown bot names ("unknown bot name" warning path,
`test_unknown_bot_name_is_left_alone_and_warned`).

**Corrected Step 3b AFTER (last three lines only):**

```python
        mentions = extract_bot_mentions(content, self._bot_uid_to_agent)
        if not mentions:
            return None
        token = mentions[0]
        # Two namespaces come out of extract_bot_mentions: a lowercased bot
        # NAME (literal @Tag) and an already-resolved agent_id (a <@Uxxx>
        # mention). Resolve the first through the name map; accept the second
        # only if it really is a roster agent_id. An UNKNOWN bot name must
        # stay unknown — get_thread_allowed_agents locks a thread on this
        # value, so returning the raw name would lock it to a phantom agent.
        agent_id = self._bot_name_to_id.get(token)
        if agent_id is None and token in set(self._bot_name_to_id.values()):
            agent_id = token
        if agent_id is None:
            return None
        return None if agent_id in SERVICE_AGENT_IDS else agent_id
```

and add a regression test to the 20.16 message_log block:

```python
    def test_an_unknown_bot_name_still_extracts_nothing(self, log):
        # Pre-fix behaviour, preserved: get_thread_allowed_agents locks a
        # thread on this value, so a phantom agent_id must never come out.
        assert log._extract_tagged_agent("ping @GhostBot") is None
```

### B5 — BLOCKER — Task 20.14: the Step-1 test cannot pass after the fix

With the fix applied, the corrected `channel` is `priv-chan`, whose visibility is
`collab_private`, so Phase 5 takes the **flat** private branch (`simulation.py:2368-2380`) —
`await self._post_message(agent.agent_id, channel, message_text)` with no `thread_ts`. Ran it
against a patched scratch copy of `src/`:

```
POSTFIX entry: 1788395559.573000 channel= priv-chan thread_ts= None vis= collab_private sender= a
PLAN ASSERTION len(replies)==1 -> 0
```

(pre-fix, same script against the real tree: `channel= general thread_ts= 100.0 vis= public`.)

**Corrected Step-1 assertion block:**

```python
        await engine._phase5_new_post(a)

        posts = [e for e in engine.message_log._entries if e.sender_agent_id == "a"]
        assert len(posts) == 1
        assert posts[0].channel == "priv-chan"
        # A collab_private channel is FLAT: correcting the channel routes the
        # action down the private branch, which posts with no thread_ts. That
        # is the point — pre-fix this landed in #general, threaded onto the
        # private root, persisted visibility='public'.
        assert posts[0].thread_ts is None
        assert posts[0].visibility == VISIBILITY_COLLAB_PRIVATE
```

and Step 2's "expected failure" text should read: pre-fix the single entry is
`channel='general', thread_ts='100.0', visibility='public'`, so
`assert posts[0].channel == "priv-chan"` fails.

### B6 — BLOCKER — Task 20.10: reopened threads are re-closed and lose their PI guidance after a restart

Step 3f seeds `self._db_reopened_thread_ids` from `latest.reopened_at`. `_sync_proposal_reviews_from_db`
skips its whole reopen block for a seeded thread (`simulation.py:5075`
`if proposal.thread_id in self._db_reopened_thread_ids: continue`). That block is the ONLY code
that sets, for a reopened thread:

* `message_count_offset=existing_count` (:5131, :5142) — without it the generic rebuild loop builds
  `ThreadState(..., message_count=msg_count)` with offset 0 (:4237-4243), and `_reply_to_thread`
  recomputes `message_count = len(history) - 0` (:1348) and closes at `>= max_thread_messages`
  (12, config.py:327) → **a reopened proposal thread, which by construction has a long history,
  is re-closed as `timeout` on the very first post-restart Phase 4.** This is COR-2's exact failure
  mode, newly created by this task. The plan calls it "noted but out of scope"; it is not out of
  scope — it is caused here.
* `pi_context=guidance` (:5130) — the rebuild omits the field entirely, so the PI's reopen guidance
  never reaches a prompt after a restart. Today's (buggy) behaviour at least re-delivers it. This
  also directly contradicts what Task 20.21 exists to manage.

Second defect in the same hunk: a reopened thread is never added to `self._closed_thread_ids`, and
`self._closed_thread_ids` is the "already accounted for" marker guarding the `_prior_threads`
append (comment at :4167-4176). So **every** rebuild re-appends the pair's prior-thread entry.
With Task 20.18 adding a per-agent rebuild on every roster re-add, that is unbounded growth of
Phase-5 dedup context.

**Corrected approach (replaces Step 3f's `_db_reopened_thread_ids` seeding):** keep the
`reopened_at` column and the rebuild's "don't re-close" behaviour, but do NOT let the durable flag
suppress the tick's `ThreadState` reconstruction. Split the reopen block's two jobs — minting the
synthetic guidance row (must happen once, ever) and rebuilding the ThreadState (must happen once
per process):

```python
# Step 3f, rebuild "1." section — replace the reopened branch with:
                    for td in all_decisions:
                        latest = latest_for_thread[td.thread_id]
                        if latest.reopened_at is not None:
                            # Reopened and not since re-closed: do NOT put it in
                            # closed_thread_ids, but DO put it in
                            # self._closed_thread_ids' accounting role by
                            # recording it here, so the _prior_threads append
                            # below stays idempotent across repeated rebuilds.
                            reopened_thread_ids.add(td.thread_id)
                        else:
                            closed_thread_ids.add(td.thread_id)
                        ...
                    self._closed_thread_ids.update(closed_thread_ids)
                    self._prior_thread_accounted.update(closed_thread_ids | reopened_thread_ids)
```
with the `_prior_threads` guard changed from `if td.thread_id in self._closed_thread_ids` to
`if td.thread_id in self._prior_thread_accounted` (a new set initialised in `__init__` and updated
by `_close_thread` alongside `_closed_thread_ids`).

```python
# Step 3g — do NOT seed _db_reopened_thread_ids from the rebuild. Instead make the
# guidance-row mint idempotent against the log the rebuild already reloaded:
                # Create a synthetic log entry for the PI guidance UNLESS the
                # thread history already carries this exact guidance (a prior
                # process minted it and the rebuild reloaded it from the DB).
                already = any(
                    e.sender_name == "PI (via web)" and e.content == guidance
                    for e in self.message_log.get_thread_history(thread_id)
                )
                if not already:
                    minted = self.mint_ts()
                    pi_entry = LogEntry(... visibility=self._resolve_channel_visibility(channel))
                    self.message_log.append(pi_entry)
```
Everything below that (the `_closed_thread_ids.discard`, `_mark_thread_decisions_reopened`,
`existing_count`, and the two `ThreadState(..., message_count_offset=existing_count,
pi_context=guidance)` constructions) then runs exactly once per process, restoring the offset and
the guidance the rebuild cannot supply.

If the coordinator prefers to keep the seeding as drafted, then Step 3f MUST also carry
`message_count_offset` and `pi_context` into the rebuild's `ThreadState` — which means reading the
latest `sender_name="PI (via web)"` entry per thread and counting history up to it. That is a
larger change; the split above is smaller and strictly safer.

Finally, the integration test as written passes with the regression present (its thread has 3
messages, well under 12, and it never asserts the offset). Add:

```python
    assert eng.agents["su"].state.active_threads[root_ts].message_count_offset > 0, (
        "a reopened thread came back with no reply budget — the first Phase 4 "
        "recompute would close it as 'timeout'"
    )
```

### M1 — MAJOR — Tasks 20.1 and 20.3: the Step-1 tests fail `scripts/ci.sh`'s ruff gate

`scripts/ci.sh:72-80,243` lints `tests/unit` with **zero** tolerance
(`"$VENV_PY" -m ruff check "${LINT_TARGETS[@]}"`; select = E,F,I,UP,B).

```
$ ruff check --stdin-filename tests/unit/test_plan_snippets.py -
tests/unit/test_plan_snippets.py:10:9: I001 Import block is un-sorted or un-formatted
tests/unit/test_plan_snippets.py:25:9: I001 Import block is un-sorted or un-formatted
tests/unit/test_plan_snippets.py:25:35: F401 `unittest.mock.AsyncMock` imported but unused
```

* 20.1 `_engine_with_failing_client`: stdlib `from unittest.mock import MagicMock` is listed AFTER
  `src.*`/`tests.*` → `I001`.
* 20.3 `_engine`: `from unittest.mock import AsyncMock` is imported and never used there → `F401`,
  and the block is also unsorted.

**Corrected 20.1 import block:**
```python
        from unittest.mock import MagicMock

        from src.agent.agent import Agent
        from src.agent.slack_client import AgentSlackClient
        from tests.fakes import slack_error
```
**Corrected 20.3 `_engine` import block (drop `AsyncMock` — it is imported again inside each test):**
```python
        from src.agent.agent import Agent
        from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE
```

### M2 — MAJOR — Task 20.9: the `rating=-1` row silences three PI-facing readers, in files this part does not own

The task's Open Decision 3 flags only "the PI forfeits the explicit review form". It misses that
the same row makes the proposal *invisible*, so the forfeiture is silent:

* `src/main.py:88-94` (`AgentBadgeMiddleware`): `reviewed = COUNT(ProposalReview WHERE agent_id=…)`,
  `badge_count += max(0, total - reviewed)` → the nav badge stops prompting.
* `src/services/email_notifications.py:170-176` (`_unreviewed_proposals`): any row → the proposal is
  dropped from the notification list → **the review-request email is never sent.**
* `src/routers/agent_page.py:251-268`: the dashboard renders it as reviewed and hides the form.
* `src/services/email_notifications.py:763-800` (weekly digest): `max(ratings) == -1` matches neither
  `>= 3` nor `1..2`, and `_status_label` falls through to the literal `"reviewed"`.

Confirmed harmless: `ProposalReview.rating` is a plain `SmallInteger NOT NULL` with no CHECK
(model `src/models/agent_registry.py:91`; migration `0004_...py:75`), `submitted_via` is
`String(10)` so `"engine"` fits, and `_sync_proposal_reviews_from_db`'s reopen branch requires
`r.rating == 0 and r.comment` (:5027) so `rating=-1, comment=None` cannot trigger a reopen while
still counting in `reviewed_set` (:5006). Focus-area (c) checks all pass.

**Recommendation for the coordinator (one of):**
(i) have M.1 or a new task teach the three "has the PI given a verdict?" readers to exclude
`ProposalReview.rating < 0` (`src/main.py`, `src/services/email_notifications.py`,
`src/routers/agent_page.py` — none owned by Part 20, so this needs assigning); or
(ii) drop the implicit row entirely and record durability some other way (e.g. reuse the
`reopened_at`-style marker that Task 20.10 already adds); or
(iii) accept it and say so explicitly in the decision, including "no badge and no email".
As drafted, Part 20 ships (iii) without saying it.

### M3 — MAJOR — Task 20.18: the `LlmCallLog` read is unscoped and unbounded

```python
                llm_rows = list(await db.execute(
                    sa_select(LlmCallLog.created_at).where(LlmCallLog.agent_id == agent_id)
                ))
                ...
                agent.api_call_count = len(llm_rows)
```

The existing rebuild scopes both reads by `simulation_run_id` and pushes the window into SQL
(`simulation.py:4326-4345` step 4 = `COUNT(*) WHERE simulation_run_id = …`;
`:4353-4393` step 4b = `WHERE simulation_run_id = … AND created_at >= cutoff`). Unscoped, a
re-added agent gets a LIFETIME cross-run `api_call_count` (immediately benching it under any
non-zero `--budget` via `_agent_within_budget`, and corrupting `SimulationRun.total_api_calls`),
and the query pulls every historical row for that agent into Python on each roster flip.

**Corrected text:**
```python
        agent = self.agents.get(agent_id)
        if not agent or not self.session_factory or not self.simulation_run_id:
            return
        ...
            from sqlalchemy import func as sa_func
            cutoff = datetime.now(UTC) - timedelta(
                seconds=get_settings().llm_rate_window_seconds
            )
            async with self.session_factory() as db:
                ...
                # Mirrors _rebuild_agent_state steps 4 and 4b exactly: an
                # all-time COUNT scoped to THIS run for api_call_count, and a
                # separate window query for the live throttle.
                agent.api_call_count = (await db.execute(
                    sa_select(sa_func.count(LlmCallLog.id)).where(
                        LlmCallLog.simulation_run_id == self.simulation_run_id,
                        LlmCallLog.agent_id == agent_id,
                    )
                )).scalar() or 0
                window_rows = (await db.execute(
                    sa_select(LlmCallLog.created_at)
                    .where(
                        LlmCallLog.simulation_run_id == self.simulation_run_id,
                        LlmCallLog.agent_id == agent_id,
                        LlmCallLog.created_at >= cutoff,
                    )
                    .order_by(LlmCallLog.created_at)
                )).all()
                agent.state.call_times.clear()
                for r in window_rows:
                    agent.state.call_times.append(r.created_at.timestamp())
```
The Step-1 test's `responses` list must then be `[decisions, reviews, [count_row], window_rows]`
(4 reads, not 3) — or simpler, keep 3 reads by leaving `api_call_count` to a scalar and adjusting
the fake's `execute` to return an object with `.scalar()`.

Answer to focus area (g): the plan is right that `_rebuild_agent_state(self)` takes no agent
argument (simulation.py:4148) and that its step 5 (`:4400-4403`) writes EVERY agent's cursor, so a
per-agent variant is the correct call; the cost objection is the query scoping above, not the
choice of a new method.

### M4 — MAJOR — Task 20.19: the cap bypass has no downstream re-check

`blocked_for_regular` is re-verified against the action the LLM actually chose
(`simulation.py:2286-2307`: `if not is_funding_reply and not is_funding_post and not
is_private_reply: return`). The new cap bypass has no equivalent, so a single funding/PI-priority/
private candidate in `interesting_posts` lets the agent emit arbitrarily many ordinary top-level
posts that day — the cap becomes unenforceable rather than merely bypassable.

**Corrected text — add after the existing `blocked_for_regular` re-check (:2307), inside the same
`if action == "reply" and target_post_id:` / else structure:**
```python
            # The daily cap was bypassed at the top of this method because a
            # bypass-eligible candidate existed (E7a). Re-check against the
            # action the LLM actually chose, exactly as blocked_for_regular
            # does above — otherwise one funding post in interesting_posts
            # makes the cap unenforceable for the rest of the day.
            if today_posts >= settings.daily_post_cap:
                cap_exempt = (
                    post_type == "funding_collab"
                    or self._channel_visibility.get(channel) == VISIBILITY_COLLAB_PRIVATE
                    or (
                        action == "reply" and target_post_id
                        and (
                            self.message_log.is_funding_thread(target_post_id)
                            or any(
                                p.post_id == target_post_id and p.pi_priority
                                for p in original_posts
                            )
                        )
                    )
                )
                if not cap_exempt:
                    logger.info(
                        "[%s] Phase 5: daily cap %d/%d — the chosen action is not "
                        "bypass-eligible", agent.agent_id, today_posts, settings.daily_post_cap,
                    )
                    return
```

### M5 — MAJOR — Task 20.16: `import re` in `message_log.py` becomes unused; four tests are placeholders

The task asserts "(`re` stays imported in this module — other functions still use it directly.)"
That is false:

```
$ grep -n '\bre\b' src/agent/message_log.py
4:import re
156:        re-persisted. Still idempotent on ts.      <- prose, not code
407:        match = re.search(r"@(\w+[Bb]ot)\b", content)
```
:407 is the line the task deletes. Step 3b must also drop `import re` from
`src/agent/message_log.py` (otherwise +1 `F401` against the `src/` ratchet and a false comment).

Separately, four of the five test blocks in Step 1 are not executable as written:
* `test_cohort_isolation.py` — the plan writes `def test_...(self, eng)`; there is **no `eng`
  fixture**. The file's `_strip_disallowed_tags` tests use a method helper,
  `eng = self._eng(monkeypatch, {"su", "wiseman"})` (see `tests/unit/test_cohort_isolation.py`
  around :955-1000). Corrected signature: `def test_an_all_caps_mention_is_stripped(self, monkeypatch):`
  with `eng = self._eng(monkeypatch, {"su", "wiseman"})`.
* `test_authorship_emit_gate.py` — "Mirrors an existing test's shape at :37-51 — check the exact
  fixture and assertion style there first" is a TBD. The `engine` fixture exists (agents
  good/wu/su, `_agent_publications` preset — dossier B.6) so the body is writable; the plan should
  write it.
* `test_service_bot_attribution.py` — `self._engine_with([])` does not exist (the helper is a
  module-level `_engine(agent_specs=…, clients=…)`), `su_client._bot_user_id` is wrong (`_FakeClient`
  exposes `bot_user_id`), and the plan explicitly says "Drive however this file's fixtures feed a
  human message … or add a minimal one". Unwritten.
* `test_message_log.py`'s two new tests are fine (the `log` fixture at :10-26 already maps
  `"wisemanbot": "wiseman"`).

I verified the `mentions.py` implementation itself against every assertion in the new
`tests/unit/test_mentions.py`; all pass:
```
'@subot'/'@SUBOT'/'@SuBot'/'@SUBot' -> ['subot']       BOT_TAG_RE: True
'<@U12345>' + {"U12345":"su"}       -> ['su']          BOT_TAG_RE: False
'<@U12345>' + None                  -> []              (focus area (h): no crash)
'hey <@U12345> look'                -> ['su']
'@GrantBot posted this — @WisemanBot?' -> ['grantbot','wisemanbot']
composed strip: 'Great point @CravattBot, shall we?' -> 'Great point, shall we?'
                'a@subot.example'                   -> 'a@subot.example'   (lookbehind intact)
```
Two notes: `test_an_unresolvable_uid_mention_is_dropped_not_guessed` passes for the wrong reason —
`<@U_UNKNOWN>` contains `_`, which `[A-Za-z0-9]+` never matches, so the uid branch does not fire at
all; use `<@UZZZZZZ>` to actually exercise the lookup miss. And Slack's labelled form
`<@U12345|name>` is not matched (returns `[]`); harmless for the Web API's text payloads, worth a
docstring line.

### M6 — MAJOR — Anchor mismatches that silently delete existing comments

Three BEFORE snippets are not byte-for-byte (comments removed), and the matching AFTER text
therefore *deletes* those comments if pasted literally:

1. **Task 20.10, Step 3f, rebuild "1." section.** The real code has an 11-line comment between
   `closed_thread_ids.add(td.thread_id)` (:4166) and `if td.thread_id in self._closed_thread_ids:`
   (:4177) explaining exactly why the `_prior_threads` append is not idempotent — the single most
   load-bearing comment for finding B6 — plus `# Carried for G3 dedup-context visibility filtering.`
   before `"origin_visibility"` (:4183). Neither appears in the plan's BEFORE or AFTER.
2. **Task 20.15, Step 3, `_poll_proposal_threads_for_pi`'s `LogEntry`.** The real code carries
   two comment lines before `slack_thread_ts=thread_id` (:3231-3232, "Slack-origin (polled from a
   Slack proposal thread) …"). Omitted from both snippets.
3. **Task 20.6, Step 3.** BEFORE/AFTER are byte-exact — no issue; listed only to note it was checked.

Fix: restore the omitted comments verbatim in both snippets, and add an explicit instruction that
comments inside a quoted region are part of the anchor.

### m1 — MINOR — Task 20.2: `purge_thread` leaves `_max_posted_at` stale, and evicted threads can be resurrected

`MessageLog._record` maintains `_max_posted_at` (`message_log.py:126-128`) and `latest_timestamp`
returns it (:444-454). `purge_thread` never recomputes it, so after purging the newest thread the
cursor Task 20.7 now derives from `latest_timestamp` sits above every surviving entry. Conservative
(nothing is re-scanned), but state and log disagree — worth a docstring line, or a recompute:
`self._max_posted_at = max((e.posted_at for e in self._entries), default=0.0)`.

Second: removing the `.discard()` means a dead thread that was NOT already closed stays *not*
closed. In-process that is safe (its log entries are gone), but Task 20.18's new
`_rebuild_one_agent_state` re-reads `self.message_log._entries` — which the purge emptied — so no
resurrection there either. It *would* resurrect on the next full restart (DB rows survive by
design). Recommend `self._closed_thread_ids.add(thread_id)` instead of removing the line, which
makes the eviction durable through the rebuild's own closed-thread accounting and keeps the
inverted test assertion (`dead_ts in engine._closed_thread_ids`) true for both cases.

### m2 — MINOR — anchor / line-number drift (all verified; none change behaviour)

| task | plan says | actual |
|---|---|---|
| 20.2 | `test_evicts_from_all_agents` at `:141-152`; `test_unknown_thread_id_is_noop` at `:153-` | 134-144; 146 |
| 20.10 | `_rebuild_agent_state` `:4148-4324` | 4148-4414 (next def 4416) |
| 20.10 | model column "(:234-237 today, right after `refined_in_channel`)" | `refined_in_channel` 232-234, `decided_at` 235-237 |
| 20.12 | `_poll_inbound_from_db` BEFORE `:2894-2899` | 2893-2899 |
| 20.12 | `_send_dm` `:394-410`, BEFORE `:400-407` | 394-420, BEFORE 400-406 |
| 20.17 | Phase-4 BEFORE `:1322-1332` | 1322-1334 |
| 20.15 | `_resolve_channel_visibility` "already used by `_post_message` at `:3402`" | correct (3402) |
| 20.10 | integration test `test_a_decided_thread_is_not_reopened_by_a_rebuild` `:157-176` | 159 |

Everything else I spot-checked is exact: `_post_message` 3280, ThreadNotFound branch 3353-3367,
`_mirrored_messages` call 3400, `_evict_dead_thread` 1788-1823 (BEFORE at 1817-1818 exact),
`load_entry` 152-160, `_entries`/`_by_ts` 113-114, `_check_thread_outcome` 1519-1570,
`_close_thread` 1572-1658, `_check_private_channel_outcome` 1660-1702 (`✅` check at 1674),
`_phase5_new_post` 2031-2464 (cap 2042-2046, channel 2264-2266, private outcome 2454-2461,
swap 2133-2135, restore 2215, `record_api_call` 2217, `try` 2218), `_run_turn` 980-1039
(cursor 1032-1033), `_phase3_activate_threads` 1174-1274 (ThreadState 1218, 1259),
`_reply_to_thread` 1336-1517 (prompt 1396-1406), `_check_pi_proposal_review` 2941-2954,
call sites 2806 / 2910 / 3250, `pi_agent_ids` 2782 / 3253, `_poll_pi_dms` 3005-3047 (BEFORE
3021-3028 exact), `_poll_proposal_threads_for_pi` 3142-3260 (LogEntry 3223-3237),
`_flush_llm_logs` 4448-4475 (BEFORE 4474-4475 exact), `_bot_uid_map` 4004-4019,
`_service_bot_uids` 301, `set_bot_name_map` 4665, to_add loop 4638-4662 (BEFORE 4657-4662 exact),
`_rebuild_agent_state` `reviewed_set` 4263 / `is_reviewed` 4295 / `ProposalRef` 4297,
`_sync_proposal_reviews_from_db` 4976-5154 (`reviewed_set` 5006, guidance branch 5027,
`unblock` 5053, `_db_reopened_thread_ids` check 5075 / add 5145, `discard` 5121,
`pi_entry` 5108-5117), `_reopen_thread` 2956 (`discard` 2958), `_strip_disallowed_tags` regex 2523,
`_reject_ungrounded_authorship` finditer 2558, PI-literal check 2830-2833, `_PROSE_LAB_RE` 225,
`ProposalRef` state.py 50-58, `ThreadDecision` model 209-240, `latest_timestamp` 444-454,
`get_thread_message_count` 239-246, `_extract_tagged_agent` 398-411, `slack_client._post_one`
681-746 / `post_message` 749-820 (`if not posted: return None` at 817-818),
`test_slack_client_contract.py` chunk test 889-903, `TestPrivateChannelFinalization` 566-651,
`TestPIHandlerAccounting` 563.

### m3 — MINOR — Task 20.7's fix narrows but does not close COR-6

`latest_timestamp` is a **global max** over the log. A row that commits after the cursor advanced
but whose `posted_at` is below that max is still filtered out by `posted_at <= since`
(`message_log.py:199/311/339/435`). The fix removes the wall-clock/DB-clock skew term, which is the
mechanism the finding names, so it is the right change — but the task's claim that "a late-arriving
row with posted_at between the old and new cursor stays visible" only holds when that row is also
the newest thing in the log. Recommend softening the comment, or (better, and cheap) noting that
`_poll_inbound_from_db`'s `PI_INBOX_LOOKBACK` is the real belt-and-braces for that residue.

### m4 — MINOR — Task 20.21 clears `pi_context` before the prompt is ever sent

The clear is placed immediately after `build_phase4_prompt` returns, i.e. before
`generate_with_tools`. `_reply_to_thread` is dispatched inside
`asyncio.gather(*tasks, return_exceptions=True)` (:1332), so an LLM/transport error is swallowed and
the PI's guidance is gone having reached nothing. Moving the clear to just after the
`generate_with_tools` await (still unconditional on whether the reply posts, matching the item's
wording) costs nothing and removes that hole.

### m5 — MINOR — Task 20.20 lets `has_pi_directive` latch forever

If Phase 5 is permanently unable to make a call (blocked with nothing available, or 20.17's rate
gate under sustained throttling), the flag never clears, so `has_new_work` is permanently true and
the spontaneous-post timer is permanently bypassed. Cheap (no LLM cost), but it is a latch;
consider a turn counter or a TTL and say so in the code comment.

### m6 — MINOR — Task 20.18's Step-1 test exercises a state combination production cannot reach

The test asserts `"100.0" in su.state.active_threads` with a `ThreadDecision(outcome="proposal")`
present and `_closed_thread_ids` empty. In production the startup rebuild puts every decided
thread into `_closed_thread_ids` (:4186), so `_rebuild_one_agent_state`'s
`if thread_id in self._closed_thread_ids: continue` would skip it. The assertion is not wrong, just
unrepresentative — add a sibling assertion that a thread in `_closed_thread_ids` is NOT restored.

### m7 — MINOR — other small items

* Tasks 20.3/20.14/20.19/20.22 depend on `settings.phase5_skip_probability == 0.0`. It is 0.0 by
  default (`config.py:330`) and absent from `.env` here, but `get_settings()` reads `.env`, so a
  developer with `PHASE5_SKIP_PROBABILITY` set gets flaky tests. Cheapest fix: give each such test
  a `pi_priority=True` PostRef (which bypasses the random skip) or monkeypatch the setting.
* Task 20.9 makes `_check_pi_proposal_review` `async` with an `await` inside
  `for agent in self.agents.values()`. `_sync_roster_from_db` mutates `self.agents` and both run
  from the same main-loop task, so there is no live "dict changed size during iteration" today —
  but the loop is now interruptible where it was not. Iterating `list(self.agents.values())` is a
  free hedge.
* Task 20.10 Step 3d/3e rely on `decision.id` being readable after `await db.commit()`. Verified
  safe: every session factory in the codebase sets `expire_on_commit=False`
  (`src/database.py:41`, `src/agent/main.py:81,161`, `src/worker/main.py:120`,
  `src/agent/grantbot.py:474`, `tests/conftest.py:95,120`). Worth stating in the task so nobody
  "cleans it up" later.
* Task 20.10's migration matches the 0022/0023/0024 template exactly (`revision: str = "0028"`,
  `down_revision = "0027"`, real `downgrade()` with `if_exists=True` on `drop_column`, nullable
  `ADD COLUMN` with no backfill, no `CONCURRENTLY`). `if_exists=` on `op.drop_column` is valid on
  the installed alembic 1.18.5 and already used at `0024_add_agent_role.py:35`. Lock footprint of a
  nullable `ADD COLUMN` is a catalog-only `ACCESS EXCLUSIVE` — fine with app/worker running (Part R.6).
* Task 20.15's second test comment ("harmless if the executor runs this task before 20.10 lands")
  has the ordering backwards: the execution table puts 20.10 in phase 3 and 20.15 in phase 4.
  Post-20.10 the `unblock` key is `(thread_decision_id, agent_id)` and the test's `ProposalRef` has
  `thread_decision_id=None`, so `newly_reviewed` is empty — which incidentally removes the live LLM
  call from that test. State it that way round.
* Task 20.15's first test is a sync `def` that calls `asyncio.run(...)` in an `asyncio_mode = "auto"`
  file. It works, but making it `async def` and `await`ing is the file's convention.
* Task 20.10's integration test re-imports `from datetime import UTC, datetime` inside the function
  although `tests/integration/test_state_rebuild.py:24` already imports it at module scope.
  Harmless, redundant.
* Coverage matrix: COR-1a is listed "not planned — fixed", but the red-team's own note (that no test
  pins `ThreadNotFound → False` or the four call-site `posted` guards) is not carried forward. Task
  20.1 already builds the exact machinery to pin it; adding one `ThreadNotFound` case there would
  close the last stated gap in this issue at near-zero cost.

---

## 3. Anchor mismatches (consolidated)

**Content mismatches (comments dropped from a quoted BEFORE → the AFTER deletes them):**
1. Task 20.10 Step 3f — rebuild "1." section: missing the 11-line non-idempotency comment
   (simulation.py:4167-4176) and `# Carried for G3 dedup-context visibility filtering.` (:4183).
2. Task 20.15 Step 3 — `_poll_proposal_threads_for_pi`'s `LogEntry`: missing the two-line
   "Slack-origin …" comment (:3231-3232).

**Line-range drift (symbol correct, range off):** see table in m2 — 20.2 (×2), 20.10 (×3),
20.12 (×2), 20.17 (×1).

**Symbol/fixture mismatches in test files:**
3. Task 20.16 — `test_cohort_isolation.py` has no `eng` fixture (helper is `self._eng(monkeypatch, …)`).
4. Task 20.16 — `test_service_bot_attribution.py` has no `self._engine_with`; the helper is a
   module-level `_engine(agent_specs=…, clients=…)`, and `_FakeClient` exposes `bot_user_id`
   (not `_bot_user_id`).
5. Task 20.16 — the claim "`re` … still use[d]" in `src/agent/message_log.py` is false (only :407).

Everything else listed in m2 was checked and is exact.

---

## 4. Counts

* Tasks audited: **22** (20.1–20.22).
* Verdicts: **OK 7** (20.7, 20.8, 20.12, 20.13, 20.17, 20.20, 20.22),
  **needs-fix 9** (20.1, 20.2, 20.3, 20.5, 20.6, 20.9, 20.18, 20.19, 20.21),
  **blocker 6** (20.4, 20.10, 20.11, 20.14, 20.15, 20.16).
* Findings: **BLOCKER 6** (B1–B6, where B2 spans 4 tasks and B3/B4 are both in 20.16),
  **MAJOR 6** (M1–M6), **MINOR 7** (m1–m7, m2 and m7 each bundling several items).
* Anchor mismatches: **2 content** (comments deleted), **8 line-range**, **3 test-fixture/symbol**,
  **1 false factual claim** (`re` still used).
* Scenarios actually executed against the tree: 8 (20.1 pre-fix, 20.3 pre-fix, 20.8 pre-fix,
  20.14 pre-fix AND post-fix on a patched scratch copy, 20.15 both pre-fix, 20.16 `_bot_uid_map`
  AttributeError, 20.16 `mentions.py` reference implementation, ruff on the extracted snippets).
* Coverage: every still-present id in `findings/issue_20.md` + `issue_20_redteam.md` maps to a task;
  the three exclusions (COR-1a fixed, COR-10(2) low, COR-9c hygiene, E6(3) cosmetic) are reasoned
  and match the findings' own verdicts. The only unaddressed red-team observation is COR-1a's
  missing definition-of-done test (see m7).
* Ownership/ordering: no task touches a file assigned elsewhere. Reconciliation items 1 (0028 head
  pin deferred to M.1), 3 (20.18 owns the `to_add` branch, 26.8 the surviving-agent loop) and 4
  (`mentions.py` owned here, `funding_rules.py` left to #23) are honoured. The one real ordering
  hazard is that 20.16 lands in phase 2 and its `__init__` change breaks test doubles written in
  phase 4 (B3) — and 8 existing ones immediately.
