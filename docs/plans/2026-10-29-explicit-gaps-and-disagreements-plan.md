# Making a project close's gaps and disagreements explicit

**Goal:** a close answers two questions about itself in a shape that a copied template cannot
satisfy by accident, and spends the turns the thread was given.

**Spec:** `docs/specs/2026-10-22-idea-pitching-prototype-design.md`, as amended by D68–D74
(`docs/plans/2026-10-29-projects-outcomes-and-refusals-plan.md`).

## Why

Run `b70d0cff` (2026-10-29, the first on the D68–D74 code) finished 20 of 20 projects, all
Ready, with zero reply failures and zero truncation. Everything the previous RCA targeted was
fixed. But the verdict carried no information:

- all 20 closes wrote a bare `"disagreements": []`;
- 9 of 20 wrote a bare `"gaps": []`, including plans of 11k and 14k characters;
- not one ⚠️ marker appeared in 116 thread calls, against 35 in 204 the run before;
- the median close landed on turn 6 of 12, and plan depth tracks the close turn
  (turn 4: ~5,700 chars; turn 6: ~9,100; turn 8: ~10,200).

The cause is in the prompt, not the grader. `prompts/projects/hub-thread-reply.md` hands the
hub a skeleton that already contains `"gaps": [], "disagreements": []` and then says *"Never
write a note saying there are none."* An empty array is the template's default, so "explicit"
was satisfied by a default. The whole-entry classifier added for D73 (`_says_no_disagreement`
and its token sets) was never reached: zero entries were dropped in 20 closes, and the prompt
forbids writing the prose it exists to read.

## Decisions (owner, 2026-10-29)

- **D75.** The close answers the disagreement question with a boolean, not a list:
  `"disagreements": {"remaining": true|false, "note": "…"}`, with `"items"` when `remaining`
  is true. No lexical guessing. A close that omits the key, or whose `remaining` is not a
  boolean, has not answered — `STATUS_NOT_AGREED` with the D73 gap, unchanged.
- **D76.** `gaps` must name at least one open item and who must close it. This is a prompt
  requirement, **not** a grading rule: an empty `gaps` list still reads as "nothing open" and
  never changes a verdict, so D70 holds — gaps decide nothing. Compliance is unenforced by
  design; the next run measures whether instruction alone is enough.
- **D77.** BlackbirdBot may not close before turn 8 unless the lab has opened ✅ on its two
  most recent consecutive posts. From turn 8 a single ✅ is enough, as now. The must-close
  turn (`HUB_CLOSE_FROM`) is unchanged.
- **D78.** Runs `d54d7386` and `b70d0cff` keep the verdicts they were given. The new rule
  applies from the next run; both are replayed read-only before release as verification only.

## My calls

- **M18.** `remaining` alone decides. A missing or empty `note` is recorded as absent and does
  not change the verdict: the boolean is the answer, the note is the evidence, and a second
  formatting cliff would re-create the failure D73 was careful to avoid.
- **M19.** `remaining: true` with no usable `items` falls back to the note as the single item,
  and to a fixed line when there is no note either. "A disagreement remains" can never render
  as no disagreement.
- **M20.** The skeleton in the prompt carries `"remaining": true`. A verbatim copy therefore
  reads as a live disagreement and the project is Not agreed. The copy-paste default must
  fail, not pass — that is the whole lesson of `b70d0cff`.
- **M21.** The legacy list branch of `live_disagreements` stays, classifier included, for a
  close written by an older agent image during a mismatched deploy. It is unreachable under
  the new prompt, so the coyne-shaped reversal risk is bounded to that path.
- **M22.** The note is stored as its own `final["disagreement_note"]`, not folded into gaps:
  gaps answer "what is open", the note answers "why there is, or is not, a dispute".
- **M23.** When the lab agrees but D77 holds the close, the hub is told so and told what to
  spend the turn on. A ✅ with no instruction is how turn-6 closes happened.

## Tasks

1. `src/services/project_plan.py` — mapping branch in `live_disagreements`; new
   `disagreement_note`; docstrings state D75/M18–M21.
2. `src/agent/project_guidance.py` — `HUB_EARLY_CLOSE_FROM = 8`, `_HUB_HOLD_CLOSE`, and
   `project_turn(..., lab_agreed_before=False)` gating `may_close_early`.
3. `src/agent/engine/projects.py` — `_lab_agreed_before`; `_close_values` stores
   `disagreement_note`.
4. `src/agent/engine/reply_lane.py` — pass `lab_agreed_before` through.
5. `prompts/projects/hub-thread-reply.md` — new skeleton and the gaps, disagreements and
   turn-8 rules. `prompts/projects/lab-thread-reply.md` — do not open ✅ on a plan still
   missing an answer.
6. `src/services/projects_view.py`, `src/services/project_chat_record.py` — render and
   describe the note.
7. `templates/help/pages/projects.html`, `glossary.html`, `docs/operations/blackbird-hub.md`,
   `docs/specs/2026-10-22-idea-pitching-prototype-design.md` — state the rule.
8. Tests; `scripts/sync_prompt_set_docs.py`; full CI.

## Verification

- `ENGINE_REFUSAL_FALLBACK_ENABLED=false ./scripts/ci.sh` green.
- Read-only replay of `b70d0cff` and `d54d7386`: every close must still parse, and no close
  may move from Not agreed to Ready (the rule only gets stricter).
- The next run must start **Fresh**: the prompt snapshot loads once per start.

## Residual risk

- D76 is unenforced. If the next run again shows empty `gaps`, instruction alone is not
  enough and the penalty question (Not agreed, or a flag on the page) comes back.
- D77 raises cost: fewer turn-4 and turn-6 closes means more thread calls. `b70d0cff` cost
  $41.12 in thread and consult calls; expect closer to $60 at the same project count.
- M20 trades a silent pass for a loud failure. A hub that copies the skeleton but fills the
  note correctly is unaffected; one that copies it whole is Not agreed, by design.
