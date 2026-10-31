"""Role and place tests on one OpenAlex authorship, shared by industry evidence and
landscaping (spec 2026-10-30 §5.2).

An authorship is one entry of an OpenAlex work's ``authorships`` list: a dict with
``author_position``, ``is_corresponding``, ``institutions`` (each with an ``id`` URL),
``raw_affiliation_strings``, ``raw_author_name`` and ``author.display_name``.
"""
import re
from collections.abc import Callable
from typing import Literal

#: Seniority of a `role`, most senior first; any other role ranks last.
ROLE_RANK = {"corresponding": 0, "last": 1, "first": 2}

# R1 (docs/specs/2026-08-13-jhu-instance-rules-design.md): consortium and collective
# authorship is not a named individual author.
_COLLECTIVE = re.compile(
    r"\b(consortium|consortia|groups?|networks?|investigators|study|studies)\b", re.IGNORECASE,
)


def _oaid(url: str | None) -> str:
    return (url or "").rsplit("/", 1)[-1]


def role(auth: dict) -> str:
    """``corresponding`` when OpenAlex flags the authorship so, else its
    ``author_position`` (``first`` or ``last``), else ``middle``."""
    if auth.get("is_corresponding"):
        return "corresponding"
    return auth.get("author_position") or "middle"


def at_institution(auth: dict, ids: frozenset[str], matcher: Callable[[str], bool], *,
                   raw_strings: Literal["always", "only_when_no_institutions"]) -> bool:
    """Whether the authorship is at a place: one of its institutions' OpenAlex ids (the
    bare ``I…`` form) is in ``ids``, or one of its raw affiliation strings passes
    ``matcher``. ``raw_strings="always"`` consults the raw strings whatever institutions
    are listed (industry evidence); ``"only_when_no_institutions"`` consults them only when
    the authorship lists no institution (landscaping, spec §5.2)."""
    institutions = auth.get("institutions") or []
    if any(_oaid(i.get("id")) in ids for i in institutions):
        return True
    if raw_strings == "only_when_no_institutions" and institutions:
        return False
    return any(matcher(s) for s in auth.get("raw_affiliation_strings") or [])


def is_individual(auth: dict) -> bool:
    """False when the authorship's display name or raw printed name names a collective
    (consortium, group, network, investigators, study); True otherwise, including when
    it carries no name at all."""
    names = ((auth.get("author") or {}).get("display_name"), auth.get("raw_author_name"))
    return not any(_COLLECTIVE.search(n) for n in names if n)
