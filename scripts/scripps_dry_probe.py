"""The Scripps dry probe (spec 2026-11-20 §8): the window's counts per source, against each
live API, before the run.

    python scripts/scripps_dry_probe.py

Run inside a one-off container of the stack's app image, so the stack's keys are used. Writes
nothing to the database; prints one JSON object (indent 1, sorted keys) for the window ending
today (``LANDSCAPE_WINDOW_DAYS``):

  openalex_works          works the OpenAlex lineage filter lists (articles, preprints, reviews)
  openalex_reported_total OpenAlex's own total for that filter
  qualifying_works        works that qualify (senior author and campus rule, qualifies_openalex)
  campus_rejected_works   works that do not qualify but have a senior authorship listing Scripps
                          Research that the campus rule rejects
  accepted_addresses      up to 20 qualifying addresses, emails redacted
  rejected_addresses      up to 20 campus-rejected addresses, emails redacted
  pubmed_count            ESearch count of the affiliation term over the window by [dp]
  reporter_rows           RePORTER's total per fiscal year the window touches
  nsf_awards              NSF's total of awards in the window
  window_from, window_to  the window's dates

The addresses name people's affiliations: keep the output outside the repository. Exits 0;
a failed request RAISES.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from datetime import date, timedelta

from src.landscaping.institutions import (
    REPORTER_ORGS,
    SCRIPPS_OPENALEX_ID,
    at_scripps_la_jolla,
    names_excluded_campus,
    qualifies_openalex,
)
from src.landscaping.sources import nsf, openalex_works, pubmed_works, reporter
from src.services import openalex, pubmed
from src.services.authorship_roles import is_individual, role

_SENIOR_ROLES = frozenset({"corresponding", "last"})
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _bare(url: object) -> str:
    return str(url or "").rsplit("/", 1)[-1]


def _lists_scripps(auth: dict) -> bool:
    return any(_bare((i or {}).get("id")) == SCRIPPS_OPENALEX_ID
               for i in auth.get("institutions") or [])


def _tagged_strings(auth: dict) -> list[str]:
    """The raw strings of the authorship's ``affiliations`` entries tagged with Scripps
    Research."""
    return [
        a.get("raw_affiliation_string") or ""
        for a in auth.get("affiliations") or []
        if SCRIPPS_OPENALEX_ID in {_bare(x) for x in a.get("institution_ids") or []}
        and a.get("raw_affiliation_string")
    ]


def _raw_strings(auth: dict) -> list[str]:
    return [s for s in auth.get("raw_affiliation_strings") or [] if s]


def _redact(text: str) -> str:
    return _EMAIL.sub("<email>", text)


def senior_rejected_by_campus(auth: dict) -> bool:
    """Whether a corresponding or last, individual authorship lists Scripps Research
    (``SCRIPPS_OPENALEX_ID``) but fails ``at_scripps_la_jolla``."""
    return (
        role(auth) in _SENIOR_ROLES
        and is_individual(auth)
        and _lists_scripps(auth)
        and not at_scripps_la_jolla(auth)
    )


def _accepted_address(auth: dict) -> str:
    """The qualifying authorship's tagged raw strings that name no excluded campus, else its
    raw strings, joined with `` | ``."""
    passing = [s for s in _tagged_strings(auth) if not names_excluded_campus(s)]
    return " | ".join(passing or _raw_strings(auth))


def _rejected_address(work: dict) -> str | None:
    """The first campus-rejected senior authorship's tagged raw strings (its raw strings when
    none is tagged); None when the work has no such authorship."""
    for auth in work.get("authorships") or []:
        if senior_rejected_by_campus(auth):
            return " | ".join(_tagged_strings(auth) or _raw_strings(auth))
    return None


def summarize(works: list[dict], *, sample: int = 20, seed: int = 0) -> dict:
    """Counts and sampled addresses for OpenAlex works (see the module docstring's keys
    ``openalex_works`` to ``rejected_addresses``). Samples hold at most ``sample`` addresses,
    drawn with ``random.Random(seed).sample``; an empty address is not sampled."""
    accepted: list[str] = []
    rejected: list[str] = []
    qualifying = campus_rejected = 0
    for work in works:
        ok, _, auth = qualifies_openalex(work)
        if ok:
            qualifying += 1
            address = _accepted_address(auth or {})
            if address:
                accepted.append(_redact(address))
            continue
        address = _rejected_address(work)
        if address is not None:
            campus_rejected += 1
            if address:
                rejected.append(_redact(address))
    rng = random.Random(seed)
    return {
        "openalex_works": len(works),
        "qualifying_works": qualifying,
        "campus_rejected_works": campus_rejected,
        "accepted_addresses": rng.sample(accepted, min(sample, len(accepted))),
        "rejected_addresses": rng.sample(rejected, min(sample, len(rejected))),
    }


def _fiscal_year(day: date) -> int:
    """NIH's fiscal year, which starts on 1 October."""
    return day.year + 1 if day.month >= 10 else day.year


async def collect(today: date, window_days: int) -> dict:
    """The live counts for the window ``today - window_days`` to ``today``: ``summarize`` of
    every OpenAlex work the source's lineage filter lists (each completed by
    ``openalex_works._complete``), plus ``openalex_reported_total``, ``pubmed_count``,
    ``reporter_rows`` (fiscal year as a string to total), ``nsf_awards``, ``window_from``
    and ``window_to``. RAISES on a failed request."""
    start = today - timedelta(days=window_days)
    filter_ = (
        f"{openalex_works.LINEAGE_FILTER},{openalex_works._TYPE_FILTER},"
        f"from_publication_date:{start.isoformat()},to_publication_date:{today.isoformat()}"
    )
    works: list[dict] = []
    reported = 0
    async for page, count in openalex.iter_works(filter_, openalex_works.SELECT):
        reported = count
        for work in page:
            works.append(await openalex_works._complete(work))
    result = summarize(works)
    result["openalex_reported_total"] = reported
    term = (
        f'{pubmed_works.AFFILIATION_TERM} AND '
        f'("{start:%Y/%m/%d}"[dp] : "{today:%Y/%m/%d}"[dp])'
    )
    result["pubmed_count"], _ = await pubmed.esearch_page(term, retstart=0, retmax=0)
    result["reporter_rows"] = {
        str(fy): await reporter._reported_total(
            {"org_names_exact_match": list(REPORTER_ORGS), "fiscal_years": [fy]}
        )
        for fy in range(_fiscal_year(start), _fiscal_year(today) + 1)
    }
    result["nsf_awards"] = (await nsf.fetch_nsf(start, today)).expected
    result["window_from"] = start.isoformat()
    result["window_to"] = today.isoformat()
    return result


def main(argv: list[str] | None = None) -> int:
    """Print ``collect``'s JSON for today and ``settings.landscape_window_days``; exits 0."""
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    from src.config import get_settings
    result = asyncio.run(collect(date.today(), get_settings().landscape_window_days))
    print(json.dumps(result, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
