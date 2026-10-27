# BlackbirdBot projects prototype: literature research finds new uses for each lab's work, refined with that lab — design

- **Status:** DRAFT for owner review, revision 4, 2026-10-26. Revisions 1–3 (2026-10-22) described a prototype that
  read staff reviews for ideas, matched them to labs, drafted experiments, and judged labs' answers on fit. On
  2026-10-26 the owner redirected it (D41): no review analysis; the specialists research the literature for new uses
  of each lab's tools, methods and models; BlackbirdBot proposes one idea, anchored in that lab's profile, to that
  lab; the two bots refine it into a project; finished projects appear on a new page, separate from Assessments.
  Revision 4 is a rewrite, with a builder check and an adversarial audit folded in (4 fact errors, 3 high, 11 medium
  and 8 low findings, 18 builder questions; all settled below or in §10, and the one question for the owner answered
  as D55). D-numbers are the owner's decisions (D1–D34 and D39 in the full design; D35–D38 and D40 from revisions
  2–3, now superseded or not applicable, §4; D41–D55 here). Uncommitted. Not approved.
- **Requested by:** the owner, 2026-10-26 (verbatim): "we need to refine the prototype, instead of getting ideas from
  reviews we will not be analyzing reviews in the prototype but we should use the specialists to perform literature
  research to identify new uses for models/techniques for the blackbirdbot to propose ideas based on pi profile
  information to pitch to that specific profile, for example figuring out what indications are aligned with the
  profiles, and all pitches must be anchored in the tools, methods, models, expertise, etc of the pi profiles. after
  refinement between the targeted pi bot and blackbird bot, once the project is ready, it should be visible in a new
  web ui page similar to but not presented on the assessments page so this should be a new landing page for these
  blackbirdbot proposed ideas. do not assume anything and brainstorm any open questions with me"
- **Deliverable:** this document and its slide deck; then, on the owner's instruction of 2026-10-26 ("once the spec
  is finished, proceed with writing the implementation plan"), an implementation plan. No code until the owner
  approves the plan.
- **Measured:** code at `blackbird@6053d30c`, read 2026-10-26 (§3). Production facts are from the read-only checks of
  2026-10-20 to 2026-10-22 recorded in the full design (`docs/specs/2026-10-20-review-learning-loop-design.md`,
  revision 8, "the full design"). Jason Zavras's design is `BlackbirdBot-Centric-CoPI.pptx`, compared in
  `docs/specs/2026-10-21-bbl-centric-proposals-comparison.md`.
- **Sensitivity:** revision 4 quotes no staff review. Its example (§5.6) uses only the Gray lab's public profile.
  `origin` is public: check before any push, as for the full design.

---

## 0. Summary

Labs pitch their own ideas today, and BlackbirdBot assesses them. Nothing looks for what else a lab's tools and
models could do: a disease they have not worked on, a target their method fits, a use their model was never tried
for. This prototype has BlackbirdBot look, with the specialists' help, and bring the best idea to the lab.

1. **One project per live lab per day** (D44). Every lab bot that is active and unmuted gets one new project every
   24 hours of run time, once its previous project has finished.
2. **Research** (D41, D48). Five specialists (clinical, scientific, technologic, commercial and legal) search
   PubMed, ClinicalTrials.gov and US patents for new uses of what that lab's public profile lists: its methods and
   technologies, model systems, disease areas and targets. Every candidate names the profile items it rests on, and
   those are checked against the profile, item by item.
3. **Choice** (D54). BlackbirdBot picks one candidate using the investment rubric's criteria, such as unmet need and
   differentiation, and writes an opening plan in Jason's template plus the lab's tools and models (D46). The
   reasons are written in words; nothing is scored (D43).
4. **Posting** (D49). BlackbirdBot posts the idea in `#general`, addressed to that lab, with no approval step. The
   text is assembled by the engine from checked fields.
5. **Refinement in Slack** (D45, D50, D51). The lab must take it up: it replies with its own post, in its own words,
   and that opens a thread of at most 12 messages, like today's interviews. The two bots work the plan until both
   agree it is feasible and complete — closing as soon as they do — or until BlackbirdBot's last turn, when it
   records what still divides them. The lab's reply does not use up its own pitch for the day.
6. **The Projects page** (D42, D47, D52). Managers, admins and reviewers see each finished project, "ready" or "not
   agreed" with its gaps and disagreements, in a list and a details page like the Assessments pages, but separate
   from them.
7. **Project reviews** (D42, D53). Staff and reviewers rate each project 1–5, comment, and say fund or don't fund.
   These reviews are kept apart from assessment reviews.

Labs keep pitching their own ideas, and BlackbirdBot assesses those as today (D5). The full design is unchanged; §12
shows how the prototype can grow into it.

---

## 1. Why

- **Labs pitch what they think of.** A lab's own pitch comes from its own view of its work. A lab's tools often
  have uses it has not pursued, and finding them means reading outside its field: other diseases, trials and
  patents.
- **The profiles already hold the anchors.** Each lab bot's public profile, written from the PI's papers, lists its
  methods and technologies, model systems, disease areas and targets. The Gray lab's lists 26 methods (from
  replica-exchange docking to phage display) and 20 model systems (`profiles/public/gray.md:11-61`). Research can
  start from those items and come back to them.
- **Jason's design asks for this shape.** His deck has BlackbirdBot write, for each PI, a one-shot experiment
  (indication, target, model, positive and negative controls, readout, design, with standard of care where one
  exists, $100–200K) and refine it with that lab's bot.
- **Staff judge, not BlackbirdBot** (D43). AI judges favour their own text (Panickssery, Bowman & Feng, NeurIPS 2024)
  and text that reads as familiar to them (Wataoka et al., 2024), and every bot here runs on the same model
  (`src/config.py:338`). The prototype stores no scores and no recommendation. The bots' agreement records whether
  the plan is feasible and complete. Whether it is worth funding is left to staff reviews.
- **The prototype asks one question cheaply:** do literature-backed ideas, built on what a lab already uses and
  refined with that lab, produce projects staff would fund?

---

## 2. Goals, non-goals, anti-goals, measures

**Goals**

- PG1. Each live lab gets one researched, profile-anchored project idea per 24 hours of run time, once its previous
  project has finished.
- PG2. Every idea rests on whole items of its lab's profile lists and on sources the research actually retrieved.
- PG3. The lab and BlackbirdBot refine the idea in Slack into a plan in Jason's template plus the lab's tools and
  models, and the outcome says whether both agree it is feasible and complete.
- PG4. Staff and reviewers see every finished project on its own page and review it there, apart from assessments.
- PG5. Nothing changes for labs' own pitches, their assessments, their reviews, Path A, the Discussions page, the
  Interviews page or the run statistics' interview figures.
- PG6. The tables and steps can grow into the full design (§12).

**Non-goals:** reading staff reviews for ideas, lessons, interests or funded projects (the full design, D1–D12);
approval before posting (D49); scores, recommendations or fit assessments of projects (D43); Slack headlines for
projects; PIs seeing the page (D52); changes to the investment rubric or to any existing prompt file; starting
simulation runs.

**Anti-goals:** an idea that rests on something the lab's profile does not list (D33); a citation the research did
not retrieve; a posting that names a PI or bot other than its lab's, or uses anything from a staff review or a private
profile; a project review that mixes into assessment reviews; a lab pitching Blackbird's project back as its own
idea; a shortcut the full design would have to tear out.

**Measures** (reported, no targets): projects started per live lab per day, and the days missed with their reasons;
cost per project, research and refinement separately; the share ready, not agreed and not finished; the most common
gaps and disagreements; how often each profile section anchors an idea; sources per project by kind; staff ratings
and the fund share, beside the ratings of labs' own pitches on assessment reviews; the time from request to finished
project.

---

## 3. What exists today (relied on)

Code at `blackbird@6053d30c`, read 2026-10-26, unless noted.

- **Lab profiles.** Each lab's public profile (`profiles/public/<agent_id>.md`) has the sections Research Summary,
  Key Methods and Technologies, Model Systems, Disease Areas / Biological Processes, Key Molecular Targets, Keywords
  and Recent Publications (`src/agent/persona_sections.py:29-45` names them; `sections()` at `:56` reads them). The
  four middle sections are bulleted lists. A lab bot's system prompt carries this public profile
  (`src/agent/agent.py:375-376`). A PI may also have a private profile (`private_profile_md`,
  `src/models/profile.py:42`); the prototype does not use it.
- **Who is live.** The agent registry records each bot's role and status; muting sets the status to inactive
  (`src/services/agent_mute.py:33-35`). On 2026-10-22, 4 lab bots were active: gray, deutsch, hu and menard
  (production, read-only). Roster changes reach a running engine without a restart; a bot taken off the roster is
  dropped from the engine (`src/agent/engine/post_lane.py:841-878` explains why its threads stop counting).
- **Specialists.** Eight personas in `prompts/specialists/` (budget, chemistry, clinical, commercial, legal,
  scientific, talent, technologic). Each is written for one question about one opportunity and answers in JSON with a
  `verdict_signal` of `blocking`, `gap` or `adequate` (`prompts/specialists/clinical.md:3-4, 37-55`). The five used
  here share the headings "What you own", "What you do not own", "You do not decide" and "Answer format". A consult is
  one model call with no tools (`_call_specialist`, `src/agent/tools.py:712`). Each consult posts a one-line panel
  note into the interview thread naming the domain, the question and the signal (`_post_panel_note`,
  `src/agent/engine/panel.py`), and consults are recorded per lab and thread.
- **Search code.**
  - PubMed: `search_pmids(term, retmax, sort)` runs an ESearch and raises on failure
    (`src/services/pubmed.py:808-830`, used by `src/services/corpus.py`); `fetch_pubmed_records` and
    `fetch_abstract` fetch records, all paced and keyed through `_ncbi_get` (`:172`).
  - ClinicalTrials.gov: `src/services/industry_sources/ctgov.py` queries the v2 API (`BASE`, `:9`) with
    `query.term`, asks only for the identification, sponsor, status and conditions modules (`:13-14`), has no pacing
    or retry, and is used only to find a PI's industry-sponsored trials.
  - Patents: `search_prior_art` (`src/services/patents.py:669`) searches US filings by title through the USPTO Open
    Data Portal, returns None when it cannot run, and backs BlackbirdBot's `search_prior_art` tool
    (`src/agent/tools.py:648`).
  - A role's tools are its `role.toml` `tools` list: `tools_for_role` filters by it and `execute_tool` refuses any
    other (`src/agent/tools.py:101-104, 184-186`).
- **Investment rubric.** `prompts/rubric/blackbird-rubric.toml` has six dimensions: differentiation & unmet need,
  scientific credibility & mechanism, translational & development path, fundable killer experiment & capital
  efficiency, venture potential, and team & executability (`:260-312`). Its fundable range is a $100K–$300K grant
  over 6–12 months (`:299`), which holds Jason's $100–200K.
- **One model for every bot.** Lab bots, specialists and BlackbirdBot run on `claude-opus-5` (`src/config.py:338`),
  priced at $5 per million input tokens and $25 per million output tokens (`src/services/llm_pricing.py:29`).
- **BlackbirdBot is reply-only.** `posts_new_threads=False` (`src/agent/role_capabilities.py:67`), and its prompt
  says "You never make a top-level post" (`prompts/roles/scout_hub/agent-system.md:268`). The engine already posts
  BlackbirdBot messages that no model writes at that moment (the run-start marker, `#assessments-summary`
  headlines). `_post_message` (`src/agent/engine/slack_io.py:311-320`) takes a `phase` that today only panel notes
  pass. The authorship check never blocks a hub post but checks and records every post except a panel note
  (`slack_io.py:540-578`). As a last step `_post_message` strips an `<assessment_json>` block (`:355-360`).
- **Long posts are split.** Text over 4,000 characters is posted as several chunks, each its own log entry in the
  thread (`slack_io.py:496-533`; `src/agent/slack_client.py:189-197`), and each chunk counts toward the thread's
  message count (`src/agent/message_log.py:441-458`).
- **Interviews.** A lab's top-level post opens one: BlackbirdBot activates a thread on every new bot top-level post
  in its channels, whatever its phase (`post_lane.py:316-374`), and a resumed run re-runs that activation over the
  run's last days (`_recover_reply_less_pitches`, `src/agent/simulation.py:387-421`). An interview is at most 12
  messages (`REQUIRED_MAX_THREAD_MESSAGES`, `src/agent/engine/constants.py:123`); a thread with 12 messages is closed
  as a time-out before the next reply (`src/agent/engine/reply_lane.py:747-753`). A reply's ordinal is the thread's
  message count plus one (`reply_lane.py:557, 714`), and the guidance changes by ordinal: EXPLORE to 4, DECIDE to 11,
  CONCLUDE after (`src/agent/thread_guidance.py:207-219`). So in a lab-rooted thread BlackbirdBot normally writes
  message 12 and the lab message 11; a split reply shifts that by one. A thread rooted at a BlackbirdBot post would
  leave BlackbirdBot on message 11 and never reach CONCLUDE (the ordinal trap).
- **What an interview produces.** BlackbirdBot's CONCLUDE reply carries an `<assessment_json>` sidecar
  (`captures_verdicts`; `_capture_hub_assessment`, `src/agent/engine/verdicts.py:231`). A ⏸️ anywhere in a reply
  closes the thread (`src/agent/engine/sidecar.py:49-50`, `reply_lane.py:1178-1216`): a hub ⏸️ is a `hub_declines`
  row (`reply_lane.py:1225`), a lab ⏸️ an assessment drop. Empty and abandoned hub replies are drops too
  (`reply_lane.py:623-676, 966-1027`). Headlines owed at shutdown are found from assessment rows
  (`src/agent/engine/headlines.py:1197-1240`). `_handle_reply_response` is exactly 200 lines
  (`reply_lane.py:947-1146`), the most the size gate allows (`tests/unit/test_phase1_size_gate.py:29-39`).
- **Thread outcomes.** `thread_decisions.outcome` is an enum of `proposal`, `no_proposal`, `timeout` and `abandoned`
  (`src/models/agent_activity.py:367-368`; `alembic/versions/0063_thread_outcome_abandoned.py` added the last).
  Closing a thread queues a working-memory update for both bots (`src/agent/engine/threads.py:107-225`).
- **Message log.** A log entry's `phase` is set when it is posted and restored from the database on resume
  (`src/agent/engine/rebuild.py:143`). Every agent-facing read skips panel notes (`message_log.py:356-548`), as does
  `_restore_active_threads` (`rebuild.py:227`). Working-memory synthesis reads the bot's own last 20 messages,
  skipping panel notes (`src/agent/engine/memory.py:215-224`). A lab's Phase-5 prompt lists its recent top-level
  posts, and its daily cap counts them, both through `get_agent_top_level_posts` (`post_lane.py:461-465`,
  `src/agent/engine/scheduler.py:164-182`). `agent_messages.phase` is 30 characters
  (`src/models/agent_activity.py:113`).
- **The lab pitch step** is `_phase5_new_post` (`post_lane.py:377-561`), reached only when the lab's spontaneous
  timer is due (`post_lane.py:158-174`). It stamps the turn's timer, then checks the daily cap — `_count_today_posts`
  against `lab_daily_post_cap` = 1 (`src/config.py:448`) — then the run's `--max-proposals` limit, then
  `active_thread_threshold` (`src/config.py:430`). `_publish_pitch` (`post_lane.py:728`) posts; on resume
  `_rehydrate_proposal_count` counts this run's bot rows with phase `new_post` (`post_lane.py:785-826`). A run that
  reached `--max-proposals` ends once no interview is open for 3 checks in a row (`PROPOSAL_DRAIN_SETTLE_TICKS`,
  `constants.py:129`); threads whose partner left the roster do not count (`post_lane.py:841-878`).
- **Pages.** The workspace router is mounted at `/workspace` (`src/main.py:349`). The Assessments pages are gated by
  `get_review_user` (admin, manager or reviewer; `src/routers/workspace/assessments.py:24`,
  `src/dependencies.py:368`), and the details page includes the interview timeline
  (`templates/assessments/_detail_body.html:1305`). The Discussions page lists every `new_post` root and every thread
  whose decision has no such root (`src/services/directory.py:1040-1080`, counts at `:1288-1316`). The Interviews and
  lab pages list `new_post` roots (`src/services/interview_posts.py:103`, `src/routers/agent_page.py:210`).
- **Worker pattern.** When it has no job, the worker makes the chat's opening questions, then interview trajectories,
  one call at a time, so a job waits behind at most one call of at most 3 minutes (`src/worker/main.py:8-12,
  550-577`; `src/services/trajectory_generation.py:87-89`). Each call's row is written before the model is called,
  priced at its worst case against a rolling daily dollar ceiling, and a refusal is never retried. The ceiling sums
  every usage entry of each row updated in the last 24 hours (`trajectory_generation.py:340-371`). Today's ceilings:
  trajectories $10, chat suggestions $30, company discovery $20 (`src/config.py:386-424`). `live_run_id` names the run
  the engine is running (`src/services/runs.py:48-64`). The worker mounts `profiles/` and `prompts/`
  (`docker-compose.prod.yml`, the uncommitted working-tree copy).
- **Run timing.** `max_runtime` is checked against the engine's own start time (`src/agent/engine/scheduler.py:55-62`),
  which every start resets (`src/agent/simulation.py:303`); nothing stores how long a run has been running.
  `simulation_runs` records `held_at`, `ended_at` and `finalized_at` (`src/models/agent_activity.py:45-74`).
- **Prompt sets.** pi_lab 1.2.0 and scout_hub 1.12.0 (`prompts/roles/*/role.toml`). A role's prompt files are listed
  in `role_capabilities.py` (`:59-62`, `:74-76`), loaded once per engine start (`src/agent/prompt_snapshot.py`).
  pi_lab's files live in `prompts/`, scout_hub's in `prompts/roles/scout_hub/`. The lab's phase-4 template frames
  every thread as an interview about the lab's own work and offers ⏸️ (`prompts/phase4-thread-reply.md:3-8,
  106-108`).

---

## 4. Decisions

**The owner's decisions for this revision, 2026-10-26** (verbatim; the questions are paraphrased):

- **D41** (the redirection, quoted in full above) no review analysis in the prototype; the specialists' literature
  research finds new uses for each lab's models and techniques; BlackbirdBot pitches profile-anchored ideas to that
  lab; the two bots refine them; ready projects appear on a new page, separate from Assessments.
- **D42** (what staff see and do on the page) "blackbird staff should be able to see details similar to what is seen
  on the assessment list and details pages for each project and they should be able to review it but these reviews
  must be kept separate from the existing assessment reviews" (§5.10, §5.11)
- **D43** (scores and recommendations?) "they should not carry scores and recommendations, staff and reviewers will
  judge"
- **D44** (when and for which labs) "one project per active and unmuted lab bot per day" (§5.2)
- **D45** (when refinement ends) "when both bots agree the plan is feasible and complete" (§5.8, §5.9)
- **D46** (what a complete plan contains) "use Jason's template plus the lab's tools and models used" (§5.5)
- **D47** (no agreement) "mark it not agreed and show it on the page for staff with gaps and disagreements" (§5.9)
- **D48** (sources) "PubMed, clinical trials and patents, like Jason's deck" (§5.3)
- **D49** (approval?) "straight to the lab, staff judge the finished project" (§5.6)
- **D50** (where refinement happens) "in Slack like today's interviews" (§5.8)
- **D51** (may a lab decline?) "no" (§5.7, §5.8)
- **D52** (who sees the page) "staff and reviewers only" (§5.10)
- **D53** (what a project review captures) "a 1-5 rating, a comment, and fund or don't fund" (§5.11)
- **D54** (choosing among candidate uses) "use Blackbird's rubric criteria like unmet need and differentiation" (§5.4)
- **D55** (asked whether project threads should show the specialists' `blocking` / `gap` / `adequate` labels, which
  interviews show as panel notes in Slack and on the details page) "Hide the labels (Recommended)" — the option read:
  specialists still check the plan and BlackbirdBot uses what they say; no label notes go into project threads in
  Slack; the Projects page shows what each specialist found without the label (§5.8, §5.10)
- **Two playbacks confirmed** ("looks correct", twice): the flow above, with these calls of mine — a day is 24 hours
  of run time; D33's rule for claims; the clinical, scientific, technologic, commercial and legal specialists
  research; a PubMed search, with the existing ClinicalTrials.gov and patent code reused; research runs in the
  background under a daily spending cap; the lab's reply does not use up its own pitch for the day; the reason for a
  choice is written in words, never a score; the review reading, approval, separate drafting and fit scoring of
  revisions 1–3 are dropped.

**Revisions 2–3's decisions in this revision:** D35 (approval with an automatic switch) is superseded by D49. D36 was
already superseded. D37 and D38 (matching an idea to chosen labs, the source lab allowed) do not apply: each project
starts from one lab. D40 (a separate drafter; BlackbirdBot judging fit only) is superseded by D43: with no scores,
BlackbirdBot writes and refines its own idea, and staff judge. D17's rule survives in a new form: the lab's reply does
not use up its pitch for the day.

**The full design's decisions that apply:** D5 (labs keep pitching their own ideas), D15, D23, D24 (spec before
plan, plan before code), D33 (claims only what the profile and papers show the lab actually used). The full design
and its D39 are unchanged.

---

## 5. Design

### 5.1 Flow

```
 run running; lab bot active and unmuted; its previous project finished; 24 h of run time since it began (§5.2)
        │  engine writes a project row: requested
        ▼
 research (worker, one model call per idle slot, §5.3): 5 specialists search PubMed, ClinicalTrials.gov, US patents
        │  candidates, each anchored in whole profile items and in retrieved sources (checked in code)
        ▼
 choice (worker, §5.4): BlackbirdBot picks one by the rubric's criteria; writes the opening plan as fields
        │  checks: anchors, sources, names, completeness of fields   → no candidate / failure: not finished
        ▼
 posting (engine, §5.6): text assembled from the fields; BlackbirdBot posts it in #general to that lab
        │
        ▼
 lab reply (engine, §5.7): the lab's own top-level post; cannot decline; its own daily pitch stays free
        │  BlackbirdBot activates on it as on any lab post
        ▼
 refinement (engine, §5.8): a project thread of at most 12 messages; no scores, no assessment, no headline
        │  a lab reply starting ✅ lets BlackbirdBot close at once with ✅; otherwise it closes on its last turn
        ▼
 outcome (§5.9): ready  |  not agreed (gaps, disagreements)  |  not finished (reason)
        ▼
 Projects page (§5.10) ─► project reviews: 1–5, comment, fund / don't fund (§5.11)
```

### 5.2 One project per live lab per day (D44)

- **Who.** A lab is live when its bot has role `pi_lab`, status `active`, is on the engine's roster, and has a public
  profile with a Research Summary. A muted bot is inactive and gets none.
- **The run clock.** "24 hours of run time" needs a clock that counts only running time, which nothing keeps today.
  A new row per run (`hub_project_clocks`) holds `run_seconds`. Once a minute the engine adds the time since its last
  addition, at most 15 minutes at a time; a stop, a hold or a crash adds nothing (a new engine starts counting
  afresh), and a stalled loop adds little. A resume continues
  from the stored value.
- **When.** Each project stores the clock's value when it was requested. A lab is due when it has no open project in
  this run and either it has had no project in this run, or 24 hours of run time have passed since its last one was
  requested. So at run start every live lab is due; a lab activated or unmuted mid-run is due at once if it has had no
  project in this run, and otherwise on its schedule. My calls: at most one open project per lab, so slow research or
  a slow refinement delays the next one (measured, §2); `PROJECT_INTERVAL_HOURS` (default 24) exists for tests and
  the dry run.
- **How.** In the same once-a-minute check the engine writes a `requested` row for each due lab. Rows are unique per
  run, lab and slot (the lab's sequence number in this run), so a restart cannot write two. The engine never calls a
  model for research.
- **Not while draining.** Once the run has reached `--max-proposals`, no project is requested or posted. A posted
  project that is still waiting for its lab's reply keeps the drain waiting, so the reply is not cut off — unless the
  lab has left the roster, in which case the project becomes not finished ("lab not live") and stops counting, so the
  drain cannot hang (the rule `_open_interview_ids` already applies to orphaned threads).
- **Status changes are compare-and-set.** Every transition is `UPDATE … WHERE status = <expected>`, so the engine and
  the worker cannot overwrite each other (for example, the worker writing "ready to post" over a project the engine
  has just ended).
- **Runs that are not running.** The worker researches only projects of the live run (`live_run_id`). A run that is
  stopped or held keeps its open projects as they are, and a resume continues them. When a run is finalized, by any
  path, its open projects count as not finished ("the run was finalized first"): the page and the measures derive
  this from the run's `finalized_at`, so no finalize path has to change.
- **Kill switch.** `HUB_PROJECTS_ENABLED`, snapshotted at engine start, stops new requests, postings and lab replies,
  and a switched-off run does not wait for lab replies at its drain.
  It never turns off how project threads are recognised or what is switched off inside them (§5.8): a run resumed
  with the switch off still treats its open project threads as projects.

### 5.3 Research (D41, D48)

Runs in the worker, in its idle slot after interview trajectories, **one model call per slot**, so a job still waits
behind at most one call. A project's research state is stored on its row and advanced one step at a time.

- **Statuses** (my call): `requested` → `researching` → `ready_to_post` → `posted` → `refining` → `ready`,
  `not_agreed` or `not_finished` (with a reason code). The worker takes the oldest `requested` or `researching`
  project of the live run.
- **Inputs.** The lab's public profile only (the seven sections above), read when research starts and hashed. No
  private profile, no staff review, no interview transcript. The titles and anchors of the lab's earlier projects (any
  run) are passed so the same use is not proposed again.
- **The specialists** (my call, confirmed): clinical, scientific, technologic, commercial and legal, each in its own
  research session, one after another. A session's system prompt is a new research frame,
  `prompts/projects/research.md`, followed by that specialist's "What you own" and "What you do not own" sections, cut
  from its persona file by heading. The persona files are not changed, and their "one question", verdict-signal and
  answer-format parts are not used. If a persona file lacks either heading, that session is skipped and logged.
- **Angles.** Clinical: indications, standard of care and trials. Scientific: mechanism and the evidence that a use
  could work. Technologic: fields where the lab's methods or platforms could apply beyond their current one.
  Commercial: unmet need and who would want the result. Legal: patent activity around a use.
- **Tools** for each session (results are untrusted text, fenced with `prompt_safety.delimit`):
  - `search_pubmed(query)`: `search_pmids(query, retmax=10, sort="relevance")`, then titles, years and abstracts
    through `fetch_pubmed_records`;
  - `search_trials(query)`: the ClinicalTrials.gov v2 API with `query.term`, with a field list that adds the design
    and arms-and-interventions modules, up to 10 studies (NCT number, title, phase, status, conditions,
    interventions), spaced at least a second apart; a 429 or an error is reported to the model as "could not run";
  - `search_prior_art(query)`: BlackbirdBot's existing patent tool, with its definition and its formatting, its
    "could not run" result kept distinct from "no matches";
  - `retrieve_abstract(pmid_or_doi)`: the existing tool definition, backed by `fetch_abstract`, for a closer read.
- **Steps.** A step is one model call. When the reply asks for tools, the worker runs them in the same step and stores
  the conversation, so the next step continues it. After 8 tool rounds the next call is made without tools, asking for
  the final answer. A session ends when the model answers without asking for tools.
- **Output** of each session (JSON, parsed tolerantly): up to 3 candidate uses, each with the use (for example a new
  indication, a target, an assay, or a model applied in a new field); its anchors; its sources (PMIDs, NCT numbers,
  patent numbers) with what each shows; and why it is promising from that specialist's angle.
- **Anchors are whole profile items.** An anchor must equal one bullet item of Key Methods and Technologies, Model
  Systems, Disease Areas / Biological Processes or Key Molecular Targets, or one comma-separated Keywords item, after
  Unicode NFKC normalisation, case folding, collapsing whitespace and treating every dash alike. A candidate needs at
  least one anchor from Key Methods and Technologies or Model Systems (D41: "anchored in the tools, methods, models");
  anchors that match nothing are dropped, and a candidate left without a qualifying anchor is dropped.
- **Sources must have been retrieved.** Every cited PMID, NCT number or patent number must have come back from a tool
  call in that session; others are dropped and logged; a candidate left with no source is dropped.
- **Sources unavailable.** A tool that cannot run says so to the model, which carries on with the others. If PubMed
  cannot be reached at all, the step is retried later (my call: PubMed is the source every specialist relies on).
- **Cost and failures.** Every call is claimed before it runs in its own ledger row (`hub_project_calls`), written
  first at the call's worst case and updated with its real usage after. The daily ceiling
  (`PROJECT_RESEARCH_DAILY_USD_LIMIT`, default $25, my call) sums the ledger rows touched in the rolling 24 hours,
  call by call, so one long project cannot hold the ceiling shut for ever. A step that meets the ceiling waits. A rate
  limit or server error (never billed) is retried without counting; any other failure counts, and a session that
  fails 3 times ends with no candidates; a refusal ends its session at once. After a worker restart, a step whose call
  never finished is simply run again (its claimed row still counts against the ceiling). My estimate, to be measured
  in the dry run (§8): $2–5 of research per project.
- **Stored:** each session's candidates (kept and dropped, with why dropped), the sources with their titles, the
  profile's hash, the prompt files' hashes, and every call's usage.

### 5.4 Choice and opening plan (D43, D46, D54)

One more worker call, written as BlackbirdBot (`prompts/projects/choose.md`, new; the hub's existing prompt files are
not used or changed), with a structured answer:

- **Inputs:** the surviving candidates with their anchors and sources, the lab's profile sections, the lab's earlier
  projects, and the anchor text of five investment-rubric dimensions read from the rubric file: differentiation &
  unmet need, scientific credibility & mechanism, translational & development path, fundable killer experiment, and
  venture potential. (Team & executability is about the lab, which is the same for every candidate; the bots test it
  in refinement.)
- **Output fields:** the chosen candidate; why it beat the others, in words; one line on each candidate not chosen;
  a short title (the use, for example "MRGPRX4-blocking antibodies for cholestatic itch"); a two- or three-sentence
  summary of the idea; and the opening plan, one field per row of §5.5, filled as far as the research supports, with
  "to settle with the lab" where it does not. No number, rank, score or band is asked for or stored (D43).
- **Checks** before the project becomes ready to post:
  - the anchors and sources it uses are among the surviving candidate's;
  - names (my call, from the audit): the summary, the plan fields and the reasons name no PI or bot other than this
    lab's. Matching is case-sensitive on full bot names, full PI names, and a surname next to "lab", "group", "Dr."
    or "Prof."; BlackbirdBot's own name is allowed; source titles are not checked. So "gray matter", "Wilson disease"
    or "green fluorescent protein" pass;
  - no `@` anywhere;
  - each field within its length limit (my call: summary 600 characters, each plan field 300).
  A failed check retries the call once with the failure stated; a second failure ends the project as not finished
  ("choice failed").
- **No candidate.** If no candidate survives research, the project ends as not finished ("no idea found"), keeping
  each session's dropped candidates and why, and the lab is due again after the interval.

### 5.5 The plan template (D46)

From Jason's deck, with the investment rubric's fundable range:

| Field | What it holds |
|---|---|
| Indication | the disease or clinical setting, or the application where the use is not clinical |
| Target or mechanism | the gene, protein, pathway or mechanism, or "none" with why |
| Model | the model or system the experiment runs in |
| Positive control | what should show the effect |
| Negative control | what should not |
| Standard of care | today's treatment or method to compare against, or "none exists" with why |
| Readout | what is measured, and what result means success |
| Design | groups, numbers, steps, and the decision the result informs |
| Time and budget | within 6–12 months and $100K–$300K (`blackbird-rubric.toml:299`) |
| The lab's tools and models used | each item, marked "in the lab's profile" when it equals a profile item (the §5.3 rule), otherwise "stated by the lab" |

A field is filled when, after trimming, it is not empty and is not just a placeholder ("to settle with the lab",
"TBD", "to be determined", "unknown", "n/a", "none", "-", "?"; "none exists, because …" is filled). A plan is
complete when every field is filled.

### 5.6 Posting (D49)

- **What is posted.** Code assembles the text from the checked fields when the choice passes its checks, and the
  engine posts it as stored, so nothing unchecked and nothing model-written at posting time is posted: a fixed
  header, `:test_tube: *Blackbird project for the <PI surname> lab*`; the summary; "Builds on:" with the anchors;
  the opening plan, one line per field; up to five sources by identifier and title (never by author names); and a
  closing line asking the lab to reply with its own post. The whole must fit one Slack message as Slack receives
  it (after formatting and escaping, which can lengthen it; §3), and stays under 3,500 characters; if it would
  not fit, sources are dropped from the end first, then titles shortened.
- **How.** As BlackbirdBot, in `#general`, through `_post_message(..., phase="hub_project")`, from the engine's
  once-a-minute check, only while BlackbirdBot is on the roster. A claim is written before posting and resolved
  on resume, so a crash between the post and its record cannot post the project twice. A post that fails clears
  its claim and is tried at the next check; a post that went out but could not be recorded is recorded at the
  next check (the plan's calls, from the plan audit, so no claim can stay stuck). The authorship check skips these posts as it skips
  panel notes. A new capability flag on `scout_hub` allows them; `posts_new_threads` stays false and `role.toml`'s
  `post_types` stays empty. No model writes at posting time. With Slack off the post is still recorded, so the lab can
  reply.
- **When a lab is no longer live** at posting time, the project ends as not finished ("lab not live at posting").
- **Kept out of every read.** A `hub_project` post and everything in its thread are invisible to every agent-facing
  read, as panel notes are: `MessageLog`'s list reads (including tags and replies inside its thread),
  `_restore_active_threads`, working-memory synthesis, `_rehydrate_proposal_count` and the web checks that treat a
  root as a lab pitch. So nothing opens a thread on a posting, which avoids the ordinal trap. The lab sees the posting
  through its reply step (§5.7); BlackbirdBot sees it through the project row (§5.8).

**Example (illustrative only).** Written by hand from the Gray lab's public profile to show the shape; not produced
by the system, and its science is not checked. Bracketed parts would come from the research.

> :test_tube: *Blackbird project for the Gray lab*
> Blackbird proposes antibodies that block MRGPRX4 as a treatment for cholestatic itch.
> Builds on: computational antibody design and specificity switching (RosettaAntibodyDesign); deep learning antibody
> Fv structure prediction (IgFold, DeepAb); high-throughput antibody expression and biophysical characterization (Tm,
> Tagg, KD, polyreactivity, self-association); MRGPRX4; itch and sensory perception.
> Opening plan — indication: cholestatic itch; target: MRGPRX4; model: [a cell assay of MRGPRX4 activation, to settle
> with the lab]; positive control: [a known MRGPRX4 blocker, if the research finds one]; negative control: an isotype
> antibody; standard of care: [today's first-line treatment, from the research]; readout: [blocking of receptor
> activation, and how much counts as success]; design: to settle with the lab; time and budget: within 6–12 months and
> $100K–$300K; the lab's tools and models: the three methods above.
> Sources: [PMIDs, NCT numbers and patent numbers, with titles].
> Reply with your own post: what your lab would use, what you would change, and what you cannot do.

### 5.7 The lab's reply (D51, D33)

- **Offered when.** A helper called from `_phase5_new_post`, inside its agent lock, right after the turn's timer is
  stamped and before the daily-cap check, offers the lab its oldest posted project in this run that has no reply,
  while the lab is under `active_thread_threshold`. The random skip does not apply. With no posted project, or no
  database session, it makes no model call, so the phase-5 golden master does not move.
- **One call** with a new pi_lab prompt file, `prompts/phase5-project-reply.md`, given the posting text (fenced). The
  lab cannot decline (D51). Its reply, in its own words and under 1,500 characters: which of its tools, models and
  data it would use, what it would change in the opening plan, and what it cannot do. It claims only what its profile
  and papers show the lab actually used (D33), proposes no other lab and does not commit the PI. The lab's existing
  prompt files are not edited; this file says that here, unlike its own pitches, the idea is Blackbird's.
- **Posting.** The engine adds the header `:test_tube: *Blackbird project:* <title>` and posts it in `#general` with
  `_post_message(..., phase="project_reply", landed_check=True)`. The authorship check applies as to any lab post,
  with its feedback stored under its own key, not the own-pitch key. Before posting, the helper writes a pending claim
  on the project; after posting, it stores the reply's id. On resume, a pending claim is matched to the lab's first
  `project_reply` row after the claim's time, or released, so a crash neither loses the link nor posts twice.
- **Failures.** Only a reply that was not posted — suppressed, blocked by the authorship check, or too long to fit
  one Slack message as Slack receives it (it would be split) — counts. A rate-limit deferral, an API error or a truncated answer is simply tried
  again at the lab's next turn, and the lab's own pitch still runs in that turn, so a reply that keeps failing
  cannot hold up the lab's pitching (my call, from the plan audit). While the run drains (`--max-proposals`
  reached), an API error, a truncated or refused answer and an empty answer count too, because the drain waits
  for the reply and a reply that cannot succeed must not hold the run open; a rate-limit deferral never counts.
  A second counted failure ends the project as not finished ("the lab's reply could not be posted").
- **Its own pitch stays free** (my call, confirmed). `get_agent_top_level_posts` leaves out `project_reply` posts, so
  the daily cap does not count them and the lab's Phase-5 prompt does not list them; `_rehydrate_proposal_count`
  never counts them (they are not `new_post`), so they do not count toward `--max-proposals`. One top-level post per
  turn still holds: if the lab replies, its own pitch waits for a later turn.
- **Where it shows.** Not on Discussions, Interviews or the lab pages; on the Projects page.

### 5.8 Refinement: a project thread in Slack (D45, D50, D51)

- **Which threads are project threads.** A thread whose root log entry has phase `project_reply`. This is set when
  the reply is posted and restored on resume, so it never depends on reading the project row, the kill switch or the
  database. The project row supplies only content (the opening plan and the research); if it cannot be read, the
  turn is skipped and retried, and nothing an interview would produce is written.
- **Turns** (my call; parity-aware, because a split reply shifts ordinals by one):

  | Ordinal of the reply | BlackbirdBot | Lab |
  |---|---|---|
  | up to 4 | open: confirm what the lab has, fill gaps | open: say what it would use and change |
  | 5 to 10 (hub) / 5 to 8 (lab) | refine: test controls, standard of care, feasibility | refine |
  | 11 and later (hub) / 9 and later (lab) | close: must close | final: must start with ✅ or ⚠️ |

  At any turn the lab starts its reply with ✅ only if it agrees the plan as it now stands is feasible and complete.
  When the lab's latest reply starts with ✅, BlackbirdBot may close at its next turn with ✅ if it agrees too; this is
  how refinement ends early on agreement (D45). On its last turn BlackbirdBot must close, with ✅ or ⚠️.
- **The close.** A BlackbirdBot reply that carries a readable `<project_json>` block (stripped before posting) is a
  close: the final plan's fields, the lab's tools and models used, its own position (✅ or ⚠️), the gaps and the
  disagreements. Its visible text starts with ✅ or ⚠️ and states the final plan. The engine then records the outcome
  (§5.9) and closes the thread with a new outcome value, `project_closed`. A reply in the close band without a
  readable block ends the project as not finished ("close unreadable") and closes the thread the same way.
- **BlackbirdBot's prompts there.** A new system prompt, `prompts/roles/scout_hub/agent-system-project.md`, composed
  like today's (identity and working memory, no `{rubric}`), and a new reply template, `phase4-project-reply.md`,
  carrying the opening plan, its anchors and sources, and the choice's reasons (all fenced). Its aim is to turn its
  idea into a plan this lab can carry out with what it has: fill every field of §5.5, test each control and the
  standard of care with the scientific and clinical specialists, press on feasibility (what the lab has used, time,
  budget), and accept the lab's corrections. It never scores, recommends, gates or declines.
- **The lab's prompts there.** Its existing system prompt, and a new reply template, `prompts/phase4-project-reply.md`
  (pi_lab), in place of `phase4-thread-reply.md`: this thread refines Blackbird's idea with the lab; it is not a
  screen of the lab's own work; claim only what the lab has actually used (D33); say plainly what it cannot do;
  propose changes; never close or decline.
- **Guidance and tools by kind.** The guidance for project threads lives in a new module with its own selector; the
  existing `phase4_guidance` and its strings stay byte-identical. In project threads BlackbirdBot's tools are today's
  plus `search_pubmed` and `search_trials` (§5.3); the lab's are its usual ones. The `role.toml` `tools` lists are not
  changed: the extra tools are allowed only in project threads, through a kind-aware tool list and gate.
- **⏸️ in a project thread** never closes it; a lab reply that starts with ⏸️ counts as ⚠️.
- **Switched off in a project thread:** verdict capture and `<assessment_json>`, every assessment drop (empty,
  abandoned, missing sidecar, lab ⏸️), ⏸️ decline rows, the panel floor, and every headline. The capability
  `captures_verdicts` stays true for interviews; the thread's kind gates all of it.
- **Kept out of both bots' working memory and prior-thread lists:** their replies in project threads, the memory
  update queued when a project thread closes, and the closed thread's entry in the list of earlier discussions that a
  lab's Phase-5 prompt shows (also on resume). BlackbirdBot's screening of labs' own pitches is not shaped by its own
  ideas, and a lab does not later pitch Blackbird's project as its own (my calls, from the audit).
- **Logging and statistics.** Replies in project threads are logged with the phase `project_thread` and their
  own stage names; specialist consults keep their `consult_<domain>` phase with the thread's id. The run
  statistics that count interviews (interviews opened, consults per interview, cost per interview) and the lab
  page's thread count leave project threads out; cost by stage shows the project stages under their own names
  (my call, from the plan audit: these read thread decisions and thread ids, not the phase).
- **Load.** Project threads run on the bots' existing rate allowances (`hub_llm_calls_per_window`,
  `llm_calls_per_load_per_window`), which are not raised. Each counts toward both bots' `active_thread_threshold`.
- **Specialists' labels are hidden** (D55). BlackbirdBot still consults the specialists and reads their whole answer,
  and each consult is still recorded, but no panel note is posted into a project thread, and its replies say what a
  specialist found without quoting a `blocking`, `gap` or `adequate` label or a confidence level.

### 5.9 Outcome (D45, D47)

Decided by the engine at the close, from the close and the lab's last position:

- **ready** — the lab's latest reply before the close starts with ✅, the close starts with ✅, the plan is complete
  (§5.5), and the close lists no disagreement;
- **not agreed** — the close was read but any of those fails; the page shows the gaps and the disagreements, and a
  missing field or a missing ✅ is listed as a gap in plain words;
- **not finished** — the project never reached a readable close. Reasons: no idea found; research failed; choice
  failed; the run ended before posting; lab not live; the lab's reply could not be posted; the thread ended without a
  close (time-out, abandoned — including a thread the engine dropped without closing it, for example because its
  first post was deleted in Slack, which counts as abandoned once no bot holds the thread and the lab's reply is
  more than 10 minutes old); close unreadable; the run was
  finalized first.

A stopped or held run keeps its open projects, shown as "paused" until the run resumes or is finalized.

### 5.10 The Projects page (D42, D47, D52)

- **Access.** A new workspace page, `/workspace/projects`, with its own navigation entry, gated by `get_review_user`:
  admins, managers and reviewers. PIs, and an admin impersonating a PI, get 403. It is not on, and does not change, the
  Assessments pages.
- **Discussions stays as it is.** Its list and its run-bar counts leave out threads rooted at a `project_reply` post,
  which today would appear there as orphan threads (§3).
- **The list**, like the Assessments list: lab, project title, outcome, run, when it finished, and its reviews
  (count, mean rating, fund count, and whether the viewer has reviewed it). Filters by run, lab, outcome and "not
  reviewed by me". By default it shows ready and not agreed projects; a filter shows not finished and paused ones with
  their reasons (my call).
- **The details page**, like an assessment's:
  - the title, lab, run, outcome and dates;
  - the final plan in the template, with each tool and model marked "in the lab's profile" or "stated by the lab";
  - both bots' positions, and for not agreed projects the gaps and disagreements;
  - why this use was chosen and what else was considered, in words;
  - the anchors in the lab's profile;
  - the research: each source and the specialist that found it; a PubMed paper links to
    `https://pubmed.ncbi.nlm.nih.gov/<pmid>/` and a trial to `https://clinicaltrials.gov/study/<NCT number>` (the form
    the registry's own sitemap uses, checked 2026-10-26); a patent is listed by its number and title, since nothing in
    the code builds a patent link;
  - the opening plan as posted, beside the final plan;
  - the conversation: the posting, the reply and the thread as a timeline, like the interview timeline, with each
    specialist consult's question and what the specialist found (what is established, the concerns, the questions
    to ask), never its `blocking` / `gap` / `adequate` label or its confidence (D55);
  - the reviews (§5.11);
  - for admins: research and refinement cost, and a link to the thread's model calls (the admin calls page
    gains a thread filter; the lab's reply step, made before the thread exists, is counted in the refinement
    cost but not listed there).

### 5.11 Project reviews (D42, D53)

- A new table, `hub_project_reviews`, separate from `assessment_reviews`. Each review: a 1–5 overall rating (required),
  a comment, and fund or don't fund (required), with its author and times.
- Admins, managers and reviewers can review; one review per person per project, which its author can edit and an
  admin can delete, as with assessment feedback (my call). Writes are refused under impersonation, and the form is
  not shown then. Everyone who can see the page sees the reviews. A review keeps its author's name, so a deleted
  user's reviews stay with the name, as assessment reviews do.
- Nothing else reads them: not Path A, not the assessment review counts, not any bot.

### 5.12 Settings and kill switches

| Setting | Default (my call) | Turns off or sets |
|---|---|---|
| `HUB_PROJECTS_ENABLED` | off | new requests, postings and lab replies (snapshotted at engine start) |
| `PROJECT_RESEARCH_ENABLED` | on | the worker's research and choice |
| `PROJECT_RESEARCH_DAILY_USD_LIMIT` | 25.0 | ceiling for the worker's research and choice calls |
| `LLM_PROJECT_RESEARCH_MODEL` | `claude-opus-5` | the worker's model; must be priced in `src/services/llm_pricing.py` |
| `PROJECT_RESEARCH_MAX_TOOL_ROUNDS` | 8 | tool rounds per specialist session |
| `PROJECT_INTERVAL_HOURS` | 24 | run time between a lab's projects |

### 5.13 Help and docs

`templates/help/` pages for the Projects page and project reviews, what labs see, and the glossary (project,
anchor, opening plan, ready, not agreed, paused); `docs/operations/blackbird-hub.md` for operators; the migration's
deploy note. Help follows `docs/operations/help-content.md`.

### 5.14 Go-live

- Deploy as `CLAUDE.md` says: commit; build the web app and worker images and, since `src/agent/` changes, the agent
  image; migrate from a one-off container off the new image (`./scripts/migrate/run_migration.sh`, rehearsed without
  `--apply` first); bring up the web app and worker; bring up the agent only when `/admin/simulation` shows no live
  run. Never start a run as part of the deploy.
- With `HUB_PROJECTS_ENABLED` still off, run the research dry run (§8) for the live labs and give the owner the
  results to read.
- If the owner agrees, set `HUB_PROJECTS_ENABLED=true` in `.env` and recreate the agent only when no run is live. The
  first run after the release, started from `/admin/simulation`, is FRESH, since both prompt sets are bumped (§6;
  superseded 2026-10-26: neither set is bumped).

---

## 6. The freeze exception this prototype needs (for the owner's approval)

> **Owner, 2026-10-26, after the first release (verbatim):** "those should be frozen and the new system should use
> separate prompt documents". Items 1, 2 and 7 are withdrawn: the four project-thread prompts are their own documents
> in `prompts/projects/` (`hub-system.md`, `hub-thread-reply.md`, `lab-posting-reply.md`, `lab-thread-reply.md`), in
> no role's prompt set; scout_hub stays 1.12.0 and pi_lab 1.2.0, and no golden master moves. The capability flag
> (item 1) stays, in `role_capabilities.py`. Items 3–6 stand.

1. **scout_hub prompt set** → next minor version: two new files, `agent-system-project.md` and
   `phase4-project-reply.md`, and their entries in `role_capabilities.py`'s prompt-file tuple; the new capability
   flag for project postings. No existing hub prompt file changes; `role.toml`'s `tools` and `post_types` do not
   change.
2. **pi_lab prompt set** → next minor version: two new files, `phase5-project-reply.md` and
   `phase4-project-reply.md`, and their tuple entries. No existing lab prompt file changes.
3. **Code-built prompt text**, new or present only in project threads, existing text byte-identical: the project
   guidance and its selector; the `<project_json>` instructions; the extra tool definitions for BlackbirdBot in
   project threads; new fence tags in `prompt_safety.FENCE_TAGS`; and the worker prompts
   `prompts/projects/research.md` and `prompts/projects/choose.md`.
4. **Rubrics:** none changes; the choice reads five dimensions' anchor text from the investment rubric.
5. **Bot behaviour on the normal path:** BlackbirdBot posts projects to labs and refines them; labs reply and refine; a
   lab that replies can still make its own pitch that day. Labs' own pitches and their interviews and assessments are
   unchanged.
6. **`#assessments-summary`:** unchanged; projects post no headlines.
7. **Golden masters:** two reviewed regenerations in `test_prompt_freeze_gm.ambr`: `test_run_start_marker_gm`
   (`tests/characterization/test_prompt_freeze_gm.py:1134`; the two prompt-set versions and hashes) and
   `test_review_bot_request_over_a_superseded_verdict_gm` (`:595`; the embedded `role.toml` files). Every other entry,
   and `test_agent_turn_gm.ambr`, stays byte-identical; the phase-5 golden master
   (`test_phase5_pitch_with_live_cohort_gate_and_prior_threads_gm`, `:442`) passes only because the reply helper
   makes no call without a posted project (§5.7), and the hub's interview requests keep their tool list because the
   extra tools are allowed only in project threads (§5.8). If the plan finds another entry that must move, it comes
   back to the owner.

Both prompt-set bumps run `scripts/sync_prompt_set_docs.py`, and the first run after the release starts FRESH.

---

## 7. Risks, and what the prototype does about them

- **PR1 Plausible but wrong science.** A plan can pair the wrong control with a target or misstate a standard of
  care. *Prototype:* research reads retrieved sources; BlackbirdBot consults the scientific and clinical specialists
  on controls and the standard of care in refinement; the lab corrects what it knows; staff judge with the sources
  in front of them.
- **PR2 Invented citations.** *Prototype:* a cited identifier must have come back from a tool call in that session
  (§5.3); the page links every source.
- **PR3 An idea the lab cannot do.** *Prototype:* anchors must be whole profile items, at least one a method or
  model; D33 in the reply and refinement; "stated by the lab" marks on the final plan.
- **PR4 Two copies of one model agree too easily.** BlackbirdBot refines its own idea with a bot on the same model,
  and both may favour text like their own (§1). *Prototype:* agreement only records feasibility and completeness;
  completeness is checked in code; worth is judged by staff (D43); the share of ready projects and the staff ratings
  show whether agreement means anything.
- **PR5 Labs must refine ideas that fit badly** (D51). *Prototype:* a poor fit ends not agreed with its gaps, which
  is itself information; the measures show how often.
- **PR6 Cost.** Research with tool calls is the most expensive worker job. *Prototype:* one call per idle slot, a
  per-call ledger under the daily ceiling, the round limit, costs stored per project, and the dry run before go-live.
- **PR7 BlackbirdBot's load.** One more thread of up to 12 messages per lab per day on the same rate allowances.
  *Prototype:* allowances not raised; interviews may slow; the switch stops new projects.
- **PR8 Public posting.** Postings are in `#general`, which every lab and workspace member reads. *Prototype:* built
  from public profiles and public databases only; assembled from checked fields; names only its lab; no `@`.
- **PR9 Repeats.** *Prototype:* earlier projects for the lab are passed to research and choice.
- **PR10 Thin profiles.** A lab with short lists gives research little to anchor on. *Prototype:* "no idea found" is
  recorded with the dropped candidates; anchor use per section is measured; the methods lists of D34 come later.
- **PR11 The run ends mid-project.** *Prototype:* paused while stopped, continued on resume, not finished once
  finalized.
- **PR12 Split replies shift turns.** *Prototype:* the parity-aware turn bands of §5.8; tested at both parities.
- **PR13 A lab re-pitches Blackbird's idea as its own.** *Prototype:* project threads stay out of the lab's memory,
  its Phase-5 recent posts and its list of earlier discussions.
- **PR14 A shortcut becomes permanent.** *Prototype:* §12.

---

## 8. Verification

- **Tests:**
  - due labs: live only, one open project per lab, the run clock across a stop and a resume (capped additions), a lab
    activated or unmuted mid-run, no requests while draining, a posted project whose lab left the roster, the unique
    slot, compare-and-set transitions;
  - research: inputs limited to the public profile; persona sections cut by heading, and a missing heading; fences;
    one call per idle slot and the stored conversation; the round limit and the tool-less final call; tolerant
    parsing; the anchor check (normalisation, whole items, the method-or-model requirement); the source check; PubMed
    unavailable; the per-call ledger, the ceiling and waiting; transient errors not counted; three failures; refusals;
    a restart mid-step; only the live run's projects;
  - choice: no score fields accepted or stored; the name rule (the examples in §5.4), `@`, length limits; one retry;
    no candidate;
  - posting: assembly, fitting one Slack message, at most five sources, header, phase, the posting claim, the authorship-check skip, the lab no longer live,
    BlackbirdBot off the roster, Slack-on and Slack-off paths; the posting and its thread excluded from every
    `MessageLog` list read, `_restore_active_threads`, working memory and `_rehydrate_proposal_count`;
  - the reply helper: no call without a posted project; the pending claim and its resume; which failures count; the
    one-message limit; its own feedback key; `get_agent_top_level_posts` leaving replies out (daily cap and
    Phase-5 recent posts) on a live run and after a resume; `--max-proposals` not counting them; one top-level post per
    turn; the skip streak and timer untouched;
  - project threads: kind from the root entry's phase, after a resume and with the switch off; the turn bands at both
    parities; early close after a lab ✅; the close at the last turn; an unreadable close; ⏸️ ignored; the hub's and
    the lab's project prompts and tools, and the gate refusing the extra tools in interviews; nothing an interview
    produces (assessment, drop, decline, panel floor, headline); memory left out; the `project_thread` log phase;
    `<project_json>` stripped by `_post_message`;
  - outcome rules: ready, not agreed with listed gaps, each not-finished reason, paused, finalized;
  - pages and reviews: the access matrix, impersonation, one review per person, edit and delete, separation from
    assessment reviews; Discussions leaving project threads out of its list and counts;
  - structure: `_handle_reply_response` and `_phase5_new_post` within the size and complexity gates (project branches
    extracted), the engine import-graph and unit-ownership pins, the role prompt-tuple pins, the `MessageLog` read
    classification, and the migration pins (preflight, postflight, harness);
  - the two golden-master diffs and no other.
- **Dry run before go-live:** a script runs research and choice for each live lab without writing project rows or
  posting, under its own dollar cap, and writes each candidate, its anchors and sources, the choice and its reasons,
  the opening plan, the posting text and the cost to a file outside the checkout, for the owner to read. Then one or
  two project threads are replayed offline on a seeded stack to confirm the closes and the outcome rules.
- **Browser:** the Projects list and details pages and a review (add, edit, delete) through `/lathe:browser-testing`
  on a seeded stack.

---

## 9. Recommended build order

1. Migration: `hub_projects`, `hub_project_calls`, `hub_project_clocks`, `hub_project_reviews`, and the
   `project_closed` outcome value (additive; migrated before the code that uses them).
2. Search tools and the worker's research and choice, with the dry-run script.
3. The Projects pages and reviews, on seeded data, and the Discussions exclusion.
4. The engine: the run clock, due labs, postings and their exclusions, the reply helper and the counter change.
5. Project threads: kind, turn bands, prompts, guidance, tools, the close and the outcome; switching off capture,
   drops, declines, headlines and memory there.
6. Help, docs and go-live (§5.14).

---

## 10. Calls made in this draft (check while reading)

- A day is 24 hours of run time, kept by a new run clock that counts only running time; at most one open project per
  lab; a lab with no project in this run is due at once (confirmed in playback, with the details here mine).
- Research by five specialists (clinical, scientific, technologic, commercial, legal), one after another, one model
  call per worker idle slot, up to 3 candidates each and 8 tool rounds; inputs are the public profile only; each
  session uses a research frame plus the persona's two domain sections (confirmed in playback, with the numbers mine).
- Anchors are whole profile items, at least one a method or model; citations must come from the session's own tool
  results.
- PubMed unavailable delays the research; the other sources may be missing.
- The choice uses five rubric dimensions' anchors and writes reasons in words, with no score stored (confirmed in
  playback).
- The posting is assembled by code from checked fields: `#general`, a fixed header, one Slack message,
  sources by identifier and title, no author names, no `@`; names are matched case-sensitively on full names and
  "<surname> lab"-style phrases.
- The lab's reply does not use up its own pitch and does not count toward `--max-proposals`; replies and postings
  get their own phases (`project_reply`, `hub_project`) rather than `new_post`; only posting failures count toward the
  reply's two tries, except while the run drains (`--max-proposals` reached), when an API error, a truncated or
  refused answer and an empty answer count too (§5.7); a reply that fails without counting leaves its turn to the
  lab's own pitch.
- Refinement is at most 12 messages with parity-aware turn bands; it ends early when the lab starts a reply with ✅
  and BlackbirdBot agrees; agreement is read from ✅ or ⚠️ at the start of the lab's latest reply and of the close;
  completeness is checked in code; ⏸️ never closes a project thread; a new thread outcome, `project_closed`.
- No headlines for projects; project threads stay out of both bots' working memory and the lab's Phase-5 recent posts.
- The page lists ready and not agreed projects by default, with a filter for not finished and paused ones; it lives
  at `/workspace/projects`; Discussions leaves project threads out.
- One review per person per project, editable by its author and deletable by an admin; reviews visible to all page
  users.
- `HUB_PROJECTS_ENABLED` off by default, turned on after the owner reads the dry run; a $25 daily ceiling.
- The lab is recorded by its agent id, not a foreign key, so deleting a user is never blocked.

## 11. Open questions for the owner

None. The one question this revision raised (the specialists' labels) was answered as D55.

## 12. How it grows into the full design

| Prototype | Full design | Where |
|---|---|---|
| Ideas from literature research on each lab's profile | Ideas from staff reviews, interests and funded projects | D1, D10–D12 |
| One project per live lab per day | Ideas pitched to chosen labs (D37, D39); research could become the matching step | §5.7 there |
| Refinement to agreement, no scores; staff judge | Answers assessed on fit | D13, D14, §5.10 there |
| Project reviews (1–5, comment, fund) on their own | Learn reviews that feed ideas and lessons; project reviews could be read the same way | D1, D8, D9 |
| Profiles as they are | Equipment, models and techniques from methods, for better anchors | D6, D28, D34 |
| No approval | Automatic pitching after checks | D7 |

The project row keeps its sources, anchors and outcome, so a later version can turn a ready, well-reviewed project
into a full-design idea without rework.

---

## Appendix A. Evidence

| Item | Value |
|---|---|
| Live labs on 2026-10-22 | 4 active and unmuted: gray, deutsch, hu, menard (production, read-only) |
| Gray profile, the example's basis | Key Methods and Technologies (26 items) and Model Systems (20 items), `profiles/public/gray.md:11-61`; "itch and sensory perception" among its disease areas and MRGPRX4 among its targets (`:63-90`) |
| Jason's template | indication, gene/target, model, positive and negative controls, readout, design (slide 4); standard-of-care controls (slide 2); $100–200K (slide 5); refinement with the lab bot (slide 5) |
| Fundable range | $100K–$300K over 6–12 months (`prompts/rubric/blackbird-rubric.toml:299`) |
| Self-preference | Panickssery, Bowman & Feng, "LLM Evaluators Recognize and Favor Their Own Generations", NeurIPS 2024 (arXiv:2404.13076); Wataoka et al., "Self-Preference Bias in LLM-as-a-Judge", 2024 (arXiv:2410.21819) |
| Model and price | `claude-opus-5` (`src/config.py:338`); $5 / $25 per million input / output tokens (`src/services/llm_pricing.py:29`) |
| Interview length | 12 messages (`src/agent/engine/constants.py:123`); EXPLORE ≤4, DECIDE ≤11, CONCLUDE after (`src/agent/thread_guidance.py:207-219`); split replies count per chunk (`src/agent/message_log.py:441-458`) |
| Lab pitch caps | `lab_daily_post_cap` = 1 (`src/config.py:448`); `active_thread_threshold` (`:430`); `_count_today_posts` (`src/agent/engine/scheduler.py:164-182`) |
| Search code | PubMed ESearch `search_pmids` (`src/services/pubmed.py:808-830`); ClinicalTrials.gov v2 `query.term` (`src/services/industry_sources/ctgov.py:9-58`); USPTO title search (`src/services/patents.py:669`) |
| Audit of revision 4 | 2026-10-26, one adversarial pass with a builder check: 4 fact errors, 3 high, 11 medium, 8 low, 18 builder questions; all folded in or settled in §10; the owner's question answered as D55 |
