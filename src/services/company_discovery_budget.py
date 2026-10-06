"""The company discovery COI extraction budget and ledger (spec 2026-10-05 §6.2, D36,
D37, E-12; the COI audit record, D62c).

Every extraction call is reserved before it is sent, at COI_RESERVE_USD, in a
`company_discovery_usage` row committed in a short transaction of its own, and settled
after it, at the reply's priced usage, in another short transaction that also writes the
statement's `company_discovery_coi_ledger` row (outcome and verified claims). A paid
result therefore survives whatever happens to the job afterwards. The ceiling is
`settings.company_discovery_daily_usd_limit` over a rolling 24 h, each row counted at its
settled cost or, while unsettled, its reservation. A reservation that would cross it
raises BudgetExhausted; once the calls already in flight have settled, the job asks
`CoiBudget.wake_time()` when `slots` reservations fit again (read then, because a settled
call usually costs far less than its reservation) and raises `job_queue.JobDeferred` with
it. An unpriced model makes no call at all (`CoiBudget.priced`).

The ledger key is (PI, PMID, statement hash, name-forms hash): a statement is paid for
once per way of naming the PI, and a name change sends it again. Only "ok" and "skipped"
outcomes are terminal (COI_LEDGER_TERMINAL); "unavailable" is sent again on the next run,
until the key has had COI_MAX_ATTEMPTS billed failures. A call is reserved at
`reservation_usd` of its prompt's size, at least COI_RESERVE_USD; one whose reservation
exceeds the whole ceiling is never sent (CallTooLarge).

Concurrency: one worker runs one job at a time (WORKER_LOCK_KEY) and, within a run, an
asyncio lock serialises these short transactions, so a reservation's sum-then-insert needs
no database lock. They run on sessions of their own on the job session's bind
(`side_session_factory`), never on the job session, which is between commits while the
calls run.

Must not import the industry modules (tests/unit/test_company_discovery_isolation.py)."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from src.config import get_settings
from src.database import get_session_factory
from src.models import CompanyDiscoveryCoiLedger, CompanyDiscoveryUsage
from src.models.pi_company import COI_LEDGER_TERMINAL, COI_MAX_ATTEMPTS
from src.services.company_sources import PiName, coi_llm
from src.services.company_sources.coi_founders import FounderClaim, locate_pi
from src.services.company_sources.coi_llm import CoiOutcome, _pi_block
from src.services.llm_pricing import PRICES, cost_for_tokens

#: What one extraction call is reserved at. Claude Opus 5.5 ($4 in, $20 out, $5 cache
#: write per MTok, src/services/llm_pricing.py): the 4,000-token output ceiling is $0.08,
#: a 20,000-token input (a consortium author list) $0.10 at the cache-write rate, and a
#: server-side fallback after a decline can bill a second attempt; $0.30 covers that. The
#: settle replaces it with the priced usage.
COI_RESERVE_USD = Decimal("0.30")
#: `CoiOutcome.reason` prefixes of a failure no model answered (nothing billed): an SDK
#: error (`coi_llm._api_error_reason`), a raise in the extraction, a cancellation, or no
#: call at all. They never count toward COI_MAX_ATTEMPTS.
UNBILLED_FAILURE_REASONS = ("api_", "client_error", "error", "cancelled", "prompt_missing", "too_large")
#: Characters per input token assumed when sizing a larger prompt's reservation: below
#: English's ~4, so an author list of initials and identifiers is not under-reserved.
CHARS_PER_TOKEN = 3
WINDOW = timedelta(hours=24)
#: Added to a deferral's wake-up time, so the job wakes after the freeing row has left
#: the window rather than just before.
DEFER_MARGIN = timedelta(minutes=1)
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
#: A row's dollars for the ceiling: the settled cost, else the reservation.
_SPEND = func.coalesce(CompanyDiscoveryUsage.cost_usd, CompanyDiscoveryUsage.reserved_usd)


@dataclass(frozen=True)
class LedgerKey:
    pmid: str
    statement_hash: str
    name_forms_hash: str


@dataclass(frozen=True)
class Reservation:
    usage_id: uuid.UUID
    key: LedgerKey
    reserved_usd: Decimal


class CallTooLarge(Exception):
    """One call's reservation exceeds the whole ceiling: it could never be sent."""


def reservation_usd(model: str, prompt_chars: int | None) -> Decimal:
    """What a call is reserved at: COI_RESERVE_USD, or more for a prompt larger than that
    constant assumes (neither the author list nor the statement has an upper bound):
    every character at CHARS_PER_TOKEN at the cache-write rate plus the full output
    ceiling (coi_llm.MAX_TOKENS), twice for a server-side fallback."""
    price = PRICES.get(model)
    if not prompt_chars or price is None:
        return COI_RESERVE_USD
    tokens_in = -(-prompt_chars // CHARS_PER_TOKEN)
    one = (Decimal(tokens_in) * price.cache_write_5m
           + Decimal(coi_llm.MAX_TOKENS) * price.output) / Decimal(1_000_000)
    return max(COI_RESERVE_USD, (2 * one).quantize(Decimal("0.0001")))


class BudgetExhausted(Exception):
    """A reservation would cross the ceiling. The job asks `CoiBudget.wake_time()` when to
    wake once the calls in flight have settled, for at least `amount`, the refused
    reservation."""

    def __init__(self, message: str, amount: Decimal = COI_RESERVE_USD) -> None:
        super().__init__(message)
        self.amount = amount


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ledger_key(record: dict, name: PiName) -> LedgerKey | None:
    """The record's ledger key; None when the PI is not located on it (such a record never
    passes the gate). The forms hash covers the PI block the prompt sends
    (`coi_llm._pi_block`), so it changes exactly when the way the PI is named does."""
    forms = locate_pi(record, name)
    if forms is None:
        return None
    statement = (record.get("coi_statement") or "").strip()
    return LedgerKey(str(record.get("pmid") or ""), _sha256(statement), _sha256(_pi_block(name, forms)))


def claims_to_json(claims: Sequence[FounderClaim]) -> list[dict]:
    return [dataclasses.asdict(claim) for claim in claims]


def claims_from_json(value: object) -> list[FounderClaim]:
    """The FounderClaims a ledger row stored; a malformed entry is skipped."""
    out: list[FounderClaim] = []
    for item in value if isinstance(value, list) else []:
        try:
            year = item.get("year")
            out.append(FounderClaim(
                str(item["company_name"]), str(item["pi_role"]), str(item["pmid"]),
                year if isinstance(year, int) else None, str(item["sentence"]),
                item.get("former") is True,
            ))
        except (AttributeError, KeyError, TypeError):
            continue
    return out


def _int(value: object) -> int:
    return value if type(value) is int else 0


def _entries_cost(entries: Sequence[object]) -> Decimal | None:
    """Dollars for the billed entries, or None when one names an unpriced model."""
    total = Decimal(0)
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("billed", True):
            continue
        cost = cost_for_tokens(
            str(entry.get("model") or ""), input_tokens=_int(entry.get("input_tokens")),
            output_tokens=_int(entry.get("output_tokens")),
            cache_read=_int(entry.get("cache_read_input_tokens")),
            cache_creation=_int(entry.get("cache_creation_input_tokens")),
        )
        if cost is None:
            return None
        total += cost
    return total


def call_cost(outcome: CoiOutcome, *, model: str, reserved: Decimal) -> Decimal:
    """What a settled call cost (A8): nothing when no call was made ("skipped") or the API
    answered an error status (`entries == []`); the priced entries, else the priced token
    counts at `model`; the reservation when the price is unknown (no usage, an unpriced
    entry): never a silent $0."""
    if outcome.status == "skipped":
        return Decimal(0)
    if outcome.entries is not None:
        cost = _entries_cost(outcome.entries)
    elif outcome.usage is not None:
        u = outcome.usage
        cost = cost_for_tokens(
            model, input_tokens=_int(u.get("input_tokens")), output_tokens=_int(u.get("output_tokens")),
            cache_read=_int(u.get("cache_read_input_tokens")),
            cache_creation=_int(u.get("cache_creation_input_tokens")),
        )
    else:
        return reserved
    return reserved if cost is None else cost


def _token_sums(outcome: CoiOutcome) -> dict[str, int | None]:
    if outcome.entries is not None:
        billed = [e for e in outcome.entries if isinstance(e, dict) and e.get("billed", True)]
        return {f: sum(_int(e.get(f)) for e in billed) for f in _TOKEN_FIELDS}
    if outcome.usage is not None:
        return {f: _int(outcome.usage.get(f)) for f in _TOKEN_FIELDS}
    return dict.fromkeys(_TOKEN_FIELDS)


def _served_by(outcome: CoiOutcome) -> str | None:
    models = [e.get("model") for e in outcome.entries or [] if isinstance(e, dict) and e.get("model")]
    return str(models[-1]) if models else None


def release_time(
    window: Sequence[tuple[datetime, Decimal]], *, spend: Decimal, limit: Decimal,
    needed: Decimal, now: datetime,
) -> datetime:
    """When the rolling window has room for `needed` more dollars: the expiry (created_at
    + WINDOW) of the oldest row whose leaving brings `spend` down to `limit - needed`
    (`window` is oldest first), plus DEFER_MARGIN; now + WINDOW + DEFER_MARGIN when no
    window could hold `needed`; now + DEFER_MARGIN when there is room already."""
    target = limit - needed
    if target < 0:
        return now + WINDOW + DEFER_MARGIN
    remaining = spend
    if remaining <= target:
        return now + DEFER_MARGIN
    for created_at, amount in window:
        remaining -= amount
        if remaining <= target:
            return created_at + WINDOW + DEFER_MARGIN
    return now + WINDOW + DEFER_MARGIN


async def _window(session: AsyncSession) -> tuple[datetime, list[tuple[datetime, Decimal]]]:
    """(the database's clock_timestamp(), every usage row of the last WINDOW as
    (created_at, dollars), oldest first)."""
    now = await session.scalar(select(func.clock_timestamp()))
    rows = (await session.execute(
        select(CompanyDiscoveryUsage.created_at, _SPEND)
        .where(CompanyDiscoveryUsage.created_at >= now - WINDOW)
        .order_by(CompanyDiscoveryUsage.created_at)
    )).all()
    return now, [(created_at, Decimal(amount)) for created_at, amount in rows]


def side_session_factory(db: AsyncSession) -> Callable[[], AsyncSession]:
    """Sessions of their own on `db`'s bind (the `profile_publish._writer_session`
    pattern): a fresh connection for an engine bind; a savepoint on the same connection
    when `db` is bound to one (tests); the app's factory when `db` has no bind."""
    bind = db.bind
    if bind is None:
        return get_session_factory()
    if isinstance(bind, AsyncConnection):
        return lambda: AsyncSession(bind=bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    return lambda: AsyncSession(bind=bind, expire_on_commit=False)


class CoiBudget:
    """One discovery run's view of the budget and the ledger for one PI (module docstring)."""

    def __init__(self, sessions: Callable[[], AsyncSession], user_id: uuid.UUID, *,
                 model: str, limit_usd: Decimal, slots: int) -> None:
        self._sessions = sessions
        self._lock = asyncio.Lock()
        self.user_id = user_id
        self.model = model
        self.limit_usd = limit_usd
        self.slots = slots

    @property
    def priced(self) -> bool:
        """The configured model has a price (`llm_pricing.PRICES`): fail closed otherwise."""
        return self.model in PRICES

    async def terminal(self, keys: Sequence[LedgerKey]) -> dict[LedgerKey, list[FounderClaim] | None]:
        """Every key in `keys` that is not sent again -> its stored claims (ledger row
        terminal), or None when it is "unavailable" after COI_MAX_ATTEMPTS billed failures."""
        if not keys:
            return {}
        ledger = CompanyDiscoveryCoiLedger
        async with self._lock, self._sessions() as session:
            rows = (await session.execute(
                select(ledger.pmid, ledger.statement_hash, ledger.name_forms_hash, ledger.outcome,
                       ledger.claims)
                .where(ledger.user_id == self.user_id,
                       or_(ledger.outcome.in_(COI_LEDGER_TERMINAL),
                           ledger.attempts >= COI_MAX_ATTEMPTS),
                       ledger.pmid.in_(sorted({k.pmid for k in keys})))
            )).all()
        wanted = set(keys)
        found: dict[LedgerKey, list[FounderClaim] | None] = {}
        for row in rows:
            key = LedgerKey(row.pmid, row.statement_hash, row.name_forms_hash)
            if key in wanted:
                found[key] = (claims_from_json(row.claims)
                              if row.outcome in COI_LEDGER_TERMINAL else None)
        return found

    async def reserve(self, key: LedgerKey, *, prompt_chars: int | None = None) -> Reservation:
        """Commit a reservation for one call (`reservation_usd` of ``prompt_chars``), or
        raise BudgetExhausted, or CallTooLarge when it exceeds the whole ceiling (nothing
        written either way)."""
        amount = reservation_usd(self.model, prompt_chars)
        if amount > self.limit_usd:
            raise CallTooLarge(f"company discovery: one call reserves ${amount}, over the ceiling")
        async with self._lock, self._sessions() as session:
            _now, window = await _window(session)
            spend = sum((spent for _, spent in window), Decimal(0))
            if spend + amount > self.limit_usd:
                raise BudgetExhausted("company discovery: the COI budget is spent", amount)
            usage_id = uuid.uuid4()
            await session.execute(insert(CompanyDiscoveryUsage).values(
                id=usage_id, user_id=self.user_id, pmid=key.pmid, model=self.model,
                status="reserved", reserved_usd=amount, created_at=func.clock_timestamp(),
            ))
            await session.commit()
        return Reservation(usage_id, key, amount)

    async def settle(self, reservation: Reservation, outcome: CoiOutcome) -> None:
        """Settle the reservation and write the statement's ledger row, one transaction."""
        cost = call_cost(outcome, model=self.model, reserved=reservation.reserved_usd)
        if outcome.status == "ok":
            claims: list[dict] | None = claims_to_json(outcome.claims)
        else:
            claims = [] if outcome.status == "skipped" else None
        key = reservation.key
        billed_failure = outcome.status == "unavailable" and (
            any(isinstance(e, dict) and e.get("billed", True) for e in outcome.entries or [])
            # A reply with no usage block was still a billed answer; no reply was not.
            or (outcome.entries is None
                and not (outcome.reason or "").startswith(UNBILLED_FAILURE_REASONS)))
        failed = 1 if billed_failure else 0
        async with self._lock, self._sessions() as session:
            await session.execute(
                update(CompanyDiscoveryUsage)
                .where(CompanyDiscoveryUsage.id == reservation.usage_id)
                .values(status="settled", cost_usd=cost, served_by_model=_served_by(outcome),
                        usage_by_model=outcome.entries, settled_at=func.clock_timestamp(),
                        **_token_sums(outcome))
            )
            stmt = pg_insert(CompanyDiscoveryCoiLedger).values(
                id=uuid.uuid4(), user_id=self.user_id, pmid=key.pmid,
                statement_hash=key.statement_hash, name_forms_hash=key.name_forms_hash,
                outcome=outcome.status, claims=claims, attempts=failed,
                processed_at=func.clock_timestamp(),
            )
            await session.execute(stmt.on_conflict_do_update(
                constraint="uq_company_discovery_coi_ledger_key",
                set_={"outcome": stmt.excluded.outcome, "claims": stmt.excluded.claims,
                      "attempts": CompanyDiscoveryCoiLedger.attempts + failed,
                      "processed_at": func.clock_timestamp()},
            ))
            await session.commit()

    async def wake_time(self, refused: Decimal = COI_RESERVE_USD) -> datetime:
        """When `slots` reservations fit again (and at least the ``refused`` one), read now:
        `release_time` over the window as it stands after the calls in flight settled
        (their reservations replaced by their usually much smaller costs), not as it stood
        when a reservation was refused."""
        async with self._lock, self._sessions() as session:
            now, window = await _window(session)
        spend = sum((amount for _, amount in window), Decimal(0))
        # As many reservations as the ceiling can ever hold, up to `slots` (at least one).
        needed = max(refused,
                     COI_RESERVE_USD * max(1, min(self.slots, int(self.limit_usd // COI_RESERVE_USD))))
        return release_time(window, spend=spend, limit=self.limit_usd, needed=needed, now=now)


def budget_for(db: AsyncSession, user_id: uuid.UUID, *, slots: int) -> CoiBudget:
    """The budget for one run, from the settings: the COI model and the daily ceiling."""
    settings = get_settings()
    return CoiBudget(
        side_session_factory(db), user_id, model=settings.llm_coi_model,
        limit_usd=Decimal(str(settings.company_discovery_daily_usd_limit)), slots=slots,
    )
