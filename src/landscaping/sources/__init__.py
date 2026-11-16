"""The record types every landscaping source returns and intake consumes (plan Task 3.1).

A source maps each raw record to one `DocumentRecord`; a paged fetch returns a `SliceResult`
whose `expected` (the source's own total) and `received` counts make the completeness check
of spec §12.4 possible. Fields beyond the plan's core set default to empty, so a source fills
only what it has.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class DocumentRecord:
    """One source record, normalized for intake (spec §6.1, §7.2).

    `kind` is work | preprint | grant | patent | jhtv | dataset. `ids` holds every identifier
    the source gives, keyed by scheme (doi, pmid, pmcid, openalex, appl_id, core_project,
    nsf_award, patent_application, jhtv_case, datacite_doi, geo_accession). `role_basis` says
    why the record qualifies (spec §5.2): an OpenAlex role ("corresponding", "last", "first"),
    "pubmed_last_author", "grant_pi", "assignee" or "jhtv"; None means not qualified.
    """

    kind: str
    ids: dict[str, str]
    title: str
    summary: str | None
    published: date | None
    doc_type: str
    language: str | None
    license: str | None
    authorships: list[dict] | None
    role_basis: str | None
    raw_qualifier: dict = field(default_factory=dict)
    # Where `summary` came from: "pubmed", "openalex", "reporter", "nsf", "odp", "jhtv",
    # "datacite" or "geo" (spec §6.1 summary precedence).
    summary_source: str | None = None
    # OpenAlex `is_retracted` or a PubMed retraction publication type (spec §5.3).
    retracted: bool = False
    # The senior person behind the record, most senior first, for the lab key (spec §5.2):
    # each {"name": str, "orcid": str | None, "openalex_author_id": str | None,
    # "nih_pi_profile_id": str | None}.
    senior_names: list[dict] = field(default_factory=list)
    # Identifiers of other records this one names, by scheme, e.g. a dataset's related
    # {"doi": [...], "pmid": [...]} (D3) or a preprint's published version.
    related_ids: dict[str, list[str]] = field(default_factory=dict)
    # Source-specific fields intake stores as they are: grant periods and amounts, patent
    # claims and inventors, JHTV categories, PubMed MeSH and grants.
    extra: dict = field(default_factory=dict)


@dataclass
class SliceResult:
    """One paged fetch: the mapped rows, the source's reported total, and how many arrived."""

    rows: list[DocumentRecord]
    expected: int
    received: int
