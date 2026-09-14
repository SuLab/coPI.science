# Strengths / risks box: explanatory bullets — implementation plan (2026-09-14)

**Status:** revision 2 (2026-09-14), post-adversarial-audit. A read-only
auditor checked revision 1 against the tree at `cd6e1ab` and returned one
BLOCKING finding (the migration number is pinned in `scripts/migrate/preflight.py`
and three tests that revision 1 did not list), two MAJOR (six derivation tests
and four template-footnote assertions that the contract change would break),
and eleven MINOR corrections. All are folded in below and marked *(audit)*.
Every claim was read from the tree on 2026-09-14; nothing is taken from memory
or from earlier plan documents.

**Operator request:** the "Strengths and risks" card on both assessment detail
pages is uninformative — each bullet is `label — stock string` ("scored 4 of
5", "met", "gap", "adequate"). The operator wants explanatory prose in bullet
form, delivered as **two layers**: (1) enrich the derived bullets from text the
row already stores, so every existing row improves without a migration; and
(2) ask the hub to write its own strengths and risks bullets in the sidecar for
every new verdict, rendered ahead of the derived ones.

**Operator decision recorded here as D9:** this **reverses D2** of
`docs/plans/2026-09-14-assessment-ux-and-prompt-suggestions-implementation-plan.md`
("derived from stored data, not from new model-written fields"). The derived
bullets stay as the fallback and are always labelled as derived; the new
model-written bullets are always labelled as the hub's own words. The two are
never mixed in one list.

---

## 0. Facts the plan rests on (all verified in the tree)

| # | Fact | Where |
|---|---|---|
| F1 | The card is rendered ONLY in `templates/admin/_assessment_detail_body.html` (`id="signals"`, `:176-243`), included by `templates/admin/assessment_detail.html:126` and `templates/manager/assessment_detail.html:101`. `_assessments_body.html:14` only mentions it in a Jinja comment *(audit)*. | |
| F2 | The context key is `verdict_signals`, built by `derive_strengths_and_risks(assessment, dimensions=, consults=, revision=)` at `src/services/assessment_detail.py:612-782`, called once at `:1011`. Return shape `{"strengths","risks","unestablished": [ {source,label,detail} ], "scale_known": bool}` is pinned by `tests/unit/test_assessment_strength_risk_derivation.py::test_the_return_shape_is_the_pinned_contract`. | |
| F3 | Each `dimensions` entry carries `key, title, weight, weight_note, score, pct` (`assessment_detail.py:856-885`). `RevisionDimension` (`src/services/rubric_revisions.py:35-40`) has `key, title, weight, weight_note` — **no anchors/description**. The live rubric document (`prompts/rubric/blackbird-rubric.toml`) has per-dimension `anchors` and per-gate `title`/`description` (`:172-182`), but the revision registry does not carry them, and the detail page renders older rows against their OWN revision, not the live document. | |
| F4 | Each consult dict (`_load_consults`, starts `:1165`) *(audit)* carries `domain, verdict_signal, confidence, question, concerns, concern_count, questions_to_ask, raw_opinion (admin only), reply_truncated, read_state, created_at`. It does **not** carry `established`, although `SpecialistConsult.established` (JSONB list, migration 0038, written since 2026-08-28) exists with three states: NULL never asked, `[]` nothing came back, non-empty list = evidence. | `src/models/specialist_consult.py:105-120` |
| F5 | `red_flags` bullets already carry the flag's full text. `score_rationale` (Text, 0048), `rationale`, `key_points` (5-group object), `recommended_next_experiment` are stored and rendered elsewhere on the same page; `score_rationale` is a single free-text blob with no per-dimension structure (prompt item 10 asks for 2-3 sentences ≤500 chars). | `phase4-thread-reply.md:291-300` |
| F6 | `_persist_assessment` (`src/agent/simulation.py:4374-`) builds `assessment_kwargs` with `_str_or_none` for Text fields and `normalize_key_points` for the group object; every narrative field degrades to NULL on a wrong type and `raw_verdict` keeps the original (rule A20). Soft-limit WARNINGs are at `:4533-4611`; the constants `_PROJECT_SOFT_LIMIT`/`_PITCH_SOFT_LIMIT` at `:9209-9226` *(audit)*. | |
| F7 | The sidecar skeleton lives at `phase4-thread-reply.md:349-373`; `tests/unit/test_rubric_prompt_sync.py::test_skeleton_carries_the_narrative_fields` (`:272`) parses it and asserts the narrative keys; `test_the_scout_hub_prompt_set_version_is_1_4_0_or_later` (`:303`; a second harmless pin `!= "1.0.0"` at `:356-369`) *(audit)* reads `prompts/roles/scout_hub/role.toml` `version = "1.4.0"`. | |
| F8 | `docs/specs/2026-08-07-hub-bot-prompts.md` embeds `phase4-thread-reply.md` verbatim; `tests/unit/test_doc_prompt_sync.py` fails on drift; `scripts/sync_prompt_set_docs.py` regenerates it. | |
| F9 | `prose_format == 'markdown'` gates `md-content` rendering for `elevator_pitch`, `score_rationale`, `recommended_next_experiment`, `rationale` (template `:96-152, :255, :580`). | |
| F10 | Latest migration is `0048_assessment_score_rationale` (`alembic/versions/`). `OpportunityAssessment` maps `key_points` as `JSONB` list|dict (`src/models/opportunity.py:75`). | |
| F11 | Empty-state text today: strengths "No dimension scored at or above 4, and no gate met." (hard-codes the 1-5 threshold), risks "None recorded." A 3-of-5 dimension lands in **no** bucket by design (`derive_strengths_and_risks` docstring). | template `:192, :207` |
| F12 | The `#assessments-summary` headline (`src/services/assessment_headline.py`, design D12) publishes label, recommendation, band/score, permalink and clipped `elevator_pitch` only. `score_rationale` is app-only by D3. | CLAUDE.md, `phase4-thread-reply.md:296` |
| F14 *(audit)* | **The migration number is pinned outside `alembic/versions/`.** `scripts/migrate/preflight.py:74` `DEFAULT_TARGET = "0048"`, `:135-139` `SUPPORTED_START_REVISIONS` (exact tuple ending `"0047"`), `:421-423` last `PlannedObject`, `:425-429` `REVISION_ORDER` ending `"0048"`. Pinned by `tests/unit/test_migration_checks.py:230-236`, `:833-834`, `:870-915` (every `add_column` in an `upgrade()` must be a `PlannedObject`) and `tests/integration/test_harness_smoke.py:62` (`assert v == "0048"`). | |
| F15 *(audit)* | `load_rubric()` (`src/services/blackbird_rubric.py:418-420`) returns the import-time `Rubric` whose `gating: dict[str, dict[str,str]]` carries `title`/`description` per key (`:138`, `:248-258`). `RUBRIC_VERSION`, `load_rubric`, `BANDING` are already imported at `assessment_detail.py:59`; `PROVENANCE_UNKNOWN` at `:61`; `blackbird_rubric.py` imports only stdlib — no circular import. `build_assessment_detail` already holds `revision_provenance` (`:992`). | |
| F16 *(audit)* | `rubric_revisions.py:138-154` resolves a row by version AND hash; same version with a wrong hash is `PROVENANCE_UNKNOWN` (`revision is None`). `PROVENANCE_LIVE` and `live_revision_view()` (`:116`) exist. The integration page fixture `_seed` (`test_assessment_detail_page.py:58-170`) stamps NO `rubric_version` (PROVENANCE_UNSTAMPED); only `_seed_scale_fixture:671` and `_seed_stamped:708` stamp. `:1891` asserts the lowercase gate label `"translational potential"` in the card. | |
| F17 *(audit)* | No existing test pins `score_rationale` out of the headline. `tests/unit/test_assessments_summary_post.py::test_the_headline_leaks_no_rationale_red_flags_gating_or_raw_verdict` (`:118-147`) is the D12 sentinel, driven by `LEAKY_VERDICT` (`:107-115`) and `_LEAK_SENTINELS` (`:98-105`). | |
| F18 *(audit)* | `weight_note` is not always `"N%"`: archived dual-scale revisions carry `"6%/4% (investment/incubation)"` (`rubric_revisions.py:86-91`). | |
| F19 *(audit)* | The 0043/0048 persistence tests live in `tests/integration/test_assessment_narrative_fields.py` (`:98-217`), with the `engine`/`async_sessionmaker` harness, mandatory `finally: await _delete_run(...)` (`:17-37`), and the caplog convention `with caplog.at_level(logging.WARNING): ... assert "..." in caplog.text` (`:247`). | |
| F20 *(audit)* | `tests/unit/test_claude_md_disclosure_sync.py` slices CLAUDE.md from the bullet `- **Inside an interview thread the hub is reply-only` (`CLAUDE.md:1489`) to the next `- **`, and checks `_visibility_clauses` (`:75-85`). The 0048 deploy box is at `CLAUDE.md:1318-~1360`. | |
| F21 *(audit)* | `test_assessment_detail_page.py` `_signals_card` (`:1802-1812`) slices `id="signals"` → first `signals-provenance`; `_signal_columns` (`:1815-1829`) indexes the FIRST occurrence of each of `assessment-signals-strengths`/`-risks`/`-unestablished`/`signals-legend`, asserts `i < j < k < legend`. `:1928` requires `<div id="signals"`. Footnote substrings asserted: `:1865` "Derived from this verdict", `:1866` "dimension scores, gating states, red", `:1909` "could not be classified", `:1910` "not in the registry". Six derivation tests assert exact entry keys or whole-dict equality: `test_assessment_strength_risk_derivation.py:115`, `:220-222`, `:227-229`, `:234-236`, `:270-272`, `:282`. | |
| F13 | The review bot reads prompt files as plain data and never imports the rubric module; it does not read `key_points`/`score_rationale` from rows (`grep` in `src/services/review_bot.py`, `interview_transcript.py` → 0 hits). | |

Things this plan does **not** know and does not assume:

* Whether a simulation run is live on the host right now. Task D1 checks
  before any deploy step.
* How many `opportunity_assessments` rows exist in production today (the 0048
  docstring says 20 as of its writing; treat as a floor).
* Whether `established` is non-empty on any production consult row. Layer 1's
  consult enrichment therefore renders correctly for all three states
  (F4) rather than assuming evidence exists.

---

## 1. Design

### 1.1 Layer 1 — derived bullets, enriched from stored text (no migration)

Principle unchanged from D2: **quote, never judge**. A bullet may carry text
the row already stores, attributed to its source; it may not synthesise a
sentence the hub or a specialist did not write, and it may not attribute a
sentence of `score_rationale` to one dimension by keyword matching (no
deterministic attribution exists — F5). So:

| source | today's `detail` | new `detail` / `body` |
|---|---|---|
| dimension (strength/risk) | "scored 4 of 5" | unchanged `detail`; **plus** `body` = `["weight: " + weight_note]` rendered VERBATIM (F18 — dual-scale notes exist) *(audit)*; no body when `weight is None` (row-extras). |
| gating met / not met | "met" / "not met" | unchanged `detail`; **plus** `body` = `[load_rubric().gating[key]["description"]]` and `label` = `load_rubric().gating[key]["title"]` **only when `revision_provenance == PROVENANCE_LIVE`** (F15/F16) *(audit: version alone is weaker than the repo's own hash-aware resolver)*. `PROVENANCE_UNSTAMPED` is NOT live: the page fixture stamps nothing and `:1891` asserts the lowercase label, so an unstamped or non-live row keeps today's `key.replace("_"," ")` label and no body. |
| red flag | full text | unchanged. |
| consult strength (`adequate`/`clear`) | "adequate" | `detail` unchanged; **plus** `body` = the consult's `established` items (non-empty list only, F4), each rendered as a sub-bullet, capped at 3 with "and N more" — labelled "what the {domain} specialist established". `[]`/NULL → no body, and the bullet stays a bare label (no fabricated positive). |
| consult risk (`blocking`/`gap`/`caution`) | "gap" | `detail` unchanged; **plus** `body` = the consult's `concerns` items, capped at 3 with "and N more" — labelled "the {domain} specialist's concerns". Empty → no body. |
| not-established bucket | stock strings | **unchanged** — these strings are the informative part (operator agreed). |

Also in layer 1:

* **`score_rationale` is linked from the card, not repeated on it** *(audit
  decision)*. The amber "Why this score" box (template `:141-156`) is the last
  child of `#brief` and `#signals` is the next card, so a second placement
  would repeat the same paragraph in two adjacent cards and would need a
  duplicated `prose_format` branch to satisfy `:1961` and `:1989-1990`. Instead:
  add `id="score-rationale"` to the div at `:142`, and put a one-line pointer
  ("The hub's own account of the score is in *Why this score* above") inside
  the `signals-provenance` footnote (`:233-242`, outside the `_signals_card`
  slice), conditional on `a.score_rationale`, in the pattern of the jump nav at
  `:56`.
* **Empty-state text stops hard-coding "4"** and states what the emptiness
  means: strengths → "No dimension scored at or above {threshold} of
  {scale_max}, and no gate was met." computed from the revision when
  `scale_known`, otherwise "Dimension scores could not be classified (unknown
  rubric revision)". Risks → "No dimension scored at or below {threshold} of
  {scale_max}, no gate was unmet, no red flag was raised, and no specialist
  signalled a gap." A row whose every scored dimension is mid-scale gets one
  extra neutral line above the three columns: "All N scored dimensions sit
  mid-scale (between {risk_threshold} and {strength_threshold}), so neither
  column lists them." — derived from the same `dimensions` list, never from a
  new judgement. **Placement constraint** *(audit, F21)*: the mid-scale line
  and any lead-in sit between the `<h3>` (`:178`) and the grid (`:179`), never
  between the grid and the legend, or they land inside the unestablished slice.

Contract change to `derive_strengths_and_risks`: each entry gains
`body: list[str]`, **present on every entry** (empty list when nothing to
quote — uniform shape so the template can `{% if item.body %}` without
`.get`), and the function returns two new keys: `thresholds: {"strength":
float|None, "risk": float|None, "scale_max": float|None}` and
`mid_scale_count: int`. It gains one keyword argument, `revision_provenance`,
passed from `build_assessment_detail` (`:992`). `source/label/detail` keep
their meaning. **Seven existing tests change, not one** *(audit)*: the
return-shape test (`:99-103`), the key-set assertion at `:115`, and the five
whole-dict equalities at `:220-222`, `:227-229`, `:234-236`, `:270-272`, `:282`
each gain `"body": []`. All other assertions in that file are untouched.

`_load_consults` gains `"established": list(row.established) if
isinstance(row.established, list) else None` — carried, not coerced, so the
NULL/`[]` distinction survives to the deriver.

### 1.2 Layer 2 — model-written bullets in the sidecar (migration 0049, prompt set 1.5.0)

Two new sidecar keys, items 11 and 12 in `phase4-thread-reply.md`:

```
"strengths": [],
"risks": [],
```

Prompt text (to be written in the same register as items 7 and 10):
2-4 bullets each, ≤200 characters, each a complete claim naming the evidence
it rests on; a risk names what would resolve it; **staff-only, never posted
to Slack** (same sentence form as item 10, so
`test_phase4_marks_the_score_rationale_staff_only`'s pattern can be reused for
the new fields); bound by the confidentiality rule (unpublished disclosures
may appear here since the fields are app-only — the same permission item 10
grants, stated the same way); no bare `~`; no score numbers or band (computed
server-side). Add both keys to the "never write a bare `~`" list.

Storage: `opportunity_assessments.strengths JSONB(none_as_null=True)` and
`.risks JSONB(none_as_null=True)`, both nullable, migration
`0049_assessment_strengths_risks`. Additive; NULL on every pre-0049 row,
never backfilled (same rule as 0043/0048).

Normaliser: `normalize_bullets(value) -> list[str] | None` in
`assessment_detail.py` next to `normalize_key_points`: a non-empty list whose
every element is a non-empty `str` (after `.strip()`) is returned stripped;
anything else → None (A20; `raw_verdict` keeps it). Soft-limit WARNINGs in
`_persist_assessment` for count outside 2-4 and any bullet >200 chars, mirroring
the `key_points` group warnings; storage proceeds either way.

Render: on the card, when `assessment.strengths` or `.risks` is non-NULL, each
column shows the hub's bullets **first** under a sub-heading "In the hub's
words", then the derived list under "Derived from the stored verdict". When
both are NULL (every existing row) the card looks exactly like layer 1 alone.
Bullets are plain text (`{{ bullet }}` escaped), NOT `md-content`: the prompt
asks for single claims, and a list item is not a Markdown document.

Not published: `assessment_headline.py` is untouched (the engine passes
explicit kwargs at `simulation.py:3813-3820` and `render_assessment_headline`
has no parameter for any of the three). No test pins `score_rationale` out of
the headline today (F17) *(audit)*, so C6 extends the D12 sentinel: add
`"score_rationale": "SENTINEL_SCORE_RATIONALE"`, `"strengths":
["SENTINEL_STRENGTH"]`, `"risks": ["SENTINEL_RISK"]` to `LEAKY_VERDICT` and the
three sentinels to `_LEAK_SENTINELS` in `tests/unit/test_assessments_summary_post.py`.

### 1.3 What is deliberately out of scope

* The rubric document: no `[meta].version` bump, so no `revisions.toml` entry.
* pi_lab prompts and `thread_guidance.py`: untouched, so the pi_lab golden
  master (`tests/characterization/__snapshots__/test_agent_turn_gm.ambr`) is
  untouched. Verify with `grep -c "strengths" ...ambr` before and after — must
  not change.
* The assessments LIST pages: they do not render the card (F1).
* Any backfill of `strengths`/`risks` for existing rows.
* Any simulation start.

---

## 2. Tasks

Packages are disjoint by file. A, B, C can be implemented in parallel; D is
sequential after the merged suite is green.

### Task A — service layer (`src/services/assessment_detail.py`, unit tests)

A1. `_load_consults`: add `"established"` (F4 rule above).
A2. `derive_strengths_and_risks`: add `body`, `thresholds`, `mid_scale_count`
    and the `revision_provenance` kwarg per §1.1; gate title/description via
    `load_rubric().gating[key]` (F15) behind `revision_provenance ==
    PROVENANCE_LIVE`; import `PROVENANCE_LIVE` next to `PROVENANCE_UNKNOWN`
    (`:61`). Update the single call site (`:1011`). Docstring table updated.
A3. `normalize_bullets` per §1.2, with docstring stating the A20 rule.
A4. Tests in `tests/unit/test_assessment_strength_risk_derivation.py`:
    - the seven tests named in §1.1 updated for `body`/new keys *(audit)*;
      every other existing test unchanged and passing;
    - new: consult strength with non-empty `established` carries body; with
      `[]` carries no body; with NULL carries no body;
    - consult risk with concerns carries body capped at 3 + "and N more";
    - gate body and title present only with `revision_provenance ==
      PROVENANCE_LIVE` (use `live_revision_view()` for the revision); absent
      for `PROVENANCE_UNKNOWN` and `PROVENANCE_UNSTAMPED`, where the label stays
      lowercase `key.replace("_"," ")`;
    - dimension body is `weight_note` verbatim, including a dual-scale note;
      absent when `weight is None`;
    - `mid_scale_count` counts only scored dims strictly between thresholds;
    - `thresholds` are None when `scale_known` is False.
    New file `tests/unit/test_normalize_bullets.py`: list of strings → stripped
    list; empty list → None; list with a non-string → None; list with an
    empty string → None; dict/str/None → None.

### Task B — template (`templates/admin/_assessment_detail_body.html`, integration tests)

B1. Card per §1.1 and §1.2: lead-in `score_rationale` block (or anchor — audit
    decides), hub-words sub-lists first, derived sub-lists second, `body`
    sub-bullets, computed empty-state text, mid-scale neutral line. Keep every
    existing CSS hook the tests slice on: `assessment-signals-strengths`,
    `assessment-signals-risks`, `assessment-signals-unestablished`,
    `signals-legend`, `signals-provenance`, `signals-empty`, `signal-entry`,
    `signal-source-*`. Add `signal-hub-words`, `signal-derived`,
    `signal-body`, `signals-midscale`.
B2. Provenance footnote updated: names both origins ("In the hub's words" =
    written by BlackbirdBot in its verdict; "Derived" = classified from stored
    scores, gates, flags and specialist signals, quoting the specialists'
    stored text), plus the conditional `#score-rationale` pointer (§1.1).
    **Keep these four literal fragments verbatim** *(audit, F21)*: "Derived
    from this verdict", "dimension scores, gating states, red", and in the
    `scale_known` false branch (`:237-241`) "could not be classified" and "not
    in the registry". Each of `assessment-signals-strengths`, `-risks`,
    `-unestablished`, `signals-legend` must appear exactly once, in that order;
    no new class name may contain those substrings.
B3. `tests/integration/test_assessment_detail_page.py`: the `_signals_card`
    slicing helper (`:1802-1830`) must still work — confirm the card's end
    marker `signals-provenance` is still the last element. New tests:
    - a row with `strengths`/`risks` renders the hub-words list before the
      derived list in each column;
    - a row with both NULL renders no "In the hub's words" heading;
    - a consult with `established` shows the sub-bullet text;
    - empty-state text carries the revision's thresholds, not a literal "4";
    - a row with six mid-scale scores shows the mid-scale line;
    - the manager page renders the same card (both routes share the body).
    The existing assertion at `:1854` (`"No dimension scored"`) still matches
    the new strengths sentence's prefix as worded in §1.1; keep that prefix.
    `:1891` (lowercase "translational potential") keeps passing because the
    default fixture is unstamped (F16).

### Task C — engine, model, migration, prompt (disjoint from A and B)

C1. `alembic/versions/0049_assessment_strengths_risks.py`: two nullable JSONB
    columns; docstring in the 0048 style (deploy order, never backfilled).
C1b *(audit, BLOCKING in rev 1)*. Migration pins outside `alembic/`:
    `scripts/migrate/preflight.py` — `DEFAULT_TARGET = "0049"`, append `"0048"`
    to `SUPPORTED_START_REVISIONS`, append `"0049"` to `REVISION_ORDER`, add
    `PlannedObject("0049", "column", "strengths", "opportunity_assessments")`
    and the same for `risks`. `tests/unit/test_migration_checks.py:231-236` —
    extend the start tuple with `"0048"`, target `"0049"`.
    `tests/integration/test_harness_smoke.py:62` — `"0049"` plus a `# 0049 …`
    comment line in the style of `:59-61`.
C2. `src/models/opportunity.py`: map both as `JSONB(none_as_null=True)`, with
    the three-state comment (NULL = pre-0049 or wrong type; list = hub's
    bullets).
C3. `src/agent/simulation.py` `_persist_assessment`: `strengths=normalize_bullets(verdict.get("strengths"))`, same for risks; soft-limit WARNINGs (§1.2); constants `_HUB_BULLETS_MIN = 2`, `_HUB_BULLETS_MAX = 4`, `_HUB_BULLET_CHARS = 200` beside `_PITCH_SOFT_LIMIT`. Import `normalize_bullets` alongside `normalize_key_points` (`:66`).
C4. `prompts/roles/scout_hub/phase4-thread-reply.md`: items 11 and 12, skeleton
    keys, tilde list; `role.toml` `version = "1.5.0"`.
C5. `.venv-test/bin/python scripts/sync_prompt_set_docs.py` (F8).
C6. Tests: `tests/unit/test_rubric_prompt_sync.py` — extend
    `test_skeleton_carries_the_narrative_fields` to assert `strengths == []`
    and `risks == []`; add `test_phase4_marks_strengths_and_risks_staff_only`
    that **slices the prompt from `"11. **"` to the tilde paragraph** before
    asserting "never posted to Slack" — a whole-file check passes vacuously
    off item 10 *(audit)*; bump the version test (`:303`) to `>= (1, 5, 0)`.
    Items 11/12 go after item 10 (`:291-300`) and before the tilde paragraph
    (`:302`), inside the sidecar-instructions slice, so `test_roles.py:318-375`
    (forbidden words in the visible-body slice) is unaffected; new prose must
    avoid the `_FORBIDDEN` phrases in `tests/unit/test_doc_prompt_sync.py:68-86`.
    Persistence tests go in `tests/integration/test_assessment_narrative_fields.py`
    beside the 0043/0048 siblings (F19), with `finally: await _delete_run(...)`
    and the file's caplog convention *(audit)*: valid lists store; wrong type
    stores NULL and `raw_verdict` keeps it; a 5-bullet list still stores and
    logs the WARNING.
    Headline sentinel extension per §1.2 (F17).
C7. CLAUDE.md: a `0049` deploy-order box in the 0048 style (migrate before
    serve; agent rebuild required; prompt-without-image writes NULL forever;
    image-without-prompt is benign since `normalize_bullets(None)` is None).
    Insert it **directly after the 0048 box (`:1318-~1360`) and before the
    "assessment archive" box**, never inside the reply-only bullet at `:1489`
    that `test_claude_md_disclosure_sync.py` slices, and do not use the phrase
    "a PI or another lab sees" alongside gating/recommendation/red
    flags/confidence in it (F20) *(audit)*.

### Task D — verification and deploy (sequential, operator-gated)

D1. `./scripts/ci.sh` on the host (alembic round-trip, ruff, full pytest with
    coverage floor). Zero failures before anything else.
D2. Confirm the golden master is byte-identical: `git diff --stat
    tests/characterization/` is empty.
D3. Check for a live run **before** touching containers:
    `docker inspect copi-blackbird-agent-1 --format '{{.State.Status}}'` and the
    `/admin/simulation` badge. If a run is live, stop from `/admin/simulation`
    (the in-process drain), then confirm "Simulation stopping..." in its log.
D4. Deploy in the 0048 order: build app+worker, build agent, `alembic upgrade
    head` from a one-off container, `alembic current` == `0049`, `up -d
    blackbird-app worker`, `up -d agent` (supervisor returns IDLE). No run is
    started by this plan.
D5. Open one existing assessment on `/admin/assessments/{id}` and on the
    manager route: card renders layer 1 (no "In the hub's words"), consult
    sub-bullets present where `established`/`concerns` exist, empty-state text
    carries thresholds.

---

## 3. Risks and how the plan answers them

| Risk | Answer |
|---|---|
| Derived bullets read as the hub's claim (the D2 concern) | Two labelled sub-lists, never merged; derived bodies are verbatim quotes of stored specialist text or rubric metadata, never synthesis. |
| Gate description from the live rubric mislabels an older row | Version guard on `rubric_version == RUBRIC_VERSION`; older rows keep bare labels. |
| Prompt-without-image skew | `normalize_bullets` lives in the image; without it the new keys are ignored and stored NULL, and `raw_verdict` keeps them. CLAUDE.md box states it. |
| Unpublished PI disclosures leaking | Fields are app-only; headline function untouched and pinned by a new test. |
| `_signals_card` test helper breaks on the new layout | B3 keeps `signals-provenance` as the end marker and adds tests rather than rewriting the helper. |
| `score_rationale` shown twice on one page | Resolved: anchor link in the footnote, no second placement *(audit)*. |
| Migration number pinned outside `alembic/` | C1b updates `preflight.py` and the three pin tests *(audit)*. |
| Live gate prose rendered against a non-live row | Guard is `revision_provenance == PROVENANCE_LIVE`, hash-aware, unstamped excluded *(audit)*. |
| Suite runtime on sshfs | Run CI on the host per CLAUDE.md. |
