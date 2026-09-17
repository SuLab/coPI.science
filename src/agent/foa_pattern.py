"""Shared FOA (Funding Opportunity Announcement) number pattern.

`src/agent/foa_cache.FOA_PATTERN` and `src/agent/funding_rules._FOA_NUMBER_RE` used to diverge in both
directions: the cache's pattern required an agency-code segment between two dashes
(`RFA-AI-27-019`) and so rejected the entire PA/PAR/PAS parent-announcement family (`PAR-24-293`,
`PA-24-293`, `PAS-24-293`), which have no such segment; funding_rules' pattern accepted that family plus
RFA but not NOT/OTA/RFI/DE-FOA. Neither was case-insensitive. This module is the one pattern both call
sites use now. NSF-style numbers are deliberately out of scope — no fixture in this repo shows
Grants.gov's real NSF `number` field shape.
"""

import re

# Three shapes NIH/DOE funding opportunity numbers take:
#   - agency-coded:                          RFA-AI-27-019, NOT-OD-24-001
#   - parent announcement (no agency code):  PA-24-293, PAR-24-293, PAS-24-293
#   - DOE:                                   DE-FOA-0003456
# The PA/PAR/PAS branch deliberately has no agency-code segment: the old cache
# pattern's PAR-AI-24-293-style composites (agency code wedged into the parent-
# announcement shape) no longer match here, because they are not a real NIH
# numbering shape and no fixture in this repo uses them.
# Years appear as 2 or 4 digits. IGNORECASE because Slack/LLM text is not guaranteed to preserve
# NIH's all-caps convention.
FOA_NUMBER_RE = re.compile(
    r"\b((?:RFA|NOT|OTA|RFI)-[A-Z]{2,4}-\d{2,4}-\d{2,5}"
    r"|PA[RS]?-\d{2,4}-\d{3,5}"
    r"|DE-FOA-\d{6,8})\b",
    re.IGNORECASE,
)


def extract_foa_number(content: str) -> str | None:
    """Return the first FOA number found in ``content`` in canonical form, or None.

    Canonical means upper case, the form NIH publishes and the form Grants.gov returns in its
    ``number`` field. The pattern is IGNORECASE, so without this the result
    would carry whatever casing the post used — and the result is used verbatim as a cache **file
    name** (``foa_cache.cache_foa`` → ``data/foa_cache/<number>.json``) and as the key of the
    per-thread FOA prompt context Phase 5 builds. Both cache writers key on the canonical
    spelling — GrantBot writes ``opportunity["number"]`` straight from Grants.gov, and
    ``backfill_cache`` replays ``grantbot_posted_foas.foa_number``, all 251 rows of which are
    upper case on the production copy — so canonicalising here orphans nothing on disk; it makes
    a lower-cased mention (NIH's own permalink lower-cases the number) resolve to the entry that
    is already there instead of missing it forever.
    """
    m = FOA_NUMBER_RE.search(content)
    return m.group(1).upper() if m else None
