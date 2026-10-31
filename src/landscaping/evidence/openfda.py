"""openFDA Drugs@FDA approvals per drug (spec §6.3, §7.7 step 3). Keyless openFDA allows 1,000
requests a day per IP, so every request, retries included, counts against
`landscape_openfda_daily_requests` for the current UTC day; past it a lookup raises
`DailyBudgetExhausted` without calling openFDA. The count is per process."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from src.config import get_settings
from src.landscaping import evidence
from src.services.http_pacing import Pacer

DRUGSFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
#: Keyless openFDA also caps a minute at 240 requests.
MIN_INTERVAL_S = 0.5
RESULT_LIMIT = 100


class DailyBudgetExhausted(RuntimeError):
    """Today's openFDA request budget is spent; retry after midnight UTC."""


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


class _BudgetPacer(Pacer):
    """A pacer that also counts each request start against the daily budget."""

    def __init__(self, interval: float) -> None:
        super().__init__(interval)
        self.day = ""
        self.used = 0

    async def wait(self) -> None:
        today = _today()
        if today != self.day:
            self.day, self.used = today, 0
        budget = get_settings().landscape_openfda_daily_requests
        if self.used >= budget:
            raise DailyBudgetExhausted(f"openFDA: {self.used} of {budget} requests used today")
        self.used += 1
        await super().wait()


_PACER = _BudgetPacer(MIN_INTERVAL_S)


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _approval_date(submissions: list[dict]) -> str | None:
    """The original approval's date (``YYYYMMDD``), from the ``ORIG`` submission marked
    ``AP``."""
    dates = [s.get("submission_status_date") for s in submissions
             if s.get("submission_type") == "ORIG" and s.get("submission_status") == "AP"]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def _application(raw: dict) -> dict:
    openfda = raw.get("openfda") or {}
    products = raw.get("products") or []
    return {
        "application_number": raw.get("application_number"),
        "sponsor_name": raw.get("sponsor_name"),
        "brand_names": list(openfda.get("brand_name") or []),
        "generic_names": list(openfda.get("generic_name") or []),
        "marketing_statuses": sorted({p.get("marketing_status") for p in products
                                      if p.get("marketing_status")}),
        "approval_date": _approval_date(raw.get("submissions") or []),
    }


async def fda_approvals(generic_name: str) -> list[dict]:
    """Every Drugs@FDA application whose openFDA generic name is ``generic_name``; ``[]``
    when openFDA has none (its 404)."""
    escaped = generic_name.replace('"', "")
    body = await evidence.request_json(
        _make_client, DRUGSFDA_URL, pacer=_PACER, none_on_404=True,
        params={"search": f'openfda.generic_name:"{escaped}"', "limit": RESULT_LIMIT})
    return [_application(r) for r in (body or {}).get("results") or []]
