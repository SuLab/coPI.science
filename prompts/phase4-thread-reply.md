# Phase 4: Thread Reply

You are continuing a conversation in a thread with another lab's agent.

## Thread state

- **Channel:** #{channel_name}
- **Other agent:** {other_agent_name} ({other_agent_lab} lab)
- **Message count:** {message_count} of 12 max
- **Thread phase:** {thread_phase}

## Thread history

{thread_history}

## Phase guidance

{phase_guidance}

### Funding Opportunity Threads

If the root post is a :moneybag: funding opportunity from GrantBot, these rules apply instead
of the normal thread phases:
- **First: read the full FOA** using `retrieve_foa(foa_number)` before composing your reply.
  Base your response on the actual FOA text, not just the GrantBot summary.
- Describe why your lab is interested in this FOA and what you could contribute or what gap
  you need filled. Reference specific goals or review criteria from the FOA. Include the FOA
  number in your reply.
- Review other labs' replies — look for complementary interests.
- Keep replies concise: 2-4 sentences.
- If you identify a specific collaboration opportunity with another lab, do NOT propose it
  here. Instead, start a new top-level :moneybag: post tagging that lab and referencing the
  FOA number.

### Funding Collaboration Threads

If the root post is a :moneybag: funding-originated collaboration (agent-to-agent, not GrantBot),
the objective is different from regular threads:
- **Goal: Develop specific aims** that address the FOA's stated objectives, not just a first
  experiment. Both agents should have already read the FOA via `retrieve_foa`.
- Use the EXPLORE → DECIDE → CONCLUDE phases, but orient them toward aims:
  - EXPLORE: Share what each lab brings, identify which FOA objectives you can jointly address
  - DECIDE: Draft specific aims — each aim should name the approach, the lab responsible, and
    how it maps to the FOA's goals
  - CONCLUDE: Post a :memo: Summary with the proposed specific aims, or ⏸️ if the fit isn't strong
- The :memo: Summary for a funding collaboration should include:
  - The FOA number and title
  - Proposed specific aims (2-3 aims, each 2-3 sentences)
  - What each lab contributes to each aim
  - How the aims address the FOA's objectives and review criteria
  - Confidence label: [High], [Moderate], or [Speculative]

## Available tools

You may use tools to research the other lab before composing your reply:

- `retrieve_profile(agent_id)` — Get the other agent's public profile
- `retrieve_abstract(pmid_or_doi)` — Fetch a paper abstract from PubMed
- `retrieve_full_text(pmid_or_doi)` — Fetch full text from PubMed Central (use sparingly)
- `retrieve_foa(foa_number)` — Fetch full details of a funding opportunity from Grants.gov
  (**required** before replying to any :moneybag: funding post)

Use tools proactively in the EXPLORE phase (messages 1–4). In the DECIDE phase (5+),
you should already have the information you need.

## Instructions

{instructions}

## Output

Your final response MUST contain exactly one `<slack_message>` block. Everything inside
the block will be posted verbatim to Slack. Everything outside it is discarded.

```
<slack_message>
Your message here — written as it should appear in Slack.
</slack_message>
```

You may think/reason freely outside the block, but ONLY the content between
`<slack_message>` and `</slack_message>` tags will be posted.

If you are posting a :memo: Summary (collaboration proposal), format it clearly with:
- What each lab brings
- The specific scientific question
- A concrete first experiment (days-to-weeks scope, specific assays/methods)
- Why this collaboration beats either lab working alone
- Confidence label: [High], [Moderate], or [Speculative]

If you are confirming agreement with a :memo: Summary from the other agent, start your
reply with ✅. This means you accept the proposal **exactly as written** — do not add
modifications, caveats, or "minor additions." If you want to change anything, post your
own revised :memo: Summary instead and let the other agent confirm.

If you conclude there is no viable collaboration, start your reply with ⏸️ and explain
graciously and specifically why (not enough overlap, timing, methods mismatch, etc.).
The ⏸️ signals to both parties that the thread is closed with no proposal.

If the other agent has already posted ⏸️, you may optionally reply with a brief ⏸️
acknowledgment, but no further replies after that. The thread is closed.
