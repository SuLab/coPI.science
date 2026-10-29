# Projects: what "Ready" means, and refusals that destroy projects — Implementation Plan

> **Revision 3**, 2026-10-29. Revision 1 was audited (6 blockers), rewritten as revision 2 after the
> owner's brainstorm (D68–D74), audited again (3 blockers, 16 mediums). This revision resolves both
> audits; the mapping is in "Audit history" at the end. Executed through `/lathe:plan-execution`:
> packages implemented in parallel without building or testing, one integrated verification, an
> adversarial audit of the code, commit, release. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make a project's outcome measure the plan rather than the two bots' private reading of the
word "complete", and stop a model refusal from destroying a project.

**Architecture:** (A) The outcome reads two things from the close — are the nine substantive plan
fields filled, and is a live scientific disagreement recorded — and nothing else; the ✅/⚠️ markers
become commentary. (B) A refusal no longer destroys a project: a refused lab reply is posted like a
refused thread reply and counted when it yields nothing; a refused thread turn backs off on its own
counter, bounded so the thread still ends. (C) One tool-marking computation, written at the close and
read by the page. (D) Prompt text: define complete, stop chasing what neither bot controls, answer the
disagreement question outright, add no new requirement in the last two turns. (E) The help pages and
glossary stop describing the old rule.

**Tech Stack:** Python 3.12 asyncio, SQLAlchemy 2 (async), Jinja2, pytest (+ testcontainers Postgres).
No migration, no schema change.

**Spec:** `docs/specs/2026-10-22-idea-pitching-prototype-design.md` §5.5, §5.7, §5.8, §5.9, amended by
Task 9. Evidence: the RCA below, measured read-only from run d54d7386.

---

## Decisions (owner, 2026-10-29)

- **D68 — Ready means fundable now, for a prototype.** A plan a PI could be handed and funded on, not
  one with nothing left to arrange. What happens downstream is not this system's concern.
- **D69 — Only two things block Ready:** a substantive plan field that is not filled, or a live
  scientific disagreement recorded at the close.
- **D70 — The ✅/⚠️ markers are commentary.** Displayed, never deciding. Neither bot's marker nor the
  lab's last-message position enters the verdict.
- **D71 — Items neither bot controls never block and are not chased.** Work Blackbird has taken on,
  figures only the institution can confirm, vendor terms, archive access, decisions belonging to the
  PI: recorded as gaps, never pursued across turns.
- **D72 — Time and budget is exempt from the completeness check.** Recorded and shown; staff settle
  it. The other nine fields block.
- **D73 — BlackbirdBot answers at the close whether a scientific disagreement remains**, so an empty
  list is an answer rather than an omission.
- **D74 — Threads stay at twelve turns**, and the room freed by D71 goes to the science.

On this run's data, 18 of the 19 closed projects become Ready, against 6 today.

### My calls (the owner may overrule any at review)

- **M5 — `gaps` stays one flat list.** Splitting it by owner needs a new stored shape and eight
  consumers for no gain under D69. The prompt asks that each gap name who must close it, and that
  items settled earlier or deferred to a later tranche are left out.
- **M6 — the scope freeze is prompt text, not a new turn band.** A band at ordinal 10 would be
  invisible to `scripts/sync_prompt_set_docs.py` (it samples the hub at 2, 6 and 12) and would collide
  with the early-close paragraph.
- **M7 — revision 1's "let the lab answer the close" is dropped**: its purpose was to stop the lab
  certifying a plan it had not read, and under D70 that certification decides nothing.
- **M8 — the page reads the stored `final.tool_marks`** when the close recorded both a `matched` key
  on every entry and `tool_profile_seen: true`; otherwise it recomputes. The flag is what stops a
  close written with an empty in-memory profile from freezing an all-unmarked list onto the page.
- **M9 — `_record_project_close` loads the project's `choice`** in the session it already opens, to
  get the anchors the tool matcher needs.
- **M10 — the placeholder screen splits in two.** Deferral phrases ("to settle with the lab", "to be
  settled with the lab", "tbd", "to be determined") make a field unfilled when the value *starts* with
  one; null phrases ("none", "n/a", "unknown", "-", "?") only on exact equality, so §5.5's
  "none exists, because …" stays filled (`tests/unit/test_project_plan.py:161`).
- **M11 — a close whose `disagreements` key is absent or is not a list is unreadable.** That is how
  D73 is enforced: `[]` is the answer "none", silence is not an answer.
- **M12 — a refused thread turn uses its own counter** (`ThreadState.refusal_count`), with the thread
  abandoned at `PROJECT_REFUSALS_MAX` so it still ends and a wind-down still drains.
- **M13 — a truncated close is graded when its blocking fields are filled**, and recorded
  `close_unreadable` only when the block is missing, unreadable, or short of a blocking field.
- **M14 — `missing_fields` gains a `fields` parameter** rather than growing a sibling, so no function
  in `src/` is left without a caller (`tests/unit/test_no_dead_src_symbols.py`).
- **M15 — `project_outcome` returns `(status, gaps, disagreements)`** and `_close_values` stores the
  disagreements it returns, so a Ready project never renders a Disagreements box and an unreadable
  value is preserved rather than dropped.
- **M16 — the displayed label "Not agreed" is kept.** Under D69 it also covers an unfilled field, so
  the help text and glossary define it by the new rule. Renaming the status is an owner call this
  plan does not take: the value is stored in `hub_projects.status` and read by the page, the report
  and the chat record.
- **M17 — `final["hub_position"]` and `final["lab_position"]` keep today's normalisation** to exactly
  `"agree"` / `"disagree"`, so `_position_label` keeps rendering them.

---

## RCA — run d54d7386 (2026-10-28, 14:06–18:42 UTC, projects only, count 20, stopped by the owner)

Measured read-only. 22 project rows (three labs got a second slot), 19 closed with a `final`, 536
thread model calls, 924 research calls, about $153.

### The 2026-10-28 fixes held
Zero thread time-outs in 19 threads (13 ran the full 12 turns at 22–30 Slack messages, 6 closed early
on agreement); the close repair fired in production and saved vogelstein (agent log 16:35:11); research
ran three lanes throughout with no double claim.

### F1 — "complete" is undefined, and every ⚠️ in the run was about something neither bot controls
In the five threads read end to end no ⚠️ was a scientific disagreement and every close had all ten
fields filled. bergles 14:45: *"three things remain open and none of them is design: no compound named
or in hand …, no bioanalysis quote, and the indirect rate is not mine to state"* — against the hub's
own 14:38 *"The contracted bioanalysis I'll carry on Blackbird's side … don't source it yourself"*.
bergles 14:39: *"'complete' would be the wrong word"*. The labs that reached ✅ drew the opposite
distinction (wilson 17:03 *"tasks inside the plan, not disputes"*; konig 16:51 *"complete as a
design"*). `prompts/projects/lab-thread-reply.md:30` says "feasible and complete" and defines neither;
§5.5's definition reaches neither prompt. 5 of the 13 Not agreed are the lab on this basis, 7 are
BlackbirdBot on the same basis (sarma's close: *"not because the science is wrong … Everything
scientific and financial, I think, is settled — and settled well"*). **Fixed by D68–D72, Tasks 1 and 7.**

### F2 — the marker tracks turn position, not plan state
wilson's lab withheld ✅ at 16:49 over the capsid supply, wrote ⚠️ at 16:58 with it still unquoted,
then ✅ at 17:03 with nothing changed; `LAB_FINAL_FROM = 9` is what moved. **Fixed by D70.**

### F3 — a prose disagreement that says there is none still blocks
`project_outcome` refuses `ready` when `disagreements` is non-empty (`src/services/project_plan.py:445`).
coyne closed with both markers ✅, `position: agree`, ten fields filled, and one string beginning
*"None outstanding on the science. Resolved during refinement, recorded for context: …"* →
`not_agreed`. **Fixed by Task 1.**

### F4 — a non-list `disagreements` reads as "none", and the text is dropped from the record
`_strings` (`project_plan.py:450`) returns `[]` for a non-list, so `"disagreements": "the lab cannot
run the NHP arm"` would pass the readiness test, and `_close_values` (`projects.py:1113-1116`) drops
the text from `final` as well. Not observed; latent. **Fixed by M11 and M15.**

### F5 — the placeholder screen is exact-match
`field_filled` rejects a value only when its whole normalised text is in `PLACEHOLDERS`
(`project_plan.py:52,230`). kass was graded correctly (its `time_and_budget` was exactly the
placeholder), but "To settle with the lab (likely $150K)" or "tbd — pending the quote" count as
filled. **Fixed by M10.** Under D72 the kass case no longer blocks anyway.

### F6 — the hub prompt instructs the model to write the placeholder
`prompts/projects/hub-thread-reply.md:40`: *"Write 'to settle with the lab' for any field still
open"* — the exact string the screen rejects. **Fixed by Task 7.**

### F7 — a refused lab reply is thrown away and retried forever
`_compose_project_reply` (`projects.py:870`) returns None whenever `_was_truncated(stop_reasons)`, true
for `refusal` as well as `max_tokens` (`src/services/llm.py:610`). jbailey's lab produced a well-formed
reply six times over four hours (253–613 output tokens, each opening `<slack_message> :bulb: Reply on
the HBV envelope panel project …`), each discarded. Outside a wind-down this counts nothing
(`projects.py:791`), and no tick step ends a `posted` project whose lab is live
(`_end_stale_projects:428-441` covers only `lab_not_live`), so it held a lab slot until the operator
stopped the run. In a thread the identical condition posts the text with `TRUNCATION_NOTICE`
(`reply_lane.py:1337-1343`). **Fixed by Task 3.**

### F8 — two refused thread turns abandon a thread three turns from its close
`_handle_project_reply_response` (`reply_lane.py:1307`) counts an empty response without consulting
`stop_reasons`, which it already holds, and the project branch collects no `provenance`. antiochos
slot 1 lost turn 8 twice and closed `abandoned` at turn 8 of 12. `empty_response_count` clears on a
new partner reply (`reply_lane.py:210`) or a landed post (`:1211`), neither of which a refused turn
produces, so the strike cannot clear itself while the partner waits. **Fixed by M12 and Task 4.**

### F9 — two tool matchers disagree and the stored one is wrong
The engine stores `final.tool_marks` from `tool_marks` → `match_anchor`, which needs full-string
equality (`project_plan.py:246,126`); the page ignores it and recomputes `display_tool_marks`
(`projects_view.py:1131`), a whole-word containment match. Over the same 19 projects: stored 8 of 210
(3.8%), page 23 of 210 (11%). The close prompt never asks for a verbatim copy, unlike `choose.md:21`.
coyne's "pan-expansion microscopy (pan-ExM) with machine-learning-facilitated NPC segmentation
(contingent, requires re-training…)" is unmarked although the posting's "Builds on:" line carries that
text. **Fixed by M8 and Task 5.**

### F10 — a posted close whose row has moved is lost entirely
`_record_project_close` updates `WHERE reply_ts = … AND status = 'refining'`
(`projects.py:1070-1083`); on zero rows it logs and returns None while the thread still closes
`project_closed` (`reply_lane.py:1347-1352`). An operator's Stop now during the hub's close call leaves
a ✅ close on Slack with no `final`. Not observed; latent. **Fixed by Task 6.**

### F11 — a truncated close is graded as if complete, in one narrow case
The close path (`reply_lane.py:1347`) does not consult `stop_reasons`. The case is **narrower than
revision 2 claimed**: `_PROJECT_JSON_RE` (`sidecar.py:193`) needs the closing tag, so a cut that lands
mid-sidecar yields no block at all and is already `close_unreadable`. What remains reachable is a cut
landing *after* `</project_json>`, where the block parses and the visible text is incomplete, and a
`max_tokens` retry that returns a short second pass. **Fixed by M13 and Task 2**, which is cheap and
closes the case rather than leaving it to chance.

### F12 — settled topics are reopened, and a refused question is asked three times
`src/agent/project_guidance.py:79-83` names controls, standard of care, readout and budget as standing
REFINE topics with no latch. The hub asked bergles' lab for its indirect rate at 14:38, 14:41 and
14:44; the lab refused at 14:39, 14:42 and 14:45 and then listed it as a ⚠️ reason. sarma's budget was
declared settled at 15:56 and again at 16:03. Roughly 3 of 12 turns. **Addressed by D71 and Task 7**
(stop chasing); the standing-topic text itself stays, since under D74 the turns go to the science.

### F13 — the hub adds required scope inside the final band
bergles ordinal 10: *"Controls — five additions"* plus a new laminar covariate, accepted by the lab in
the same message it ⚠️'d. wilson ordinal 11 added a terminal immunisation. **Addressed by M6 and
Task 7.**

### F14 — `gaps` mixes five kinds of thing
The model's open items; the code's "The lab did not agree" / "BlackbirdBot did not agree"
(`project_plan.py:441-444`); items settled earlier (bergles records a telemetry gap the hub retired at
14:34); items deferred by design; items never asked of the lab; and one dependency split three ways
(sarma's vendor). 13.8 gaps per not-agreed project. **Partly fixed:** the code's verdict lines go
(Task 1), and the prompt excludes settled and deferred items (M5, Task 7). The triple-split and
"never asked" classes are **deferred** — drafting quality, no failure path.

### F15 — refusals are frequent and mostly absorbed
19 specialist consults returned nothing; 3 research or choice steps were refused (`refusal:bio`),
ending casadevall slot 1 `choice_failed`; 6 lab replies and 3 hub thread turns were refused. Only 2 of
20 projects were lost, through F7 and F8. The refusal rate is a model-side fact this plan does not
change.

### Not defects
- kass's grading was correct under the old rule; D72 changes the rule.
- The failure stop never fired; research concurrency, the lease and post counting behaved.
- The hub reading its own marker from the logged post (`projects.py:1094-1095`) stops deciding
  anything under D70, so it is retired rather than fixed (Task 2).

---

## Global Constraints

- No migration; schema head stays `0076`. The only new stored keys are `truncated`,
  `tool_profile_seen` and `matched` (inside `tool_marks`) in `final`; `truncated` and `matched` are
  already covered by `src/web/presentation.py`'s `VIEW_FIELDS`, and `final` reaches the page through
  the existing `JSON_FIELDS` passthrough — **the implementer confirms this with
  `grep -n "truncated\|matched\|tool_profile_seen" src/web/presentation.py` and registers anything
  missing.**
- `prompts/projects/` belongs to no role's prompt set: editing it bumps no `role.toml` and the
  interview prompt sets stay frozen (`src/agent/CLAUDE.md`). After any edit under `prompts/`, run
  `.venv-test/bin/python scripts/sync_prompt_set_docs.py` and then `--check`; it rewrites
  `docs/specs/2026-10-27-projects-prompts.md`, which is committed with the change.
- `src/agent/project_guidance.py`'s band strings are **not** edited (M6), so
  `scripts/sync_prompt_set_docs.py::project_code_blocks` keeps finding what it expects.
- Interviews unchanged: no edit to `thread_guidance.py`, `phase4_guidance`, the interview branch of
  `_handle_reply_response`, or the `_PI_LAB`/`_SCOUT_HUB` strings. Never `pytest --snapshot-update`.
- Help content follows `docs/operations/help-content.md`: exact UI wording, no numbers beyond the
  1–5 rating and the 10,000-character comment limit, no real names, no repository internals. No help
  screenshot sources a help page (every `manifest.json` entry sources workspace templates plus
  `base.html` and `help/_link.html`), so Task 8 stales no shot; `capture --check` proves it.
- Before editing an engine method, `grep -rn getsource tests/` for it.
  `tests/unit/test_slack_post_landed_check.py` counts `landed_check=True` in `reply_lane.py` (keep it
  at one) and `tests/unit/test_reply_lane.py` runs an AST lock-order scan over `src/agent/engine/*`.
- `tests/unit/test_no_dead_src_symbols.py`: every function in `src/` needs a caller outside `tests/`.
- `tests/unit/test_engine_import_graph.py` pins method ownership and re-exports every name in
  `constants.__all__`; `PROJECT_REFUSALS_MAX` therefore lives in `src/agent/project_guidance.py`
  (dependency-free, already imported by the reply lane), not in `constants.py`.
- Host commands: git, pytest and scripts on the host
  (`ssh -4 -o StrictHostKeyChecking=yes ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com`,
  `cd ~/blackbird-copi-science`); pytest as `.venv-test/bin/python -m pytest …`; CI as
  `ENGINE_REFUSAL_FALLBACK_ENABLED=false ./scripts/ci.sh`.
- Never start a simulation; never run the `agent` service without an explicit command. The agent image
  is recreated only when `/workspace/simulation` shows no live run. Never push; never stage
  `docker-compose.prod.yml`.
- This plan file is committed with the change, so it needs no
  `tests/unit/test_claude_md_references.py` ALLOWLIST entry; `test_allowlist_entries_are_needed`
  would fail on one.
- Commit as `git -c user.name=alan -c user.email=alan@hueb.org commit` with the session's two
  trailers.

## Review Focus

1. **A close whose `disagreements` is a non-empty string** must not read as Ready, and its text must
   survive into `final` (F4). Task 1, Task 2.
2. **"none exists, because no drug is approved"** stays filled; "To settle with the lab (likely
   $150K)" does not (M10, §5.5). Task 1.
3. **A lab reply refused on every attempt** must end its project after two attempts that yield
   nothing, and a refused reply whose text is usable must be posted (F7). Task 3.
4. **A thread whose hub turn is refused** must not spend an empty-reply strike, must still end at the
   cap, and must not leave the project `refining` so a wind-down cannot drain (F8, M12). Task 4.
5. **A tool string containing a profile item plus a qualifier** must mark in-profile; the page and the
   stored record must agree; and a close written with no profile in memory must not freeze an
   all-unmarked list onto the page (F9, M8). Task 5.
6. **A Ready project must never render a Disagreements box** (M15). Task 2, Task 8.

---

## Package A — the outcome rule

### Task 1: `project_outcome` reads the plan, not the markers

**Files:**
- Modify: `src/services/project_plan.py`
- Test: `tests/unit/test_project_plan.py`

**Interfaces:**
- `DEFERRALS = frozenset({"to settle with the lab", "to be settled with the lab", "tbd",
  "to be determined"})`; `NULLS = frozenset({"none", "n/a", "unknown", "-", "?"})`;
  `PLACEHOLDERS = DEFERRALS | NULLS` stays, for any importer.
- `_is_placeholder(value: str) -> bool`: true when `normalize_item(value)` is in `NULLS`, equals a
  `DEFERRALS` phrase, or starts with a `DEFERRALS` phrase followed by a character that is not a letter
  or a digit. `field_filled` uses it.
- `BLOCKING_FIELDS: tuple[tuple[str, str], ...]` — `TEMPLATE_FIELDS` without `time_and_budget` (D72).
- `missing_fields(plan, fields=TEMPLATE_FIELDS)` gains the parameter (M14); `project_outcome` calls
  `missing_fields(plan, BLOCKING_FIELDS)`. No sibling function is added.
- `live_disagreements(value: object) -> list[str] | None`: `None` when `value` is absent or not a list
  (M11); otherwise the entries that name a live dispute. An entry is dropped when
  `normalize_item(entry)` is in `NULLS`, or when the tokens of its **first clause** (up to the first
  `.`, `;`, `:` or `-`, or the whole string) are all drawn from
  `{"none", "no", "nothing", "not", "outstanding", "remaining", "remain", "remains", "known",
  "disagreement", "disagreements", "dispute", "disputes", "on", "the", "science", "scientific"}`.
  So "None outstanding on the science.", "No disagreements remain.", "Nothing outstanding." and
  "None known." all drop, while "None of the proposed controls is feasible in our hands" survives
  ("of", "proposed" are not in the set).
- `project_outcome(*, close: Mapping) -> tuple[str, list[str], list[str]]` — **`lab_position` and
  `hub_marker` leave the signature** (D70), and it returns `(status, gaps, disagreements)` (M15).
  - `disagreements` is `live_disagreements(close.get("disagreements"))`, or — when that is `None` —
    `[str(close["disagreements"])]` for a non-empty string value and `[]` otherwise, so nothing is
    lost (F4).
  - `STATUS_NOT_FINISHED` when `live_disagreements(...)` is `None`, with the gap
    `"The close's disagreements could not be read"`.
  - `STATUS_READY` when the blocking fields are all filled and `disagreements` is empty.
  - Otherwise `STATUS_NOT_AGREED`.
  - `gaps` is the close's own gaps plus `"<label> is not filled"` per blocking field missing. The
    "did not agree" lines are gone.

- [ ] **Step 1: Write the failing tests** (reuse `FULL_PLAN`, whose `standard_of_care` is already
  "none exists, because no drug is approved"):
  - `test_a_disagreement_that_says_none_is_not_a_disagreement`: the coyne string → `STATUS_READY`,
    returned disagreements `[]`, and the string absent from the gaps.
  - `test_other_no_disagreement_phrasings_drop`: each of "No disagreements remain.", "Nothing
    outstanding on the science.", "None known.", "None remaining." → `STATUS_READY`.
  - `test_a_disagreement_opening_with_none_of_survives`: `["None of the proposed controls is feasible
    in our hands"]` → `STATUS_NOT_AGREED`, and the string is in the returned disagreements.
  - `test_a_non_list_disagreement_is_an_unreadable_close` (Review Focus 1):
    `disagreements="the lab cannot run the NHP arm"` → `STATUS_NOT_FINISHED`, the gap "The close's
    disagreements could not be read", and the returned disagreements hold that text; an absent key →
    `STATUS_NOT_FINISHED` with `[]`.
  - `test_the_markers_no_longer_decide`: a complete plan with no disagreements is `STATUS_READY`
    whatever `close["position"]` says.
  - `test_time_and_budget_does_not_block` (D72): `{**FULL_PLAN, "time_and_budget": "to settle with the
    lab"}` → `STATUS_READY`, and "Time and budget is not filled" is not a gap.
  - `test_a_blocking_field_placeholder_is_not_agreed`:
    `{**FULL_PLAN, "positive_control": "to settle with the lab"}` → `STATUS_NOT_AGREED` with the gap
    "Positive control is not filled".
  - `test_placeholder_rule` (Review Focus 2): `field_filled` is True for "none exists, because no drug
    is approved", "None-the-less we will", "tbdx", "unknown aetiology, so the model is …"; False for
    "To settle with the lab (likely $150K)", "TBD — pending the quote", "none", "to settle with the
    lab".
  - `test_missing_fields_takes_a_field_set` (M14): `missing_fields(FULL_PLAN)` is `[]`;
    `missing_fields({}, BLOCKING_FIELDS)` has nine labels and not "Time and budget".
  - `test_run_d54d7386_coyne_is_ready`: coyne's exact close shape → `STATUS_READY`.
  - Update every existing outcome test for the three-tuple and the new signature; keep
    `test_field_filled_and_missing_fields` passing unchanged.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.** Docstrings state the rule and name D68–D73 in one clause.
- [ ] **Step 4: Run** `tests/unit/test_project_plan.py` and `tests/unit/test_no_dead_src_symbols.py`
  → PASS.

### Task 2: the close is recorded from the close

**Files:**
- Modify: `src/agent/engine/projects.py` (`_close_values`, `_record_project_close`),
  `src/agent/engine/reply_lane.py` (`_handle_project_reply_response`)
- Test: `tests/integration/test_project_threads.py`, `tests/integration/test_projects_engine.py`

**Interfaces:**
- `_close_values(thread, close, *, anchors, truncated, profile_seen)` — `posted_ts` leaves the
  signature, since `hub_marker` was its only use (D70).
  - `status`, `gaps` and `disagreements` all come from `project_outcome(close=close)` (M15); `final`
    stores the returned `gaps` and `disagreements`, so a Ready project carries none (Review Focus 6).
  - `hub_position` is `"agree"` when `close.get("position") == "agree"` else `"disagree"`;
    `lab_position` stays `_latest_lab_position(...)` (M17). Both are commentary.
  - `final` gains `"truncated": True` when the close turn was cut off, and
    `"tool_profile_seen": bool` (M8).
- `_record_project_close(agent, thread, raw_response, posted_ts, *, truncated: bool = False)` keeps
  `posted_ts` for its log line only. It selects the project's `id`, `choice` and `status` by
  `(simulation_run_id, reply_ts)` in the session it already opens (M9), then:
  - no readable block → `not_finished` / `close_unreadable`, as today;
  - `project_outcome` returning `STATUS_NOT_FINISHED` (M11) → `not_finished` / `close_unreadable`,
    and `final` is still written, so the unreadable text is kept;
  - `truncated` **and** the blocking fields incomplete → `not_finished` / `close_unreadable` (M13);
  - otherwise the graded status.
  `final` is written on every path that had a readable block.
- `_handle_project_reply_response` passes `truncated=_was_truncated(stop_reasons)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_truncated_but_complete_close_is_graded`: ordinal-12 turn, complete readable block,
    `stop_reasons == ["max_tokens"]` → `ready`, `final["truncated"] is True`, posted text carries
    `TRUNCATION_NOTICE`.
  - `test_a_truncated_close_missing_a_blocking_field_is_unreadable` → `not_finished` /
    `close_unreadable`.
  - `test_a_close_with_no_disagreements_key_is_unreadable` (M11), with `final` present.
  - `test_a_ready_close_stores_no_disagreements` (Review Focus 6): the coyne shape end to end →
    `ready` and `final["disagreements"] == []`.
  - `test_a_lab_that_closed_with_a_warning_is_still_ready` (D70): the lab's last post opens ⚠️, the
    close is complete with no disagreement → `ready`, `final["lab_position"] == "disagree"`.
  - `test_hub_position_is_normalised` (M17): `close["position"] = "Agree"` → stored `"disagree"`
    (anything but exactly `"agree"`), so `_position_label` keeps working.
  - **Rewrite** `tests/integration/test_projects_engine.py::test_record_close_ready_and_not_agreed`:
    it currently produces `not_agreed` only because the lab's last message opens ⚠️ and asserts
    `"The lab did not agree" in final["gaps"]`; both are removed. It must now pin that the same close
    is `ready` and that a close missing a blocking field is `not_agreed`.
  - Update `:1177`'s `final["tool_marks"]` assertion for Task 5's shape.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both files → PASS.

## Package B — a refusal must not destroy a project

### Task 3: a refused lab reply is posted, and a project that cannot be answered ends

**Files:**
- Modify: `src/agent/engine/projects.py` (`_compose_project_reply`, `_offer_project_reply`)
- Test: `tests/integration/test_projects_engine.py`

**Interfaces:**
- `_compose_project_reply(agent, project, settings) -> tuple[str | None, bool]` — the text and whether
  the call was cut off. Every case: cut off with usable text `(text, True)`; cut off with none
  `(None, True)`; the normal path with text `(text, False)`; the normal path with an empty extraction
  `(None, False)`; an exception `(None, False)`.
- `_offer_project_reply` builds `REPLY_HEADER + text`, appends `TRUNCATION_NOTICE` **only when the
  result still satisfies `fits_one_message`** (the notice must never be what pushes a reply over the
  limit — drop the notice, not the reply), and posts. `(None, True)` counts a reply failure through
  `_count_project_reply_failure` **whether or not Projects is winding down**, and returns True.
  `(None, False)` keeps today's behaviour: uncounted outside a wind-down. `REPLY_FAILURES_MAX` stays 2,
  so two cut-off attempts that yield nothing end the project `not_finished` / `reply_failed`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_refused_lab_reply_with_usable_text_is_posted`: stub reports `["refusal"]` with a complete
    `<slack_message>` body → posted, carries `TRUNCATION_NOTICE`, project `refining`,
    `reply_failures == 0`.
  - `test_the_notice_is_dropped_rather_than_the_reply`: a body that fits only without the notice →
    posted without it, `reply_failures == 0`.
  - `test_a_refused_lab_reply_with_no_text_counts_outside_a_drain` (Review Focus 3) →
    `reply_failures == 1`, returns True.
  - `test_two_such_attempts_end_the_project` → `not_finished` / `reply_failed`.
  - `test_a_model_exception_still_counts_nothing_outside_a_drain` — unchanged.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.** The docstrings name jbailey's six discarded replies.
- [ ] **Step 4: Run** `tests/integration/test_projects_engine.py` → PASS.

### Task 4: a refused thread turn backs off on its own counter, bounded

**Files:**
- Modify: `src/agent/state.py` (one field), `src/agent/project_guidance.py` (one constant),
  `src/agent/engine/reply_lane.py` (`_reply_to_project_thread`, `_handle_project_reply_response`,
  `_post_reply`)
- Test: `tests/integration/test_project_threads.py`

**Interfaces:**
- `ThreadState.refusal_count: int = 0` — consecutive project-thread turns whose model call produced
  nothing and reported a refusal or truncation. Distinct from `empty_response_count` (nothing produced,
  no such signal) and `suppressed_post_count` (text produced, never posted). Its comment records that
  it is not persisted: a restart rebuilds the thread with 0, so a permanently refusing thread survives
  a restart rather than abandoning at the cap.
- `PROJECT_REFUSALS_MAX = 4` in `src/agent/project_guidance.py` (dependency-free, already imported by
  the reply lane; `constants.__all__` is re-export-checked, so the constant does not go there). Its
  comment records run d54d7386: antiochos lost two consecutive turns to refusals at ordinal 8 of 12
  and was abandoned three turns from its close.
- `_reply_to_project_thread` collects `provenance: list[dict]` and passes
  `on_provenance=provenance.append` (as `reply_lane.py:628` does for interviews), then hands it to the
  handler.
- `_handle_project_reply_response(agent, thread, raw_response, stop_reasons, turn, provenance)`: when
  the extracted text is empty **and** (`_was_truncated(stop_reasons)` or
  `_last_refusal_category(provenance)` is not None):
  - `thread.refusal_count += 1`, `empty_response_count` untouched;
  - one warning naming the refusal category and the count;
  - `thread.retry_after = deps.time.time() + reply_backoff_seconds(thread.refusal_count)` — from
    `refusal_count`, because `_note_reply_success` (`reply_lane.py:1296`) runs before this handler and
    zeroes `failed_call_count`;
  - at `PROJECT_REFUSALS_MAX`, `has_pending_reply = False` and `_close_thread(..., ABANDONED_OUTCOME)`,
    as the empty-reply branch does, so `_end_refining_projects` reaches `thread_abandoned` and a
    wind-down drains;
  - below the cap, `has_pending_reply` stays True and the thread retries after the backoff.
- `_post_reply` resets `refusal_count` to 0 alongside its existing resets.
- An empty reply with neither signal keeps today's two-strike rule exactly.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_refused_hub_turn_does_not_spend_a_strike` (Review Focus 4): two consecutive refused turns →
    thread `active`, project `refining`, `empty_response_count == 0`, `refusal_count == 2`,
    `retry_after > now`.
  - `test_a_thread_refused_to_the_cap_is_abandoned`: `PROJECT_REFUSALS_MAX` refusals → thread
    `abandoned`, one `ThreadDecision`, and `_end_refining_projects` ends the project `not_finished` /
    `thread_abandoned`.
  - `test_a_refusal_then_a_good_reply_clears_the_counter`.
  - `test_a_plain_empty_reply_still_abandons_after_two` — unchanged.
  - `test_the_refusal_category_is_logged`: provenance `{"refusal_category": "bio"}` → the warning names
    `bio` (`caplog`).
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/integration/test_project_threads.py`, `tests/unit/test_reply_lane.py`,
  `tests/unit/test_slack_post_landed_check.py`, `tests/unit/test_engine_import_graph.py`,
  `tests/integration/test_rebuild_projects.py` → PASS.

### Task 5: one tool-marking computation, written once and read by the page

**Files:**
- Modify: `src/services/project_plan.py` (`tool_marks`), `src/agent/engine/projects.py`
  (`_close_values`), `src/services/projects_view.py`
- Test: `tests/unit/test_project_tool_marks.py`, `tests/integration/test_projects_engine.py`,
  `tests/integration/test_projects_view.py`

**Interfaces:**
- `project_plan.tool_marks(lab_tools, items, anchors=()) -> list[dict]` becomes the single
  implementation, with `display_tool_marks`' rule verbatim: candidates are `METHOD_MODEL_SECTIONS`
  items plus anchors from those sections, normalised, at least two words; a tool matches when its
  normalised text contains a candidate as whole words; the longest wins. Each mark is
  `{"item", "in_profile", "matched"}`.
- `projects_view.display_tool_marks(tools, profile_items, anchors)` keeps its name and signature and
  delegates, so its tests pass unchanged.
- `_close_values` computes the marks with the project's anchors and records
  `tool_profile_seen`: whether the profile yielded any `METHOD_MODEL_SECTIONS` item.
- `build_project_detail` uses the stored `final["tool_marks"]` when every entry carries a `matched`
  key **and** `final.get("tool_profile_seen")` is true (M8); otherwise it recomputes, which covers both
  pre-change closes and a close written with no profile in memory (Review Focus 5).
- `match_anchor` is unchanged: `choose.md:21` tells the model to copy anchors verbatim, so equality is
  right there and only there.

- [ ] **Step 1: Write the failing tests:**
  - In `tests/unit/test_project_tool_marks.py`: the same cases against `project_plan.tool_marks`
    (containment, longest match, whole words, anchors, the two-word floor), plus
    `test_the_view_delegates` (identical output for identical input).
  - `test_a_qualified_profile_item_marks_in_profile`: profile method "pan-expansion microscopy
    (pan-ExM) with machine-learning-facilitated NPC segmentation", tool the same plus " (contingent,
    requires re-training)" → `in_profile` True, `matched` the profile's text.
  - In `tests/integration/test_projects_view.py`: `test_the_page_shows_the_stored_marks`;
    `test_a_pre_change_close_is_recomputed`; `test_a_close_with_no_profile_seen_is_recomputed`
    (`tool_profile_seen: false` with all-unmarked stored marks → the page marks the in-profile tool).
  - In `tests/integration/test_projects_engine.py`: the close stores marks computed with the project's
    anchors, and `tool_profile_seen` is true for a lab with a profile.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the three files plus `tests/integration/test_projects_pages.py` → PASS.

### Task 6: a posted close is never lost

**Files:**
- Modify: `src/agent/engine/projects.py` (`_record_project_close`)
- Test: `tests/integration/test_project_threads.py`, `tests/integration/test_projects_engine.py`

**Interfaces:** on a zero-row compare-and-set, a second statement writes `final` by the project's id
**with `HubProject.final.is_(None)` in its WHERE**, so a row that already carries a close is never
overwritten by a second one, and `status`, `reason_code`, `finished_at` and `finished_run_seconds` are
untouched. One warning names what happened. The method still returns None.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_close_landing_after_the_row_was_stopped_is_kept`: seed `not_finished` / `stopped` with its
    `reply_ts`, drive the hub's close turn → status and reason unchanged, `final["plan"]` is the
    close's plan.
  - `test_a_second_close_does_not_overwrite_the_first`: a `ready` row with a `final`, driven through a
    second close → both status and `final` unchanged.
  - Rewrite `tests/integration/test_projects_engine.py:1186-1189`'s "not written twice" assertion to
    pin the new rule: the status is unchanged and `final` is now present.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.** Name in the docstring the three readers that now see a `final` on such a
  row: `projects_view.build_project_detail`'s `has_close`, `project_chat_record`, `projects_report`.
- [ ] **Step 4: Run** both files plus `tests/integration/test_projects_report.py` and
  `tests/integration/test_project_chat_service.py` → PASS.

## Package C — prompts

### Task 7: the project prompts

**Files:**
- Modify: `prompts/projects/lab-thread-reply.md`, `prompts/projects/hub-thread-reply.md`
- Also committed: `docs/specs/2026-10-27-projects-prompts.md` (rewritten by the sync script)
- Test: `tests/unit/test_doc_prompt_sync.py`, `tests/unit/test_template_token_contract.py`,
  `tests/unit/test_agent_project_prompts.py`

Text only; no `{token}` is added or removed. Steps 3 and 4 touch `hub-thread-reply.md` only — the lab
prompt has no close block and must not gain one.

- [ ] **Step 1 (D68, D71, D72):** `lab-thread-reply.md`'s Rules gains: *"Complete means every field of
  the plan is filled and you would run it as written. An item Blackbird has said it will source, a
  figure only your institution can confirm, a vendor's terms, or a decision that belongs to your PI is
  a gap to record, not a reason to withhold ✅. Time and budget is settled later by Blackbird's
  staff."*
- [ ] **Step 2 (D71):** both prompts gain: *"Do not spend turns pursuing an item neither of you
  controls — name it once as a gap and move on."*
- [ ] **Step 3 (D73, M11, F6):** in `hub-thread-reply.md`'s Closing section, replace *"Write 'to settle
  with the lab' for any field still open and list what is missing as gaps; list anything the two of you
  still dispute as disagreements."* with: *"Fill every field. If a field genuinely has no answer, say
  so in the field itself and why ('none exists, because …'). List as gaps what is still open, each
  naming who must close it; leave out anything settled earlier in this thread or deferred to a later
  tranche. `disagreements` must always be present: `[]` when no scientific disagreement remains,
  otherwise one entry per live dispute. Never write a note saying there are none."*
- [ ] **Step 4 (M6, F13):** `hub-thread-reply.md`'s Closing section gains: *"From your second-to-last
  turn, add no new required control, endpoint or analysis: confirm what is agreed and close."*
- [ ] **Step 5:** `.venv-test/bin/python scripts/sync_prompt_set_docs.py`, then `--check` clean.
- [ ] **Step 6: Run** the three test files → PASS.

## Package D — the product's own description

### Task 8: help pages and glossary

**Files:**
- Modify: `templates/help/pages/projects.html`, `templates/help/pages/glossary.html`
- Test: `tests/integration/test_help_pages.py` (or the help tests that exist), `tests/unit/test_help_registry.py`

The shipped text states the retired rule and would be a lie after release. Keep every `id=`, every
anchor and the page structure; change wording only, per `docs/operations/help-content.md`.

- [ ] **Step 1:** `templates/help/pages/projects.html:114-118` — **Ready** becomes "every field of the
  plan is filled, apart from time and budget, and the close records no scientific disagreement";
  **Not agreed** becomes "BlackbirdBot closed the thread, but a field of the plan was left open or a
  scientific disagreement was recorded. The details page lists the gaps and disagreements."
- [ ] **Step 2:** `projects.html:185-192` — the Positions paragraph says the two positions are what
  each side said at the close and do not decide the outcome; "Gaps (a field left open or a missing
  agreement)" becomes "Gaps (what is still open, including items Blackbird or the lab's institution
  must close)".
- [ ] **Step 3:** `glossary.html` — `term-ready` (`:461`), `term-not-agreed` (`:327`) and
  `term-acknowledged-limits` (`:21`) take the same wording, and "both agreed" leaves all three.
- [ ] **Step 4:** `grep -rn "both agreed\|did not agree\|every field of it is filled\|missing agreement" templates/help/`
  returns nothing.
- [ ] **Step 5: Run** the help tests plus
  `PYTHONPATH=$PWD .venv-test/bin/python -m tests.e2e.help_shots.capture --check` (expect "29 shots
  current": no shot sources a help page).

## Package E — documentation

### Task 9: docs

**Files:** `docs/operations/blackbird-hub.md`,
`docs/specs/2026-10-22-idea-pitching-prototype-design.md`

- [ ] **Step 1:** the hub doc's projects section records the new outcome rule (two checks, markers as
  commentary, time and budget exempt), the refusal handling on both paths, the single tool matcher and
  its `tool_profile_seen` flag, and that a posted close is kept whatever the row says.
- [ ] **Step 2:** dated amendment lines under §5.5 (the placeholder split, time and budget exempt,
  tool marking by containment), §5.7 (a refused reply posted, counted after two), §5.8 (refusal
  backoff and the cap, scope freeze) and §5.9 (the outcome reads fields and disagreements only), each
  naming D68–D74 and this plan's path. Do not add the plan's path to
  `tests/unit/test_claude_md_references.py`'s ALLOWLIST: it is committed with this change.
- [ ] **Step 3: Run** `tests/unit/test_claude_md_disclosure_sync.py`,
  `tests/unit/test_claude_md_references.py`, `tests/unit/test_doc_prompt_sync.py` → PASS.

## Task 10: verify, audit, commit, release (main session)

- [ ] **Tripwire sweep:** `grep -rn "getsource\|read_text" tests/` filtered to the touched modules.
  Pass: no source-scanning assertion references a string this change removes, and
  `test_slack_post_landed_check` still finds exactly one `landed_check=True` in `reply_lane.py`.
- [ ] **Checks:** the focused tests of Tasks 1–9; `scripts/sync_prompt_set_docs.py --check`;
  `scripts/build_css.sh --check`; `help_shots.capture --check`; `ruff check` with no new finding
  against HEAD; the 200-line function gate; then the full CI.
- [ ] **Adversarial audit of the code:** `lathe:plan-auditor` and `lathe:semantic-reviewer` in one
  batch; fix, rerun the focused tests and CI.
- [ ] **Commit** an explicit file list, this plan included; never `docker-compose.prod.yml`.
- [ ] **Release.** No migration. Pre-build gate:
  `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts help_assets pyproject.toml alembic.ini`
  must print nothing (the builder runs `git clean -ffdx`). Rebuild all three images —
  `blackbird-app` (Packages A, C's page reads, D), `worker` (`project_plan` reaches it through
  `src/services/project_research.py`, imported at `src/worker/main.py:66`) and `agent` (Packages A, B
  and the prompts, which are baked in as well as mounted). Rollback tags `rollback-pre-projrca` on
  each image and `rollback-pre-projrca-git` at the pre-change HEAD. Order: build all three;
  `$DC up -d --no-deps blackbird-app worker`; then `$DC --profile agent up -d --no-deps agent`
  **only** when `/workspace/simulation` shows no live run. Health 200, copi-edge IP still 172.19.0.2,
  org1 untouched.
- [ ] **Regenerate the human review prompt documents** (owner's request): the sync script has already
  rewritten `docs/specs/2026-10-27-projects-prompts.md`; export it to Word with
  `pandoc docs/specs/2026-10-27-projects-prompts.md -o docs/specs/2026-10-27-projects-prompts-<sha12>-review.docx`
  on the workstation (pandoc is not on the host), where `<sha12>` is
  `sha256sum docs/specs/2026-10-27-projects-prompts.md | cut -c1-12`, and report whether the
  superseded `…-0b8c0ea40201-review.docx` was replaced.
- [ ] **Do not start a run.**

## Verification against run d54d7386 (read-only, after release)

- [ ] **Replay the closes from the raw model text, not from `final`** (the stored `final.gaps` already
  holds the code's appended lines and its `disagreements` is already filtered, so replaying `final`
  would double-count). Select on `phase = 'project_thread' AND response_text LIKE '%<project_json>%'`
  for the run — not on `thread_phase`, since an early close in the open band logs as `project_open`.
  Run `_extract_project_json` and the new `project_outcome` over each, newest per thread, in a one-off
  container off the **blackbird-app** image:
  `docker compose -f docker-compose.prod.yml run --rm --no-deps -T blackbird-app python - < replay.py`.
  Pass: coyne and the twelve other `not_agreed` projects become `ready`; no project that is `ready`
  today becomes anything else.
- [ ] **Recompute the tool marks** over the same closes. Pass: the in-profile share is about 11%, not
  3.8%, and no project sits at zero whose posting's "Builds on:" line names one of its tools.
- [ ] **On the next run:** no project sits `posted` through more than two refused reply attempts; no
  thread closes `abandoned` with fewer than `PROJECT_REFUSALS_MAX` refusals recorded; the Ready share
  is reported against this run's 6 of 19.

## Residual risks and owner notes

- **The ready rate is now a prompt property.** D68's wording is the main lever and its effect is a
  guess until a run tests it. It can overshoot: a lab may write ✅ over something that should have
  blocked. The nine-field check is the only mechanical backstop.
- **`disagreements` becomes the single load-bearing judgement.** D73 makes the hub answer and M11
  makes silence an unreadable close, but nothing catches a wrong "none". In this run the field was
  empty in four of five threads and misused in the fifth.
- **Three paths now feed the failure streak**, where today they stall or pass: `reply_failed`
  (Task 3), and `close_unreadable` from both Task 2's truncated-and-incomplete case and M11's
  absent-or-unreadable `disagreements`. All are in `FAILURE_REASONS` (`projects.py:98-106`), so three
  in a row wind a Projects session down. D67 (the stop unchanged) stands; this is the explicit note.
- **A refused thread now costs up to four turns** before abandoning, where today it costs two.
- **`refusal_count` is not persisted**: an engine restart resets it, so a permanently refusing thread
  survives a restart.
- **The label "Not agreed" now also covers an unfilled field** (M16). The help text defines it; the
  owner may want it renamed.
- **F14's drafting classes are deferred**; F12's standing REFINE topics are untouched by design (D74).
- **Latent, fixed on reasoning only:** F4, F5, F10, F11 were never observed in production.

## Audit history

**Revision 1 audit (6 blockers, 23 findings).** Placeholder rule breaking "none exists, because …" →
M10; truncated close discarding good closes → M13; the refusal counter that could never increment and
the leaked thread → M12; anchors unreachable from `_close_values` → M9; grouped gaps discarded before
storage → dropped with M5; the sync script's ordinals → avoided by M6. Mediums folded into the task
texts; the hub marker read from the logged post was retired by D70 rather than fixed.

**Revision 2 audit (3 blockers, 16 mediums).** `missing_fields` left dead → M14 (a parameter, not a
sibling); `final["disagreements"]` still storing the raw list → M15 (`project_outcome` returns it);
help pages still stating the old rule → Package D, Task 8. Mediums: the "Not agreed" label → M16 and
Task 8; `PROJECT_REFUSALS_MAX` and `constants.__all__` → the constant moved to `project_guidance.py`;
`hub_position` normalisation → M17; Task 6's fallback overwriting a second close → `final IS NULL` in
its WHERE; F11's reachability → corrected and narrowed; M11 feeding the failure streak → residual
risks; the `live_disagreements` whitelist → a token-subset rule with four more phrasings tested;
the plan file's ALLOWLIST status → committed, no entry; `test_record_close_ready_and_not_agreed` →
named in Task 2; M8's empty-profile seam → `tool_profile_seen`; the replay query and its container
command → made exact; `posted_ts` unused in `_close_values` → dropped; the normal-path empty
extraction → added to Task 3's contract; `refusal_count` across a restart → documented; F8's "clears
only on" → corrected.
