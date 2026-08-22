# RCA + adversarial re-audit of `README.md` — corrections and root causes

Five independent falsification passes were run against the findings in `README.md`, each
instructed to try to **break** the claim rather than confirm it. This file is the result.
**Read it before acting on `README.md`:** three of that document's claims are wrong, two of
its recommended fixes would cause harm, and one of its headline framings is backwards.

Baseline: run `8b64a0e0-1fa7-40c4-b9a2-f57a4e058fb0`, images built from `accd634`.
Verdicts are CONFIRMED / REVISED / REFUTED. Everything here was re-derived independently.

---

## Scorecard

| # | Claim | Verdict |
|---|---|---|
| C1 | Capture gate biased against positive verdicts | **REVISED** — mechanism worse than stated; one evidence bullet refuted |
| C2 | Two verdicts discarded at ordinal 10, run then ended | **CONFIRMED in full** |
| C3 | Confidential verdict delivered to the PI in public | **REFUTED as framed** — the disclosure is prompt-mandated |
| C4 | Active bot for a scientist who died in 2021 | **CONFIRMED**; root cause materially better |
| C5 | 26 billed consults with no DB trace | **CONFIRMED**; consequence refuted, a worse one found |
| C6 | Two 600 s SDK read timeouts froze the run | **CONFIRMED**; the recommended fix is **REFUTED** |
| H1 | PI bots closed their own interviews | **REVISED** — 7 not 8; provenance and fix both wrong |
| H2 | Prior art truncated to 10, `count` discarded | Code **CONFIRMED**; impact **REVISED DOWN** |
| H3 | Term backoff lands on generic/mangled terms | **CONFIRMED**, with a sharper root cause |
| H4 | Refusal-truncated output consumed as complete | **CONFIRMED and stronger** |
| H5 | Parse failure laundered into a valid opinion | Counts **CONFIRMED**; diagnosis **REVISED** |
| H8 | `advance` empirically unreachable | **CONFIRMED**, and survived a censoring correction |
| M1 | `proposal` outcome unreachable | **CONFIRMED** but already documented in-repo |
| M3 | Memory convoy relocated, not removed | **CONFIRMED** — 0.0% overlap, independently |
| M6 | No prompt caching | **REVISED upward** as cost; **REFUTED** as a perf fix |
| — | Two `end_turn` empties "lost with no trace" | **REVISED** — both turns recovered; mechanism unresolved |

---

## The three things `README.md` gets wrong

### 1. C3 is refuted: the hub is obeying its prompt, and CLAUDE.md is the defect

`README.md` measured 18 hub messages restating gating + red flags + recommendation and called
it a leak. **The prompt mandates exactly that, in four places**, naming the same five fields:

- `src/agent/thread_guidance.py:131-134` — *"Close with your verdict stated inline so nothing is
  lost: the funnel stage, which gating criteria are met, not met, or unconfirmed, your
  recommendation …, the red flags you saw, and a confidence label."*
- `src/agent/thread_guidance.py:142-145`, `prompts/roles/scout_hub/agent-system.md:222-225`,
  `prompts/roles/scout_hub/phase4-thread-reply.md:121-124` — same instruction.

So the measured "leak rates" (11/13, 10/13, 13/13) are **compliance rates**. The sidecar-only
fields leaked **0/13** corpus-wide: no rubric dimension key, no `band`, no `weighted_score`,
no `raw_verdict`, in any of 1,354 messages. The strip is airtight.

**The actual defect is a documentation regression, traced to a commit.** `CLAUDE.md:471-473`
now reads *"the full verdict — rationale, red flags, gating, `raw_verdict` — never appears on
anything a PI or another lab sees."* Before `5d67e92` ("feat(hub): post a headline summary to
#assessments-summary on every held verdict") it read *"`:mag:` names the sidecar, not a post
label — **it** never appears on anything a PI or another lab sees."* That commit expanded "it"
into the **D12 field list**, which is correct for the `#assessments-summary` headline and false
for the interview thread. Verified by `git show 5d67e92 -- CLAUDE.md`.

**CLAUDE.md is wrong; the prompts are the design.** Nothing pins that sentence, and no test
anywhere asserts anything about the visible reply's content.

> **`README.md`'s fix #5 ("stop the model restating the verdict in the visible reply") must not
> be implemented.** It contradicts four prompt files and would leave the interview ending with
> nothing — and for the interviews whose sidecar was never stored, the prose is the *only*
> surviving record of the verdict.

**The real untested invariant, which nobody measured:** the prompt's actual confidentiality
boundary (`phase4-thread-reply.md:128-137`) is that the visible half may describe the idea *"only
at the level the PI has already made public"*, with the PI's own confidential specifics confined
to the sidecar. The protected class is **the PI's unpublished disclosures**, not the verdict. No
code and no test checks it. That is the finding C3 should have been.

### 2. C1's framing is too strong, and one evidence bullet is a measurement artifact

**Mechanism CONFIRMED and worse than stated.** The gate is `concluding = thread_phase == CONCLUDE
or closes_thread`. The prompts bind those to *mutually exclusive* outcomes: Outcome 1 = inline
verdict **+ sidecar, no ⏸️**; Outcome 2 = **⏸️ and "Emit no sidecar."** So the only sidecar the
code reliably accepts is one the prompt forbids. And the ordinal-12 escape hatch is empirically
dead — it was offered **once in 140 hub reply turns** this run, and **0 of 15 sidecars** used it.
Every verdict arrived at ordinal 6, 8 or 10.

A model that complied *perfectly* with the prompt would have produced 13 sidecar-less declines
and 2 refused positives: **zero stored assessments.** The 13 that survived did so through
non-compliance.

**REFUTED — "833 hub messages, only 28 contain ⏸️; 25 verdict-bearing messages lack it."**
Slack's `chat.postMessage` rewrites `⏸️` to the literal `:double_vertical_bar:`, and because of
M2, 536 of those 833 rows were re-imported from Slack and carry the shortcode. Correct in-run
figures: **27 of 129 hub thread replies carry ⏸️ (21%)**, and verdict-shaped-without-⏸️ is **8**
loose / **1** strict, not 25. (The model never writes the shortcode itself — 0 occurrences across
all runs — so `_reply_closes_thread`'s blindness to it is **latent, not active**. Do not "fix"
that as if it were the bug.)

**REVISED — "positives can't be stored."** 7 of the 8 historical `route-to-incubation` rows
arrived with **no** pause marker, through the ordinal-12 door — but 7 of those 8 predate the
`premature_sidecar` gate (`81dbe44`, 2026-08-20). Exactly **one** positive has ever been stored
under any gate: `huganir`, 3.29, via ordinal 12. The door is not broken, it is **starved**.

**The fix the authors already built and did not use.** `_retire_superseded_verdict` shipped in
**the same commit** as the gate. Once last-write-wins supersession exists, the refusal arm's own
justification ("the score is formed on a partial record — which is exactly why it yields to a
later one") is satisfied *by supersession*. Accepting an early verdict and letting a later one
replace it delivers the stated policy while never leaving an interview with nothing. The refusal
arm was dead weight the moment it shipped, and it is what destroyed markham.

### 3. H1's count, provenance, and recommended fix are all wrong

- **Count: 7, not 8.** The 8th (`epearce`) is a `timeout` row from the max-messages path at
  `06:25:56`, on a Slack-reconciled thread — nothing to do with ⏸️.
- **Provenance REFUTED.** ⏸️ is **an explicit `pi_lab` instruction**, in the very file
  `README.md` cites: `thread_guidance.py:56` (*"If you conclude this is not what Blackbird is
  looking for, start your reply with ⏸️"*) and `:75` (*"If YOU are the one declining to continue,
  start your reply with ⏸️ … Both are acceptable outcomes."*). The PI bots were **complying**,
  not imitating.
- **Severity holds exactly.** All 7 had `hub_pause = 0` — the hub had not closed, and its
  preceding message was a *question* in all 7. None produced a verdict. 7 of 7 killed a live
  interview mid-screen.

> **`README.md`'s fix #2 (a bare `agent.role == "scout_hub"` gate) must not be shipped alone.**
> The prompt still tells PI bots to withdraw with ⏸️, so after the gate the withdrawal does
> nothing: the thread stays `active`, `has_pending_reply` keeps flipping, and the hub keeps
> interviewing a partner that has publicly withdrawn until message 12. It converts 7 destroyed
> interviews into 7 **zombie** interviews and a larger bill.

---

## Corrections to the rest

**C2 — CONFIRMED in full.** Ordinal 10 re-derived from first principles (`get_thread_history`
excludes panel notes, so markham's 22 rows minus 13 panel notes leaves 9 prior → ordinal 10).
weeraratna's verdict was refused **12 seconds** before the main loop exited. Both recomputed with
the repo's own rubric code: markham **3.0400 → conditional / route-to-incubation**, weeraratna
2.2200 → pass. Two further facts `README.md` missed: `_record_assessment_drop` **does not persist
the sidecar** on any drop path (recovery was possible only incidentally, via `llm_call_logs`), and
`README.md`'s suggested "drain owed verdicts at shutdown" is **not implementable as written** —
`_pending_assessments` holds only *accepted* verdicts whose DB write failed, so there is nothing
to drain.

**C4 — CONFIRMED; better root cause.** Death independently verified (AAI In Memoriam, JHU BCMB,
an *Immunity* obituary) — not from the bot's own text. **The disqualifier was in the source data
and discarded:** `data/JHU_directory1_with_ORCID_v3_BBL.xlsx` row 61 says *"Former BDP (2018-2020);
**died 2021**"*, but `scripts/generate_sparsedata_user.py`'s input schema is
`Name<TAB>ORCID<TAB>Affiliations`, and the curated columns were routed to a human-review sidecar
CSV that never gates. Blast radius **1 of 62** (exhaustive regex over all 63 workbook rows).
Critically, **`README.md`'s own recommended detector would not have caught it**: the ORCID record
carries no death signal, and the DB holds 6 *posthumous* publications so `max(pub_year)=2024`.
Adjacent, unreported: 3 confirmed + 1 probable active bots represent **non-independent** PIs
(`camacho` inside the Casadevall lab, `gordy` inside the Markham lab, `tripathi`), and both parent
labs also have active bots — so two lab hierarchies are double-counted.

**C5 — CONFIRMED; the stated consequence is latent, a worse one is live.** Mechanism exact:
`generate_agent_response` returns `""` at `llm.py:425`, before the callback at `:503`, while
`on_api_call` already fired at `tools.py:554`. Reconciles three ways (838−812 = 558−532 = 26).
The `panel_incomplete` consequence is **REFUTED for this run** — the floor keys on
`recommendation ∈ {advance, conditional}`, which production has never emitted. The real
consequence: **both `api_call_count` and the rate limiter's `call_times` ledger are rebuilt from
`llm_call_logs`**, so 26 refusals booked throttle slots in-process and vanish on restart. Worse in
substance: `sinnis` has **no chemistry consult on record** while chemistry was attempted and
refused 5 times, and markham (the discarded 3.04) has none either — so even after a backfill, that
panel record is substantially fictional.

**C6 — CONFIRMED with a stronger proof; the recommended `timeout=120` is REFUTED.** Both stalls
fingerprint at **600.09 / 600.10 s** — `read=600` exactly — and all 838 responses were `200`, so
no overload/rate-limit explanation survives. Corrections: there were **three** SDK retries (the
third, at 07:39:19, was a fast connection failure, not a timeout — so the retry log line alone is
not a timeout detector); dead air is **1,167 s (10.6%)**, not 1,200 s; and the two slow turns are
**76% / 71%** stall, not ~100%. **`timeout=120` would cut legitimate calls** — the run's largest
legitimate reply took 119.18 s, 0.8 s under that ceiling, and `max_tokens=16000` at the observed
60.7 tok/s authorises up to 264 s. Worse, passing any `timeout=` **disables the SDK's own
`max_tokens > 21_333` guard** (`Messages.create` only applies it when `timeout == DEFAULT_TIMEOUT`)
— the guard CLAUDE.md documents. **Use `timeout=300`**, which cuts zero legitimate calls here.
**New, unreported:** `src/services/email_inbound.py:342` calls `messages.create` **synchronously
inside an `async def`**, with no timeout — one stall freezes the inbound-email poller for up to
1,801.5 s.

**The two `end_turn` empties — REVISED, and this is the biggest single correction.** There *is* a
DB trace (both rows exist with full `call_stats`), and **both turns fully recovered on retry** —
one of them produced the stored `epearce` verdict at 08:26:50. Measured cost: 6 API calls,
167,894 input tokens, ~141 s, and **zero lost verdicts**. The `has_pending_reply` retry policy
worked as designed, and a drop row on the first empty would have been a false alarm in 2 of 2
cases. The *mechanism* remains **unresolved**: the reply carried several hundred non-reasoning
output tokens with zero `text` and zero `tool_use` blocks. The deployed SDK's own docstring
(`output_tokens - thinking_tokens` approximates non-reasoning output) plus 36 calibration rows and
8 negative controls rule out the summarized-thinking and accounting explanations. The only
surviving hypothesis is a content-block type `_all_text` ignores (`redacted_thinking`), and it is
**INFERRED, not confirmed** — zero such blocks appear in 5,543 stored rows. One line in
`_call_stat` recording `[b.type for b in message.content]` would settle it permanently.

**H4 — CONFIRMED and stronger.** All 4 truncated hub replies proved to have reached Slack by
joining each row's last 60 characters against `agent_messages.content` — all 4 matched exactly, so
the truncation *is* the end of the posted message. The klein memory write **persisted twice**: a
`profile_revisions` row 1.6 ms later (replacing a complete 1,977-char memory with the 1,437-char
truncated one) and `profiles/memory/klein/public.md` on disk. The 3 truncated consults were
published as `⚠️ caution` and fed verbatim back into the hub's next prompt. True refusal count is
**47** (21 recorded + 26 traceless), so H4's own denominator was censored by C5. Root cause:
`stop_reason` is compared against `"max_tokens"` at nine sites in `llm.py` and **branched on
nowhere else in `src/`**.

**H5 — counts CONFIRMED; diagnosis REVISED.** 3 of the 6 are recoverable *by a helper this repo
already contains* — running `llm.py::_extract_json` verbatim over them succeeds on all three
`end_turn` cases (including restoring chute's `blocking`/`high` from the stored `caution`/`low`)
and fails on all three `refusal` cases. The line is clean: `end_turn` ⇒ complete JSON + trailing
prose ⇒ recoverable; `refusal` ⇒ JSON cut mid-array ⇒ unrecoverable. So the primary defect is a
**brittle parser**, not the `caution` default — which is defensible, since `clear` would turn an
unreadable specialist into an approval.

**H2 — code CONFIRMED, impact REVISED DOWN.** Live probes reproduce the counts exactly
(`resistance` 27,906, `COVID` 2,070) and confirm `count` is discarded. But the claimed PI-facing
harm is **one** message, not two, and the caveat discipline is near-perfect: **77 of 79** in-run
prior-art absence claims carry the "title-only, US-only, not novelty and not FTO" caveat, and
`fto_achievable` is **never once `met`** in 55 stored verdicts. No optimistic bias reached the
score or the gate. The real residual is a different axis: the caveats disclaim **scope** and
cannot disclaim **completeness**, because the code never tells the model there was any.
CLAUDE.md's "an empty title search is never FTO" guardrail works end-to-end — it was written
against the *empty* case, and this is its mirror image.

**H3 — CONFIRMED, with a sharper root cause.** `_salience` awards `+3` for containing a digit
**and** `+2` for `not token.islower()` — and a **pure-digit token satisfies both** (`"1".islower()`
is `False`), scoring 5 before the length bonus and beating every 2–4-letter gene symbol
(`GDF`=3, `LOX`=3). Combined with `_Q_SANITISE` splitting on hyphens, **the numeric suffix of every
`SYMBOL-N` name is promoted to rank 1**. Two examples in `README.md` need revision: `['1','LOX']`
is `1 AND LOX` (26 real hits), not a search for "1"; and "every one returned exactly 10" is true
only for single-generic-word tiers.

**H8 — CONFIRMED, and it survived the hardest available test.** Recovering *every* sidecar the
model ever emitted (stored **and** dropped) to correct for C1's censoring: 36 v2 verdicts, max
still **3.29**, still **0 ≥ 3.4**. Censoring is real (route-to-incubation 8.3% emitted → 3.4%
stored, a 2.4× bias — C1 confirmed from a second direction) but does not rescue `advance`. The
decisive evidence is that the threshold's stated premise is dead: the v2 proposal placed 3.4 on a
predicted anchor "lift", and scores went **down** (pre-v2 mean 2.459 → v2 2.404). ⚠️ **Confounded**:
`5bb5093 feat(llm): move to Opus 5 / Sonnet 5` landed 2026-08-19, between the two corpora, so
"the anchors failed" is inferred, not proven. **Do not re-fit the thresholds yet** — the stored
corpus is censored at the top, the comparison spans a model change, and the original back-test's
34-row corpus no longer exists (26 remain). A new defect found behind H8: the specialist floor
gates on the **model-written `recommendation`**, not the **computed `band`**, so a verdict that
bands `conditional` while saying `pass` exempts itself — and `route-to-incubation`, **Blackbird's
own positive outcome**, is exempt too, on the rationale that "a decline costs Blackbird nothing".

**M1 — CONFIRMED but not a discovery.** `tests/integration/test_proposal_review.py:22-29` already
states it. The actionable defect is that a structurally-zero counter is rendered to end users on
**every authenticated page load** (`src/main.py:88-96`), and that the label is affirmatively wrong
for the 13 threads that are the entire output of the pipeline.

**M3 — CONFIRMED independently.** 0 of 72 memory windows overlap any other Anthropic call; 26 of
27 drain episodes were immediately followed by a `thread_reply`, so the drain **creates** reply-lane
idle time rather than filling it. Per-agent parallelism alone buys only 3.4%, because **47% of
drain wall is the hub's own serial chain** — the hub is a party to every close.

**M6 — REVISED upward as cost, REFUTED as performance.** Measured with the real tokenizer, the
re-sent system prompt is **5.18 M tokens = 40.6%** of input (not 4.66 M / 36.5%), and the hub's
stable prefix is **90.4%** of its prompt — cacheable only because `_compose_system_prompt` renders
working memory **last**. But OLS over 810 calls gives `latency ≈ 3.4 + 13.66·output_k` with input
tokens having **no measurable effect** (R²=0.53). So caching buys ~40% of input spend and ~0 wall
time; it belongs in a cost cluster, not a perf one. The **5-minute TTL beats the 1-hour** here
(median hub turn inter-arrival 58 s; the extra hits don't pay for doubling the write premium).

**M5 — root cause is not the post lane.** The tick is **85% reply sweep**; the post lane is 8.8 s
of API time per tick. "Nobody got a second turn" is an **initialisation artifact**:
`state.py:103` sets `last_selected: float = 0.0`, so a never-selected agent's staleness weight is
~1.79e9 against ~187 for a just-run agent — a 10⁷ ratio that makes the draw a shuffle-without-
replacement over all 63 before anyone can repeat.

---

## New findings, in neither document

1. **`#assessments-summary` has one member — the hub bot.** All 13 headlines **did** post
   (confirmed via read-only `conversations.history`: 14 messages, 13 `:mag:` lines, timestamps
   matching the "Assessment stored" log lines to the second, all D12-compliant, all with live
   permalinks). Nobody has joined, and `_post_assessment_summary` logs nothing on success and
   returns **silently** on the `is_connected` guard — so an empty audience is undetectable.
2. **The rate limiter meters booked turns, not requests** — 558 booked vs 812 recorded calls, a
   **1.46× global undercount** (~1.55× for the hub). "The limiter never bound" is true but partly
   because it is metering the wrong quantity.
3. **No result cache for prior art** — 109 searches resolve to 91 distinct term-sets, so 18
   redundant searches (16.5%) re-paid full enrichment; ~24% of USPTO budget, ~10× the permutation
   waste `README.md` flagged. And every one of the 10 429s landed on the **3rd POST of its own
   un-paced tier ladder** — the burst is ours, which fuses M9 and M13 into one root cause.
4. **`--fresh` does not clear agent working memory.** 56 `profiles/memory/*/public.md` files
   survive it, including klein's truncated directive. With the reconcile fix below, the message log
   is now fresh while memory stays stale — see "Residual" in `fresh-start-fix.md`.

---

## What must not be done

1. Do **not** implement `README.md` fix #5 (stop the inline verdict) — see §1.
2. Do **not** ship a bare `scout_hub` role gate on `_check_thread_outcome` — see §3.
3. Do **not** set `timeout=120`; use `300`, and know it disables the SDK's `max_tokens` guard.
4. Do **not** re-fit the rubric thresholds on the 29 stored v2 rows — censored, confounded, and
   the baseline is unreproducible. Fix C1 first, then re-fit on the 36 *emitted* verdicts.
5. Do **not** add `:double_vertical_bar:` to `_reply_closes_thread` — the model never emits it,
   and doing so would make the predicate true for reconciled Slack text.
6. Do **not** relax the capture gate without first moving the `#assessments-summary` post behind
   the final verdict — it currently fires **before** supersession, so provisional headlines would
   become public and unretractable, violating design D14.
7. Do **not** change `llm_call_logs.latency_ms` to the sum — `api_call_count` and the limiter's
   `call_times` are rebuilt from row counts. Add a new `wall_ms` column instead.

## Ranked, with measured payoff

| # | Change | Payoff | Cluster |
|---|---|---|---|
| 1 | Trust the sidecar: delete the `premature_sidecar` arm, let `_retire_superseded_verdict` do its job — **plus** move the summary post behind the final verdict | recovers every dropped verdict; markham 3.04 | correctness |
| 2 | Persist the raw sidecar on `assessment_drops` | makes every future gate policy non-destructive | correctness |
| 3 | Parallelise `consult_specialist` blocks within a round only | **2,344 s (21.2%)** | perf |
| 4 | `timeout=300` on the shared client; fix `email_inbound.py` | **600 s (5.4%)** | perf |
| 5 | Per-agent memory drain, overlapped with the reply sweep | up to **1,100 s (9.9%)** | perf |
| 6 | `stop_reason` in the return contract; each call site decides | stops durable damage (memory, panel notes) | correctness |
| 7 | Log the row before the empty-content early return (`llm.py:425`) | closes C5 for all four call sites | observability |
| 8 | `parse_opinion` uses a tolerant extractor | recovers 3 of 6, incl. the `blocking`→`caution` inversion | correctness |
| 9 | `_call_stat` records `block_types` | settles the `end_turn` mystery permanently, free | observability |
| 10 | Disclose truncation: return ODP's `count`, render "10 of N" | removes the false-exhaustive read | evidence quality |
| 11 | Fix `_salience` (all-digit token gets no bonus) + keep `SYMBOL-N` intact | fixes the backoff inversion | evidence quality |
| 12 | Per-run prior-art cache + pace the ladder + retry 429 | ~24% of USPTO budget, removes the self-inflicted 429s | evidence quality |
| 13 | Gate the specialist floor on `band` OR `recommendation`; stop exempting `route-to-incubation` | the funding recommendation stops being the unreviewed one | correctness |
| 14 | Prompt caching, 5-minute TTL, breakpoint before working memory | ~40% of input spend | **cost, not perf** |

## Needs a human decision, not a commit

- **Rewrite `CLAUDE.md:471-473`** to restore the narrow claim, and decide whether the visible
  verdict may paraphrase the PI's *unpublished* specifics at all. Only then can the real invariant
  be encoded.
- **Roster policy:** may a bot represent a deceased or departed PI? Should nested lab members
  (`camacho`, `gordy`) have independent bots alongside their parent labs?
- **Calibration:** adjudicate the 8 `route-to-incubation` verdicts by hand. Then either keep 3.4
  and rename `conditional` to match what it actually triggers, or re-anchor `advance` near the top
  of the emitted distribution (~3.2–3.3).
- **Panel notes in the PI's thread:** design D5 accepted this on the premise that no PI is in the
  workspace — measured true today (69 members per channel = 64 bots + 5 Blackbird staff, and
  agents structurally cannot read panel notes). It becomes live the day a real PI joins.
