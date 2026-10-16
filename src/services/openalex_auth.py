"""The OpenAlex API key (``OPENALEX_API_KEY``).

Since October 2026 OpenAlex refuses keyless cursor paging past the first page (HTTP 401,
"API key required for cursor paging"), and a free key raises the daily budget tenfold
(help.openalex.org/api/authentication). The key travels as an ``Authorization: Bearer``
header, which OpenAlex accepts like the ``api_key`` query parameter, so it is never part
of a URL that httpx logs or an exception carries. This is the only module that reads it.
"""
from src.config import get_settings


def openalex_headers() -> dict[str, str]:
    """``{"Authorization": "Bearer <key>"}`` when a key is configured, else ``{}`` (keyless,
    first page only)."""
    key = get_settings().openalex_api_key.strip()
    return {"Authorization": f"Bearer {key}"} if key else {}
