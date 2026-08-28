# Adversarial analysis: integrating the 2026-08-28 JZ prompt redlines

**Status:** analysis only. **Nothing has been integrated and no prompt file has been
modified.** Measured 2026-08-28 against `/home/a/Downloads/2026-08-28-hub-bot-prompts-JZ-redline.docx`
and `…-pi-bot-prompts-JZ-redline.docx`, and against the repo at `b648be5`.

> ## ⚠️ CORRECTION (2026-08-28, superseding this document's first revision)
>
> The first revision of this file concluded that **the hub document contained no
> reviewer edits**. **That conclusion was wrong.** The hub document carries **four
> substantive human edits**, saved with change-tracking switched off. They are
> enumerated in Finding 1 below.
>
> Two mistakes produced the error, both worth recording because they will recur:
>
> 1. **"No tracked-changes markup" was read as "no edits."** The hub docx has zero
>    `w:ins`/`w:del`/`w:moveFrom`/`w:rPrChange` elements in any XML part and no
>    `word/people.xml`. But `docProps/core.xml` shows `created: 2026-08-21T16:32`,
>    `modified: 2026-08-28T16:17:15` — the file was **re-saved seven days later with
>    tracking off**, which is precisely the state in which text edits leave no markup.
>    Absence of markup was evidence of nothing.
> 2. **The comparison was swamped by its own confound.** The doc renders the
>    `{rubric}` placeholder inline (~1,300 words of rubric text) while the repo file
>    carries the placeholder. Comparing word *multisets* without excising that block
>    reported ~1,341 "doc-only" words, and a 40-word human insertion sat inside that
>    noise indistinguishably. The insertion only became visible after the rendered
>    rubric was excised at its anchors and the comparison switched from multisets to
>    an **order-aware aligned diff**.
>
> The operator flagged this independently ("changes to the hub bot prompts in the new
> document may be entirely untracked"), which is what prompted the re-examination.

## The headline: both documents carry reviewer edits; only one carries them as markup

| | hub doc | pi doc |
|---|---|---|
| tracked insertions / deletions | **0 / 0** | **171 / 69** |
| human edits actually present | **4, untracked** | substantial (+2,453 words inserted) |
| base commit | **`3cdb7f5`** (2026-08-24) | ~current (~2–4% word drift) |
| base staleness vs repo | **a full rubric regime** (pre-v3.0.0) | mild |
| safe to integrate as-written | **NO — would revert v3.0.0–v3.2.0** | no, but for a different reason |

Both were **generated from the repo** — each section carries a `Source: <path>` line
naming its origin file — then circulated for review, and a human wrote back into each.
The hub doc simply did not record how.

## Finding 1 — the hub document carries four untracked edits on a stale base

**Base commit: `3cdb7f5`** (2026-08-24, "v2.1.0 — apply the 2026-08-21 prompt-set
review"), identified by exhaustive aligned-diff against all 16 historical versions of
`agent-system.md`. The tell is unambiguous: the doc's `[Moderate]` confidence label
reads "…or **the funnel placement and** path to an instrument still need definition",
which is the exact pre-`fd12e5e` text present in 9 historical versions; `fd12e5e`
(v3.1.0) cut "funnel placement and". So the doc is **one full rubric regime behind**,
and everything said in the first revision about its staleness stands (see Finding 2).

### The four edits

Each was tested against a **complete per-file corpus** of every historical version
(16 of `agent-system.md`, 12 of `phase4-thread-reply.md`, 10 of `thread_guidance.py`)
using order-aware span containment on normalised word streams, and E1/E2 additionally
by literal pickaxe (`git log --all -S`) across **all 1,057 commits**. All four appear
in **no revision of the repo, ever**.

| | file | what it does |
|---|---|---|
| **E1** | `prompts/roles/scout_hub/agent-system.md` (opening) | turns three parallel outcomes into a **sequence** |
| **E2** | `prompts/roles/scout_hub/agent-system.md` (Communication Style) | adds a **"Thought partner, not a bench"** bullet |
| **E3** | `src/agent/thread_guidance.py` `_SCOUT_HUB` DECIDE | **co-develop the experiment with the PI**; ask the lab for rough scope |
| **E4** | `prompts/roles/scout_hub/phase4-thread-reply.md` item 10 | a **project** counts as a fundable unit, not only an experiment |

**E1 — grant, then licence.**

- repo: "…could be **licensed out of the university, de-risked with an incubation grant,** or built into a company."
- doc: "…could be **de-risked with an incubation grant, and from there licensed out of the university** or built into a company."

Not cosmetic: the repo lists three independent destinations; the doc makes licensing a
*downstream consequence* of the grant. Pickaxe: `and from there licensed out` → **0 of
1,057 commits**.

**E2 — a new Communication Style bullet.**

> **Thought partner, not a bench** — Blackbird brings funding, thinking, and expertise;
> the lab brings expertise, thinking, and the bench. Whatever is funded, the work is
> performed in the PI's lab, and Blackbird staff may think alongside them once it is.

Pickaxe: `Thought partner` → **0 of 1,057 commits**. This is the strongest single proof
of authorship: **the same phrase is a *tracked* insertion in the pi doc** (present in
`pi-marked.txt` and `pi-final.txt`, absent from `pi-base.txt`). One reviewer, one voice,
two documents — recorded as markup in one and not the other.

**E3 — an 82-word insertion into the DECIDE phase guidance.**

> Treat that as something you develop with the PI rather than hand down: your commercial
> read tells you what has to be decided, their knowledge of the system tells you what
> would actually decide it, so expect to refine and re-scope the experiment across a turn
> or two until both hold. Ask them for rough scope while you are there —
> order-of-magnitude cost and duration — rather than estimating it yourself; they are
> permitted to give it, and it is what item 10 needs.

`hand down`, `rough scope`, `order-of-magnitude` each occur **0 times** in the current
`thread_guidance.py` and in all 10 historical versions.

**Adjudicated: novel and compatible — it closes a real gap.** It looks at first like a
collision with rule 7 ("Commercial and IP diligence is yours, not the PI's"), but rule
7's enumerated list is commercial/IP only — market size, TAM, comparables, competing
programs, investor sentiment, FTO, encumbrances, licensing path — and it explicitly
reserves the interview for "what only the lab can tell you". Meanwhile
`phase4-thread-reply.md:213` *already* requires the hub to state "roughly what it would
cost and how long it would take" **without ever saying who sources that number**. E3
answers exactly that question. Two mechanical fixes are required on application: it
references "**item 10**", which is the doc's stale v2.x numbering (current
`phase4-thread-reply.md` numbers it **item 5**), and `_SCOUT_HUB` — unlike `_PI_LAB` —
is *not* snapshot-pinned, so it carries no `.ambr` prohibition.

**E4 — a project is a fundable unit.** Item 10 gains "the single experiment **or tightly
scoped project**", the closing line gains "not 'further validation' but the actual
experiment **or scope of work**", and a worked example is inserted:

> A project counts wherever it is scoped like an experiment and ends in a concrete
> result: refining a diagnostic algorithm against a defined set of additional patient
> samples is a project, and its readout is measured performance against that set.

Absent from all 12 historical versions of the file.

### What is *not* an edit

- **The eight specialist personas and `identity.md` are untouched** — see Finding 5.
- The doc's remaining ~30 differences from current are **staleness**, each located in a
  historical version by the same containment test: funnel-stage language, the
  13-dimension score map, `weighted_score` in the skeleton,
  `suggested_derisking_milestones`, the target-level checklist,
  `credible_tech_source`/`fto_achievable`.
- Section 3 additionally contains pre-v2.1.0 draft text that was **never committed in
  that form** (`the existing credible_tech_source key`, `the schema has no key for
  translational potential yet`). These read as implementation notes from the 2026-08-21
  review round, superseded by `3cdb7f5`. They are **not** reviewer intent to capture —
  applying them would revert v2.1.0.

### Two measurement traps this exercise exposed

Both produced wrong intermediate answers in this analysis and are recorded so the next
comparison does not repeat them:

1. **Line wrapping defeats substring grep on the repo side.** `grep "funnel placement
   and path to an instrument"` returned **0 of 16** versions; the phrase is present in
   **9**, split across a line break. Every comparison must run on a
   whitespace-normalised single-line word stream, never on raw file lines.
2. **Pickaxe is byte-literal — never feed it normalised text.** Running `git log -S`
   with lowercased, punctuation-stripped fragments reported "0 commits" for 33 of 40
   spans, nearly all of them false. Pickaxe takes hand-checked raw substrings only; the
   corpus-containment test is the right tool for bulk classification.

## Finding 2 — the hub document's base is a regime out of date

Its `<assessment_json>` skeleton is the **13-dimension v2.x contract**:

```
"funnel_stage": "incubation | pre-seed | seed | follow-on",
"gating": { …, "credible_tech_source": …, "fto_achievable": … },
"scores": { 13 keys }, "weighted_score": 0,
"suggested_derisking_milestones": []
```

Every one of those was removed or renamed on 2026-08-27:

| in the doc | current reality |
|---|---|
| `funnel_stage` | removed entirely (v3.1.0 — zero measured entropy) |
| `credible_tech_source` | renamed `credible_science` (v3.0.0) |
| `fto_achievable` | renamed `translational_potential` (v2.1.0) |
| 13 score keys | consolidated to **6** (v3.0.0) |
| `weighted_score` in the skeleton | cut (v3.2.0) |
| `suggested_derisking_milestones` | cut (v3.2.0) |
| "target-level scientific checklist" | folded into `evidence` lists (v3.0.0) |

It carries **"funnel" 13 times**; the repo's `prompts/` and `thread_guidance.py` carry
it **zero** times.

**So the document still must not be applied wholesale** — that would revert three rubric
releases. What changed with the correction is that it must not be *discarded* either: E1–E4
are reviewer intent that exists nowhere else, and regenerating the doc without capturing
them first would destroy them silently.

## Finding 3 — the pi document has real edits on a nearly-current base

Reviewer edits by section:

| section → target | +ins | −del |
|---|---|---|
| 1. System prompt → `prompts/agent-system.md` | **131** | 39 |
| 5. Making a new post → `prompts/phase5-new-post.md` | 18 | 13 |
| 3. Replying during an interview → `prompts/phase4-thread-reply.md` | 8 | 6 |
| EXPLORE / DECIDE / MUST CONCLUDE → `src/agent/thread_guidance.py` `_PI_LAB` | 7 | 7 |

Drift measured on the system prompt (word multiset):

- **JZ's base vs repo now:** 54 base-only, 93 repo-only words of ~2,511 — the base is
  close to current. The repo's extra words are the 2026-08-27 funnel→instrument
  rewording (`instrument`, `instruments`, `grant-shaped`, `company-shaped`, `de-risking`).
- **JZ's base vs JZ's final:** +1,215 / −140. The review is large and overwhelmingly
  **additive**: 2,511 → 3,586 words, **+43%**.

The substance is a reframing of Blackbird: from "not a funding agency and not a
collaborator" to "an incubator with funding and an early-stage biotech investor … a
thought partner with strong scientific, translational and commercial expertise";
non-dilutive grants named as the focus for a university PI; an explicit
feasibility-versus-commitment distinction; and a stated reason for the prompt's many
prohibitions.

**JZ's insertions do not reintroduce v2.x machinery** — zero mentions of `funnel`,
`pre-seed`, `seed`, `follow-on`, `weighted_score`, `derisking`, or
`route-to-incubation` across all 2,453 inserted words.

E1 and E2 are the hub-side expression of this same reframing, which is why they are
credible as deliberate reviewer intent rather than stray keystrokes.

## Finding 4 — the real conflict: two different replacements for the same deletion

JZ and the repo **independently removed funnel-stage language**, then replaced it with
**different concepts**:

- **Repo (v3.1.0):** replaced it with **instrument fit** — `prompts/agent-system.md:47`
  "The two instruments", `:49` "the evidence bar follows the instrument", `:102` "Say
  which instrument fits… grant-shaped de-risking or company-shaped", `:216`.
- **JZ:** replaced it with **maturity honesty** — `[[DEL:Locate it on the funnel. Say
  which stage you think the idea sits at]] [[INS:Be honest about where it sits. … Say how
  mature the work actually is and why — concept, proof-of-principle, or something already
  in hand]]`.

Both deletions agree. The replacements are **compatible in spirit but not identical**, and
applying JZ's text verbatim would overwrite a deliberate v3.1.0 design decision with a
different one. This is a semantic merge requiring adjudication, not a mechanical patch.

**Also:** three funnel references survive in JZ's final text at doc lines 21 ("The
funnel" heading), 94, and 233's tail — places JZ **did not touch**. Those are misses, not
endorsements. Applying the document's final state wholesale would **reintroduce funnel
language the repo deliberately removed.**

## Finding 5 — the phase-guidance edits are under a standing prohibition

The EXPLORE/DECIDE/MUST CONCLUDE edits (+7/−7) target `_PI_LAB` in
`src/agent/thread_guidance.py`, which CLAUDE.md pins to
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr` (164 KB) with:

> "do not reword them, and never run `pytest --snapshot-update` to make a mismatch go
> away."

Exactly one reviewed regeneration has occurred (2026-08-27), "executed at the operator's
direction with the `.ambr` diff audited hunk-by-hunk — every changed line belonged to that
one rewrite. Any future pi_lab change takes the same reviewed-diff path."

So these seven edits are integrable **only** via an explicitly operator-directed,
hunk-by-hunk-audited snapshot regeneration. They cannot ride along with the rest.

**E3 is not caught by this.** The prohibition names `_PI_LAB`; E3 targets `_SCOUT_HUB`,
which no snapshot pins.

## Finding 6 — the Phase B interaction, which is still benign

Phase B modifies the eight specialist personas (add `established`, add a `{stage_bar}`
placeholder, rename `blocking`/`caution`/`clear` → `blocking`/`gap`/`adequate`). The hub
doc reproduces those same eight personas.

**They are provably untouched.** Order-aware aligned diff of each persona section against
both `3cdb7f5` and current gives, for all eight:

```
repo-only words = 0      doc-only words = 2   (the doc's own "[Heading3] X specialist" title)
```

`identity.md` likewise: 1 doc-only word, its own heading. The one apparent exception,
`budget.md` (33 repo-only words), is the document **ending** before that persona's closing
`questions_to_ask` paragraph — the doc stops there; `most valuable field` appears 8 times
across the document, so no persona body is truncated mid-text.

So there is no three-way merge on the personas: **Phase B owns them outright and the
document has no competing claim.** The collision dissolves, on stronger evidence than the
first revision had.

The **ordering** still matters. A regenerated hub doc is only worth sending for review
*after* Phase B lands, or JZ reviews personas that are about to change again.

## Three integration strategies

**A — Apply each document's final text wholesale.** Rejected. For the hub doc this
reverts three rubric releases *and* re-applies never-committed pre-v2.1.0 draft notes. For
the pi doc it reintroduces three funnel references and overwrites the instrument framing.

**B — Apply JZ's tracked changes as a patch onto the current files.** Rejected as the
primary method. The base differs, so some hunks will not apply; several are already
satisfied by v3.1.0 (redundant); and Finding 4's hunks would silently replace the
instrument framing. A patch cannot distinguish "already done" from "conflicts with a
later decision" — and for the hub doc there are no tracked changes to patch *from*.

**C — Treat the redlines as *intent* and re-derive each change against current text.**
Recommended, and now applicable to **both** documents. For each edit, classify:

1. **already satisfied** by v3.x → skip, record as such;
2. **novel and compatible** → apply, adapted to current wording (E3 is adjudicated here);
3. **conflicts with a v3.x decision** (Finding 4) → adjudicate explicitly, do not
   silently pick;
4. **snapshot-pinned** (Finding 5) → hold for a separate operator-directed regeneration.

Cost: every edit needs a decision, and there are ~200. Benefit: it is the only method that
cannot silently revert a deliberate decision, which is the failure mode that matters here.

## Recommended sequence

1. **Phase B first**, on the eight personas — unblocked, no competing claim (Finding 6).
2. **Then the hub doc's E1–E4 via strategy C**, as four individually adjudicated edits
   against current text — small, and they are the only reviewer intent that exists in no
   other artifact.
3. **Then the pi doc via strategy C**, in three tranches: the system prompt (+131/−39),
   `phase5-new-post.md` (+18/−13), and `phase4-thread-reply.md` (+8/−6).
4. **The `_PI_LAB` edits last**, as their own operator-directed `.ambr` regeneration with
   a hunk-by-hunk audited diff.
5. **Regenerate the hub doc from the post-Phase-B repo** and send it back for review —
   noting that it was a stale export, and asking that future rounds be saved **with
   change-tracking on**, since this round's hub edits were recoverable only by
   full-history forensics.

## What needs an operator decision

1. **E1, E2, E4** — three prompt-content edits (E3 is adjudicated as compatible in
   Finding 1). Each is a judgement about what Blackbird tells its bots, not a merge
   mechanic. **Not applied.**
2. **Finding 4's conflict** — keep the repo's *instrument-fit* framing, adopt JZ's
   *maturity-honesty* framing, or combine them.
3. **The `.ambr` regeneration** — CLAUDE.md requires explicit operator direction.
4. **JZ's +43% expansion** — a prompt that grows by 43% changes token cost and dilutes
   emphasis. Worth confirming the whole expansion is wanted before it ships.

~~**Whether JZ made hub-side edits at all**~~ — **answered: yes, four of them, saved
without tracking.** See Finding 1.
