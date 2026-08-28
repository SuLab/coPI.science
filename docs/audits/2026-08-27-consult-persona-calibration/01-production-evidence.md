# What production actually shows

All numbers measured 2026-08-28 from production `copi` (Postgres 15) on
ec2-3-21-33-147, read-only. `specialist_consults` held **1,192 rows across 9
runs** at the time of measurement.

## 1. The distribution, and its history

```sql
SELECT verdict_signal, COUNT(*), ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (),2) pct
FROM specialist_consults GROUP BY 1;
```

| | all-time (n=1192) | run 61ccad6d (n=163) |
|---|---|---|
| caution | 1022 (**85.74%**) | 143 (87.73%) |
| blocking | 161 (13.51%) | 16 (9.82%) |
| clear | **9 (0.76%)** | **4 (2.45%)** |

Per run, oldest first: 60c53424 0/63 · 076e80b6 0/217 · 8b64a0e0 1/168 (0.60%) ·
6fb83501 0/62 · ee419dd3 2/229 (0.87%) · 89442d15 1/153 (0.65%) · aa8359b9
1/135 (0.74%) · **61ccad6d 4/163 (2.45%)**.

Two facts that frame everything below:

- **This is a long-standing condition, not a regression.** Four runs have zero
  clears; no run has ever exceeded 2.45%. `git log -- prompts/specialists/` shows
  the eight persona files created 2026-08-07 and touched exactly once since
  (`3cdb7f5`, 2026-08-24), with the verdict-signal definitions unchanged
  throughout. The rubric v3.x work did not cause this.
- **The run that fired the alarm is the best-performing run ever recorded** on the
  metric the alarm measures — 2.45% against a 0.60–0.87% ceiling everywhere else.

## 2. The alarm's own instruction is already in the prompt, and it did not work

All eight personas carry this verbatim:

> **clear** — nothing in your domain stands in the way. Say this when it is true;
> a panel that never clears anything is noise.

The exhortation the alarm is asking for has been in front of the model on every
one of 1,192 consults and produced 0.76%. **Any remediation that amounts to
saying it louder has already been tested at n=1192 and failed.** This is the
single most important constraint on the fix, and it is corroborated by the
literature: prompting moves a judge's *criterion*, not its *resolution*
(arXiv:2606.15610).

## 3. The `caution` mass is real, not a parsing artifact

`parse_opinion` defaults an unreadable reply to `caution`/`low`
(`src/agent/specialists.py:330`), so the 85.7% had to be tested for
contamination. Regex against the model's own stored reply:

| stored | n | raw says caution | raw says blocking | raw says clear |
|---|---|---|---|---|
| blocking | 161 | 0 | 161 | 0 |
| clear | 9 | 0 | 0 | 9 |
| caution | 1022 | **1020** | 2 | 0 |

The exact default fingerprint — `caution` + `low` + empty `concerns` + empty
`questions_to_ask` — matches **15 rows all-time (1.26%)**, of which 6 are the
already-documented laundered consults from run 8b64a0e0. Excluding all 15 moves
the all-time clear rate from 0.76% to 0.76%, and run 61ccad6d's from 2.47% to
2.48%.

**Refuted: parsing accounts for none of the distribution.** Two genuine
blocking→caution downgrades exist all-time (`a2e4a7fa`, `2887d7b0`, both
scientific, both cut off before the JSON closed); neither is in run 61ccad6d.

## 4. `clear` does not mean "no concerns" — and this is the real defect

Mean `concerns` entries per stored opinion, all-time:

| signal | n | mean concerns | zero-concern rows |
|---|---|---|---|
| blocking | 161 | 7.81 | 0 |
| caution | 1022 | 7.16 | 15 (all parse defaults) |
| clear | **9** | **6.11** | **0** |

**Not one of the nine `clear` opinions in the history of this system has an empty
concerns list.** They carry 4, 5, 5, 6, 6, 6, 7, 7 and 9 concerns. The
nine-concern one (`talent`, yarchoan, run 61ccad6d) includes *"Succession risk is
high as described"* and *"Conflicts of interest are undeclared in what has been
shown"* — under a label that renders in Slack as **✅ clear**
(`_PANEL_NOTE_SIGNAL_EMOJI`, `specialists.py:150`).

The whole-panel spread is 6.11 → 7.81 concerns across all three labels, on an
observed range of 0–13. Per domain it inverts: `legal` averages 8.94 on
`blocking` and **9.01** on `caution`; `budget` averages 6.40 on `caution` and
**6.20** on `clear`.

So the label and the content are close to decoupled. A staff member reading ✅ is
being told something the opinion body does not say.

## 5. The mechanism: the output schema cannot express a positive finding

The response contract is four fields — `verdict_signal`, `concerns`,
`questions_to_ask`, `confidence`. **Two carry content and both are
negative-valence.** There is no field for what the record establishes. The
personas then instruct: *"state what is and is not established, and what you
would need to see"*, and *"`questions_to_ask` is the most valuable field you
produce."*

The specialists visibly work around this. From the nine `clear` opinions, each of
these is a **`concerns` array entry**:

- *"The behavior observed is epistemic honesty, which is a genuine positive signal
  on a PI's reliability as a reporting partner — but it is evidence about candor,
  not about execution capacity."* (talent, cai)
- *"The candour is real but it is candour about someone else's asset class…"*
  (talent, weeraratna)
- *"Candour is well-evidenced (two unprompted self-corrections, pre-emptive naming
  of the chart-access ceiling) — this is the strongest positive signal in my
  domain and I have no counter-evidence against it, but note that candour is not
  the same as delivery capacity."* (talent, yarchoan)

**The specialist has a positive finding, the schema offers only `concerns`, so the
positive is filed as a concern with a hedge appended.** That inflates the
concern count on exactly the opinions where the label is most favourable, which
is why `clear` opinions still average 6.11 concerns.

### 5a. Concern volume sits at or above the measured LLM over-production norm

The panel averages **7.16–7.81 `concerns` entries per opinion** (7.35–7.94 in run
61ccad6d, max 13). For scale, MetaCritique (Sun et al., ACL 2024 Findings,
arXiv:2401.04518) measured critique verbosity directly: **human critiques average
3.31 atomic information units, LLM critiques 8.10** — a 2.4× over-production — with
AIU-level **precision 87.61% for humans versus 71.85% for LLMs**.

**Unit caveat, and it matters:** a `concerns` *entry* here is typically a multi-clause
paragraph, so it contains more than one atomic unit. The comparison therefore
establishes only that this panel is **at or above** the measured LLM
over-production norm, not that it sits exactly at 2.3×. Taken with MetaCritique's
precision figure, it implies a non-trivial nitpick fraction — on the order of two
of every seven concerns being unfounded — but that is an **inference from external
data, not a local measurement.** Nobody has labelled these concerns for accuracy;
see `04` R9.

Two things follow. First, the personas do not ask for a fixed number of concerns,
which is correct — CriticGPT (arXiv:2407.00215) measured that demanding more claims
buys recall and nitpicks together on a curve with no principled operating point.
Second, the personas *do* say *"`questions_to_ask` is the most valuable field you
produce"*, which is a volume incentive on the other negative-valence field
(mean 7.44 questions per opinion, essentially the same as concerns).

## 6. Where the middle of the scale collapses

`caution` is defined identically in all eight personas as *"a real weakness that
changes how much weight the result carries"* — with no materiality threshold. Every
early-stage academic record contains a real weakness, so the category is
absorbing. The positive control shows the consequence precisely: **`caution` share
is 68.8% at the `MEDIUM` tier and 68.8% at the `STRONG` tier** — identical. A
single `caution` consult cannot distinguish a mediocre opportunity from an
excellent one.

The panel *as a whole* still separates them (5 blocking / 0 clear at `MEDIUM`
versus 0 blocking / 5 clear at `STRONG`). The information loss is at the level of
the individual label, in the middle of the scale — **not at the `clear` end the
alarm is watching.**

## 7. Signal entropy per domain: the alarm's metric measures the wrong thing

```
domain          n  clear%  H(bits)  %of max  modal share
chemistry      82   0.00%    0.914    57.7%     67.1%
scientific    277   0.00%    0.719    45.4%     80.1%
commercial    182   0.00%    0.718    45.3%     80.2%
legal          91   0.00%    0.695    43.8%     81.3%
talent        188   2.13%    0.468    29.5%     92.0%
budget         67   7.46%    0.383    24.2%     92.5%
clinical      161   0.00%    0.383    24.1%     92.5%
technologic   144   0.00%    0.146     9.2%     97.9%
ALL          1192   0.76%    0.634    40.0%     85.7%
```

Maximum entropy for three labels is 1.585 bits.

- **`chemistry` is the most informative domain in the panel (0.914 bits, 57.7% of
  maximum) and has never once cleared.** It discriminates entirely through
  `blocking` (33% of its consults).
- `budget` has the **highest** clear rate (7.46%) and **lower** entropy than
  chemistry, because it never blocks (0/67).
- Raising the clear rate to the 5% floor while holding blocking at 13.5% would
  raise panel entropy from 0.634 to 0.847 bits — **+0.213 bits**, and would say
  nothing about whether the added clears were true.

**The clear rate and the discriminative power it claims to proxy are close to
unrelated in this data.** That is the defect in the alarm, and it is the same
error the literature warns about most sharply: spread is cheap to buy and is not
validity (arXiv:2606.03043 — a 3.4× widening with the evaluation axis unmoved).

## 8. `verdict_signal` feeds no decision

```bash
grep -rn "verdict_signal" src/ | grep -v specialists.py | grep -v models/
```

Every reader outside the persistence layer is display: `routers/admin.py:492`,
`services/thread_panel.py:135-179`, `services/assessment_detail.py:238-750`. The
specialist floor counts consults **by domain, not by signal** — `panel_is_owed`
(`specialists.py:589`) never looks at it, and `weighted_score` is computed from
the hub's own sidecar dimension scores. The signal reaches a verdict only
textually, because the hub reads the full opinion body (`tools.py:775`).

**Consequence for severity:** this is not a defect that has mis-scored a funding
decision. It is a defect in what staff are shown and in what the run-level alarm
asserts. The panel's actual work — the opinion bodies and `questions_to_ask` that
shape the interview — is unaffected.

## 9. Predictive validity: cannot be measured, and why

Only **6** `opportunity_assessments` rows survive (the v3.2.0 residuals purge on
2026-08-27 removed all 82 earlier verdicts after a verified backup). Joining what
remains:

| subject | recommendation | score | consults | blocking% |
|---|---|---|---|---|
| rothstein | conditional | 2.85 | 28 | **0.0** |
| coyne | pass | 2.60 | 26 | 19.2 |
| slusher | pass | 2.50 | 30 | 10.0 |
| konig | pass | 2.45 | 26 | 11.5 |
| yarchoan | pass | 2.30 | 18 | 11.1 |
| lamichhane | pass | 2.25 | 16 | 12.5 |

Spearman ρ = −0.371 overall; among the five `pass` rows ρ = +0.10. **At n=6 this
is not evidence of anything** and is reported only to record that the check was
run and that the one non-`pass` verdict was also the only one with zero blocking
consults.

**Measurement gap worth naming:** 1,186 consults are now orphaned from the
verdicts they informed. Panel-to-verdict validity cannot be studied historically
and will not be answerable until ~20 more v3 verdicts accumulate.

## 10. One thing the architecture already gets right

The literature's strongest single prohibition for a critic panel is: never put a
prior score or a sibling critic's verdict into a critic's context ahead of its own
reasoning — anchoring reaches Cohen's d = 0.71, blocks 48% of error corrections,
and is **not** removable by instructing the model to ignore it (arXiv:2608.25869).

Measured against all 1,192 consults:

| probe | hits |
|---|---|
| context contains `verdict_signal` | **0** |
| context contains a "blocking/caution/clear signal" phrase | **0** |
| context contains a numeric score | **0** |
| context mentions "panel" | 205 — all inspected samples are *assay* panels (cryptic-exon panel, isolate panel, metabolite panel) |

**Every specialist forms its opinion without seeing any other specialist's
verdict.** That is the correct design and it should be protected explicitly, not
just left true by accident.
