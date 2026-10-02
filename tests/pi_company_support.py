"""Shared helpers for the PI Companies tests (no ``test_`` prefix, so not collected).

``companies_dir`` matters on the host: the suite runs inside the production checkout,
whose ``profiles/`` is the live bind mount, so every test that can reach
``export_companies_file`` points the export at a temporary directory first.
"""

from datetime import date

from src.models import PiCompany
from src.services import pi_companies, user_deletion

PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/39433569/"
EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001783735&type=D"
#: The Wikidata sandbox item: a real item that claims nothing about anyone.
WIKIDATA_URL = "https://www.wikidata.org/wiki/Q4115189"

#: Package D's ``evidence`` shape for a discovered row (src/services/company_discovery.py).
#: The sentence, the accession number, the amount and the related-person roles are the ones
#: spec F14/F15 quote; the filing date is illustrative.
DISCOVERED_EVIDENCE = {
    "coi": [
        {
            "pmid": "39433569",
            "year": 2024,
            "former": False,
            "pi_role": "founder",
            "company_name": "DELFI Diagnostics",
            "sentence": "V.E.V. is a founder of DELFI Diagnostics, serves on the Board of "
                        "Directors, and owns DELFI Diagnostics stock.",
            "url": PUBMED_URL,
        },
    ],
    "wikidata": [],
    "form_d": {
        "status": "ok",
        "funding_usd": 224_999_876,
        "funding_as_of": "2022-03-15",
        "filings_page": EDGAR_URL,
        "not_fetched": 0,
        "filings": [
            {
                "accession": "0001783735-22-000002",
                "filing_date": "2022-03-15",
                "form": "D",
                "cik": "0001783735",
                "file_num": None,
                "issuer_name": "DELFI Diagnostics, Inc.",
                "is_amendment": False,
                "previous_accession": None,
                "total_offering_amount": 224_999_876,
                "total_offering_amount_raw": "224999876",
                "total_amount_sold": 224_999_876,
                "total_amount_sold_raw": "224999876",
                "amount_note": None,
                "pi_listed": True,
                "pi_relationships": ["Executive Officer", "Director", "Promoter"],
                "url": "https://www.sec.gov/Archives/edgar/data/1783735/000178373522000002/primary_doc.xml",
                "counted": True,
            },
        ],
    },
    "former": False,
}


def companies_dir(monkeypatch, tmp_path):
    """Point the export and the deletion teardown at ``tmp_path/companies``; return it."""
    target = tmp_path / "companies"
    monkeypatch.setattr(pi_companies, "COMPANIES_DIR", target)
    monkeypatch.setattr(user_deletion, "_COMPANIES_DIR", target)
    return target


async def seed_company(db, user, **overrides) -> PiCompany:
    """A discovered ``suggested`` DELFI row with a Form D floor, flushed. ``overrides``
    replace any column; ``normalized_name`` follows ``company_name`` unless given."""
    data = dict(
        user_id=user.id,
        company_name="DELFI Diagnostics",
        pi_role="founder",
        funding_usd=224_999_876,
        funding_as_of=date(2022, 3, 15),
        source_url=PUBMED_URL,
        funding_source_url=EDGAR_URL,
        status="suggested",
        origin="discovered",
        evidence=DISCOVERED_EVIDENCE,
    )
    data.update(overrides)
    data.setdefault("normalized_name", pi_companies.normalize_company_name(data["company_name"]))
    row = PiCompany(**data)
    db.add(row)
    await db.flush()
    return row
