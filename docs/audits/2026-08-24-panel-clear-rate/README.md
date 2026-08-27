# The specialist panel almost never clears anything (0.9%, floor 5%)

**Status:** measurement complete, cause NOT established. No code or prompt change
is proposed here — the decisive experiment (§7) has not been run.

**Raised by:** the engine's own alarm, at the end of run `ee419dd3` on 2026-08-24:

```
[specialists] 2 of 228 consults this run returned 'clear' (0.9%, floor 5%).
A panel that clears almost nothing cannot discriminate — check persona calibration.
```

That alarm is `clear_rate_warning` (`src/agent/specialists.py:394`, floor
`MIN_CLEAR_RATE = 0.05` at `:391`), emitted from
`SimulationEngine.stop()` (`src/agent/simulation.py:1178`). The comment directly
above the emit site (`:1174-1177`) already recorded the same phenomenon for run
`8b64a0e0` — so this is at least the second run to trip it, and the condition
predates the 2026-08-24 deploy.

> Note the alarm counts 228 (its in-process counter) while the DB holds 229 rows
> for the run. Both numbers are in this document deliberately; the one-row
> discrepancy is unexplained and is listed as an open question in §8.

---

## 1. How to reproduce every number in this document

Everything below comes from the production database and the run's saved log. No
step writes anything.

**Host access** (the repo is an sshfs mount of this host; run the commands ON the
host, not through the mount):

```bash
ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com
cd /home/ubuntu/blackbird-copi-science
```

**Database.** Use `docker exec` WITHOUT `-i` when the command is itself inside a
heredoc-fed script — `docker exec -i` consumes the remaining stdin and silently
swallows the rest of your script (this cost two empty-output runs while preparing
this report):

```bash
docker exec copi-blackbird-postgres-1 psql -U copi -d copi -t -A -F' | ' -c "<SQL>"
```

**Run identifiers.** `simulation_runs` accumulates across `--fresh` starts, so
always scope by run id rather than by "the latest":

| run id | started (UTC) | note |
|---|---|---|
| `ee419dd3-60ac-49e4-8de5-9ca47fb40514` | 2026-08-24 14:34:41 | the run this report is about; first run on rubric v2.1.0 |
| `6fb83501-adaf-4d2c-b8e8-018d1ced81b5` | 2026-08-22 16:35:27 | last run before the 2026-08-24 deploy |
| `8b64a0e0-1fa7-40c4-b9a2-f57a4e058fb0` | 2026-08-22 06:25:39 | the run the emit-site comment cites |

**Table shape** (verified 2026-08-24, so you can write your own queries without
introspecting first). `specialist_consults`: `id, simulation_run_id, agent_id,
subject_agent_id, thread_id, channel_name, domain, question, context_excerpt,
verdict_signal, confidence, concerns, questions_to_ask, raw_opinion, truncated,
created_at`. Note `question` is present — the hub's own consult question is
stored, which is what makes H3 in §6 directly checkable.

**Saved log** for `ee419dd3`: `logs/blackbird_run_1787589543.log` (2057 lines).
Logs rotate — `ls -t logs/blackbird_run_*.log | head -1` was this run at the time
of writing, but confirm by grepping for the run's start banner
(`Starting simulation: 62 agents, 120m max runtime`).

---

## 2. The measurement

### 2.1 Clear rate, per run (all runs that recorded consults)

```sql
SELECT r.id, r.started_at::date, count(*) consults,
       count(*) FILTER (WHERE sc.verdict_signal='clear') clear,
       round(100.0*count(*) FILTER (WHERE sc.verdict_signal='clear')/count(*),1) pct_clear,
       count(*) FILTER (WHERE sc.verdict_signal='blocking') blocking
FROM specialist_consults sc JOIN simulation_runs r ON sc.simulation_run_id=r.id
GROUP BY r.id, r.started_at ORDER BY r.started_at;
```

| run | date | consults | clear | % clear | blocking |
|---|---|---|---|---|---|
| `60c53424` | 2026-08-20 | 63 | 0 | 0.0% | 8 |
| `076e80b6` | 2026-08-21 | 217 | 0 | 0.0% | 37 |
| `8b64a0e0` | 2026-08-22 | 168 | 1 | 0.6% | 26 |
| `6fb83501` | 2026-08-22 | 62 | 0 | 0.0% | 14 |
| `ee419dd3` | 2026-08-24 | 229 | 2 | 0.9% | 24 |

**739 consults across five runs; 3 have ever cleared (0.41%).** The floor is 5%.

### 2.2 Clear rate by domain, all time

```sql
SELECT domain, count(*) n, count(*) FILTER (WHERE verdict_signal='clear') clear,
       round(100.0*count(*) FILTER (WHERE verdict_signal='clear')/count(*),1) pct
FROM specialist_consults GROUP BY domain ORDER BY pct, n DESC;
```

| domain | consults | clear | % |
|---|---|---|---|
| scientific | 185 | 0 | 0.0% |
| commercial | 121 | 0 | 0.0% |
| technologic | 99 | 0 | 0.0% |
| clinical | 96 | 0 | 0.0% |
| chemistry | 45 | 0 | 0.0% |
| legal | 33 | 0 | 0.0% |
| talent | 136 | 2 | 1.5% |
| budget | 24 | 1 | 4.2% |

**Six of the eight personas have never once cleared, over 579 combined consults.**
This is the strongest single finding: it is not one miscalibrated persona.

### 2.3 Signal distribution for `ee419dd3`

```sql
SELECT sc.domain, sc.verdict_signal, count(*)
FROM specialist_consults sc JOIN simulation_runs r ON sc.simulation_run_id=r.id
WHERE r.id='ee419dd3-60ac-49e4-8de5-9ca47fb40514'
GROUP BY 1,2 ORDER BY 1,2;
```

Totals: **229 consults = 203 caution (88.6%) + 24 blocking (10.5%) + 2 clear
(0.9%)**, across 8 domains, 1 truncated. Per domain: scientific 44c/10b,
commercial 36c/4b, technologic 35c/2b, talent 28c/1b/1clear, clinical 24c,
legal 17c/4b, budget 14c/1clear, chemistry 5c/3b.

---

## 3. Ruling out the obvious measurement artifacts

### 3.1 The cautions are real, not parse defaults

`parse_opinion` degrades an unreadable reply to `_DEFAULT_SIGNAL = "caution"`
(`src/agent/specialists.py:43`) — deliberately, since an unread specialist has
cleared nothing. That makes "caution" the bucket a measurement bug would hide in,
so it must be checked rather than assumed:

```sql
SELECT verdict_signal,
       count(*) FILTER (WHERE raw_opinion ILIKE '%"verdict_signal": "clear"%'
                          OR raw_opinion ILIKE '%"verdict_signal":"clear"%') raw_clear,
       count(*) FILTER (WHERE raw_opinion ILIKE '%"verdict_signal": "caution"%'
                          OR raw_opinion ILIKE '%"verdict_signal":"caution"%') raw_caution,
       count(*) FILTER (WHERE raw_opinion ILIKE '%"verdict_signal": "blocking"%'
                          OR raw_opinion ILIKE '%"verdict_signal":"blocking"%') raw_blocking,
       count(*) n
FROM specialist_consults sc JOIN simulation_runs r ON sc.simulation_run_id=r.id
WHERE r.id='ee419dd3-60ac-49e4-8de5-9ca47fb40514' GROUP BY 1;
```

Result: **229/229 agree** with their own raw JSON (24 blocking, 203 caution,
2 clear). Zero rows lack a `verdict_signal` field. The log carries exactly **one**
defaulted-parse warning for the whole run:

```bash
grep -i "DEFAULTED to" logs/blackbird_run_1787589543.log
# 16:26:42 [specialists] legal opinion did NOT parse; signal DEFAULTED to 'caution'
```

So at most 1 of 203 cautions is an artifact. **The panel genuinely emits caution
~89% of the time.**

### 3.2 The personas DO ask for `clear`

All eight persona files carry the instruction verbatim (line-wrapped, so grep for
the tail):

```bash
grep -rl "never clears anything" prompts/specialists/   # -> all 8 files
```

> `clear` — nothing in your domain stands in the way. Say this when it is true; a
> panel that never clears anything is noise.

The prompt is not missing the option, and does not hedge it. Whatever is
happening, it is not that the personas were never told.

### 3.3 It is not the 2026-08-24 prompt-set deploy

Runs on 2026-08-20 and 2026-08-21 (0.0%) predate rubric v2.1.0 entirely. The
condition is at least five runs old. The 2026-08-24 run is, if anything, the
*best* clear rate ever recorded (0.9%).

---

## 4. The mechanism question: does the clear rate move the score?

**No — not arithmetically.** `verdict_signal` is never an input to scoring. Every
reader outside the persistence layer is display-only:

```bash
grep -rn "verdict_signal" src/ | grep -v "src/agent/specialists.py" | grep -v "src/models/"
```

- `src/routers/admin.py:474,611`, `src/services/thread_panel.py:118-179`,
  `src/services/assessment_detail.py:237-762` — rendering.
- `src/agent/simulation.py:4275-4438` — writes the `specialist_consults` row.
- `src/agent/tools.py:722-738` — records the consult; `:775` returns
  `f"{spec.title} — signal: {opinion.verdict_signal}\n\n{opinion.raw}"` to the hub.

`weighted_score` is computed by `blackbird_rubric.weighted_score` purely from the
model's own 13 sidecar dimension scores. So the panel's signal reaches the score
only **textually** — the hub reads the full raw opinion (tools.py:775) in its
context window and then chooses its own numbers. That is a persuasion path, not a
formula, and its strength is unmeasured. Do not describe the clear rate as
"causing" the low scores without running §7.

Two consequences worth separating:

1. The floor alarm is about **panel discriminative power** (can a specialist ever
   say "fine"?), which is a real defect on its own terms regardless of scoring.
2. The all-pass outcome is a **separate** observation that may or may not share a
   cause.

---

## 5. The co-occurring all-pass outcome

```sql
SELECT r.started_at::date, oa.recommendation, count(*),
       round(avg(oa.weighted_score)::numeric,2) avg, min(oa.weighted_score), max(oa.weighted_score)
FROM opportunity_assessments oa JOIN simulation_runs r ON oa.simulation_run_id=r.id
GROUP BY 1,2 ORDER BY 1,2;
```

| date | recommendation | n | avg | min | max |
|---|---|---|---|---|---|
| 2026-08-17 | pass | 16 | 2.25 | 1.85 | 2.57 |
| 2026-08-17 | route-to-incubation | 6 | 2.61 | 2.43 | 2.77 |
| 2026-08-20 | pass | 3 | 2.22 | 2.07 | 2.40 |
| 2026-08-20 | route-to-incubation | 1 | 2.69 | 2.69 | 2.69 |
| 2026-08-21 | pass | 15 | 2.38 | 1.94 | 2.94 |
| 2026-08-21 | route-to-incubation | 1 | 3.29 | 3.29 | 3.29 |
| 2026-08-22 | pass | 21 | 2.36 | 2.00 | 2.96 |
| 2026-08-22 | route-to-incubation | 1 | 3.04 | 3.04 | 3.04 |
| 2026-08-24 | pass | 9 | 2.27 | 2.12 | 2.60 |

**`advance` and `conditional` have never been written, in any run, ever.** The
best score in the database is 3.29. Incubation banding is advance ≥3.4,
conditional ≥2.7 (`prompts/rubric/blackbird-rubric.toml:103-104`); investment is
4.0/3.0 (`:81-82`). Run `ee419dd3`'s top verdict (leung, 2.60) misses
`conditional` by **0.10**.

### 5.1 The scores are uniformly low, not dragged by missing keys

The rubric warns that an omitted key scores zero and drags the total. **That is
not what happened here** — all 9 verdicts supplied all 13 keys:

```sql
SELECT subject_agent_id, (SELECT count(*) FROM jsonb_object_keys(scores)) keys, weighted_score
FROM opportunity_assessments oa JOIN simulation_runs r ON oa.simulation_run_id=r.id
WHERE r.id='ee419dd3-60ac-49e4-8de5-9ca47fb40514' ORDER BY weighted_score DESC;
```
→ 13 keys for every row (leung 2.60, camacho 2.44, suez 2.35, egeblad/dang/davis
2.20, gill 2.18, huganir 2.16, pearce 2.12).

Per-dimension means for the run:

```sql
SELECT k dimension, count(*) present, round(avg(v::numeric),2) avg_score,
       count(*) FILTER (WHERE v::numeric = 0) zeros
FROM opportunity_assessments oa JOIN simulation_runs r ON oa.simulation_run_id=r.id,
     jsonb_each_text(oa.scores) s(k,v)
WHERE r.id='ee419dd3-60ac-49e4-8de5-9ca47fb40514' GROUP BY k ORDER BY avg_score;
```

| dimension | mean | zeros |
|---|---|---|
| exit_thesis | 1.11 | 0 |
| ip_fto | 1.22 | 0 |
| chemistry_dc_path | 1.44 | 2 |
| external_signals | 1.78 | 0 |
| platform | 2.00 | 0 |
| dev_regulatory_feasibility | 2.11 | 0 |
| differentiation | 2.22 | 0 |
| mechanism_validation | 2.22 | 0 |
| experimental_rigor | 2.22 | 0 |
| toxicity_selectivity | 2.33 | 1 |
| market_unmet_need | 2.56 | 0 |
| workplan_capital_efficiency | 2.67 | 0 |
| team | 2.89 | 0 |

**No dimension averages above 2.89 and nine of thirteen sit below 2.4.** The four
lowest (exit thesis, IP/FTO, chemistry-to-DC, external signals) are exactly the
dimensions an unformed academic idea has least evidence for — which is the
observation that makes §6 genuinely ambiguous.

---

## 6. Candidate explanations, and what would distinguish them

None of these is established. They are not mutually exclusive.

**H1 — Persona calibration.** The personas are written as risk-finders ("what you
own" is a list of failure modes) and each returns `concerns` and
`questions_to_ask`; a specialist that answers "clear" produces an empty-looking
response. The instruction to clear exists but competes with the whole rest of the
prompt. *Distinguishing evidence:* H1 predicts a strong idea still draws caution.

**H2 — The population really is early.** 62 academic labs pitching mostly
unpublished, unfiled, pre-commercial work; the incubation anchors were re-cut for
exactly this and the population still lands at 2.1–2.6. *Distinguishing evidence:*
H2 predicts a strong idea draws `clear` from most domains.

**H3 — The hub asks questions that cannot be cleared.** The hub composes each
consult question, and it asks about the weakest point by design ("close the
biggest gap"). A question of the form "is X adequately established?" on a thin
record has no clearing answer. *Distinguishing evidence:* inspect the questions —
`specialist_consults.question` — and check whether any were answerable "yes".

**H4 — Panel scope mismatch.** Six domains never clear, but `talent` and `budget`
— the two that have — are the two whose subject matter (does this team exist, does
this fit a band) is checkable from what a PI actually says. *Distinguishing
evidence:* per-domain clear rate on a strong synthetic case.

---

## 7. The decisive experiment (NOT run)

The measurement above cannot separate H1 from H2, because every consult in the
database was asked about the same weak population. A **positive control** settles
it:

1. Build 2-3 synthetic opportunities that should clear most domains: replicated
   data, filed IP, a named licensee, a precedented modality, an identified
   syndicate, a costed 12-month workplan.
2. Drive `consult_specialist` against each, all 8 domains, no engine changes.
   `tests/unit/test_specialists.py` shows the call shape; a scratch script that
   imports `src.agent.tools` and calls the consult path directly is enough, and it
   needs no database.
3. Record the signal distribution.

Interpretation: if a strong case still draws ~90% caution, H1 is confirmed and the
personas need rebalancing. If it clears broadly, H1 is refuted, the panel is
working, and the all-pass outcome belongs to the population (H2) — a business
finding, not a bug.

**Do not "fix" the clear rate by editing the personas before running this.**
Loosening eight personas to make an alarm stop is how a panel that discriminates
nothing becomes a panel that clears everything, and the second failure is worse:
it manufactures the verification the specialist floor exists to demand.

Related prior context worth reading first:
`docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md` (the same all-pass
question, one run earlier) and
`docs/specs/2026-08-20-rubric-v2-incubation-rebaseline-proposal.md` §7.3, which
schedules a band re-check after ≥20 v2-stamped verdicts — this run contributed the
first 9, so that checkpoint is now within reach and should be honored before
anyone re-cuts a threshold on the strength of this document.

---

## 8. Open questions

1. **The 228 vs 229 discrepancy.** The alarm's in-process counter
   (`_consult_signal_counts`, `simulation.py:405`) says 228; `specialist_consults`
   holds 229 rows for the run. One row is unaccounted for. Candidates: a consult
   persisted but not counted, a double-write, or a rehydrated row from
   `_seed_consults_from_db`. Not chased.
2. **Does the alarm's floor belong at 5%?** 0.05 is asserted at
   `specialists.py:391` with no derivation recorded. If the true population clear
   rate is genuinely ~1%, the floor is a permanent false alarm; if it is 30%, the
   floor is far too lenient. §7 is what would tell.
3. **Do blocking signals correlate with the dimensions they map to?**
   `SpecialistSpec.maps_to_dimensions` exists so a blocking signal "has somewhere
   to land". Whether a blocking chemistry consult actually coincides with a low
   `chemistry_dc_path` score is checkable by joining `specialist_consults` to
   `opportunity_assessments.scores` on `(simulation_run_id, subject_agent_id)` and
   would test whether the hub integrates the panel at all.
4. **Were the 24 blocking signals concentrated on particular interviews?** If a
   handful of subjects absorbed them, the "panel cannot discriminate" reading
   weakens — it discriminated, just downward.

---

## 9. Provenance

Measured 2026-08-24 by Claude Fable 5, from production `copi` (Postgres 15) on
ec2-3-21-33-147 and `logs/blackbird_run_1787589543.log`. Read-only throughout; no
row was written and no prompt was changed in service of this report. Every table
in §2-§5 is reproducible with the SQL as printed. Numbers were re-derived from the
database rather than taken from the run's own log except where the log is
explicitly quoted (the alarm text, the defaulted-parse warning).
