# Adversarial review of README.md (the panel-clear-rate report)

**Reviewed:** 2026-08-24, by Claude Fable 5, against the production `copi` database on
ec2-3-21-33-147, the saved log `logs/blackbird_run_1787589543.log`, and the working
tree at commit `3cdb7f5`. Every SQL query in the README was re-run verbatim; every
code citation was opened; the checks the README did not run were run.

**Verdict:** the measurement core (§2, §3, §5.1 tables, the alarm text, every code
cite for the alarm and the personas) is accurate and fully reproducible. Two claims
are wrong in ways that change what a reader would do next (findings 1 and 2), and two
of §8's four "open questions" were answerable in minutes with evidence the README had
already collected (findings 3 and 4).

---

## What was verified and holds (checked, not assumed)

* §2.1, §2.2, §2.3, §3.1, §5, §5.1 — all six queries reproduce **byte-for-byte**
  (739 consults / 3 clears / 0.41%; the domain table; 203/24/2; 229/229 raw
  agreement; the by-date assessment table; 13 keys per row; the dimension means).
* The INNER JOINs hide nothing: 0 consults and 0 assessments have a NULL or orphan
  `simulation_run_id` (checked with LEFT JOIN / IS NULL).
* Log claims exact: 2057 lines; the alarm line verbatim at 16:38:32 as the run's
  penultimate engine line; banner `62 agents, 120m max runtime` (a `--fresh` start,
  so no resume confounds the counter); `Screening rubric: version 2.1.0`; exactly
  one `DEFAULTED to 'caution'` warning (legal, 16:26:42).
* Code cites exact: `MIN_CLEAR_RATE = 0.05` at `specialists.py:391`,
  `clear_rate_warning` at `:394`, `_DEFAULT_SIGNAL = "caution"` at `:43`, emit site
  `simulation.py:1178` with the 8b64a0e0 comment at `:1174-1177`,
  `tools.py:722-738` and `:775`, band lines `blackbird-rubric.toml:103-104` (3.4/2.7)
  and `:81-82` (4.0/3.0).
* §3.2: all 8 persona files carry the clear-instruction, wording as quoted.
* §4's grep is complete: no reader of `verdict_signal` outside persistence is
  anything but display; `weighted_score` is `rubric_weighted_score(scores, stage)`
  (`simulation.py:3372`, `:4741`) — the "persuasion path, not a formula" framing is
  right.
* §1 run-start times match `simulation_runs` to the second. `ee419dd3` is indeed the
  first run to write rubric-2.1.0-stamped verdicts.
* `specialist_consults.question` is non-null on all 229 rows — H3 is checkable as
  claimed. The only domains ever to clear are talent (cai 08-22, weeraratna 08-24)
  and budget (markham 08-24), as §6 H4 assumes.
* `assessment_drops` contains no `advance`/`conditional` in any dropped
  `raw_verdict` either — the recommendation half of §5's claim is even stronger than
  the README checked.

---

## Finding 1 — §7's rebaseline-checkpoint claim is wrong; the re-check is already due

README §7: proposal §7.3 "schedules a band re-check after ≥20 v2-stamped verdicts —
**this run contributed the first 9**, so that checkpoint is now within reach."

The database says otherwise:

```sql
SELECT coalesce(rubric_version,'NULL') v, count(*), min(created_at)::date, max(created_at)::date
FROM opportunity_assessments GROUP BY 1 ORDER BY 3;
-- NULL  | 22 | 2026-08-17 | 2026-08-19
-- 1.0.0 |  4 | 2026-08-20 | 2026-08-20
-- 2.0.0 | 38 | 2026-08-21 | 2026-08-22
-- 2.1.0 |  9 | 2026-08-24 | 2026-08-24
```

**47 v2-stamped verdicts exist.** The first v2 verdicts landed 2026-08-21 (rubric
2.0.0, the version that introduced the incubation band lines under re-check), and the
≥20 threshold was crossed on 2026-08-22 (16 + 22 = 38). The v2.1.0 commit (`3cdb7f5`)
states in its own diff: "**no weight, threshold or scale changes**" — so 2.0.0 rows
are v2 data in exactly the sense §7.3 means (its text: "re-run this back-test on v2
data"). This run contributed verdicts 39–47, not 1–9.

Consequence: the README's advice ("that checkpoint … should be honored before anyone
re-cuts a threshold") is right but its tense is wrong — the checkpoint is not "within
reach", it is **passed at more than double the required n**, and the §7.3 back-test
re-run is due now. (Caveat for whoever runs it: v2.1.0 did reword some *anchors* —
ip_fto softened, the workplan grant range widened to $100K–$1M — so the re-baseline
should stamp-split 2.0.0 vs 2.1.0 before pooling, and per proposal §1 dedup to
latest-per-thread.)

## Finding 2 — "advance and conditional have never been written, in any run, ever" is false for the band column

README §5 bolds that sentence directly under a recommendation-grouped table, then
immediately reasons about *bands* ("Incubation banding is advance ≥3.4, conditional
≥2.7 … leung misses conditional by 0.10"). `band` and `recommendation` are separate
columns (band computed from the score, recommendation taken from the model), and:

```sql
SELECT rubric_version, band, recommendation, count(*), max(weighted_score)
FROM opportunity_assessments GROUP BY 1,2,3 ORDER BY 1,2;
-- 2.0.0 | conditional | route-to-incubation | 2 | 3.29
-- 2.0.0 | conditional | pass                | 4 | 2.96
-- (every other row bands 'pass')
```

**Six verdicts have banded `conditional`** — all rubric-2.0.0, 2026-08-21/22, scores
2.7–3.29. What is true: the *recommendation* values `advance` and `conditional`
(both legal — the sidecar contract at `prompts/roles/scout_hub/phase4-thread-reply.md:261`
is `"advance | conditional | pass | route-to-incubation"`) have never been emitted,
and the band `advance` has never been computed. But a reader of §5 as written takes
away "no verdict has ever reached even the conditional band", which is false.

Two knock-ons the README misses because of this:

1. The all-pass narrative is overstated: under the incubation banding it cites, 6 of
   38 v2.0.0 verdicts cleared the conditional line.
2. The interesting delta is the opposite of the one §3.3 celebrates: run `ee419dd3`
   (2.1.0) banded **0 of 9** conditional where the 2.0.0 runs banded 6 of 38 — on a
   rubric release whose commit says no threshold changed but whose anchor rewordings
   could plausibly move scores. Nine verdicts is far too few to call that a
   regression, but it is a reason the §7.3 re-check (finding 1) should stamp-split.

## Finding 3 — Open question §8.1 (228 vs 229) is solved; the answer was one log line below the line the README quotes

The unaccounted row is the **truncated legal consult** — the only `truncated IS TRUE`
row in the run (and in the whole table):

```sql
SELECT domain, verdict_signal, created_at FROM specialist_consults
WHERE simulation_run_id='ee419dd3-60ac-49e4-8de5-9ca47fb40514' AND truncated IS TRUE;
-- legal | caution | 2026-08-24 16:26:42.016194+00
```

Mechanism, deliberate and documented in the code: `src/agent/tools.py:714-722` — a
truncated consult fires `on_consult_record` (durable row, `truncated=True`) but the
tally callback `on_consult` (→ `_note_consult` → `_consult_signal_counts`,
`simulation.py:2022`, `:4697`) sits in the `elif` branch and is skipped. "Record it;
just do not count it" is the module's own comment. The log states it outright at line
**1958**:

```
16:26:42,014 ERROR src.agent.tools: [specialists] legal consult for blackbird was cut
off mid-reply (stop_reason='refusal') — recorded, but NOT counted as consulted
```

— the line directly after log line 1957, the `DEFAULTED to 'caution'` warning §3.1
quotes. Same event: the truncation is *why* the parse defaulted. So: DB 229 = alarm
228 + 1 uncounted truncated row. The README's candidate 1 ("a consult persisted but
not counted") was correct; it is **by design, not a bug**, and the alarm's
denominator is the right one (it counts only consults credited to the floor).

Bonus corollary for §3.1: the one artifact-caution row is exactly the row the alarm
never counted, so the alarm's own 228-consult sample contained **zero** defaulted
signals — the 0.9% is even cleaner than the README argues.

Nit while here: the truncated row's raw text happens to *begin* with
`{"verdict_signal": "caution"` before being cut off, so §3.1's "229/229 agree with
their own raw JSON" holds for it only coincidentally — its stored signal came from
the parse default, not from reading that JSON. True as stated, but the agreement
check cannot distinguish a parsed caution from a defaulted one whose prefix survived.

## Finding 4 — Open question §8.4 (blocking concentration) answers "not concentrated" in one query

```sql
SELECT subject_agent_id, count(*) FROM specialist_consults
WHERE simulation_run_id='ee419dd3-60ac-49e4-8de5-9ca47fb40514'
  AND verdict_signal='blocking' GROUP BY 1 ORDER BY 2 DESC;
```

The 24 blocking signals are spread across **14 of the 17 subjects consulted**
(hamacherbrady 4; suez, camacho 3; markham, davis, gill 2; eight others 1). No small
set of subjects absorbed them, so the escape hatch §8.4 was probing — "it
discriminated, just downward" onto a few bad ideas — is closed: the panel blocked a
little bit of almost everyone, which is the non-discrimination reading.

---

## Minor accuracy notes

* **§5.1's dimension means are means of *stored* values, not *as-scored* values.**
  `weighted_score` clamps every numeric score to the rubric scale **[1, 5]**
  (`blackbird_rubric.py:480ff`, `_MIN_SCORE = scale_min = 1`, toml `[scale]`), so the
  three out-of-scale `0` entries (huganir `chemistry_dc_path` and
  `toxicity_selectivity`, egeblad `chemistry_dc_path`) counted as **1** in the actual
  scores. As-scored means: chemistry_dc_path ≈ 1.67 (not 1.44), toxicity_selectivity
  ≈ 2.44 (not 2.33). Two sub-points: (a) the model emitting 0 on a declared 1–5 scale
  is a small unremarked contract violation, 3 of 117 cells; (b) the table's `zeros`
  column invites the wrong inference — a present-0 drags *less* than an omitted key
  (clamped to 1 vs scored 0), so the zeros do not undercut the section's "not dragged
  by missing keys" conclusion, they slightly reinforce it.
* **§1's column list has the last two columns swapped**: actual order ends
  `…, raw_opinion, created_at, truncated`. Same set; matters only for positional
  SQL, but "so you can write your own queries without introspecting first" is the
  section's stated purpose.
* §1's quoted start banner is a prefix — the full line ends
  `, 0 budget/agent (fresh start)`. The `(fresh start)` is worth keeping: it rules
  out a mid-run restart as a 228/229 explanation, which the README's §8.1 candidate
  list implicitly worried about (`_seed_consults_from_db` does not touch the tally
  in any case — it calls `_record_consult`, not `_note_consult`).

## What this review did not re-litigate

The README's judgment calls stand up: refusing to name a cause without the §7
positive control is right; the warning against loosening personas to silence the
alarm is right; §4's separation of "panel can't discriminate" from "everything
passes" is right (and finding 2's six conditional bands sharpen it — the two
phenomena already diverge in the data). H1–H4 remain the live hypotheses; nothing
found here favors one over another, though finding 4 removes §8.4's escape hatch and
the H3 inputs (`question` column) are confirmed present for all 229 rows.

Read-only throughout; no production row was written and README.md was not modified.
