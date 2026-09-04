"""Shared bot-mention detection: literal @BotName tags and Slack's <@Uxxx> form.

Four copies of a bot-tag regex existed before this module (message_log.py,
funding_rules.py, and two in simulation.py), all case-sensitive on the
trailing "ot" (`@(\\w+[Bb]ot)\\b` matches "@SuBot" and "@subot" but not
"@SUBOT" or "@SUBot"). None handled Slack's own `<@Uxxx>` mention syntax at
all — a real Slack user typing "@SuBot" gets autocompleted to `<@U12345>` by
the Slack client before the text ever reaches this codebase, so a genuine
in-Slack mention of a bot was invisible everywhere. See issue #20 COR-8 (this
module). `funding_rules.py` now imports `BOT_TAG_RE` from here instead of
keeping its own copy (issue #23 COR-27/COR-28).
"""

from __future__ import annotations

import re

BOT_TAG_RE = re.compile(r"@(\w+bot)\b", re.IGNORECASE)

_MENTION_RE = re.compile(
    r"<@(?P<uid>[A-Za-z0-9]+)>|@(?P<bot_name>\w+bot)\b", re.IGNORECASE,
)


def extract_bot_mentions(
    text: str, bot_uid_to_agent: dict[str, str] | None = None,
) -> list[str]:
    """Return every bot mention in ``text``, in order of appearance.

    Two forms, both recognised, and NOT normalised to one namespace:

    - Literal ``@BotName`` (fully case-insensitive) -> the matched name,
      **lowercased** (e.g. ``"wisemanbot"``). The caller resolves this
      through its own bot_name -> agent_id map.
    - Slack's ``<@Uxxx>`` mention -> resolved directly through
      ``bot_uid_to_agent`` (Slack ``bot_user_id`` -> agent_id, e.g.
      ``SimulationEngine._bot_uid_map()``) and appended as an **agent_id**,
      not a bot name. Skipped entirely if ``bot_uid_to_agent`` is None or the
      uid isn't in it (an unresolvable/human mention) — never guessed.

    A caller that needs a single namespace should do
    ``bot_name_to_id.get(token, token)``: a bot-name token is resolved by the
    lookup, and an already-resolved agent_id token round-trips through the
    ``.get(..., token)`` default unchanged, because bot names and agent_ids
    never collide across a real roster.
    """
    mentions: list[str] = []
    for m in _MENTION_RE.finditer(text):
        uid = m.group("uid")
        if uid is not None:
            if bot_uid_to_agent:
                agent_id = bot_uid_to_agent.get(uid)
                if agent_id:
                    mentions.append(agent_id)
            continue
        mentions.append(m.group("bot_name").lower())
    return mentions
