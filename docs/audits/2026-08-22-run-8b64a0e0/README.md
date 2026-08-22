# Adversarial audit — production run `8b64a0e0-1fa7-40c4-b9a2-f57a4e058fb0`

> ## ⚠️ SUPERSEDED IN PART — read `rca-and-corrections.md` first.
>
> Five independent falsification passes were run against this document. **C3 is refuted**
> (the inline verdict is mandated by four prompt files; the defect is a CLAUDE.md regression
> introduced by `5d67e92`), **H1's count and provenance are wrong** (7 not 8; ⏸️ is an explicit
> `pi_lab` instruction), and **one of C1's evidence bullets is a measurement artifact** (Slack
> rewrites `⏸️` to `:double_vertical_bar:`, so the "25 verdict messages lack it" figure is
> wrong — the true number is 8). The "two turns lost with no DB trace" finding is also revised:
> both turns **recovered on retry** and cost zero verdicts.
>
> Three of the "If you fix five things" recommendations below are actively harmful as written
> (#2, #4 and #5). `rca-and-corrections.md` has the corrected statements, the root causes, the
> naive-fix traps, and the measured payoffs.

**Run:** 2026-08-22 06:25:39Z → 09:29:59Z (184 min, hit its `--max-runtime 180` timer).
63 active agents, `--fresh`, exit 0, no OOM, no restarts.
**Code:** images built 06:19:18Z from HEAD `accd634`; includes the E1 empty-reply fix
(`2ef6361`) and all 13 tasks of the 2026-08-21 perf remediation. Migration head `0034`.
**Rubric:** 2.0.0 / `e3ef75f84c48`.
**Log:** `logs/blackbird_run_1787391032.log` (3211 lines).

Everything below is CONFIRMED by query, log, code read, or live API probe unless
labelled inferred. Nothing was fixed; nothing in the database was modified.

---

## Headline

The screening pipeline **stored 13 verdicts, all declines, and discarded the single
best opportunity it found.** The discarded verdict — markham, recovered intact from
`llm_call_logs` — scores **3.04**, higher than any stored verdict in the run, bands
`conditional`, and is the only `route-to-incubation` recommendation the run produced.

The mechanism is a structural bias, not bad luck. See C1/C2.

---

## Critical

### C1 — The capture gate is structurally biased against positive verdicts

`_reply_closes_thread` (`src/agent/simulation.py:6458-6478`) recognises a terminal
reply **only** by the literal `⏸️` / `:pause_button:` marker. A sidecar is stored if
its reply concludes (ordinal 12) **or** closes the thread.

- ⏸️ is the *decline* convention. A decline therefore has **two** doors.
- A positive verdict carries no ⏸️, so ordinal 12 is its **only** door.
- **All 13 stored assessments arrived on a reply containing ⏸️** (verified by joining
  `opportunity_assessments.slack_ts` to `agent_messages`). Zero were admitted via the
  ordinal-12 door.
- Across the run: 833 hub messages, only 28 contain ⏸️; **25 verdict-bearing messages
  lack it.**

This partially reframes the earlier "all-pass = calibration" RCA: a share of it is a
**capture artifact**, not a scoring artifact.

### C2 — Two completed verdicts were discarded at ordinal 10 and the run then ended

`markham` (09:23:42) and `weeraratna` (09:29:47) were refused `premature_sidecar` —
"a later turn is still owed the verdict". The run's main loop exited at **09:29:59**.
No later turn existed.

Both replies are explicit, complete conclusions that simply omitted the marker:

> Closing this out with a verdict rather than another question… **Verdict: pass for
> now, at Blackbird's incubation bar.**

**Recoverable.** 15 raw `<assessment_json>` sidecars sit in `llm_call_logs.response_text`
— the 13 stored plus both refused. Both refused blocks parse cleanly (13 scores, full
gating, 12 red flags each):

| subject | recommendation | weighted_score (incubation) | band |
|---|---|---|---|
| markham | **route-to-incubation** | **3.04** | conditional |
| weeraratna | pass | 2.22 | pass |

A backfill of both rows is possible from data already in the database.

### C3 — The confidential verdict is delivered to the PI, in public, in prose

CLAUDE.md: *"the full verdict … never appears on anything a PI or another lab sees."*

The `<assessment_json>` strip is airtight (zero leaks of `weighted_score`, `band`, or
any rubric dimension key anywhere in the corpus). **The model simply restates the
verdict in prose in the same visible reply.** 18 hub messages this run carry gating +
red flags + recommendation together, all `visibility='public'`, in topic channels every
lab bot is joined to. Field leak rates against the 13 stored rows: `recommendation`
11/13, `confidence` 10/13, red-flag substance 13/13.

The inversion is the damaging part: **Sinnis and Markham each received a full verdict
in Slack while `opportunity_assessments` has no row for them.** The PI gets the
confidential assessment; Blackbird staff get nothing.

### C4 — An active bot is impersonating a scientist who died in 2021

`agents`: `shastri` / `ShastriBot` / `status='active'` → `users.name = 'Nilabh Shastri'`,
ORCID `0000-0002-8060-3025`. The bot posted, in `#structural-biology`:

> **Prof. Shastri died in 2021**, and I cannot name the responsible PI at Hopkins
> today… I'm not going to guess at a signer for a scope of work.

The specialist panel surfaced the same fact independently. This is a roster-hygiene
failure, not a model failure. **Recommend a roster audit against ORCID/institutional
status before any real PI sees this workspace.**

### C5 — 26 billed specialist consults left zero trace in the database

`generate_agent_response` returns `""` at `src/services/llm.py:425` *before* the
`_call_log_callback` at `:503`. So a refused consult writes **no** `llm_call_logs` row,
and `has_usable_content("")` then suppresses the `specialist_consults` row too — but
`on_api_call` already fired, so it is billed.

Reconciles exactly three ways: 838 httpx POSTs − 812 `call_stats` requests = 26;
`simulation_runs.total_api_calls` 558 − `llm_call_logs` 532 = 26; and 26
`returned no usable content` log events. Per domain: chemistry 10 (40% refusal rate),
scientific 9, technologic 4, talent 3.

**A refused consult is indistinguishable from one never attempted**, so
`panel_incomplete`/`missing_domains` blames the hub for the model's refusals. The only
record is the container log, which rotates at 10 files.

### C6 — Two 600 s SDK read timeouts froze the whole simulation for 20.9 minutes

`src/services/llm.py:133` calls `messages.create` with no `timeout=`. The deployed
`anthropic 1.0.0` defaults to a flat 600 s read timeout against a workload whose p90
request latency is 34 s. Two requests got no response at all; the arithmetic closes
exactly (600.0 + 0.49 retry + 16.6 = 617.1 s; 600.0 + 0.41 + 38.4 = 638.5 s).

Cost: **1,200 s of dead air = 10.8% of the run.** Because achieved concurrency is 1.57,
a stalled request stalls nearly everything — the two slowest turns in the run
(`gill` 811 s, `weeraratna` 903 s) are each almost entirely one of these stalls, and
`gill`'s turn made zero LLM calls of its own. A `timeout=120` with the SDK's existing
2 retries converts 1,200 s into ~240 s.

---

## High

### H1 — A PI's own bot can kill the PI's pitch, and did, 8 times

`_check_thread_outcome` (`simulation.py:2093`) runs `_reply_closes_thread()` on
**whoever just replied**, with no role gate. ⏸️ is a *hub* instruction from
`thread_guidance.py`, but PI bots read the hub's closes in thread history and copied
it. 16 PI-bot messages carried a pause marker; **8 closed their own interview**
(chute ×2, casadevall, shastri, mugnier, dang, huganir, epearce). None produced an
assessment.

They are not agreeing — they are withdrawing their PI's work in public:

> [chute] ⏸️ I'm withdrawing this one…
> [casadevall] ⏸️ You've found the load-bearing gap and I don't have an answer that survives it.

One condition (`agent.role == "scout_hub"`) fixes it.

### H2 — Prior-art results are truncated to 10, sorted newest-first, and `count` is discarded

`_search_titles` (`patents.py:185-201`) requests `limit: 10`, `sort: filingDate desc`,
and throws away the API's `count`. Nothing tells the model the list was cut, while
`_scope_note` affirmatively tells it *"an empty result at this breadth is the strongest
negative this tool can give you."*

**49 of 109 broadened searches (45%) returned exactly 10 — truncated with no indication.**
Live counts for terms this run actually searched on: `resistance` 27,906 · `exposure`
14,639 · `screening` 14,380 · `COVID` 2,070. This reached scientists — two PIs were
told a long-COVID search *"returned … nothing on computable phenotypes"*, a negative
drawn from the 10 most-recently-filed of 2,070.

`filingDate desc` is also backwards for prior art: of 624 hit rows shown to the hub,
only 12% are "Patented Case", while 18% are expired provisionals that were never
published and are **not prior art at all**.

### H3 — Term backoff lands on generic words and mangled identifier fragments

`_salience` (`patents.py:76-87`) ranks on uppercase-ness, digits and length — typography,
not specificity — and `_GENERIC` (60 words) misses `screening`, `detection`, `resistance`,
`exposure`, `microbiota`, `vaccine`, `PCR`. Worse, `_Q_SANITISE` shreds hyphenated
symbols and then ranks the fragment above the real one:

- `MIP-3alpha fusion vaccine` → `['3alpha']`
- `LOX-1 myeloid-derived suppressor cells` → `['1','LOX']` — a title search for "1"
- `L-DOPA mosquito control` → `['DOPA']` — the subject discarded

≈25 of 125 searches (20%) produced a hit set built entirely from non-distinctive terms;
**every one returned exactly 10 hits.** 37% of all prior-art text injected into Phase-4
prompts came from those payloads. The model discarded 18 of 22 on its own — the control
is model judgement, with no code-level enforcement.

### H4 — Refusal-truncated output is consumed as if complete

21 refusal stop_reasons across 812 requests. 12 lost all text; **8 kept partial text and
were used**: 4 hub replies posted to Slack mid-word, 1 `memory` write storing a truncated
directive, 3 consults returning truncated JSON. Nothing checks `stop_reason` other than
`== "max_tokens"` (`llm.py:433`).

> …so any composition-of-matter position on a CDR-mutated derivative s

### H5 — A parse failure is laundered into a valid cautious opinion

`parse_opinion` (`specialists.py:231-235`) returns `_DEFAULT_SIGNAL="caution"`,
`confidence="low"`, empty concerns/questions on any JSON failure — and that satisfies the
panel-completeness floor. 6 of 168 consults are affected (JSON+trailing prose, fenced
JSON+trailing prose, and the 3 truncated refusals). **One was a `blocking` opinion
published to Slack as `⚠️ caution`** (chute/scientific). `epearce` and `klein` have stored
assessments resting on such panels with `panel_incomplete=false`.

### H6 — Candid internal critique is published into the PI's own thread

`panel_notes_in_thread` defaults `True` (`src/config.py:371`) and prod `.env` does not
override it. The design doc explicitly recommended against this. The *opinion* is
correctly withheld — but the hub's **question** is published verbatim, and the question
carries the critique:

> 🧪 Panel · talent — asked: "…is this a technology or a person, and does that change
> how Blackbird staff should record the close?"

168 panel notes vs 98 PI messages this run.

### H7 — The evaluative apparatus has no dynamic range

- Consults: **141 caution / 26 blocking / 1 clear** (0.6% clear). The built-in alarm
  (`simulation.py:955-962`) is a zero-test, not a rate test, so one outlier silenced it.
- Scores: this run min 2.00, mean 2.36, max 2.96, **sd 0.252** on a 1–5 scale.
- `external_signals` = 1.00 for all 13 (min 1, max 1); `fto_achievable` = `unconfirmed`
  for all 13, because `search_prior_art` is title-only and the hub correctly refuses to
  call that FTO. **One of three gating criteria can never be met by the tooling that exists.**

### H8 — `advance` is empirically unreachable

Incubation thresholds are `advance_min=3.4`, `conditional_min=2.7`. Across all **29
v2.0.0-stamped verdicts**: min 1.94, mean 2.40, **max 3.29** → 0 advance, 4 conditional,
25 pass. Nothing has ever come within 0.11 of the bar. The rubric's own §7.3 re-check
trigger is ≥20 v2 verdicts — **now met (29)**.

Also: 3 of the 4 `conditional` bands carry `recommendation='pass'`. Band and
recommendation disagree in 75% of non-`pass` bands.

---

## Medium

- **M1 — `no_proposal` is the only outcome the code can produce.** Only two `_close_thread`
  call sites exist: `"timeout"` and `"no_proposal"`. Across the entire database, all runs:
  106 `no_proposal`, 9 `timeout`, **0 `proposal`, ever.** "0 proposals" is a dead metric
  mislabelled as a funnel outcome, and it is actively wrong for the 13 threads that did
  produce a verdict.
- **M2 — `--fresh` is not fresh.** It wiped the tables, then Slack reconcile re-imported
  914 messages / 86 threads. **916 of the 1,354 `agent_messages` rows attributed to this
  run (68%) were posted before it started** — oldest 2026-08-14. Per-run analytics inflate
  ~3.1×. Three restored hub threads refused twice immediately and were abandoned
  (139,257 input tokens, zero output).
- **M3 — Memory drain relocated, not removed.** T1 correctly took the two
  `_update_agent_memory` calls out of `acquire_all`, but `_drain_memory_events` runs on
  the main loop *after* the reply-lane gather barrier, one event at a time. 72 calls,
  1,100 s = 9.9% of the run at **0.0% overlap** with any other call.
- **M4 — Tool blocks in a round run strictly sequentially** (`llm.py:755`, no `gather`).
  Consults are 48.9% of all API-seconds at mean concurrency 1.16; a 6-consult round takes
  ~165 s instead of ~50 s.
- **M5 — Post lane gives one agent one turn per ~187 s tick.** 59 turns, 59 distinct
  agents, nobody twice; 4 agents never ran; 12 made zero LLM calls. One full round needs
  63 × 187 s ≈ 196 min > the 180-min budget.
- **M6 — No prompt caching anywhere.** Zero `cache_control` in `src/`. 12.76 M input
  tokens across 812 requests; **4.66 M (36.5%) is re-sent system prompt**, and the hub's
  `thread_reply` system prompt (~7,580 tokens) has only 1–2 distinct variants.
- **M7 — `commercial` and `budget` can never be required by the floor**
  (`required_domains_for`, `specialists.py:382-415`). `commercial` owns `differentiation`,
  the highest-weighted dimension (16/100 incubation). `legal` is required only when
  `fto_achievable == 'met'` — i.e. never, given H7.
- **M8 — 5 of 13 rubric dimensions have no owning specialist** (25% of incubation weight),
  including `mechanism_validation` and `toxicity_selectivity`, the two most-cited rejection
  reasons in the stakeholder document that justified the panel.
- **M9 — USPTO 429 aborts the whole search with no retry**, asymmetric with
  `pubmed._ncbi_get` which retries 3× with backoff. The 429s are transient, not our burst —
  one succeeded **41 ms later** on the same key. 10 of 125 searches lost; 1 of 10 disclosed
  to the PI.
- **M10 — NCBI api_key in 601 plaintext log lines** (`pubmed.py:124-125` puts it in
  `params`; httpx logs the full URL). Not committed (`.env` and `logs/*.log` are ignored),
  blast radius small (rate-limit token), but the host is shared with an unrelated tenant.
  Rotate and move it to the header NCBI also accepts.
- **M11 — `logs/*.sql` and `logs/*.tgz` are untracked and NOT ignored.** `.gitignore`
  covers only `logs/*.json` and `logs/*.log`, so `git add -A` would commit
  `logs/opportunity_assessments_backup_1787265062.sql`.
- **M12 — PubMed transport failure becomes a nonexistence claim.** A truncated body raises
  `RemoteProtocolError`, which is not retried; `fetch_pubmed_records` swallows it, and the
  model is told *"No PubMed record found for 41130592"* — for a paper the hub had cited by
  DOI four seconds earlier.
- **M13 — 17 provably-wasted USPTO POSTs** from permutation tiers (`_tiers` dedupes with a
  *list* comparison; AND is commutative — confirmed live, both orders return count 6179).
  Separately, 57% of the USPTO budget (493/865) is unconditional full-text enrichment,
  fired even on searches already collapsed to a generic word.
- **M14 — 194 wasted NCBI round trips**: `IDCONV_BASE` omits a trailing slash, so every
  idconv call 301-redirects. The pacer counts one, so the real rate against NCBI is 2×
  what it believes.

---

## Low / observability

- **`llm_call_logs.latency_ms` is the LAST call's latency, not the turn's** — verified
  equal to last-call in 532/532 rows, equal to the sum in only 334. Stored total 215.9 min
  vs true **289.4 min** (25% undercount).
- **Two turns lost with no DB trace at all.** 08:24:38 and 08:26:36: `stop_reason=end_turn`
  (not refusal), 86,725 / 81,169 input tokens, 3 API calls each, ~1,400 output tokens,
  `response_text=''`. `_all_text` (`llm.py:222-239`) returns `""` when no `text` block
  exists. A single empty reply writes no drop row (drops need 2 consecutive), so the only
  forensics are in the ephemeral container log.
- **`docker stop -t 420` is below the observed tail.** CLAUDE.md's grace period is sized on
  "a 16000-token call runs 4–5 min", but observed single uninterruptible calls hit **638.5 s**
  and **617.1 s** — both `end_turn` with only 2,231 / 959 output tokens. `max_tokens=16000`
  is ~2× the observed ceiling (88% of calls returned under 2,000 tokens; max ever 8,566).
- **`simulation_runs` counters match nothing**: `total_api_calls` 558 vs 532 log rows vs
  812 real requests; `total_messages` 271 vs 438 in-run message rows.
- **Panel-note emoji split across two write paths**: 168 rows render `🧪 ⚠️`, 217 render
  `:test_tube: :warning:` — raw-content admin surfaces show unresolved colon-codes.
- **`sender_name` inconsistent for the hub**: `U0BN9BQE4LD` on 534 rows vs `BlackbirdBot`
  on 299.
- **Assessments list page conflates `not_owed` with `verified`**
  (`templates/admin/_assessments_body.html:295` renders the badge only if
  `panel_incomplete`).
- **27% of consults are redundant** (168 consults over 123 distinct thread/domain pairs);
  **9 pairs contradicted themselves** (e.g. `hardwick/scientific` caution→blocking) with no
  tie-break in the schema or the floor.

---

## Verified clean

These were probed adversarially and held:

- `weighted_score` recomputation: all 13 match the **incubation** weight set exactly;
  banding matches the incubation thresholds. The arithmetic is sound — the calibration
  problem is in the scores, not the math.
- 13/13 assessment `slack_ts` resolve to a stored hub message; zero dangling.
- One-assessment-per-subject invariant holds; no duplicates; all attributed to interview
  channels, never a summary channel.
- Rubric stamping consistent (2.0.0 / `e3ef75f84c48`); migration head `0034` = `alembic heads`.
- Correct deploy ordering: images built 06:19Z, run started 06:25Z, HEAD contains the E1 fix.
- The E1 fix works as designed — 4 `empty_reply` drop rows where the old code logged and
  moved on.
- **Zero persona breaks** (`as an AI`, `Claude`, `Anthropic`, `system prompt` — all zero).
  **Zero raw JSON/XML in Slack.** Zero PII, zero profanity, zero disparagement of a named
  PI as a person.
- **No hallucination in the PI personas.** No fake DOIs, no fake collaborators, no
  fabricated data; they refuse to invent numbers and flag encumbrances unprompted.
- Red flags are specific, sourced and falsifiable; no rationale contradicts its
  recommendation.
- `#assessments-summary` content is D12-compliant by code read (five fields, nothing else).
  It writes no `agent_messages` row by design, so the DB cannot confirm the posts fired;
  the absence of any `Failed to post` log line is the only (inferred) evidence.
- The 404s in the USPTO `404,404,404,200` ladder are **not** a wrong URL — 404 is ODP's
  "zero matches" response (confirmed live). Do not "fix" them.
- No OOM, no restarts, no resource limits hit; the co-tenant `copi-python` stack is not
  competing (9% CPU); external APIs are 3.4% of wall, not a bottleneck; the rate limiter
  never bound.

---

## If you fix five things

1. **Accept a sidecar on any reply that concludes in substance, not just one carrying ⏸️**,
   and drain owed verdicts at shutdown. Then backfill markham + weeraratna from
   `llm_call_logs` (C1, C2).
2. **Gate `_check_thread_outcome` on `agent.role == "scout_hub"`** — one condition, recovers
   8 destroyed interviews per run (H1).
3. **Audit the roster against ORCID/institutional status today.** `ShastriBot` is active (C4).
4. **Pass `timeout=120` to `messages.create`** — buys back 10.8% of wall for one argument (C6).
5. **Stop the model restating the verdict in the visible reply** — the sidecar strip is
   perfect and is doing nothing, because the verdict is written twice (C3).
