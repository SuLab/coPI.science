# Reviewer rubric edits and six key-point groups — design

**Status:** approved in conversation 2026-09-24; adversarially audited 2026-09-25 (9 findings, all folded in below). The operator directed implementation without a separate spec-review stop.
**Scope:** rubric 3.4.0 → 3.5.0, scout_hub prompt set 1.7.1 → 1.8.0, key-point
rendering on both assessment surfaces, two consequential fixes. No migration.
**Inputs:** two documents from a Blackbird reviewer (docx metadata
`lastModifiedBy: Jason Zavras`):

- `blackbird-rubric-0922.docx` — an edited rubric review copy (modified 2026-09-22).
- `09-21-run.docx` — five assessment cards with "Proposed Changes" (created
  2026-09-22, modified 2026-09-24).

Every factual claim below was checked against the repository, the production
database (read-only) or the documents themselves, and cross-checked by two
independent adversarial audits. Line numbers are as of `91f34f1`.

---

## 1. What the documents say

### 1.1 The rubric document

**Provenance.** The reviewer edited the **3.2.0** review copy
(`docs/rubric-review/blackbird-rubric-v3.2.0-42aec0479ac6-review.docx`, generated
2026-08-27), not the live 3.4.0 document. Evidence: identical `docProps/core.xml`
creation timestamp (2026-08-27T20:40:08Z); leftover empty bookmarks for the
deleted Appendices A/B; no stage-bars section (the renderer emits one from 3.3.0
on). Only a 3.2.0 `.docx` was ever generated.

**The reviewer's edits — exactly two.** No tracked changes, comments, hidden
text, highlights or colour in `word/document.xml`.

1. Weights 25/20/15/15/15/10 → **25/25/25/15/5/5**:

   | Dimension | 3.4.0 | 3.5.0 |
   |---|---|---|
   | `differentiation_unmet_need` | 25 | 25 |
   | `scientific_credibility` | 20 | **25** |
   | `translational_path` | 15 | **25** |
   | `fundable_experiment` | 15 | 15 |
   | `venture_potential` | 15 | **5** |
   | `team_executability` | 10 | **5** |

2. `fundable_experiment` anchor: "$100K–$1M grant over 12–24 months" →
   "**$100K–$300K** grant over **6–12 months**"; "less than a $200K budget" →
   "less than **the proposed budget**"; the sentence "State and regional
   non-dilutive leverage (wherever the lab's institution is eligible) adds."
   deleted.

The header block, the generated footer and Appendices A/B were also deleted —
review-copy chrome, not rubric content.

**Differences from the live document that are NOT edits** (artifacts of the
3.2.0 base): the Vocabulary line reads `pass (decline)` (live `pass_label` has
been `decline` since 3.4.0), and the `[stage_bar*]` section (added in 3.3.0) is
absent. The live versions of both are kept.

**Left inconsistent by the edit** (the reviewer never saw these):

- `[scoring].preamble` still says the science block carries **35%** and the
  commercial block **65%**. Under the new weights it is **50% / 50%**. The review
  copy's own note says this prose must be re-derived when weights change.
- `[stage_bar.budget]` restates the old anchor ("$100K–$1M grant over 12–24
  months … under a $200K budget").
- File comments at `blackbird-rubric.toml:225-235` (weight rationale), `:299`
  (">$200K replication") and `:354` ("the 35/65 weighting").

### 1.2 The run document

It covers the five verdicts of run `75ca77f9` (2026-09-22) — the whole run:
#1 konig, #2 coller, #3 slusher, #4 rothstein, #5 vogelstein. For each card:

- **Title.** "No change" except #2: "An intrathecal oligonucleotide polyA booster
  for children with SYNGAP1 epilepsy that boosts the protein output from their
  one working gene copy" — **143 characters** (cap 110) with an unexplained
  "polyA" (the headline allows one abbreviation, spelled out).
- **In One Minute.** "No change" except #1, which replaces "Evidence is in vitro
  only." with a sentence naming a mouse xenograft. The hub had access to that
  fact: konig's public profile lists mouse xenograft models
  (`profiles/public/konig.md:25`, `:49`) and the 9G4 preprint under PubMed's
  bioRxiv journal title (`:100`). This is a hub accuracy error, not a missing
  tool. The wrong sentence starts at character 570 and ends past 600, so the
  public `#assessments-summary` excerpt (cut at the last sentence end inside 600)
  should not contain it — inferred from the clip rule, not checked in Slack.
- **Key points.** All five rewritten into six groups — Indication / Audience;
  Lab Background; Proposal; Clinical Actionability; Key Questions/Experiment;
  Commercial Opportunity — with bullet counts 1/2/2/2/1/2 on every card.
  Bullets: median 223 characters, max 440, 44 of 50 over today's 160;
  2,216–2,663 characters of key-point text per card (today's cards ≈1,200–1,500).

**Content the exemplars carry that the hub cannot source.** Most Lab Background
credentials and spinouts (e.g. "Bloomberg Distinguished Professor", WyveRNA,
Slusher's 18 years in pharma and four spinouts, Ludwig Center/HHMI, TRBV9
engagers) are absent from the PI profiles the hub reads, and the hub's Core
Rule 1 (`prompts/roles/scout_hub/agent-system.md:14-16`) forbids stating PI
facts not drawn from the profile, publications or the PI. Several dated facts
(QurAlis interim data Feb 2026, Trace Phase 1/2 mid-2026, the Rare Pediatric
Disease voucher reauthorisation) cannot come from any hub tool — the hub has no
web or news search. None of the reviewer's facts has been verified by us.

---

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | What are the five rewritten cards? | **Exemplars only.** They define the new groups; the five stored verdicts stay exactly as the hub wrote them (no edits, no provenance field). |
| D2 | How do older verdicts render? | **As stored, under their original labels and order.** Only new verdicts use the six groups. |
| D3 | Key-point length | **Match the exemplars:** bullet counts fixed at 1/2/2/2/1/2, each bullet ≤300 characters (40 of 50 exemplar bullets fit). |
| D4 | Is $100K–$300K / 6–12 months the grant size? | **No — it only tightens the killer-experiment scoring bar.** Every other $100K–$1M / 12–24-month statement stays (team anchor, `[stage_bar.talent]`, hub and PI prompts, budget specialist, tool descriptions). The PI prompt and its snapshot are untouched. |
| D5 | Lab Background sourcing | **Profile, publications and the interview only;** anything not on that record is left out. |
| D6 | Competitors in key points | **Allowed**, as the exemplars do, in Clinical Actionability and Commercial Opportunity. The staff-only competitive-landscape section stays as the fuller list. (No reviewer-tier accounts exist today: 3 admins, 3 managers.) |
| D7 | Headline | **Keep ≤110 characters and the one-abbreviation rule;** add a rule to name modality and route accurately, in the field's own terms where a plain noun would be vague or wrong (the reviewer kept "Oral drug…", "Cell therapy…", "A test…" and "A screening platform…" unchanged and rewrote only "injected RNA drug"). |
| D8 | Fundable anchor wording | **The reviewer's text plus one clause** stating the grant itself can run larger. |
| D9 | Evidence claims (default, accepted) | The hub never asserts an evidence ceiling ("in vitro only") without a source. Stating the most advanced evidence stays with item 14 (evidence maturity); the new rule is a prohibition only, so it cannot push unpublished results into the publicly posted pitch. |
| D10 | Recent facts (default, accepted) | Time-sensitive facts carry a date; unsourced recent events are never presented as current. No new tool. |
| D11 | Proposal format (default, accepted) | Bullets open "The asset: …" and "The work: …". |
| D12 | Enforcement (default, accepted) | Counts and the 300-character bound are warnings only; nothing is truncated or dropped. |
| D13 | Versioning (default, accepted) | Rubric **3.5.0**, thresholds 3.4/2.8 retained; prompt set **1.8.0**; one spec, one plan, one deploy. |

---

## 3. Rubric 3.5.0 — `prompts/rubric/blackbird-rubric.toml`

### 3.1 Content changes (exact)

**`[meta]`** — `version = "3.5.0"`, `date = "2026-09-25"`, and a new changelog
entry at the top:

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

**Weights** — `scientific_credibility` 25, `translational_path` 25,
`venture_potential` 5, `team_executability` 5 (the other two unchanged).

**`fundable_experiment.anchors`** — the reviewer's text verbatim, then the D8
clause:

> Could a $100K–$300K grant over 6–12 months buy a *decisive* de-risking
> result? 5 = a crisp, quantified killer experiment within budget; 1 = no
> experiment articulable, scope far beyond an incubation grant, or key data
> that cannot be replicated for less than the proposed budget and a reasonable
> timeline. The incubation grant can run larger; this asks whether a decisive
> result comes within the first $100K–$300K and 6–12 months.

**`[scoring].preamble`** — "carry 35% of the total; the four commercial
dimensions carry 65%" → "carry 50% of the total; the four commercial
dimensions carry 50%". Nothing else in the preamble changes (its first sentence
is `[stage_bar_global]` verbatim, and stays so).

**`[stage_bar.budget].text`** — re-derived from the new anchor, kept a
one-line basic string directly after its `source =` line
(`tests/unit/test_stage_bars.py` mutates it by that anchor):

> Adequate here is a scope where a $100K–$300K grant over 6–12 months could buy
> a DECISIVE de-risking result — the incubation grant can run larger, but the
> bar is whether the decisive result comes that early and that cheaply. Below
> the bar is no articulable experiment, scope far beyond an incubation grant,
> or key data unreplicable for less than the proposed budget and a reasonable
> timeline.

**Comments** — `:225-235` restated for the new weights (attributed to the
2026-09-22 review; the 2026-08-27 back-test history kept as history); `:299`
">$200K replication" → "replication costlier than the proposed budget";
`:354` "the 35/65 weighting" → "the 50/50 weighting"; the `[banding]` comment
records that 3.4/2.8 were retained at 3.5.0 and the back-test result.

**Unchanged:** thresholds, `pass_label = "decline"`, gating, dimension keys and
titles, evidence lists, red flags, recommendation, heuristic, the team anchor's
12–24 months and every stage bar except `budget`.

### 3.2 Registry and review copy

- **`prompts/rubric/revisions.toml`** — append the outgoing **3.4.0** entry
  (content hash `b7b0a1d6a4a5`, verified by `sha256sum`; scale 1–5; bands
  3.4/2.8; `pass_label = "decline"`; the six dimensions at 25/20/15/15/15/10; a
  `banding_note` saying 3.4.0 only renamed the display label) plus its
  provenance line. Without it the 21 verdicts and 2 reviews stamped 3.4.0
  render as "matches no entry" (`rubric_revisions.py:148-154`).
- **Review copy** — `scripts/render_rubric_review_doc.py` computes the
  "Note for reviewers" split from the weights instead of hard-coding "35% /
  65%" (`:105-107`); regenerate `docs/rubric-review/blackbird-rubric-v3.5.0-<hash>-review.md`
  and its `.docx` (pandoc).

### 3.3 Consequences to know

- **Band shifts** (back-test, `weighted_score` re-implemented exactly and
  reproducing all 27 stored values):

  | Verdict | Stored (3.x) | Under 3.5.0 |
  |---|---|---|
  | velculescu 2026-08-31 | advance 3.40 | conditional 3.35 |
  | coller 2026-09-09 | advance 3.50 | conditional 3.35 |
  | lamichhane 2026-09-09 | conditional 2.80 | pass 2.75 |
  | slusher 2026-09-22 | conditional 3.35 | advance 3.40 |

  Band counts advance 4→3, conditional 15→15, pass 8→9; band/recommendation
  agreement 24/27 → 22/27. Stored scores and bands are write-time facts and are
  not rewritten; only new verdicts are scored with 3.5.0.
- **Thresholds.** 3.4/2.8 were marked provisional pending ≥20 verdicts stamped
  3.x (`blackbird-rubric.toml:115-119`); there are 27. Retained per the
  reviewer; re-check is out of scope.
- **Panel obligation.** `panel_is_owed` reads the computed band, so the new
  weights change which new verdicts owe a panel near the band lines.

---

## 4. Hub prompt set 1.8.0 — `prompts/roles/scout_hub/`

`role.toml`: `version = "1.8.0"`. All edits are in `phase4-thread-reply.md`.

### 4.1 Item 6 (headline) — one added paragraph

After the three elements, directly before "No colon-stacked noun phrases":

> Name the modality, and the delivery route where it matters, accurately. A
> plain noun — "oral drug", "blood test", "cell therapy" — is right when it is
> accurate; use the field's own term when the plain word would be vague or
> wrong: "intrathecal oligonucleotide", not "injected RNA drug". The
> 110-character bound and the abbreviation rule still apply.

It sits beside element 2's plain-noun list and the existing `Write:` examples
without contradicting them: a plain noun stays correct when it is accurate.

### 4.2 Item 7 (key points) — replaced

The heading literal `7. **Key points.**` is kept (tests locate item 6 by it).

> 7. **Key points.** Six labelled groups, in this order, each holding EXACTLY
>    the number of bullets shown; each bullet is a complete claim of at most
>    300 characters, not a topic:
>    - `indication_audience` — **one bullet**: the disease or condition and the
>      patient population, its rough size (an order-of-magnitude US prevalence
>      or incidence is enough), what drives it biologically, and what those
>      patients get today.
>    - `lab_background` — **two bullets**: first, who the PI is and the lab's
>      established work that this idea builds on; second, the lab's wider
>      platform or track record — the same approach in other diseases, prior
>      programmes or spin-outs, and any existing IP or option rights worth
>      checking early. State only what the PI's public profile, their
>      publications or the lab in this interview established; leave out
>      anything not on that record rather than supplying it from general
>      knowledge.
>    - `proposal` — **two bullets**: "The asset: …" — what it is, how it works
>      and why it differs from what exists; then "The work: …" — what the grant
>      would buy.
>    - `clinical_actionability` — **two bullets**: what patients get today and
>      the clinical-stage alternatives, each with its stage, and how this
>      differs from them; then the route, endpoint or regulatory precedent that
>      would make it actionable.
>    - `key_questions` — **one bullet**: the single deciding question, phrased
>      as a question, naming the experiment that answers it.
>    - `commercial_opportunity` — **two bullets**: the closest deal comparable
>      or funding signal (who, how much, when); then the realistic commercial
>      shape — licence, platform partnership or spin-out — with the competitive
>      position and any IP or novelty caveat.
>
>    Competing programmes may be named in `clinical_actionability` and
>    `commercial_opportunity`; item 13 remains the fuller, staff-only
>    competitive landscape. Together the six must let a reviewer who reads
>    nothing else say who this is for, who is behind it, what it is, how it
>    would change care, what decides it, and why it is worth building. Record
>    them in `key_points` as an object with exactly those six keys, each an
>    array of strings.

### 4.3 Two new rules for every sidecar field

Placed immediately before "**Never write a bare `~` in any sidecar field**":

> **Never assert an evidence ceiling you have not checked.** In any sidecar
> field, do not write "in vitro only", "no animal data" or any other limit on
> the evidence unless the profile, a paper you retrieved or the lab itself
> confirms it; where you could not check, say that instead.
>
> **Date what can go stale.** A trial readout, a deal, a regulatory status or
> a programme's stage is true as of a date: write it with one ("as of 2025"),
> and never present a recent event you have no source for as current.

### 4.4 Skeleton

```
  "key_points": {"indication_audience": [], "lab_background": [], "proposal": [], "clinical_actionability": [], "key_questions": [], "commercial_opportunity": []},
```

Nothing else in the prompt set changes. `docs/specs/2026-08-07-hub-bot-prompts.md`
is re-synced with `scripts/sync_prompt_set_docs.py`.

---

## 5. Code

### 5.1 Key-point groups (`src/services/assessment_detail.py`)

- `KEY_POINT_GROUPS` becomes the six current groups, in order, with the
  exemplars' labels verbatim: Indication / Audience, Lab Background, Proposal,
  Clinical Actionability, Key Questions/Experiment, Commercial Opportunity.
- `LEGACY_KEY_POINT_GROUPS` (new, display-only) holds the 1.3.0–1.7.1 five with
  their original labels and order: Significance, Innovation, Clinical
  actionability, Key questions / experiments, Commercial potential.
- **Write acceptance is the union** of current and legacy keys. A rename
  otherwise drops `key_points` in both skew directions (new prompt on an old
  image, and a stale prompt on the new image). Unknown keys are still
  rejected; blank bullets are stripped (today they render as an empty `<li>`).
- **One read helper** returns a stored value's renderable sections:
  - a dict with legacy-only keys (`significance`, `innovation`,
    `commercial_potential`) and no current-only key → the legacy set's labels
    and order;
  - any other dict → the current set, followed by any legacy-only group present
    (so nothing stored is ever hidden);
  - a flat list (≤1.2.0 rows) → unchanged flat rendering;
  - only non-empty groups are returned, with blank bullets dropped on READ as
    well as on write, so "has points" is simply "the helper returned
    something".
  - Known, accepted edge: a legacy row holding ONLY the shared keys
    (`clinical_actionability`, `key_questions`) renders under the new labels
    for those two groups (case and wording only). Measured: 0 of the 9 grouped
    production rows have that shape; none has a blank bullet.
- The helper is registered as a Jinja global on **both** routers
  (`src/routers/admin.py`, `src/routers/manager.py` — each owns its own
  `Jinja2Templates`).

### 5.2 Consumers

- `templates/admin/_assessments_body.html` and
  `templates/admin/_assessment_detail_body.html` render from the helper; the
  duplicated `has_points` expression (`:353`, `:113`) goes away, which also
  fixes the empty "Key points" box and the misplaced "Why this score" block for
  a row whose only groups are unknown to the render loop.
- `src/services/assessment_chat_record.py` builds the key-point citations from
  the same helper, so the chat record quotes exactly what the page shows,
  legacy groups included.
- `src/models/opportunity.py` comment updated.
- Not consumers (verified): the review bot, `#assessments-summary`,
  `directory.py`, exports, `static/`.

### 5.3 Engine persist warnings (`src/agent/simulation.py` ≈4603-4660)

Warnings only (D12):

- per-group expected counts 1/2/2/2/1/2 replace `_KEY_POINT_GROUP_MIN/MAX`;
- a bullet over 300 characters (new `_KEY_POINT_BULLET_CHARS = 300`);
- a legacy-shaped sidecar logs one warning naming the pre-1.8.0 keys (and skips
  the per-group checks) instead of "omits 4 of 6 groups";
- the existing "DROPPED … unknown group key(s)" warning keeps working against
  the union.

### 5.4 Headline sweep uses stored score and band

`_announce_owed_headline` (`simulation.py:4252-4259`) today passes only
`row.scores`, and `render_assessment_headline` then recomputes band and score
from the live weights (`assessment_headline.py:230-238`). After a weights change
an older verdict owing a headline would be announced — publicly and
unretractably — with a band it was never given. Fix: `_post_assessment_summary`
gains keyword-only `score`/`band` parameters, forwarded to
`render_assessment_headline`; the sweep passes `row.weighted_score`/`row.band`.
They are never read from the sidecar dict, which a model can populate with its
own `weighted_score`. The in-turn path is unchanged. (No verdict owes a
headline today: `summary_posted_at IS NULL` counts 0 in every run.)

### 5.5 Backfill guard

`scripts/backfill_dropped_verdicts.py` scores with the live weights (`:143`) but
stamps the run's own derived stamp (`:546-556`). It refuses when the stamp's
revision (resolved through `resolve_revision`) weighs the dimensions differently
from the live document, or matches no revision; an unstamped run resolves to
the live view and behaves as today. A new `--allow-rubric-drift` flag overrides
(the sibling `backfill_assessment_headlines.py` already uses that name).

---

## 6. Verification

- **Pinned tests updated** (deliberate calibration change): weights
  (`test_blackbird_rubric.py`, `test_rubric_document.py`), 35→50 science block
  and prose (`test_rubric_prompt_sync.py`, `test_blackbird_rubric.py`,
  `test_rubric_document.py`), `"3.4.0"` literals (`test_rubric_document.py`),
  hand-computed scores (3.35→3.40, 4.5→4.75), the rounding case restored to an
  exact 3.395 raw mean, the `_TEAM_BLOCK` mutation fixture (weight 5),
  `tests/integration/test_opportunity_assessment_persistence.py` (3.40/advance),
  the stage-bar hub-only needles (`"35%"`/`"65%"` → `"50%"`, so they keep
  guarding), prompt version pins (`test_headline_contract.py`), the skeleton
  key-order test, and the key-point fixtures in the integration and chat tests.
- **New tests:** a legacy five-group and a three-group row render with their
  old labels and order; a six-group row renders with the new ones; a mixed row
  hides nothing; a stale-prompt (legacy-key) sidecar stores on the new image; a
  new-key sidecar stores; blank bullets are stripped; the chat record quotes a
  key-point sentinel present on the page (the parity test today checks only
  containment); the list-page size fixture uses the new keys at realistic
  length; a headline announced by the sweep after a weights change shows the
  stored band; the backfill guard allows the live and unstamped cases,
  refuses a different-weights or unknown stamp, and honours the override; the
  review-copy renderer's note quotes the split the weights produce; the
  engine's legacy-shape, count and 300-character warnings (the existing
  legacy-partial test in `test_assessment_narrative_fields.py` changes to
  expect the legacy warning).
- **Procedure:** implement on a branch in a git worktree under
  `/home/ubuntu/blackbird-worktrees/`, never in the live tree (`prompts/roles/**`
  edits reach a run on its next turn) and never inside the repo root (it is the
  image build context and there is no `.dockerignore` — see §7). Create the
  worktree's own test venv on the host, not over sshfs. Run
  `scripts/sync_prompt_set_docs.py`, then the full `./scripts/ci.sh`, then an
  adversarial audit of the diff against this spec.

---

## 7. Deploy

No migration. Images: web, worker and agent all rebuild (templates and `src/`
are baked in; the engine imports `KEY_POINT_GROUPS`/`normalize_key_points`).

```
DC="docker compose -f docker-compose.prod.yml"
for s in blackbird-app worker agent; do
  docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-3.5.0
done
# land the branch in the live tree (fast-forward) — prompts are live from here
$DC build blackbird-app worker
$DC --profile agent build agent
$DC up -d blackbird-app worker
$DC up -d agent        # ONLY with no run live; the supervisor loads the rubric at boot
```

- **Pairing hazard.** New prompt on the old image: the old normalizer rejects
  the six new keys and every new verdict's `key_points` is stored NULL (kept
  only in `raw_verdict`). Old prompt on the new image: benign (union
  acceptance, legacy rendering). So **no run may start between landing the
  tree and `up -d agent`**; confirm `/admin/simulation` is idle before landing.
- The rubric is parsed once at import by the web tier and by the agent
  supervisor at container boot (`supervisor.py:34` → `main.py:22`), so both
  restarts above are required for 3.5.0 to take effect.
- Check afterwards: the next run's startup banner reads `Screening rubric:
  version 3.5.0 (content hash …)` matching `sha256sum` of the file, and the
  run-start announcement carries scout_hub 1.8.0.
- Start the next run **fresh**. A resume only logs a warning about the rubric
  change (`src/agent/main.py:314-325`) and would mix 3.4.0- and 3.5.0-scored
  verdicts in one run; per-row stamps keep them separable either way.
- `CLAUDE.md` gains a deploy box for this change.
- **Pre-existing, not fixed here:** finding S1 in
  `docs/audits/2026-09-24-comment-cleanup-rca/README.md` — with no
  `.dockerignore`, every image bakes `.env`, `backups/` (six production dumps),
  `.git` and `.venv-test`. This deploy's rebuild and rollback tag carry the same
  contents. The operator will address S1 and the rest of that write-up
  separately (decision 2026-09-24); nothing here depends on it.
- Rollback: redeploy the `rollback-pre-3.5.0` images and revert the commit,
  but in that revert **append a 3.5.0 entry to `prompts/rubric/revisions.toml`**
  (sha256[:12] of the 3.5.0 file) — otherwise every row and review stamped
  3.5.0 renders "matches no entry". Old images render only
  `clinical_actionability` and `key_questions` of a six-group row; the other
  four groups stay in the column but are hidden until the new code is back.

---

## 8. Out of scope and residual risk

- **Not done:** the S1 `.dockerignore` fix and the rest of the 2026-09-24
  comment-cleanup RCA (handled separately, §7); a web/news search tool for the hub; PI-profile enrichment
  (appointments, centres, prior companies) for Lab Background; re-checking the
  3.4/2.8 thresholds; editing the five 2026-09-22 verdicts; the
  `/admin/assessments` "Dimension distribution" table, which pools verdicts
  scored under different weights and labels them with the live ones
  (`directory.py:672-689`); the headline's one-decimal score display
  (3.35 shows as "3.4"); the review bot, which never sees headline, pitch or
  key points, and which sends each row's stored band/score alongside only the
  LIVE rubric file (`review_bot.py:324-325`), so a 3.4.0 row is discussed
  against 3.5.0 weights.
- **Accepted costs:** cards roughly double in height (D3); Lab Background will
  be thinner than the exemplars (D5); competitor names become visible to any
  future reviewer-tier account (D6); the hub may still state deal facts from
  training knowledge, now dated (D10).
