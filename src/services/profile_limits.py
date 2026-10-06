"""The profile text limits (spec 2026-10-05 D24, P30): what synthesis is asked for, what
it is cut to before storing, and (Phase 4) what a human edit may save."""

import re
from collections.abc import Sequence

SUMMARY_MIN_WORDS = 100
SUMMARY_MAX_WORDS = 350
# A word-count limit alone accepts an arbitrarily large whitespace-free token.
# This generous text bound protects persona/model contexts without cutting a save.
SUMMARY_MAX_CHARS = 20_000
SUMMARY_AIM = "150–250"
TAG_LIST_MAX_ITEMS = 30
TAG_MAX_CHARS = 200
#: The wording the prompt, the retry text and the validation messages share.
SUMMARY_RULE = f"{SUMMARY_MIN_WORDS}–{SUMMARY_MAX_WORDS} words (aim for {SUMMARY_AIM})"
TAG_RULE = (
    f"at most {TAG_LIST_MAX_ITEMS} items per list, each at most {TAG_MAX_CHARS} characters, "
    "with no line breaks and not starting with #"
)

_WHITESPACE = re.compile(r"\s+")


def clean_tag(value: str) -> str:
    """One tag cut to the limits: newlines and other whitespace runs become one space,
    leading '#' characters and spaces are removed, and a tag over TAG_MAX_CHARS is cut at
    the last space at or before the limit (at the limit when it has none). "" when nothing
    is left."""
    tag = _WHITESPACE.sub(" ", value).lstrip("# ").strip()
    if len(tag) > TAG_MAX_CHARS:
        cut = tag.rfind(" ", 0, TAG_MAX_CHARS + 1)
        tag = (tag[:cut] if cut > 0 else tag[:TAG_MAX_CHARS]).rstrip()
    return tag


def cap_tags(values: Sequence[str]) -> list[str]:
    """``clean_tag`` over ``values`` (non-strings dropped), empties dropped, first
    TAG_LIST_MAX_ITEMS kept, order preserved."""
    cleaned = (clean_tag(v) for v in values if isinstance(v, str))
    return [t for t in cleaned if t][:TAG_LIST_MAX_ITEMS]
