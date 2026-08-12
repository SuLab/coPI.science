"""Helpers for safely embedding untrusted content in LLM prompts.

Content that originates outside the agent's own trusted instructions — PubMed
abstracts and methods, other agents' Slack posts, user-editable profile text,
proposal summaries — must be presented to the model as *data*, not as
instructions, to blunt prompt injection (audit SEC-14). We fence each such
value in an XML-like block and neutralize any attempt inside the content to
forge the closing tag and "escape" back into instruction context.
"""

import re


def delimit(content: object, tag: str = "untrusted_content") -> str:
    """Fence ``content`` in ``<tag>…</tag>`` for safe inclusion in a prompt.

    Any literal ``<tag>`` / ``</tag>`` occurrences inside the content are
    stripped first so the fence cannot be closed early from within.
    """
    text = "" if content is None else str(content)
    text = re.sub(rf"</?\s*{re.escape(tag)}\s*>", "", text, flags=re.IGNORECASE)
    return f"<{tag}>\n{text}\n</{tag}>"
