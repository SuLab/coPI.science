# Reviewer rubric edits and six key-point groups — Implementation Plan

> **For agentic workers:** execute with `/engineering:plan-execution` (the
> operator's standing policy replaces superpowers' executing-plans /
> subagent-driven-development). The work is split into **12 disjoint-file
> packages (P1–P12, see the Package map)** dispatched in one parallel batch;
> `src/services/assessment_detail.py` is written by the integrator BEFORE
> fan-out (it is the shared interface). **Implementers do not build, run
> tests, lint, run scripts, or run git write commands.** Per the operator: no
> build or test runs until every package is implemented, committed and merged
> into the local `blackbird` branch (Task 6); every "Run" step below is the
> integrator's, after that merge. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Apply the 2026-09-22 reviewer's rubric edits as rubric 3.5.0 and
replace the five key-point groups with the reviewer's six (scout_hub 1.8.0),
without hiding or rewriting any stored verdict.

**Architecture:** The rubric is data (`prompts/rubric/blackbird-rubric.toml`)
parsed once at import; the key-point contract is prompt text plus one constant
set in `src/services/assessment_detail.py` that both write acceptance and every
read surface derive from. Legacy rows keep their own labels via a second,
display-only group set; writes accept the union so neither prompt/image skew
direction drops data.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy async, pytest
(testcontainers Postgres), TOML.

**Spec:** `docs/specs/2026-09-24-reviewer-rubric-and-key-points-design.md`

**Where:** worktree `/home/ubuntu/blackbird-worktrees/reviewer-rubric-key-points`
(branch `feat/reviewer-rubric-key-points`, from `91f34f1`). From the operator's
machine the same tree is `/home/a/mounts/ubuntu/blackbird-worktrees/reviewer-rubric-key-points`
(sshfs). **Never edit `/home/ubuntu/blackbird-copi-science` (the live tree).**
All paths below are relative to the worktree root.

## Global Constraints

- Rubric `[meta].version = "3.5.0"`, `[meta].date = "2026-09-25"`; thresholds stay `advance_min = 3.4`, `conditional_min = 2.8`; `pass_label = "decline"`.
- Weights exactly: differentiation_unmet_need 25, scientific_credibility 25, translational_path 25, fundable_experiment 15, venture_potential 5, team_executability 5.
- scout_hub `role.toml` `version = "1.8.0"`.
- Key-point keys/labels exactly: `indication_audience` "Indication / Audience", `lab_background` "Lab Background", `proposal` "Proposal", `clinical_actionability` "Clinical Actionability", `key_questions` "Key Questions/Experiment", `commercial_opportunity` "Commercial Opportunity".
- Legacy keys/labels exactly: `significance` "Significance", `innovation` "Innovation", `clinical_actionability` "Clinical actionability", `key_questions` "Key questions / experiments", `commercial_potential` "Commercial potential".
- Bullet counts 1/2/2/2/1/2; bullet ≤300 characters; both are WARNINGS only — nothing truncated or dropped.
- Every `$100K–$1M` / `12–24 months` statement outside the fundable anchor and `[stage_bar.budget]` is unchanged (spec D4). Dashes in rubric/prompt text are U+2013 en dashes, as in the existing file.
- No migration. No change to the five stored 2026-09-22 verdicts.

## Review Focus

1. A row stored under the 1.4.0–1.7.1 five-group shape renders with its original labels and order on the list card, the detail page and in the chat record — Task 3 tests.
2. A sidecar still using legacy keys (stale prompt on the new image) is stored, not dropped, and logs one legacy-shape warning — Task 4 test.
3. A row whose only groups are empty or unknown renders NO "Key points" box and does not move "Why this score" — Task 3 tests.
4. An owed headline for a verdict stored before the weights change is announced with its STORED band/score, never recomputed — Task 4 test.
5. The prompt's new item 7 names the six groups in skeleton order with counts that match the engine's warning table — Task 2 + Task 4 tests.

---

## Package map (write ownership is exclusive)

| Pkg | Owns (create/modify only these) | Steps |
|---|---|---|
| pre | `src/services/assessment_detail.py` — integrator, before fan-out | Task 3 Step 2 |
| P1 | `prompts/rubric/blackbird-rubric.toml`, `prompts/rubric/revisions.toml`, `scripts/render_rubric_review_doc.py`, `tests/unit/test_render_rubric_review_doc.py` (new) | Task 1 Steps 1-10, 10b |
| P2 | `tests/unit/test_blackbird_rubric.py`, `tests/unit/test_rubric_document.py`, `tests/unit/test_stage_bars.py`, `tests/integration/test_opportunity_assessment_persistence.py` | Task 1 Steps 11-14 |
| P3 | `prompts/roles/scout_hub/phase4-thread-reply.md`, `prompts/roles/scout_hub/role.toml` | Task 2 Steps 1-5 |
| P4 | `tests/unit/test_headline_contract.py`, `tests/unit/test_rubric_prompt_sync.py` | Task 2 Steps 6-7 |
| P5 | `tests/unit/test_key_point_sections.py` (new) | Task 3 Step 1 |
| P6 | `src/routers/admin.py`, `src/routers/manager.py`, `templates/admin/_assessments_body.html`, `templates/admin/_assessment_detail_body.html`, `src/models/opportunity.py` | Task 3 Steps 3, 4, 5, 7 |
| P7 | `src/services/assessment_chat_record.py`, `tests/unit/test_assessment_chat_record.py`, `tests/integration/test_assessment_chat_parity.py` | Task 3 Steps 6, 10, 11 |
| P8 | `tests/integration/test_assessment_detail_page.py`, `tests/integration/test_assessment_queue_controls.py` | Task 3 Steps 8, 9 |
| P9 | `src/agent/simulation.py` | Task 4 Steps 1-4 |
| P10 | `tests/integration/test_assessment_narrative_fields.py`, `tests/integration/test_assessment_headline_delivery.py` | Task 4 Steps 6, 7 |
| P11 | `scripts/backfill_dropped_verdicts.py`, `tests/unit/test_backfill_dropped_verdicts.py` | Task 4 Steps 5, 8 |
| P12 | `CLAUDE.md` | Task 5 |

Read-only for everyone: the spec, this plan, and every file not in the
package's row. `docs/specs/2026-08-07-hub-bot-prompts.md` and
`docs/rubric-review/*` are regenerated by the integrator (Task 6), not edited.

---

### Task 1: Rubric 3.5.0 — document, registry, review-copy renderer, rubric tests

**Files:**
- Modify: `prompts/rubric/blackbird-rubric.toml`
- Modify: `prompts/rubric/revisions.toml`
- Modify: `scripts/render_rubric_review_doc.py:105-107`
- Create: `tests/unit/test_render_rubric_review_doc.py`
- Modify: `tests/unit/test_blackbird_rubric.py`
- Modify: `tests/unit/test_rubric_document.py`
- Modify: `tests/unit/test_stage_bars.py:141-144`
- Modify: `tests/integration/test_opportunity_assessment_persistence.py:218-226`

**Interfaces:** Produces the 3.5.0 document; `RUBRIC_WEIGHTS` becomes the new weights (consumed by Task 2's prose test and the whole suite).

- [ ] **Step 1: `[meta]`.** Set `version = "3.5.0"` and `date = "2026-09-25"`. Insert as the FIRST lines inside `changelog = """` (before the `3.4.0 (2026-08-29)` entry):

```
3.5.0 (2026-09-25): applies the 2026-09-22 Blackbird review of the 3.2.0
review copy (docs/specs/2026-09-24-reviewer-rubric-and-key-points-design.md).
Weights 25/20/15/15/15/10 -> 25/25/25/15/5/5 (scientific_credibility 20->25,
translational_path 15->25, venture_potential 15->5, team_executability
10->5); the science block is now 50 points and the scoring preamble's split
is re-derived to 50%/50%. fundable_experiment's anchor tightens the
killer-experiment bar to a $100K–$300K grant over 6–12 months, measures
replication against the proposed budget instead of a fixed $200K, and drops
the state/regional non-dilutive leverage credit; one added clause says the
incubation grant itself can run larger. [stage_bar.budget] is re-derived
from the new anchor. Thresholds 3.4/2.8, gating, dimensions, pass_label and
the other stage bars are unchanged. Back-test of the new weights over the 27
stored 3.x verdicts: 4 bands would move (2 advance->conditional, 1
conditional->pass, 1 conditional->advance); stored rows are never rescored.
```

- [ ] **Step 2: `[banding]` comment.** After the line `# re-check after >=20 verdicts stamped 3.x.` (currently line 119) add:

```
#
# 3.5.0 (2026-09-25) retained 3.4/2.8 unchanged under the new weights. Re-run
# over the 27 stored 3.x verdicts, the new weights move 4 bands (velculescu
# 08-31 and coller 09-09 advance->conditional, lamichhane 09-09
# conditional->pass, slusher 09-22 conditional->advance); the re-check itself
# is still owed.
```

- [ ] **Step 3: `[scoring].preamble`.** Replace exactly `development path) carry 35% of the total; the four commercial dimensions carry 65% —` with `development path) carry 50% of the total; the four commercial dimensions carry 50% —`. Change nothing else in the preamble.

- [ ] **Step 4: weight-rationale comment.** Replace the comment block from `# Weight rationale (docs/plans/2026-08-27-rubric-v3-consolidation.md, from a` through `#   - team_executability 10.` (currently lines 225-235) with:

```
# Weights since 3.5.0 are the 2026-09-22 Blackbird review's own calibration
# (docs/specs/2026-09-24-reviewer-rubric-and-key-points-design.md):
#   - differentiation_unmet_need 25, scientific_credibility 25,
#     translational_path 25: the science block is 50 points, so a purely
#     scientific objection moves the band on its own even more readily than
#     under 3.0.0's 35.
#   - fundable_experiment 15: unchanged.
#   - venture_potential 5, team_executability 5.
# The 3.0.0-3.4.0 weights (25/20/15/15/15/10) and their back-test rationale
# are in docs/plans/2026-08-27-rubric-v3-consolidation.md and in
# prompts/rubric/revisions.toml.
```

- [ ] **Step 5: weights.** In the `[[dimension]]` tables set `weight = 25` for `scientific_credibility` (was 20) and for `translational_path` (was 15); `weight = 5` for `venture_potential` (was 15) and for `team_executability` (was 10).

- [ ] **Step 6: fundable anchor.** Replace the whole `anchors = "Could a $100K–$1M grant …adds."` line of the `fundable_experiment` table with:

```toml
anchors = "Could a $100K–$300K grant over 6–12 months buy a *decisive* de-risking result? 5 = a crisp, quantified killer experiment within budget; 1 = no experiment articulable, scope far beyond an incubation grant, or key data that cannot be replicated for less than the proposed budget and a reasonable timeline. The incubation grant can run larger; this asks whether a decisive result comes within the first $100K–$300K and 6–12 months."
```

- [ ] **Step 7: other comments.** In the `[red_flags]` comment replace `unprecedented path with no plan, >$200K replication) live in the dimension` with `unprecedented path with no plan, replication costlier than the proposed budget) live in the dimension` (re-wrap that comment paragraph to ≤80 columns if needed). In the `[stage_bar_global]` comment replace `and it discloses the 35/65 weighting)` with `and it discloses the 50/50 weighting)`.

- [ ] **Step 8: `[stage_bar.budget]`.** Replace its `text = "…"` with the line below — ONE line, a basic `"…"` string, immediately after its `source = "fundable_experiment"` line (`tests/unit/test_stage_bars.py` mutates the bar through the anchor `source = "…"\ntext = "…"`):

```toml
text = "Adequate here is a scope where a $100K–$300K grant over 6–12 months could buy a DECISIVE de-risking result — the incubation grant can run larger, but the bar is whether the decisive result comes that early and that cheaply. Below the bar is no articulable experiment, scope far beyond an incubation grant, or key data unreplicable for less than the proposed budget and a reasonable timeline."
```

- [ ] **Step 9: registry.** In `prompts/rubric/revisions.toml` add this provenance line at the end of the "Provenance of the entries below" comment list:

```
#   3.4.0 b7b0a1d6a4a5  <- git 91f34f1 (the document live at the 3.5.0 bump);
#         entry appended 2026-09-25 per the maintenance rule — sha256 12-hex
#         of the outgoing file bytes, verified with sha256sum. 3.4.0 only
#         renamed pass_label from 3.3.0.
```

and append at the end of the file:

```toml
[[revision]]
version = "3.4.0"
content_hash = "b7b0a1d6a4a5"
scale_min = 1
scale_max = 5
advance_min = 3.4
conditional_min = 2.8
pass_label = "decline"
banding_note = "No weight, threshold, dimension, gating key, band semantic or red flag changes from 3.3.0 — 3.4.0 only renamed the display label 'pass (decline)' -> 'decline'."

  [[revision.dimension]]
  key = "differentiation_unmet_need"
  title = "Differentiation & unmet need"
  weight = 25
  [[revision.dimension]]
  key = "scientific_credibility"
  title = "Scientific credibility & mechanism"
  weight = 20
  [[revision.dimension]]
  key = "translational_path"
  title = "Translational & development path"
  weight = 15
  [[revision.dimension]]
  key = "fundable_experiment"
  title = "Fundable killer experiment & capital efficiency"
  weight = 15
  [[revision.dimension]]
  key = "venture_potential"
  title = "Venture potential: IP path, platform & signals"
  weight = 15
  [[revision.dimension]]
  key = "team_executability"
  title = "Team & executability"
  weight = 10
```

- [ ] **Step 10: renderer.** In `scripts/render_rubric_review_doc.py`, at module level add `_SCIENCE_KEYS = ("scientific_credibility", "translational_path")`. In `render_review_markdown`, before the `lines += [` that starts `"## 2. Weighted scoring dimensions"`, add:

```python
    science_pct = sum(d.weight for d in r.dimensions if d.key in _SCIENCE_KEYS)
    commercial_pct = sum(d.weight for d in r.dimensions) - science_pct
```

and replace the literal `"> Note for reviewers: the 35% / 65% split quoted below is derived from the",` with `f"> Note for reviewers: the {science_pct}% / {commercial_pct}% split quoted below is derived from the",`.

- [ ] **Step 10b: renderer test.** Create `tests/unit/test_render_rubric_review_doc.py`:

```python
"""The review copy's reviewer note must quote the split the live weights produce
(it hard-coded "35% / 65%" until rubric 3.5.0 changed the weights)."""
from scripts.render_rubric_review_doc import render_review_markdown
from src.services.blackbird_rubric import RUBRIC_WEIGHTS, load_rubric


def test_the_reviewer_note_quotes_the_split_the_weights_produce():
    science = RUBRIC_WEIGHTS["scientific_credibility"] + RUBRIC_WEIGHTS["translational_path"]
    markdown = render_review_markdown(load_rubric(), "2026-09-25", "")
    assert f"the {science}% / {100 - science}% split quoted below" in markdown
    assert "35% / 65%" not in markdown
```

- [ ] **Step 11: `tests/unit/test_blackbird_rubric.py`.**
  - `test_weights_are_the_six_dimensions_and_sum_to_one_hundred`: expected dict → 25/25/25/15/5/5.
  - Rename `test_science_carries_thirty_five_percent` → `test_science_carries_fifty_percent`; docstring "…jointly carry 50 points."; assert `== 50`.
  - `test_a_real_verdict_scores_as_hand_computed`: comment `# 100 + 75 + 75 + 60 + 10 + 20 = 340 / 100`; assert `== 3.4`.
  - `test_non_finite_scores_count_as_zero_not_a_perfect_five`: comment `(100 - 5) * 5 / 100 = 4.75`; the three `== 4.5` asserts → `== 4.75`.
  - `test_display_rounding_cannot_flip_the_band_across_a_threshold`: set BOTH `scores["venture_potential"] = 3.35` and `scores["team_executability"] = 3.35` (weights 5+5 at 3.35, 90 at 3.4 → true mean exactly 3.395); update the comment to say so; asserts unchanged (`== 3.39`, `"conditional"`).
  - `test_case_and_whitespace_variant_keys_still_match_their_dimension`: `== 3.4`.
  - `test_bool_dimension_values_count_as_zero_not_as_one_or_zero`: the two `== 4.5` → `== 4.75`.

- [ ] **Step 12: `tests/unit/test_rubric_document.py`.**
  - `EXPECTED_WEIGHTS` → 25/25/25/15/5/5; its comment "The exact weights of the 2026-09-25 3.5.0 document".
  - `_TEAM_BLOCK`: `weight = 5`.
  - Rename `test_characterization_science_block_is_35_points` → `..._is_50_points`; assert `== 50`; comment "carry 50%".
  - `test_version_and_content_hash_are_exported`: assert `"3.5.0"`; add to the comment: `"3.5.0" applies the 2026-09-22 review: weights 25/25/25/15/5/5 and the tightened fundable-experiment anchor.`
  - `test_rejects_missing_version` and `test_rejects_version_longer_than_the_column_width`: mutation anchor `'version = "3.4.0"'` → `'version = "3.5.0"'`; the long value → `'version = "3.5.0-twenty-one-chars"'`.

- [ ] **Step 13: `tests/unit/test_stage_bars.py`.** In `_HUB_ONLY_MACHINERY` replace the two tuples for `"35%"` and `"65%"` with one: `("50%", "the science/commercial weight split is how the HUB combines six dimensions into one score"),`.

- [ ] **Step 14: `tests/integration/test_opportunity_assessment_persistence.py`.** Update the comment to `math: 100 + 75 + 75 + 60 + 10 + 20 = 340 / 100` and the asserts to `row.weighted_score == pytest.approx(3.4)` and `row.band == "advance"`; keep the "computed, not 4.8" claim.

- [ ] **Step 15 (integrator, Task 6): Run** `.venv-test/bin/python -m pytest tests/unit/test_blackbird_rubric.py tests/unit/test_rubric_document.py tests/unit/test_stage_bars.py tests/unit/test_rubric_revisions.py -q` — expected PASS.

---

### Task 2: scout_hub prompt set 1.8.0 and its contract tests

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md` (item 6 ≈l.239-294, item 7 ≈l.297-315, rule block before l.403, skeleton l.456)
- Modify: `prompts/roles/scout_hub/role.toml:7`
- Modify: `tests/unit/test_headline_contract.py`
- Modify: `tests/unit/test_rubric_prompt_sync.py`

**Interfaces:** Produces the six skeleton keys (must equal Task 3's `KEY_POINT_GROUPS` keys, in order) and the counts 1/2/2/2/1/2 (must equal Task 4's `_KEY_POINT_GROUP_BULLETS`).

- [ ] **Step 1: item 6.** Insert this paragraph (3-space indent, the item's body level) directly before the line `   No colon-stacked noun phrases. No slash-separated alternatives. **At most one`, i.e. after element 3 ("Why it is fundable"):

```
   Name the modality, and the delivery route where it matters, accurately. A
   plain noun — "oral drug", "blood test", "cell therapy" — is right when it is
   accurate; use the field's own term when the plain word would be vague or
   wrong: "intrathecal oligonucleotide", not "injected RNA drug". The
   110-character bound and the abbreviation rule still apply.

```

- [ ] **Step 2: item 7.** Replace everything from `7. **Key points.** Five labelled groups` through the line ending `array of strings.` (the end of item 7, just before `8. **Elevator pitch.**`) with:

```
7. **Key points.** Six labelled groups, in this order, each holding EXACTLY
   the number of bullets shown; each bullet is a complete claim of at most
   300 characters, not a topic:
   - `indication_audience` — **one bullet**: the disease or condition and the
     patient population, its rough size (an order-of-magnitude US prevalence
     or incidence is enough), what drives it biologically, and what those
     patients get today.
   - `lab_background` — **two bullets**: first, who the PI is and the lab's
     established work that this idea builds on; second, the lab's wider
     platform or track record — the same approach in other diseases, prior
     programmes or spin-outs, and any existing IP or option rights worth
     checking early. State only what the PI's public profile, their
     publications or the lab in this interview established; leave out
     anything not on that record rather than supplying it from general
     knowledge.
   - `proposal` — **two bullets**: "The asset: …" — what it is, how it works
     and why it differs from what exists; then "The work: …" — what the grant
     would buy.
   - `clinical_actionability` — **two bullets**: what patients get today and
     the clinical-stage alternatives, each with its stage, and how this
     differs from them; then the route, endpoint or regulatory precedent that
     would make it actionable.
   - `key_questions` — **one bullet**: the single deciding question, phrased
     as a question, naming the experiment that answers it.
   - `commercial_opportunity` — **two bullets**: the closest deal comparable
     or funding signal (who, how much, when); then the realistic commercial
     shape — licence, platform partnership or spin-out — with the competitive
     position and any IP or novelty caveat.

   Competing programmes may be named in `clinical_actionability` and
   `commercial_opportunity`; item 13 remains the fuller, staff-only
   competitive landscape. Together the six must let a reviewer who reads
   nothing else say who this is for, who is behind it, what it is, how it
   would change care, what decides it, and why it is worth building. Record
   them in `key_points` as an object with exactly those six keys, each an
   array of strings.
```

- [ ] **Step 3: two rules.** Immediately before the paragraph starting `**Never write a bare \`~\` in any sidecar field**`, insert:

```
**Never assert an evidence ceiling you have not checked.** In any sidecar
field, do not write "in vitro only", "no animal data" or any other limit on
the evidence unless the profile, a paper you retrieved or the lab itself
confirms it; where you could not check, say that instead.

**Date what can go stale.** A trial readout, a deal, a regulatory status or
a programme's stage is true as of a date: write it with one ("as of 2025"),
and never present a recent event you have no source for as current.

```

- [ ] **Step 4: skeleton.** Replace the `"key_points": {…},` line with:

```
  "key_points": {"indication_audience": [], "lab_background": [], "proposal": [], "clinical_actionability": [], "key_questions": [], "commercial_opportunity": []},
```

- [ ] **Step 5: version.** `prompts/roles/scout_hub/role.toml`: `version = "1.8.0"`.

- [ ] **Step 6: `tests/unit/test_headline_contract.py`.** In `test_the_prompt_set_version_was_bumped` assert `'version = "1.8.0"' in toml`. Add:

```python
def test_item_six_names_modality_and_route_accurately():
    """2026-09-22 review: card #2's "injected RNA drug" was rewritten as
    "intrathecal oligonucleotide"; cards #1/#3/#4/#5 kept plain nouns ("Oral
    drug", "Cell therapy", "A test", "A screening platform"). So the rule asks
    for accuracy, not jargon. `_item_six_flat` lowercases."""
    flat = _item_six_flat()
    assert "use the field's own term when the plain word would be vague or wrong" in flat
    assert '"intrathecal oligonucleotide", not "injected rna drug"' in flat
    assert "a plain noun" in flat
```

- [ ] **Step 7: `tests/unit/test_rubric_prompt_sync.py`.**
  - `test_skeleton_carries_the_narrative_fields`: replace both key_points assertions with the six-key object/order:

```python
    assert skeleton["key_points"] == {
        "indication_audience": [],
        "lab_background": [],
        "proposal": [],
        "clinical_actionability": [],
        "key_questions": [],
        "commercial_opportunity": [],
    }
    assert list(skeleton["key_points"]) == [
        "indication_audience",
        "lab_background",
        "proposal",
        "clinical_actionability",
        "key_questions",
        "commercial_opportunity",
    ]
```

  and update its docstring "…ORDERED equality against the six groups…".
  - Rename `test_science_weights_sum_to_thirty_five_and_the_prose_says_so` → `test_science_weights_sum_to_fifty_and_the_prose_says_so`; asserts `science_total == 50`, `commercial_total == 50` (the two prose asserts are unchanged — they format from the totals).
  - Add:

```python
def test_item_seven_lists_the_six_groups_with_their_bullet_counts_in_skeleton_order():
    """The prose contract and the skeleton must agree on names, order and
    counts; the engine warns against the same counts
    (src/agent/simulation.py `_KEY_POINT_GROUP_BULLETS`)."""
    from src.agent.simulation import _KEY_POINT_GROUP_BULLETS

    text = _phase4_text()
    item = text[text.index("7. **Key points.**"): text.index("8. **Elevator pitch.**")]
    body = _norm(item)
    words = {1: "**one bullet**", 2: "**two bullets**"}
    positions = []
    for key in _skeleton()["key_points"]:
        at = body.index(f"`{key}` — {words[_KEY_POINT_GROUP_BULLETS[key]]}")
        positions.append(at)
    assert positions == sorted(positions)
    assert "at most 300 characters" in body
    assert list(_KEY_POINT_GROUP_BULLETS) == list(_skeleton()["key_points"])


def test_item_seven_confines_lab_background_to_the_record():
    body = _norm(_phase4_text())
    assert "leave out anything not on that record" in body


def test_phase4_forbids_an_unsourced_evidence_ceiling_and_dates_stale_facts():
    body = _norm(_phase4_text())
    assert "Never assert an evidence ceiling you have not checked" in body
    assert "Date what can go stale" in body
```

  (`_norm`, `_phase4_text` and `_skeleton` already exist in this file.)

- [ ] **Step 8 (integrator, Task 6): Run** `.venv-test/bin/python scripts/sync_prompt_set_docs.py` then `.venv-test/bin/python -m pytest tests/unit/test_headline_contract.py tests/unit/test_rubric_prompt_sync.py tests/unit/test_doc_prompt_sync.py tests/unit/test_pitch_contract.py tests/unit/test_claude_md_disclosure_sync.py -q` — expected PASS.

---

### Task 3: key-point groups, read helper, both surfaces, chat record

**Files:**
- Modify: `src/services/assessment_detail.py:110-155`
- Modify: `src/routers/admin.py:57,142-147` and `src/routers/manager.py:65,99-102`
- Modify: `templates/admin/_assessments_body.html:349-403`
- Modify: `templates/admin/_assessment_detail_body.html:107-179`
- Modify: `src/services/assessment_chat_record.py:48,331-337`
- Modify: `src/models/opportunity.py:68-73` (comment)
- Create: `tests/unit/test_key_point_sections.py`
- Modify: `tests/integration/test_assessment_detail_page.py`
- Modify: `tests/integration/test_assessment_queue_controls.py`
- Modify: `tests/unit/test_assessment_chat_record.py:230-240`
- Modify: `tests/integration/test_assessment_chat_parity.py`

**Interfaces — Produces (all in `src/services/assessment_detail.py`):**
- `KEY_POINT_GROUPS: tuple[tuple[str, str], ...]` (six, current)
- `LEGACY_KEY_POINT_GROUPS: tuple[tuple[str, str], ...]` (five, display-only)
- `KEY_POINT_ACCEPTED_KEYS: frozenset[str]` (union — write acceptance)
- `key_point_shape(value: object) -> str | None` → one of `"flat"`, `"current"`, `"legacy"`, `"mixed"`, or `None`
- `key_point_sections(value: object) -> list[tuple[str | None, list[str]]]`
- `normalize_key_points(value: object) -> list | dict | None` (same name, widened)

- [ ] **Step 1: write `tests/unit/test_key_point_sections.py`.**

```python
"""Key-point groups: the current six (scout_hub >= 1.8.0), the legacy five
(1.3.0–1.7.1, display-only) and the one read helper every surface uses."""
from src.services.assessment_detail import (
    KEY_POINT_ACCEPTED_KEYS,
    KEY_POINT_GROUPS,
    LEGACY_KEY_POINT_GROUPS,
    key_point_sections,
    key_point_shape,
    normalize_key_points,
)

CURRENT = [k for k, _ in KEY_POINT_GROUPS]
LEGACY = [k for k, _ in LEGACY_KEY_POINT_GROUPS]


def test_the_current_and_legacy_sets_are_the_reviewers_and_the_old_ones():
    assert KEY_POINT_GROUPS == (
        ("indication_audience", "Indication / Audience"),
        ("lab_background", "Lab Background"),
        ("proposal", "Proposal"),
        ("clinical_actionability", "Clinical Actionability"),
        ("key_questions", "Key Questions/Experiment"),
        ("commercial_opportunity", "Commercial Opportunity"),
    )
    assert LEGACY_KEY_POINT_GROUPS == (
        ("significance", "Significance"),
        ("innovation", "Innovation"),
        ("clinical_actionability", "Clinical actionability"),
        ("key_questions", "Key questions / experiments"),
        ("commercial_potential", "Commercial potential"),
    )
    assert KEY_POINT_ACCEPTED_KEYS == frozenset(CURRENT) | frozenset(LEGACY)


def test_shape_classification():
    assert key_point_shape(["a"]) == "flat"
    assert key_point_shape({"significance": ["s"]}) == "legacy"
    assert key_point_shape({"lab_background": ["l"]}) == "current"
    assert key_point_shape({"key_questions": ["q"]}) == "current"   # shared key only
    assert key_point_shape({"significance": ["s"], "proposal": ["p"]}) == "mixed"
    assert key_point_shape({}) is None
    assert key_point_shape(None) is None
    assert key_point_shape("text") is None


def test_a_six_group_row_renders_in_the_new_order_under_the_new_labels():
    value = {k: [f"{k} point"] for k in reversed(CURRENT)}   # seeded out of order
    sections = key_point_sections(value)
    assert [label for label, _ in sections] == [label for _, label in KEY_POINT_GROUPS]
    assert sections[0] == ("Indication / Audience", ["indication_audience point"])


def test_a_five_group_legacy_row_keeps_its_own_labels_and_order():
    value = {k: [f"{k} point"] for k in reversed(LEGACY)}
    sections = key_point_sections(value)
    assert [label for label, _ in sections] == [label for _, label in LEGACY_KEY_POINT_GROUPS]


def test_a_three_group_1_3_0_row_keeps_its_own_labels_and_order():
    value = {"commercial_potential": ["c"], "significance": ["s"], "innovation": ["i"]}
    assert key_point_sections(value) == [
        ("Significance", ["s"]), ("Innovation", ["i"]), ("Commercial potential", ["c"]),
    ]


def test_a_mixed_row_hides_nothing():
    value = {"significance": ["s"], "proposal": ["p"], "key_questions": ["q"]}
    assert key_point_sections(value) == [
        ("Proposal", ["p"]), ("Key Questions/Experiment", ["q"]), ("Significance", ["s"]),
    ]


def test_empty_blank_unknown_and_non_list_groups_render_nothing():
    assert key_point_sections({"significance": [], "innovation": []}) == []
    assert key_point_sections({"proposal": ["  ", ""]}) == []
    assert key_point_sections({"not_a_group": ["x"]}) == []
    assert key_point_sections({"proposal": "not a list"}) == []
    assert key_point_sections({"proposal": [" p ", "", None, 3]}) == [("Proposal", ["p"])]
    assert key_point_sections(None) == []
    assert key_point_sections([]) == []


def test_a_flat_list_is_one_unlabelled_section():
    assert key_point_sections([" a ", "", "b"]) == [(None, ["a", "b"])]


def test_normalize_accepts_current_legacy_and_mixed_and_strips_blanks():
    six = {k: ["x"] for k in CURRENT}
    five = {k: ["x"] for k in LEGACY}
    assert normalize_key_points(six) == six
    assert normalize_key_points(five) == five
    assert normalize_key_points({"significance": ["s"], "proposal": ["p"]}) == {
        "significance": ["s"], "proposal": ["p"],
    }
    assert normalize_key_points({"proposal": [" p ", "  "]}) == {"proposal": ["p"]}
    assert list(normalize_key_points(dict(reversed(list(six.items()))))) == list(reversed(CURRENT))


def test_normalize_still_rejects_unknown_keys_and_wrong_types():
    assert normalize_key_points({"proposal": ["p"], "open_questions": ["y"]}) is None
    assert normalize_key_points({"proposal": "p"}) is None
    assert normalize_key_points({"proposal": [3]}) is None
    assert normalize_key_points({}) is None
    assert normalize_key_points("text") is None
    assert normalize_key_points(["a", " ", "b"]) == ["a", "b"]
```

- [ ] **Step 2: implement in `src/services/assessment_detail.py`.** Replace the block from the `#: F3. scout_hub >= 1.3.0 emits \`key_points\`` comment through the end of `normalize_key_points` (≈l.110-155) with:

```python
#: scout_hub >= 1.8.0 (the 2026-09-22 Blackbird review): six named groups, in
#: the reviewer's own labels. The (key, label) order is the render order on
#: both assessment surfaces and in the assessment-chat record.
KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("indication_audience", "Indication / Audience"),
    ("lab_background", "Lab Background"),
    ("proposal", "Proposal"),
    ("clinical_actionability", "Clinical Actionability"),
    ("key_questions", "Key Questions/Experiment"),
    ("commercial_opportunity", "Commercial Opportunity"),
)

#: scout_hub 1.3.0-1.7.1 (1.3.0 carried the first, second and last). Kept for
#: two reasons only: a stored verdict renders under the labels and order it was
#: written with (design D2), and a sidecar from a stale prompt still stores
#: rather than losing the field to `raw_verdict`.
LEGACY_KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("significance", "Significance"),
    ("innovation", "Innovation"),
    ("clinical_actionability", "Clinical actionability"),
    ("key_questions", "Key questions / experiments"),
    ("commercial_potential", "Commercial potential"),
)

_CURRENT_KEY_POINT_KEYS = frozenset(k for k, _ in KEY_POINT_GROUPS)
_LEGACY_KEY_POINT_KEYS = frozenset(k for k, _ in LEGACY_KEY_POINT_GROUPS)
_LEGACY_ONLY_KEY_POINT_KEYS = _LEGACY_KEY_POINT_KEYS - _CURRENT_KEY_POINT_KEYS
_CURRENT_ONLY_KEY_POINT_KEYS = _CURRENT_KEY_POINT_KEYS - _LEGACY_KEY_POINT_KEYS

#: Write-time acceptance: the UNION. A rename that accepted only the current
#: keys would drop `key_points` in both skew directions — a 1.8.0 prompt on an
#: old image, and a stale prompt on this one.
KEY_POINT_ACCEPTED_KEYS: frozenset[str] = _CURRENT_KEY_POINT_KEYS | _LEGACY_KEY_POINT_KEYS


def key_point_shape(value: object) -> str | None:
    """``"flat"`` (<= 1.2.0 list), ``"legacy"`` (a legacy-only key and no
    current-only key), ``"current"``, ``"mixed"`` (both), or None for anything
    that is not a non-empty list/dict."""
    if isinstance(value, list):
        return "flat"
    if not isinstance(value, dict) or not value:
        return None
    keys = set(value)
    legacy = bool(keys & _LEGACY_ONLY_KEY_POINT_KEYS)
    current = bool(keys & _CURRENT_ONLY_KEY_POINT_KEYS)
    if legacy and current:
        return "mixed"
    return "legacy" if legacy else "current"


def _clean_bullets(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x.strip() for x in value if isinstance(x, str) and x.strip()]


def key_point_sections(value: object) -> list[tuple[str | None, list[str]]]:
    """The renderable key-point sections of a stored value, in display order.

    A grouped value yields ``(label, bullets)`` pairs: a legacy-shaped row uses
    `LEGACY_KEY_POINT_GROUPS`; every other row uses `KEY_POINT_GROUPS` followed
    by any legacy-only group it also carries, so nothing stored is hidden. A
    flat list yields one ``(None, bullets)`` pair. Only non-empty groups are
    returned — blank bullets dropped, unknown keys and non-list values ignored —
    so "is there anything to show" is the truthiness of the result. Both
    assessment templates and the chat record render from this, which is what
    keeps the page and the record identical.
    """
    shape = key_point_shape(value)
    if shape == "flat":
        bullets = _clean_bullets(value)
        return [(None, bullets)] if bullets else []
    if shape is None:
        return []
    if shape == "legacy":
        groups = LEGACY_KEY_POINT_GROUPS
    else:
        groups = KEY_POINT_GROUPS + tuple(
            (key, label) for key, label in LEGACY_KEY_POINT_GROUPS
            if key in _LEGACY_ONLY_KEY_POINT_KEYS
        )
    sections: list[tuple[str | None, list[str]]] = []
    for key, label in groups:
        bullets = _clean_bullets(value.get(key))
        if bullets:
            sections.append((label, bullets))
    return sections


def normalize_key_points(value: object) -> list | dict | None:
    """Write-time shape check for the sidecar's ``key_points``.

    Accepts the flat list (<= 1.2.0) and any grouped object whose keys are a
    non-empty SUBSET of `KEY_POINT_ACCEPTED_KEYS` — current, legacy or both —
    with every value a list of strings. Blank bullets are stripped. Anything
    else is None: a malformed narrative field never costs the verdict (A20),
    and `raw_verdict` keeps the original. An UNKNOWN key is still rejected
    outright — that is real shape drift, and `_persist_assessment` logs it.
    """
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return [x.strip() for x in value if x.strip()]
    if isinstance(value, dict) and value and set(value) <= KEY_POINT_ACCEPTED_KEYS and all(
        isinstance(v, list) and all(isinstance(x, str) for x in v) for v in value.values()
    ):
        return {k: [x.strip() for x in v if x.strip()] for k, v in value.items()}
    return None
```

Also delete the now-unused `_KEY_POINT_KEYS` name (it is replaced by `KEY_POINT_ACCEPTED_KEYS`); grep the file to confirm nothing else referenced it.

- [ ] **Step 3: routers.** In `src/routers/admin.py` change the import to `from src.services.assessment_detail import build_assessment_detail, key_point_sections` and replace the `key_point_groups` registration and its comment with:

```python
# The key-point sections a stored `key_points` value renders as (current or
# legacy labels, see `key_point_sections`), used by both
# `_assessments_body.html` and `_assessment_detail_body.html`. Registered as a
# Jinja global rather than a context key: the admin assessments handler
# forbids a new one (see the comment on `_assessments_body.html`'s card-list
# block).
templates.env.globals["key_point_sections"] = key_point_sections
```

In `src/routers/manager.py` make the same import change and replace its registration with `templates.env.globals["key_point_sections"] = key_point_sections`, keeping its "See src/routers/admin.py's identical registration" comment. Grep `templates/` for `key_point_groups` afterwards — there must be no remaining use.

- [ ] **Step 4: list card.** In `templates/admin/_assessments_body.html` replace lines 349-353 (the `has_points` comment paragraph and the `{% set has_points = … %}` line) with:

```jinja
           `key_point_sections` (src/services/assessment_detail.py) returns only
           non-empty groups, under the row's own labels (current or legacy), so
           "has points" is a CONTENT check: a mapping of only empty lists, or
           of groups nothing renders, shows no box rather than an empty one. #}
        {% set kp_sections = key_point_sections(a.key_points) %}
        {% set has_points = kp_sections | length > 0 %}
```

and replace lines 382-403 (from `{% if has_points and a.key_points is mapping %}` through the matching `{% endif %}`) with:

```jinja
            {% if has_points %}
            <div class="assessment-card-points rounded-lg border border-gray-200 bg-white p-3">
                <div class="text-xs font-semibold uppercase tracking-wide text-gray-500">Key points</div>
                {% if kp_sections[0][0] is none %}
                <ul class="mt-1 list-disc list-inside text-sm text-gray-700 space-y-0.5">
                    {% for point in kp_sections[0][1] %}<li>{{ plain_citations(point) }}</li>{% endfor %}
                </ul>
                {% else %}
                <div class="mt-1 space-y-2">
                    {% for label, points in kp_sections %}
                    <div><span class="font-semibold text-gray-800">{{ label }}</span>
                        <ul class="list-disc list-inside text-sm text-gray-700">
                        {% for point in points %}<li>{{ plain_citations(point) }}</li>{% endfor %}
                        </ul></div>
                    {% endfor %}
                </div>
                {% endif %}
            </div>
            {% endif %}
```

- [ ] **Step 5: detail page.** In `templates/admin/_assessment_detail_body.html` replace line 113 with:

```jinja
    {% set kp_sections = key_point_sections(a.key_points) %}
    {% set has_points = kp_sections | length > 0 %}
```

and replace lines 158-179 (from `{% if has_points and a.key_points is mapping %}` through the matching `{% endif %}`) with:

```jinja
        {% if has_points %}
        <div class="assessment-brief-keypoints">
            <div class="text-sm font-semibold text-gray-600">Key points</div>
            {% if kp_sections[0][0] is none %}
            <ul class="assessment-brief-points assessment-prose max-w-none mt-1 list-disc list-inside space-y-0.5">
                {% for point in kp_sections[0][1] %}<li>{{ plain_citations(point) }}</li>{% endfor %}
            </ul>
            {% else %}
            <div class="assessment-brief-points assessment-prose max-w-none mt-1 space-y-2">
                {% for label, points in kp_sections %}
                <div><span class="font-semibold">{{ label }}</span>
                    <ul class="list-disc list-inside">
                    {% for point in points %}<li>{{ plain_citations(point) }}</li>{% endfor %}
                    </ul></div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
        {% endif %}
```

- [ ] **Step 6: chat record.** In `src/services/assessment_chat_record.py` change the import to `from src.services.assessment_detail import build_assessment_detail, key_point_sections` and replace the `if isinstance(a.key_points, dict): … else: …` block (≈l.331-337) with:

```python
    for group_label, points in key_point_sections(a.key_points):
        for point in points:
            if group_label is None:
                doc.add("Key point", [point], anchor="brief")
            else:
                doc.add(f"Key point — {_label_safe(group_label)}", [point], anchor="brief")
```

- [ ] **Step 7: model comment.** In `src/models/opportunity.py` replace the `key_points` comment lines (`# 3-5 short strings (scout_hub <= 1.2.0), or the three-group object` … `# \`normalize_key_points\`/\`KEY_POINT_GROUPS\`.`) with:

```python
    # 3-5 short strings (scout_hub <= 1.2.0); the three-group (1.3.0) or
    # five-group (1.4.0-1.7.1) legacy object; or, from 1.8.0, the six-group
    # object — see src/services/assessment_detail.py's `KEY_POINT_GROUPS`,
    # `LEGACY_KEY_POINT_GROUPS`, `normalize_key_points` and
    # `key_point_sections`.
```

(keep the following `none_as_null=True` sentences unchanged).

- [ ] **Step 8: detail-page tests.** Add to `tests/integration/test_assessment_detail_page.py` (next to `test_the_five_key_point_groups_render_in_order`, whose docstring should now say it pins the LEGACY five):

```python
async def test_the_six_key_point_groups_render_in_order(client, db_session, admin):
    """scout_hub 1.8.0: the reviewer's six groups, in order, under their labels —
    seeded out of order on purpose."""
    _, assessment = await _seed(db_session)
    assessment.key_points = {
        "commercial_opportunity": ["Comm point"],
        "key_questions": ["Question point"],
        "proposal": ["Proposal point"],
        "indication_audience": ["Indication point"],
        "clinical_actionability": ["Actionability point"],
        "lab_background": ["Lab point"],
    }
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    block = body[
        body.index("assessment-brief-keypoints") : body.index("assessment-signals")
    ]
    labels = (
        "Indication / Audience", "Lab Background", "Proposal",
        "Clinical Actionability", "Key Questions/Experiment", "Commercial Opportunity",
    )
    positions = [block.index(label) for label in labels]
    assert positions == sorted(positions), dict(zip(labels, positions, strict=True))
    assert "Significance" not in block and "Innovation" not in block


async def test_a_row_whose_groups_render_nothing_shows_no_key_points_column(
    client, db_session, admin
):
    """An unknown-only (hand-built) or blank-only mapping is not "there are key
    points": no column, no two-column grid."""
    _, assessment = await _seed(db_session)
    assessment.elevator_pitch = "PITCH-MARKER. Hopkins has data on 124 patients."
    assessment.key_points = {"not_a_group": ["x"], "proposal": ["  "]}
    await db_session.flush()

    body = _main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text)
    assert "assessment-brief-keypoints" not in body
    assert "md:grid-cols-2" not in body
```

- [ ] **Step 9: list-card tests.** In `tests/integration/test_assessment_queue_controls.py` add after `test_the_list_page_renders_the_pitch_and_key_points_side_by_side`:

```python
async def test_the_card_renders_the_six_groups_in_order(client, db_session, admin):
    run, _ = await _seed_narrative_row(
        db_session, project="Six Groups Co",
        elevator_pitch="PITCH-MARKER: one tube of blood, two-week answer.",
        key_points={
            "commercial_opportunity": ["C-MARK"], "lab_background": ["L-MARK"],
            "indication_audience": ["I-MARK"], "proposal": ["P-MARK"],
            "key_questions": ["Q-MARK"], "clinical_actionability": ["A-MARK"],
        },
    )
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    block = html[html.index("assessment-card-points"):]
    marks = ["I-MARK", "L-MARK", "P-MARK", "A-MARK", "Q-MARK", "C-MARK"]
    assert [block.index(m) for m in marks] == sorted(block.index(m) for m in marks)
    assert "Indication / Audience" in block and "Commercial Opportunity" in block
```

and in the page-size test replace the `points = {…}` fixture with the six current keys at realistic length:

```python
    counts = {
        "indication_audience": 1, "lab_background": 2, "proposal": 2,
        "clinical_actionability": 2, "key_questions": 1, "commercial_opportunity": 2,
    }
    bullet = ("A complete claim of realistic length for triage, naming the disease, "
              "the population, the asset and the evidence it rests on. ") * 2
    points = {key: [bullet[:225]] * n for key, n in counts.items()}
```

Leave `CEILING` unchanged here; the integrator re-measures it (Task 6).

- [ ] **Step 10: chat-record test.** In `tests/unit/test_assessment_chat_record.py`, import `RUBRIC_VERSION` from `src.services.blackbird_rubric` and change the archived case's expected string to `f"an archived revision from the revision registry; the current rubric is {RUBRIC_VERSION}"` (the parametrize list becomes a list built at import time with that f-string). Add:

```python
def test_six_group_key_points_are_quoted_under_their_labels_in_order():
    detail = synthetic_detail()
    detail["assessment"].key_points = {
        "commercial_opportunity": ["KP-C"], "indication_audience": ["KP-I"],
        "lab_background": ["KP-L"],
    }
    record = build_chat_record(detail, tier="staff")
    text = _all_text(record)
    assert text.index("Indication / Audience") < text.index("Lab Background") < text.index(
        "Commercial Opportunity"
    )
    assert "KP-I" in text and "KP-L" in text and "KP-C" in text
```

- [ ] **Step 11: parity positive control.** In `tests/integration/test_assessment_chat_parity.py`, under "# Positive controls", add `assert "PARITY-KP-SIGNIFICANCE" in record_text and "PARITY-KP-QUESTIONS" in record_text` (the legacy-shaped seed must be quoted, not silently dropped).

- [ ] **Step 12 (integrator, Task 6): Run** `.venv-test/bin/python -m pytest tests/unit/test_key_point_sections.py tests/unit/test_assessment_chat_record.py tests/integration/test_assessment_detail_page.py tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_chat_parity.py -q` — expected PASS (page-size test may need the ceiling re-measured).

---

### Task 4: engine warnings, owed-headline override, backfill guard

**Files:**
- Modify: `src/agent/simulation.py:66-70` (imports), `:3736-3738` (signature), `:3826-3833` (render call), `:4252-4259` (sweep), `:4603-4660` (key-point warnings), `:9322-9327` (constants)
- Modify: `scripts/backfill_dropped_verdicts.py:94-95,316-363,546-556`
- Modify: `tests/integration/test_assessment_narrative_fields.py:273-317` (+ new tests)
- Modify: `tests/integration/test_assessment_headline_delivery.py` (new test)
- Modify: `tests/unit/test_backfill_dropped_verdicts.py` (new test)

**Interfaces:**
- Consumes (Task 3): `KEY_POINT_GROUPS`, `LEGACY_KEY_POINT_GROUPS`, `KEY_POINT_ACCEPTED_KEYS`, `key_point_shape`, `normalize_key_points` from `src/services/assessment_detail.py`.
- Produces: `_KEY_POINT_GROUP_BULLETS: dict[str, int]` and `_KEY_POINT_BULLET_CHARS = 300` in `src/agent/simulation.py` (Task 2's test imports the former); `SimulationEngine._post_assessment_summary(agent, thread, verdict, slack_ts, *, score: float | None = None, band: str | None = None) -> bool`; `scripts.backfill_dropped_verdicts._refuse_stamp_drift(rubric_version, rubric_hash, *, allow_rubric_drift: bool = False) -> str | None` and a `--allow-rubric-drift` CLI flag.

- [ ] **Step 1: constants.** In `src/agent/simulation.py` replace

```python
# Task 7 / F3: the grouped (>= 1.3.0) key_points shape bounds each of the
# named groups individually rather than the flat 3-5 total above.
_KEY_POINT_GROUP_MIN = 1
_KEY_POINT_GROUP_MAX = 2
```

with

```python
# scout_hub >= 1.8.0: the exact bullet count each current group carries
# (prompt item 7), and the per-bullet bound. Warnings only (design D12) — a
# shape violation is never a drop. Keys and order are pinned to
# KEY_POINT_GROUPS by tests/unit/test_rubric_prompt_sync.py.
_KEY_POINT_GROUP_BULLETS = {
    "indication_audience": 1,
    "lab_background": 2,
    "proposal": 2,
    "clinical_actionability": 2,
    "key_questions": 1,
    "commercial_opportunity": 2,
}
_KEY_POINT_BULLET_CHARS = 300
```

- [ ] **Step 2: imports.** Extend the `from src.services.assessment_detail import (…)` block (≈l.66-70) to also import `KEY_POINT_ACCEPTED_KEYS`, `LEGACY_KEY_POINT_GROUPS` and `key_point_shape`.

- [ ] **Step 3: warnings.** Replace the `elif isinstance(key_points, dict):` branch (≈l.4610-4658, through the "omits %d of %d groups" warning) with:

```python
        elif isinstance(key_points, dict):
            # The whole field is about to be DROPPED (stored NULL, kept only in
            # `raw_verdict`) for any dict `normalize_key_points` rejects: an
            # unknown group key, or a value that is not a list of strings.
            if normalize_key_points(key_points) is None:
                unknown = sorted(set(key_points) - KEY_POINT_ACCEPTED_KEYS)
                logger.warning(
                    "[%s] Assessment key_points was DROPPED (stored NULL; the "
                    "value survives only in raw_verdict): %s. Keys present: %s",
                    agent_id,
                    f"unknown group key(s) {unknown}" if unknown
                    else "a group value is not a list of strings",
                    sorted(key_points),
                )
            shape = key_point_shape(key_points)
            legacy_only = sorted(
                set(key_points)
                & ({k for k, _ in LEGACY_KEY_POINT_GROUPS} - set(_KEY_POINT_GROUP_BULLETS))
            )
            if shape in ("legacy", "mixed"):
                # A stale prompt (scout_hub < 1.8.0) on this image: stored and
                # rendered under the legacy labels, never dropped — but the
                # per-group checks below describe the CURRENT contract, so a
                # legacy object gets this one warning instead of "omits 4 of 6".
                logger.warning(
                    "[%s] Assessment key_points uses pre-1.8.0 group name(s) %s; "
                    "stored and rendered under the legacy labels. Is "
                    "prompts/roles/scout_hub at 1.8.0 on this host?",
                    agent_id, legacy_only,
                )
            if shape in ("current", "mixed"):
                for group_key, expected in _KEY_POINT_GROUP_BULLETS.items():
                    group = key_points.get(group_key)
                    if isinstance(group, list) and len(group) != expected:
                        logger.warning(
                            "[%s] Assessment key_points.%s carries %d bullets "
                            "(contract asks for %d)",
                            agent_id, group_key, len(group), expected,
                        )
                    if isinstance(group, list):
                        for bullet in group:
                            if isinstance(bullet, str) and len(bullet) > _KEY_POINT_BULLET_CHARS:
                                logger.warning(
                                    "[%s] Assessment key_points.%s has a %d-char "
                                    "bullet (contract asks for at most %d)",
                                    agent_id, group_key, len(bullet),
                                    _KEY_POINT_BULLET_CHARS,
                                )
                # An ABSENT group is `None` and fails the isinstance above, so
                # the count check cannot see it; a partial object still stores
                # (normalize accepts a subset), so name the omission here.
                absent = [k for k in _KEY_POINT_GROUP_BULLETS if k not in key_points]
                if absent:
                    logger.warning(
                        "[%s] Assessment key_points omits %d of %d groups: %s",
                        agent_id, len(absent), len(_KEY_POINT_GROUP_BULLETS),
                        ", ".join(absent),
                    )
```

Grep the file afterwards: `_KEY_POINT_GROUP_MIN`, `_KEY_POINT_GROUP_MAX` and the `KEY_POINT_GROUPS` loop in this branch must be gone; keep `KEY_POINT_GROUPS` in the import only if still referenced elsewhere in the file (grep; drop it from the import if not).

- [ ] **Step 4: override.** Change the signature to

```python
    async def _post_assessment_summary(
        self, agent: Agent, thread: ThreadState, verdict: dict, slack_ts: str | None,
        *, score: float | None = None, band: str | None = None,
    ) -> bool:
```

add to its docstring: "``score``/``band``: the STORED values, passed only by `_announce_owed_headline`; never read from ``verdict`` — a sidecar dict can carry a model-written ``weighted_score``." In the `render_assessment_headline(...)` call add `score=score, band=band,` after `permalink=permalink,`. In `_announce_owed_headline` change the call to:

```python
        posted = await self._post_assessment_summary(
            agent, thread, verdict, row.slack_ts,
            score=row.weighted_score, band=row.band,
        )
```

with a one-line comment above it: `# Stored score/band, not a live recomputation: a weights change since the row was written must not re-band a public headline.`

- [ ] **Step 5: backfill guard.** In `scripts/backfill_dropped_verdicts.py` add `from src.services.blackbird_rubric import RUBRIC_WEIGHTS` next to the existing rubric imports and `from src.services.rubric_revisions import resolve_revision`; add after `_derive_rubric_stamp`:

```python
def _refuse_stamp_drift(
    rubric_version: str | None,
    rubric_hash: str | None,
    *,
    allow_rubric_drift: bool = False,
) -> str | None:
    """None when rows stamped (rubric_version, rubric_hash) may be scored with
    the LIVE weights; otherwise the reason to refuse.

    `_score_and_band` always scores with the live document, so a stamp whose
    revision weighs the dimensions differently (rubric 3.5.0 changed them)
    would store a score that revision never produced. An unstamped run
    resolves to the live view and behaves exactly as before this guard.
    `--allow-rubric-drift` overrides.
    """
    if allow_rubric_drift:
        return None
    view, _provenance = resolve_revision(rubric_version, rubric_hash)
    if view is None:
        return (
            f"refusing: rubric stamp {rubric_version!r}/{rubric_hash!r} matches no "
            "revision in prompts/rubric/revisions.toml; pass --allow-rubric-drift "
            "to score with the live weights anyway"
        )
    stamped = {d.key: d.weight for d in view.dimensions}
    live = dict(RUBRIC_WEIGHTS)
    if stamped != live:
        return (
            f"refusing: rubric {view.version} weighs the dimensions {stamped} but "
            f"the live document weighs them {live}; recovered rows would carry a "
            "score their stamp never produced. Pass --allow-rubric-drift to override"
        )
    return None
```

In `_build_arg_parser()` add:

```python
    parser.add_argument(
        "--allow-rubric-drift",
        action="store_true",
        help=(
            "score recovered verdicts with the live weights even when the run's "
            "rubric stamp weighs the dimensions differently"
        ),
    )
```

and in `main()`, right after the `logger.info("rubric stamp for this run: …")` call:

```python
        refusal = _refuse_stamp_drift(
            rubric_version, rubric_hash, allow_rubric_drift=args.allow_rubric_drift,
        )
        if refusal is not None:
            logger.error("%s", refusal)
            return 1
```

- [ ] **Step 6: narrative-field tests.** In `tests/integration/test_assessment_narrative_fields.py`, rename `test_a_missing_key_point_group_is_stored_and_warned` → `test_a_legacy_shaped_key_points_is_stored_with_one_legacy_warning`, keep its `partial = {"significance": ["s"], "innovation": ["i"]}` seed and `row.key_points == partial` assert, and replace its two warning asserts with `assert "pre-1.8.0 group name(s)" in warnings` and `assert "key_points omits" not in warnings`. Add, modelled on it (same imports, `SimulationEngine` stub, `_persist_assessment` call and `finally: await _delete_run(...)`):
  - `test_a_missing_current_group_is_stored_and_warned`: `partial = {"indication_audience": ["i"], "proposal": ["p", "q"]}` → stored equal; warnings contain `"key_points omits 4 of 6 groups"` and `"lab_background"`.
  - `test_a_wrong_count_and_an_overlong_bullet_are_warned_not_dropped`: all six keys with the right counts except `key_questions: ["a", "b"]`, and `proposal: ["x" * 301, "ok"]` → stored equal; warnings contain `"key_points.key_questions carries 2 bullets (contract asks for 1)"` and `"key_points.proposal has a 301-char bullet"`.

- [ ] **Step 7: headline test.** Add to `tests/integration/test_assessment_headline_delivery.py`:

```python
@pytest.mark.asyncio
async def test_an_owed_headline_uses_the_rows_stored_band_not_a_live_recompute(
    engine, monkeypatch,
):
    """Rubric 3.5.0 changed the weights: a verdict stored under 3.4.0 that still
    owes its headline must be announced with the band it was GIVEN. The live
    recomputation is sabotaged here so a regression cannot pass by accident."""
    import src.services.assessment_headline as headline_module

    monkeypatch.setattr(headline_module, "_rubric_weighted_score", lambda _s: 1.0)
    monkeypatch.setattr(headline_module, "_rubric_band", lambda _s: "pass")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, _agent = _hub(factory, run_id)
    _wire_summary_channel(sim)
    client = sim.slack_clients["blackbird"]
    async with factory() as db:
        db.add(OpportunityAssessment(
            simulation_run_id=run_id, agent_id="blackbird",
            channel_name="single-cell-omics", thread_id="owed-stored-band",
            recommendation="advance", weighted_score=3.4, band="advance",
            scores={"differentiation_unmet_need": 3, "scientific_credibility": 3,
                    "translational_path": 3, "fundable_experiment": 5,
                    "venture_potential": 3, "team_executability": 4},
        ))
        await db.commit()
    try:
        assert await sim._announce_owed_headline("owed-stored-band", trigger="test")
        [posted] = _headlines(client)
        assert "band: advance, score: 3.4" in posted["text"]
        assert "band: pass" not in posted["text"]
    finally:
        await _delete_run(factory, run_id)
```

- [ ] **Step 8: backfill tests.** Add to `tests/unit/test_backfill_dropped_verdicts.py` (import `_refuse_stamp_drift` in the existing `from scripts.backfill_dropped_verdicts import (…)` block, and `RUBRIC_CONTENT_HASH, RUBRIC_VERSION` from `src.services.blackbird_rubric`):

```python
def test_the_guard_allows_the_live_stamp_and_an_unstamped_run():
    assert _refuse_stamp_drift(RUBRIC_VERSION, RUBRIC_CONTENT_HASH) is None
    assert _refuse_stamp_drift(None, None) is None


def test_the_guard_refuses_a_stamp_whose_weights_differ_from_the_live_document():
    reason = _refuse_stamp_drift("3.4.0", "b7b0a1d6a4a5")   # registry entry, 25/20/15/15/15/10
    assert reason is not None
    assert "refusing" in reason and "3.4.0" in reason and "--allow-rubric-drift" in reason


def test_the_guard_refuses_an_unknown_stamp():
    reason = _refuse_stamp_drift("9.9.9", "000000000000")
    assert reason is not None and "matches no revision" in reason


def test_the_override_allows_any_stamp():
    assert _refuse_stamp_drift("3.4.0", "b7b0a1d6a4a5", allow_rubric_drift=True) is None
    assert _refuse_stamp_drift("9.9.9", "000000000000", allow_rubric_drift=True) is None
```

- [ ] **Step 9 (integrator, Task 6): Run** `.venv-test/bin/python -m pytest tests/integration/test_assessment_narrative_fields.py tests/integration/test_assessment_headline_delivery.py tests/unit/test_backfill_dropped_verdicts.py tests/unit/test_assessments_summary_post.py -q` — expected PASS.

---

### Task 5: CLAUDE.md deploy box

**Files:** Modify: `CLAUDE.md` (insert immediately after the `0051_assessment_chat` deploy box, before `> ### ⚠️ The assessment archive: never purge, never delete a run row.`)

- [ ] **Step 1:** Insert:

```markdown
> **The 2026-09-25 reviewer change ships NO migration — rubric 3.5.0 and
> scout_hub 1.8.0 together, and all three images rebuild.** Design:
> `docs/specs/2026-09-24-reviewer-rubric-and-key-points-design.md`.
>
> * **Rubric 3.4.0 → 3.5.0.** Weights 25/25/25/15/5/5 (science block 50), the
>   fundable-experiment anchor tightened to a $100K–$300K / 6–12-month
>   decisive result, `[stage_bar.budget]` re-derived. Thresholds unchanged.
>   Stored scores/bands are never rescored; the outgoing 3.4.0 entry is in
>   `prompts/rubric/revisions.toml`. The web tier and the agent SUPERVISOR
>   parse the document once at import — the supervisor at container boot — so
>   both must restart.
> * **scout_hub 1.7.1 → 1.8.0.** `key_points` is six groups (Indication /
>   Audience, Lab Background, Proposal, Clinical Actionability, Key
>   Questions/Experiment, Commercial Opportunity). Rows stored under the old
>   five (or three) keep their own labels; writes accept old and new keys.
>   **The hazardous half is prompt-without-image:** an old image rejects the
>   six new keys and stores `key_points` NULL. `prompts/` is live from the
>   moment the tree lands, so no run may start between landing and `up -d
>   agent`.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-3.5.0
>     done
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC up -d blackbird-app worker
>     $DC up -d agent        # ONLY when /admin/simulation shows no live run
>
> Start the next run FRESH: a resume only warns about the rubric change and
> would mix 3.4.0- and 3.5.0-scored verdicts in one run.
>
> Rollback: the `rollback-pre-3.5.0` images plus a revert commit — and that
> revert must APPEND a 3.5.0 entry to `prompts/rubric/revisions.toml`
> (sha256[:12] of the 3.5.0 file), or every row and review stamped 3.5.0
> renders "matches no entry". Old images render only `clinical_actionability`
> and `key_questions` of a six-group row; the other four groups stay in the
> column, hidden until the new code is back.
```

---

### Task 6 (integrator — not dispatched): integrate, merge, then verify and audit

- [ ] **Step 1 (before fan-out):** place the spec at `docs/specs/2026-09-24-reviewer-rubric-and-key-points-design.md` and this plan at `docs/plans/2026-09-25-reviewer-rubric-and-key-points.md`; implement Task 3 Step 2 (`src/services/assessment_detail.py`).
- [ ] **Step 2 (after all 12 reports):** reconcile conflicting assumptions by editing directly; `git status` / `git diff --stat` in the worktree must show only files in the Package map (plus the two docs and the two generated artifacts below).
- [ ] **Step 3:** regenerate the derived documents (generation, not verification): `…/.venv-test/bin/python scripts/sync_prompt_set_docs.py` (re-embeds the prompt set in `docs/specs/2026-08-07-hub-bot-prompts.md`), `…/.venv-test/bin/python scripts/render_rubric_review_doc.py`, then `pandoc` the new `docs/rubric-review/blackbird-rubric-v3.5.0-*-review.md` to `.docx`; the file name's hash must equal `sha256sum prompts/rubric/blackbird-rubric.toml | cut -c1-12`.
- [ ] **Step 4:** commit on `feat/reviewer-rubric-key-points`; confirm `/admin/simulation` shows no live run (the `simulation_process_status` row is `idle`); merge into local `blackbird` from the live tree: `git -C /home/ubuntu/blackbird-copi-science merge --ff-only feat/reviewer-rubric-key-points` (fall back to a normal merge only if `blackbird` moved; never touch the uncommitted `docker-compose.prod.yml`). From this moment the 1.8.0 prompt is live for any run the old images start — report that to the operator.
- [ ] **Step 5:** run the package test commands (Tasks 1-4, "Run" steps) in the worktree (same commit as `blackbird`); triage failures with `engineering:test-triage`, repair in the worktree, commit, fast-forward `blackbird` again.
- [ ] **Step 6:** page-size test: if `tests/integration/test_assessment_queue_controls.py::test_the_list_page_stays_under_a_size_ceiling` fails, measure the populated 50-row page and set `CEILING` to the measurement +20%, recording the measurement in the docstring as that docstring requires.
- [ ] **Step 7:** `VENV_PY=/home/ubuntu/blackbird-copi-science/.venv-test/bin/python ./scripts/ci.sh` — must end `==> CI passed.` (baseline at 91f34f1: 4283 passed, 95 skipped, coverage 86.06%).
- [ ] **Step 8:** adversarial audit of the full diff against this plan and the spec: `engineering:plan-auditor` + `engineering:semantic-reviewer` (Opus); fix confirmed findings, re-run CI, re-audit the fixed areas; commit and fast-forward `blackbird`.
- [ ] **Step 9:** stop before any image build or deploy; report to the operator.
