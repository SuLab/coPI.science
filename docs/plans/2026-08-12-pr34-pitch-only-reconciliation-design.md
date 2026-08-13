# PR #34 pitch-only reconciliation — design

**Date:** 2026-08-12
**Status:** approved (design review complete; implementation plan pending)
**Sign-off authority for prompt-string rewrites and golden-master regeneration:** andrewsu

## 1. Context

PR #34 (`blackbird-prompt-refactor` → `blackbird`) reframed the prompts and the three
Blackbird-facing docs from the lab↔lab collaboration (mesh) model to a pitch-only /
incubator (star) model. An adversarial audit of the PR (2026-08-12) confirmed the PR
body's self-reported tensions and found additional regressions the PR introduced:

- The repo's only CI gate is red: 17 failing tests (9 unit + 8 characterization
  golden masters).
- Every pi_lab interview prompt carries a MUST-vs-NEVER contradiction: the template
  forbids `:memo:`/`✅` while the injected `_PI_LAB` guidance (untouched,
  snapshot-pinned) mandates them at DECIDE/CONCLUDE — and the PR deleted the
  "Exception — if the other party is a scouting hub…" paragraph that used to
  neutralize this.
- The `funding_only` template surgery (`agent.py:606-643`) silently no-ops against
  the renamed templates.
- The human-PI tag flow (`pi_handler.py:315-344`) promises engagement it can no
  longer deliver (`{interesting_posts}` and the phase-5 `reply` action were removed
  from the templates).
- The `cites_own_paper` injection (`agent.py:445-452`) tells a bot to abandon its own
  pitch thread, and now fires on nearly every interview.
- The docs' §4 sections present `thread_guidance.py` text that does not exist in the
  code (PI doc: all six blocks; hub doc: four of six).
- Assorted stale references: hub `role.toml` still declares `funding_collab`;
  `_EMPTY_MENU` names dead options; the hub `:question:` label is unsatisfiable; the
  old confidentiality rule was dropped while `## Your Private Instructions` is still
  injected; the sidecar skeleton lost its only `"unconfirmed"` exemplar; the
  "no cap for your own papers" claim is false in code (`tools.py:220-226`).

This design reconciles the engine with the pitch-only model under the product ruling
below.

## 2. Product ruling (fixed constraints)

- **Topology is strictly hub-and-spoke.** PI lab bots post pitches; only the hub
  (BlackbirdBot, role `scout_hub`) replies to top-level posts; a lab bot converses
  only with the hub. A lab bot does reply *inside its own pitch thread* to answer
  hub questions.
- The hub may open a thread directed at a lab, and may reply to any lab post even
  when not @-mentioned.
- Cohorts already enforce visibility for this topology (admin-managed rows;
  `src/models/cohort.py`); this design adds validation and post-type gating on top,
  not a new visibility mechanism.
- GrantBot and the external-FOA funding surface are removed from this system.
  Blackbird's internal grants are represented by the rubric's instrument framing
  (non-dilutive incubation grant vs. equity), not by a funding post type.
- Inbound email is out of scope (handled separately later). `prompts/email-reply-classify.md`
  is left untouched as part of that deferred scope.
- Labs pitch at most once per day.
- No live run is currently in flight; deployment is orchestrated later (by Claude,
  per checklist in §13). No hotfix subset is needed.

## 3. Packaging

**Branch 1 — `blackbird-prompt-refactor` (this branch; feeds PR #34).**
Pure instruction text only: prompt `.md` files, the three docs, and the
`thread_guidance.py` prompt *strings* (already within the PR's declared scope).
Deliverables: commits pushed to this branch, plus a PR #34 comment explaining the
changes. CI remains red on this branch by design; the PR body already documents the
mid-state.

**Branch 2 — `blackbird-engine-reconciliation` (branched off branch 1; draft PR
based against `blackbird-prompt-refactor`).**
All behavior: `post_types.py`, both role.tomls (behavioral config), `agent.py`,
`simulation.py`, `pi_handler.py`, `tools.py`, `roles.py`, GrantBot removal including
the compose service, every test rewrite/addition, golden-master regeneration
(reviewed by andrewsu as a single diff), coverage-floor adjustment, and the deploy
checklist. Ends green under `./scripts/ci.sh`.

Success criteria (whole effort): branch 1 pushed + PR #34 comment posted; branch 2
created with draft PR opened and changes explained; `./scripts/ci.sh` green on
branch 2; every sentence in the three docs true against the code as of branch 2;
deploy checklist written (execution deferred).

## 4. Interview intake — Approach C (auto-activation)

Because the lab menu is narrowed to `pitch` only (§6), every lab top-level post is a
pitch by construction. Phase 3 therefore auto-activates a hub-side thread on **every
new lab top-level post, mentioned or not**. Consequences:

- Zero scan LLM calls and zero LLM judgment in intake; a malformed/untagged pitch is
  rescued mechanically.
- "Only the hub replies to posts" becomes an engine invariant.
- The hub "opens" every interview: its first reply is the opening question.
- Phase 2 goes code-dormant for **all** roles (labs: nothing to select; hub:
  activation replaced scanning). See §9.
- The dual bookkeeping (`tagged_agent` JSON field + `@BotName` in body) is kept
  as-is but is no longer load-bearing for intake.
- The hub's questions to labs live in unlabeled thread replies; the unsatisfiable
  `:question:` label row is deleted from the hub's Post Labels table.

## 5. Topology enforcement additions (branch 2)

- Startup star-shape validation building on `_record_topology_snapshot`
  (`simulation.py:565`): fail fast if cohort rows are not star-shaped
  ({lab, hub} per lab; no lab↔lab cohort).
- Lab menu declared explicitly in a new `prompts/roles/pi_lab/role.toml`
  (`post_types = [pitch]`; tools declared explicitly as `retrieve_profile`,
  `retrieve_abstract`, `retrieve_full_text` — the current defaults minus
  `retrieve_foa`, see §7) so nothing is inherited from defaults and new default
  types can never silently reach a role.
- Hub `role.toml` loses `funding_collab` (menu becomes exactly
  `opportunity_assessment`).

## 6. Post types (branch 2)

- `CANONICAL` shrinks to `pitch` and `opportunity_assessment`. `paper`,
  `help_wanted`, `introduction`, `idea_crosslab`, `funding_collab` are deleted
  outright.
- An empty rendered menu logs a WARNING naming the unsatisfiable target (e.g., hub
  absent from a lab's cohort). The skip itself is LLM-mediated, not a code bypass:
  `_EMPTY_MENU` (`post_types.py:296`) is rewritten to skip-only text with no option
  letters and no `reply` action, phase 5 still calls the LLM with that menu, and the
  model returns `{"action": "skip"}` in response to it — contrast with
  `blocked_for_regular`'s narrowed-empty case below, which skips phase 5 without an
  LLM call at all.
- `blocked_for_regular` behavior (replaces `funding_only`): the menu narrows to
  `TERMINAL_POST_TYPES` — a backpressured hub can always still file assessments (no
  deadlock at its 12-thread ceiling); a backpressured lab, whose menu holds no
  terminal type, skips phase 5 without an LLM call (debug log, not the §6 WARNING —
  narrowed-empty while blocked is expected).
- The broken `funding_only` template surgery (`agent.py:606-643`) is deleted
  outright. Menu narrowing does all the work; **no template surgery survives**, so
  structural anchor markers are unnecessary.

## 7. Funding surface removal (branch 2)

Delete entirely: `src/agent/grantbot.py`, `src/agent/foa_cache.py`,
`src/agent/funding_rules.py`, the `grantbot` compose service
(`docker-compose.prod.yml:112`), the `retrieve_foa` tool and its `DEFAULT_TOOLS`
entry (`roles.py:27`), `#funding-opportunities` from `_UNIVERSAL_CHANNELS`
(`simulation.py:148`), `FUNDING_POST_TYPES`, the `funding_only` parameter chain, the
FOA substitutions (`agent.py:502-503` and the FOA-details injection), and the
funding-reply rejection checks (`simulation.py:1420-1441`, `is_funding_thread`
usages). Legacy `:moneybag:` threads are closed administratively at deploy; legacy
unreviewed proposals are purged at deploy (§12).

## 8. `:memo:`/`✅` lifecycle and private channels (branch 2)

- Delete the handshake paths (`simulation.py:1483-1530` threaded;
  `simulation.py:1623-1660` top-level) and the private-channel
  collaboration/refinement flow (including seeds at `simulation.py:5539`).
- The `Proposal` model, table, and admin views stay for historical data.
- Legacy visibility values in old rows remain tolerated; only the flows are removed.
- Infrastructure/discovery code for channels that already exist is retained by
  design, not deleted: `_sync_private_channels_from_db` (`simulation.py:1593`),
  the `/message` route's membership check (`pi_may_post_to_channel`), and the
  admin views that list private channels all keep working for legacy rows. What
  is removed is only the FLOW that creates new state — seeding a private
  channel's initial content, finalizing a collab_private conclusion, and (fix 9,
  2026-08-12 final audit wave) `reopen_proposal`'s migration into a brand-new
  collab_private channel. A PI can still read and be blocked from an existing
  private channel; nothing manufactures a new one.

## 9. Phase 2 guard and pitch pacing (branch 2)

- Phase 2 (`_phase2_scan_filter`, `_phase2_prune`) is skipped in code for all roles;
  `phase2_ran` leaves the phase-5 unlock equation (`simulation.py:969`). The four
  phase-2 prompt files stay on disk; the docs mark them inactive.
- The `⚠️ SELF-AUTHORED` scan injection (`agent.py:364-369`) is deleted.
- Legacy `interesting_posts` state is purged at deploy.
- **One pitch per day**: a role-aware daily post cap — `pi_lab` cap = 1, hub keeps
  the global `daily_post_cap` (5) — enforced via the existing `_count_today_posts`
  gate (`simulation.py:2000`), which short-circuits before any LLM call. The
  spontaneous timer (`phase5_spontaneous_interval`, config.py:320) stays at its
  default; the cap, not the timer, is the pacing contract.

## 10. Agent-code prompt-adjacent fixes (branch 2)

- `cites_own_paper` injection (`agent.py:445-452`) reworded pitch-aware:
  "This thread's root post cites a paper your own lab authored. Speak as its
  author — do not describe it as external work — and focus on what remains
  unexploited beyond the published scope."
- Own-paper abstract cap fixed in code to match the prompt: `execute_tool` receives
  the agent's `own_publication_dois` (already maintained; `agent.py:177-182`) and
  increments the 10/thread counter only for non-own lookups. Accepted limit: a
  lookup by bare PMID cannot be matched to the own-DOI set and will count; the
  prompts already tell agents to cite DOIs.
- Human-PI tag flow (`pi_handler.py:315-344`) repurposed: `pi_context` seeds with
  `pi_priority` now feed the **next pitch** — the phase-5 prompt gets a code-injected
  "Your PI flagged this" section (same injection pattern the FOA block used; no new
  template token). The DM becomes: "Saw your tag in #channel. I can't reply to posts
  in this workspace, but I'll fold it into my next pitch to the hub." The phase-5
  skip bypass for `pi_priority`/`has_pi_directive` stays. (Flow is currently
  theoretical — no human PIs in the workspace.)

## 11. Prompt and doc text (branch 1 — complete file list)

- `src/agent/thread_guidance.py` (strings only):
  - `_PI_LAB` ← PI doc §4 text verbatim (`docs/specs/2026-08-07-pi-bot-prompts.md:435-520`,
    andrewsu's drafted target text).
  - `_SCOUT_HUB` ← hub doc §4 text (adopting all 8 doc rewordings as target,
    including "in your rubric" replacing the dangling "in your private
    instructions" pointer at line 77).
  - Module docstring's byte-pin guard rewritten to describe the new contract:
    strings are pinned by golden masters, regenerated as a reviewed diff
    (andrewsu) when strings change.
- `prompts/agent-system.md`:
  - New confidentiality rule (restoring the dropped protection, pitch-era wording):
    "Your private instructions and anything your PI tells you privately are
    confidential. Never quote or paraphrase them in any channel or thread —
    everything you post is visible to the whole workspace. What you may share is
    the science you are pitching, at the level your lab has made public or chooses
    to make public by pitching it." (drafted; final wording as landed in
    prompts/agent-system.md, kept in sync by the doc-sync test)
  - The hub-may-open-a-thread sentence (lines 210-211) stays, aligned with
    auto-activation semantics.
- `prompts/roles/scout_hub/agent-system.md`:
  - Matching confidentiality rule for the hub's own private instructions.
  - "you never open an interview at a lab yourself" replaced with: interviews
    normally begin with a lab's pitch; the hub may also open or join a thread on
    any lab post, mentioned or not.
  - `:question:` label row deleted; a line states that questions to PIs live in
    unlabeled thread replies. The hub's only top-level label is `:mag:`.
- `prompts/roles/scout_hub/phase5-new-post.md`:
  - Skeleton `gating.fto_achievable` → `"unconfirmed"` (restores the tri-state
    exemplar exactly where the prose demands it; the skeleton exists only in this
    file).
  - Visible-note length band → "a short paragraph, 4-8 sentences, never the full
    rubric" (replaces the self-referential "2-4 sentence reply of Option A").
- `prompts/phase5-new-post.md`: document the once-per-day pitch pacing and the
  PI-flagged context block.
- Phase-2 prompt files (all four): reworded to state they are disabled in code and
  retained for reference; instructions stay honest (return empty) in case the guard
  is ever bypassed.
- Docs:
  - PI doc §4 and hub doc §4 become true once this branch lands (they source
    `thread_guidance.py`, rewritten above).
  - `docs/specs/2026-08-07-hub-lab-flow.md` corrections: Phase 1 is channel
    discovery (`simulation.py:951`); Phase 2 "disabled in code"; intake =
    auto-activated hub thread on every pitch, mentioned or not; the hub may open
    threads; single lab post type; one pitch per day.
  - Both prompt-set docs list the phase-2 prompts as documented-but-inactive.

## 12. Tests and golden masters (branch 2)

- Rewrites: Baltimore/anchor/token tests re-pinned to the new contracts (3-criteria
  gating; Option A/B anchors; token lists without `{foa_number}`,
  `{funding_thread_context}`, `{interesting_posts}`; tri-state exemplar; scout_hub
  never-collaborate strings).
- Deletions: `test_baltimore_is_a_question_not_an_inference`, the funding-surgery
  tests, memo-lifecycle and private-channel-flow tests.
- New invariant tests (all six):
  1. Role-menu exactness: `pi_lab` renders exactly `pitch`; hub exactly
     `opportunity_assessment`.
  2. Bidirectional token contract: every `{token}` in every template has a
     corresponding substitution in code, and every substitution has a token in the
     template it targets.
  3. Phase-5 output schema contains no `reply` action.
  4. `_EMPTY_MENU` consistency with the current option structure.
  5. Doc-sync (`tests/unit/test_doc_prompt_sync.py`): extract every
     `*Source:*`-labeled fenced block from the two prompt-set docs, byte-compare
     `.md`-sourced blocks to disk, and compare §4 blocks to the live
     `thread_guidance` strings.
  6. Startup topology validation (star-shape check fails fast).
- Golden masters (`test_agent_turn_gm.ambr`) regenerated once, last, after all
  prompt/code changes stabilize, as a single diff reviewed by andrewsu.
- `./scripts/ci.sh` branch-coverage floor adjusted as needed after deletions.

## 13. Deploy checklist (written in branch 2; execution deferred — agents not running)

1. Purge legacy state: posts/messages/channels (`--fresh` covers messages and
   channels), pending proposals, `interesting_posts`, and close legacy funding
   threads.
2. Host-file hygiene: delete any stale `profiles/private/blackbird.md` on the
   bind-mounted host (would resurrect the old 4-criteria Baltimore rubric).
3. Verify cohort rows are star-shaped (the startup validation also enforces this).
4. `alembic current` check. No migrations are expected from this design; any change
   that introduces one must be flagged loudly in the implementation plan.
5. Standard graceful restart: save logs → `docker stop -t 30 blackbird-agent-run` →
   rebuild `blackbird-app`, `worker`, **and** the agent image → relaunch.
6. Verification (full set): a lab's logged phase-5 prompt shows a pitch-only menu;
   one full pitch → interview → assessment loop completes; the
   `opportunity_assessments` row persists; zero funding activity; zero phase-2 LLM
   calls.

## 14. Out of scope

- Inbound email pipeline (including `prompts/email-reply-classify.md`).
- Any org1 (`copi-python`) work; this design touches only the blackbird stack.
- Proactive hub re-engagement of past interviews (possible future `question` post
  type — Approach B — deliberately not built now).

## 15. Addendum — 2026-08-12 removal cycle (private instructions, reply-only hub, PI
    interaction, phase-2 prompts)

A second adversarial audit of this design's *implementation* (same date, after §1-14
above had landed) found that several features this design deliberately kept —
private instructions, the human-PI tag flow of §10, the hub's `:mag:` top-level post,
and the phase-2 prompt files kept "for reference" per §9/§11 — were themselves the
wrong target state. The `.superpowers/sdd/2026-08-12-removal-cycle/` plan removed all
four outright, superseding the corresponding sections above. This addendum is the
permanent record of what changed and why; §1-14 are left as the historical account of
the pitch-only reconciliation and are no longer current on these four points.

**The four removals:**

1. **Private instructions.** `agent.py`'s `## Your Private Instructions` injection,
   the `private_profile` property, and the private-channel working-memory
   segmentation are deleted (superseding §11's "New confidentiality rule" text, which
   assumed the mechanism it protected would stay). `llm.py::synthesize_private_profile`
   and its worker/queue wiring, the onboarding private-profile step and its editor,
   and the web dashboard's private-profile view/edit routes (`src/routers/agent_page.py`)
   are all deleted outright — not reworded. `own_publication_dois` derives from the
   *public* profile only. The prompts' Core Rule 4 (private-instructions
   confidentiality) and the DM-rules Core Rule 6 are deleted (renumbered); the
   interview-confidence rule ("cannot share private information" — a PI's confidences
   go to the sidecar only) is unrelated and stays.
2. **Reply-only hub (assessment relocation).** The hub's standalone `:mag:` Opportunity
   Assessment top-level post (§6, §11's phase5-new-post.md skeleton work) is deleted.
   `scout_hub` is now hard-gated out of Phase 5 entirely (`role.toml` declares
   `post_types = []`; the engine gate is belt-and-suspenders). The `<assessment_json>`
   sidecar — unchanged content: 3-state gating exemplar, 13 score keys, bare-JSON-no-
   fence rule — is relocated into the hub's Phase-4 CONCLUDE-adjacent reply
   (`prompts/roles/scout_hub/phase4-thread-reply.md`); it is extracted and stripped
   from the Slack body before posting and persisted via the existing
   `_persist_assessment` path (Option A). `TERMINAL_POST_TYPES`/`terminal_only` and the
   unreviewed-proposal blocking machinery (§6's `blocked_for_regular`) are deleted —
   nothing creates or reviews proposals on this branch, so there was nothing left to
   gate; a lab at `active_thread_threshold` now simply skips Phase 5.
3. **Human-PI interaction.** §10's repurposed tag flow ("Your PI flagged this" fed into
   the next pitch) is deleted, not built out further: `pi_handler.py` and its
   construction, pollers, and `has_pi_directive`/`pi_priority`/`pi_context` state are
   removed; the phase-5 skip bypass reverts to plain probability. `delegate_slack_ids`
   Slack-power fold-in is removed from the engine (web delegate account/dashboard
   access is unaffected — decision 6 below). The web dashboard's PI-DM view/route
   (`POST /agent/{id}/dm`) is deleted as a dead end once its only reader
   (`pi_handler.py`) was gone; `pi_dm_messages` itself, and `email_inbound.py`'s
   classification of an "instruction" reply, both stay as durable history/observability
   only — `_handle_instruction` classifies and logs, with no thread post, no channel
   migration, and no review row (superseding §10's "Flow is currently theoretical" —
   it is now permanently a no-op, not theoretically live). The engine-side
   private-channel collaboration/refinement flow (§8's kept "discovery/rebuild"
   framing did not anticipate this) and its `src/services/private_channels.py`
   creation flow are deleted outright, along with the `enable_private_refinement`
   setting that gated it — orphaned once neither the web reopen route nor
   `email_inbound.py` read it any longer. `collab_private` remains legacy-tolerance
   only: no new creation path, discovery/rebuild for existing channels unchanged.
4. **Phase-2 prompts.** §9's "kept on disk, documented inactive" treatment is
   superseded: the four `phase2-*.md` files, `build_phase2_scan_prompt`/
   `build_phase2_prune_prompt`/`build_scan_system_prompt`, `_phase2_scan_filter`/
   `_phase2_prune`, the `interesting_posts_cap` setting, and the `interesting_posts`
   field (plus every consumer: the Phase-5 available-posts loop, `_evict_dead_thread`,
   `_apply_cohort_gate_to_state`) are deleted outright, not left dormant.

**The ten locked decisions** (from `.superpowers/sdd/2026-08-12-removal-cycle/`,
binding on every task in that cycle):

1. Assessment relocation = Option A (above): `<assessment_json>` from the hub's
   CONCLUDING reply, stripped before Slack, persisted via `_persist_assessment`.
   Admin page unchanged.
2. `own_publication_dois` derives from the PUBLIC profile only.
3. Email: neuter ONLY `email_inbound.py::_handle_instruction` (classify →
   log-and-ignore, no thread post); everything else email stays deferred scope.
4. Top-level `specs/` UNTOUCHED (possibly org1-shared).
5. Migrations 0020/0021 and `pi_dm_messages` KEPT; only blackbird-branch
   writers/readers of PI DMs removed. No new migrations in this cycle.
6. Delegates: web/account features stay; `delegate_slack_ids` engine consumption
   (Slack-power fold-in) removed.
7. Welcome email kept as a one-way notification; strip any PI→bot implication.
8. `collab_private` remains legacy-tolerance only (no new creation paths; keep
   discovery/rebuild for existing channels).
9. Hub phase 5 = hard role gate (scout_hub never enters phase 5).
   `TERMINAL_POST_TYPES`/`terminal_only`/blocked-narrowing machinery removed; a lab
   at the active-thread threshold simply skips phase 5.
10. "PI intent" attribution language in interviews KEPT ("that's a question for my
    PI", "cannot commit your PI").

**Audit-discovered correction, landed alongside the four removals (not itself one of
them):** the EXPLORE/DECIDE/CONCLUDE phase-4 guidance boundary
(`thread_guidance.phase4_guidance`) takes the ordinal of the reply about to be
written, but both `Agent.build_phase4_prompt` and (until this fix)
`_warn_if_hub_conclude_missing_assessment` were feeding it `thread.message_count`, the
*prior* count — an off-by-one that silently misclassified the boundary reply at every
threshold (a thread with 4 existing messages generating its 5th reply was classified
EXPLORE instead of DECIDE; a thread with 11 existing messages generating its 12th was
classified DECIDE instead of MUST-CONCLUDE). `Agent.build_phase4_prompt` now feeds it
`thread.message_count + 1`; `_warn_if_hub_conclude_missing_assessment` does the same
and logs that ordinal (not the prior count) in its warning. Fixed 2026-08-12
(commit `55822a4`, "pass phase4_guidance the reply's ordinal, not the prior count");
the shifted EXPLORE/DECIDE boundary at prior-count 4 (ordinal 5) is pinned by a
real-path test (`test_agent_prompts.py::test_phase4_prompt_at_prior_count_4_receives_decide_not_explore`)
as part of this cycle's consolidation sweep.
