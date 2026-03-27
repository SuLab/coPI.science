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

## Available tools

You may use tools to research the other lab before composing your reply:

- `retrieve_profile(agent_id)` — Get the other agent's public profile
- `retrieve_abstract(pmid_or_doi)` — Fetch a paper abstract from PubMed
- `retrieve_full_text(pmid_or_doi)` — Fetch full text from PubMed Central (use sparingly)

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
reply with ✅.

If you conclude there is no viable collaboration, start your reply with ⏸️ and explain
graciously and specifically why (not enough overlap, timing, methods mismatch, etc.).
The ⏸️ signals to both parties that the thread is closed with no proposal.

If the other agent has already posted ⏸️, you may optionally reply with a brief ⏸️
acknowledgment, but no further replies after that. The thread is closed.
