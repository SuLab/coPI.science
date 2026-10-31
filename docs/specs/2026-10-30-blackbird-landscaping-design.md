# Blackbird landscaping: continuous JHU drug-target intake into BlackbirdBot assessments

**Status:** design, 2026-10-30, branch `blackbird-landscaping`.
**Implements:** the owner's decisions of 2026-10-30 (§3.1).
**Read with:** the pre-spec analysis `docs/specs/2026-10-30-blackbird-landscaping-analysis.md`.
"Analysis B2" cites its finding B2.
**Evidence:** external facts were checked live on 2026-10-30. Repository facts cite `file:line` at
commit `0c035b43`. *Unverified* marks anything this design could not confirm; each one is a
Phase 0 task (§15).

## 1. Intent

Blackbird screens an idea today only when a rostered lab agent pitches it in an interview.
Landscaping turns that around:

1. It reads, continuously, everything Johns Hopkins researchers in Baltimore publish, are
   funded for, patent or list for licensing.
2. LLMs find therapeutic ideas in it: new targets for a disease (biology ideas), and new ways
   to make or deliver a medicine (chemistry and delivery ideas).
3. The ideas are deduplicated and ranked with BlackbirdBot's own rubric and prompts, and the
   top 1% is kept.
4. Those ideas are folded into opportunities, each a cluster of related ideas.
5. Each opportunity goes to Blackbird's reviewers and managers as one card on a new page, in
   small batches, and is reviewed the way assessments are reviewed today.

## 2. Goals, non-goals, anti-goals

**Goals.**
- G1. Continuous intake of every qualifying JHU (Baltimore) research output from the sources in
  §6, with a completeness check on every fetch.
- G2. A fully automated LLM funnel, S1-S9 (§7). LLM judgments are calibrated against Claude
  Opus 5. Human expert judgments are collected only to measure the funnel (§10.6), never to
  run it.
- G3. One final assessment per opportunity, in BlackbirdBot's verdict contract: the same fields,
  the same score and band arithmetic. Reviewers and managers decide on it in batches on a new
  page. They make a quick pursue/pass decision and can add an optional full review, as on
  assessments, plus an assessment-style chat.
- G4. Reuse existing code wherever it exists (§14).
- G5. Haiku- or Sonnet-class models for the largest screens and Claude Opus 5 for the deep
  stages. Opus 5.5 is not used.
- G6. Every LLM output is reproducible from stored inputs. It is stamped with its judgment
  epoch (§7.11), and every call lands in a ledger.
- G7. Nothing is thrown away silently:
  - every exclusion keeps its reason;
  - a sample of exclusions is re-checked automatically every cycle;
  - staff can rescue an item (§10.5, §13.4).
- G8. Every statement on a card is marked as sourced (with its references) or as the system's
  estimate (L30).
- G9. Reviewer decisions and their reasons are stored in a structured form a later version can
  learn from (L33).

**Non-goals (v1).**
- Interviews, lab agents, PI profiles, the agent roster.
- Any Slack post. Any contact with a PI or with JHTV.
- Diagnostics, devices, tools and prophylactic vaccines (§5.4).
- Licensed full text, and evidence sources whose license restricts commercial use.
- Changes to:
  - the frozen interview prompt sets;
  - the rubric file;
  - the existing assessment tables or the Assessments page;
  - the simulation engine or the existing worker's job handling.
- Human labeling that feeds the funnel, or human checkpoints inside it. Staff can pause it
  (§13.4). Expert-agreement samples (§10.6) measure the funnel and do not feed it.
- Tuning anything from reviewer decisions (L33).
- A back-test on documents older than the window (L37).
- An embeddings vendor.

**Anti-goals: failure even if the funnel works.**
- A landscaping row reaching Slack, interview headlines, a PI-facing page, or the per-PI
  evidence tables (analysis C1, D6).
- A source truncated without anyone being told (analysis D1).
- A ranking driven by journal or lab prestige rather than evidence (analysis F12).
- An extracted claim that cannot be traced to a verbatim quote in a stored source (analysis
  F13).
- One ranking that mixes judgments made under different criteria, prompts or models (L27).
- An item dropped with no reason recorded or no way back (G7).
- Spend nobody can see.
- The existing worker, the web app or the simulation slowed or blocked by landscaping work
  (analysis D2).

## 3. Decisions

### 3.1 Owner decisions, 2026-10-30

**First round** (answers to the analysis's §7 questions):

| # | Decision |
|---|---|
| L1 | Reviewers and managers review the final output in the same fashion as current assessments, on a new web UI page. |
| L2 | Continuous intake. |
| L3 | Every Johns Hopkins / JHU entity located in Baltimore. |
| L4 | The senior or the corresponding author must be at such an entity, decided by the author-contribution logic that already exists (§5.2 says what that logic is). |
| L5 | Every source already in the system, plus any new source valid for this intake. |
| L6 | Therapeutics only for now; may expand later. |
| L7 | A fixed top 1% of the ranked step, deduplicated. |
| L8 | Duplicate ideas are merged before ranking. Clustering was the portfolio view; L42 makes the cluster the unit that is assessed and reviewed. |
| L9 | Automated LLM judgment to begin with; no human labeling (amended by L35: expert samples measure, they do not feed). |
| L10 | Open-access full text only, for now. |
| L11 | The BlackbirdBot prompt set means BlackbirdBot's interview prompts (`prompts/roles/scout_hub/`). |
| L12 | Triage uses the gates, the three science/need dimensions and the Target Rubric checklist, renormalized. |
| L13 | Outputs reach review and chat, on a new page: not the Assessments page, not Slack. |
| L14 | Ignore profiles; obtain all JHU-associated data from the databases. No interviews in v1. |
| L15 | Separate pipeline tables. |
| L16 | Budget unlimited for now; Haiku or Sonnet for the largest screens. |
| L17 | Claude Opus 5 rather than 5.5, because of refusal rates. |
| L18 | A separate ingest worker. |
| L19 | A sane default that needs no extra licensing. |
| L20 | Every JHU researcher is in scope; existing profiles are ignored. |

**Second round** (from comparing the design with the build team's scout briefing; "G"
numbers are those questions' numbers):

| # | Decision |
|---|---|
| L21 (G1) | Direction is part of the idea key. A mention with unknown direction joins the one idea with the same targets and disease and a known direction, when exactly one exists. |
| L22 (G2) | Chemistry and delivery ideas are in scope, keyed by their agent or technology, and ranked in the same list. |
| L23 (G3) | Evidence grade, stage and independent-group count are shown, given to the judging steps, and used to break ties in the cut. |
| L24 (G4) | Deal-breakers are flags shown to the judging steps and to reviewers. They never exclude automatically. |
| L25 (G5) | Landscaping's own criteria live in a new versioned file; the rubric is untouched. |
| L26 (G6) | No worked examples from Blackbird's past deals in the prompts, for v1. |
| L27 (G7) | A rubric, prompt or model change re-judges everything automatically; the last complete ranking is shown until that finishes. |
| L28 (G8) | A fixed regression set gates any criteria, prompt or model change; an admin can override. |
| L29 (G9) | Rejects: an automated Opus re-check of a sample each cycle, plus a staff view with a rescue action. |
| L30 (G10) | Every key-point bullet and disease-profile field is tagged with its sources or as an estimate. |
| L31 (G11) | Prevalence for common diseases comes from the LLM, with citations, labelled as an estimate. |
| L32 (G12) | A "useful opportunity" is one a reviewer chose to pursue, or approved. |
| L33 (G13) | Reviewer decisions and reasons are recorded in a structured form; nothing is tuned from them in v1. |
| L34 (G14) | Budget stays unlimited. |
| L35 (G15) | Add both a blind comparison mode and an expert-agreement sample. |
| L36 (G16) | Summaries through triage; full text only for the top slice. |
| L37 (G17) | No back-test outside the window. |
| L38 (G18) | Reviewers get batches of cards per cycle. |
| L39 (G19) | Both: a quick pursue/pass with reason tags and a short reason on the card, and the full review as an option. |
| L40 (G20) | A reviewer sees other reviewers' votes only after submitting their own. |
| L41 (G21) | The card title is the BlackbirdBot headline plus a structured line per idea, such as "Inhibit KRAS · pancreatic cancer · small molecule". |
| L42 (G22) | One card per opportunity, an S8 cluster, assessed as a whole. |

**Third round** (details the second round left open; "P" numbers are those questions'
numbers). Where the owner answered "rec", the recommendation was taken, and the row says what
it was.

| # | Decision |
|---|---|
| L43 (P1) | Staff (admins and managers) may rescue a filtered item. |
| L44 (P1) | A rescued item re-enters at the next stage and competes normally. |
| L45 (P2, rec) | Batches of 10 cards, issued weekly. |
| L46 (P2) | Every reviewer and manager gets the same batch. |
| L47 (P2) | Cards left unreviewed carry over to the next batch. |
| L48 (P2) | Only managers and admins also see the full lists of opportunities; reviewers see their batches. |
| L49 (P3) | Claude drafts the starting reason-tag list from the rubric dimensions and the deal-breakers; the owner approves it; it lives in the criteria file. |
| L50 (P4) | Managers and admins are also blind to other votes until they decide. |
| L51 (P4) | Only the quick pursue/pass decision unlocks other votes. |
| L52 (P5, rec) | A blind batch draws 2 of its 10 cards at random from ideas ranked between the cut and five times the cut. |
| L53 (P5, rec) | Score, band and rank are hidden on every card of a blind batch. |
| L54 (P5, rec) | Every fourth batch is blind. |
| L55 (P5, rec) | Staff mark expert accounts on the pipeline page. Each expert judges 10 opportunities per quarter, drawn at random from assessed and blind cards. Naming the people is done by marking their accounts. |
| L56 (P6) | In the tie-break, an earlier stage ranks first. |
| L57 (P7) | One opportunity joins ideas from the same lab that share a target or a disease, plus ideas from one document. Other links show as "related" and do not merge cards. |
| L58 (P8) | Full text is read for exactly the top-1% cut. |
| L59 (P9) | An unregistered agent's key carries its senior lab. |
| L60 (P10) | An opportunity keeps its identity while it keeps at least half its members. Reviewers keep their decisions, marked "changed since your decision", and re-assessment continues. |
| L61 (P11) | An Opus 5 refusal falls back to `claude-opus-4-8`. |

**Fourth round** (the adversarial audit's two owner questions):

| # | Decision |
|---|---|
| L62 (A1) | The Target Rubric checklist counts in the triage score: 80% from the three-dimension mean, 20% from the checklist fraction. Both weights are held in the criteria file. |
| L63 (A2) | Landscaping chat widens the shared chat tables, as Projects did: one budget, one answer in flight across subjects. |

**Fifth round** (the third audit's owner question):

| # | Decision |
|---|---|
| L64 (F3) | Blind probes get full text too: an exception to L58 of 2 ideas per blind batch, so probes are judged on the same footing as the in-cut cards they are compared with. |

Nothing is left open (§18).

### 3.2 Calls this design makes

The owner may override any of these. Each names the section that carries it.

| # | Call | Why | § |
|---|---|---|---|
| D1 | The Baltimore set is the list of OpenAlex institutions whose `geo.city` is Baltimore and which belong to JHU or Johns Hopkins Medicine or are JHU-affiliated (Kennedy Krieger, Jhpiego, Children's Center, Center for Health Security). It is held as an explicit allowlist, and a weekly drift report proposes additions. | L3 says "present in Baltimore"; OpenAlex carries the city (verified). | 5.1 |
| D2 | The "existing determination" is the industry-evidence role logic (`_role`, `_ROLE_RANK`, `_pi_at_jhu`), lifted into a shared module and parameterized by institution set, plus the profile corpus's individual-author rule (R1). | The profile pipeline itself has no senior or corresponding logic; this is the code that does. | 5.2 |
| D3 | Datasets enter only as evidence linked to a qualifying work. | Datasets carry no author roles, so L4 cannot be applied to them. | 5.2 |
| D4 | Reviews are evidence only, never idea sources. Editorials, letters, errata, comments, conference abstracts and papers, book chapters, retracted and non-English works are excluded. | A review reports others' work; the excluded types have no evaluable evidence. | 5.3 |
| D5 | S1 applies no topic filter; the topical decision is S2's. | OpenAlex topics are a poor drug filter (Drug Discovery tags 1 JHU article in the window), and S2 over 45k abstracts costs dollars. | 7.2 |
| D6 | Full text only under CC0, CC BY, CC BY-SA or CC BY-ND. Author manuscripts and CC BY-NC* stay summary-only. | L10 + L19. PMC contradicts itself on manuscripts (analysis G1). | 6.2 |
| D7 | Evidence sources only under unrestricted licenses. DrugBank, IHME GBD, OMIM, NORD, DGIdb, Pharos and Semantic Scholar are excluded. | L19 and analysis F14, F15. | 6.3 |
| D8 | Model choice per stage comes from a Phase 0 bake-off against Opus 5 (§10.1). Haiku 4.5 is excluded. | L16 + L17. Haiku 4.5's tentative retirement is "not sooner than 2026-10-15", already passed. Anthropic promises at least 60 days' notice and none has been given, but a continuous pipeline should not start on the model with the nearest possible retirement. | 9 |
| D9 | Biology ideas are keyed on (target ids, direction, disease id) with the L21 merge rule. Chemistry ideas are keyed on (agent or technology id, target ids if any, disease id if any); an unregistered agent's key also carries its senior lab (L59). Modality is an attribute. | L8, L21, L22, L59. Normalized ids make the merge deterministic. | 7.4 |
| D10 | The triage score blends the rubric-weighted mean of the three dimensions (80%) with the checklist fraction (20%) on the same 1-5 scale (L62). Gates are enforced in code; flags never exclude. Ties break on fixed keys, including grade, groups and stage (earlier first, L56). The tie class straddling rank K is resolved pairwise (§7.6). | L12, L23, L24, L56; analysis B2, B4; audit M6. | 7.5-7.6 |
| D11 | K = ceil(1% of eligible ideas), recomputed every cycle within the active epoch. A slot lost to an S6 gate is not refilled in that cut. The excluded idea version leaves later pools. | L7 says "fixed". | 7.6 |
| D12 | S6 runs 7 specialists for a biology idea (all but `technologic`) and all 8 for a chemistry or delivery idea, with identities revealed. | `technologic` owns platform claims (the rubric's platform guidance). Talent and budget judge the lab's database record and the proposed experiment. | 7.7 |
| D13 | S7 keeps one ranked list per epoch. It is rebuilt by one batched sampled tournament when empty or more than 25% new; otherwise new or changed ideas are inserted one at a time by synchronous binary search under a per-epoch lock. Comparisons are cached across cuts. S9 never waits for S7. | Analysis B2; audits B1, X3. | 7.8 |
| D14 | S8 joins ideas into opportunities by L57's rule. Wider links, including an LLM "same program?" similarity judgment, show as "related". There are no embeddings in v1. | L57; analysis D7; audit m10 (the request's similarity axis); Anthropic offers no embedding model. | 7.9 |
| D15 | S9 assesses per opportunity (L42). Each member idea keeps its own S6 record and structured line. | Reviews attach to one verdict per card. | 7.10 |
| D16 | The BlackbirdBot prompt set is reused by verbatim extraction of named sections at load time, never edited. Landscaping frames and criteria live in `prompts/landscaping/`. | L11, L25 + the prompt freeze. | 8 |
| D17 | Cadence: daily incremental pulls, a weekly OpenAlex full-window sweep, monthly RePORTER. | OpenAlex's created/updated filters need a paid Member+ plan (verified). | 6.1 |
| D18 | Visible to admins, managers and reviewers only. | L1, L13. | 13 |
| D19 | No spend cap by default (L16, L34), but a ledger, a spend report and a kill switch. | Anti-goal "spend nobody can see". | 9.5 |
| D20 | S4 is blind to authors, institutions, journals and grant PIs; S6 reveals them. | Analysis F12. | 7.5 |
| D21 | Landscaping decisions and reviews are stored in structured form (L33). There is no `feedback_mode`, and they never feed the existing review bot. | The review bot proposes edits to frozen interview prompts, which had no part here. | 13.3 |
| D22 | The `landscape-worker` service goes into the working-tree compose file under the RCA plan's decision D5 (root `CLAUDE.md`; not this spec's D5). | Precedent: the worker's prompts mount and the agent's supervisor command. | 12.5 |
| D23 | Landscaping has its own work-state tables and does not use `jobs`. | §12.1: the existing worker would claim, sweep and kill its rows. | 12.1 |
| D24 | The pages are GET-only under `/workspace/landscape`. Writes go to `/reviews` and a new `/landscape` router. | Workspace POST inventory test. | 13.1 |
| D25 | Chat runs behind `landscape_chat_enabled`, default false, on its own model setting `llm_landscape_chat_model = claude-opus-5`. It is keyed on the stable opportunity and widens the shared chat tables (L63). | L17, L63; audit M1: the shared chat model setting is `claude-opus-5-5`; audit X7. | 13.5 |
| D26 | The list, detail and review templates are copied, not refactored out of the assessment templates. | The Assessments page stays untouched (non-goal). | 13.2 |
| D27 | A `direction: unknown` mention's attachment (L21) is decided once, when it merges, and is not revisited if a second direction appears later. | A deterministic, stable merge; the evidence stays visible on the idea it joined. | 7.4 |
| D28 | Full-text evidence gathered at S6 feeds S6-S9 only. It never creates a new idea version or moves the S5 cut. | L36: triage and the cut run on summaries; without this rule every S6 would trigger its own re-triage. | 7.7 |
| D29 | The structured line (L41) is composed in code from the normalized fields, not written by the model. | It must match the idea record exactly. | 7.10 |
| D30 | Cards are ordered within the list and a batch by their best member idea's S7 rank, or its S5 rank until S7 has placed it. | One order, derived from the ranking reviewers are measuring (§10.6). | 13.1 |
| D31 | Batch protocol. Intent rows (`submitting`) and per-item rows are committed before `batches.create`, and `custom_id` is `b{seq}-{item_seq}`, which fits `^[a-zA-Z0-9_-]{1,64}$`. Startup reconciles every batch since the oldest `submitting` row, holding its items until matched. Results are ingested idempotently. Every result type has a transition. | Audits M5, X4. | 7.1, 9.3 |
| D32 | One request builder per stage sets `thinking` (adaptive), effort and `max_tokens` (a constant ≤ 21,333) for both the batch and synchronous paths. A `max_tokens` stop is retried once with the stage's higher limit; a second one records `failed:truncated`, which is terminal. | Audit M3; the existing non-streaming ceiling. | 9.2 |
| D33 | Shared quotas. Landscaping paces NCBI at ≤ 3 requests/s on the shared key, and its synchronous Opus work yields while a simulation run is live. Every client backs off on 429. | Audit M4; anti-goal "the simulation slowed". | 12.2 |
| D34 | Near-duplicates fold at the cut. Ideas with the same targets and direction whose diseases are Mondo ancestor and descendant, or whose target sets nest, take one slot: the higher-ranked keeps it and lists the others. | L7 ("deduplicated"); audit M7. | 7.6 |
| D35 | An idea's dossier carries at most 20 documents. All grants, patents and JHTV listings come first, then the most recent qualifying papers; the rest are listed by id. | Audit M7: broad keys would grow without bound. | 7.4 |
| D36 | When a provisional key resolves to an existing normalized key, the two ideas merge through an alias record that keeps every foreign key valid. | Audit M7. | 7.4, 11.1 |
| D37 | An idea's version changes only when a new qualifying idea-source document attaches. A database refresh updates evidence without a new version. An assessed opportunity's S6-S9 re-run at most once per batch cycle. | Audit M8; L45. | 7.4, 7.10 |
| D38 | Document identity precedence is DOI > PMID > OpenAlex id. A record seen first in PubMed is re-keyed in place when OpenAlex arrives, and S1 is recomputed. S2 and S3 re-run only if the summary text changed. | Audit M12. | 6.1 |
| D39 | An OpenAlex work listed with exactly 100 authorships is refetched singly (free) before qualification. | Audit m8: list responses cap authorships at 100. | 6.1 |
| D40 | Full texts are kept, not pruned. | Audit m9: G6 and F13 need them; the size is small (§11.3). | 11.2 |
| D41 | A failed text fetch is retryable (`text_basis="fetch_failed"`) and never mistaken for "summary only". | Audit m14. | 6.2 |
| D42 | The fallback chain is stage model → `claude-opus-5` → `claude-opus-4-8`, skipping the model that refused. It is synchronous and client-side. | L61; audit M2. | 9.3 |
| D43 | If no Haiku or Sonnet candidate passes the bake-off for S2-S4, the best of them is used and the shortfall is shown on the pipeline page. | L16; audit m5. | 10.1 |
| D44 | Untrusted text is fenced with tags already in `FENCE_TAGS` (`publication_abstract`, `methods_section`, `patent`, `trial_record`); `FENCE_TAGS` does not change. | Keeps `src/agent/` untouched (audit M10); matches `SYNTHESIS_DATA_NOTICE` (audit m-A). | 9.2 |
| D45 | Shared modules the agent imports change additively only: new functions, with existing signatures, outputs and defaults unchanged. The agent needs no rebuild for landscaping, and its next ordinary rebuild picks the additions up. | Audit M10; root `CLAUDE.md` deploy rule. | 12.5 |
| D46 | `/workspace/landscape/pipeline` is registered before the UUID detail route, and a test pins the order. | Audit m13. | 13.1 |
| D47 | Landscaping chat has its own daily dollar sub-cap inside the shared chat total: `50.0`, half the total's default. | Audit m12: it must not lock out assessment chat; audit m-C. | 13.5 |
| D48 | A weekly batch holds up to 10 never-issued opportunities in card order, the same for everyone. Carry-overs are per viewer and do not count toward the 10. Changed cards show as "changed since your decision". | L45-L47; audit X6. | 13.1 |
| D49 | One lab key for every record type: ORCID > OpenAlex author id > RePORTER PI profile id > a normalized name key, with aliases. | L57, L59; audit X8. | 5.2 |
| D50 | Decisions, reviews, assignments and chat are keyed on the stable opportunity. Decisions and reviews record the `landscape_assessment_id` they acted on, and "changed since your decision" compares it with the assessment shown. A reviewer reads only opportunities in issued batches or in their own expert sample (D63). The same rule guards the `/reviews` POST routes and the chat router. | L60, L48; audits X7, N15. | 13.1, 13.3, 13.5 |
| D51 | Documents, texts and the evidence cache are shared inputs. Every judgment, ideas and opportunity versions included, is epoch-scoped. The active epoch keeps its own intake and is never changed by a candidate. | L27; audit X1. | 7.1, 7.11 |
| D52 | The S5 boundary tie class is resolved by one Message Batch of pairwise comparisons over blinded S4 dossiers (round-robin up to 20, else a balanced 10-regular pairing), ordered by win fraction. The previous cut stays current until it ends. | Audits M6, X2. | 7.6 |
| D53 | Blind-batch probes are single-idea opportunities outside S8 grouping and identity. Blind batches are shuffled, and blinding covers card, detail and chat until the viewer decides. | L52-L54; audit X5. | 7.9, 10.6, 13.1 |
| D54 | The regression set is versioned and covers S6, S7, S8 and S9 with their own thresholds against the active epoch's stored outputs. | L28; audits X9, F9. | 10.4 |
| D55 | Only the active epoch's S8 and an epoch's activation create, version or retire opportunities. A candidate's S8 writes provisional groupings that are matched at activation. Probes are the one exception (D62). | L60; audit F1. | 7.9 |
| D56 | Blinding also hides the rationale fields. A viewer with undecided blind cards does not see that batch in Current or All. Probe flags appear only after the batch is superseded. S9 never sees rank. | L53; audit F2. | 7.9, 7.10, 10.6 |
| D57 | Stage rows are updated by compare-and-set on ingestion; only the ledger is insert-ignore. | Audit F4. | 7.1 |
| D58 | A process-level NCBI interval, set at landscaping worker start, covers every E-utilities call in that process. | D33; audit F6. | 14 |
| D59 | A candidate epoch's S9 assesses its current provisional groupings and the issued probes (D62). An issued probe whose candidate S6 ended terminally, or whose idea has no row in the candidate, gets the terminal S9 status `failed:no_deep`, so it cannot hold activation back. At activation, the assessment of each current grouping is re-pointed to the version minted for it, in the activating transaction; assessments of groupings the candidate replaced stay with them as history. | Otherwise a candidate's S9 has no key and activation can never complete. Audit N1. | 7.9, 7.10, 11.1 |
| D60 | The Unreviewed / Reviewed tabs are per viewer, by the viewer's own quick decision. | L48 asks for the Assessments tabs; shared tabs would show others' activity before the viewer decides, which L50 forbids. Audit N2. | 13.1, 13.3 |
| D61 | An epoch is identified by content hashes of its judging inputs only: the eight judging prompt files, the criteria file's judging tables (all but `[meta]` and `[reason_tags]`), the rubric, the extracts and the eight stage model settings. The fallback chain, the audit model and the chat model are not epoch inputs. | Editing `chat.md` or a reason tag must not re-judge everything and mark every decision changed. Audits N5, m12. | 7.11, 8.1 |
| D62 | Probe lifecycle. The probe draw makes probes, the one exception to D55. Before issue, a probe is redrawn, and the replaced one retired, when its S6 finds a gate not met, when its S6 or S9 ends `refused` or `failed:*`, when it enters the top K before issue, or when its epoch is retired before issue. An issued probe is never redrawn or retired. If a probe is not ready at issue, that week's batch is a normal one and the next is blind. A candidate epoch re-runs S6 and S9 on every issued probe. A card whose opportunity is retired leaves carry-overs and is never shown as changed; a card with no completed assessment in the active epoch stays and says so. Carry-overs are grouped by batch, newest first: a normal batch in card order (D30), a blind batch in its stored shuffled order. | Audits N6, N10; round-six audit M3, L4; final check F4, F11, F16. | 7.9, 10.6, 13.1 |
| D63 | An expert's sample comes from opportunities they have not decided on or reviewed. From the draw until they submit, every surface hides S9's and S6's calls on those items from them (gates, scores, provisional scores, deal-breaker confirmations, panel opinions, rationales, band, recommendation), in the chat record too (the Verdict and Panel documents, and any S6 call in the Disease profile and Evidence documents); on a blind-batch card both hidings apply. They can open their own sample items even when never issued. Items issued before the draw, and manager-experts' items, are reported separately. | An anchored expert measures nothing, and the sample includes unissued cards. Audit N8; implementation-plan audit M4; round-six audit M4. | 10.6, 13.1 |
| D64 | A chemistry key carries its lab's canonical key at merge time. A later lab unification merges the idea through the alias path (D36) in every non-retired epoch at once, and opportunity matching compares keys after resolving both epochs' aliases. | Audit N9; round-six audit M5. | 5.2, 7.4, 7.9 |
| D65 | After each activation, a new regression-set version freezes the new active epoch's outputs. The first epoch is checked on S2-S4 only. A further input change supersedes an unactivated candidate: it is retired, and the new candidate is built from the active epoch, or, while none is active, from the replaced candidate's unchanged stages. | Audit N13; round-six audit L9. | 7.11, 10.4 |
| D66 | The reject audit runs as a Message Batch, built with the audited stage's request builder on `landscape_audit_model`. | It is not time-critical, and one builder per stage keeps the comparison fair. Audit N14. | 7.1, 9.2, 10.5 |
| D67 | A patent or JHTV extraction lists the papers it names (`linked_publications`), each verbatim in its text. A link is written only when one resolves to a stored document by DOI, PMID or exact title. | §6.1 needed a field to carry the link; this keeps it traceable. | 6.1, 7.4 |
| D68 | Phase 0 ingests one month from each window year, picked at random and recorded, from every enabled source, plus every positive-control document, through S1. The bake-off samples those documents; with its model picks applied, the whole funnel then runs on them, issuing nothing. The pilot report holds the bake-off, the probe results and the §10.2 measurements. | §10.1 samples "in-window JHU documents" before any backfill exists; the implementation-plan audit (M8) found the pilot report undefined; the round-six audit (M7) found random months would miss the positive controls. | 10.1, 15 |
| D69 | The worker moves the phase from `backfill` to `continuous` by itself once the backfill has drained: every enabled source's backfill slices have a terminal fetch (complete, or incomplete after its retries and reported, §12.4), and no S2-S4 work is pending for documents first seen in the backfill. S5-S9 do not run in phase `backfill`, so the first full cut covers the whole window. | Nothing else would end the backfill and start the D17 cadence; a flip on fetches alone would cut a partial pool. Implementation-plan re-audit N1; final check F3. | 12.3, 15 |
| D70 | No candidate activates while the phase is `pilot` or `backfill`, and a candidate activates only when the readiness rule of §7.11 step 4 holds: its S2-S4 work has drained in phase `continuous`, it has a complete cut taken after that (then frozen), and S6, S8 and S9 have ended on that cut. Until the first activation the pages show no opportunities, and no review batch is issued. | The pilot's cut covers only the pilot sample and must not go live; a cut taken before the drain is partial. Final check F8; scoped re-check M1. | 7.11, 15 |

## 4. Constraints

- **K1. Prompt freeze.** "The interview prompt sets are frozen (owner, 2026-10-26)"
  (`src/agent/CLAUDE.md:41`). Landscaping reads `prompts/roles/scout_hub/*` and
  `prompts/specialists/*` but never edits them, and never bumps a `role.toml`. Its own prompts
  live in `prompts/landscaping/`, with their own version manifest (§8.1). That is unlike
  `prompts/projects/`, which carries only a content hash
  (`src/agent/project_prompts.py:45-57`) and is embedded in its review doc by
  `scripts/sync_prompt_set_docs.py`.
- **K2. The rubric.** `prompts/rubric/blackbird-rubric.toml` is read through
  `src/services/blackbird_rubric.py` (`load_rubric`, `render_rubric_markdown`,
  `weighted_score`, `band`) and never edited.
- **K3. Schema.**
  - New tables only, in additive migrations.
  - No foreign key to `simulation_runs` and no new value in an existing enum (the `jobs` table
    is not used, D23).
  - Exactly two exceptions touch existing tables:
    - `app_settings` rows under four new keys: `landscape_paused`, `landscape_phase`,
      `landscape_positive_controls`, `landscape_experts` (§12.3);
    - the chat tables, widened as Projects did (L63, §13.5).
- **K4. Separate worker** (L18).
  - The ingest worker never claims, sweeps or requeues the existing worker's jobs.
  - It holds its own advisory-lock key.
  - The existing worker's code paths are unchanged.
  - Shared modules change additively only (D45), and the characterization and profile tests
    prove it.
- **K5. Deploy.** Root `CLAUDE.md` rules hold: build, migrate from a one-off container off the
  new image, then `up -d` per service. Never `--remove-orphans`, never `up -d --build`, and the
  agent is never recreated for this work. The compose file is uncommitted by decision D5 of
  the RCA plan; §12 says how the new service is added under that decision.
- **K6. Public origin.** `origin` is public. No landscaping output, no ranking of named
  researchers, nothing from `data/` and no secret ever enters git. Reports go to the database
  or to host paths outside the checkout.
- **K7. Rights** (L19, D6, D7). Full text and evidence only under the licenses in §6.2-§6.3.
  PMC full text only through the permitted routes: the PMC Cloud Service, OAI-PMH,
  E-utilities and the BioC API ([PMC text mining](https://pmc.ncbi.nlm.nih.gov/tools/textmining/)).
- **K8. NCBI.**
  - Rates: 10 requests/s with the existing key, 3 without.
  - PubMed ESearch returns at most 9,999 records per query, so the backfill is sliced by date.
  - PMC ESearch, like PubMed, returns at most 9,999 records per query (announced 2026-01-06,
    in effect since early February 2026).
  - BioC allows at most 3 requests/s.
  - NCBI asks that jobs of more than 100 requests run on weekends or 9 pm-5 am ET.
  - The key's limit is shared by the app, the worker and the agent, so landscaping paces itself
    at ≤ 3 requests/s (D33). It does this through a process-level interval set at worker start,
    which covers reused PubMed functions too (§14, F6).
- **K9. OpenAlex.**
  - Cursor paging needs a key, and production has one.
  - `per-page=200` is honoured (measured).
  - A list page costs 1 credit, $0.0001.
  - `from_created_date` and `from_updated_date` return "Plan upgrade required" (Member+ and
    Partner only, measured).
  - The free-key budget is $1 a day.
- **K10. Models.**
  - No `claude-opus-5-5` (L17).
  - No `claude-haiku-4-5-20251001` (D8).
  - The Batches API has no server-side fallback, so refused items are re-run synchronously
    down the D42 chain.
  - `bio` refusals are billed even with no output.
  - Model retirement dates "not sooner than": Opus 5 2027-07-24, Sonnet 5 2027-06-30, Sonnet
    5.5 2027-09-28, Haiku 5.5 2027-10-07
    ([deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations)).
- **K11. Host.** One production host, shared with another deployment; 18 GB free (73% used,
  measured 2026-10-30). Landscaping stores text, not PDFs or XML (§11.3).
- **K12. Separation from per-PI data.** Landscaping never reads or writes `publications`,
  `pi_grants`, `pi_industry_evidence`, `pi_companies`, profiles or the roster (L14, L20,
  analysis D6). Its own tables hold everything it ingests.

## 5. Scope rules

### 5.1 The Baltimore institution set (L3, D1)

**Included.** OpenAlex institutions with `geo.city` Baltimore that are part of, or affiliated
with, Johns Hopkins:

| OpenAlex id | Institution | Lineage root |
|---|---|---|
| I145311948 | Johns Hopkins University | itself |
| I2799853436 | Johns Hopkins Medicine | itself |
| I4210150714 | Johns Hopkins Hospital | JH Medicine |
| I4210164401 | Sidney Kimmel Comprehensive Cancer Center | JH Medicine |
| I2802697821 | Johns Hopkins Bayview Medical Center | JH Medicine |
| I4392738222 | Center for Innovative Medicine | JH Medicine |
| I4391767829 | Neurofibromatosis Therapeutic Acceleration Program | JH Medicine |
| I4210092215 | Berman Institute of Bioethics | JHU |
| I4210151848 | Center for Communication Programs | JHU |
| I4391012584 | Hopkins Extreme Materials Institute | JHU |
| I4394709191 | Johns Hopkins University Press | JHU |
| I4389425327 | JHU Center for AIDS Research | JHU |
| I4405260374 | JH Materials Characterization and Processing Facility | JHU |
| I4392738135 | Packard Center | JHU |
| I4387152773 | PARADIM | JHU (+ others) |
| I4388891755 | Materials Science in Extreme Environments University Research Alliance | JHU (+ others) |
| I4210120405 | Maryland Space Grant Consortium | JHU (+ others) |
| I4210129832 | Johns Hopkins Children's Center | itself |
| I4210114877 | Johns Hopkins Center for Health Security | itself |
| I4405267562 | Jhpiego | itself |
| I1298154867 | Kennedy Krieger Institute | itself |

Source: `https://api.openalex.org/institutions?filter=lineage:I145311948|I2799853436`, and a
search for "hopkins" and "kennedy krieger" with `select=id,display_name,geo,lineage`, run
2026-10-30.

**Excluded (not in Baltimore):**
- Applied Physics Laboratory (North Laurel, MD)
- All Children's Hospital (St. Petersburg, FL)
- SAIS, Washington, with its Bologna and Nanjing centers
- Sibley Memorial (Washington, DC)
- Suburban Hospital (Bethesda, MD)
- Howard County General (Columbia, MD)
- Peninsula Regional (Salisbury, MD)
- the Saudi Aramco medical services organizations (Dhahran)
- MUJHU (Kampala)

**Other sources.**
- NIH RePORTER organizations: `JOHNS HOPKINS UNIVERSITY` and `HUGO W. MOSER RES INST KENNEDY
  KRIEGER`. NSF awardee: `Johns Hopkins University`.
- ROR ids, used for DataCite: 00za53h95 (JHU), 037zgn354 (JH Medicine), 05q6tgt32 (Kennedy
  Krieger), 05m5b8x20 (Sidney Kimmel).

**Raw affiliation strings.** These cover records with no OpenAlex institution, such as a
PubMed-only record. A string qualifies when:
- `jhu_rules.is_hopkins_affiliation(text)` is true, or it names Kennedy Krieger or Jhpiego;
  **and**
- it names none of the excluded places above: "Applied Physics Laboratory", "Laurel", "All
  Children's", "St. Petersburg", "SAIS", "Bologna", "Nanjing", "Sibley", "Suburban", "Howard
  County", "Peninsula Regional", "Aramco", "Dhahran", "Kampala".

**Drift.** A weekly job lists OpenAlex institutions that match "Hopkins" or "Kennedy Krieger"
and are not in either list. The list goes to the pipeline page (§13.4) for a code change;
nothing is added automatically.

### 5.2 Author qualification (L4, D2, D3)

**What exists today:**
- **R1 (profile corpus).** The profile corpus requires a PI to be a *named individual author*
  and excludes consortium and collective authorship (`docs/specs/2026-08-13-jhu-instance-rules-design.md:28-46`).
  It has no senior or corresponding logic.
- **Role determination (industry evidence).** It lives in
  `src/services/industry_sources/openalex_industry.py`:
  - `_role(auth)` returns `corresponding` when OpenAlex `is_corresponding`, else
    `author_position` (`first`/`last`/`middle`), at :98-101;
  - `_ROLE_RANK = {"corresponding": 0, "last": 1, "first": 2}`, at :56;
  - `_pi_at_jhu(auth)` tests the authorship's institution ids against `JHU_OPENALEX_IDS`, then
    its raw affiliation strings with `is_hopkins_affiliation`, at :92-95.
- **Role weighting.** `industry_score._ROLE_FACTOR` weighs first, last and corresponding
  authorship 1.5× (`src/services/industry_score.py:29`).
- **Unused column.** `publications.author_position` exists (`src/models/publication.py:102`)
  but nothing writes it.

**The landscaping rule.** A work qualifies when at least one *individual* authorship passes
both tests:
- **Role:** `_role` is `corresponding` or `last`.
- **Place:** an institution id on the authorship is in the §5.1 set. If the authorship lists
  no institutions, one of its raw affiliation strings passes the §5.1 matcher.

An authorship whose display name is a collective (consortium, group, network, investigators,
study) is not individual, per R1. The builder writes that test with fixtures.

**Implementation.** Lift `_role`, `_ROLE_RANK` and the place test into a shared module. The
place test takes three parameters (audit m7):
- the institution-id set;
- the raw-string matcher;
- a fallback mode: check raw strings *always*, as `_pi_at_jhu` does, or *only when no
  institutions are listed*, as landscaping needs.

`openalex_industry` calls it with `JHU_OPENALEX_IDS`, `is_hopkins_affiliation` and mode
*always*, so its behaviour and tests do not change (D45). Landscaping calls it with the
Baltimore set, the §5.1 matcher and mode *only when none*.

**By record type:**
| Record | Rule | `role_basis` |
|---|---|---|
| OpenAlex work | the rule above | `corresponding` or `last` |
| PubMed-only work (no OpenAlex match) | the last valid individual author (no `CollectiveName`) is senior, tested with the §5.1 matcher; PubMed carries no corresponding flag | `pubmed_last_author` |
| NIH grant | any PI or multi-PI on an award whose organization is in §5.1 | `grant_pi` |
| NSF grant | the awardee is JHU | `grant_pi` |
| Patent | assignee or applicant is JHU or Kennedy Krieger. APL inventions are also assigned to JHU and cannot be told apart, so S2 judges relevance. | `assignee` |
| JHTV listing | qualifies | `jhtv` |
| Dataset | never qualifies alone; attached as evidence to a qualifying work it cites or is cited by (D3) | n/a |

**Lab identity** (X8, D49). A *lab key* is defined for every record type. One function,
`lab_key(record)`, serves the chemistry key (L59), the same-lab edges (L57), the JHU group
count and the card's "labs, named by person". It is never used for filtering.

The key takes the first of these that exists:

| # | Key | Where it comes from |
|---|---|---|
| 1 | `orcid:<id>` | OpenAlex authorship, PubMed `Identifier` (RePORTER has no ORCID search criterion, `src/services/nih_reporter.py:3-4`; its record fields are probed in Phase 0) |
| 2 | `oa:<author id>` | OpenAlex |
| 3 | `nih:<PI profile id>` | RePORTER |
| 4 | `name:<surname>|<first initial>` | normalized |

Each record type yields its names as follows:

| Record | Senior names |
|---|---|
| OpenAlex work | the qualifying authorship |
| PubMed-only work | the last valid individual author |
| NIH grant | the contact PI |
| NSF grant | the PI |
| Patent | the first inventor |
| JHTV listing | the RSS item's `author` (the listed inventor or contact) |

`landscape_labs` keeps aliases (F5). Two rules unify keys:
- **One record, two keys.** When one record carries two keys for the same person (an OpenAlex
  authorship with an ORCID), both resolve to the highest-ranked one.
- **Cross-record links.**
  - A grant's PI (`nih:` or `name:`) is unified with a paper's senior lab key when RePORTER
    links that grant to that paper (`publications_for_cores`) and the names agree on surname
    and first initial.
  - A patent inventor or JHTV contact (`name:`) is unified with an existing lab only when
    exactly one Baltimore lab has that name key. With two or more, it stays a separate
    `name:` lab, and the collision is reported.

- **Keys that change later** (D64). A chemistry idea's key carries its lab's canonical key at
  merge time. When a later unification changes that lab's canonical key, the idea merges into
  the re-keyed idea through the alias path of §7.4 (D36), so its history and foreign keys stay
  valid. The re-key runs in every non-retired epoch (the active one and any candidate) in one
  transaction, and matching at activation resolves keys through both epochs' alias tables.

Phase 0 probes RePORTER's PI fields to confirm no ORCID is available (§15).

### 5.3 Window and document types

- **Window:** a rolling 3 years ending today.
  - Works, patents, JHTV listings and datasets: by publication date.
  - Grants: by any overlap of the project period with the window, or a fiscal year in the
    window.
  - A record that leaves the window stays stored. It stops counting toward eligibility (§7.6).
- **Idea sources:** articles, preprints, grants, patent applications and grants, JHTV
  listings.
- **Evidence only:** reviews, datasets, and works found by the S6 literature search.
- **Excluded (D4):** editorials, letters, errata, comments, conference abstracts and papers,
  book chapters, retracted works (OpenAlex `is_retracted`, PubMed retraction publication
  types), and works whose title language is not English.

### 5.4 Therapeutic scope (L6, L22)

The S2 and S4 prompts carry this definition verbatim, from the criteria file (§8.1).

**In scope.** There are two idea types, and a document can hold both. Each idea is then judged
on its own terms, and the two are linked (§7.4).
- **Biology ideas:** research that identifies, validates or modulates a specific molecular
  intervention point in a human disease.
  - *Intervention points:* a gene, protein, RNA, complex, defined pathway node, or a cell
    population with a molecular handle.
- **Chemistry and delivery ideas:** a new way to make or deliver a medicine. This covers a new
  agent, series or format against a target, including targets ordinary drugs cannot reach, and
  a delivery technology such as one that carries a drug across the blood-brain barrier. A
  single disease or target may or may not be named.

**What either type covers:**
- **Any modality:** small molecule, antibody or other biologic, peptide, oligonucleotide, gene
  therapy, gene editing, cell therapy, degrader, therapeutic vaccine, microbiome therapeutic.
- **Repurposing:** an existing drug moved to a new indication with a mechanistic rationale.

**Out of scope:**
- diagnostics, and biomarkers without a therapeutic hypothesis;
- devices and imaging;
- prophylactic vaccines;
- epidemiology, health services, and behavioural or surgical interventions;
- pure research methods or tools that make no medicine;
- veterinary or agricultural work.

**Not a filter at S2:** unmet need (analysis F3) and druggability. Druggability is
modality-aware and judged at S4 and S6 (analysis F4).

## 6. Sources

### 6.1 Discovery sources

Each fetch records the source's reported total against the rows received; a mismatch fails
the fetch loudly (§12.4).

| Source | Query | Backfill | Incremental (D17) | Reuse |
|---|---|---|---|---|
| **OpenAlex works** | `authorships.institutions.lineage:I145311948\|I2799853436\|I1298154867\|I4210129832\|I4210114877\|I4405267562`; `type:article\|preprint\|review`; publication-date range; `select` of `id,ids,doi,title,type,publication_date,language,is_retracted,authorships,abstract_inverted_index,primary_location,best_oa_location,open_access,primary_topic,referenced_works`. Then §5.2 qualification on the returned authorships. | window sliced by month, cursor paging, 200 per page | daily: publication date ≥ today − 120 days; weekly: the full window | a new public `openalex.iter_works` (cursor paging, no cap, page counts) and `fetch_work`; `_list_works` unchanged; `reconstruct_abstract`; `openalex_auth`; `openalex_budget` |
| **PubMed** | `("Johns Hopkins"[ad] OR "Kennedy Krieger"[ad] OR "Jhpiego"[ad])` | by month of `[dp]` (each slice under 9,999) | daily `[edat]` range (1,097 records 2026-10-01..29, measured) | new additive `pubmed.esearch_page`, `fetch_pubmed_xml`, `parse_pubmed_extras` (`search_pmids` and `_parse_pubmed_xml` unchanged); `lookup_pmcids` |
| **NIH RePORTER** | `org_names_exact_match` = the two §5.1 names; `fiscal_years`; include `AbstractText`, `PhrText`, `Terms`, `PrefTerms`, PIs, dates, amounts | per fiscal year (≈1.7k a year) | monthly: current and previous FY; upsert by `appl_id`, group by `core_project_num` | `nih_reporter.search_projects` (raised `max_total`, `AbstractText` added), `publications_for_cores` (grant → PMID links) |
| **NSF Award API** | `awardeeName="Johns Hopkins University"`, date range; `abstractText` | one query (206 in window) | weekly | new client |
| **USPTO ODP** | applications search, applicant or assignee "Johns Hopkins" or "Kennedy Krieger", date range | by quarter | weekly | `odp` client, a new applicant-only query modeled on `uspto_inventor.py:103-104`. *Unverified:* field names for dates, abstract and claims (keyless 401) |
| **JHTV technologies** | RSS `https://jhu.technologypublisher.com/rss.aspx` | the whole feed, filtered to the window (440 items) | daily; upsert by `caseId`/`guid` | new reader; RSS only, no page scraping (terms unverified) |
| **Preprints** | inside the OpenAlex query (`type:preprint`; 6,792 in window) | — | — | — |
| **Datasets (evidence)** | DataCite: JHU ROR affiliation id and name variants, resource type dataset. GEO: `[INST]` name variants with `gse[ETYP]` | window | weekly; linked to works by PMID/DOI (D3) | new clients |

**Not used, with the reason:**
- ORCID: per person; profiles are ignored (L14).
- Company discovery and COI: per PI, depends on profiles.
- ClinicalTrials.gov: an evidence source (§6.3), not a discovery source.
- PatentsView: API paused since 2026-03-20.
- bioRxiv API: cannot filter by affiliation.

**Cross-source linking** (D38). One work found in OpenAlex and PubMed is one document.
- **Matching:** on DOI, else PMID; OpenAlex `ids` carries both.
- **Identity precedence:** DOI > PMID > OpenAlex id.
- **PubMed first.** A record first seen in PubMed (the usual case, because daily `[edat]` pulls
  come before OpenAlex indexes a work) is re-keyed in place when its OpenAlex record arrives.
  S1 is then recomputed, so `pubmed_last_author` gives way to the OpenAlex roles.
- **Summary precedence** (m-N). The summary text is PubMed's abstract when PubMed has one,
  else OpenAlex's reconstructed abstract. Re-keying therefore rarely changes it. S2 and S3
  results are kept unless the summary text changed; if it changed, they re-run.
- **Two existing rows** (F10). When two documents already stored turn out to be one work, the
  row with the higher-precedence id is kept.
  - The other becomes a tombstone (`merged_into`). Its own S2 and S3 rows stay attached to it
    as history and are not re-pointed.
  - Shared-input links (`landscape_document_links`, idea evidence of later epochs) point at the
    kept row.
  - If the kept row's summary is the one being replaced, its S2 and S3 re-run.
  - Ideas count, hash and pool only documents that are not tombstones (`merged_into` is
    null), so the merged work is never counted twice (§7.4).
- **Large author lists.** An OpenAlex work listed with exactly 100 authorships is fetched again
  singly, which is free, before qualification (D39). List responses cap authorships at 100
  (`src/services/openalex.py:63-64`).
- A preprint and its published article are linked: OpenAlex ids, else exact title match
  within 2 years. The pair counts as one source of an idea, and the article is preferred.
- A grant links to papers through RePORTER publication links.
- Patents and JHTV listings link to papers only when the S3 extraction names one (its
  `linked_publications`, §7.4, D67). Such a link
  is LLM-derived, so it is epoch-scoped (`landscape_extraction_links`, F1), not a shared
  document link.

**Pacing for sources without a published limit.**
- ClinicalTrials.gov reuses the existing 1 request/s pacer
  (`src/services/project_search.py:45`).
- Europe PMC starts at 1 request/s.
- Phase 0 records any 429s (§15).
- NCBI is paced at ≤ 3 requests/s (D33).

**APL patents** (audit m16). If ODP exposes inventor cities (a Phase 0 probe), a JHU-assigned
patent whose inventors are all in Laurel or North Laurel is excluded at S1 as APL.

### 6.2 Full text (S6 only; L10, L36, D6)

**When.** Full text is read only for the documents behind ideas in the current cut (L36, L58)
and behind the two probe ideas of each blind batch (L64).
S2-S5 work from summaries: title + abstract, grant summary, patent abstract and first claim,
JHTV description. "The top slice" is exactly the S5 cut (L58).

**Routes**, tried in order:
1. **PMC** (`pmcid` known). Read the per-article metadata JSON in the `pmc-oa-opendata` S3
   bucket (anonymous). It gives `license_code`, `is_manuscript`, `is_retracted` and the text,
   XML and PDF URLs; verified on PMC176545.1.
   - When `license_code` is CC0, CC BY, CC BY-SA or CC BY-ND, `is_manuscript` is false and the
     article is not retracted, fetch the text.
   - The BioC API (sectioned passages, at most 3 requests/s) is the alternative route.
2. **Europe PMC** `fullTextXML` (no key). Used for open-access articles whose Europe PMC
   `license` field (for example `cc by`; verified 2026-10-30) is in the same set.
3. **bioRxiv / medRxiv JATS** for CC BY-family preprints. The API's `details` endpoint
   returns `license` and `jatsxml` (verified 2026-10-30).
4. Otherwise **summary only**, recorded as `text_basis="summary"`. S6 and the card show that
   basis.

**A failed fetch is not "summary only"** (D41). A transient error (HTTP 5xx, 429 or a timeout)
records `text_basis="fetch_failed"` and is retried up to 3 times with backoff before falling
back to the summary.

**Never used:** publisher text-mining APIs, OpenAlex content downloads (license per item
unknown), PDFs.

**Text handling:**
- Parse into sections: title, abstract, introduction, results, discussion, methods, figure and
  table captions. Drop references.
- Store as gzip text (§11.3).
- Cap at 200,000 characters. A longer text is truncated at section boundaries and flagged.

### 6.3 Evidence and normalization sources (D7)

All are called with pacing and cached per source release (§11.2).

| Source | Used for | License |
|---|---|---|
| PubTator3 export, `/publications/export/biocjson?pmids=` (3 req/s) | gene, disease and chemical mentions with ids, per document | NCBI; no restriction stated |
| HGNC REST | gene symbol, alias and previous symbol → HGNC id, Entrez, Ensembl, UniProt | no restrictions |
| Mondo SSSOM mappings, release file | MeSH → Mondo exact matches; Mondo labels and synonyms; parent ids | CC BY 4.0 |
| Open Targets Platform GraphQL (release 26.09) | target-disease association (overall and genetic), tractability by modality (SM, AB, PROTAC, other), safety events, `drugAndClinicalCandidates` | CC0 (third-party sources vary) |
| ChEMBL web services (release 37) | target mechanisms, max phase, indications | CC BY-SA 3.0, internal use |
| ClinicalTrials.gov API v2 | trials by condition and intervention, phase, status | free with attribution |
| openFDA Drugs@FDA (keyless: 1,000 calls/day) | approvals per drug | CC0 |
| Orphadata API | prevalence class for rare diseases | CC BY 4.0 |
| UniProt REST, Human Protein Atlas | protein function, tissue expression | CC BY 4.0 |
| PubMed, Europe PMC, OpenAlex | co-mention counts (novelty), citations, references | public / CC0 |

**Excluded (D7):**
- DrugBank (CC BY-NC; downloads paused)
- IHME GBD (non-commercial license)
- OMIM (licensed)
- NORD (no API, scraping prohibited)
- DGIdb (mixes non-commercial sources)
- Pharos/TCRD (last changelog entry 2024-02-15; its program's funding has ended)
- Semantic Scholar (no key in production; API license restricts redistribution)

**Open Targets in bulk.** Its docs point systematic users to the data downloads or BigQuery.
The GraphQL rate stays paced, with results cached per (release, target) and per (release,
disease). If the backfill needs more than 20,000 Open Targets queries, the builder switches
target and tractability lookups to the release's downloadable files instead (§17).

## 7. The funnel

### 7.1 Items, statuses, epochs and cycles

- **Shared inputs.** These hold *inputs*, never judgments, and are shared by every epoch:
  - `landscape_documents`, one per source record after linking;
  - `landscape_texts`;
  - `landscape_evidence_cache`.
- **Epoch-scoped judgments.** Every row derived from an LLM judgment carries `epoch_id` and is
  unique within it:
  - S2 screens and S3 extractions;
  - ideas, with their evidence, links and aliases (an idea is derived from S3, so it exists
    per epoch; its *identity* across epochs is its `idea_key` string);
  - S4 triage, S5 cuts, S6 deep records and consults;
  - S7 comparisons and rankings, similarity judgments;
  - opportunity versions and S9 assessments (§7.11, X1).
- **Per-stage statuses.** Each LLM stage's status lives on that stage's epoch-scoped row (for
  example, a document's S2 status is its `landscape_screens` row for that epoch), never on the
  shared document row. The statuses are `pending`, `submitting`, `submitted`, `running`, `done`,
  `excluded:<reason>`, `rescued`, `refused`, `failed:<reason>`, held by a CHECK constraint.
  - S1 is the one exception. It is deterministic, not a judgment, so its result (`eligible` or
    `excluded:<reason>`) lives on the shared document row, with its own CHECK.
- **Batched stages:** S2, S3, S4, the S5 boundary tie-break, the S7 rebuild, S8 labels, S9 and
  the reject audit (D66).
  Items are gathered into Message Batches: up to 100,000 requests or 256 MB per batch, results
  usually within an hour and at most 24 hours.
- **Synchronous stages:** S6, S7 insertions, similarity judgments, and fallback re-runs.
- **The batch protocol** (D31, X4):
  1. A `landscape_batches` row (`submitting`, with a bigint `seq`) and its
     `landscape_batch_items` rows (`(batch_id, item_seq, item_ref)`) are committed before
     `batches.create`.
  2. Each request's `custom_id` is `b{seq}-{item_seq}`. That fits the API's grammar,
     `^[a-zA-Z0-9_-]{1,64}$` (verified 2026-10-30); a UUID pair with a colon would not.
  3. The returned batch id is then stored.
  4. **Reconciliation at startup.** At startup, every batch created since the oldest
     `submitting` row, other than those whose ids are already stored, is listed and paged once;
     that list is then fixed. Once each listed batch has ended, the `b{seq}-` prefixes of its
     results are matched to the `submitting` rows. The check does not block the loop: it
     repeats each loop over the fixed list until every listed batch has ended. A `create`
     that fails during the run re-arms the same check for its row, with a list fixed at that
     moment. The row counts its failed creates (`create_failures`). Its items become
     `failed:create`, terminal and reported, only after its third failed create and its last
     re-armed scan has found no matching batch, since a `create` that raised may still have
     made one.
     - A matched row adopts its batch, and its stage rows move to `submitted` with their batch
       items.
     - A row with no match after the scan is resubmitted.
     - Its items stay held in `submitting` until the row is matched or resubmitted, so nothing
       is billed twice.
  5. **Ingestion is idempotent** (F4). Each result updates its stage row with a compare-and-set:
     `UPDATE … SET status = <outcome>, … WHERE id = <row> AND status = 'submitted' AND
     batch_item_id = <this item>`. A chunk replayed after a crash changes nothing. Only
     `landscape_llm_calls` rows are inserted, with `ON CONFLICT DO NOTHING` on
     `(batch_id, item_seq)`.
     - Rows derived from a result (ideas, evidence, links, resubmissions, fallback work) are
       written in the same transaction, and only when the compare-and-set changed a row
       (N12).
     - A batch whose results are all ingested is marked `ingested` and is not read again.
- **Result types:**
  - `succeeded`: processed.
  - `refusal` (a succeeded result with `stop_reason: refusal`): goes down the D42 chain.
  - `errored`: resubmitted once, then `failed:errored`.
  - `expired`: resubmitted once, then `failed:expired`.
  - `canceled`: resubmitted.
  - `truncated` (a succeeded result with `stop_reason: max_tokens`): retried once with the
    stage's higher limit; a second truncation records `failed:truncated`, which is terminal
    (D32).
- **Cycles.**
  - An **intake cycle** runs daily: intake, then S1-S5 for the active epoch.
  - The cut (S5) runs once no S4 work of that cycle is pending, or at 06:00 UTC, whichever
    comes first.
  - A **batch cycle** is the week between review-batch issues (§13.1). S6-S9 re-runs for an
    already-assessed opportunity are throttled to once per batch cycle (D37).

### 7.2 S1 Filter (deterministic, at ingest)

Each document is marked `eligible` or `excluded:<reason>`:
- §5.2 qualification;
- §5.3 window;
- §5.3 type, retraction and language rules.

There is no topic filter (D5).

### 7.3 S2 Screen (batch; model per §10.1)

**Input:** what the document says about itself.
- Works: title + the stored summary: PubMed's abstract first, else OpenAlex's reconstructed
  abstract (§6.1; analysis F7).
- Grants: title + abstract + public health relevance.
- Patents: title + abstract + first claim.
- JHTV: title + description.

No names, institutions or journal.

**Output** (structured output, JSON schema):

```json
{"in_scope": "yes | no | unsure",
 "research_kind": ["target_discovery", "target_validation", "disease_mechanism",
                   "medicinal_chemistry", "biologic_engineering", "oligonucleotide",
                   "gene_or_cell_therapy", "repurposing", "clinical_study", "other"],
 "targets_mentioned": ["<as written>"], "diseases_mentioned": ["<as written>"],
 "modality_mentioned": "<as written or null>", "reason": "<= 200 chars"}
```

**Rule.** `yes` and `unsure` go to S3; `no` is `excluded:screen`. Recall comes first (analysis
F3).

**Prompt:** `prompts/landscaping/screen.md`, which carries §5.4 verbatim.

### 7.4 S3 Extract from summaries (batch) and evidence assembly

1. **Text.** S3 reads the summary text S2 read, which is stored with the document. Full text
   waits for S6 (L36).
2. **Extraction** (batch; model per §10.1; `prompts/landscaping/extract.md`). One document
   gives 0..n idea candidates:

   ```json
   {"ideas": [{
      "idea_type": "biology | chemistry",
      "targets": [{"as_written": "", "kind": "gene|protein|rna|complex|pathway_node|cell_population",
                   "species": "human|mouse|rat|other|unstated"}],
      "direction": "inhibit|activate|degrade|replace|edit|deliver_to|modulate|unknown",
      "agent": {"as_written": "", "kind": "small_molecule|antibody|peptide|oligonucleotide|gene_therapy|cell_therapy|degrader|vaccine|delivery_technology|other"},
      "disease": {"as_written": ""},
      "modality": {"as_written": "", "basis": "proposed|tested|none"},
      "stage": "association|target_validation|tool_or_hit|lead|preclinical_candidate|clinical|repurposing",
      "evidence_grade": "correlation|in_vitro|in_vivo|patient_material|human_genetics|clinical",
      "evidence_summary": "<= 200 chars",
      "mechanism_hypothesis": "<= 300 chars",
      "target_rubric": [{"item": 1, "status": "present|absent|unclear",
                         "grade": "<ladder value or null>",
                         "quote": "<verbatim, <= 300 chars>", "section": ""}],
      "key_results": [{"claim": "", "quote": "", "section": ""}],
      "model_systems": [""], "patient_population": "", "biomarkers": [""],
      "novelty_claims": [{"quote": "", "section": ""}],
      "limitations": [{"quote": "", "section": ""}],
      "unmet_need_statements": [{"quote": "", "section": ""}],
      "funding_and_ip_mentions": [""]}],
    "linked_publications": [{"doi": "", "pmid": "", "title": ""}]}
   ```

   - **Vocabularies and grade order.** The `direction`, `stage`, `evidence_grade` and
     `agent.kind` values, and the evidence-grade order (correlation < in vitro < in vivo <
     patient material < human genetics < clinical), come from the criteria file (§8.1).
   - **Which fields each type needs.** A chemistry idea needs `agent`; its `targets` and
     `disease` may be empty (a delivery technology has neither). A biology idea needs
     `targets` and `disease`; `agent` may be empty.
   - **Linked publications** (patents and JHTV listings only; D67). Each DOI, PMID or title
     must appear verbatim in the document's text; the quote check drops the rest. A remaining
     entry that resolves to a stored document (by DOI, else PMID, else an exact normalized
     title) becomes an epoch-scoped `landscape_extraction_links` row (§6.1).
   - **The rubric checklist.** `target_rubric` holds the 11 items of the rubric's two
     `evidence` lists, in file order: the 5 on `scientific_credibility`, then the 6 on
     `translational_path`. The prompt reads them from `load_rubric()`, never from a copy.
3. **Quote check** (deterministic).
   - Each quote must appear in the stored text after Unicode NFKC folding, whitespace collapse
     and case folding.
   - A failed quote is dropped and counted. A rubric item left without a verified quote
     becomes `unclear`.
   - S4 sees only verified quotes (anti-goal F13).
4. **Normalization** (deterministic; analysis F5; leave unmatched rather than guess).
   - **Targets → HGNC ids.**
     - First choice: the PubTator3 gene annotations of the same document.
     - Fallback: an HGNC symbol, alias or previous-symbol lookup, used only when the name maps
       to exactly one approved gene and is not on the criteria file's ambiguous-word list.
     - Anything else stays unmatched, with a provisional key.
     - Non-human gene ids map to the human ortholog through Open Targets `homologues` (field
       verified live 2026-10-30) or HGNC HCOP. Phase 0 picks the route.
   - **Diseases → Mondo ids.** PubTator3 MeSH annotation → Mondo SSSOM exact match; else an
     exact Mondo label or synonym match; else unmatched.
   - **Agents → ChEMBL ids**, when the name resolves to exactly one ChEMBL molecule; else
     unmatched.
5. **Database evidence and data flags**, per idea, cached per source release:
   - **Open Targets:**
     - association, overall and genetic;
     - tractability by modality;
     - `drugAndClinicalCandidates`;
     - `isEssential` and `depMapEssentiality`, `safetyLiabilities`, `geneticConstraint`;
     - for the card: `functionDescriptions`, `baselineExpression`, `subcellularLocations`,
       `chemicalProbes`.
     
     Every field named here was verified live on 2026-10-30.
   - **Other sources:**
     - ChEMBL mechanisms with max phase;
     - ClinicalTrials.gov counts by phase for condition × target or agent;
     - PubMed co-mention counts;
     - the Orphadata prevalence class.
   - **Data deal-breaker flags.** These are flags only and never exclude (L24). Each carries
     its evidence:
     - `target_essential`: Open Targets `isEssential`.
     - `safety_liabilities`: a non-empty `safetyLiabilities`.
     - `same_target_in_clinic`: a clinical-stage drug or candidate against the target for the
       idea's disease or a Mondo descendant, from `drugAndClinicalCandidates` and
       ClinicalTrials.gov.
   - **Failures.** Each source's lookup is tracked on the idea on its own (status and attempts
     per source). One that still fails after three attempts is marked `failed:<source>`, and
     assembly has ended once every source is done or failed. S4 then runs with it marked unavailable, so assembly never
     holds the funnel up, and an idea version counts as pending S4 only until it has a terminal
     triage row.
6. **Independent groups.** S3 counts the distinct qualifying JHU labs among the idea's
   documents. S6 adds outside groups (§7.7).
7. **Canonicalize and merge** (D9, L21, L22).
   - **Biology key:** (sorted HGNC ids, else provisional target keys; direction; Mondo id,
     else provisional disease key).
     - A candidate with `direction: unknown` joins the one idea with the same targets and
       disease and a known direction, when exactly one exists.
     - Otherwise it keeps its own key, with direction `unknown`.
     - The attachment is decided once (D27).
   - **Chemistry key:** (ChEMBL id, else the normalized agent name plus its senior lab's lab key
     (§5.2, L59); target ids if any; Mondo id if any). The lab key is the lab's canonical key
     at merge time; a later unification re-keys the idea through an alias (D64).
   - Ideas exist per epoch (§7.1). The key string is what links an idea across epochs.
   - **Aliases** (D36). When a provisional key later resolves to a normalized key that already
     exists, the provisional idea merges into the existing one. An alias row records the old
     key, every foreign key is re-pointed, and earlier results stay as history.
   - **Links.** A document that yields both types links its ideas (`landscape_idea_links`).
   - **Versions** (D37).
     - The idea version is a hash of its attached *qualifying idea-source documents* that are
       not tombstones (§6.1). A new
       qualifying document makes a new version, which goes back to S4.
     - A database-evidence refresh, or a new evidence-only document, updates the dossier but
       not the version.
     - Tombstoning a document never changes an idea's version; the next attachment's hash
       counts the kept row once (§6.1).
     - §7.6 says how the cut treats a version still waiting.
   - **Dossier cap** (D35). A dossier carries at most 20 documents: every grant, patent and
     JHTV listing first, then the most recent qualifying papers. The rest are listed by id.

### 7.5 S4 Triage (batch; model per §10.1; L12, L23, L24, D10, D20)

**Input:** the blinded dossier, built from a fixed template. It holds:
- idea type;
- targets with ids, direction, agent, disease with id;
- modality variants, stage, evidence grade, evidence summary, mechanism;
- verified quotes per Target Rubric item, and key results;
- model systems;
- the database evidence summary and the data flags;
- independent-group count, document kinds and dates.

It holds no author, lab, institution, journal or grant-PI names.

**System prompt:** the BlackbirdBot standards extract (§8.2) + the criteria file (§8.1) +
`prompts/landscaping/triage.md`. Chemistry and delivery ideas are judged with the platform
guidance the rubric already gives, which the extract carries.

**Output** (structured):
- `gating`: the three keys, each `met | not_met | unconfirmed`, with `gating_rationales` of at
  most 200 characters.
- `scores` for `differentiation_unmet_need`, `scientific_credibility` and `translational_path`
  (integers 1-5), with `dimension_rationales` of at most 200 characters.
- `target_rubric`: 11 × `present | absent | unclear`.
- `evidence_grade` and `stage`, confirmed or corrected.
- `llm_flags`, each `{raised: bool, reason: <= 200 chars}`:
  - `correlation_only`;
  - `model_unrepresentative_no_human_data`;
  - `key_molecule_owned_elsewhere`;
  - `literature_contradiction`.
- `red_flags`: at most 3.
- `confidence`: `High | Moderate | Speculative`.
- `in_therapeutic_scope`: boolean.
- `druggability_note`: at most 300 characters, modality-aware.

**Computed in code** (new function; `weighted_score` is not changed):
- `dimension_mean = Σ wᵢ·sᵢ / Σ wᵢ` over the three dimensions, with weights read from the
  rubric, currently 25/25/25.
- `checklist_score = 1 + 4 × (present items ÷ 11)`, which puts the checklist on the same 1-5
  scale.
- `triage_score = α · dimension_mean + (1 − α) · checklist_score`, with α = 0.8 read from the
  criteria file (`[triage] dimension_share`, L62).
- `evidence_count` is the number of `present` rubric items.
- The idea is excluded (`excluded:gate` / `excluded:scope`) when any gate is `not_met` or
  `in_therapeutic_scope` is false. `unconfirmed` passes. Flags never exclude (L24).

### 7.6 S5 Cut (deterministic, plus a batched boundary tie-break; L7, L23, L56, D10, D11, D34)

- **Pool:** within the active epoch, ideas that have at least one qualifying idea-source
  document in the window and an S4 result that is not excluded, or that was rescued (§10.5).
  - An idea whose newest version is still waiting for S4 is ranked on its previous version's
    S4 result in the same epoch. It does not drop out and come back.
  - An idea version excluded at S6 (`excluded:deep_gate`) leaves the pool from the next cut on,
    unless it was rescued (D11).
  - An idea left with no qualifying document leaves the pool.
- **Order:**
  1. `triage_score` ↓ (with the checklist blended in, L62)
  2. `evidence_count` ↓
  3. evidence grade ↓ (ladder order, §7.4)
  4. JHU independent groups ↓ (the S3 count only, never S6's outside count, so earlier cuts
     give no advantage)
  5. stage, earlier first (L56)
  6. distinct qualifying documents ↓
  7. newest qualifying document date ↓
  8. idea id ↑
- **Near-duplicates fold** (D34). In that order, an idea is folded under a higher-ranked idea
  when:
  - they share an idea type, the same targets (or agent) and direction, and their diseases are
    a Mondo ancestor and descendant; or
  - one target set contains the other, with the same direction and disease.
  
  A folded idea takes no slot and is listed under the idea that keeps it.
- **Size:** N = the number of unfolded ideas, K = ceil(0.01 · N).
- **The boundary** (audit M6, X2). If ranks K and K+1 are equal on keys 1-5, the whole class
  equal to them on keys 1-5 goes into one Message Batch of pairwise comparisons:
  - **Inputs:** the blinded S4 dossiers (`rerank.md`, basis "triage").
  - **Pairs:** for a class of ≤ 20, every pair, each asked in both orders. For a larger class,
    a balanced random 10-regular pairing (each idea meets exactly 10 others), in both orders
    (F8).
  - **Ordering:** members are ordered by win fraction. A pair whose two answers disagree counts
    as a tie. Ties fall to keys 6-8.
  - **Filling:** the first places in the class fill the remaining slots.
  - **No S6, and no wait on a live simulation run** (D33 governs only synchronous work). Until
    the batch ends, the previous cut stays current. The tie-class size is reported on every cut.
- **One cut in flight.** At most one cut per epoch is in flight. A cut that falls due while
  a boundary batch is pending is skipped until that batch completes.
- **A candidate's cut is frozen.** Once a candidate has a complete cut taken after its drain
  (§7.11 step 4), it takes no further cut until it activates, so its S6, S8 and S9 can finish.
  New documents still go through its S2-S4.
- **Snapshot.** Each cut stores N, K, the ordered ids, the folds and the epoch id.
- **Entry.** An idea entering the top K is queued for S6. An idea already in the cut whose
  version changed is re-queued for S6 at most once per batch cycle (D37).
- **Leaving.** An idea leaving the top K keeps its results but drops off the page's "Current"
  view.
- **No refill.** A slot lost to an S6 exclusion is not refilled in that cut.

### 7.7 S6 Deep analysis (Claude Opus 5, synchronous; D12, D28)

**Scope.** One per idea in the cut, plus each blind-batch probe idea (L64).

**Input:** the full dossier, which adds what S4 never saw:
- qualifying labs, with names, affiliations and OpenAlex or ORCID ids;
- every linked document, with grant periods and amounts, patents and JHTV listings;
- the linked idea of the other type, if any.

**Steps:**
1. **Full text** (L36).
   - Fetch per §6.2 for the idea's documents.
   - Re-run the S3 extraction on each full text, with the same prompt and schema, and run the
     quote check against the full text.
   - Full-text evidence is stored as S6 evidence. It feeds S6-S9 only and never makes a new
     idea version (D28).
2. **Literature search loop.** It uses the projects research tools: `search_pubmed`,
   `search_trials`, `search_prior_art_text` and `retrieve_abstract_text`
   (`src/services/project_search.py:84-207`).
   - At most 8 rounds.
   - A claim may cite only a source the session retrieved (`project_plan.source_key` /
     `resolve_source`).
   - The loop also counts outside groups, meaning non-JHU labs reporting the same finding, for
     the independent-group count (L23).
3. **Disease profile** (structured; L30, L31). Every field carries `support`: either a list of
   references (document ids, database records, retrieved sources) or `"estimate"`. The fields:
   - indication, with its Mondo id;
   - prevalence or incidence with geography: Orphadata's class for a rare disease; otherwise
     the LLM's figure with its citations, always tagged `estimate` (L31);
   - standard of care;
   - approved drugs for the indication (openFDA, Open Targets);
   - clinical pipeline, as counts by phase plus the nearest named programs;
   - competing targets;
   - unmet need.
4. **Target biology summary** for the card, from the Open Targets fields in §7.4 step 5, plus
   UniProt and HPA. Each line carries its source.
5. **Gates re-judged**, each with a rationale.
6. **Deal-breakers reviewed.** The data and LLM flags are confirmed or cleared, each with a
   reason. They are shown, never excluding (L24).
7. **Provisional six scores**, each with a rationale. `team_executability` and
   `venture_potential` are judged from the database record: grants, publications, patents and
   JHTV listings.
8. **Specialist panel.** Seven consults for a biology idea (`scientific`, `chemistry`,
   `clinical`, `commercial`, `legal`, `talent`, `budget`); a chemistry or delivery idea adds
   `technologic` (D12).
   - The persona is composed exactly as `src/agent/tools.py::_load_persona_text` composes it:
     the persona file at `specialists.persona_path(domain)`, with `{stage_bar}` replaced by
     `blackbird_rubric.render_stage_bar_markdown(domain)`. A parity test holds the two
     compositions equal.
   - Each consult gets the dossier, the S6 summary and a domain question.
   - Each call goes through the landscaping ledgered client, not `tools._call_specialist`.
     That function books into the engine's per-run call log, which a worker process does not
     have.
   - Opinions are parsed by `specialists.parse_opinion(raw, domain=...)`.

**Rule.** A `not_met` gate at S6 gives `excluded:deep_gate`, shown on the pipeline page with
its reason.

### 7.8 S7 Rerank (Claude Opus 5; D13)

S7 keeps one ranked list per epoch over the S6-passed ideas of the current cut.

**The comparator** (`prompts/landscaping/rerank.md`, basis "deep") is given two S6 summaries.
- It is asked in both orders.
- When the two answers agree, that idea wins.
- When they disagree, the higher S6 provisional `weighted_score` wins (computed by the existing
  function), then the lower idea id.

Comparisons are cached on (idea a, version a, idea b, version b, epoch, basis, order) and
reused across cuts.

**Rebuild by batch** (X3, F8). This runs when the list is empty, or when more than 25% of the
cut is new since the last rebuild:
- **The draw:** one Message Batch of a balanced sampled tournament. Each idea meets exactly 10
  others, through a random 10-regular pairing (or every other idea, when there are 11 or
  fewer), in both orders.
- **The order:** by win fraction, with ties broken by the S6 provisional `weighted_score`, then
  the idea id.
- **While a rebuild batch is in flight:**
  - insertions wait;
  - later cuts submit no second rebuild;
  - when it lands, ideas that left meanwhile are removed, and ideas that entered meanwhile are
    inserted one at a time.

**Incremental insertion** otherwise:
- New or changed ideas are inserted one at a time by binary search, under a per-epoch advisory
  lock, so two insertions never race for the same gap.
- Each binary search is sequential and synchronous (about log₂K questions). It waits while a
  simulation run is live (D33).

**The rest:**
- **Removal.** An idea that leaves the cut is removed without new calls.
- **A re-cut with unchanged ideas** makes no new calls.
- **S9 never waits for S7.** Cards use the S7 position when it exists, else the S5 rank (D30).
- **Stored:** every comparison, and the list per epoch.

### 7.9 S8 Opportunities (deterministic, plus Claude Opus 5 labels; L42, L57, L60, D14)

An opportunity is a group of S6-passed ideas from the current cut. It is the unit S9 assesses,
and reviewers see it as one card (L42).

**What joins ideas into one opportunity** (L57):
- the same lab (the lab key of §5.2) **and** a shared target or a shared disease (Mondo id); or
- a biology–chemistry link from one document (§7.4 step 7).

Opportunities are the connected components of those edges only.

**"Related" links** are shown on the card and the detail page but never merge cards:
- the same HGNC target or gene group (HGNC `gene_group`) across labs;
- the same Mondo disease or a shared direct parent;
- a shared source document;
- an LLM **similarity** judgment: one Opus 5 call per candidate pair that shares any facet
  above, asking whether the two pursue the same program and why (`similarity.md`). The verdict
  is cached per pair, version and epoch (D14). This covers the request's "similarity" axis.

**Labels.**
- An opportunity of size ≥ 2 gets one call (`cluster_label.md`). It returns:
  - a label of at most 60 characters;
  - a one-line theme;
  - `platform_signal`: whether a reusable platform runs across the members, and why.
- A single-idea opportunity is labelled by its structured line (D29), with no call.

**Identity across cycles and epochs** (L60, X1, m-L, F1).
- **Tables.** `landscape_opportunities` holds the stable identity.
  `landscape_opportunity_versions` holds each version: its epoch, its member `idea_key`s,
  label, theme and platform signal. Members are compared by `idea_key`, which is stable across
  epochs.
- **Who writes them.** Only the *active* epoch's S8 runs, and an epoch's activation, create,
  version or retire opportunities. The one exception is a blind-batch probe, which the probe
  draw creates (§10.6, D62).
  - A candidate epoch's S8 writes only *provisional groupings*, held in
    `landscape_candidate_groupings` with its epoch: member `idea_key`s, label, theme, platform
    signal and edge kinds. It touches no stable row.
  - The candidate's S9 assesses those groupings (§7.10, D59).
- **Matching** is one-to-one:
  1. List the pairs (new group, existing opportunity) that share at least half of the existing
     opportunity's members in its current active-epoch version. Keys are compared after
     resolving idea aliases from both epochs (D36, D64).
  2. Sort them by overlap ↓, then by the existing opportunity's age (older first), then by id.
  3. Assign greedily: each existing opportunity and each new group is used at most once.
- **In the active epoch's S8.** A matched group becomes a new version when its members or any
  member's version differ. An unmatched group is a new opportunity. An opportunity left
  unmatched is retired, with its history.
- **At activation.** In the same transaction that activates a candidate epoch, its provisional
  groupings are matched against the active epoch's current versions by the same rule. Only
  then are versions minted (tagged with the new epoch), new opportunities created and
  unmatched ones retired, and the assessment of each current grouping is re-pointed from that
  grouping to the version minted for it (D59). Assessments of groupings the candidate replaced
  before activation stay with them, as history. Decisions therefore survive activation (L60,
  D50).
- **The version counter** belongs to the opportunity and only ever moves forward. "Changed
  since your decision" compares the `landscape_assessment_id` decided on with the assessment
  the page shows, so a new version whose S9 has not run yet changes nothing (D37, D50).

**Blind probes** (L52, X5).
- An idea drawn from below the cut for a blind batch (§10.6) becomes a single-idea **probe
  opportunity**. It never joins S8 groups and never takes part in identity matching.
- Probes are flagged in the database only. No page shows which cards are probes, or the blind
  metric, until the next batch supersedes that blind batch (§10.6, F2).
- **Lifecycle** (D62). Before issue, a drawn probe is replaced by a fresh draw from the same ranks when its S6
  finds a gate not met, when its S6 or S9 ends `refused` or `failed:*`, when its idea enters
  the top K before the batch is issued, or when the epoch it was drawn in is retired before
  issue. The replaced probe opportunity is retired and never shown. An issued probe is never
  redrawn or retired; a failed assessment leaves it a card with no completed assessment
  (§7.10). Probes take no part in matching, so a candidate epoch re-runs S6 and S9 on every
  issued probe (§7.11).

### 7.10 S9 Final assessment (Claude Opus 5, batch; L41, L42, D15, D29)

**Scope.** One per opportunity version in the active epoch, probes included (§7.9). In a
candidate epoch, one per current provisional grouping (D59) and one per issued probe (D62).
They run in the order of the best member's S7 rank (S5 rank until S7 has placed it).

**Prompt:** the BlackbirdBot verdict-contract extract (§8.2) + the criteria file (§8.1) +
`prompts/landscaping/final.md`.

**The response** is the contract's JSON object, enforced as structured output:
- `company_or_project`: the opportunity label;
- `headline`;
- `key_points`: the six groups;
- `elevator_pitch`;
- `score_rationale`;
- `strengths`;
- `risks`;
- `competitive_landscape`;
- `evidence_maturity`;
- `gating` + `gating_rationales`;
- `scores` (all six) + `dimension_rationales`;
- `red_flags`;
- `recommendation`;
- `rationale`;
- `recommended_next_experiment`;
- `confidence`.

`subject_agent_id` is dropped: there is no lab agent.

**Landscaping additions** (outside the contract):
- `claim_support` (L30): for every key-point bullet, by group and index, either a list of
  references (member ideas' documents, quotes, database records, S6 sources) or `"estimate"`.
  The page renders it as a badge on each bullet.
- `member_notes`: one line per member idea, giving what it adds to the opportunity.

**Inputs.** Rank or position is deliberately not among them, so the verdict's prose can never
reveal rank in a blind batch (F2):
- every member idea's S6 record: dossier, full-text evidence, disease profile, target
  biology, flags, panel opinions;
- the opportunity's label, theme and platform signal.

**Computed:**
- `weighted_score` and `band` through `blackbird_rubric.weighted_score` / `band`, unchanged. The
  page shows the band with the rubric's own words: provisional thresholds, advisory band
  (analysis B1).
- **Structured lines** (L41, D29), one per member idea, composed in code from normalized
  fields:
  - biology: `"{Direction} {SYMBOL} · {disease label} · {modality or 'modality open'}"`;
  - chemistry: `"{agent label} ({agent kind}) · {target or 'no single target'} · {disease or 'no single disease'}"`.

**No completed assessment** (D62). A card whose opportunity has no completed S9 in the active
epoch (it ended `failed:*`, `failed:no_deep` included, or `refused`, for example right after an
activation) keeps its card
and its place in carry-overs. The card and the page say the assessment failed and why, and link
the last earlier assessment as history; the pipeline page lists the card.

**Validation.** It reuses `verdict_fields.sidecar_column_kwargs` and `sidecar._normalize_gating`.
An invalid object is retried once on the same model, then recorded `failed:invalid`.

**Re-assessment** (L60, D37). An opportunity is assessed again when its membership changes or
a member's S6 record changes, at most once per batch cycle. Earlier decisions and reviews stay
with the opportunity, marked "changed since your decision" (§13.3).

### 7.11 Judgment epochs and re-judging (L27, L28, X1)

**What an epoch is.** A judgment epoch is a set of judging inputs (D61):
- the rubric content hash;
- the BlackbirdBot extract hash (§8.2);
- the content hash of each judging prompt file (`screen.md`, `extract.md`, `triage.md`,
  `deep.md`, `rerank.md`, `similarity.md`, `cluster_label.md`, `final.md`), so a change marks
  exactly the stages that read that file;
- the content hash of the criteria file's judging tables: everything except `[meta]` (its
  version and changelog) and `[reason_tags]` (§8.1);
- the eight stage model settings: `landscape_screen_model`, `landscape_extract_model`,
  `landscape_triage_model`, `landscape_deep_model`, `landscape_rerank_model`,
  `landscape_similarity_model`, `landscape_label_model` and `landscape_final_model` (§9.1). The
  fallback chain, the audit model and the chat model are not epoch inputs.

`chat.md`, the reason tags and the criteria file's `[meta]` judge nothing, so editing them
creates no epoch. The set and
criteria versions are recorded on the epoch for people to read; they do not identify it.

Every epoch-scoped row (§7.1) stores its epoch id.

**When a candidate epoch appears.** Any change to those inputs, detected at worker start,
creates a *candidate* epoch.
1. **Regression.** The regression set runs against it (§10.4). If it fails, the candidate is
   held and the pipeline page shows why. An admin can override (L28). The first candidate's
   regression waits for the bake-off, which seeds the set; it does no S2 work until then.
2. **Re-judging.** Once it passes, or is overridden, the worker builds the candidate's rows:
   - every stage whose inputs changed re-runs, together with every stage downstream of it;
   - unchanged upstream stages are copied forward unchanged, since their results are still
     valid.
   
   The re-judge covers every in-window item, plus S6 and S9 for every issued probe (D62).
   Probes take no part in matching, so this keeps their cards assessed after activation.
3. **The active epoch keeps serving.**
   - It keeps running its own full intake cycle (S2-S9 for new documents), whether the
     candidate is re-judging or held.
   - Documents are shared inputs (§7.1), so both epochs see the same new documents.
   - The active epoch's rows, cuts, opportunity versions and page never change because of the
     candidate. A test pins this byte for byte.
4. **Activation** (D70). A candidate is ready when all of these hold:
   - **Drained.** In phase `continuous`, no S2, S3, S4 or evidence-assembly work is pending
     for any item it had when its re-judge began. The time is recorded on the epoch; for the
     candidate that is current when the backfill drains, the D69 flip records it, whatever
     was recorded before.
   - **Its post-drain cut.** It has a complete cut taken after that time, with no boundary
     batch pending (§7.6). That cut is frozen until activation (§7.6).
   - **S6 and S8 on that cut.** S6 has ended (`done`, `excluded:*`, `refused` or `failed:*`)
     for every idea in that cut, and S8 has grouped that cut, its label batch ended.
   - **S9.** S9 has ended (`done`, `failed:*` or `refused`) for every grouping of that cut and
     every issued probe. Once the first three hold, an issued probe whose candidate S6 ended
     terminally, or whose idea has no row in the candidate, is given the S9 status
     `failed:no_deep`.

   A ready candidate becomes active in one transaction, and the old epoch is retired (D59).
   No candidate activates in phase `pilot` or `backfill`.
5. **A second change** (D65). If the inputs change again while a candidate is unactivated,
   that candidate is retired unactivated, and a new candidate is built from the active epoch.
   While no epoch is active, the new candidate is built from the replaced candidate's
   unchanged stages instead, so a change during the backfill keeps its finished S2-S4 work.

**Pinned to the active epoch.** The page, the batches and the chat show the active epoch's
assessments only. Older and retired ones stay readable as history and are never "newest" by
accident (§13.2).

**Never mixed, with one exception.** A cut and a page never mix epochs. Inside one epoch, an
item served by a fallback model after a refusal (D42) is the only result from a different model.
Its `served_by_model` is stored, and its share is reported (m-D).

## 8. Prompts

### 8.1 The landscaping prompt set and criteria file (L25, L26)

`prompts/landscaping/` holds:
- one prompt per stage: `screen.md`, `extract.md`, `triage.md`, `deep.md`, `rerank.md`,
  `cluster_label.md`, `final.md`;
- `similarity.md` for the S8 "same program?" judgment (§7.9);
- `chat.md` for the chat (§13.5);
- `criteria.toml`, the landscaping criteria file (L25);
- `set.toml`, carrying `version` and the content hash of every other file in the directory.

**The criteria file** is versioned (`[meta].version`, with a changelog) and is the one written
place for landscaping's own judgement, read by S2, S3, S4, S6 and S9. It holds:
- the therapeutic-scope definition (§5.4);
- the idea types;
- the vocabularies for direction, stage, evidence grade and agent kind, and the grade order;
- the deal-breaker list, as flags with the data or LLM test behind each (§7.4, §7.5);
- the ambiguous gene-word list (§7.4);
- the reason tags for quick decisions. Claude drafts the starting list from the rubric
  dimensions and the deal-breakers, and the owner approves it before go-live (L49);
- the triage blend, `[triage] dimension_share = 0.8` (L62).

It holds no worked examples from Blackbird's past deals (L26). The rubric file stays the
source of the dimensions, gates, weights and bands, and is not edited (K2).

**Loading and stamping.**
- A process loads the set once at start and validates the criteria file; an invalid file
  stops the worker, as the rubric loader does.
- Every stored LLM output is stamped with its epoch (§7.11). The epoch records the set version
  and the criteria version, and is identified by the judging hashes (D61).
- The criteria file records the owner's approval of the reason tags (`[reason_tags]
  approved`). No review batch is issued while it is false (L49, §15). Since the tags are no
  epoch input, approving or editing them re-judges nothing.
- `set.toml` records `version` and the content hash of every other file in the directory,
  `criteria.toml` included. Editing a file means recording its new hash with a new `version`.
  A file that does not match its recorded hash is an unrecorded edit: the worker refuses to
  start, and the chat refuses to answer when the file is `chat.md`.

**Isolation.**
- The set is in no role's `prompt_files`, and the engine's prompt-drift check ignores it.
- `scripts/sync_prompt_set_docs.py` embeds `prompts/projects/` in its review doc, but it does
  not cover this set, and this design does not add it.

**A tripwire on every prompt file** (audit m2, m-I).
`tests/unit/test_doc_prompt_sync.py:102-130` scans every `prompts/**/*.md` against its
`_FORBIDDEN` list, case-insensitively. The list has 12 phrases (`:102-120`), among them "Baltimore", "phase
2", "private instructions" and "dm rules".
- No landscaping prompt or criteria text may contain any of them. A landscaping test applies
  the same list.
- The Baltimore scope lives in code (§5.1), not in prompt text.
- Clinical stages are written as "clinical-stage" or "Phase II".

### 8.2 Reuse of the BlackbirdBot prompt set (L11, D16)

At load, landscaping extracts the following **verbatim** from the frozen files:

| Extract | From | Used by |
|---|---|---|
| `## Opportunity Assessment Quality Standards` (Core Principles 1-8 and Confidence Labels), up to the `{rubric}` line | `prompts/roles/scout_hub/agent-system.md` | S4, S6, S9 |
| the rubric, rendered by `render_rubric_markdown()` in place of `{rubric}` | `blackbird_rubric.py` | S4, S6, S9 |
| the paragraphs beginning "**What this looks like in practice.**" and "Or: an institution has chemical matter" | `agent-system.md`, *Interview Conclusions* | S4, S6, S9 |
| `### Concluding with an Opportunity Assessment: the sidecar` through the end of the post-skeleton rules (verdict items 1-14, the skeleton, the gating and rationale rules) | `prompts/roles/scout_hub/phase4-thread-reply.md` | S9 |
| the specialist personas plus stage bars, as the consult path builds them | `prompts/specialists/*.md`, `render_stage_bar_markdown()` | S6 |

**The landscaping frames.** Each frame states precedence once:

> There is no interview, no PI and no lab agent here, and nothing is posted anywhere. Where
> the BlackbirdBot text refers to them, read it as the published record in the dossier. From
> Core Principle 6, keep its rule that `unconfirmed` is an honest, non-blocking record and that
> freedom-to-operate is never resolved by an empty search; its advice about asking a lab agent
> does not apply. From Core Principle 8, keep its definition of "PI credibility" as the record
> of executing comparable work; its rules about the agent's conduct do not apply. Emit only the
> requested JSON.

(Audit m3: dropping Principles 6 and 8 whole would lose rules S4 and the team score rely on.)

**Stamping.**
- Every output stores the scout_hub `role.toml` version and a sha256 of the extracted text.
- If an expected heading is missing, the worker refuses to start, as the rubric loader does
  (fail-fast).
- A test pins each heading and checks that every extract is non-empty.

## 9. Models, batching, refusals and cost

### 9.1 Stage models (D8)

New settings, each overridable from the environment:

| Setting | Stage | Initial value | Changes when |
|---|---|---|---|
| `landscape_screen_model` | S2 | `claude-sonnet-5` | the Phase 0 bake-off picks the cheapest passing model among `claude-haiku-5-5`, `claude-sonnet-5`, `claude-sonnet-5-5`; if none passes, the best of them (D43) |
| `landscape_extract_model` | S3 | `claude-sonnet-5` | the same rule, among the same three |
| `landscape_triage_model` | S4 | `claude-sonnet-5` | the same rule, among the same three |
| `landscape_deep_model`, `landscape_rerank_model`, `landscape_label_model`, `landscape_final_model` | S6-S9 (S6 includes the full-text re-extraction) | `claude-opus-5` | fixed (L17) |
| `landscape_similarity_model`, `landscape_audit_model` | S8 similarity; reject audit | `claude-opus-5` | fixed |
| `landscape_fallback_models` | any refused item | `["claude-opus-5", "claude-opus-4-8"]` | fixed chain (L61, D42) |
| `llm_landscape_chat_model` | chat | `claude-opus-5` | fixed (L17, D25) |

Sonnet 5 starts as the screening default because the refusals page does not list it among the
models that run classifiers. That is a reason to measure it, not proof (§10.1).

### 9.2 Calls

- **Structured outputs** (`output_config.format` with a JSON schema) on every stage. All the
  candidate models support it. They cannot be combined with citations, which are not used.
- **Batches API:** S2, S3, S4, the S5 boundary tie-break, the S7 rebuild, S8 labels, S9 and the
  reject audit. The tie-break and the rebuild run at effort `medium`. The audit is built by the
  audited stage's builder on `landscape_audit_model` (D66).
- **Synchronous:** S6 (tool loop and consults), S7 insertions and similarity judgments, and
  fallback re-runs.
- **One request builder per stage** (D32) serves the batch path, the synchronous path, the
  fallback re-runs and the bake-off. Each stage has its constants:
  - `thinking: {"type": "adaptive"}`;
  - `output_config.effort`: S2 `low`; S3 and S4 `medium`; S6 and S9 `high`; S7 and S8
    `medium`;
  - `max_tokens`, a named constant ≤ 21,333.
  
  A `max_tokens` stop is retried once with the stage's higher limit, still ≤ 21,333; a
  second one records `failed:truncated`, which is terminal. Batch bodies are dicts, which the existing ceiling test does not scan
  (it reads keyword arguments), so a landscaping test checks every stage constant against the
  ceiling (§12.6).
- **Never sent:** sampling parameters (removed in API version `2026-10-21`).
- **Untrusted text** (D44, m-A). It is fenced with `delimit(text, tag)` using tags already
  registered in `FENCE_TAGS` (`src/agent/prompt_safety.py:18-23`), so that file does not
  change:
  - summaries and abstracts: `publication_abstract`;
  - full-text sections: `methods_section`;
  - patents: `patent`;
  - trials: `trial_record`.
  
  Prompts carry the `SYNTHESIS_DATA_NOTICE` wording (`src/services/llm.py:944-948`), which
  names those tags.
- **Batch requests** are not bound by the SDK's non-streaming timeout. Their `max_tokens`
  constants still stay ≤ 21,333 for one rule across both paths, and Phase 0 measures S6 and
  S9 truncation (m-O).

### 9.3 Refusals

- A result with `stop_reason == "refusal"` records `stop_details.category`.
- It is re-run synchronously down the chain in `landscape_fallback_models` (D42, L61),
  skipping the model that refused. A Sonnet or Haiku refusal tries `claude-opus-5`, then
  `claude-opus-4-8`. An Opus 5 refusal tries `claude-opus-4-8`.
- The chain is client-side. Server-side fallback does not exist in batches, and a synchronous
  re-run names its model explicitly so the ledger records exactly what served.
- When the chain is exhausted, the item is `refused`, which is terminal and reported.
- `claude-opus-4-8` is priced in `llm_pricing.py` already ($5 / $25). Its retirement is "not
  sooner than 2027-05-28".
- Batch refusals carry no fallback credit, and a `bio` refusal is billed (K10). The ledger
  records both attempts.

### 9.4 Pricing

Add to `src/services/llm_pricing.py`:
- `claude-sonnet-5-5`: $2 in / $10 out; cache write $2.50 (5 m) and $4 (1 h); cache read
  $0.10.
- `claude-haiku-5-5`, two tiers chosen *per request* by its prompt length:
  - up to 100,000 tokens: $0.10 / $0.50, cache write $0.125 (5 m) and $0.20 (1 h), cache read
    $0.01;
  - above: $0.50 / $2.50, $0.625 / $1, cache read $0.05.
- A batch multiplier of 0.5 on all token classes.

Per-request pricing needs a new function. `cost_for_tokens` prices aggregates and stays
unchanged (D45). The module docstring line "No batch/fast-mode/inference_geo modifiers apply"
is updated, and pricing tests cover both tiers and the batch modifier (audit m15).

An unpriced model still returns `None`, never $0 (`llm_pricing.py:43-54`). Landscaping refuses
to submit work for an unpriced model.

### 9.5 Ledger, spend and kill switch (D19)

- **Ledger.** Every call writes one `landscape_llm_calls` row (§11): stage, model, batch id or
  synchronous, `custom_id`, token counts by class, cost, refusal category, the model that
  served it, and timing. This is the only cost record for landscaping, because
  `llm_call_logs` requires a simulation run (analysis C3).
- **Spend.** Shown per day and per stage on the pipeline page.
- **Kill switch.** `landscape_enabled=false` makes the worker finish in-flight HTTP calls,
  submit nothing new and poll existing batches only. An optional `landscape_daily_usd_cap` is
  unset, meaning no cap (L16). When set, the worker defers new submissions once the day's spend
  reaches it.

## 10. Evaluation (L9, L28, L29, L35)

### 10.1 Phase 0 bake-off

A stratified sample of the pilot documents (D68), by OpenAlex field, kind and year:
- 600 summaries for S2;
- 60 kept documents' summaries for S3;
- 60 merged ideas for S4.

Opus 5 is the reference on each.

| Stage | A candidate passes when |
|---|---|
| S2 | refusal rate ≤ 2%, **and** recall of the reference's `yes` + `unsure` ≥ 0.95, **and** precision ≥ 0.6 |
| S3 | after normalization, target, direction and disease agree with the reference on ≥ 90% of ideas, **and** quote verification ≥ 95% |
| S4 | all three gates agree on ≥ 90%, **and** each score is within ±1 on ≥ 90% |

**Choosing.**
- The cheapest passing model becomes the stage setting.
- If no Haiku or Sonnet candidate passes, the best-scoring one is used anyway and the
  shortfall is shown on the pipeline page (L16, D43).

**Refusals.** Refusal rates are reported before and after the fallback chain (D42). Items the
Opus 5 reference itself refused are left out of the agreement figures and counted separately
(audit M2).

**Regression seed.** The bake-off's items, with their Opus 5 reference outputs, seed the
regression set (§10.4). The bake-off uses the production request builders (D32).

### 10.2 Other Phase 0 measurements

- **Ingest completeness:** received ÷ source-reported total ≥ 0.99 per slice and source.
- **S4 repeatability:** 50 ideas scored twice by the chosen model. Report the share with
  identical gates and every score within ±1 (analysis B7).
- **Normalization rate:** the share of targets, diseases and agents with HGNC, Mondo and
  ChEMBL ids, and how many names were left unmatched as ambiguous.
- **Positive controls.** Staff name the papers behind Blackbird's past positive decisions, on
  the pipeline page, before the pilot run; the pilot report says so when the list is empty.
  Those inside the window are traced through S1-S5 and the stage that drops each is reported.
  The list is entered on the pipeline page and stored in the database, never in git. There is
  no back-test outside the window (L37).
- **Cost and throughput per stage**, projected for the backfill.

### 10.3 Running metrics

Shown on the pipeline page:
- counts per stage and status, per source and per institution (the qualifying Baltimore
  institution of each document);
- refusal rate per stage and per OpenAlex field;
- spend per day and stage, and **cost per useful opportunity**: total spend ÷ opportunities a
  reviewer chose to pursue or approved (L32);
- per-source completeness;
- quote-verification and normalization rates;
- reject-audit disagreement (§10.5);
- regression results per epoch (§10.4);
- blind-comparison and expert-agreement results (§10.6).

### 10.4 The regression set (L28, X9)

**Contents.** The set is versioned. A candidate epoch is judged on the set version current when
it was created; a candidate created before any version exists is judged on the first one, the
bake-off's.
- **S2-S4 items:** the Phase 0 bake-off items with their reference outputs.
- **S6-S9 items:** 20 opportunities from the active epoch's latest cut, frozen with their stored
  S6 and S9 outputs.
- **Positive controls.**
- **Rescued items** (§10.5).
- **Expert-judged items** (§10.6).

Additions create a new set version, so "fixed" means fixed per version.
- **Baseline refresh** (D65). After each activation, a new set version freezes the new active
  epoch's S6-S9 items and its comparison and similarity pairs, so later candidates are measured
  against what is live.
- **The first epoch** has no active epoch to compare with, so only its S2-S4 items are checked,
  against the bake-off references, once the bake-off has seeded the set. When no Haiku or Sonnet
  candidate passed the bake-off (D43), this check fails the same way, and an admin override with
  a reason lets the pilot go on (L28).

**When it runs:** on every candidate epoch, for the stages the change touches.

**Pass rule.**

| Stage | The candidate passes when |
|---|---|
| S2-S4 | the §10.1 thresholds, against the stored references |
| S6 | all three gates agree on ≥ 90% of S6 items, and each provisional score is within ±1 on ≥ 90% |
| S7 (also the S5 tie-break) | on 30 frozen comparison pairs from the active epoch, the winner agrees on ≥ 80% |
| S8 | on 30 frozen similarity pairs, the verdict agrees on ≥ 85%. Labels are wording only and are not scored. |
| S9 | the recommendation agrees on ≥ 80%, gates agree on ≥ 90%, and each score is within ±1 on ≥ 90% |
| expert-labelled items | the candidate agrees with the expert at least as often as the active epoch does |

The S6, S7, S8 and S9 comparisons are made against the active epoch's stored outputs (F9).

**Failing.** A failing candidate is held. An admin can override it from the pipeline page; the
override is recorded with the admin and a reason.

### 10.5 Reject audit and rescue (L29, L43, L44)

**The audit.** Each cycle, as one Message Batch (D66), `landscape_audit_model` (Opus 5)
re-judges a random sample of that
cycle's S2 `no` documents and S4 exclusions. The size is set by `landscape_reject_audit_size`,
default 100 per stage.
- The disagreement rate is reported per stage and source.
- Every disagreement is listed on the pipeline page.

**The staff view.** The pipeline page lists filtered items with their stage and reason.

**Rescue.** Staff (admins and managers) may rescue an item. Its status becomes `rescued` with a
compare-and-set update (`UPDATE … WHERE status = <the status seen>`), so two staff cannot double
it. It then competes normally from the next stage; it gets no slot.

| Rescued at | What happens |
|---|---|
| S2 | the document goes to S3 |
| S4 | the idea joins the S5 pool |
| S6 (`excluded:deep_gate`) | the idea joins S7 and S8 while it stays in the cut |

**Across epochs.** Rescues carry forward: in a later epoch, an item excluded again at the same
stage is rescued again automatically, recorded as "carried". Every rescue is recorded with the
actor and a reason, and joins the regression set.

### 10.6 Blind comparison and expert agreement (L35, L52-L55, X5)

**Blind comparison.**
- **Cadence:** every fourth weekly batch is blind (L54).
- **Mix:** a blind batch holds 8 cards from the normal selection plus 2 probe opportunities
  (§7.9). The probes come from ideas drawn at random from ranks K+1 to 5K (L52), and run
  through S6, full text included (L64), and S9 like any other. They are drawn one batch
  cycle ahead, so their S6 and S9 are ready by issue. If either is not ready at issue, that
  week's batch is issued as a normal batch and the next one is blind instead. Their lifecycle
  is in §7.9 (D62).
- **Order:** the batch is shuffled, not shown in rank order.
- **Hidden** (L53, F2), for every card of a blind batch, on the card, the detail page and in
  the chat record:
  - score, band, rank, dimension scores and the provisional scores;
  - `score_rationale` and `dimension_rationales`, which explain the band and the scores.
  
  The read model enforces this. Blinding ends when the viewer has made their quick decision on
  that card.
- **No side channel** (F2).
  - While a viewer has undecided cards in a blind batch, their **Current** and **All assessed**
    views leave out every card of that batch.
  - No page shows probe flags or the blind metric until the next batch supersedes the blind
    batch (§7.9).
- **Metric:** the pursue rate of in-cut cards against probes. The ranking works only if in-cut
  cards are pursued clearly more often.

**Expert agreement** (L55).
- **Who:** staff mark expert accounts on the pipeline page. The marks are stored in
  `app_settings` under `landscape_experts` (K3).
- **Sample:** each quarter, the worker draws 10 opportunities per expert at random from
  assessed and probe cards on which the expert has no quick decision or review (D63).
- **Form:** the expert records their own gate calls, dimension scores, deal-breaker calls and
  recommendation, using the full review form plus a deal-breaker section. From the draw
  until they submit, every surface (card, lists, detail page, chat record) hides S9's and S6's
  calls on those items from that expert (gates, scores, provisional scores, deal-breaker
  confirmations, panel opinions, rationales, band and recommendation), so their calls are not
  anchored on the pipeline's (D63). In the chat record that means the Verdict and Panel
  documents, and any S6 call in the Disease profile and Evidence documents. On an item that is
  also a blind-batch card, both hidings apply until both lift. An expert can open their own sample items even when those were never
  issued; they find them on `/workspace/landscape` and submit through `/reviews` (§13.1,
  §13.3).
- **Comparison:** item by item with S9, with the disagreements listed. Items that were in a
  batch issued before the draw, and items judged by manager-experts (who see every current
  card), are reported separately, because the expert may have seen S9's calls.

**Measurement only.** Both measure the funnel and never feed it (L9, L33). Expert judgments also
join the regression set (§10.4).

## 11. Data model

All tables are new, prefixed `landscape_`, and additive (K3). The builder fixes column types;
the uniqueness rules below are part of the design.

### 11.1 Tables

**Shared inputs.** These carry no epoch (§7.1).

| Table | One row per | Unique on | Notes |
|---|---|---|---|
| `landscape_sources` | source | `source` | watermarks, last success, last completeness ratio, enabled flag |
| `landscape_fetches` | paged fetch slice | `(source, slice_key, started_at)` | expected vs received counts, status, error; the completeness record (G1) |
| `landscape_documents` | linked source record | `(kind, canonical_external_id)` | `merged_into` (tombstone, F10); kind (work/preprint/grant/patent/jhtv/dataset); ids; title; summary text and its source; dates; type; language; license; OA flags; qualifying authorships (JSON); qualifying institution; `role_basis`; lab key; S1 status and reason |
| `landscape_document_links` | pair | `(doc_a, doc_b, link_kind)` | same-work / preprint-of / grant-cites / dataset-of |
| `landscape_texts` | fetched full text | `document_id` | gzip text, sections index, `text_basis` (`full_text`/`summary`/`fetch_failed`), fetch attempts, license, route, truncation flag |
| `landscape_labs` | lab key | `lab_key` | display name, ORCID, OpenAlex author id, RePORTER profile id, aliases (§5.2) |
| `landscape_evidence_cache` | external lookup | `(source, release, query_key)` | JSON payload, fetched_at |

**Epoch-scoped judgments.** Each carries `epoch_id` in its uniqueness key.

| Table | One row per | Unique on | Notes |
|---|---|---|---|
| `landscape_epochs` | judgment epoch | `id`; one `active` at a time | component versions and hashes, status (candidate/held/active/retired), regression run, activation time, override actor and reason |
| `landscape_screens` | S2 result | `(document_id, epoch_id)` | status, decision, kinds, mentions, reason, refusal, `served_by_model` |
| `landscape_extractions` | S3 or S6 extraction | `(document_id, epoch_id, basis)` | `basis` is `summary` (S3) or `full_text` (S6); status; raw JSON; quote-verification counts; refusal or failure state |
| `landscape_ideas` | idea in an epoch | `(epoch_id, idea_key)` | type; targets, direction, agent, disease (ids and labels); normalized flags; modality variants; stage; grade; JHU and outside group counts; data flags; version hash; current-cut flag (stage statuses live on the stage rows, §7.1) |
| `landscape_idea_links` | linked ideas from one document | `(epoch_id, idea_a, idea_b)` | biology ↔ chemistry |
| `landscape_extraction_links` | LLM-named document link | `(epoch_id, doc_a, doc_b)` | patent or JHTV listing ↔ paper, named by an S3 extraction (§6.1, F1) |
| `landscape_candidate_groupings` | provisional S8 grouping of a candidate epoch | `id`; `(epoch_id, grouping_key)` | `grouping_key`, the sha256 of its sorted (`idea_key`, idea version) pairs; member `idea_key`s, label, theme, platform signal, edge kinds; a current flag, since a later S8 run replaces groupings; matched at activation (§7.9, F1, D59) |
| `landscape_idea_evidence` | idea × extraction candidate | `(idea_id, extraction_id, candidate_index)` | which candidate fed the idea; merge reason |
| `landscape_idea_aliases` | resolved provisional key | `(epoch_id, old_key)` | the idea it merged into (D36) |
| `landscape_triage` | S4 result | `(idea_id, idea_version, epoch_id)` | gating, scores, rationales, checklist, grade, stage, LLM flags, `triage_score`, `evidence_count`, exclusion, `served_by_model` |
| `landscape_cuts` | S5 run | `id`, with `epoch_id` | N, K, ordered idea ids, folds, tie-class size, boundary batch, created_at |
| `landscape_deep` | S6 result | `(idea_id, idea_version, epoch_id)` | full-text evidence ref, disease profile with support tags, target biology, gates, flags reviewed, provisional scores, search transcript ids |
| `landscape_consults` | S6 specialist consult | `(deep_id, domain)` | parsed opinion and raw text |
| `landscape_comparisons` | pairwise call | `(idea_a, version_a, idea_b, version_b, epoch_id, basis, order)` | winner, reason (§7.6, §7.8) |
| `landscape_rankings` | S7 position | `(epoch_id, idea_id)` | position, inserted_at |
| `landscape_similarity` | similarity judgment | `(idea_a, version_a, idea_b, version_b, epoch_id)` | verdict, reason |
| `landscape_opportunity_versions` | opportunity version | `(opportunity_id, version)`, with `epoch_id` | member `idea_key`s, label, theme, platform signal, edge kinds, probe flag |
| `landscape_assessments` | S9 verdict | `(opportunity_id, opportunity_version, epoch_id)`; a candidate's grouping assessment is keyed `(epoch_id, grouping_id)` until activation re-points it (D59), and a candidate's probe assessment uses the probe's own key | every contract field as its own column (mirroring `opportunity_assessments`' sidecar columns), `claim_support`, `member_notes`, structured lines, computed `weighted_score`/`band`, `served_by_model`, rank |

**Stage columns** (§7.1, N3). Every row a stage result fills carries that stage's `status`.
Rows filled from a Message Batch also carry `batch_item_id`, which the compare-and-set matches:
screens, extractions (S3), triage, comparisons (the S5 tie-break and the S7 rebuild),
opportunity versions and candidate groupings (their S8 label), assessments, and reject audits.
`landscape_deep` carries a `status` too.

**Stable identities and review.** These carry no epoch.

| Table | One row per | Unique on | Notes |
|---|---|---|---|
| `landscape_opportunities` | stable opportunity | `id` | created, retired (§7.9) |
| `landscape_decisions` | quick decision (L39) | `(opportunity_id, reviewer_id)` | `pursue`/`pass`, reason tags, reason ≤ 1,000 characters, the `landscape_assessment_id` decided on, edited, recorded_by |
| `landscape_reviews` | optional full review | `(opportunity_id, reviewer_id)` | score 1-5, comment, dimension scores, rubric stamp, the `landscape_assessment_id` reviewed, edited, recorded_by |
| `landscape_review_events` | approve/disapprove/clear | append-only | opportunity, assessment, actor |
| `landscape_review_assignments` | assignment | `(opportunity_id, assignee_id)` | |
| `landscape_review_batches`, `landscape_review_batch_items` | weekly batch; card in it | `issued_on`; `(batch_id, opportunity_id)` | blind flag; per card, the probe flag and its position (a blind batch's shuffled order) |
| `landscape_expert_judgments` | expert sample item | `(opportunity_id, expert_id, quarter)` | gates, scores, deal-breaker calls, recommendation (L35) |

**Audits, cost and reports:**

| Table | One row per | Unique on | Notes |
|---|---|---|---|
| `landscape_reject_audits` | audited reject | `(item_kind, item_id, epoch_id)` | original and audit decision, agree flag, status (D66) |
| `landscape_rescues` | rescue | `id` | item, stage, epoch, actor, reason, carried flag |
| `landscape_regression_sets`, `landscape_regression_items`, `landscape_regression_runs` | set version; item; run | `version`; `(set_version, id)`; `(epoch_id, started_at)` | expected outputs; per-stage results, pass or fail |
| `landscape_batches`, `landscape_batch_items` | Message Batch; request in it | `seq` (bigint) and `batch_id`; `id` and `(batch_id, item_seq)` | stage, model, status (`submitting`, `submitted`, `ended`, `ingested`), timestamps; the item's ref (D31) |
| `landscape_llm_calls` | LLM call | `id` (plus `(batch_id, item_seq)` when batched) | ledger (§9.5) |
| `landscape_reports` | generated report (bake-off and its requests, probes, pilot, completeness, drift, positive controls, metrics) | `(kind, created_at)` | JSON |

### 11.2 Retention

- Raw source JSON is not stored. Normalized fields and the summary text are.
- `landscape_texts` (full text) exists only for documents behind ideas that reached S6. It is
  kept (D40): G6 (reproducibility) and the traceable-quote anti-goal need it, and the total is
  small (§11.3).
- Evidence cache rows expire when a newer release of that source is seen.
- Rows from retired epochs stay as history and are never deleted automatically.

### 11.3 Size estimate

Every figure here is to be measured in Phase 0.
- Documents: about 100k rows over the window.
- Texts: full text only for S6 documents. That is a few hundred to a few thousand documents
  × about 40 KB, gzipped to about 10-15 KB, so tens of MB.
- Extractions and ideas: tens of MB.

Well inside K11.

## 12. Ingest worker and operations (L18)

### 12.1 Why not the existing job queue (D23)

The existing worker's queue cannot carry this work. All of the following are verified at
`0c035b43`:
- `claim_job` claims any pending row whatever its type (`src/worker/main.py:94-106`).
- `tests/unit/test_job_handlers.py:9-10` requires the existing worker to hold a handler for
  every `job_type_enum` value.
- The boot sweep requeues *every* `processing` row (`main.py:552-556`).
- The stale sweep kills rows processing for more than 30 minutes (`main.py:133`), while a
  Message Batch can take 24 hours.

So landscaping keeps its work state in its own tables: the per-item stage statuses (§7.1),
`landscape_batches` and `landscape_fetches`. It claims and sweeps only those tables, and the
existing `jobs` table and `job_type_enum` are untouched.

### 12.2 The process

**Entry point:** `python -m src.landscaping.worker`. It handles SIGTERM/SIGINT the way
`src/worker/main.py` does.

**At start:**
- It holds `LANDSCAPE_WORKER_LOCK_KEY = fixed_key("landscape_worker")`
  (`src/services/advisory_locks.py`), taken with `pg_try_advisory_lock` on a dedicated
  AUTOCOMMIT connection. A second instance exits before touching anything.
- The new key joins the distinctness list in `tests/unit/test_advisory_locks.py`.
- It gets its own `EngineRole` in `src/database.py`, with an assertion in
  `tests/unit/test_make_engine.py`.
- It loads and validates the rubric, the BlackbirdBot extracts (§8.2) and the landscaping
  prompt set, and fails fast on any error.

**Each loop**, while `landscape_enabled` is true and the database pause flag is off:
1. Run due source fetches. Each source has its own `next_due_at` in `landscape_sources`.
2. Submit batches for each batched stage. A stage submits when it has at least
   `landscape_batch_min_items` pending items, or when its oldest pending item has waited
   longer than `landscape_batch_max_wait_minutes`.
3. Poll submitted batches every `landscape_batch_poll_seconds` and ingest their results.
4. Run synchronous work (S6, S7, similarity judgments, fallback re-runs) with at most
   `landscape_sync_concurrency` calls in flight.
   - While a simulation run is live, synchronous Opus work waits (D33). "Live" means
     `src.services.simulation_control.engine_alive(db)` is true: the engine advisory lock is
     held. That is the authoritative signal; a stale heartbeat with the lock held is still a
     live run.
   - Every client backs off on HTTP 429.
5. Run the S5 cut, S8, the weekly batch issue (§13.1) and the reports when they are due.
6. Handle epochs (§7.11). Detect a candidate epoch, run its regression set, re-judge under it,
   and activate it when its S9 is complete.
7. Run the reject audit (§10.5) once per cycle. Run S6 and S9 (not S7 or S8) on the
   below-cut ideas drawn as probes for the next blind batch (§10.6).

**Transactions.** Every unit of work commits on its own: one fetch page, one batch's results
in chunks, one idea's S6. A failure never rolls back finished work (analysis D2).

**On restart:**
- Reconciliation of batches left `submitting` starts first and never blocks the loop (D31,
  §7.1).
- Items left `submitted` in a live batch stay as they are; the batch id is durable.
- Items left `running` in a synchronous stage, with no live call (older than
  `landscape_running_stale_minutes`), return to `pending`. Only landscaping tables are
  touched.

### 12.3 Settings

All settings are new, in `src/config.py`.
- Model names (§9.1) and `landscape_enabled` (default false).
- `landscape_daily_usd_cap` (default unset).
- `landscape_ncbi_max_rps` (default 3, D33).
- `llm_landscape_chat_model` (default `claude-opus-5`).
- `landscape_chat_daily_usd_limit`, the sub-cap (D47), a static default of `50.0`: half of
  `assessment_chat_daily_total_usd_limit`'s default of 100 (`src/config.py:398`). It joins the
  finite guard tuple.
- `landscape_similarity_model`, `landscape_audit_model` (§9.1).
- `landscape_batch_min_items`, `landscape_batch_max_wait_minutes`,
  `landscape_batch_poll_seconds`.
- `landscape_sync_concurrency`, `landscape_running_stale_minutes`.
- `landscape_reject_audit_size` (default 100, §10.5).
- Per-source pacing.
- `landscape_window_days` (default 1096).
- `landscape_cut_fraction` (default 0.01, L7).
- `landscape_chat_enabled` (default false, §13.5).

Rules:
- Model-name fields join `NON_SECRET_STR_FIELDS`
  (`tests/unit/test_config_secret_redaction.py`).
- The optional cap gets its own guard, which accepts `None` and rejects non-finite or
  non-positive values. Putting it into the non-finite guard tuple (`src/config.py:702-711`)
  would call `math.isfinite(None)` and stop every service at startup (audit m4). The chat
  sub-cap joins that tuple, because it always has a value.
- Four values are database state in `app_settings`, so the pipeline page can change them
  without recreating a container:
  - the pause flag (`landscape_paused`);
  - the phase (`landscape_phase`, §15): set to `backfill` by the admin's Start backfill, and to
    `continuous` by the worker (D69);
  - the positive-control identifiers (`landscape_positive_controls`, §10.2);
  - the expert accounts (`landscape_experts`, §10.6).

### 12.4 Completeness and failure reporting

- Every fetch slice writes `landscape_fetches` with the source-reported total and the rows
  received.
- A ratio under 0.99 marks the slice `incomplete` and retries it with backoff. After the
  retries it stays `incomplete` and shows on the pipeline page. Nothing downstream is
  blocked.
- Exceptions follow the repo's rules:
  - stored error text goes through `job_error_text` (`tests/unit/test_last_error_writes.py`);
  - keys are read only through `openalex_auth` and `pubmed`;
  - model JSON is parsed only through `json_extract.extract_json`;
  - untrusted text is wrapped in `delimit` with a registered tag (§9.2).

### 12.5 Compose and deploy (K5, D22)

**The compose service.** It goes in the working-tree `docker-compose.prod.yml`, the file the
RCA plan's D5 keeps uncommitted (root `CLAUDE.md`). That is how the worker's prompts mount and the agent's supervisor command
were added. Its definition:
- Name and image: service `landscape-worker`, `build: .`.
- Start: `profiles: [landscape]`, so a plain `up -d` never starts it.
- `container_name: copi-blackbird-landscape-1`, `restart: unless-stopped`.
- `stop_grace_period: 330s`; the client read timeout is 300 s.
- `command: ["python","-m","src.landscaping.worker"]`.
- `env_file: .env`, plus the worker's `DATABASE_URL`/`SECRET_KEY`/`ENVIRONMENT` block.
- Volumes: `./prompts:/app/prompts:ro`.
- Network: `default` only.
- Logging: json-file, 10m × 5.
- A memory limit, set from the Phase 0 measurement.

The D5 box in `docs/operations/host-and-simulation.md` gains one line naming the service. A
new operations doc, `docs/operations/landscaping.md`, holds the runbook. Root `CLAUDE.md` is
one line under its limit (`tests/unit/test_claude_md_references.py:219` fails at 200 lines),
so its edits are net-zero: its "Before you…" table gains a row for the runbook, its D5 bullet
names the service without a new line, one existing bullet is reflowed to make room, and its
"(head is `0076` since the project chat)" note becomes "(head is `0078` since the landscaping
chat)" in place.

**Deploy.**
1. Commit; the porcelain check prints nothing.
2. `$DC build blackbird-app worker`, then `$DC --profile landscape build landscape-worker`.
3. Migrate from a one-off container off the new image (`./scripts/migrate/run_migration.sh`,
   rehearsed first).
4. `$DC up -d blackbird-app worker`, then `$DC --profile landscape up -d landscape-worker`.

**The agent** (D45). The design does change modules the agent imports:
- `src/config.py`, `src/database.py`;
- `src/services/llm.py`, `llm_pricing.py`, `advisory_locks.py`, `pubmed.py`, `openalex.py`;
- `industry_sources/openalex_industry.py`;
- the chat service.

Every such change is additive: new functions, settings and constants, with existing
signatures, outputs and defaults unchanged. `tests/characterization/` and the profile and
industry tests must pass unchanged to prove it. So the running agent needs no rebuild for
landscaping to work, and its next ordinary rebuild picks the additions up. Per root
`CLAUDE.md`, that rebuild is still recreated only when `/workspace/simulation` shows no live
run.

Never `--remove-orphans`, never `up -d --build`.

**Migrations** are `0077_landscape` (tables) and `0078_landscape_chat` (§13.5). Both are
additive. With both:
- `scripts/migrate/preflight.py` moves its pins: `DEFAULT_TARGET`, `REVISION_ORDER`,
  `SUPPORTED_START_REVISIONS`, `PLANNED_OBJECTS`. `PLANNED_OBJECTS` gets every new table,
  column, foreign key, index, unique constraint and CHECK. `0078`'s three redefined CHECKs go
  into neither
  `PLANNED_OBJECTS` (they recreate existing constraints, which the collision check must not
  list, as `preflight.py:114` notes for `0065`) nor `PLANNED_DROPS`, which is tables-only
  (`test_migration_checks.py:959-970`, F7).
- `scripts/migrate/postflight.py` adds `0077` and `0078` to `VERIFIED_REVISIONS`, as the test's
  docstring asks of a later migration (`test_migration_checks.py:1312-1318`), with
  `EXPECTED_TABLES` (`postflight.py:264`; every 0077 table, `test_migration_checks.py:1338-1343`),
  `EXPECTED_COLUMNS`, `EXPECTED_INDEXES` and `EXPECTED_CONSTRAINTS` entries for every object of
  both, `0078`'s three new check texts included. The pin at
  `tests/unit/test_migration_checks.py:2207-2208` moves. The two `_subject` expectations
  (`postflight.py:452-453`, matched as substrings at `:739`) change to `'landscape'`, so 0078 is
  verified, and their equality pins at `:2216-2217` move with them (m-H, N11).
- The head pins in `tests/unit/test_migration_checks.py` and
  `tests/integration/test_harness_smoke.py` move.
- `docs/operations/migration-deploy-notes.md` gains a deploy-order box per revision.

**Schema and code order.**
- Old code on the new schema ignores the new tables. `0078` widens the shared chat tables;
  old chat code, which names its columns, keeps working.
- New code on the old schema breaks every chat request, assessment chat included, as `0076`'s
  docstring records for its own change. So both migrations are applied before the new images
  serve, which the deploy order above already does.
- The landscaping pages and worker refuse to start against a schema without `0077`.

### 12.6 CI tripwires the build must satisfy

Each item names the test that enforces it.
- New tables, columns and checks:
  - JSONB columns use `none_as_null=True` (`test_json_none_as_null.py`);
  - no `simulation_run_id` (`test_run_scoped_tables.py`);
  - string + CHECK instead of new Postgres enums (repo pattern).
- No literal `max_tokens` keyword above 21,333 anywhere in `src/`
  (`test_llm_nonstreaming_ceiling.py`). That test reads keyword arguments only, so batch
  bodies (dicts) are covered by a landscaping test on every stage constant (D32).
- `test_llm_private_imports.py` bans `_acreate` and `_all_text` imports. Landscaping uses
  `acreate`/`abeta_create` for synchronous calls and a new, tested Batches wrapper next to
  them.
- No landscaping prompt contains "Baltimore" or "phase 2"
  (`test_doc_prompt_sync.py:102-130`).
- `delimit` is used only with tags already in `FENCE_TAGS` (`publication_abstract`,
  `methods_section`, `patent`, `trial_record`; the S6 research tools fence with
  `pubmed_record` themselves), so `FENCE_TAGS` is unchanged (`test_prompt_safety.py`; D44).
- Pricing tests cover new models and the batch modifier (`test_llm_pricing.py`).
- Code style and structure:
  - every function ≤ 200 lines and complexity ≤ 20;
  - no new ruff findings under `src/` (ceiling 231);
  - no dead symbols;
  - every route and template reachable (`test_reachability.py`).
- No redefinition of corpus matcher names (`test_matcher_is_not_duplicated.py`). No
  `slack_sdk`.
- Coverage stays ≥ 60%. The code runs on Python 3.11 (images) and 3.12 (`.venv-test`).
- The Batches API exists in both SDKs: `anthropic` 0.120.2 in `.venv-test` and the 1.9.0
  wheel the images install (`resources/messages/batches.py` in both, verified 2026-10-30).
- The repository's source-scanning tests are many: 44 test files use `rglob`, 175 use
  `read_text` and 34 use `inspect.getsource` (audit m2 count). The build runs the whole suite
  through `./scripts/ci.sh`, not a hand-picked subset.

## 13. Web UI (L1, L13, D18)

### 13.1 Pages

All pages are GET-only on the workspace router; no POST goes there
(`test_workspace_permissions_matrix.py`). Writes go to `/reviews` (§13.3) and to a new
`/landscape` router (§13.4).

| Path | Who | What |
|---|---|---|
| `/workspace/landscape` | admin, manager, reviewer (`get_review_user`) | **This week's batch** and the viewer's own **carried-over** cards. Every reviewer and manager also sees **earlier batches**. Managers and admins also get **Current** (every opportunity in the active epoch's cut) and **All assessed**, with Unreviewed / Reviewed / All tabs as on Assessments (L48), defined per viewer: *Unreviewed* means the viewer has no quick decision on the card, *Reviewed* that they have one. No tab or count reflects anyone else's activity (L50, D60). |
| `/workspace/landscape/{opportunity_id}` | admin, manager; a reviewer only for an opportunity in an issued batch or in their own expert sample (404 otherwise, X7, D63) | The detail page (§13.2): the opportunity's current assessment in the active epoch. |
| `/workspace/landscape/pipeline` | admin and manager (`get_staff_user`) | The pipeline page (§13.4). |

**Weekly batch selection** (L45-L47, X6, D48).
- **Issue.** Once a week, the worker issues one batch, the same for everyone (L46). It holds up
  to 10 opportunities never issued before, taken in card order (D30). A blind batch holds 8 of
  those plus 2 probes (§10.6). If fewer are available, the batch holds what there is, possibly
  none.
- **Carry-over is per viewer** (L47). Each reviewer's and manager's batch view adds every
  card from earlier batches on which *they* have no quick decision. Carry-overs do not count
  toward the 10. They are grouped by batch, newest first: a normal batch's cards in card order
  (D30), a blind batch's in its stored shuffled order (D62).
- **Retired cards** (D62). An issued card whose opportunity has been retired, by S8 or at an
  activation, leaves every carry-over; its page stays readable as history. Issued probes are
  re-assessed by a candidate (§7.11), so they stay. A card with no completed assessment in the
  active epoch stays too, and says so (§7.10).
- **Changed cards.** A card whose shown assessment is not the one the viewer decided on is
  shown in their view as "changed since your decision" (D50), never a card whose opportunity
  is retired (D62). It does not count toward the 10.
- **Expert sample** (D63). A marked expert's view also lists their open sample items for the
  quarter, with S9's and S6's calls hidden until they submit.

**A card** (L41; readable in under a minute) shows:
- the BlackbirdBot headline;
- one structured line per member idea (D29);
- the first two sentences of the elevator pitch;
- the labs behind it, named by person;
- band and score, hidden on every card of a blind batch until the viewer decides (L53), and on
  an expert's own sample items until they submit (D63);
- the quick decision (§13.3).

Cards are ordered by their best member's S7 rank (D30), except in blind batches, which are
shuffled.

**Route order** (D46). `/workspace/landscape/pipeline` is registered before the UUID-typed
`/workspace/landscape/{opportunity_id}`, and a test pins that the pipeline path never reaches
the detail handler.

**Nav.** "Landscape" joins `WORKSPACE_NAVIGATION` (`src/web/navigation.py`) for everyone with
`permissions.research`, like Projects. PIs get 403, as on every workspace page.

**Data to templates.** The pages pass plain dicts built by a landscaping read model, as the
Projects pages do. That avoids a new ORM schema entry in `src/web/presentation.py`. Every new
key is added to `VIEW_FIELDS`, staff-only keys to `STAFF_KEYS`, and admin-only keys to
`ADMIN_KEYS`.

**Staff-only fields.** The automatic staff-only stripping is keyed on the
`OpportunityAssessment` type name (`presentation.py:265`). The landscaping read model
therefore drops the same staff-only verdict fields itself for reviewers. A sentinel test
proves no staff-only field reaches a reviewer.

### 13.2 The detail page ("in the same fashion as current assessments")

**Templates are copied, not shared** (D26). This follows the Projects precedent: copy the
list, detail and review-form templates, and share only `_head.html`, the timeline script and
the chat drawer. The assessment templates are not refactored, so the Assessments page is
untouched.

**Opportunity sections, in assessment order.**
- **Brief:** headline, structured lines, elevator pitch, and the six key-point groups. Each
  bullet carries its sourced or estimate badge (`claim_support`, L30).
- **Evidence summary.** `derive_strengths_and_risks` gets the S6 consults in the dict shape it
  expects.
- **The ask:** the recommended next experiment.
- **Verdict:** score and band, with the rubric's "provisional, advisory" note and the epoch.
- **Gating; red flags; rationale; dimension scores** (`build_dimension_rows` and
  `resolve_revision`, reused).
- **Deal-breaker flags** (L24): each raised flag, with its evidence, and whether S6 confirmed
  or cleared it.
- **Human review** (§13.3) and the **chat drawer** (§13.5).
- **Raw verdict JSON**, admin only.

**Member idea sections**, one per idea in the opportunity, collapsed after the first:
- the structured line;
- type, stage, evidence grade and group counts;
- **Target biology** (§7.7 step 4);
- **Disease profile** (§7.7 step 3), with a support badge on every field;
- the panel opinions;
- the full-text and summary evidence: verified quotes by section, with the evidence grade;
- **Sources:** every document, with its role basis (§5.2), license and `text_basis`;
- **Labs:** the qualifying labs, with OpenAlex and ORCID links. There is no PI page, because
  these researchers are not on the roster.

**Which assessment** (X1). The page shows the opportunity's current assessment *in the active
epoch*. Older versions and retired epochs are linked as history, never shown as current.
Decisions and reviews carry across versions, marked "changed since your decision" (L60).

**Blind batches** (L53, X5, F2). For a card of a blind batch, the detail page hides everything
§10.6 lists, the score and dimension rationales included, until the viewer has decided.

**Help with this page** links to the new help page (§13.6).

### 13.3 Decisions and reviews (L39, L40, L50, L51, L60, L33, D21, X7)

**Keyed on the opportunity.** Decisions and reviews are keyed on the stable opportunity, not on
one assessment version. Each records the `landscape_assessment_id` it was made on.
- When the assessment shown is not the one decided on, the decision stays attached and shows
  "changed since your decision" until the reviewer confirms or changes it (L60, D50).
- This holds across epochs too: opportunities keep their identity through §7.9's matching.

**The quick decision** (L39) is on every card and on the detail page:
- **pursue** or **pass**;
- reason tags from the criteria file (L49);
- a short reason, at most 1,000 characters.

It is one `landscape_decisions` row per (opportunity, reviewer), which the reviewer can edit.

**The optional full review** mirrors assessment review: one `landscape_reviews` row per
(opportunity, reviewer).
- score 1-5 (check) and comment ≤ 10,000 characters;
- `dimension_scores` validated against the live rubric (reuse `assessment_reviews._validate`
  and `_validate_comment`);
- `rubric_version` and `rubric_content_hash`, stamped;
- `edited`; the reviewer foreign key SET NULL, with `reviewer_name`; `recorded_by_user_id`.

Alongside it:
- `landscape_review_events`: approved, disapproved, cleared; append-only.
- `landscape_review_assignments`: unique per (opportunity, assignee).

**Independent votes** (L40, L50, L51). Until a viewer has made their own quick decision on an
opportunity, they see none of these from others:
- decisions, reviews and status events, and counts of any of them, tab membership included
  (D60);
- the same content on the card, the detail page or the chat record.

This binds managers and admins too (L50). Only the quick decision unlocks it, never a full
review (L51). Because the key is the opportunity, a new version does not re-blind a viewer who
has decided. The read model enforces the rule, and tests pin it. Admin access to the raw
tables, for audit, is unaffected.

**Recorded, not learned from** (L33, D21). Decisions, tags and reasons are stored in
structured form for a later version. Nothing in v1 tunes from them. There is no
`feedback_mode`, and they never feed the existing review bot or prompt suggestions.

**Routes** go in `src/routers/reviews.py`, keyed by opportunity id:
- submit and edit decision;
- submit and edit full review;
- delete (admin);
- status (approve / disapprove / clear);
- assign and unassign (staff; impersonation refused);
- expert judgment (a marked expert, on their own sample items, D63).

The POST allowlist test
(`tests/integration/test_reviews_router.py::test_the_reviews_router_posts_are_an_explicit_allowlist`)
gains the new paths. As today, there are no notifications.

### 13.4 The pipeline page and its controls

`/workspace/landscape/pipeline` is staff only. It shows:
- the phase (pilot, backfill or continuous) and the pause state;
- the epochs: active, candidate or held, with regression results (§7.11, §10.4);
- per-source completeness and last fetch;
- the §10.3 metrics;
- the latest cut: N, K and time;
- the bake-off, positive-control and drift reports;
- incomplete slices;
- the reject audit's disagreements, and the filtered-items browser with reasons (§10.5).

**Controls** post to a new router, `src/routers/landscape.py` at `/landscape`. It gets its own
POST-allowlist test, and the Origin guard covers it automatically.

| Control | Who | Notes |
|---|---|---|
| Pause / resume | staff | |
| Start backfill | admin | allowed only in phase `pilot` with a stored pilot report |
| Re-run bake-off | admin | |
| Override a held epoch | admin | needs a reason (L28) |
| Rescue an item | staff (L43) | needs a reason; re-enters the next stage (L44) |
| Positive-control identifiers | staff | DOIs or PMIDs; stored in the database, never in git |
| Mark or unmark an expert account (L55) | staff | |

Each control:
- refuses impersonation;
- is wrapped on the page in a confirm that says what it does.

### 13.5 Chat (L13, D25)

**Table layout** (L63). Chat follows the Projects precedent (migration 0076).
`0078_landscape_chat` widens the shared chat tables rather than adding parallel ones. The chat
is keyed on the stable opportunity, so a conversation does not split at a new version (X7). The
record always loads the current assessment in the active epoch.
- `assessment_chat_turns` gains `landscape_opportunity_id` (CASCADE);
- its check becomes `num_nonnulls(assessment_id, project_id, landscape_opportunity_id) = 1`;
- `assessment_chat_usage` and `assessment_chat_opens` gain the column (SET NULL) and the
  subject value `'landscape'`;
- an index is added;
- the check-text pin in `tests/unit/test_migration_checks.py` (`:2207-2208`) moves, the subject
  expectations change to `'landscape'` with their pins (`:2216-2217`), and the three redefined
  CHECKs stay out of `PLANNED_OBJECTS` (§12.5).

**The service.** Every two-way branch in `ChatSubject`, `_subject_*`, `PreparedTurn` and the
drawer (`templates/assessments/_chat_drawer.html`) gets an explicit third branch, so there is
no fall-through to "assessment". Analysis of the precedent names this as the main risk, and
isolation tests prove a landscaping conversation never reads or writes another subject's
turns.

**The record builder** is modelled on `project_chat_record.build_project_chat_record`. Its
documents are:
- Verdict;
- Evidence: sources and quotes;
- Disease profile;
- Panel;
- Reviews;
- Rubric.

There is no interview document. The Reviews document follows the independent-votes rule
(§13.3): a user who has not yet decided sees no other reviewer's decision or review there
either. The Verdict document follows the blind-batch rule (§10.6). The expert-sample rule
(D63) covers the Verdict and Panel documents, and any S6 call in the Disease profile and
Evidence documents.

**The rest:**
- **Prompt:** `prompts/landscaping/chat.md`. The web app reads it per question, as it reads
  `prompts/project-chat.md`; it is stamped like the rest of the set.
- **Router:** `src/routers/landscape_chat.py`, copied from `project_chat.py`, with the same
  refusal order (impersonation, `can_review`, both switches, 404, 415).
- **Switch:** `landscape_chat_enabled`, default false.
- **Model.** `llm_landscape_chat_model`, default `claude-opus-5` (D25). The shared
  `llm_assessment_chat_model` defaults to `claude-opus-5-5` (`src/config.py:345`), which L17
  rules out. The model is chosen per subject, and a test pins it.
- **Limits.** The per-user caps and the one-answer-in-flight rule are shared with every subject,
  unchanged. Landscaping spend also has its own daily sub-cap (`landscape_chat_daily_usd_limit`,
  D47), so it can never use up the shared total that assessment chat depends on. Opening
  questions are template-only, as for Projects.

### 13.6 Help

Per `docs/operations/help-content.md`:
- A new help page `landscape` (list, detail, reviewing, pipeline) in `src/web/help_pages.py`,
  with literal template names, under `templates/help/pages/`.
- Glossary terms: cut, opportunity, batch, blind batch, disease profile, role basis, text
  basis.
- Troubleshooting entries for the new refusals.
- A reviewer tutorial step and a manager tutorial step.
- Screenshots captured from a seeded throwaway instance on a workstation, as the last step
  before deploy.

Content rules apply:
- no numbers from settings or the rubric;
- staff-only sections inside `{% if permissions.staff %}`;
- no real names;
- no repository internals.

The rendered-page gate and the help-link test cover the new pages.

## 14. Reuse ledger

| Need | Reused as is | Reused with a change (stated) | New |
|---|---|---|---|
| JHU markers | `jhu_rules.is_hopkins_affiliation` | — | Baltimore set and matcher (§5.1) |
| Author role | — | `openalex_industry._role`, `_ROLE_RANK`, `_pi_at_jhu` lifted into a shared module and parameterized by institution set; industry behaviour unchanged | collective-author test |
| OpenAlex | `reconstruct_abstract`, `openalex_auth`, `openalex_budget`; `_list_works` left exactly as it is (m-B) | — | a new public `iter_works` (cursor paging, no cap, page counts) and `fetch_work` |
| PubMed / PMC | `lookup_pmcids`, `convert_dois_to_pmids`, `ncbi_session`, `identifiers.normalize_*`; `search_pmids` and `_parse_pubmed_xml` left as they are | — | new functions in `pubmed.py` (`esearch_page`, `fetch_pubmed_xml`, `parse_pubmed_extras`), plus an additive process-level setter, `pubmed.set_min_interval(seconds)`. The landscaping worker calls the setter once at start with 1/`landscape_ncbi_max_rps`, so every E-utilities call in that process honours ≤ 3 req/s (F6). That includes reused `lookup_pmcids`, `convert_dois_to_pmids` and S6's `project_search.search_pubmed`. Also the PMC cloud / BioC / Europe PMC text client. |
| RePORTER | `publications_for_cores` | `search_projects` (+ `AbstractText`, higher `max_total`, FY slices) | — |
| ODP | `odp` client and pacer | — | applicant-only query |
| Literature tools | `project_search` tools; `project_plan.source_key` / `resolve_source` | — | — |
| LLM | `llm.acreate`/`abeta_create`, `refusal_category`, the structured-output pattern in `coi_llm`, `delimit`/`_escape`, `extract_json` | `llm_pricing` (+ Sonnet 5.5, Haiku 5.5 tiers, batch modifier) | Batches wrapper; ledger |
| Rubric | `load_rubric`, `render_rubric_markdown`, `render_stage_bar_markdown`, `weighted_score`, `band` | — | `triage_score` (§7.5) |
| Panel | `specialists.parse_opinion`, `specialists.persona_path`, `blackbird_rubric.render_stage_bar_markdown` | the persona composition, re-done next to `tools._load_persona_text` with a parity test | S6 consult runner on the ledgered client |
| Verdict contract | `verdict_fields.sidecar_column_kwargs` (sidecar keys → column values), `sidecar._normalize_gating` | — | landscaping assessment row |
| Page | `build_dimension_rows`, `resolve_revision`, gating definitions, `normalize_*`, `key_point_sections`, `derive_strengths_and_risks` (fed dicts), `_head.html`, the timeline script | chat drawer (third branch) | list, detail and pipeline templates (copied), read model |
| Review | `assessment_reviews._validate`, `_validate_comment`, `reviews._parse_dimension_scores`, `_load_assignee`, `_form_str` | `/reviews` router (+ routes) | review tables, service |
| Chat | `ChatRecord`, `_Doc`, `prepare_turn`/`run_turn`, `assessment_chat.js` | `ChatSubject` and drawer (third branch), chat tables (0078) | record builder, prompt, router |
| Worker plumbing | `advisory_locks.fixed_key`, `make_engine`, `http_pacing`, `job_error_text` | — | the landscaping worker |

## 15. Rollout

1. **Phase 0, probes and pilot.**
   - **Probes** run inside a one-off container off the new image, so the production keys are
     used without being read on a workstation. They resolve the remaining *unverified*
     items:
     - ODP field names, and whether inventor cities are exposed (§6.1, APL exclusion);
     - the ortholog route (§7.4);
     - RePORTER's PI fields, confirming no ORCID is exposed (§5.2, F5);
     - JHTV's terms of use, read before the first RSS fetch. If they forbid automated reading,
       JHTV is disabled as a source;
     - adaptive thinking with `output_config.effort` on Sonnet 5 and Haiku 5.5, through the
       production request builders (structured output already runs in the images, as
       `coi_llm.py:222-226` does in production);
     - S6 and S9 truncation rates at their `max_tokens` constants (m-O);
     - 429 behaviour of ClinicalTrials.gov and Europe PMC at the starting pace (§6.1).
     
     These were settled by the live fact-check of 2026-10-30:
     - the Europe PMC `license` field;
     - bioRxiv `details` `license` and `jatsxml`;
     - HGNC `gene_group`;
     - Batches in `anthropic` 1.9.0.
   - **Positive controls.** Staff enter their identifiers on the pipeline page first (§10.2).
   - **Pilot intake** (D68). One month from each window year, picked at random and recorded,
     is ingested from every enabled source through S1, together with every positive-control
     document, fetched by DOI or PMID.
   - **Bake-off.** The §10.1 bake-off runs on samples of the pilot documents, through the
     production request builders. It seeds the regression set (§10.4), and an operator applies
     its model picks. The first epoch is created at the worker's first start and does no S2
     work until the bake-off has seeded its regression set; applying the picks supersedes it
     before any S2 work (D65).
   - **Pilot run.** The funnel then runs on all pilot documents: S2-S4, the S5 cut over the
     pilot pool, and S6-S9 on that cut. The epoch is regression-checked on S2-S4 (D65). It does
     not activate (D70), and nothing is issued to reviewers. The run yields the §10.2 measurements: S4 repeatability,
     normalization, the positive-control trace (S1-S4, with S5 judged against the pilot pool),
     S6 and S9 truncation rates, and cost and throughput per stage.
   - **Reason tags.** Claude drafts the starting reason-tag list, and the owner approves it (L49).
   - **Exit:** a stored pilot report (the bake-off, the probe results and the §10.2
     measurements, D68), and an admin's go on the pipeline page.
2. **Phase 1, backfill.**
   - The 3-year window runs in monthly slices, oldest first. NCBI-heavy slices run during
     NCBI's off-peak hours (K8).
   - S2-S4 run on every backfill document; S5-S9 wait for the phase to change.
3. **Phase 2, continuous.** The worker enters it by itself once the backfill has drained (D69),
   and the D17 intake cadence starts. S5-S9 then run on the whole window; the first ready cut
   activates the first epoch (§7.11 step 4, D70), and the first weekly batch is issued
   (§13.1). From then on cuts recompute after each cycle, and a batch is issued every week
   (L45), every fourth one blind (L54).

## 16. Risks

| Risk | Mitigation |
|---|---|
| `bio` refusals at scale (analysis E1) | measured in Phase 0 before and after fallback; the D42 chain; refusal-rate monitoring per field |
| A model is retired | settings per stage; deprecation dates in K10; bake-off and regression set rerunnable (§10.4) |
| A source API changes (PMC did in 2026; PatentsView paused) | completeness record per fetch; loud failure; a source can be disabled alone |
| LLM score instability flips the cut | repeatability measured (§10.2); deterministic tie keys; pairwise at the boundary and in S7 |
| Criteria changes mix judgments | epochs (§7.11); regression gate (§10.4) |
| A re-judge after an epoch change is costly (everything re-runs) | L27 and L34 accept it; the spend report shows it; the active epoch keeps serving meanwhile |
| Opportunities chain unrelated ideas | the L57 rule joins only same-lab ideas sharing a target or disease; wider links are "related" only |
| Opportunity identity churns and strands decisions | the L60 rule (half the members); decisions carried forward and marked changed |
| Idea churn under daily intake | versions change only on new qualifying documents; re-assessment at most weekly (D37) |
| Blind batches spend S6-S9 on below-cut ideas | 2 cards per fourth batch (L52, L54); L34 accepts the cost |
| Shared quotas with the simulation (NCBI key, Anthropic rate limits) | ≤ 3 NCBI requests/s; synchronous Opus work waits during a live run; 429 backoff (D33) |
| Batch orphans or lost spend | the intent-row protocol and startup reconciliation (D31) |
| Prestige or institution bias | blinding at S4 (D20) |
| Hallucinated targets or evidence | verbatim-quote check; ids from PubTator3, HGNC, Mondo and ChEMBL; ambiguous names left unmatched |
| Licensing | D6 and D7 sets only; license stored per text; JHTV terms read in Phase 0 |
| Political sensitivity of ranking named JHU researchers | admin, manager and reviewer visibility only; no export to Slack; nothing in git (K6) |
| A change to a shared module disturbs the agent or profile pipeline | additive-only changes; the characterization and profile suites must pass (D45) |
| Host disk | full text only for S6 documents (tens of MB, §11.3) |
| Spend | ledger, spend report, kill switch (§9.5) |
| The OpenAlex free-key budget ($1/day) runs out in sweeps | a weekly sweep is about 310-350 credits; daily deltas tens; measured in Phase 0. Prepaid credit is the owner's option. |

## 17. Left to the builder

- Module layout inside the landscaping package.
- Exact column types and indexes.
- Pacing constants per source within the stated limits.
- Batch sizes.
- Page layout within §13.
- Whether Open Targets uses GraphQL or release files (§6.3 trigger).
- The collective-author test (§5.2).
- Test structure.

## 18. Open items

None. Every owner question has been answered (§3.1, L1-L64). The remaining *unverified* items
are Phase 0 probes (§15).
