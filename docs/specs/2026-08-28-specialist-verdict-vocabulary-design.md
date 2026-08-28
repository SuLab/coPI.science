# Specialist verdict vocabulary: stage bars, read-state, and the retirement of the clear-rate floor

**Status:** design approved in conversation 2026-08-28. **Phase A and Phase B are both
implemented**, and §3.2's field-ordering decision was subsequently **REVERSED by the
ladder measurement it required** — see the box in §3.2. Phase B landed in `46d9a99`
(stage bars, the vocabulary rename, and a nine-item audit fix wave) with the ordering
revert on top. Original Phase A note follows.

**Phase A is implemented**
(§8 steps 1-5, plus the migration), in commits `fd12e5e..6970488` on the
`blackbird` branch: the harness (§7.1, `scripts/panel_calibration_ladder.py`),
the retirement of the clear-rate floor and the alarm/display/hub-label
reordering (§5, `src/agent/specialists.py`, `src/agent/tools.py`), the
no-cross-anchoring test (§7.4), migration `0038` (§6, four additive nullable
columns on `specialist_consults`), and the `read_state` derivation itself
(§3.3). **Phase B — the label rename and the extracted stage bars (§8 step
6) — is NOT implemented.** No prompt, persona or rubric file has been
touched: the stored vocabulary is still `blocking`/`caution`/`clear`, and no
domain has been shown a stage bar.

**One deviation from this document, flagged in review and recorded here
rather than silently absorbed:** §5 says the hub-label reorder step also puts
`read_state` on the staff surfaces — `assessment_detail` and `thread_panel`
cards carry "label + concern count + `read_state`". That display step did
NOT ship in Phase A; it moved to Phase B. So Phase A writes
`specialist_consults.read_state` on every new consult (§3.3, §6) and no human
surface renders it yet — `assessment_detail.py`'s card dict and
`thread_panel.py`'s explicit column list both omit it. `established` is
NULL on every row for the same reason (§6): the column exists, nothing writes
it.

**Follows from:** `docs/audits/2026-08-27-consult-persona-calibration/` — a
root-cause analysis that ran a 48-consult positive control through the production
consult path and established that the specialist panel is **not** miscalibrated.

**Supersedes nothing.** It resolves two items the 2026-08-18 design
(`docs/specs/2026-08-18-specialist-panel-remediation-design.md`) explicitly left
open, and it is the "narrower, evidence-driven look" that document deferred.

---

## 1. What the RCA established, in one place

Measured, not inferred. Full evidence and SQL in the audit.

| finding | evidence |
|---|---|
| The panel **discriminates strongly** | Across a controlled quality ladder, `blocking` share fell 87.5% → 31.2% → 0.0% and `clear` rose 0% → 0% → 31.2%. Fisher exact on the blocking shift: **p = 5.1 × 10⁻⁷**. |
| Construct sensitivity is **~2× the published average** for LLM judges | R = 0.594 at one quality rung, 0.875 at two, against a published average of 0.319 (arXiv:2608.24419). |
| Question framing is **not** the cause | Rewriting every question from presupposition-laden to neutral-symmetric moved the clear count by one consult (**p = 1.00**), and the neutral framing was *harsher* at the medium tier. |
| The **clear rate measures the population**, not the instrument | The population-faithful tier reproduces production within overlapping CIs. `chemistry` is the panel's most informative domain (0.914 bits of 1.585) and has **never cleared in 82 consults**. |
| **Exhortation has already failed** at n=1192 | *"Say this when it is true; a panel that never clears anything is noise"* is in all eight personas and produced 0.76%. Human analogue: NIH instructs reviewers that "5 is average" and to "use the full range"; observed centres are 2, 2, 3, 3, 4. |
| `clear` is **semantically decoupled** from its content | All nine `clear` opinions ever emitted carry 4–9 concerns; one lists *"Succession risk is high as described"* under a Slack ✅. |
| The information loss is in the **middle** of the scale | `caution` share is 68.8% at the medium tier and 68.8% at the strong tier — identical. |
| `legal` is the **one genuinely flat domain** | `caution` in all six probe cells; 0 of 91 `clear` in production; mean concern count *inverted* (9.01 on caution vs 8.94 on blocking). |
| The label **feeds no decision** | Every reader outside persistence is a display surface. The floor counts consults by domain (`panel_is_owed`), and `weighted_score` comes from the hub's own sidecar. |

**Two root causes, and the second was found while writing this design.**

**RC1 — the vocabulary.** `clear` is defined as *"nothing in your domain stands in
the way"* — a universal negative over an entire domain — while the question asked is
narrow. That is why six of eight domains never reach it. `budget` is the
counter-example that proves the mechanism: it is the only persona with external
decidable thresholds (`$100K–$1M`, `12–24 months`) and the only one instructed to
render an affirmative determination, and it has the panel's best clear rate at 7.46%.

**RC2 — the propagation gap, which is the deeper of the two.** The rubric is
rendered into the hub's system prompt precisely so that the hub's judgment and the
code's arithmetic cannot drift apart (`render_rubric_markdown()` → the `{rubric}`
placeholder, `agent.py:322`). **The specialists were never included in that
mechanism** — `_execute_consult_specialist` reads the persona file raw
(`tools.py:593`). So for the panel's entire history it has been judging against
standards the Blackbird document explicitly disclaims: the document says
*"Freedom-to-operate is diligence, not a gate"*, *"'FTO secured' is not the bar"* and
*"Unresolved FTO on unpublished academic science is the normal starting condition,
not a disqualifier"* — three separate statements — and the `legal` persona has none
of them, which is why it has never cleared once in 91 consults.

RC2 subsumes RC1's remedy: the bars do not need to be written, only propagated
(§4.2).

## 2. Decisions taken

Resolved with the operator before this document was written.

| # | Question | Decision |
|---|---|---|
| **D1** | Should the signal exist at all? | **Yes.** Three of four consumers matter — the hub's reasoning, the workspace-visible Slack panel note, and staff read surfaces. Deletion is off the table. |
| **D2** | Raise the clear rate? | **No.** Spread is not validity (a 3.4× widening left the evaluation axis unmoved, arXiv:2606.03043); prompting moves the criterion, not the resolution (arXiv:2606.15610); and the exhortation is already present and already failed. |
| **D3** | Keep three levels, or collapse to binary? | **Keep three, re-cut the boundaries.** The probe showed three tiers are distinguishable; collapsing to binary would discard the measured top-end channel and merely relocate the absorbing category (~86.5%). |
| **D4** | Where do the bars live? | **`prompts/rubric/blackbird-rubric.toml`, keyed by DOMAIN**, rendered into each persona at consult time — mirroring how the rubric is already rendered into the hub's `{rubric}` placeholder so document and behaviour cannot drift. Keying by domain rather than dimension is what makes it work for all eight. |
| **D5** | Restore the three deleted rubric dimensions? | **No.** `market_unmet_need`, `ip_fto` and `platform` were removed by v3 on evidence. A panel fix must not quietly reopen a scoring decision. |
| **D6** | Who writes the bars? | **Nobody writes them — they are EXTRACTED.** The Blackbird document already states what is adequate at incubation stage, for every one of the eight domains (§4.2). No new policy is authored, so there is no ratification blocker. The only authored artifact is a domain→source mapping, which is config, not policy. |
| **D9** | Why did the personas not have the bars? | **Because the rubric is rendered into the hub's prompt and was never rendered into the specialists'.** `render_rubric_markdown()` fills `{rubric}` in `prompts/roles/scout_hub/agent-system.md` (`agent.py:322`); `_execute_consult_specialist` reads the persona file raw (`tools.py:593`). The fix is to extend an existing mechanism, not to invent one. |
| **D7** | Put the concern count in the Slack note? | **No.** `format_panel_note`'s narrow signature is documented as *"the enforcement... deliberately not widened"*. Better wording achieves the same fix without widening a boundary whose value is that it never has been. Counts go to staff surfaces only. |
| **D8** | One harness or two? | **One.** Absorb `scripts/diagnose_specialist_calibration.py` rather than add a parallel probe. |

## 3. The contract

### 3.1 Labels

| label | meaning | status |
|---|---|---|
| `blocking` | a defect that disqualifies this opportunity in my domain as it stands | **unchanged** — carries all the panel's measured information |
| `gap` | the record falls short of the bar for this stage, **and I can name the specific thing that must be produced to reach it** | replaces `caution` |
| `adequate` | the record **meets the bar for this stage** in my domain | replaces `clear` |

Two properties are load-bearing:

**`gap` has a threshold that `caution` never had.** `caution` was *"a real weakness
that changes how much weight the result carries"*, and every early-stage academic
record contains a real weakness, so the category absorbed 85.7% of all consults.
Requiring the specialist to *name the specific missing artifact* is the threshold: a
gap that cannot be named is not a gap.

**`adequate` is a positive claim, not an absence claim.** It explicitly does **not**
mean "no concerns". Concerns are expected alongside it, because the assertion is
about sufficiency for a stage decision. "Adequate for stage · 6 concerns" is
coherent where "✅ clear · 6 concerns" is a contradiction — which is what all nine
production clears currently are.

**Constraint:** `specialist_consults.verdict_signal` is `String(10)`. `blocking`
(8), `gap` (3) and `adequate` (8) fit with no DDL change; `disqualifying` (13) would
not. This is a second reason to retain `blocking`.

### 3.2 The response schema

```
{
  "established":      ["what the record does support in your domain"],
  "concerns":         ["one specific concern per entry"],
  "questions_to_ask": ["a question the hub should put to the PI"],
  "verdict_signal":   "blocking | gap | adequate",
  "confidence":       "high | moderate | low"
}
```

Two changes from today, both with independent justification:

**`established` is new, and it is load-bearing rather than decorative.** For
`adequate` to be evidenced rather than asserted, the specialist must name what the
record establishes — which converts the top label from an unprovable universal
negative into a positive claim with stated grounds. It also fixes a measured
workaround: three of the nine production `clear` opinions file a *positive finding
inside the `concerns` array* with a hedge appended (*"this is the strongest positive
signal in my domain and I have no counter-evidence against it, but…"*), because
`concerns` and `questions_to_ask` are the only content fields and both are
negative-valence. Supporting evidence: LLMs outperform single human raters at
identifying weaknesses while humans remain better at identifying strengths
(arXiv:2605.04298), and in human grant review **proposal risks predict reviewer
scores more strongly than proposal strengths** (Kaatz et al. 2022, *PLOS ONE*
17(9):e0273813) — the response there was an instrument change, not reviewer
retraining.

**`verdict_signal` and `confidence` move to the end.** A model generates left to
right, so the current schema commits to a label *before* writing a word of evidence.
Multiple Evidence Calibration — evidence first, then rate — is worth **+6 to +11
accuracy points and +0.06 to +0.21 κ** (arXiv:2305.17926). Anchoring on a score
already in context is large and not instructable away (arXiv:2608.25869, d = 0.71).
Parsing is unaffected: `parse_opinion` uses `data.get(...)`, `extract_json` is
order-agnostic, and no test pins key order (verified).

*Honest limit:* the clean A/B of "score then justify" versus "justify then score" is
unpublished. This is MODERATE evidence, and the ladder must validate it.

> ### ❌ REVERSED BY MEASUREMENT (2026-08-28). This section's decision did not survive.
>
> The ladder was asked to validate this and **invalidated it.** `verdict_signal` is back
> at the FRONT of the contract; `confidence` stays at the end. This is the spec working
> as designed — the honest limit above named the risk, and the instrument it demanded
> settled it — so the section is left standing rather than rewritten.
>
> Seven 48-consult ladder runs (336 consults), one variable isolated per arm against a
> pre-registered design (`docs/audits/2026-08-27-consult-persona-calibration/
> 05-isolation-series-design.md`):
>
> | contract | pooled R | top label reached |
> |---|---|---|
> | verdict FIRST (baseline, before any change) | 0.625 | 7 of 48, 4 domains |
> | verdict LAST | 0.281 – 0.469 | 0 – 10 of 48, 0 – 5 domains |
> | **verdict FIRST (restored, keeping every other change)** | **0.531 - 0.594** (2 runs, pooled 0.5625) | **20 of 48, all 8 domains** (both runs) |
>
> With the verdict last, two consecutive runs could not reach the top label **at all**,
> and pooled construct sensitivity fell to less than half of baseline. Moving the key
> back recovered it to within one paired comparison of baseline while nearly tripling
> top-label reach.
>
> **Why the cited warrant did not transfer.** The +6-to-+11 MEC figure is **k = 6
> ensembling on PAIRWISE judging**, not a k = 1 reorder of one key in one schema, and the
> same review grades the opposite finding — chain-of-thought narrowing the criterion — as
> STRONG. The d = 0.71 anchoring result is an EXTERNAL-anchor effect, not a
> self-generated-token one. Both citations were qualified in place in the source comments
> that relied on them.
>
> **Mechanism, as far as it is understood.** `concerns` is a REQUIRED negative-valence
> array. A verdict written after it is chosen with a freshly-authored list of problems
> adjacent in context, and self-consistency pressure compresses the scale toward the
> middle — `blocking` fell to 0 of 48 and the top label became unreachable. Ordering the
> rating after the evidence ordered it after *negative* evidence. Everything else in this
> spec — `established`, the stage bars, the `blocking`/`gap`/`adequate` vocabulary,
> `read_state` — was measured to be worth keeping; only the key's POSITION was costly.

### 3.3 Read-state comes out of the judgment axis

Today `caution` means both *"I found a weakness"* and *"we could not read this
reply"* — `parse_opinion` defaults to it, and `_warn_defaulted` exists solely to
make that difference greppable in a log.

Add `read_state ∈ {parsed, defaulted, truncated}`, **derived in code, never asserted
by the model**, from state the code already has: `extract_json` success,
`is_truncated_stop`, and `has_usable_content`.

The conservative default becomes **`gap`, never `adequate`** — so an unread
specialist still cannot read as approval, which is the safety property
`_DEFAULT_SIGNAL = "caution"` was protecting.

This also generalises an existing special case. `_post_panel_note` already pulls
`truncated` back out of `**_withheld` because *"it CANCELS the note. A consult the
API cut off mid-sentence parsed to nothing, so `verdict_signal` is the schema's
DEFAULT `caution` and no specialist ever said it."* That reasoning is correct and
applies equally to a reply that arrived **complete and simply failed to parse** —
which is not `truncated`, and therefore currently **does** post a workspace-visible
note asserting a verdict nobody produced. One predicate (`read_state != parsed`)
replaces one special case and closes that gap.

## 4. Where the bars come from

### 4.1 Location

A new domain-keyed table in `prompts/rubric/blackbird-rubric.toml`, rendered into
each persona at consult time via a placeholder, exactly as the rubric is already
rendered into the hub's system prompt.

This buys four things at no extra cost: fail-fast validation at import (the loader
already raises `RubricError` on a malformed document); version stamping;
content-hash tracking; and inclusion in the human-reviewable rubric export
(`scripts/render_rubric_review_doc.py`), which satisfies the standing operator
directive that the review doc never lag the document — without a separate step.

The TOML already carries a per-dimension `specialist` field with bidirectional test
coverage (`test_document_specialist_fields_name_real_specialist_domains`), so a
domain-keyed section fits the existing structure rather than inventing one.

### 4.2 The bars already exist in the document

The bars are not authored. **The rubric states what is adequate at incubation stage,
globally and per domain, and the specialists have never been shown any of it.**

The global bar is one sentence in `[scoring].preamble`:

> *"Screen at the incubation grain: the bar is scientific robustness plus
> translational potential — **never the replicated data, filed IP, or identified
> syndicate a later-stage deal would show.**"*

And every domain has its own stage clause, in the dimension `anchors` field, the
gating descriptions, or `red_flags`:

| domain | source in the document | the stage clause, verbatim |
|---|---|---|
| `scientific` | `scientific_credibility.anchors`, `gating.credible_science` | *"Judge the evidence they have, not evidence the stage hasn't produced: animal rescue is a 5, not the bar for a 4."* · *"IP is not required at this stage."* |
| `chemistry` | `translational_path.anchors` | *"tractability, not progress"* · *"ignorance of the risk scores low; absence of data at this stage does not"* |
| `clinical` | `differentiation_unmet_need.anchors` | *"order-of-magnitude prevalence/TAM suffices, actionability over precision"* |
| `commercial` | `differentiation_unmet_need.anchors`, `venture_potential.anchors` | *"a differentiated mechanism against an actionable, quantifiable need"* · *"If the science works, is there a company or license a VC or pharma would want"* |
| `legal` | `venture_potential.anchors`, `gating.translational_potential`, `red_flags` | *"a clean **path** to ownable IP (disclosure filed or filable, no known encumbrance or hostile co-ownership, plausible university license path — **'FTO secured' is not the bar**)"* · *"Freedom-to-operate is diligence, not a gate"* · *"Unresolved FTO on unpublished academic science is the normal starting condition, not a disqualifier."* |
| `technologic` | `venture_potential.anchors` | *"**a single asset is the normal shape of a de-risking grant** — mark it down only where a clean result would still leave nothing worth building"* · *"its absence at this stage is expected and scores nothing down on its own"* |
| `talent` | `team_executability.anchors` | *"complementary expertise **identified, not necessarily hired**"* |
| `budget` | `fundable_experiment.anchors` | *"Could a $100K–$1M grant over 12–24 months buy a **decisive** de-risking result?"* |

**This reframes the defect.** The RCA concluded the personas lack a stage bar. The
document shows something sharper: **the personas have been applying standards the
document explicitly disclaims.** `legal` is the clearest case — its bar is stated
three separate times, and it has none of them, which is why it cautions on
everything and has never once cleared in 91 consults. `technologic` objected in the
probe that a platform claim was untested on a fourth target; the document says a
single asset is the *normal shape*. `clinical` pressed for precise incidence
numbers; the document says order-of-magnitude suffices.

**A limitation of the RCA's own experiment falls out of this, and is recorded rather
than buried.** The probe's `STRONG` case was built with replicated data, filed IP
and named syndicate interest — the exact three things `[scoring].preamble` says are
*never* the bar. So it was constructed against a later-stage standard than Blackbird
screens at, which means its 31.2% top-label rate is an upper bound on an unusually
favourable record and not a target. This is consistent with Jensen & Thursby (2001):
only 28% of inventions universities actually license hold an issued patent.

### 4.3 Shape of the change

Add a `[stage_bar.<domain>]` table with two keys per domain:

- `source` — which dimension key, gating key, or `red_flags` entry the bar derives
  from. Validated at import against the real keys, in both directions.
- `text` — a verbatim quotation or faithful condensation of that clause.

Recording `source` is what keeps this honest: a reviewer can check every
condensation against the original, and the rubric-sync tests can assert that no bar
names a source that does not exist. The `[scoring].preamble` sentence is rendered
into **all eight** personas unchanged.

Rendering reuses the existing mechanism (`render_rubric_markdown()` →
`{rubric}` placeholder) extended to a specialist-scoped render, so the persona a
specialist reads and the anchors the hub scores against cannot drift apart — the same
invariant `blackbird_rubric.py` already exists to hold for the hub.

Supporting evidence for why a *bar* rather than more disposition prose: explicit
threshold language moves behaviour by large margins (a 28.7-point swing on a
39-point risk-tolerance scale from framing alone, arXiv:2509.23058), while persona
*assertion* does not improve judgment at all (Findings of EMNLP 2024, 162 personas ×
2,410 questions), and personas changed decisions only when a mediator converted them
into explicit heuristic values (IJCNLP-AACL 2025).

### 4.4 Anchor availability — v3 folded, it did not delete

Five domains own a current dimension: `scientific` → `scientific_credibility`,
`chemistry` → `translational_path`, `commercial` →
`differentiation_unmet_need` + `venture_potential`, `talent` →
`team_executability`, `budget` → `fundable_experiment`.

Three own none — `clinical`, `legal`, `technologic` — because v3 removed
`market_unmet_need`, `ip_fto` and `platform`. **But the content was folded, not
deleted**, and the folds are recorded in the document's own consolidation comment:

| deleted dimension | folded into | the clause that carries the old domain's bar |
|---|---|---|
| `market_unmet_need` | `differentiation_unmet_need` | *"aimed at a real clinical decision point with a downstream intervention — order-of-magnitude prevalence/TAM suffices"* |
| `ip_fto` | `venture_potential` | *"a clean **path** to ownable IP … 'FTO secured' is not the bar"* |
| `platform` | `venture_potential` | *"A reusable platform … but a single asset is the normal shape of a de-risking grant"* |

So all eight bars are derivable from the live document, and none needs authoring.
This **corrects an earlier claim in this design** that v3 had removed the repair
material: it relocated it. Per D5, `maps_to_dimensions` stays empty for those three
— a bar's `source` may name a clause in a dimension the domain does not own, which
is exactly why `source` is a free-form key validated against real keys rather than a
reuse of `maps_to_dimensions`. The existing test tolerates the empty tuple
explicitly (*"An empty tuple is allowed — some specialists inform judgement without
owning a dimension"*).

**One correlation, reported but not leaned on:** the three domains that own no
dimension are also the flat one (`legal`) and the two lowest-entropy ones
(`technologic` 0.146 bits, `clinical` 0.383). Mean signal entropy is 0.640 bits for
owning domains against 0.408 for non-owning. At n=8, with subject matter confounded
with ownership, this is suggestive only — and `legal`'s flatness predates v3, so v3
did not cause it. The causal claim this design does rest on is narrower and does not
depend on that correlation: **no persona has ever received any stage clause, owned or
folded** (D9).

### 4.5 A side effect to fix while here

`directory.py:421` builds `specialist_for` from `maps_to_dimensions` to answer "who
to ask when this dimension is scoring badly". All six current dimensions still have
an owner, so nothing is broken — but `clinical`, `legal` and `technologic` can no
longer be surfaced there. Key that hint off the new domain bars instead.

## 5. Consumers

**Hub.** `tools.py:775` returns `f"{spec.title} — signal: {signal}\n\n{opinion.raw}"`
— the label sits *before* the body, the worst position per §3.2's anchoring
evidence. Move it after the body and carry `read_state` with it.

**Slack panel note.** Wording carries the fix: `⛔ disqualifying` / `⚠️ gap` /
`☑️ adequate for stage`. The note is cancelled whenever `read_state != parsed`
(§3.3). Per D7, `format_panel_note` keeps its narrow signature — no concern count,
no opinion content — and a test asserts the signature has not widened.

**Staff surfaces.** `assessment_detail` and `thread_panel` cards carry label +
concern count + `read_state`. `thread_panel.py:142` already comments on parse
defaults, so this extends existing awareness rather than introducing it.

**The run-level alarm.** `clear_rate_warning` currently asserts *"A panel that clears
almost nothing cannot discriminate — check persona calibration."* That assertion is
falsified for seven of eight domains. Replace with a report of the signal mix that
names the population as the expected cause and points at the harness, plus a
per-domain modal-share warning worded as *a prompt to run the ladder*, not a
verdict — `technologic` sits at 97.9% modal share yet was fully quality-sensitive in
the probe, so a modal-share number alone must not be allowed to convict a domain.

**Retire the numeric floor.** `MIN_CLEAR_RATE = 0.05` sits above what a correct
panel produces on this population, making it a permanent false alarm. There is no
number to replace it with: the optimal threshold for a screening instrument is a
**likelihood ratio**, a function of the base rate of the population screened, so a
fixed floor on the output rate is the wrong shape of constraint. `NeurIPS 2021`'s
two-committee experiment concluded that *"making the conference more selective would
increase the arbitrariness of the process."*

`test_the_clear_rate_floor_is_pinned` asserts `MIN_CLEAR_RATE == 0.05` literally,
with the docstring *"pinned literally so a future loosening is a diff, not a
drift."* That is working as designed: the change must appear as an explicit diff to
that assertion, with the new reasoning recorded in its docstring.

## 6. Migration and comparability

Four additive **nullable** columns on `specialist_consults`:

| column | type | why |
|---|---|---|
| `read_state` | String | §3.3 |
| `established` | JSONB | §3.2 |
| `rubric_version` | String | **consults are currently unstamped** |
| `rubric_content_hash` | String | same |

The stamp is arguably the highest-value item in this migration. Assessments have
carried `rubric_version`/`rubric_content_hash` since `0030`; consults never have —
which is precisely why pre- and post-change consults cannot be compared. With the
bars living in the rubric document, a bar change bumps the version and every consult
records which bars it was judged against.

**No width change** is needed: all three labels are ≤ 10 characters.

**Existing rows are not rewritten.** `blocking` remains valid across the boundary;
`caution` and `clear` become historical values the read path must continue to
render. Read paths therefore handle five values, which is an ongoing cost to state
rather than a one-off. This follows the v3 precedent of not retro-fitting stored
history, and differs from the assessment purge only because consults were
deliberately retained.

**Migration number.** CLAUDE.md reserves `0038` for the deferred `users.is_admin`
drop, which is unwritten. This may therefore be `0038` (and the drop moves later) or
`0039`. Resolve at implementation; do not assume.

**Deploy order — migrate BEFORE the new code serves.** Additive and nullable, so old
code against the new schema is safe; the reverse is not, because the new code maps
the new columns and every `select(SpecialistConsult)` would raise `UndefinedColumn`.
Same sequence as the `0028`/`0030`/`0036`/`0037` boxes:

    DC="docker compose -f docker-compose.prod.yml"
    $DC build blackbird-app worker
    $DC run --rm blackbird-app alembic upgrade head
    $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
    $DC up -d blackbird-app worker

The agent image bakes `src/` in and must be rebuilt separately
(`$DC --profile agent build agent`). **Persona and rubric edits alone need no image
build** — `prompts/` is bind-mounted and `_execute_consult_specialist` reads the
persona file per call (`tools.py:593`), so a persona change is live on the next
consult. The rubric, by contrast, is read once at import, so a bar change **does**
require a restart.

## 7. Validation

### 7.1 The harness

Absorb `scripts/diagnose_specialist_calibration.py` (98 lines, committed `84fa1aa`,
self-described as throwaway) into a maintained calibration harness. Three
substantive upgrades over what it does today:

1. **Use the production path.** It calls `generate_agent_response` directly with
   `persona_path` + `parse_opinion`, bypassing `_execute_consult_specialist` — so it
   does not exercise the pinned `llm_agent_model_opus`, `max_tokens=4000`, or the
   truncation handling. The new harness calls `_execute_consult_specialist` with both
   persistence callbacks `None`, so it writes no rows and credits no floor.
2. **Full ladder.** Three quality tiers × two framings × eight domains, keeping its
   three original `STRONG_CASES` as additional fixtures so the 2026-08-18 result
   stays reproducible.
3. **Report R and S**, the construct-sensitivity and invariance pair, not just the
   raw signal counts.

**Not in `ci.sh`** — it makes real API calls (~48 Opus calls, roughly 13% of a
one-hour run).

### 7.2 Acceptance criteria, as numbers that can fail

| criterion | today | requirement |
|---|---|---|
| `WEAK` tier: `blocking` + `gap` share | 100% | **≥ 85%** — the leniency-drift tripwire |
| `legal` verdict changes across tiers | 0 of 2 | **must change** — the falsifiable success criterion for the whole approach |
| Construct sensitivity R at one rung | 0.594 | **must not fall below 0.594** |
| `STRONG` tier: `adequate` count | 5 of 16 (`clear`) | **≥ 5 of 16** |
| `STRONG` tier: which domains reach the top label | only `budget`, `talent`, `scientific` | **at least one domain outside those three** — otherwise the bars changed the word, not the reachability |

**Which criteria bind at which step.** The first and third are **regression
tripwires** and bind at every step that touches model behaviour — step 5 (schema
reorder and `established`) as well as step 6. The second and fourth are
**success criteria for step 6 specifically** and cannot be evaluated before the bars
exist. Steps 1–4 touch no model behaviour and are gated by `ci.sh` alone.

**If the `legal` bar does not move `legal`, the bar is not decidable and the
approach has failed for that domain** — report it rather than reword the bar until
the number moves, which would be fitting the instrument to the metric. The same rule
applies to the fourth criterion: if the top label stays reachable only for
`budget`, `talent` and `scientific`, the bars renamed the word without changing what
it can be applied to, and that is a failed step, not a partial success.

### 7.3 Sequencing within validation

**One domain at a time, with a ladder run between each, or attribution is lost.**
This is the same discipline the RCA was conducted under and it is the reason the
RCA could refute three hypotheses instead of one.

### 7.4 Tests

- Vocabulary pinned literally (so a future change is a diff, not a drift).
- `read_state` derivation for each of parsed / defaulted / truncated.
- Panel note **cancelled** when `read_state != parsed`.
- `format_panel_note` signature has **not** widened — assert it cannot receive
  `concerns`, `questions_to_ask`, `established`, `confidence` or the opinion body.
- The no-cross-anchoring invariant: a consult's assembled `question`/`context`
  contains no sibling `verdict_signal` and no numeric score. Currently true at
  0 of 1,192 and protected by nothing.
- Rubric-sync tests extended bidirectionally: every domain has a bar, every bar
  names a real domain.
- Alarm wording, including the new "counted consults" denominator language.

**No snapshot regeneration.** The `.ambr` GM snapshot pins `pi_lab` strings, not
specialist personas. `pytest --snapshot-update` remains prohibited.

## 8. Sequencing

| step | scope | vocabulary? | gate |
|---|---|---|---|
| 1 | Harness (§7.1) | untouched | none — new script, not in `ci.sh` |
| 2 | Alarm + display + hub label ordering (§5) | untouched | `ci.sh`; agent image rebuild; flag restart to operator |
| 3 | No-cross-anchoring test (§7.4) | untouched | `ci.sh` |
| 4 | Migration (§6) | untouched | migrate-before-serve; `alembic current` must equal `heads` |
| 5 | Schema reorder + `established` (§3.2) | untouched | ladder run before and after; persona files only, live on next consult |
| 6 | **The label rename (§3.1) + the bars (§4), together** — extracted from the document, one domain at a time starting with `legal` | **changes here and only here** | ladder run between each domain; rubric change needs a restart |

**The rename and the bars are one step, deliberately.** `adequate` without a
decidable bar behind it is a renamed `clear` — pure churn that pays the full cost of
a vocabulary migration, adds a fifth stored label value, and carries the leniency
risk with none of the benefit. They ship together or not at all.

**Step 6 is no longer blocked.** Under D6 the bars are extracted from the live
rubric rather than authored, so there is no ratification gate — what remains is a
review that each `text` faithfully condenses the clause its `source` names, which is
a diff review, not a policy decision.

**Steps 1–5 leave the vocabulary alone, and are worth doing on their own.** They
retire a false assertion and a permanently-firing floor, stop the workspace-visible
note asserting verdicts nobody produced, stop `clear` rendering as a bare ✅, put
evidence before the verdict in the schema, give consults their first rubric stamp,
and stand up the instrument that makes every later change an experiment. None of
them changes a stored label value, so all five are independently reversible and none
depends on D6.

**Consequence worth naming: this stops cleanly at any step boundary.** Step 6 is the
only step that touches a stored label value, and nothing in steps 1–5 leaves a
half-migrated vocabulary in the tree. If step 6 is abandoned — because the extracted
clauses turn out not to move a verdict, or because the condensation review rejects
them — steps 1–5 stand on their own and no rollback is needed. That is the intended
shape, not a fallback.

## 9. Out of scope

- **Restoring the three deleted rubric dimensions** (D5).
- **Splitting the middle into four levels.** The presence/severity separation is
  specifically unmeasured in the literature; `gap`'s naming requirement is the
  cheaper test of the same hypothesis and should be tried first.
- **Human-labelled anchors.** ~100 labelled consults would be the only route to a
  *validity* claim as opposed to a *sensitivity* claim (arXiv:2506.02945;
  arXiv:2605.09227). It is operator time, not code, and it is the highest-value item
  not in this design.
- **Making panel opinions reach the CONCLUDE turn** (F7 of the 2026-08-18 design,
  deliberately deferred there and still open). This design does not change the fact
  that the panel steers the hub's questions rather than its score.
- **Prestige and identity leakage.** Every consult names the PI, lab and
  institution, and revealing author identity has been measured lowering LLM
  rejection recommendations by ~25% of the mean rejection rate on identical content
  (arXiv:2509.15122). Intrinsic to a simulation that interviews a named PI;
  recorded, not fixed.
- **Gating on `confidence`.** Never do this: 84.3% overconfidence across nine models
  and 13 biomedical datasets, with fine-tuning failing to fix it (JAMIA Open 2025).
  Locally, `blocking` is 91.9% high-confidence while `clear` is 8 of 9 moderate.

## 10. Risks

| risk | consequence | mitigation |
|---|---|---|
| A bar's `text` drifts from the clause its `source` names | The panel judges against a standard the document does not set — the present failure, reintroduced by the fix | `source` is recorded and validated against real keys; the condensation is reviewed as a diff against the original; rubric-sync tests assert both directions |
| The extracted clauses turn out not to be decidable enough to move a verdict | Vocabulary migration paid for nothing | §7.2's `legal`-must-move criterion fails the step explicitly. Mitigating evidence: `legal`'s clause is unusually concrete (*"disclosure filed or filable, no known encumbrance or hostile co-ownership, plausible university license path"*) and its current standard is explicitly disclaimed three times over, so this is the domain with the most headroom, not the least |
| Renaming makes the panel more lenient | Manufactured `adequate` verdicts feed the specialist floor's credibility | `WEAK`-tier ≥ 85% tripwire; the default stays `gap` |
| Five stored label values forever | Read-path complexity; charts that silently mix vocabularies | Rubric stamp on every consult row (§6) makes the boundary queryable rather than guessed |
| `adequate` still misread as "no concerns" | The ✅ problem returns under a new word | Wording is "adequate for stage"; concern count on staff surfaces; `established` makes the positive claim explicit |
| Ladder becomes the thing being optimised | Goodhart — fitting the instrument to its own test | Fixtures are versioned and reviewed; a failing criterion is reported, not reworded. Rubric-criterion gaming is measured and real (arXiv:2608.11669) |

## 11. Relationship to prior work

- **`docs/audits/2026-08-27-consult-persona-calibration/`** — the RCA this
  implements. Note its own recorded correction: an earlier, smaller diagnosis was run
  on **2026-08-18** and reached the same conclusion; the three domains it tested
  (`scientific` → `clear`, `chemistry` → `caution`, `commercial` → `caution`)
  reproduce **exactly** in the 2026-08-28 probe, ten days and a full rubric regime
  apart, on a different code path.
- **`docs/specs/2026-08-18-specialist-panel-remediation-design.md`** — its D2
  ("diagnose first, do not rewrite eight personas on a hypothesis") is honoured
  here: the rewrite is now evidence-led and narrowed to a vocabulary change plus one
  flat domain. Its **D6 prompt freeze** exempted `prompts/specialists/*` explicitly,
  and has in any case been superseded in practice — hub prompts and
  `thread_guidance` strings have both changed since, most recently in the 2026-08-27
  v3 work.
- **`docs/audits/2026-08-24-panel-clear-rate/`** — framed the hypotheses and issued
  the warning this design is built to respect: *"Do not 'fix' the clear rate by
  editing the personas before running this. Loosening eight personas to make an
  alarm stop is how a panel that discriminates nothing becomes a panel that clears
  everything."* Nothing here loosens a persona; the top label becomes **harder** to
  assert, because it now requires a citation.
