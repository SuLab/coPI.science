"""The Baltimore Johns Hopkins institution set and the author qualification of an OpenAlex
work (spec 2026-10-30 §5.1, §5.2; L3, L4, D1, D2).

Nothing here is added automatically: a new Hopkins institution in OpenAlex reaches this
set only through a code change, prompted by the weekly drift listing (§5.1 "Drift").
"""
import re

from src.services.authorship_roles import ROLE_RANK, at_institution, is_individual, role
from src.services.jhu_rules import is_hopkins_affiliation

#: The OpenAlex institutions in Baltimore that are part of, or affiliated with, Johns
#: Hopkins (§5.1 table, from OpenAlex on 2026-10-30).
BALTIMORE_OPENALEX_IDS = frozenset({
    "I145311948",   # Johns Hopkins University
    "I2799853436",  # Johns Hopkins Medicine
    "I4210150714",  # Johns Hopkins Hospital
    "I4210164401",  # Sidney Kimmel Comprehensive Cancer Center
    "I2802697821",  # Johns Hopkins Bayview Medical Center
    "I4392738222",  # Center for Innovative Medicine
    "I4391767829",  # Neurofibromatosis Therapeutic Acceleration Program
    "I4210092215",  # Berman Institute of Bioethics
    "I4210151848",  # Center for Communication Programs
    "I4391012584",  # Hopkins Extreme Materials Institute
    "I4394709191",  # Johns Hopkins University Press
    "I4389425327",  # JHU Center for AIDS Research
    "I4405260374",  # JH Materials Characterization and Processing Facility
    "I4392738135",  # Packard Center
    "I4387152773",  # PARADIM
    "I4388891755",  # Materials Science in Extreme Environments University Research Alliance
    "I4210120405",  # Maryland Space Grant Consortium
    "I4210129832",  # Johns Hopkins Children's Center
    "I4210114877",  # Johns Hopkins Center for Health Security
    "I4405267562",  # Jhpiego
    "I1298154867",  # Kennedy Krieger Institute
})

#: The Hopkins-lineage OpenAlex institutions §5.1 excludes because they are not in
#: Baltimore; the drift listing skips them as already decided.
EXCLUDED_OPENALEX_IDS = frozenset({
    "I2802946424",  # Applied Physics Laboratory (North Laurel, MD)
    "I4210098865",  # All Children's Hospital (St. Petersburg, FL)
    "I4390039338",  # SAIS (Washington)
    "I4210131961",  # SAIS Bologna Center
    "I4210162165",  # Hopkins-Nanjing Center
    "I4210148073",  # Sibley Memorial Hospital (Washington, DC)
    "I2799487757",  # Suburban Hospital (Bethesda, MD)
    "I2800996824",  # Howard County General Hospital (Columbia, MD)
    "I1326779652",  # Peninsula Regional Medical Center (Salisbury, MD)
    "I2800952698",  # Saudi Aramco Medical Services Organization (Dhahran)
    "I4210152837",  # Dhahran Health Center
    "I4210121676",  # MUJHU Research Collaboration (Kampala)
})

#: NIH RePORTER organization names of the Baltimore set.
REPORTER_ORGS = ("JOHNS HOPKINS UNIVERSITY", "HUGO W. MOSER RES INST KENNEDY KRIEGER")
#: The NSF awardee name of the Baltimore set.
NSF_AWARDEE = "Johns Hopkins University"
#: ROR ids for DataCite: JHU, JH Medicine, Kennedy Krieger, Sidney Kimmel.
ROR_IDS = frozenset({"00za53h95", "037zgn354", "05q6tgt32", "05m5b8x20"})

_ALSO_BALTIMORE = re.compile(r"\b(kennedy[\s-]+krieger|jhpiego)\b", re.IGNORECASE)
# The excluded places of §5.1, as raw affiliation text names them.
_EXCLUDED_PLACE = re.compile(
    r"\b(applied physics laboratory|laurel|all children[’']?s|(?:st\.?|saint)\s+petersburg"
    r"|sais|bologna|nanjing|sibley|suburban|howard county|peninsula regional|aramco|dhahran"
    r"|kampala)\b",
    re.IGNORECASE,
)

#: The roles that make an authorship senior enough to qualify a work (§5.2).
_QUALIFYING_ROLES = frozenset({"corresponding", "last"})


def is_baltimore_affiliation(text: str) -> bool:
    """Whether a raw affiliation string is a Baltimore Hopkins one (§5.1): it names Johns
    Hopkins (``jhu_rules.is_hopkins_affiliation``), Kennedy Krieger or Jhpiego, and names
    none of the excluded places."""
    if not text:
        return False
    if not (is_hopkins_affiliation(text) or _ALSO_BALTIMORE.search(text)):
        return False
    return not _EXCLUDED_PLACE.search(text)


def qualifies_openalex(work: dict) -> tuple[bool, str | None, dict | None]:
    """Whether an OpenAlex work qualifies (§5.2): some individual authorship is
    corresponding or last and lists a Baltimore institution id, or, listing no
    institution, a raw affiliation string passing ``is_baltimore_affiliation``.

    Returns ``(True, role_basis, authorship)`` for the most senior such authorship
    (``ROLE_RANK``; the earliest listed on a tie), ``role_basis`` being ``"corresponding"``
    or ``"last"``; ``(False, None, None)`` when none qualifies."""
    best: dict | None = None
    best_role: str | None = None
    for auth in work.get("authorships") or []:
        r = role(auth)
        if r not in _QUALIFYING_ROLES or not is_individual(auth):
            continue
        if not at_institution(auth, BALTIMORE_OPENALEX_IDS, is_baltimore_affiliation,
                              raw_strings="only_when_no_institutions"):
            continue
        if best_role is None or ROLE_RANK[r] < ROLE_RANK[best_role]:
            best, best_role = auth, r
    if best is None:
        return False, None, None
    return True, best_role, best
