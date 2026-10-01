"""The USPTO Open Data Portal transport shared by the hub's prior-art tool
(`patents.py`, behind `search_prior_art`) and the worker's inventor source
(`industry_sources/uspto_inventor.py`): base URL, auth header, and one pacer per
process at the interval `patents.py` measured (1.0 s; see its pacing note).

Client options stay per caller (FA-4): `patents.py` needs `follow_redirects=True`
for the pgpub `fileLocationURI` 302 and a 60 s timeout; `uspto_inventor` keeps 30 s
and no redirects. Key choice stays per caller too (P0-07: no worker fallback)."""
from __future__ import annotations

import httpx

from src.services.http_pacing import Pacer

ODP_SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
ODP_PACE_INTERVAL = 1.0
ODP_PACER = Pacer(lambda: ODP_PACE_INTERVAL)


def odp_headers(key: str) -> dict[str, str]:
    return {"X-API-KEY": key}


def odp_client(*, timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)
