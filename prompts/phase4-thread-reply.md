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

Write ONLY the message text as it should appear in Slack. Do not include:
- Internal reasoning, thinking, or commentary about your research process
- JSON, metadata, or formatting instructions
- Preamble like "Based on my research..." or "Let me think about..."
- Separators (--- or similar) before your actual message

Start your reply directly with the words you want posted to the Slack thread.

If you are posting a :memo: Summary (collaboration proposal), format it clearly with:
- What each lab brings
- The specific scientific question
- A concrete first experiment (days-to-weeks scope, specific assays/methods)
- Why this collaboration beats either lab working alone
- Confidence label: [High], [Moderate], or [Speculative]

If you are confirming agreement with a :memo: Summary from the other agent, start your
reply with ✅.

If you are closing the thread with no proposal, be gracious and specific about why
(not enough overlap, timing, etc.).
