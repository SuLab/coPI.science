# Lost `#assessments-summary` headlines — RCA (2026-08-29)

**Symptom as reported.** Run `61ccad6d-eb1e-4023-81ba-adcea726a196` (2026-08-27
23:21:23 → 2026-08-28 00:25:32 UTC) produced **six** `opportunity_assessments`
rows and only **five** `:mag:` headlines in `#assessments-summary`.

**Verdict.** Confirmed, reproduced, and it is not a Slack fault. The missing
headline is **Rothstein — CHMP7 / ESCRT-III–nuclear-pore-injury axis in ALS**
(`37406954-accb-4eb3-a202-a192b4a34052`, `recommendation=conditional`,
`weighted_score=2.85`), the highest-scoring verdict of the run and its only
non-decline. The assessment row is intact and staff can see it on
`/admin/assessments`; the public headline was never attempted and nothing will
ever retry it.

This is the **second consecutive run** to lose a headline the same way, and
both losses were `conditional` verdicts. The loss is systematically biased
toward positives.

---

## 1. Evidence

Three independent sources agree.

**Database.** Six rows for the run:

| created_at (UTC) | subject | recommendation | score | headline? |
|---|---|---|---|---|
| 23:42:02 | lamichhane | pass | 2.25 | yes |
| 23:48:42 | slusher | pass | 2.50 | yes |
| 23:56:39 | coyne | pass | 2.60 | yes |
| 00:07:09 | yarchoan | pass | 2.30 | yes |
| 00:20:30 | **rothstein** | **conditional** | **2.85** | **NO** |
| 00:24:29 | konig | pass | 2.45 | yes |

**Run log** (`logs/blackbird_run_1787876967.log`, md5
`ee252757ad3357116a9e3c56b11064e6`). Five `Posted #assessments-summary
headline` lines. In place of the sixth:

```
00:20:29,588 [blackbird] Splitting a 4144-char post to #chemical-biology into 2 Slack messages (limit 4000)
00:20:30,083 [blackbird] Assessment stored: rothstein -> conditional (2.85, conditional)
00:20:30,084 [blackbird] Provisional verdict stored for rothstein (message ordinal 11); no
                         #assessments-summary headline until the interview concludes
00:20:30,084 === Turn 19: rothstein ===
00:20:31,086 [rothstein] Thread 1787874135.848239 reached max messages, closing
00:20:31,095 [rothstein] Thread 1787874135.848239 closed: timeout
```

The interview the log promises will "conclude" was closed **one second later**,
five minutes before the run's timer ended.

**Slack itself.** `conversations.history` on `C0BRVG6MTD3` returns exactly five
`:mag:` messages between 23:42 and 00:24. (The `Jeffrey Rothstein` headline at
2026-08-27 19:59:43 is a *different* project from the preceding run
`aa8359b9`.) Across the channel's entire life: 42 headlines, 39 `pass`
(decline) and 3 `conditional`.

---

## 2. Root cause

### 2.1 Announcement requires a "terminal" reply, and only a decline reliably is one

`_verdict_is_terminal` (`src/agent/simulation.py:3880`):

```python
thread_phase, _, _ = phase4_guidance(role, thread.message_count + 1)
return closes_thread or thread_phase == CONCLUDE
```

and the announcement gate (`:3310`, `:3326`):

```python
announce = terminal and not already_announced
...
if announce:
    await self._post_assessment_summary(agent, thread, verdict, slack_ts)
```

`closes_thread` is `"⏸️" in body` (`_reply_closes_thread`, `:7996`), and the
prompts reserve ⏸️ for a **decline**. So a decline is terminal by construction
and always announces. Every other verdict class depends entirely on landing on
message ordinal ≥ 12, which is where `phase4_guidance` renders CONCLUDE
(`src/agent/thread_guidance.py:216`).

Verified in the data: all five announced verdicts closed via
`⏸️ no-proposal close`; the one that did not was `conditional`.

### 2.2 Ordinal 12 is a single slot, and a Slack split steals it

`settings.max_thread_messages = 12` (default, **not** overridden in production
`.env`) and `thread_guidance`'s CONCLUDE boundary is a hardcoded 12. The hub
normally holds even ordinals (2, 4, 6, 8, 10, **12**) and reaches CONCLUDE
fine.

`SLACK_MAX_TEXT_CHARS = 4000` (`src/agent/slack_client.py:146`). When a reply
exceeds it, `split_for_slack` cuts it, and `_post_message` deliberately writes
**one log entry per real Slack message** — "the mirror is only in bijection with
Slack if the row count matches the message count". `get_thread_history` returns
every entry (panel notes excluded), and `_reply_to_thread` sets
`thread.message_count = len(history_entries)`.

So **one split permanently flips the hub onto odd ordinals for the rest of that
thread.** Reconstructed for thread `1787874135.848239`:

| ordinal | who | note |
|---|---|---|
| 1 | rothstein | thread root |
| 2, 4, 6, 8 | blackbird | hub on even ordinals, as designed |
| 9 + 10 | rothstein | **one 4181-char reply, split in two at 00:12:18** |
| 11 | blackbird | the verdict — DECIDE, not CONCLUDE → provisional |

The hub's own ordinal-11 reply was 4144 chars and split too, taking the thread
to 12 messages. On the next turn `simulation.py:2029` fired:

```python
if thread.message_count >= settings.max_thread_messages:
    await self._close_thread(agent, thread, "timeout")
    return
```

The thread was full, so it was closed **before generating any reply at all**.
The hub never got ordinal 12 and never will.

### 2.3 Nothing announces a verdict when the interview simply ends

This is what turns a delay into a permanent loss.

`_post_assessment_summary` has **exactly one caller in `src/`** —
`_capture_hub_assessment:3327`. `_close_thread` has no announcement hand-off.
Neither does `stop()`. When an interview ends holding an unannounced verdict,
that verdict is dropped on the floor: no headline, no `AssessmentDrop`, no
WARNING, no durable trace. The only way an operator finds out is by counting
rows against Slack messages by hand.

---

## 3. Adversarial audit

Every competing explanation was tested and refuted.

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Slack post failed / was rate-limited | **Refuted** | Zero `Failed to post assessments-summary` and zero `Skipping #assessments-summary` lines across all ten distinct saved run logs. The post was never attempted. |
| The 60-minute run timer cut it short | **Refuted as cause** | Thread force-closed 00:20:31; run ended 00:25:32. Unlimited runtime changes nothing. |
| The two `Empty reply from the model` errors ate the concluding turn | **Refuted** | Both were on thread `1787873876.657849` (**pardoll**), which was abandoned with an `empty_reply` drop and produced no assessment at all. |
| The verdict was a duplicate / superseded | **Refuted** | One row for rothstein; `assessment_drops` for the run holds exactly one row (the pardoll `empty_reply`). |
| The reply contained a ⏸️ that should have closed the thread | **Refuted** | Zero occurrences in the stored message content. The reply opens "Closing this out with an assessment rather than a further question". |
| A human deleted the Slack message | **Refuted** | The log line proves the post was never attempted. |
| The reproduction passes for the wrong reason | **Caught and corrected** | The first draft passed while logging `Skipping … channel_id=None, transport not connected` — nothing could post. After wiring `_assessments_summary_channel_id`, the positive control posts and the negative control still loses the headline. |

### 3.1 The parity rule holds on every provisional verdict ever recorded

Four on record across all saved logs, no exceptions:

| date | subject | ordinal | outcome |
|---|---|---|---|
| 2026-08-26 | konig | 10 (even) | hub reached 12 → **promoted, announced** |
| 2026-08-27 | pardoll | 10 (even) | hub reached 12 → **promoted, announced** (explicit supersession log line) |
| 2026-08-27 | slusher | 11 (odd) | thread closed `timeout` → **LOST** |
| 2026-08-28 | rothstein | 11 (odd) | thread closed `timeout` → **LOST** |

Even ordinal survives; odd ordinal dies. Slusher's split was a 4021-character
PI reply — 21 characters over the limit.

### 3.2 Reproduced in code

Driving the real `_reply_to_thread` path on a live engine with the summary
channel wired:

* seed 10 prior messages → hub replies at ordinal 11 → row written,
  `announced is False`, no headline;
* append the PI's ordinal-12 message → hub's next turn finds
  `message_count >= 12` → thread closed `timeout`, **no headline, ever**;
* **positive control:** seed 9 instead → hub reaches ordinal 12 → verdict
  promoted and headline posted.

Both assertions pass. The scratch test was removed after the run; the host
working tree is unchanged apart from the known uncommitted
`docker-compose.prod.yml` edit.

---

## 4. Why this bites positives only

A decline sets `closes_thread` and is terminal at any ordinal — structurally
immune. Only non-decline verdicts depend on the fragile ordinal, and they are
the ones staff most need to see. In the last run all five announced verdicts
were declines and the single non-decline vanished. Channel-wide the ratio is
39 declines to 3 conditionals.

Exposure is **growing**: the 2026-08-21 raise of `thread_reply` `max_tokens`
from 4000 to 16000 makes over-4000-character replies routine. Four messages
split in the last run alone, touching 3 of its 7 interview threads.

---

## 5. Required invariant

> Every `opportunity_assessments` row whose interview has ENDED gets exactly
> one `#assessments-summary` headline, exactly once, and the fact that it was
> posted survives a process restart.

Three properties, each independently violated today:

1. **Completeness** — an interview that ends by timeout, by abandonment, or by
   the run's own shutdown must still announce the verdict it holds. Today only
   a terminal *reply* announces.
2. **At-most-once** — a headline is an unretractable public post.
   `_rehydrate_assessed_threads` currently restores `announced=False` by
   design, so a restart can already double-post; any completeness fix that does
   not also make the flag durable makes that worse.
3. **Observability** — a verdict that ends up un-announced must say so. Today
   the failure is completely silent.

See `docs/plans/2026-08-29-assessment-headline-delivery-plan.md` for the fix.

---

## 6. Deliberately out of scope here

The **message-count parity break** of §2.2 — Slack splits consuming the
thread's 12-message budget — is a real second defect and it degrades more than
announcements: it locks the hub out of the CONCLUDE turn whose guidance asks
for a well-formed final verdict, and it closes interviews early as `timeout`.

It is not fixed by the plan above, deliberately. `thread.message_count` feeds
three consumers at once — the prompt phase (`phase4_guidance`), the
system-enforced close (`:2029`), and `_sidecar_refusal`'s ordinal comparisons —
and the `_PI_LAB` guidance strings are pinned by a golden master
(`tests/characterization/__snapshots__/test_agent_turn_gm.ambr`) that must not
be regenerated without operator sign-off. Changing what `message_count` counts
changes all four. That deserves its own brainstorm, spec and plan; the
delivery fix must not wait for it, and the delivery fix makes the parity break
non-destructive in the meantime.
