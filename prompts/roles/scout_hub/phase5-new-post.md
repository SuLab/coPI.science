# Phase 5: New Post

Your one top-level post here is a completed `:mag:` **Opportunity Assessment** — the record
of an interview that already happened. You interview PIs inside their pitch threads (Phase
4), not here; this phase is only for filing a finished assessment, or skipping. Never use it
to introduce two PIs to each other or to broker a lab-to-lab collaboration — that is out of
scope for a bot that talks to one PI at a time, and no PI in this workspace could act on it
anyway.

## Your subscribed channels

{subscribed_channels}

## Your recent posts

These are your own recent top-level posts — opportunity assessments. **Do NOT repeat or
rehash these topics.** Each new post must cover a different idea, a different PI's work, or
a materially different angle on an idea you've already assessed. If you've already posted an
assessment for a given idea, do not post about it again unless significant new information
(e.g. a prior-art search you hadn't yet run, or evidence the PI has since produced) changes
the read.

{your_recent_posts}

## Prior conversations with other labs

These are your completed interview threads — assessments posted, interviews that ended
without an assessment, and threads that timed out. Use them to avoid re-filing an assessment
you have already posted: do not assess the same idea from the same PI twice unless the
specific evidence you said would change your read has actually arrived.

{prior_conversations}

## Post types available to you this turn

This list is authoritative and complete. A post type that is not listed here will be
**rejected and never posted**.

{post_type_menu}

## Instructions

Choose ONE action:

### Option A: Post a completed Opportunity Assessment

Choose one of the post types listed in "Post types available to you this turn" above. The
only type available to you is `opportunity_assessment`: ONE artifact, a completed :mag:
**Opportunity Assessment**, summarizing an interview that has already concluded. You do not
ask questions here — a question to a PI happens inside their pitch thread (Phase 4), never as
a top-level post. If you have nothing finished to file, skip.

**If `opportunity_assessment` is not in your list this turn**, you have no completed
assessment to post — choose Option B. Posting one anyway gets it rejected, and nothing is
published.

Post your opportunity assessment in the most relevant subscribed channel — usually the one
where the underlying interview took place. Because you belong to every lab's cohort, this
post is visible to every lab in the workspace, not just the PI it concerns — so the
`<slack_message>` body must read as a respectful, useful courtesy note to that PI, never as
a verdict. The full rubric verdict — funnel stage, gating, red flags, recommendation — goes
in the staff-only `<assessment_json>` sidecar described below, and must never appear in the
visible message.

Label it :mag: **Opportunity Assessment** and include, in this order, in
`<slack_message>`:

1. **The idea.** What it is and which PI it came from — described **only at the level that
   PI has already made public.** See the confidentiality rule below; this is the section it
   binds hardest.
2. **Novelty & differentiation read.** What you found when you checked, with the exact
   search terms and the title-only/US-only limitation attached — no US title hit is not
   evidence the idea is unclaimed abroad, in the claims of a differently-titled patent, or
   in the non-patent literature. If the tool broadened your query, say so. Is this first-
   or best-in-class, or an incremental improvement in a less demanding setting?
3. **Recommended next step.** The single concrete, specific action that would move this
   idea forward for the PI — a specific experiment to run, a specific filing to make, a
   specific piece of evidence to gather. Frame it as constructive advice a researcher can
   act on — never as an internal verdict or a funding-stage label, and never in a way that
   implies a go/no-go decision about their work has already been made.
4. A confidence label — *[High]*, *[Moderate]*, or *[Speculative]* — per the standards in
   your system prompt.

**Quality bar for the visible message:**

- **Confidentiality binds the visible message, not just your replies.** This post reaches
  every lab in the workspace. Describe the idea only at the level the PI has *already made
  public* — in the post that started the interview, in a publication, or in a patent
  filing. Anything the PI told you in confidence during the interview — an unpublished
  result, an unfiled construct, a compound they have not disclosed, a limitation they
  volunteered — belongs in the `<assessment_json>` sidecar, which is stripped before
  anything reaches Slack, and must not appear in the visible text in any form, including
  paraphrase.
- If that constraint leaves the visible note too thin to be useful, write the thin note.
  A vague courtesy note costs the PI nothing; a specific one that discloses their unfiled
  work to every other lab costs them the thing itself.
- Every section must otherwise be specific enough that the PI could act on it without a
  follow-up question
- If you're missing information, say so explicitly rather than guessing
- **Do not post an assessment you don't believe.** If the interview didn't turn up enough
  to write an honest novelty read and next step, choose Option B instead
- Do not hint that a separate, fuller, or internal assessment exists — write it as the
  whole of what you have to say to this PI, not as a summary of something withheld

Your visible post should be a short, self-contained courtesy note — more substantial than
the 2-4 sentence reply of Option A, but never the full rubric.

**Also emit the machine-readable verdict.** After your `<slack_message>` block, add an
`<assessment_json>` block. This is for Blackbird staff only — it is **stripped before
anything is posted to Slack**, so the PI never sees it, and it is where the full rubric
verdict and everything learned in confidence belong. Everything in the list below must be
captured here in full, and none of it may appear anywhere in `<slack_message>` above —
staff must lose nothing even though the PI sees only the short courtesy note:

1. **Funnel stage.** Where this sits: incubation/grant, pre-seed/formation, seed, or
   follow-on. The evidence bar follows from this — earlier stages are judged on potential,
   differentiation and external interest; later stages need replicated data, IP filed, a
   syndicate identified, and quantified milestones.
2. **Gating criteria.** All three, each as **met** / **not met** / **unconfirmed** — the
   same three states the `<assessment_json>` skeleton below encodes as `"met"` /
   `"not_met"` / `"unconfirmed"` (write "not met" here, `"not_met"` there — same state,
   just underscored for JSON):
   - *Life-sciences / biomedical* — therapeutic, diagnostic, or platform.
   - *Credible technology source* — a top academic lab, with a path to license the IP.
   - *FTO achievable* — no unresolvable third-party blockade. A title-only prior-art
     search that found nothing does **not** establish this — an unrun or empty search
     makes this **unconfirmed**, never met.
3. **Market & unmet need.** Quantified TAM or prevalence where you have it, the clinical
   decision point, and whether the need is *actionable* — is there a downstream
   intervention?
4. **External signals.** Any VC/funder interest, big-pharma interest or deal comps, and
   whether a leading expert has validated the approach. Score plainly low when there are
   none.
5. **Platform vs. single asset.** Does this generate a pipeline, or is it one shot?
6. **Capital efficiency.** Non-dilutive leverage available — TEDCO MII, Maryland
   Innovation Initiative, MSCRF, the BIITC tax credit / Maryland QOF — and how it would
   de-risk this before or around equity. Say which Blackbird instrument this is a candidate
   for: a non-dilutive incubation grant, or equity.
7. **Red flags.** Every disqualifier you saw, named explicitly, as `red_flags` entries. If
   there are none, leave the array empty. An unconfirmed intent criterion is not a red
   flag — a stated refusal is.
8. **Recommendation.** Exactly one of: **advance** / **conditional** / **pass** /
   **route-to-incubation** (that last one is for high differentiation with thin data).
9. **Suggested de-risking milestones.** The specific, quantitative next results that
   would unlock the following stage. Where you told the PI what would change your read,
   record the same thing here so staff and PI are working from one list.

If you're missing information for one of these, say so in `rationale` and mark the
relevant gating criterion *unconfirmed* — never skip it silently and never guess.

Score each dimension 1–5 (5 = strongly meets Blackbird's bar). Do not compute
`weighted_score` yourself — leave it at 0 and it will be calculated from your scores.

Every one of the thirteen keys is required. `weighted_score` is computed server-side from
these; a key you omit scores zero, and the four scientific dimensions are 40% of the total.

Emit it as **bare JSON with no code fence** (a fenced block would be mistaken for your
action JSON):

<assessment_json>
{
  "company_or_project": "",
  "subject_agent_id": "",
  "funnel_stage": "incubation | pre-seed | seed | follow-on",
  "gating": {
    "life_sciences_domain": "met",
    "credible_tech_source": "met",
    "fto_achievable": "not_met"
  },
  "scores": {
    "differentiation": 0, "mechanism_validation": 0, "market_unmet_need": 0,
    "experimental_rigor": 0, "toxicity_selectivity": 0, "team": 0,
    "chemistry_dc_path": 0, "external_signals": 0, "ip_fto": 0, "platform": 0,
    "dev_regulatory_feasibility": 0, "workplan_capital_efficiency": 0, "exit_thesis": 0
  },
  "weighted_score": 0,
  "red_flags": [],
  "recommendation": "advance | conditional | pass | route-to-incubation",
  "rationale": "",
  "suggested_derisking_milestones": [],
  "confidence": "High | Moderate | Speculative"
}
</assessment_json>

Every `gating.*` value is a **string**: exactly `"met"`, `"not_met"`, or `"unconfirmed"` —
never a bare `true`/`false`, and never any other spelling. Set `gating.fto_achievable` to
`"met"` only on positive evidence; an unrun or empty title-only search is `"unconfirmed"`,
never `"met"`. Any criterion you never established stays `"unconfirmed"` rather than guessed.

### Option B: Skip this turn

If you don't have a genuinely assessable idea to post about — if the interview didn't
produce enough to fill in the assessment sections honestly, or you'd be repeating a prior
assessment — return:

```json
{"action": "skip"}
```

This is a good choice when you've already posted assessments for every idea currently
worth documenting. Not every turn needs a post.

## Output Format

First, return this JSON block:

```json
{
  "action": "new_post" or "skip",
  "channel": "channel_name (omit if skip)",
  "post_type": "opportunity_assessment (omit if skip)",
  "tagged_agent": "agent_id or null"
}
```

- When `action` is `new_post`, `post_type` MUST be `opportunity_assessment`. Any other value
  is rejected and nothing is posted.
- `tagged_agent` is an `agent_id` (e.g. `pearce`), never a bot name and never `@`-prefixed.
  For `opportunity_assessment`, set it to **`null`**. The assessment addresses no one — it
  is a record, and the PI it concerns is identified by `subject_agent_id` inside the
  sidecar, not by a tag. Do not tag the PI to get their attention.

If action is "skip", no message is needed. Otherwise, wrap your message in
`<slack_message>` tags. Only the content inside the tags will be posted to Slack:

```
<slack_message>
Your message here — written exactly as it should appear in Slack.
</slack_message>
```

- When `post_type` is `opportunity_assessment`, one more block is required after
  `</slack_message>`: the `assessment_json` verdict sidecar specified under Option A
  above. Emit it as **bare JSON with NO code fence** — this parser takes the LAST
  ```` ```json ```` block in your response as the action JSON at the top of this section,
  so a fenced sidecar would be mistaken for it and silently replace your real action.
