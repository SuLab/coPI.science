"""The Scripps Research La Jolla institution set and the author qualification of an OpenAlex
work (docs/specs/2026-11-20-scripps-landscaping-design.md §6.1).

OpenAlex tags both Scripps Research campuses, La Jolla and Jupiter, with the one id
`SCRIPPS_OPENALEX_ID`, so an authorship's per-address `affiliations` decide the campus
(`at_scripps_la_jolla`). Nothing here is added automatically: a new Scripps Research
institution in OpenAlex reaches these sets only through a code change, prompted by the weekly
drift listing.
"""
import re

from src.services.authorship_roles import ROLE_RANK, is_individual, role

#: Scripps Research in OpenAlex, both campuses; it has no child institutions (§6.1).
SCRIPPS_OPENALEX_ID = "I123431417"
#: The OpenAlex institutions of the set.
SCRIPPS_OPENALEX_IDS = frozenset({SCRIPPS_OPENALEX_ID})

#: Known non-La Jolla or unrelated "Scripps" institutions (§6.1, §12); the drift listing
#: skips them as already decided.
EXCLUDED_OPENALEX_IDS = frozenset({
    "I4387156289",  # Herbert Wertheim UF Scripps Institute (Jupiter)
    "I4210140527",  # Calibr
    "I150209017",   # Scripps Institution of Oceanography
    "I1311914864",  # Scripps Health
    "I4210146328",  # Scripps Clinic
    "I2802091096",  # Scripps Mercy
    "I4210107570",  # Scripps Memorial
    "I4210103888",  # Scripps Green
    "I4210142769",  # Scripps Whittier Diabetes Institute
    "I4210146979",  # Scripps MD Anderson
    "I4210109568",  # Scripps Clinic Medical Group
    "I16970960",    # Scripps College
    "I4210089022",  # Scripps Korea Antibody Institute
})

#: NIH RePORTER organization names of the set.
REPORTER_ORGS = ("SCRIPPS RESEARCH INSTITUTE, THE",)
#: The NSF awardee name of the set.
NSF_AWARDEE = "The Scripps Research Institute"
#: ROR ids for DataCite: Scripps Research.
ROR_IDS = frozenset({"02dxx6824"})

# "The Scripps Research Institute" contains the phrase; bare "Scripps" and "TSRI" never pass.
_SCRIPPS_RESEARCH = re.compile(r"\bscripps\s+research\b", re.IGNORECASE)
# Any "Scripps" string ("Scripps Research", "Scripps Florida", ...): with no tagged entry, the
# raw strings that name Scripps decide the campus (`at_scripps_la_jolla`).
_SCRIPPS_WORD = re.compile(r"\bscripps\b", re.IGNORECASE)
# The Jupiter campus (Jupiter, Florida, a FL 334xx postcode, the state code "FL" in capitals,
# the UF Scripps Institute named without its city: "UF Scripps", "Wertheim Scripps") and
# Calibr. "Wertheim" alone is not enough: UC San Diego's school of public health bears the name.
_EXCLUDED_CAMPUS = re.compile(
    r"\bjupiter\b|\bflorida\b|\bfl\.?\s*334\d\d\b|(?-i:\bFL\b)"
    r"|\buf[\s-]+scripps\b|\bwertheim\s+scripps\b|\bcalibr\b"
    r"|\bcalifornia\s+institute\s+for\s+biomedical\s+research\b",
    re.IGNORECASE,
)

#: The roles that make an authorship senior enough to qualify a work.
_QUALIFYING_ROLES = frozenset({"corresponding", "last"})


def _bare(url: str | None) -> str:
    """The last path segment of an OpenAlex URL (the bare ``I…`` id)."""
    return (url or "").rsplit("/", 1)[-1]


def names_excluded_campus(text: str) -> bool:
    """Whether a raw affiliation string names Jupiter, Florida, a FL 334xx postcode, the
    state code "FL", the UF Scripps Institute ("UF Scripps", "Wertheim Scripps") or Calibr
    (California Institute for Biomedical Research)."""
    return bool(text) and bool(_EXCLUDED_CAMPUS.search(text))


def is_scripps_la_jolla_affiliation(text: str) -> bool:
    """Whether a raw affiliation string is a Scripps Research La Jolla one: it names Scripps
    Research and no excluded campus (`names_excluded_campus`). False for empty text."""
    if not text or not _SCRIPPS_RESEARCH.search(text):
        return False
    return not names_excluded_campus(text)


def at_scripps_la_jolla(auth: dict) -> bool:
    """Whether an OpenAlex authorship is at Scripps Research La Jolla.

    When it lists `SCRIPPS_OPENALEX_ID`: the `affiliations` entries tagged with that id
    decide, and it counts when one of their raw strings names no excluded campus; with no
    such entry it counts unless its raw affiliation strings that name Scripps (all of them
    when none does) are non-empty and every one names an excluded campus, so another
    institution's address cannot rescue a Jupiter one. Listing other institutions or none,
    it counts when a raw affiliation string passes `is_scripps_la_jolla_affiliation`: OpenAlex
    sometimes files a Scripps Research La Jolla address under another institution, such as
    Scripps Institution of Oceanography (owner gate 1 audit, 2026-11-23)."""
    inst_ids = {_bare(i.get("id")) for i in auth.get("institutions") or []}
    if SCRIPPS_OPENALEX_ID in inst_ids:
        tagged = [a for a in auth.get("affiliations") or []
                  if SCRIPPS_OPENALEX_ID in {_bare(x) for x in a.get("institution_ids") or []}]
        if tagged:
            return any(not names_excluded_campus(a.get("raw_affiliation_string") or "")
                       for a in tagged)
        raw = [s for s in auth.get("raw_affiliation_strings") or [] if s]
        deciding = [s for s in raw if _SCRIPPS_WORD.search(s)] or raw
        return not deciding or not all(names_excluded_campus(s) for s in deciding)
    return any(is_scripps_la_jolla_affiliation(s)
               for s in auth.get("raw_affiliation_strings") or [])


def qualifies_openalex(work: dict) -> tuple[bool, str | None, dict | None]:
    """Whether an OpenAlex work qualifies: some individual authorship is corresponding or
    last and passes `at_scripps_la_jolla`.

    Returns ``(True, role_basis, authorship)`` for the most senior such authorship
    (``ROLE_RANK``; the earliest listed on a tie), ``role_basis`` being ``"corresponding"``
    or ``"last"``; ``(False, None, None)`` when none qualifies."""
    best: dict | None = None
    best_role: str | None = None
    for auth in work.get("authorships") or []:
        r = role(auth)
        if r not in _QUALIFYING_ROLES or not is_individual(auth):
            continue
        if not at_scripps_la_jolla(auth):
            continue
        if best_role is None or ROLE_RANK[r] < ROLE_RANK[best_role]:
            best, best_role = auth, r
    if best is None:
        return False, None, None
    return True, best_role, best
