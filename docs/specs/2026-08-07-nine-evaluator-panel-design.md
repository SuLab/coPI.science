# The nine-evaluator panel: specialists as consultable advisors

**Date:** 2026-08-07
**Status:** Design, approved. Not implemented.
**Branch:** `blackbird`
**Source:** `BBL Evaluator Notes.docx` (stakeholder, 2026-08-07)

## 1. What the document asks for, and what is actually missing

The stakeholder document names nine "Potential Agentic Evaluators" — Commercial,
Clinical, Scientific, Chemistry, Legal, Technologic, Talent and Budget Specialists, plus
"Blackbird – the head agent that integrates perspectives".

The obvious reading is "we need nine agents". That is the wrong conclusion, and an
adversarial audit of the current rubric against the document says why: **six of the eight
specialist perspectives already exist**, collapsed into `profiles/private/blackbird.md`'s
weighted dimensions.

| Persona | Where it already lives |
|---|---|
| Commercial | dimension 1 (20%), dimension 9 |
| Clinical | dimension 2 (15%) |
| Legal | gating criterion, dimension 5 (10%), a red flag, and `search_prior_art` |
| Technologic | dimensions 6 and 7 |
| Talent | dimension 3 (15%), gating criterion |
| Budget | dimension 8 (5%), plus the check-size table in `profiles/public/blackbird.md` |
| **Scientific** | **nothing** |
| **Chemistry** | **nothing** |

The two with no representation are the two that decide real cases.

### Measured against the document's own 15 rejections

Themes derived from the document's "Project Examples Not Funded" section, counted:

| Theme | n/15 | Instrumentation today |
|---|---|---|
| Mechanism / target validation not established | **5** | one unweighted checklist line |
| Toxicity, selectivity, therapeutic index | **4** | one checklist line, and the DECIDE prompt omits it |
| Crowded landscape vs. **named** competitors | **4** | "not incremental" in the abstract; no tool can name a competitor |
| Chemistry maturity / path to a development candidate | **4** | **nothing** |
| Weak clinical value proposition / patient numbers | **4** | dimension 2 rewards *large* markets — polarity inverted |
| External expert consulted and killed it | 3 | dimension 4 counts expert validation only as a **positive** |
| Mouse→human translatability | 1 | **nothing** |
| Licensing / FTO encumbrance | 1 | a gate, a 10% dimension, a red flag, a tool, a DECIDE bullet |
| Team COI / over-commitment | 1 | dimension 3 is credentials-positive only |

Instrumentation is close to **inversely** correlated with rejection frequency. FTO decides
1 case in 15 and has five separate mechanisms; chemistry decides 4 and has none.

**The one-sentence finding: BBL rejects on science; the rubric scores commerce.** The
target-level scientific checklist (`profiles/private/blackbird.md` §4) is the only place
science lives, and it carries **zero weight** — it feeds no score, no band, and no column.
`RUBRIC_WEIGHTS` (`src/services/blackbird_rubric.py:31-41`) totals 100 points across nine
commercial dimensions; a purely scientific objection can move at most the 7 points of
`dev_regulatory_feasibility`.

This design does two things together, and neither works alone: it adds the missing
perspectives as consultable advisors (§2), and it gives their findings a dimension to land
in (§3). A panel whose verdict cannot move the score is consultation without consequence.

## 2. Architecture: one agent, eight consultable personas

`scout_hub` gains one tool:

```
consult_specialist(domain, question, context) -> opinion
```

`domain` is an enum of eight values. Dispatch loads `prompts/specialists/{domain}.md`,
makes one synchronous LLM call with that persona and the supplied context, and returns a
structured opinion.

**Why a tool and not agents.** Slack agents take turns asynchronously — the hub cannot post
a question and receive an answer inside its own turn. On-demand consultation is only
possible synchronously, and the tool layer is the only synchronous seam the agent has.
Everything else follows from that: no `AgentRegistry` rows, no Slack tokens, no cohort or
topology changes, no roster sync, no new container, no migration.

It also reuses machinery that already exists and is already enforced two ways:
`tools_for_role` filters what the model sees (`src/agent/tools.py:128-131`) and
`execute_tool` refuses at dispatch (`:148-150`), both keyed on `role.toml`'s `tools` list.

**Auditability is free.** The agentic loop appends `tool_use` and `tool_result` blocks to
the message list (`src/services/llm.py:417-427`), and the whole list is persisted to
`llm_call_logs.messages_json`. Every consultation — domain, question, and opinion — is
already on the record without a new table. A dedicated `specialist_opinions` table and an
admin view would be nicer to query; they are not needed for correctness and are out of
scope here.

### The eight specialists

| Domain | Owns | Consult when |
|---|---|---|
| `scientific` | rigor, controls, statistical power, interpretability, translatability | any experimental claim |
| `chemistry` | path to a development candidate, medchem tractability, tolerability, in-family off-targets | chemical matter or a modality choice |
| `clinical` | unmet need against current standard of care, indication choice, patient numbers | any disease claim |
| `commercial` | crowded landscape, named competing programs, deal comps | any differentiation claim |
| `legal` | FTO, licensing, research-tool encumbrance | IP or third-party materials |
| `technologic` | platform feasibility, whether the project's output tests it | any platform claim |
| `talent` | execution probability, conflicts of interest, over-commitment | at conclusion, always |
| `budget` | scope against the $300K–$847K grant band and 12–24 month duration | any workplan or cost claim |

The Scientific and Chemistry personas carry the vocabulary the audit found absent
everywhere in the hub's current instructions: `control`, `adequately powered`,
`interpretable`, `benchmark`, `translatab`, `off-target`, `tolerability`,
`competitive landscape` — all zero occurrences today across the rubric and every
`scout_hub` prompt.

### Opinion contract

```json
{
  "domain": "chemistry",
  "verdict_signal": "blocking | caution | clear",
  "concerns": ["..."],
  "questions_to_ask": ["..."],
  "confidence": "high | moderate | low"
}
```

`questions_to_ask` is the field that makes mid-interview consultation worth more than a
post-hoc review: it feeds the hub's next question to the PI.

## 3. Scoring: give the specialists somewhere to land

A floor that mandates consultation but gives its answer no route to the verdict is
consultation without consequence — the Chemistry specialist could return
`verdict_signal: "blocking"` and the hub could still emit `advance` at 4.2, because no
dimension exists where a chemistry objection can be recorded. That is the same failure mode
as a prompt that merely asks nicely, one level up. So the panel and the score change
together.

### Four new dimensions

Each dominant rejection theme gets a dimension:

| Dimension | Covers | Theme freq. | Specialist |
|---|---|---|---|
| `mechanism_validation` | target validation, proof of mechanism, contradictory literature | 5/15 | `scientific` |
| `toxicity_selectivity` | on-target liability, in-family off-targets, therapeutic index | 4/15 | `chemistry` |
| `chemistry_dc_path` | medchem tractability, path to a development candidate | 4/15 | `chemistry` |
| `experimental_rigor` | controls, statistical power, interpretability, translatability | the whole "don't want" list | `scientific` |

### Weights

Science ~40%, commerce ~60%, approved by the stakeholder. `ip_fto` drops from 10 to 6: it
decides 1 rejection in 15 and already carries a gating criterion, a red flag and a dedicated
tool besides, so its weight was double-counting an over-instrumented concern.

```python
RUBRIC_WEIGHTS: dict[str, int] = {
    "differentiation": 15,
    "mechanism_validation": 12,      # new
    "market_unmet_need": 12,
    "experimental_rigor": 10,        # new
    "toxicity_selectivity": 10,      # new
    "team": 10,
    "chemistry_dc_path": 8,          # new
    "external_signals": 8,
    "ip_fto": 6,
    "platform": 4,
    "dev_regulatory_feasibility": 3,
    "workplan_capital_efficiency": 1,
    "exit_thesis": 1,
}
```

Science total = 40. Commerce total = 60. Sum = 100.

The numbers are a first cut derived from rejection frequency in a single document. They are
expected to move once real assessments exist; the *dimensions* are the durable part.

### Why this is cheap right now, and expensive later

- **`scores` is JSONB** (`src/models/opportunity.py`), keyed to `RUBRIC_WEIGHTS`. Adding
  dimensions needs **no migration**.
- **Band thresholds are unaffected.** `_BAND_THRESHOLDS = (3.0, 4.0)`
  (`src/services/blackbird_rubric.py:49`) apply to the normalised 1–5 score, and
  `_TOTAL_WEIGHT = sum(RUBRIC_WEIGHTS.values())` (`:45`) recomputes itself, so the score
  stays on the same scale and `advance`/`conditional`/`pass` keep their meaning.
- **Production has zero `opportunity_assessments` rows.** Nothing is invalidated. Once rows
  exist, changing weights makes historical `weighted_score` values incomparable and
  silently reorders the triage queue — so this is the cheapest moment there will ever be.

### Blast radius

| File | Change |
|---|---|
| `src/services/blackbird_rubric.py:31-41` | the dict above |
| `tests/unit/test_blackbird_rubric.py:7-18` | pins the dict and the sum; both must be updated |
| `profiles/private/blackbird.md` §3 | the dimension table the model reads |
| `profiles/private/blackbird.md` §6 | the `scores` skeleton |
| `prompts/roles/scout_hub/phase5-new-post.md` | the `scores` contract in the output format |
| `src/routers/admin.py:930` | passes `RUBRIC_WEIGHTS` to the template — no code change, but the assessments page will render 13 rows instead of 9 |

`weighted_score` already warns and scores 0 for any key not in `RUBRIC_WEIGHTS`
(`blackbird_rubric.py:103-112`), so a model emitting the old 9-key `scores` object during
rollover degrades to a low score rather than crashing — but it *will* score low. The rubric
and the prompt must therefore land in the same change as the weights.

> **⚠️ `profiles/` is not in version control.** `.gitignore:36` (`profiles/**/*.md`) has
> excluded it since commit `18dfc17`, and `git ls-files profiles/` returns zero. The rubric
> edits above cannot be committed, reviewed, or rolled back through git; they are applied
> directly to the production host's disk, which is root-owned and needs `sudo`. This is a
> pre-existing policy decision, not something this design changes — but it means the most
> consequential document in the screening system has no history. Worth revisiting
> separately.

### Wiring the specialist to the dimension

A specialist's `verdict_signal` does not *set* a score — the hub still scores, because only
the hub has the whole picture. But the mapping in the table above is stated in the phase-5
prompt so the hub knows where a given specialist's concerns belong, and a `blocking` signal
from a specialist whose mapped dimension is scored 4 or 5 is a contradiction the hub must
explain in its `rationale`.

## 4. The enforced floor

Prose triggers make consultation happen. Code makes it non-optional where money is at stake.

In `_persist_assessment` (`src/agent/simulation.py:2545`), when `recommendation` is
`advance` or `conditional`, the domains the idea actually touches must have been consulted
in that thread. If they were not, the verdict is **rejected with a WARNING and not
persisted** — the same shape as an unreachable `tagged_agent` blocking a post today.

`pass` and `route-to-incubation` require no panel. Cheap ideas stay cheap; only ideas
heading toward real money must face the specialists.

**Required domains are derived from the verdict's own content**, not from the hub's opinion
of what it needed:

| Condition | Required |
|---|---|
| always | `scientific`, `talent` |
| the verdict names chemical matter, a compound, or a modality | `chemistry` |
| the verdict names a disease or indication | `clinical` |
| `gating.fto_achievable` is `met` | `legal` |
| the verdict claims a platform | `technologic` |

Deriving requirements from the verdict rather than from a hub self-report is the whole
point: a hub that skipped Chemistry cannot also declare Chemistry unnecessary.

### Where the consultation record comes from

The floor needs to know which domains were consulted during the thread. Two options, in
preference order:

1. **In-turn accumulation.** `execute_tool` records each consult on the `thread_state`
   object it already receives (`src/agent/tools.py:138`). Zero storage, but the record dies
   with the turn — and the assessment is a *separate* phase-5 turn from the phase-4
   interview, so this alone is insufficient.
2. **Per-thread counter on the engine**, mirroring `self._cohort_tags_stripped` and
   `self._post_type_rejections`: `self._specialist_consults: dict[str, set[str]]` keyed by
   `thread_id`. In-memory, lost on restart, which is acceptable — a restart already loses
   in-flight turns, and the floor failing open after a restart is better than blocking every
   assessment for threads that predate it.

Option 2 is the design. A restart clears the map, so the first assessment on a resumed
thread is exempt; log that explicitly rather than silently.

## 5. Data flow

```
Phase 4 interview turn
   └─ hub reads specialist trigger text in the tool description
        └─ consult_specialist(domain, question, context)   [synchronous, in-turn]
             ├─ load prompts/specialists/{domain}.md
             ├─ one LLM call, small context
             ├─ record domain in self._specialist_consults[thread_id]
             └─ return opinion -> shapes the hub's next question to the PI

Phase 5 assessment turn
   └─ hub emits the <assessment_json> verdict
        └─ _persist_assessment
             ├─ derive required domains from the verdict's content
             ├─ required - consulted == empty?  ── no ──► REJECT, WARNING, do not persist
             └─ yes ──► existing persistence path, unchanged
```

## 6. Cost, measured

From `llm_call_logs` on the live database, `blackbird` only:

| Phase | calls | avg input | avg output |
|---|---|---|---|
| `thread_reply` (interview turn) | 138 | 23,989 | 840 |
| `new_post` (assessment) | 89 | 22,301 | 504 |
| `scan` | 68 | 8,039 | 123 |

A consult is far smaller than a hub turn: it receives the relevant transcript slice and one
persona file, not the hub's full system prompt (public profile + private rubric + working
memory + lab directory). Estimate ~4k input / ~600 output.

Two or three consults across an interview, plus a two-to-four-domain floor at conclusion,
is **5–7 additional calls per assessed idea** and **zero** for the majority of interviews
that end without an assessment. Against a 12-turn interview at ~290k input tokens, that is
roughly a 10% increase on the ideas that reach a verdict.

### The constraint that actually bites: `max_tool_rounds`

`generate_agent_response_with_tools` defaults to `max_tool_rounds: int = 5`
(`src/services/llm.py:305`), and the loop is `for round_num in range(max_tool_rounds + 1)`
(`:336`). The hub already has four tools competing for those rounds — `retrieve_profile`,
`retrieve_abstract`, `retrieve_full_text`, `search_prior_art`.

A four-domain floor plus a prior-art search plus a profile lookup can exhaust the budget. A
round may contain several `tool_use` blocks, so a model that batches its consults fits
comfortably; a model that serialises them does not.

**Resolution:** raise `max_tool_rounds` for the `scout_hub` phase-5 turn specifically, and
have the phase-5 prompt instruct the hub to request all outstanding consults in **one**
round. Do not raise it globally — a higher cap on every `pi_lab` turn is a cost regression
for 55 agents to fix a problem one agent has.

## 7. Error handling

Every failure mode fails toward "no verdict" rather than "a verdict nobody vetted", except
where that would silence the hub entirely.

| Condition | Behaviour |
|---|---|
| Unknown `domain` | Tool returns an error string naming the valid domains; no LLM call. Mirrors `execute_tool`'s existing refusal style. |
| `prompts/specialists/{domain}.md` missing | Return an error string, log ERROR, do **not** record the domain as consulted. A missing persona file must not satisfy the floor. |
| Specialist LLM call fails or times out | Return an error string the hub can see; do **not** record the domain as consulted. |
| Specialist returns unparseable JSON | Pass the raw text through as the opinion; record the consult. A prose opinion is still an opinion; only a *failed* call is not. |
| Floor unsatisfied on `advance`/`conditional` | Reject, WARNING naming the missing domains, do not persist. |
| Floor unsatisfied on `pass` | Persist normally. No panel required. |
| `thread_id` absent from `_specialist_consults` (post-restart) | Fail **open**, persist, log INFO naming the reason. Blocking every assessment on a resumed thread is worse than one unvetted verdict. |
| `consult_specialist` called by a `pi_lab` agent | Refused at dispatch by the existing allow-list. No new code. |

## 8. Testing

1. **Tool gating** — `consult_specialist` is in `scout_hub`'s allow-list and absent from
   `pi_lab`'s; a `pi_lab` dispatch is refused. Extends `tests/unit/test_tool_gating.py`.
2. **Domain enum** — an unknown domain returns an error and makes no LLM call.
3. **Floor arithmetic, table-driven** — one case per row of §4's requirement table, and one
   where every required domain was consulted and the verdict persists.
4. **Floor rejects** — an `advance` verdict naming chemical matter with no `chemistry`
   consult is not persisted, and the WARNING names `chemistry`.
5. **`pass` needs no panel** — a `pass` verdict with zero consults persists.
6. **Fail-open after restart** — an empty `_specialist_consults` map persists the verdict
   and logs the reason. This one matters: it is the difference between a degraded system and
   a stopped one.
7. **A failed consult does not satisfy the floor** — missing persona file, and LLM error.
8. **`max_tool_rounds`** — a phase-5 turn requesting four consults in one round completes.
9. **No `pi_lab` regression** — a `pi_lab` phase-4 turn's tool list and round budget are
   byte-identical to today.

## 9. Out of scope, recorded

Found in the same audit, deliberately not addressed here:

- **An `open_questions` field.** Rejections 1 and 2 in the document both use the formula
  "significant questions remain on our side about X, Y and Z". The schema has `red_flags`
  (assertions) and `suggested_derisking_milestones` (actions) but no slot for unresolved
  questions — the actual shape of a real BBL pass. Needs a migration.
- **Few-shot exemplars.** Three graduated deals and 15 rejection rationales are the best
  calibration data available and are used nowhere. No prompt in `prompts/roles/scout_hub/`
  contains a worked example.
- **Dimension 7's inverted polarity.** It rewards "precedented modality"; EGFRviii ADCs were
  rejected *because* the precedent failed in clinic. Nothing distinguishes precedent that
  succeeded from a well-trodden graveyard.
- **`search_prior_art` is the wrong instrument for the dominant theme.** Crowded-landscape
  rejections name clinical and corporate programs — Tango, Resmetirom, AbbVie/Amgen ADCs,
  CUE-401. A title-only USPTO search reaches none of that. The Commercial specialist will
  reason from model knowledge, which is weaker and staler than a real landscape search. A
  clinicaltrials.gov or company/asset tool is the real fix.
- **Transcription drift from the source PDF.** `data/Blackbird_initial_priorities-criteria_v1.pdf`
  dimension 2 reads "…downstream actionability; standard-of-care gap"; the rubric dropped
  "downstream actionability". Dimension 7 reads "feasible timeline with current tools"; the
  rubric dropped "with current tools" — the closest thing it had to a technical-feasibility
  check.
- **Check sizes live in the *public* profile.** `profiles/public/blackbird.md` carries the
  grant bands and is injected under "## Your Lab Profile (Public)" with no confidentiality
  marking, so the hub may volunteer Blackbird's economics to a PI mid-interview. Probably
  acceptable; worth a deliberate decision.

## 10. What this does not change

`pi_lab` behaviour, the cohort gate, the post-type allow-list, the roster, the Slack
topology, the database schema, and `pi_lab`'s scoring path (no pi_lab agent emits a rubric verdict). The panel is additive: with
`consult_specialist` absent from a role's allow-list, every other agent behaves exactly as
it does today.
