"""Helpers for safely embedding untrusted content in LLM prompts.

Content that originates outside the agent's own trusted instructions — PubMed
abstracts and methods, other agents' Slack posts, user-editable profile text,
proposal summaries — must be presented to the model as *data*, not as
instructions, to blunt prompt injection (audit SEC-14). We fence each such
value in an XML-like block and strip from the content every tag that could
close that fence or forge another one (spec 2026-10-05 §6.5, D27).
"""

import re

#: Every tag ``src/`` passes to ``delimit``, plus ``lab_message`` (the hub's
#: fenced transcript lines). ``delimit`` strips all of them from any content,
#: so text fenced as one kind of data cannot forge another kind.
#: ``tests/unit/test_prompt_safety.py`` fails when a ``delimit`` call in ``src/``
#: uses a literal tag missing from this set.
FENCE_TAGS: frozenset[str] = frozenset({
    "agent_profile", "authors", "lab_message", "paper_abstract", "paper_methods",
    "paper_title", "patent", "pi", "proposal_summary", "staff_company_record",
    "statement",
})

# Ordinary nested forgeries are stripped exactly; adversarial depth must not force
# quadratic rescanning in the shared agent loop. Residual markup is escaped below.
_MAX_STRIP_PASSES = 16


def _strip_fences_once(text: str, prefix: re.Pattern[str]) -> str:
    """One non-overlapping stripping pass, without regex backtracking over attributes.

    The prefix scan and the searches for closing '>' advance through disjoint spans.
    A prefix without a closing '>' means later prefixes cannot close either.
    """
    chunks = []
    cursor = 0
    for match in prefix.finditer(text):
        if match.start() < cursor:
            continue
        end = text.find(">", match.end())
        if end < 0:
            break
        chunks.append(text[cursor:match.start()])
        cursor = end + 1
    chunks.append(text[cursor:])
    return "".join(chunks)


def delimit(content: object, tag: str = "untrusted_content") -> str:
    """Fence ``content`` in ``<tag>…</tag>`` for safe inclusion in a prompt.

    Every opening or closing tag named in ``FENCE_TAGS`` or ``tag`` is removed
    from the content first, attributes included, case-insensitively, and the
    removal repeats until stable, so a tag split around another
    (``</agent_<agent_profile>profile>``) cannot reassemble. After 16 changing
    passes, residual '<' characters are escaped instead of rescanning indefinitely.
    Other markup is kept except in that adversarial-depth fallback.
    """
    text = "" if content is None else str(content)
    names = "|".join(re.escape(name) for name in sorted(FENCE_TAGS | {tag}))
    prefix = re.compile(rf"</?\s*(?:{names})\b", re.IGNORECASE)
    for _ in range(_MAX_STRIP_PASSES):
        stripped = _strip_fences_once(text, prefix)
        if stripped == text:
            break
        text = stripped
    else:
        text = text.replace("<", "&lt;")
    return f"<{tag}>\n{text}\n</{tag}>"
