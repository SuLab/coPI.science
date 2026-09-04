"""Shared FOA (Funding Opportunity Announcement) number pattern.

`src/agent/foa_cache.FOA_PATTERN` and `src/agent/funding_rules._FOA_NUMBER_RE` diverged in both
directions (issue #23 COR-27): the cache's pattern required an agency-code segment between two dashes
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
    """Return the first FOA number found in ``content``, or None."""
    m = FOA_NUMBER_RE.search(content)
    return m.group(1) if m else None
