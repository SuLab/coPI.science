# Landscape pages remediation — Implementation Plan (revision 4)

> **For agentic workers:** execute through `/lathe:plan-execution`. The owner's operating
> policy routes plan execution there; it replaces subagent-driven-development and
> executing-plans.
>
> **Implementers.** Packages in a phase are implemented in parallel. Implementers write code and
> tests but do not build or run them.
>
> **Main session.** It runs the **Verify** commands below on the host after the merge, one
> package's tests at a time, then audits adversarially and fixes.
>
> **CI.** ONE full `ci.sh` runs when Phases A, B and C are all done. Never run two CI runs at
> once.
>
> Steps use checkbox (`- [ ]`) syntax.

**Goal.** Make the Landscape pages tell a manager or reviewer, correctly and in the same form as the
Assessments and Projects pages:
- what is proposed and what it costs;
- what the AI concluded and why;
- how far to trust it, and where its sources are.

**Architecture.**
- `src/services/landscape_view.py` builds richer card, list and detail data.
- Two new focused modules:
  - `src/services/landscape_sources.py` turns support strings and `doc:` references into readable,
    linked sources;
  - `src/services/landscape_quotes.py` reads a member idea's verified quotes, moved out of the chat
    record.
- The Landscape templates are rebuilt on copies of the assessment card and detail markup (D26).
- S9 stamps `prose_format`.
- No migration.

**Tech stack.** FastAPI, SQLAlchemy 2 (async), Jinja2, Tailwind (precompiled
`static/css/app.css`), vanilla JS (`static/js/markdown.js`, `static/js/assessment_timeline.js`),
pytest with testcontainers Postgres on the host, Playwright on the workstation.

**Spec.**
- **Design (wins on any conflict):** `docs/specs/2026-11-19-landscape-ui-remediation-design.md`,
  approved by the owner on 2026-11-19.
- **Findings R1–R23:** `docs/audits/2026-11-19-landscape-ui/README.md`.
- **Base spec:** `docs/specs/2026-10-30-blackbird-landscaping-design.md`, §13 (lines ~1995–2250)
  and §10.6 (~1636–1654). Its decision table ends at D83 (line 267).

## Decisions (approved 2026-11-19; the design gives the reasons)

| # | Decision | Tasks |
|---|---|---|
| OD1/OD4 | **Card = the Assessments card.** Shows the recommendation chip ("decline" for pass); full "In one minute"; the cost line, then key points with risks; score and band under "Why this score"; footer counts, Assigned and "Decided by" (after your own decision); open quick decision with reason tags folded. | A1, A3 |
| OD1 (blind) | On an undecided blind card, the chip, the counts and "Why this score" are hidden; the cost line, pitch and key points stay. Card only: the detail page and the chat record are unchanged. | A1, A3, A6 |
| OD3 | No rank number on any card. The pipeline order stays the default. | A3 |
| OD12 | The Current and All lists get a Sort (pipeline default, score, recommendation; shown values only) and a Lab filter (matches any lab), with Apply. | A1, A2, A3 |
| OD2 | Prose renders as Markdown. NULL `prose_format` on a Landscape row counts as markdown, and S9 stamps it. | A1, A4, B1 |
| OD9 | Approve/Disapprove buttons removed (Assessments precedent, `docs/specs/2026-09-21-assessment-queue-and-headline-contract-design.md` D2/D3). The route and table stay, with a `ROUTE_ALLOWLIST` entry, and existing marks show read-only. | A4, A6, A7 |
| OD5 | Read-only history view: `?assessment=<id>`, assessment sections only, no forms, no chat, no deal-breakers. | A1, A2, A4, A6 |
| OD8 | One override line on the Verdict when the active epoch was overridden. | A1, A4 |
| OD6/OD7 | Visible sources. Only DOI, PMID and NCT become links; patents and records show as labels. Grey "analysis" badge for non-sources, unlabelled on an expert's open item. | A1, A4 |
| OD10 | Help screenshots captured on the workstation before deploy. | Phase C |
| OD11/OD13 | Pipeline page: completeness labelled "last slice"; override and hold reasons shown to staff, folded. | A1, A5 |

New spec rows D84–D94, one per row above in that order, go in with A8. The table has 11 rows; an earlier draft said D84–D92.

## Global Constraints

**Hiding (anti-goal: never reveal what today's rules hide).**
- The rules: `_BLIND_ASSESSMENT`, `_EXPERT_ASSESSMENT`, `_BLIND_DEEP`
  (`src/services/landscape_view.py:106-126`), the card's `rank = None if hiding.blind or
  hiding.expert` (`_card`, :687), `_withheld_blind` (:829-842) and the independent-votes rule.
- Every new value is hidden under the rule of the data it comes from. No hidden value may affect a
  count, a sort order or a filter result.
- Exception: the default pipeline order (spec D30, unchanged from today, lv:931) places every card
  at its pipeline place. The rule binds the score and recommendation sorts, the tiles and the lab
  filter.

**Projection.**
- Every new view key is registered in `src/web/presentation.py`: `VIEW_FIELDS` :167-276;
  `JSON_FIELDS` :134-141 for maps with data keys; `ADMIN_KEYS` :154-158.
- `STAFF_KEYS`/`ADMIN_KEYS` match by name at any depth, so admin-only keys get unique names
  (`flag_evidence_raw`, `database_evidence_raw`). Never `evidence`.

**Anti-goals.**
- No green "sourced" badge on anything but a document, record or verified quote.
- No production write from the web tier.

**Section ids stay:** `brief verdict gating scores evidence reviews review disease-profile panel`
(`src/services/landscape_chat_record.py`; `tests/integration/landscaping/test_landscape_pages.py:99-113`).

**Copied, not shared (D26).** Copy assessment markup into the Landscape templates. These Jinja
globals may be used directly: `md_citations`, `plain_citations`, `band_label`, `band_class`,
`key_point_sections`, `doi_url`, `pubmed_url` (`src/web/templating.py`).

**Template scanners.** The rendered-page gate (`tests/integration/test_rendered_page_gate.py`:
one `<main>`, one `<h1>`, no skipped heading level, named `<nav>`, named controls), plus:

| Rule | Test |
|---|---|
| No inline `on…=` | `tests/unit/test_ui_behaviours.py:21` |
| Forbidden contrast classes | `test_contrast_classes.py:10-42` |
| Script nonce/src | `test_csp_templates.py:14,34` |
| No `\bband\s*==` | `test_bands.py:97` |
| `band_class(…, 700, 600)` only | `test_compiled_css.py:83` |
| No `doi.org/{{` or `pubmed.ncbi.nlm.nih.gov/{{` | `test_publication_links.py:50` |
| A GET filter form with a `<select>` has a submit button | `test_filter_forms_apply.py:25` |

**Reachability** (`tests/unit/test_reachability.py`).
- Bare `#…` hrefs and `href="{{ x.url }}"` count as unresolvable. The cap of 86 (:1032-1071) is
  raised to the measured count, with a comment naming this plan.
- A route with no caller needs a `ROUTE_ALLOWLIST` entry (:109-165).

**CSS.**
- Use only classes already compiled into `static/css/app.css`; every class this plan names is
  copied from an assessment or project template.
- A new class fails `scripts/build_css.sh --check` and stales every help shot.

**Source gates.**
- New functions need a live caller (`test_no_dead_src_symbols.py:432`).
- At most 200 lines per function; C901 ≤ 20; never raise the ruff ceiling of 231.
- `src/services/landscape_*.py` must not import `src.models.publication`, `enrichment` or
  `pi_company` (`tests/unit/landscaping/test_separation.py`).

**Help rules** (`docs/operations/help-content.md:49-80`): exact UI text; the only numbers are 1–5
and 10,000; staff-only labels inside `{% if permissions.staff %}`; no real names; no repository
internals.

**Release.**
- Build `blackbird-app` (Phase A) and `landscape-worker` (Phase B) only.
- No migration, no simulation start.
- Never use `--remove-orphans` or `up -d --build`.
- Do not edit `docker-compose.prod.yml`.
- Do not push.
- The main worker imports `publication_links`; A1's change there is additive, so the worker is not
  rebuilt.

## Review Focus

1. **Malformed or partial S9 rows.** Cases: `recommended_next_experiment` not opening with `**`,
   `key_points` empty or not a dict, `claim_support` shorter than the bullets, `elevator_pitch`
   NULL. Each card and page renders: the cost line is absent, a column with nothing to show is
   dropped, and a missing badge counts as estimate. Tests: A1 `test_card_handles_partial_rows`,
   A6 `test_card_page_partial_row_renders`.
2. **`doc:` and support strings in hostile shapes.** Cases: a token matching no document or two
   documents; titles containing `<`, `*`, `]` or `(`; a DOI with parentheses. Never an unescaped
   character or a broken link. Tests: A1 `test_rewrite_doc_refs_*`, `test_resolve_support_*`.
3. **Hidden states leaking through aggregates.** Cases: an undecided blind card, or an expert's open
   item, in the tiles, the Sort and the Lab filter. Each counts only in "Total" and, under a score
   or recommendation sort, sorts with the Nones, whatever its stored values. Tests: A1
   `test_list_sort_uses_shown_values`, and `test_tiles_count_shown_values`, which includes a blind
   batch in `batch_view`.
4. **Multi-member, multi-lab opportunities.** "Idea N" numbering matches the evidence-row links,
   every structured line shows once, and the Lab filter matches any member's lab. Tests: A1
   `test_lab_filter_matches_any_lab`; A6 `test_multi_member_page_links_ideas`.
5. **History requests from outside the listed rows.** A candidate-epoch row, a foreign id or a
   malformed id → 404. A retired opportunity's own page keeps its forms. Tests: A2
   `test_history_view_*`.

---

## Contracts (fixed before fan-out; A1 and A2 implement, A3–A5 consume)

**C1. Labels.**
- A2 passes `banding` (`src/services/blackbird_rubric.py:484-488` `BANDING`) top-level on the
  list and detail routes.
- A band prints as `band_label(band, banding.pass_label)`.
- A recommendation prints as `banding.pass_label if rec == 'pass' else rec`.
- The recommendation chip colours come from the map at `templates/assessments/_body.html:316-321`.

**C2. Card** (`_card`, `landscape_view.py:668-701`). The dict gains these keys; `rank` stays in
the dict for the chat record and is never rendered.

| Key | Type | Value | Hidden |
|---|---|---|---|
| `elevator_pitch` | str \| None | Full pitch, C4 rewrite applied | never |
| `prose_format` | str | `row.prose_format or "markdown"` | — |
| `cost_line` | str \| None | First line of `recommended_next_experiment` when it opens with `**`, the `**` pairs removed | never (owner: shows on blind cards) |
| `key_points` | list[dict] | `_key_points(values)` (lv:1105-1129), each bullet gaining `kind` ∈ {"risk", "companies", None} and `body`, from `assessment_detail.classify_key_point` (:212-235). On the card, each bullet drops `support`. | never |
| `score_rationale` | str \| None | From the row | already blind/expert-hidden |
| `recommendation`, `confidence` | str \| None | From the row | None on an undecided blind card, and under the expert rule |
| `red_flag_count`, `confirmed_flags` | int \| None | `len(red_flags)`; members' S6 `flags_reviewed` with `status == "confirmed"`, after `hide_deep` | None on an undecided blind card, and under the expert rule |
| `assigned_names` | list[str] | Assignees | never (assignments are public to the page's viewers) |
| `decided_by` | list[str] \| None | Names of everyone who has decided | None until the viewer has decided (independent votes) |
| `lines` | list[str] | Structured lines, de-duplicated | — |
| `show_label` | bool | False when there is one line and it equals the label, or starts with the label minus a trailing "…" (D80, spec:264) | — |

**C3. Support** — `src/services/landscape_sources.py`.
- `resolve_support(refs: list[str] | str, docs: Mapping[str, DocInfo], quotes: Sequence[Quote], *, expert_hidden: bool) -> SupportView`.
- `SupportView = TypedDict("SupportView", {"kind": Literal["sourced","analysis","estimate"], "items": list[{"label": str, "url": str | None}]})`.
- `DocInfo = {id, title, doi, pmid, kind}`, from the members' `_sources` (lv:1019-1029).

Each string is tried against these classes in order:

| # | Class | Result |
|---|---|---|
| 1 | `doc:<uuid>` or `doc:<≥8 hex>`, matching exactly one doc | The doc's short title, with url `doi_url(doi)` or else `pubmed_url(pmid)` |
| 2 | `pmid:` | `pubmed_url` |
| 3 | `nct:` | `TRIAL_URL` |
| 4 | `patent:`, `grant:`, `orcid:` | Label only |
| 5 | `uniprot:`, `opentargets:`, `hpa:`, `mondo:`, `orphadata:`, `chembl:`, `openfda:`, `trials:` | Label ("UniProt Q9HBA0") |
| 6 | A string equal to a member quote, after normalising whitespace and case | That quote's **document**: short title and DOI/PMID link, with the quote as `quote` on the item |
| 7 | `"estimate"` or `estimate: …` | estimate |
| 8 | Anything else | `analysis`: a readable label, or the bare "analysis" when `expert_hidden` |

- The list's kind is "sourced" if any item matched classes 1–6, else "analysis" if any matched 8,
  else "estimate".
- Move `_TRIAL_URL` (renamed `TRIAL_URL`) and `_NCT_RE` (renamed `NCT_RE`) from
  `src/services/projects_view.py:146-147` into `src/services/publication_links.py`, and import
  them back into `projects_view`.

**C4. In-text references** — `rewrite_doc_refs(text: str | None, docs: Mapping[str, DocInfo], *, markdown: bool) -> str | None` in the same module.
- **Markdown fields:** `[short title](<url>)`, with `[` and `]` stripped from the title, or the bare
  title when there is no url.
- **Plain fields:** `short title (url)`. Templates wrap these fields in `plain_citations`.
- **No unique match:** "an unnamed dossier document".
- **When:** applied in `detail_view` and `_card` after hiding and before `_verdict` (lv:1137-1156).
- **Fields:**
  - S9: pitch, rationale, ask, score rationale, key-point bullets, strengths, risks, competitive
    landscape, evidence maturity, red flags, gating and dimension rationales;
  - S6: summary, target biology, disease-profile values, gating rationales;
  - consult established/concerns/questions.

**C5. Quotes** — `src/services/landscape_quotes.py`.
- `async member_quotes(db: AsyncSession, idea_id: uuid.UUID, deep_values: Mapping | None, documents: Mapping[str, DocInfo]) -> list[Quote]`.
- `Quote = {document: str, section: str, quote: str, basis: Literal["summary","full_text"]}`.
- **Summary basis:** for each `landscape_idea_evidence` row of the idea
  (`src/models/landscape.py:744-768`), take the extraction's `raw["ideas"][candidate_index]`
  entries that carry `quote`. That covers `target_rubric`, `key_results`, `novelty_claims`,
  `limitations` and `unmet_need_statements` (`src/landscaping/schemas.py:86-141`).
- **Full-text basis:** what `landscape_chat_record._quotes` reads today (:666-694).
- Move `_quotes_in`/`_quotes` into this module, public as `quotes_in`/`full_text_quotes`, and make
  the chat record import them.
- Quotes stay under the expert rule, because they are verbatim source text. Record that in a code
  comment.

**C6. Deal-breakers.** Each member gets
`flags: list[{"name": str, "raised": bool, "status": "confirmed"|"cleared"|None, "reason": str|None, "evidence_text": str}]`.
- **Sources:** `idea.data_flags` (`src/landscaping/flags.py:74-123`), plus every `flags_reviewed`
  key after `hide_deep`, triage flags included (`deep.py:640-649, 838-843`).
- **Shown:** raised or reviewed flags. Unraised, unreviewed flags collapse into `not_raised:
  list[str]`.
- **Expert rule:** `status` and `reason` are None.
- **`evidence_text`** is a sentence:

  | Flag | Sentence |
  |---|---|
  | target essential | "Open Targets: essential = <true/false>; DepMap: <summary>" |
  | safety liabilities | the liability names joined |
  | same target in clinic | "<n> candidates, <n> trials" |

- The raw evidence moves to `flag_evidence_raw` (admin only).
- `evidence_summary` leaves the member dict; admins get it as `database_evidence_raw`.
- `detail_view` gains `confirmed_not_red_flagged: bool`: confirmed flags exist and `red_flags` is
  empty. It is None under the expert rule.

**C7. Cut sentence.** `lists_view` returns `cut = {"n", "k", "completed_at", "opportunities"}`.
`pipeline_view` returns `latest_cut_opportunities: int | None`.

**C8. History.** `detail_view(db, user, opportunity_id, *, history_assessment_id: uuid.UUID | None = None)`.
- **Accepted ids:** only those of `_history` rows (lv:1205-1215: `active`/`retired` epochs, done,
  not the displayed row). Anything else raises `LookupError`, which A2 turns into 404.
- **Result:** `history_view = True` (A1 produces and registers it), `members = []`, no
  deal-breakers, and an Evidence summary built with `consults=[]`.
- **Still loaded:** member `sources` and quotes, so C3/C4 resolve.
- `rank` is None, and the same hidings apply.

**C9. Return state.**
- `_decision_form.html` takes `surface` (`"detail"` default | `"landscape-list"`), `list_state`
  (`{view, tab, assignment, sort, lab}`), `next_id` and `scope`. Defaults are keyed on `surface`.
- The Landscape POST routes accept `surface`, `list_state` fields, `next_id` and `scope`, validated
  against allowlists (unknown values dropped).
- **A list card's decision** redirects to `/workspace/landscape?<state>#o-<scope>-<id>`. Cases:
  - the next card's anchor when the decision moved the card off Unreviewed;
  - always the next card for scopes `carry` and `changed`;
  - `#earlier` for an earlier-batch scope.

  Flash "Decision saved." or "Decision saved; moved to Reviewed."
- **Every detail-page form** redirects to the detail page with `list_state`. That covers decision,
  review submit/edit/delete, assign, unassign and expert judgment. Implement it once, in
  `_landscape_redirect` (`src/routers/reviews.py:948-949`).
- The detail route accepts the same state and passes `list_state` top-level for the back link.

**C10. Verdict.**
- `epoch.summary()` (lv:372-380) gains `overridden: bool`.
- `detail_view` returns `verdict_revision = {advance_min, conditional_min, pass_label}` from the
  row's own revision (`resolve_revision`, already called in `_verdict`).

**C11. Pipeline.**
- `audit_disagreements[]` gains `label` (lv:1453-1484 pattern).
- Epoch rows gain `was_held: bool`.

**C12. Members.** Each member gains:
- `n` (the "idea N" order, lv:1088-1098);
- `stage_label` and `evidence_grade_label` (underscores → spaces);
- `quotes` (C5);
- `flags` and `not_raised` (C6).

Sources gain:
- `kind_label`: work→paper, preprint, grant, patent, jhtv→technology listing, dataset
  (`src/models/landscape.py:210`);
- `url`: DOI, else PMID.

Consult signal items gain `idea_n`, parsed from the "· idea N" suffix (lv:987).

**C13. Review form.** `detail_view` returns
`review_rubric = {version, scale_min, scale_max, dimensions: [{key, title, weight, anchors, bot_score}]}`,
shaped like `assessment_detail._review_rubric` (:1816-1839). `bot_score` comes from S9 `scores`
and is None where `scores` is hidden.

**C14. Sort and lab.**
- Signature: `lists_view(db, user, *, view, tab, assignment, sort: Literal["pipeline","score","recommendation"] = "pipeline", lab: str | None = None)`.
- Sorting uses the cards' **shown** values. Score sorts descending. Recommendation sorts in this
  order: advance, route-to-incubation, conditional, pass. None sorts last. Ties break by pipeline
  order, then opportunity id.
- `lab` keeps cards whose `labs` contain it.
- `lists["lab_options"]` holds the sorted distinct lab names of the cards `lists_view` loaded: after
  the blind and probe exclusions and the assignment filter, before the lab filter and the tab.
- `lists["tiles"]` = `{total, advance, conditional, route_to_incubation, pass}`:
  - counted from shown recommendations, over the same set as `lab_options` (list-wide, whatever the
    tab and lab);
  - a hidden value counts only in `total`;
  - the header says the counts cover the whole list.
- `batch["tiles"]` counts this week's batch the same way.
- A1 registers `advance`, `conditional`, `route_to_incubation` and `pass` in `VIEW_FIELDS`
  (`total` is already there, presentation.py:237).
- **Batch view header:** the band legend and the tiles. The cut sentence is only on the staff lists,
  because `cut` comes from `lists_view`.

## Pre-fan-out checks (main session, read-only)

- [x] **8-hex `doc:` prefixes (C4).** Unique across all 78,060 documents (2026-11-19), so the
  ambiguity rule is a safety net only.
- [x] **`raw["ideas"][candidate_index]` (C5).** Confirmed on 3 active-epoch rows. The candidate
  object carries `target_rubric`, `key_results`, `limitations`, `novelty_claims` and
  `unmet_need_statements`.
- [x] **Quote references in S9 `claim_support` (C3, class 6).** None today: 0 quote-form strings
  and 34 long free-text strings. Class 6 is kept as a cheap safeguard.
- [x] **`openalex_url` (C12).** Data-dependent: 10,178 of 22,045 labs have an OpenAlex author id.
  The sampled opportunities' labs have none. Help says "where known".

## Phase A — web tier (`blackbird-app`)

A1, A2, A3, A4, A5, A7 and A8 touch disjoint files and run in parallel. A6 follows them.

### Task A1: Read model and resolvers

**Files:**
- Modify: `src/services/landscape_view.py`, `src/services/landscape_chat_record.py` (imports only),
  `src/services/publication_links.py`, `src/services/projects_view.py` (imports only),
  `src/web/presentation.py`
- Create: `src/services/landscape_sources.py`, `src/services/landscape_quotes.py`
- Test: `tests/unit/test_landscape_sources.py`,
  `tests/integration/landscaping/test_landscape_view.py`

**Interfaces.** Produces C2–C14, exactly as named above.

**Step 1 — write the tests.**

`test_landscape_sources.py`:
- `test_resolve_support_classes` — one case per C3 class, asserting `kind` and items.
- `test_resolve_support_expert_hides_analysis_labels` — "talent-specialist: …" →
  `{"label": "analysis", "url": None}`.
- `test_rewrite_doc_refs_unique_ambiguous_missing` — the three cases.
- `test_rewrite_doc_refs_escapes` — a title with `<b>`, `*x*`, `a]b`, and a DOI `10.1000/(abc)` →
  `[a b](<https://doi.org/10.1000/(abc)>)`.

`test_landscape_view.py`:
- `test_card_b_fields` — the card dict carries the C2 keys; `cost_line` strips `**`.
- `test_card_handles_partial_rows` — `recommended_next_experiment` without `**` → `cost_line` None;
  `key_points` None → `[]`.
- `test_blind_card_hides_chip_counts_keeps_cost` — undecided blind: `recommendation`,
  `confidence`, `red_flag_count`, `confirmed_flags` are None; `cost_line`, `elevator_pitch` and
  `key_points` are present. After the decision, all present. The detail view's `assessment`
  keeps `recommendation` in both states.
- `test_expert_card_and_rows` — expert open item:
  - chip, counts and `score_rationale` are None;
  - deal-breaker rows have `status` None;
  - analysis labels are bare;
  - quotes are present.
- `test_decided_by_only_after_own_decision`.
- `test_list_sort_uses_shown_values` — an expert's open item with a stored score of 4.9 sorts after
  every scored card.
- `test_tiles_count_shown_values`.
- `test_lab_filter_matches_any_lab`.
- `test_flags_rows_raised_reviewed_not_raised` — raised, reviewed-only (triage) and unraised cases.
- `test_history_view_accepts_only_listed_rows` — a candidate-epoch row raises `LookupError`.
- `test_prose_format_null_is_markdown`.
- `test_member_quotes_candidate_index` — only the member's candidate's quotes.
- `test_verdict_revision_and_overridden` — `verdict_revision` comes from the row's revision;
  `epoch.overridden` is True only when `override_at` is set.

**Step 2 — implement C2–C14.**
- One helper per contract.
- Batch the per-card loads (sources, S6 rows, decisions, assignments) into one query each per page,
  not one per card.
- Every function at most 200 lines.
- Register every new key in `presentation.py`; `flag_evidence_raw` and `database_evidence_raw` go
  in `ADMIN_KEYS`.

**Verify (main session).**
`.venv-test/bin/python -m pytest tests/unit/test_landscape_sources.py tests/integration/landscaping/test_landscape_view.py tests/integration/landscaping/test_landscape_chat.py -q`
→ all pass.

### Task A2: Routes

**Files:**
- Modify: `src/routers/reviews.py` (the Landscape routes, :937-1295, and `_landscape_redirect`),
  `src/routers/workspace/landscape.py`
- Test: `tests/integration/landscaping/test_landscape_reviews.py`,
  `tests/integration/landscaping/test_landscape_pages.py`

**Interfaces.**
- Consumes C8, C9 and C14 from A1; `history_view` comes from `detail_view`.
- Produces these top-level template keys: `banding`, `list_state`.

**Step 1 — write the tests.**
- `test_list_decision_returns_to_card` — a 303 to `/workspace/landscape?view=current&tab=unreviewed#o-list-<next>`.
- `test_detail_forms_keep_list_state`.
- `test_invalid_state_dropped`.
- `test_history_view_*` — a listed row → 200 with no `<form`; a candidate row, a foreign id or
  `assessment=bad` → 404.
- `test_history_view_has_no_chat`.
- `test_history_view_page_problems` — `page_problems` finds nothing on the history view.

**Step 2 — implement.**
- `workspace_landscape(…, sort: str | None = None, lab: str | None = None)`.
- `workspace_landscape_detail(…, assessment: str | None = None, view/tab/assignment/sort/lab state)`.
- `chat_available` also requires `not history_view`.
- `_landscape_redirect(opportunity_id, list_state)`.
- The decision routes read `surface`, `next_id` and `scope`, validated as in
  `_list_filter_query` (`reviews.py:86-129`).

**Verify.** `pytest tests/integration/landscaping/test_landscape_reviews.py tests/integration/landscaping/test_landscape_pages.py -q`

### Task A3: List and card templates

**Files:** Modify `templates/workspace/landscape.html`, `templates/landscape/_card.html` and
`templates/landscape/_decision_form.html`.

**Interfaces.** Consumes C1, C2, C7, C9 and C14.

**List header (copy the Assessments list):**
- [ ] The band legend (copy `templates/workspace/assessments.html:27`).
- [ ] Count tiles from `tiles` (copy `templates/assessments/_body.html:147-176`): labels Total,
  Advance, Conditional, Route-to-incubation, and Decline from `banding.pass_label`.
- [ ] The cut sentence: "{k} ideas kept of {n}, grouped into {opportunities} opportunities."
- [ ] On staff lists: a Sort and Lab GET form with an Apply button (copy
  `assessments.html:13-21` styling).
- [ ] The tab links and the assignment form (`landscape.html:44, :48-54`) carry `sort` and `lab`.

**Card (copy `_body.html:262-506`, adapted):**
- [ ] `id="o-{{ scope }}-{{ card.opportunity_id }}"` with `scroll-mt-16`.
- [ ] Headline link; structured lines via `lines`; the label only when `show_label`; then lab and
  issue date.
- [ ] The recommendation chip (C1), or nothing when it is None.
- [ ] Two columns:
  - left: "In one minute" via `data-markdown`/`plain_citations` by `prose_format`, plus the
    "detail →" button;
  - right: `cost_line` in bold, then `key_points` with a bold "Risk:"/"Companies:" prefix by `kind`.
  - Single column when one side is empty.
- [ ] A closed "Why this score" disclosure with `score_rationale`, score/band, and the line "The
  band is computed from the dimension scores, not taken from BlackbirdBot." Render it only when
  `score_rationale` or the score is shown.
- [ ] Footer: the counts (amber when above 0, `_body.html:470-472`), "Assigned: …", and
  "Decided by: …" when `decided_by` is not None.
- [ ] No rank number.
- [ ] `id="earlier"` and `scroll-mt-16` on the Earlier batches section (`landscape.html:91`).

**Decision form:**
- [ ] The C9 parameters.
- [ ] Reason tags inside a closed "Reason tags (optional)" disclosure on list cards.
- [ ] Legend "Reason tags (what drove your decision)".

**Blind notes:**
- [ ] `_card.html:25` and `landscape.html:65` name what an undecided blind card hides: the
  recommendation, score, band and flag counts.

**Verify (with A6).** `pytest tests/integration/landscaping/test_landscape_pages.py tests/integration/test_rendered_page_gate.py -q`;
`grep` each class in `static/css/app.css`.

### Task A4: Detail templates

**Files:** Modify `templates/workspace/landscape_detail.html`,
`templates/landscape/_member_idea.html`, `_review_form.html` and `_expert_form.html`. Optionally
create `templates/landscape/_signals.html` (macros copied from
`templates/assessments/_detail_body.html:303-382`).

**Interfaces.** Consumes C1, C3–C6, C8–C13 and `history_view`.

**Header and layout (copy `_detail_body.html` unless noted):**
- [ ] H1 "Opportunity detail"; the back link carries `list_state`; the headline as
  `p.assessment-headline`; the label sub-line by the `show_label` rule.
- [ ] Sticky jump nav of bare fragments (:76-102): Brief, Evidence, Ask, Verdict, Gating,
  Rationale, Scores, Deal-breakers (not on the history view), Review, Ideas. Add Expand all /
  Collapse all and `scroll-mt-16 max-md:scroll-mt-28` on each section.
- [ ] Section headings `text-lg font-semibold`
  (`docs/plans/2026-09-11-assessment-readability-implementation-plan.md:24-28`).
- [ ] Two-column brief (:127-215) with `.assessment-prose`, "Why this score" beside the pitch, and
  key points with a bold "Risk:".

**Sources and prose:**
- [ ] Support badges from C3: sourced in green, with its items listed visibly (linked where `url`);
  analysis in grey with its label; estimate in grey.
- [ ] Pitch, ask and rationale through `data-markdown="{{ md_citations(x) | e }}"` when
  `prose_format == 'markdown'`, else `plain_citations` (:142-146, 544-559, 899-908). The ask shows
  its bold first line, with the rest behind "Show the full plan".
- [ ] Every C4-rewritten plain field through `plain_citations`.

**Evidence summary:**
- [ ] The mid-scale sentence, empty states, legend and provenance (:384-527), with a branch that
  claims nothing about dimensions when `dimensions == []`.
- [ ] Consult rows with note, preview and show (:305-338); `idea_n` links to `#idea-<n>`.

**Verdict:**
- [ ] "BlackbirdBot's recommendation" (chip and confidence) and "Score and band" (`band_label`),
  with "computed from the dimension scores, not the model's call · ≥{advance_min} advance,
  <{conditional_min} {pass_label}" from `verdict_revision`.
- [ ] Links to the glossary's Band and Recommendation entries.
- [ ] One sentence when both values are present and differ.
- [ ] The override line when `epoch.overridden`.
- [ ] Criteria and prompt versions move here.

**Scoring and flags:**
- [ ] Gating collapsed; "Full rationale (N paragraphs)" and "Dimension scores" open.
- [ ] Weights as "25% weight"; dimension scores as `"%g"`, with None printing "not scored"; the
  weighted score as `"%.2f"`.
- [ ] Red flags: an amber count, plus the C6 sentence when `confirmed_not_red_flagged`.
- [ ] Beneath The ask: "Budget specialist" with the latest budget signal and first concern per
  member, from the `verdict_signals` consult items.
- [ ] Deal-breakers from C6: confirmed chips amber, cleared green, readable evidence, the "Checked
  and not raised: …" line, and `flag_evidence_raw` last and collapsed (admin only).

**Human review:**
- [ ] One sentence each for the quick decision and the full review.
- [ ] **Remove** the Approve / Disapprove / Clear forms (`landscape_detail.html:161-170`); keep the
  read-only "Current mark" and status-event lines (:182, :204-206).
- [ ] The full review copies the assessment intro, "What the scale means here" and "BlackbirdBot
  scored N" from `review_rubric` (:1006-1056), with the label "Overall rating (1 = weak … 5 =
  strong)".
- [ ] `_expert_form.html:61` prints `pass_label` for "pass".

**Members and history:**
- [ ] Members: `id="idea-<n>"`; summary "Idea N · structured line" with `stage_label` and
  `evidence_grade_label`.
- [ ] Panel opinions: `text-base leading-relaxed max-w-[68ch]`, markers outside, each clamped with
  "Show full message" (:1358-1362).
- [ ] Quotes grouped by section; sources with `kind_label` and `url`; `database_evidence_raw` for
  admins.
- [ ] History rows link with `page_url(request, 'workspace_landscape_detail', opportunity_id=…, query={'assessment': h.assessment_id})`.
- [ ] `history_view` renders the history note and no forms, members or deal-breakers.
- [ ] Blind note (`landscape_detail.html:36`) unchanged in substance (OD1: card only).

**Verify (with A6).** `pytest tests/integration/landscaping/ tests/integration/test_rendered_page_gate.py tests/integration/test_help_links_render.py -q`

### Task A5: Pipeline template

**File:** Modify `templates/workspace/landscape_pipeline.html`.

- [ ] Replace `Latest cut: {{ m.latest_cut | tojson }}` (:118) with "{n} ideas ranked, {k} kept,
  {latest_cut_opportunities} opportunities, {time}".
- [ ] When `was_held`: "Held, then overridden {override_at}". Both reasons go in a closed "Reasons"
  disclosure.
- [ ] Column header "Completeness (last slice)". When received > expected, print "complete
  ({received})".
- [ ] Each reject-audit row shows its `label` and `reason` above the Rescue form.
- [ ] Each metrics JSON block keeps its disclosure, titled in words.

**Verify (tests in A6).** `pytest tests/integration/landscaping/test_landscape_pages.py -k pipeline -q`

### Task A6: Page and gate tests

**Files:**
- Modify: `tests/integration/landscaping/test_landscape_pages.py`,
  `tests/integration/test_rendered_page_gate.py` (`seed_landscape_opportunity`, :492-556; also
  used by the permissions matrix and help-link tests, whose assertions must stay true),
  `tests/unit/test_reachability.py`
- Create: `tests/integration/landscaping/test_landscape_projection.py`

**Step 1 — write the tests.**

Existing assertions to update:
- the badge markup (`test_landscape_pages.py:112`);
- the red-flags span (:136-150).

`test_landscape_pages.py`:
- `test_card_page_partial_row_renders`.
- `test_card_b_markup` — the chip shows "decline" for pass; there is no "rank " text; a "Why this
  score" disclosure exists.
- `test_blind_card_markup` — no chip and no counts; the cost line is present.
- `test_multi_member_page_links_ideas`.
- `test_no_literal_markdown` — zero `**` in the rendered text of a seeded Markdown rationale.
- `test_no_raw_json_for_manager` — no `<pre>` for a manager; present for an admin.
- `test_no_doc_tokens_in_text`.
- `test_approve_buttons_removed` — no `/status"` action on the page.
- `test_override_line_markup` — the line appears only for an overridden active epoch.
- `test_pipeline_cut_sentence`, `test_pipeline_held_then_overridden`,
  `test_pipeline_last_slice_label`, `test_pipeline_audit_label` (for A5).

`test_landscape_projection.py`:
- `test_every_template_key_registered` — every attribute the Landscape templates read on `card.`,
  `member.`, `a.`, `lists.`, `batch.`, `tiles.`, the loop variables (`s.`, `q.`, `f.`, `h.`,
  `group.`, `bullet.`, `c.`) and top-level resolves in the projection.

Gate seed:
- Add a Markdown rationale, a `doc:` reference, a raised and an unraised flag, a second member with
  a second lab, and a quote.

Reachability:
- Add the `ROUTE_ALLOWLIST` entry for `POST /reviews/landscape/{opportunity_id}/status`, worded
  like :137-150.
- Raise the unresolved-href cap to the measured count, with a comment.

**Verify.** The limited set under "Phase A verification".

### Task A7: Help, glossary, tutorials

**Files:** Modify `templates/help/pages/landscape.html`, `templates/help/pages/glossary.html`,
`templates/help/tutorials/reviewer/09-landscape.html` and
`templates/help/tutorials/manager/11-landscape.html`. Modify
`tests/unit/landscaping/test_help_rules.py` only if a new staff-only label appears.

**Help page.** Rewrite every statement that no longer matches the page, naming UI text exactly as
A3–A5 print it:

| Help lines | Topic |
|---|---|
| :24-27 | order, with no rank number |
| :44-49 and :124-130 | blind cards: what the card hides; the cost line shows |
| :51-60 | the card |
| :81-82 | history |
| :85-104 | sections and badges |
| :107-122 | members, quotes, links (OpenAlex per the pre-fan-out check) |
| :134-165 | the quick decision and the review; delete "You can also mark the opportunity approved or disapproved, or clear that mark." |

**Glossary.**
- New entries:
  - Order: cards follow the pipeline's ranking of the ideas, no number is shown, and a blind batch
    is shuffled;
  - Deal-breaker flag (confirmed, cleared, not raised);
  - Sourced / analysis / estimate badges;
  - Evidence grade;
  - Structured line;
  - each licence value the pages print, from the code that sets `LandscapeText.license`, TDM
    included.
- Extend Recommendation and Band for Landscape.
- "probe" appears only inside a staff block.

**Reviewer tutorial.** Blind cards are decided "from the headline, the pitch, the cost line, the
key points and the evidence on the opportunity's page".

**Verify.** `pytest tests/unit/landscaping/test_help_rules.py tests/unit/test_help_registry.py tests/integration/test_help_links_render.py -q`

### Task A8: Spec and operations doc

**Files:** Modify `docs/specs/2026-10-30-blackbird-landscaping-design.md`,
`docs/operations/landscaping.md`, and `docs/audits/2026-11-19-landscape-ui/README.md` (status line).

- [ ] Add rows D84–D94 (the decisions table above, in order; format of D80, line 264), and amend
  the §13.1, §13.2 and §13.4 text they change.
- [ ] Add an operations-doc paragraph: the pipeline page's staff contents and the history view.

### Phase A verification (main session)

- [ ] `grep` each new Tailwind class in `static/css/app.css`.
- [ ] `.venv-test/bin/python -m pytest tests/integration/landscaping tests/unit/landscaping tests/unit/test_landscape_sources.py tests/integration/test_rendered_page_gate.py tests/integration/test_help_links_render.py tests/integration/test_workspace_permissions_matrix.py tests/unit/test_reachability.py tests/integration/test_reviews_router.py tests/unit/test_ui_behaviours.py tests/unit/test_contrast_classes.py tests/unit/test_csp_templates.py tests/unit/test_bands.py tests/unit/test_compiled_css.py tests/unit/test_publication_links.py tests/unit/test_filter_forms_apply.py tests/unit/test_control_names.py tests/unit/test_no_dead_src_symbols.py tests/integration/test_projects_pages.py -q`
  → all pass.
- [ ] Adversarial audit of the merged diff against this plan and the design (lathe:auditor, model
  opus).

## Phase B — worker (`landscape-worker`)

### Task B1: S9 stamps the prose format

**Files:**
- Modify: `src/landscaping/stages/final.py` (`_verdict_values`, :597-628; the fallback path goes
  through `_reply_values`, :695-699)
- Test: the S9 value tests (locate with `grep -rln "_verdict_values\|final_stage" tests/`)

**Steps:**
- [ ] `test_s9_stamps_markdown_prose_format`: a done row has `prose_format == "markdown"`.
- [ ] Add `"prose_format": "markdown"` to the values.

**Verify.** That test file → pass.

## Phase C — help screenshots (workstation)

**Files:** `tests/e2e/help_shots/shots.py`; `tests/e2e/help_shots/seed.py` (Landscape seed at
:558-643: add a Markdown ask, sources, quotes and a confirmed deal-breaker); `figure(...)` calls in
the four Landscape help templates; `src/web/help_pages.py` (`shots` on steps 9 and 11,
:147-148, :173-174); `help_assets/shots/*.png` and `manifest.json`.

- [ ] Take these shots:
  - `landscape-batch` (reviewer);
  - `landscape-detail-brief` (reviewer);
  - `landscape-detail-dealbreakers` (reviewer);
  - `landscape-pipeline` (manager).
- [ ] Capture on the workstation with
  `.venv-test/bin/python -m tests.e2e.help_shots.capture --only <id> --executable <headless shell>`
  (needs Docker).
- [ ] Commit the PNGs and `manifest.json` together.
- [ ] `capture.py --check` lists nothing stale.

**Verify.** `pytest tests/unit/test_help_shots.py tests/unit/test_help_registry.py -q`

## Final verification and release (main session)

- [ ] The single full CI run:
  `ENGINE_REFUSAL_FALLBACK_ENABLED=false LANDSCAPE_SCREEN_MODEL=claude-sonnet-5 LANDSCAPE_EXTRACT_MODEL=claude-sonnet-5 LANDSCAPE_TRIAGE_MODEL=claude-sonnet-5 LANDSCAPE_BATCH_MODE=api ./scripts/ci.sh`
  → "CI passed.".
- [ ] Commit each phase on `blackbird`, with the host's established commit identity and the
  attribution lines. The porcelain check must print nothing.
- [ ] Rollback tags: `copi-blackbird-blackbird-app:rollback-pre-lsui` and
  `copi-blackbird-landscape-worker:rollback-pre-lsui`.
- [ ] `$DC build blackbird-app` → `$DC up -d blackbird-app`; check the `copi-edge` address is kept.
- [ ] `$DC --profile landscape build landscape-worker` → `$DC --profile landscape up -d landscape-worker`.
- [ ] Re-render the production pages read-only (the review README's method) and check every row of
  the acceptance table.

## Acceptance (on the re-rendered production pages)

| R | Check |
|---|---|
| 1 | The card chip is the recommendation; "Why this score" holds score and band; the detail page shows both, labelled, with the differ sentence; undecided blind cards show no chip. |
| 2 | 0 `doc:` tokens; every green badge lists a visible source; DOI/PMID/NCT linked; analysis badges grey. |
| 3 | Quotes under each idea, or "No verified quotes for this idea". |
| 4 | 0 `**` in the visible text of all 40 pages. |
| 5 | No "pass" label anywhere on the pages. |
| 6 | Every card has the full pitch, the cost line and key points; lines shown once; reason tags folded. |
| 7 | No rank number; the cut sentence is present. |
| 8 | Deal-breaker rows per C6; the red-flags sentence. |
| 9 | Jump nav, Expand/Collapse, clamped opinions; heights reported. |
| 10 | H1 "Opportunity detail". |
| 11 | 17 px prose, two-column brief, 18 px headings. |
| 12 | No JSON `<pre>` for managers; page sizes reported. |
| 13 | The budget line beside The ask. |
| 14 | One sentence per control; Approve/Disapprove gone; a card decision returns to the list. |
| 15 | "Idea N" headings, with links from the evidence rows. |
| 16 | Help text matches; history links work; shots present. |
| 17 | Evidence-summary explanations present. |
| 18 | Legend, tiles, cut sentence, Sort and Lab filter. |
| 19 | No `_` enums; glossary entries. |
| 20 | Cut sentence, held-then-overridden, labelled audit rows, "last slice", no "917 of 916". |
| 21 | Review guidance; "25% weight"; "4 of 5". |
| 22 | Multi-member lines separated at 390 px. |
| 23 | The override line. |

## Audit record

- **Revisions 1–3** were audited in three adversarial rounds (lathe:auditor, model opus,
  2026-11-19). The results are folded into the contracts above.
  - **Round 1:** four blocking defects in blind and expert hiding and the history scope, plus
    coupling and escaping fixes.
  - **Round 2:** two leaks through dense position numbering and through sorting by hidden values.
  - **Round 3:** five wording defects.
- **Revision 4** implements the approved design. Changes since revision 3:
  - card B;
  - no rank number;
  - the cost line on every card;
  - sort and lab filter;
  - Approve/Disapprove removed;
  - blind hiding on the card only;
  - "Decided by" after your own decision.

  Position (old C7), OD3's "#n of N" and the ask line as a separate card element are gone.
- **Revision 4's audit** (same auditor) found no new hiding leak and every revision-3 correction
  present. Applied:
  - **Blocking:**
    - the glossary "Order" entry;
    - the band note in the card's "Why this score";
    - the default pipeline order exempted from the hidden-value rule, which binds the other sorts,
      the tiles and the filter.
  - **Non-blocking:**
    - card key points drop `support`;
    - a quote reference renders as its document;
    - `lab_options`/tiles scope and tile-key registration stated;
    - batch-view header stated;
    - `history_view` owned by A1;
    - tab links carry sort and lab;
    - blind note without "rank";
    - pipeline tests plus a valid `-k` filter;
    - RF5 owner fixed;
    - blind batch in the tiles test;
    - override, `verdict_revision` and history `page_problems` tests;
    - batched per-card loads;
    - wider projection test.
  - **Accepted residual:** "Risk:" key points stay on undecided blind cards (the design's card-only
    rule; the detail page shows them anyway).
