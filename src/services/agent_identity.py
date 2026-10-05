"""Derive an agent's (agent_id, bot_name) pair from a PI's display name.

Moved out of ``src/routers/agent_page.py`` so the manager Add-PI flow can mint
agents through the same logic as self-service signup. It is the only copy
(the two script copies were retired): display casing is preserved
(``McCarthyBot``).
"""

import logging
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry

logger = logging.getLogger(__name__)

# Numeric tier bound: suffixes 2..19.
_NUMERIC_TIER_MAX = 20

# Slack caps an app name at 35 characters (docs.slack.dev/reference/app-manifest,
# display_information.name). The longest bot name built below is the initial,
# the surname, a two-digit suffix and "Bot": 1 + 28 + 2 + 3 = 34.
_DISPLAY_MAX = 28

# Latin letters NFKD does not decompose into an ASCII base letter.
_ASCII_LETTERS = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
    "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "ł": "l", "Ł": "L", "þ": "th",
    "Þ": "Th", "ı": "i",
})

# Generational and degree suffixes an ORCID family-name field can carry
# ("Smith Jr.", "Jones III", "Picard PhD"); never a surname on their own.
_NAME_SUFFIXES = frozenset({
    "jr", "sr", "ii", "iii", "iv", "phd", "md", "mph", "dphil", "dds", "dvm",
    "msc", "mba", "facs", "frs", "esq",
})


def _ascii_letters(text: str) -> str:
    """``text`` folded to ASCII with every character that is not an ASCII letter or
    digit dropped: "Müller" -> "Muller", "O'Brien" -> "OBrien"."""
    folded = unicodedata.normalize("NFKD", text.translate(_ASCII_LETTERS))
    return "".join(c for c in folded if c.isascii() and c.isalnum())


def _surname(full_name: str) -> str:
    """The last word of ``full_name`` that can be a surname, as ASCII letters: a word
    holding a digit (an ORCID iD standing in for a private name) and a trailing
    generational or degree suffix are skipped. Empty when no word qualifies."""
    words = [_ascii_letters(w) for w in full_name.split()]
    words = [w for w in words if w and not any(c.isdigit() for c in w)]
    while words and words[-1].lower() in _NAME_SUFFIXES:
        words.pop()
    return words[-1][:_DISPLAY_MAX] if words else ""


async def _is_taken(db: AsyncSession, agent_id: str) -> bool:
    row = await db.execute(
        select(AgentRegistry.id).where(AgentRegistry.agent_id == agent_id)
    )
    return row.first() is not None


async def derive_agent_identity(
    db: AsyncSession, full_name: str, orcid: str | None = None
) -> tuple[str, str]:
    """Return ``(agent_id, bot_name)`` for a PI's display name.

    Both values are derived here, together, because they must agree: the
    collision prefix used to be applied to agent_id at one line and bot_name
    rebuilt from the bare last name four lines later, so Peng Wu got
    ``pwu`` / ``WuBot`` — colliding with Chunlei Wu's bot while the ids
    differed. docs/operations/pis-and-access.md documents ``pwu`` / ``PWuBot``.

    Tiers: bare last-name stem → first-initial prefix → numeric suffix
    (``wu2``..``wu19``). The numeric tier exists because the manager Add-PI
    flow creates rows without a human in the loop, and a second collision
    used to surface as an IntegrityError on the unique ``agents.agent_id``.

    The slug and the bot name hold ASCII letters and digits only: the slug names
    files and cohorts that ``[a-z0-9_-]`` checks guard (``src/agent/tools.py``,
    ``user_deletion``, ``pi_companies``), and Slack restricts a bot's display
    name to ASCII (``display_name``: a-z, 0-9, -, _ and .; capitals are accepted
    in practice — every bot so far is CamelCase). Accents are folded
    ("Müller" -> ``muller`` / ``MullerBot``), punctuation is dropped
    ("O'Brien" -> ``OBrienBot``) and a trailing "Jr."/"III"/"PhD" is skipped.

    A name with no usable surname (non-Latin script, or the ORCID iD itself,
    which is what an ORCID record with a private name yields) falls back to an
    ``orcid``-derived stem (``pi6789``).
    """
    display = _surname(full_name)
    stem = display.lower()

    if not stem:
        digits = "".join(c for c in (orcid or "") if c.isdigit())
        stem = f"pi{digits[-4:]}" if digits else "lab"
        display = stem.capitalize()
        logger.warning(
            "derive_agent_identity: no usable surname in %r; "
            "falling back to stem %r", full_name, stem,
        )

    if not await _is_taken(db, stem):
        return stem, f"{display}Bot"

    initial = next((c for c in _ascii_letters(full_name) if c.isalpha()), "")
    if initial:
        prefixed = f"{initial.lower()}{stem}"
        if not await _is_taken(db, prefixed):
            return prefixed, f"{initial.upper()}{display}Bot"

    for n in range(2, _NUMERIC_TIER_MAX):
        candidate = f"{stem}{n}"
        if not await _is_taken(db, candidate):
            return candidate, f"{display}{n}Bot"

    raise RuntimeError(
        f"Could not derive a free agent_id for {full_name!r} after "
        f"{_NUMERIC_TIER_MAX - 2} numeric candidates"
    )
