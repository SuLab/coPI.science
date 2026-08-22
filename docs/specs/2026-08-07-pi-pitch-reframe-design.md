# Blackbird star topology: PI bots pitch, the hub screens

**Status:** PROPOSED. Nothing in here has been applied.
**Written:** 2026-08-07, against `blackbird` @ `2e68d64`. **Revision 3.**

**Companion documents — these hold the actual prompt text:**

- `docs/specs/2026-08-07-pi-bot-prompts.md` — every `pi_lab` prompt, in full
- `docs/specs/2026-08-07-hub-bot-prompts.md` — every `scout_hub` prompt, in full

This document is the *why*: the decisions, the findings behind them, the code changes, and
the order of work. It deliberately does **not** duplicate the prompt text — three copies
would drift. Revisions 1 and 2 carried full prompt replacements here; those have moved to
the companion documents and this file was rewritten around them.

---

## 1. The decisions

| # | Decision | Consequence |
|---|---|---|
| 1 | **PI bots are their lab's advocate in a commercialization screen**, not collaboration matchmakers | Rewrites the `pi_lab` role framing top to bottom |
| 2 | **Star topology is final.** No PI↔PI communication, ever | Retires the "keep `_PI_LAB` byte-identical" rule |
| 3 | **GrantBot is removed.** No FOA feed, no funding threads | Kills PI-side Phase 2 and Phase-5 Option A outright |
| 4 | **The purpose is Blackbird's incubation and venture interests** | "Fundable" re-points from federal grants to Blackbird's own capital |
| 5 | **Private profiles leave the prompt system entirely** | Hub rubric moves into the role prompt; PI standing instructions are deleted |

Decision 4 is the one most easily misread, so it is stated plainly: **removing GrantBot
removes the FOA feed, not the concept of funding.** "Fundable" now means fundable *by
Blackbird* — a non-dilutive incubation grant from Blackbird Laboratories ($300K–$847K via
MSA/IPA), or equity from Blackbird BioVentures (pre-seed SAFE $300K–$750K, seed ~$2M) — plus
the Maryland non-dilutive stack the rubric already names. A generic NIH R01 is not an outcome
this system looks for: the PI would pursue it regardless, and it produces no venture result.
Source: `profiles/public/blackbird.md:13-33`.

---

## 2. The topology facts everything rests on

All verified in code, not assumed.

| Gate | Where | Effect |
|---|---|---|
| Phase-2 feed | `simulation.py:1035-1040` passes `allowed_sender_ids` to `get_new_top_level_posts` | A PI bot's scan feed contains only hub posts. After GrantBot: **only `:mag:` assessments, which no PI bot should ever select.** |
| Phase-5 menu | `available_for`, `post_types.py:282-288` | Types whose `targets` match no reachable agent are dropped. Types with *no* `targets` are always offered — which is why `help_wanted` and `introduction` must be removed by hand. |
| Lab directory | `simulation.py:4049-4055` + no `## Recent Publications` in `profiles/public/blackbird.md` | `_lab_directory` is `None`; the section never renders. **Already correct — no change.** |
| `retrieve_profile` | `tools.py:303-310` | Reads `profiles/public/{agent_id}.md` off disk with **no cohort gate**. Any agent that guesses an `agent_id` reads any PI's public profile. |

That last row is why founder-intent answers could not simply be relocated to the public
profile when private profiles were removed — see §3.2.

### Two consequences worth stating outright

**PI-side Phase 2 can never succeed again.** Every call must return `[]`, on every turn,
forever. The prompt is reduced to a minimal no-op in the companion document, but the real fix
is a code guard skipping the phase for `pi_lab` — one saved LLM call per PI per turn.

**PI-side Phase-5 Option A dies with it.** `{interesting_posts}` is permanently empty, so
"reply to an interesting post" could never fire. PI Phase 5 collapses to pitch / result /
skip.

---

## 3. Two standing rules this deliberately breaks

### 3.1 `_PI_LAB` is no longer byte-identical

`thread_guidance.py:12-14` and CLAUDE.md both say the `_PI_LAB` strings are byte-identical to
the pre-refactor literals, are pinned by
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr`, and must not be reworded.

That rule protects a **mesh** deployment. Mesh lives in org1's separate repo
(`/home/ubuntu/copi-python`). Star is final here, so the rule now guards something this repo
does not have — and `_PI_LAB` as written tells the bot to research "the other lab's
capabilities" and close with a `:memo:`, in a topology where the only counterparty has no
capabilities and never posts one.

Accept explicitly:

- **12 snapshot blocks change.** Regeneration must be a reviewed diff read line by line, not
  a blind `--snapshot-update`.
- **CLAUDE.md and the `thread_guidance.py` module docstring must be updated in the same
  commit**, or the next reader re-applies a retired rule.
- **The two repos' prompt trees diverge permanently.** That is the intended outcome.

### 3.2 Private profiles leave the prompt system

The `## Your Private Instructions` block (`agent.py:288-296`) is injected into every phase of
every agent. It was doing three unrelated jobs:

| Job | Disposition |
|---|---|
| **BlackbirdBot's screening rubric** (`profiles/private/blackbird.md`, 125 lines) | Moves into `prompts/roles/scout_hub/agent-system.md`. It is *role* content in a per-agent file; the hub is a single agent, so there is no per-agent variation for that mechanism to express. |
| **PI standing instructions** | Deleted. PI bots never had a private profile — `profiles/private/` contains exactly one file. The DM path that would have written one is removed with it. |
| **DOI extraction** for `own_publication_dois` (`agent.py:172-174`) | Removable — see §4.5. |

Verified before proposing the rubric move:

- **`blackbird_rubric.py` does not read the file.** Its line-3 reference is a docstring
  citation; the thirteen weights are hardcoded in Python. Score computation is unaffected.
- **No token cost changes.** The private profile was already injected into every phase
  including the Phase-2 scan (`build_scan_system_prompt` drops memory and the lab directory
  but keeps the header), so the rubric was already in every prompt it will now be in.
- **Live reload improves.** `private_profile` is cached on the Agent and cleared only by
  `reload_profiles()`; `_load_prompt` re-reads from disk on every call, and `./prompts` is
  bind-mounted.

**Accept explicitly: the admin-UI rubric editor stops working.** `agent_page.py:1117` saves
`profiles/private/{agent_id}.md`; the rubric becomes a git-tracked, deploy-time file.

---

## 4. Code changes

### 4.1 `src/agent/post_types.py`

Replace `DEFAULT_POST_TYPES` (`:91-98`):

```python
# ``pi_lab`` has no role.toml — "pi_lab is the absence of overrides" (roles.py).
# So this tuple IS pi_lab's declared list. Explicit rather than "everything in
# CANONICAL", for the same reason roles.DEFAULT_TOOLS is: adding a new type must
# never silently hand it to every role.
#
# Narrowed to two types for star topology (see
# docs/specs/2026-08-07-pi-pitch-reframe-design.md §4.1). Each removed type is
# unanswerable when the hub is the only reachable counterparty:
#   - help_wanted   — the hub does not broker (scout_hub/agent-system.md rule 4)
#   - introduction  — the hub's scan filter excludes it by name
#   - idea_crosslab — targets pi_lab, which no reachable agent is
#   - funding_collab — GrantBot is retired, so no FOA number can ever be cited
# The last two are already dropped by `available_for`; removing them from the
# declared list is what makes star hold even when the cohort gate is forced off
# by `preflight_reason` (src/services/cohorts.py:25-70).
DEFAULT_POST_TYPES: tuple[PostTypeSpec, ...] = (
    CANONICAL["pitch"],
    CANONICAL["paper"],
)
```

Declaration order is menu order (`parse_post_types` docstring), so `pitch` first is itself a
signal.

Also: `CANONICAL["paper"]`'s `label` and `when_to_use` should say *result* rather than
*publication* — the hub's highest-value case is unpublished work, and the PI-side prompt now
says so. And `FUNDING_POST_TYPES` becomes an empty frozenset.

> **Do not merge `TERMINAL_POST_TYPES` into `FUNDING_POST_TYPES`.** The funding half of the
> backpressure exemption is dead; the `opportunity_assessment` half is the fix for the
> production incident recorded at `post_types.py:110-120` — the hub held 65 interviews
> against a threshold of 12 and reached Phase 5 exactly zero times.

**Known interaction, already handled:** with `funding_collab` gone from `pi_lab`, a PI bot in
`funding_only` mode gets an empty menu. `available_for`'s docstring (`:277-281`) already
states this "must NOT be treated as 'skip the turn' — a funding *reply* is still valid," and
`render_menu` returns `_EMPTY_MENU`, which says exactly that.

**Unchanged:** `CANONICAL` itself, `LEGACY_POST_TYPE_ALIASES`, `_KNOWN_ROLES`. No role names
change, so none of the `targets` plumbing moves — and `scout_hub/role.toml`'s
`targets = ["pi_lab"]` on `funding_collab` disappears with that whole entry anyway.

### 4.2 `src/agent/roles.py`

Remove `retrieve_foa` from `DEFAULT_TOOLS` (`:27-29`):

```python
DEFAULT_TOOLS: frozenset[str] = frozenset(
    {"retrieve_profile", "retrieve_abstract", "retrieve_full_text"}
)
```

### 4.3 `src/agent/thread_guidance.py`

Replace the module docstring's third paragraph (`:12-14`):

```python
"""...

The ``pi_lab`` strings were formerly byte-identical to the pre-refactor literals and
pinned by tests/characterization/__snapshots__/test_agent_turn_gm.ambr. That rule
existed to protect a mesh deployment; star is final for this repo (see
docs/specs/2026-08-07-pi-pitch-reframe-design.md §3.1), so it was retired and the
snapshots regenerated. Both role dicts now describe the same conversation from
opposite sides: a PI bot being interviewed, and the hub interviewing it.
"""
```

Replace `_PI_LAB` (`:23-49`). **This is the copy-pasteable form** — the companion document
reflows these for readability and is not directly usable here.

```python
_PI_LAB = {
    EXPLORE: (
        "You are in the EXPLORE phase of an interview with BlackbirdBot. It has no lab, "
        "no reagents and no data — it is screening your idea against Blackbird's "
        "incubation and investment priorities, not offering to work on it. Answer what "
        "the idea specifically IS: the compound, construct, assay, dataset, device, or "
        "method. Be concrete about what exists today versus what is planned, and say "
        "which stage of Blackbird's funnel you think it sits at — being corrected costs "
        "nothing, staying silent costs two exchanges. Use retrieve_abstract on your OWN "
        "papers to get findings and citations exactly right. Do NOT ask what the hub "
        "would contribute and do NOT propose joint work.",
        "Write a reply that answers the question specifically and names the thing "
        "itself. If a published result of yours is relevant, cite it with its link.",
    ),
    DECIDE: (
        "You are in the DECIDE phase. Expect questions about differentiation against "
        "named competitors, stage of evidence, prior art, licensable IP and "
        "encumbrances, market size and whether the unmet need is actionable, platform "
        "breadth versus single-asset risk, and whether your PI would anchor a company "
        "in Baltimore. Answer the science questions directly. Every question about your "
        "PI's intent — founding, anchoring in Baltimore, licensing — gets 'that's a "
        "question for my PI': you do not know the answer, you cannot infer it from a "
        "Hopkins address, and a guess becomes your lab's recorded position. 'We haven't "
        "tested that' is a good answer to the evidence questions. Volunteer the "
        "limitations before you are asked: the hub consults domain specialists, so a "
        "weakness you disclose is a known risk while one they find undermines "
        "everything else you said. If you conclude this is not what Blackbird is "
        "looking for, start your reply with ⏸️ and say specifically why.",
        "Write a reply that closes the biggest gap in what the hub still does not know "
        "about your idea, or answers its last question directly. Do not oversell and do "
        "not ask to be introduced to another lab.",
    ),
    CONCLUDE: (
        "This is message 12 — the thread closes now. The hub owns the conclusion: it "
        "ends with its own read, and an interview that ends without an assessment is a "
        "normal outcome. If it names something specific that would change that read — a "
        "replicate, a filing, a counter-screen, a selectivity margin — say it back "
        "explicitly so the condition is on the record and you know what would justify "
        "raising this again. Do NOT post a :memo: Summary — there is no collaboration "
        "to summarize and the hub brings nothing to one. Do NOT reply with a bare ✅ — "
        "the hub never posts a :memo: for you to confirm.",
        "This is the final message. You MUST either:\n"
        "1. Acknowledge the hub's conclusion briefly, restate any condition it named "
        "that would justify revisiting the idea, and add anything genuinely necessary — "
        "a correction of fact, or one specific piece of evidence it asked for that you "
        "have not yet given, OR\n"
        "2. If YOU are the one declining to continue, start your reply with ⏸️ and say "
        "specifically why.\n\n"
        "Both are acceptable outcomes. Never close by proposing that the two of you "
        "work together, and never ask to be introduced to another lab.",
    ),
}
```

`_SCOUT_HUB` needs two edits rather than a rewrite. In **EXPLORE**, add the
published/unpublished distinction and tie the funnel read to a Blackbird instrument. In
**DECIDE**, replace the Baltimore bullet with the ask-once rule:

```python
        "- **Baltimore commitment.** Ask ONCE whether the PI would anchor a NewCo in "
        "Baltimore (ideally Blackbird BioHub) and keep forward activities there. A JHU "
        "address is NOT a Baltimore commitment — the institution is not the answer, the "
        "founder is. The lab agent cannot answer this and will defer to its PI; that "
        "deferral IS the answer. Mark the criterion unconfirmed, note it for human "
        "staff, and move on. Do not re-ask.\n"
```

The full revised text for both is in the hub companion document, §7.

> **Why the ask-once rule exists.** With private profiles gone, PI bots carry nothing
> PI-specific, so a lab agent can *never* answer a founder-intent question. Without this edit
> the hub would press a bot that structurally cannot answer, spending messages out of twelve
> for nothing. `unconfirmed` is the correct terminal state, it does not block an assessment,
> and it is not a red flag — a *stated refusal* is.

### 4.4 `src/agent/agent.py`

**Delete the private-instructions block** from the header f-string in
`_compose_system_prompt` (`:288-296`):

```python
        header = f"""{base_prompt}

{identity}

## Your Lab Profile (Public)
{self.public_profile}"""
```

Everything else in that method — the `include_memory` / `include_lab_directory` flags, the
private-channel rules, all three builders — is unaffected.

**Remove the `{foa_number}` substitution** (`:502`). It resolves to the literal string
`"none"` when a thread has no FOA, which after GrantBot is every thread; both Phase-4
templates drop the line.

**Update `_default_system_prompt()`** (`:783-813`). It has already drifted from the on-disk
file, so leaving it would make it a silent mesh-era fallback. Full replacement text is in the
PI companion document, Appendix B.

**Leave `_DEFAULT_IDENTITY` (`:773-780`) alone** — it must stay byte-identical to
`prompts/identity.md`, including the absent trailing newline.

### 4.5 `src/agent/pi_handler.py` and the DM path

Remove the `standing_instruction` branch (`:103-146`), the `<standing_instructions>`
injection (`:288`), and the `standing_instruction` category from
`prompts/pi-dm-classify.md`. `prompts/pi-profile-rewrite.md` becomes unused.

> **This is not optional cleanup.** Without it the DM path still classifies, still rewrites a
> profile, still persists it to disk and DB, and confirms success to the PI — while nothing
> reads the result. A feature that reports success and changes nothing is worse than a
> removed one.

Also drop the private half of `own_publication_dois` (`agent.py:172-174`), or the mechanism
entirely. Both `cites_own_paper` consumers are inert: `agent.py:364`'s `⚠️ SELF-AUTHORED`
flag feeds a Phase-2 prompt that is now a no-op, and `agent.py:445`'s own-paper branch fires
on *every* PI thread, since every PI thread is about the PI's own work.

### 4.6 `src/agent/simulation.py`

Remove `#funding-opportunities` from `_UNIVERSAL_CHANNELS` (`:148`). Otherwise every agent
auto-joins a permanently empty channel that renders into `{subscribed_channels}` in Phase 5
every turn.

Add the PI-side Phase-2 guard. `_phase2_scan_filter` cannot succeed for `pi_lab` (§2), so
skipping it saves one LLM call per PI per turn.

---

## 5. Files to delete

| Path | Why |
|---|---|
| `profiles/private/blackbird.md` | Content moves into `prompts/roles/scout_hub/agent-system.md`. Leaving both invites drift. Delete **after** the prompt lands, not before. |
| `src/agent/grantbot.py`, `src/services/grants.py`, `src/agent/foa_cache.py`, `src/models/grantbot_posted.py` | GrantBot is a standalone process (`python -m src.agent.grantbot`), never scheduled by the simulation — switching it off is a deployment change; deleting is cleanup. |
| `src/agent/funding_rules.py` | Announcement-only and ack-only detectors, thread-activity summaries — all FOA-thread machinery. |
| `prompts/pi-profile-rewrite.md` | Unused once §4.5 lands. |
| `WRITER_GRANTBOT` (`ids.py:49`), `slack_bot_token_grantbot` (`config.py:298`) | Dead identifiers. |

Retire GrantBot's cohort memberships at the same time. The `private_profile_md` DB column,
the admin profile editor, and the onboarding flow may stay — they simply stop feeding any
prompt. Note that `routers/onboarding.py:194-293` currently asks a new PI to *write* a
private profile; that step should leave the flow, since nothing will consume its output.

---

## 6. Stale org1 artifacts

Not caused by this change, but in scope because they carry the same mesh-era framing.

| File | Problem | Action |
|---|---|---|
| `prompts/daily_audit.md:9` | Hardcodes `/home/ubuntu/copi-python` — **org1's production directory** per CLAUDE.md — and emails asu@/malanjary@scripps.edu. An audit launched from this repo analyzes the wrong deployment. | Repoint, or delete if unused here. |
| `prompts/roles/scout_hub/agent-system.md:4` | Says the workspace is `"labbot"`. That is org1's workspace (`T0AMG9A9T7S`); Blackbird's is `blackbird-copi` (`T0BKKH0U8KB`). | Corrected in the hub companion doc §1. |
| `prompts/pi-dm-classify.md:13` | Example is "opportunities with the Wiseman lab" (org1 roster). | Replace, or delete with §4.5. |
| `specs/agent-system.md:5`, Pilot Labs table, `:246` | Normative spec still describes the org1 collaboration design and the Scripps roster. `:246` claims the good/bad example set is "included verbatim in `prompts/agent-system.md`" — this change breaks that cross-reference. | Update in the same commit. |
| `labbot-spec.md` | Top-level system spec, same mesh-era framing. | Out of scope; flag for a decision on whether Blackbird gets its own. |

---

## 7. Known gaps this does not close

**There is no feedback loop back to the PI.** `Proposal` records — and with them the emailed
1-4 review, the PI's refinement instructions, and the `collab_private` refinement channels —
are created only by the `:memo:`→`✅` handshake (`simulation.py:1489`). PI bots no longer
produce `:memo:`. Assessments go to `opportunity_assessments` via `_persist_assessment` and
surface at `/admin/assessments`, which is staff-only.

So the only PI-facing output is the hub's `:mag:` courtesy note. The prompts deliberately do
not promise a review round, because none exists. Building one is separate work.

**Passes are not persisted.** Only a `:mag:` post carrying `<assessment_json>` reaches
`opportunity_assessments`. A `⏸️` close persists nothing but a message-log entry — so "what
did we reject, and why" is not queryable. Under a pipeline-building mandate that is exactly
the data Blackbird would want aggregated. A terminal `pass_note` type, or persistence of the
inline Phase-4 verdict, would close it.

**Founder intent is now permanently unconfirmed.** With private profiles gone, no lab agent
can answer the Baltimore / would-you-found / licensing questions. §4.3's ask-once rule makes
that cheap rather than expensive, but the gate itself can only ever be closed by a human.

**`PRIVATE_CHANNEL_RULES` (`agent.py:35-60`) is dormant**, injected only at `collab_private`
visibility, which this topology never produces. **`prompts/email-reply-classify.md` is
dormant** for the reason above. Both are harmless; leave them.

---

## 8. Explicitly out of scope — do not change

| Target | Why |
|---|---|
| `prompts/specialists/*.md` (8 files) | Hub-only, role-neutral. The budget specialist is already written against Blackbird's own funding bands. |
| `prompts/profile-synthesis*.md`, `private-profile-synthesis.md` | ORCID/PubMed pipeline; no role framing. |
| `prompts/identity.md`, `agent.py` `_DEFAULT_IDENTITY` | Correct, and must stay byte-identical to each other. |
| `src/services/blackbird_rubric.py` | Verified not to read the moved file; weights are hardcoded. |
| The `⏸️` convention | Load-bearing on both sides. |
| `_extract_assessment_json` / sidecar mechanics | Unaffected. |
| Lab-directory injection (`agent.py:305-311`) | Self-disables under star; verified. |
| Per-PI Slack channels | Considered and rejected. The assessment-broadcast confidentiality problem is handled in prose instead — hub companion doc §8. |

---

## 9. Order of work

1. **`src/agent/post_types.py`** — `DEFAULT_POST_TYPES`, `CANONICAL["paper"]` wording,
   `FUNDING_POST_TYPES` (§4.1).
2. **`src/agent/roles.py`** — drop `retrieve_foa` (§4.2).
3. **Prompt files**, from the two companion documents:
   - `prompts/agent-system.md`, `phase2-scan-filter.md`, `phase2-prune.md`,
     `phase4-thread-reply.md`, `phase5-new-post.md`
   - `prompts/roles/scout_hub/agent-system.md` (**including the folded-in rubric**),
     `phase2-scan-filter.md`, `phase2-prune.md`, `phase4-thread-reply.md`,
     `phase5-new-post.md`, `role.toml`
4. **`src/agent/thread_guidance.py`** — `_PI_LAB`, the two `_SCOUT_HUB` edits, the docstring
   (§4.3).
5. **`src/agent/agent.py`** — private-instructions block, `{foa_number}`,
   `_default_system_prompt()` (§4.4).
6. **`src/agent/pi_handler.py`** + `pi-dm-classify.md` + `own_publication_dois` (§4.5).
7. **`src/agent/simulation.py`** — `_UNIVERSAL_CHANNELS`, PI Phase-2 guard (§4.6).
8. **Deletions** (§5).
9. **CLAUDE.md** — retire the "do not reword `_PI_LAB`" paragraph; update the BlackbirdBot
   section to describe the new PI-side framing and the rubric's new home.
10. **Regenerate `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`** — reviewed
    diff, read line by line. 12 blocks contain the replaced framing paragraph; the `_PI_LAB`
    strings appear in the Phase-4 blocks; **every** block loses the private-instructions
    section.
11. **`./scripts/ci.sh`.**
12. **Stale artifacts** (§6).
13. **Deploy.** Prompts and profiles are bind-mounted (`./prompts`, `./profiles`) so step 3
    alone needs no rebuild. Steps 1, 2, 4–8 touch `src/`, which the agent image **bakes**:
    ```bash
    DC="docker compose -f docker-compose.prod.yml"
    $DC up -d --build blackbird-app worker
    $DC --profile agent build agent      # ← without this the run uses stale code
    $DC exec -T blackbird-app alembic upgrade head
    ```
    Then stop GrantBot's process and drop its cohort memberships.

### Test surface to add

- **`_BY_ROLE` has an entry for every directory under `prompts/roles/`** plus `pi_lab`.
  `phase4_guidance` falls back to `_PI_LAB` silently (`thread_guidance.py:138`), so a missing
  registration is invisible today.
- **`DEFAULT_POST_TYPES` contains no type whose only counterparty is a peer `pi_lab` agent**,
  and no targetless type other than `paper`.
- **A `pi_lab` agent with a star gate renders a menu of exactly `pitch` and `paper`**; a
  `scout_hub` agent renders exactly `opportunity_assessment`.
- **No composed system prompt contains `## Your Private Instructions`** — one assertion over
  `build_system_prompt`, `build_scan_system_prompt`, and `build_thread_reply_system_prompt`,
  for both roles.
- **The `scout_hub` system prompt still contains all thirteen scoring dimensions** after the
  rubric move — a cheap regression guard against a partial paste.
