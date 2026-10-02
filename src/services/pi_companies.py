"""The staff Companies list per PI (spec 2026-10-02 §7.2, §7.3, §7.5).

A row is a company the PI founded, co-founded, advises or sits on the board of
(src/models/pi_company.py). Managers add rows by hand, which are confirmed at once (O9),
and confirm or reject the rows company discovery suggests. Only ``confirmed`` rows leave
the table: ``export_companies_file`` writes them to ``profiles/private/companies/<agent_id>.md``,
which the hub's ``retrieve_profile`` appends fenced as ``staff_company_record``
(src/agent/tools.py).

Every write here commits and then rewrites that file. The file follows the commit because
a file write cannot roll back; a failed write is logged, never raised, and the next write
or profile publish (src/services/profile_publish.py) repairs it.

This module's output reaches the hub, so it must not import the industry modules that
tests/unit/test_enrichment_isolation.py keeps out of profile and prompt code. Company
discovery (src/services/company_discovery.py) and the profile pipeline import this module,
so it imports neither of them. tests/unit/test_pi_companies.py checks both rules.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, PiCompany
from src.models.pi_company import PI_COMPANY_ROLES
from src.services.fs import atomic_write_text

logger = logging.getLogger(__name__)

#: One file per agent. src/services/user_deletion.py keeps its own copy
#: (``_COMPANIES_DIR``) so its tests patch it apart; tests/unit/test_pi_companies.py pins
#: the two equal. Module-level so tests can point it at a tmp_path.
COMPANIES_DIR = Path("profiles/private/companies")

#: The agent id names a file, so it passes the check user_deletion._SAFE_AGENT_ID applies
#: (tests/unit/test_pi_companies.py pins the pattern).
_SAFE_AGENT_ID = re.compile(r"[a-z0-9_-]{1,50}")

#: How a role reads on the manager card and in the exported file.
PI_COMPANY_ROLE_LABELS = {
    "founder": "Founder",
    "co_founder": "Co-founder",
    "board": "Board member",
    "advisor": "Advisor",
}

MAX_NAME_CHARS = 200
#: The ceiling of the column's Postgres bigint.
_MAX_FUNDING_USD = 2**63 - 1
#: The trailing words normalize_company_name drops (§7.5), compared case-folded.
_CORPORATE_SUFFIXES = frozenset({"inc", "llc", "ltd", "corp", "co", "gmbh", "ag", "sa", "plc"})


class CompanyValidationError(ValueError):
    """A refused write. The message is shown to the manager as it stands."""


class CompanyNotFoundError(LookupError):
    """No row with that id belongs to that PI. The routes answer 404."""


def normalize_company_name(name: str) -> str:
    """The merge and dedupe key (§7.5): NFKC, case-folded, punctuation removed,
    whitespace runs collapsed to one space, then trailing corporate suffixes (Inc, LLC,
    Ltd, Corp, Co, GmbH, AG, SA, PLC) dropped while another word remains.

    "DELFI Diagnostics" and "Delfi Diagnostics, Inc." both give "delfi diagnostics".
    Punctuation is removed rather than spaced so "S.A." and "L.L.C." still read as
    suffixes. Accents are kept. "" when nothing alphanumeric remains.
    """
    folded = unicodedata.normalize("NFKC", name or "").casefold()
    kept = "".join(ch for ch in folded if ch.isalnum() or ch.isspace())
    words = kept.split()
    while len(words) > 1 and words[-1] in _CORPORATE_SUFFIXES:
        words.pop()
    return " ".join(words)


def _is_edgar_url(url: str | None) -> bool:
    """True for an http(s) link on sec.gov: the mark of a Form D floor total."""
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme.lower() in ("http", "https") and (
        host == "sec.gov" or host.endswith(".sec.gov")
    )


def format_funding(
    funding_usd: int | None, funding_as_of: date | None, funding_source_url: str | None
) -> str | None:
    """The one wording of a funding figure, for the manager card, the assessment page and
    the hub's file. None when no figure is recorded.

    Whole dollars with thousands separators and no rounding ("$224,999,876"), so rounding
    can never overstate a floor. A figure whose ``funding_source_url`` is on sec.gov is the
    Form D floor total (O13): "at least $X raised (SEC Form D filings, latest {date})".
    Any other figure: "$X raised as of {date}". Dates are ISO (YYYY-MM-DD); a figure with
    no recorded date says so instead of borrowing one.
    """
    if funding_usd is None:
        return None
    amount = f"${funding_usd:,}"
    if _is_edgar_url(funding_source_url):
        if funding_as_of is None:
            return f"at least {amount} raised (SEC Form D filings)"
        return f"at least {amount} raised (SEC Form D filings, latest {funding_as_of.isoformat()})"
    if funding_as_of is None:
        return f"{amount} raised (date not recorded)"
    return f"{amount} raised as of {funding_as_of.isoformat()}"


def parse_funding_usd(raw: str | None) -> int | None:
    """A form's funding field. Blank is None; digits, with an optional leading "$" and
    thousands commas, are whole US dollars. Anything else is refused."""
    text = (raw or "").strip().replace(",", "").removeprefix("$").strip()
    if not text:
        return None
    if not re.fullmatch(r"[0-9]+", text):
        raise CompanyValidationError(
            "Funding must be a whole number of US dollars, such as 2500000."
        )
    value = int(text)
    if value > _MAX_FUNDING_USD:
        raise CompanyValidationError("That funding figure is too large.")
    return value


def parse_funding_as_of(raw: str | None) -> date | None:
    """A form's as-of field (an ISO date, which ``<input type="date">`` sends). Blank is None."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise CompanyValidationError(
            "The funding as-of date must be a date (YYYY-MM-DD)."
        ) from exc


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def _clean_name(company_name: str) -> tuple[str, str]:
    """(name as stored, its normalized form), or CompanyValidationError."""
    name = (company_name or "").strip()
    if not name:
        raise CompanyValidationError("Enter the company's name.")
    if _has_control_chars(name):
        raise CompanyValidationError("The company name must be one line of text.")
    normalized = normalize_company_name(name)
    if len(name) > MAX_NAME_CHARS or len(normalized) > MAX_NAME_CHARS:
        raise CompanyValidationError(
            f"Company names are limited to {MAX_NAME_CHARS} characters."
        )
    if not normalized:
        raise CompanyValidationError("The company name needs at least one letter or digit.")
    return name, normalized


def _check_role(pi_role: str) -> str:
    if pi_role not in PI_COMPANY_ROLES:
        raise CompanyValidationError(
            "Choose the PI's role: founder, co-founder, board member or advisor."
        )
    return pi_role


def http_url(value: object) -> str | None:
    """``value`` trimmed when it is one http(s) address with a host and no whitespace,
    else None. The manager card renders no other link, whoever wrote the value."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or _has_control_chars(text) or any(ch.isspace() for ch in text):
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return text


def _check_url(source_url: str) -> str:
    """The trimmed link, or CompanyValidationError unless it is one http(s) address."""
    text = (source_url or "").strip()
    if not text:
        raise CompanyValidationError("Give a source link for this company.")
    if _has_control_chars(text) or any(ch.isspace() for ch in text):
        raise CompanyValidationError("The source link must be one web address with no spaces.")
    if http_url(text) is None:
        raise CompanyValidationError("The source link must start with http:// or https://.")
    return text


def _check_funding(funding_usd: int | None, funding_as_of: date | None) -> None:
    if funding_usd is None:
        if funding_as_of is not None:
            raise CompanyValidationError(
                "Leave the as-of date blank when no funding figure is given."
            )
        return
    if isinstance(funding_usd, bool) or not isinstance(funding_usd, int):
        raise CompanyValidationError("Funding must be a whole number of US dollars.")
    if funding_usd < 0:
        raise CompanyValidationError("Funding cannot be negative.")
    if funding_usd > _MAX_FUNDING_USD:
        raise CompanyValidationError("That funding figure is too large.")
    if funding_as_of is None:
        raise CompanyValidationError("Give the date the funding figure is as of.")


def _duplicate_message(row: PiCompany) -> str:
    if row.status == "confirmed":
        return f"{row.company_name} is already on this PI's confirmed list."
    if row.status == "suggested":
        return f"{row.company_name} is already suggested for this PI: confirm it under Suggested."
    return (
        f"{row.company_name} was rejected for this PI earlier. Rejected names are kept so "
        "discovery never suggests them again, so it cannot be added here."
    )


def _text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _dicts(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dollars(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return f"${value:,}"


def evidence_for_display(evidence: object) -> dict:
    """A discovered row's ``evidence`` reduced to what the manager card shows.

    The shape is company discovery's (src/services/company_discovery.py): ``coi``
    statements, ``wikidata`` items, a ``form_d`` block with its ``filings``, and a
    top-level ``former`` flag. It is read defensively: a manual row's None, a missing key or
    an entry of another shape yields less, never an error, and every link passes
    ``http_url`` or is dropped. A filing's amount sold shows as dollars when it is a whole
    number, else as its ``amount_note`` or raw text (for example "Indefinite"), and
    ``counted`` says whether it is in the floor total. ``funding_note`` is the Form D
    block's ``note`` whatever its status (``unavailable``, ``ambiguous`` or another), so
    the card says why a suggestion carries no figure.
    """
    ev = evidence if isinstance(evidence, dict) else {}
    statements = []
    for item in _dicts(ev.get("coi")):
        text = _text(item.get("sentence"))
        if text is None:
            continue
        pmid = _text(item.get("pmid"))
        url = http_url(item.get("url"))
        if url is None and pmid is not None and re.fullmatch(r"[0-9]+", pmid):
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        statements.append({
            "text": text,
            "pmid": pmid,
            "year": _text(item.get("year")),
            "url": url,
            "former": item.get("former") is True,
        })
    wikidata = []
    for item in _dicts(ev.get("wikidata")):
        url = http_url(item.get("url"))
        label = _text(item.get("label")) or _text(item.get("item")) or url
        if label is not None:
            wikidata.append({"label": label, "url": url})
    form_d = ev.get("form_d") if isinstance(ev.get("form_d"), dict) else {}
    filings = []
    for item in _dicts(form_d.get("filings")):
        relationships = item.get("pi_relationships")
        filings.append({
            "filed": _text(item.get("filing_date")),
            "form": _text(item.get("form")),
            "sold": (
                _dollars(item.get("total_amount_sold"))
                or _text(item.get("amount_note"))
                or _text(item.get("total_amount_sold_raw"))
                or "not stated"
            ),
            "counted": item.get("counted") is True,
            "pi_listed": item.get("pi_listed") is True,
            "relationships": [
                r for r in relationships if isinstance(r, str)
            ] if isinstance(relationships, list) else [],
            "accession": _text(item.get("accession")),
            "url": http_url(item.get("url")),
        })
    status = _text(form_d.get("status"))
    return {
        "statements": statements,
        "wikidata": wikidata,
        "filings": filings,
        "form_d_status": status,
        "funding_note": _text(form_d.get("note")),
        "former": ev.get("former") is True or any(s["former"] for s in statements),
    }


async def list_companies(db: AsyncSession, user_id: uuid.UUID) -> list[PiCompany]:
    """Every row of the PI in all three statuses, in normalized-name order. Callers pick by
    status: the manager card lists confirmed and suggested rows, never rejected ones."""
    rows = await db.execute(
        select(PiCompany)
        .where(PiCompany.user_id == user_id)
        .order_by(PiCompany.normalized_name, PiCompany.id)
    )
    return list(rows.scalars())


async def _confirmed_rows(db: AsyncSession, user_id: uuid.UUID) -> list[PiCompany]:
    rows = await db.execute(
        select(PiCompany)
        .where(PiCompany.user_id == user_id, PiCompany.status == "confirmed")
        .order_by(PiCompany.normalized_name, PiCompany.id)
    )
    return list(rows.scalars())


async def confirmed_companies_for_agent(
    db: AsyncSession, agent_id: str | None
) -> list[PiCompany]:
    """The confirmed rows of the PI behind ``agent_id`` (AgentRegistry.agent_id ->
    user_id), in normalized-name order. [] when ``agent_id`` is None, names no agent, or
    names an agent with no linked user (a hub or specialist row)."""
    if not agent_id:
        return []
    user_id = (
        await db.execute(select(AgentRegistry.user_id).where(AgentRegistry.agent_id == agent_id))
    ).scalar_one_or_none()
    if user_id is None:
        return []
    return await _confirmed_rows(db, user_id)


async def _load_row(db: AsyncSession, user_id: uuid.UUID, company_id: uuid.UUID) -> PiCompany:
    """The PI's row ``company_id``, locked and re-read, so two reviews of one suggestion
    cannot both apply."""
    row = (
        await db.execute(
            select(PiCompany)
            .where(PiCompany.id == company_id, PiCompany.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise CompanyNotFoundError(str(company_id))
    return row


async def add_company(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    company_name: str,
    pi_role: str,
    funding_usd: int | None,
    funding_as_of: date | None,
    source_url: str,
    created_by_user_id: uuid.UUID | None,
) -> PiCompany:
    """Add a manual entry, confirmed at creation (O9) and so reviewed by its creator at
    that moment. A name whose normalized form the PI already has, in any status, is
    refused with a message naming the existing row. Commits, then re-exports."""
    name, normalized = _clean_name(company_name)
    role = _check_role(pi_role)
    url = _check_url(source_url)
    _check_funding(funding_usd, funding_as_of)
    existing = (
        await db.execute(
            select(PiCompany).where(
                PiCompany.user_id == user_id, PiCompany.normalized_name == normalized
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise CompanyValidationError(_duplicate_message(existing))
    now = datetime.now(UTC)
    row = PiCompany(
        user_id=user_id, company_name=name, normalized_name=normalized, pi_role=role,
        funding_usd=funding_usd, funding_as_of=funding_as_of, source_url=url,
        funding_source_url=None, status="confirmed", origin="manual", evidence=None,
        created_by_user_id=created_by_user_id, created_at=now,
        reviewed_by_user_id=created_by_user_id, reviewed_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        # A concurrent add of the same name won uq_pi_companies_user_normalized_name.
        raise CompanyValidationError(f"{name} was just added for this PI by someone else.") from exc
    await db.commit()
    await export_companies_file(db, user_id)
    return row


async def delete_company(db: AsyncSession, *, user_id: uuid.UUID, company_id: uuid.UUID) -> None:
    """Delete one confirmed row. A suggestion is rejected instead, and a rejected row is
    kept, so discovery never offers either name again; both are refused here. Commits,
    then re-exports (the last confirmed row's delete removes the file)."""
    row = await _load_row(db, user_id, company_id)
    if row.status != "confirmed":
        raise CompanyValidationError(
            "Only confirmed entries can be deleted; reject a suggestion instead."
        )
    await db.delete(row)
    await db.commit()
    await export_companies_file(db, user_id)


async def confirm_company(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    company_id: uuid.UUID,
    reviewer_id: uuid.UUID | None,
    pi_role: str | None = None,
    funding_usd: int | None = None,
    funding_as_of: date | None = None,
    clear_funding: bool = False,
) -> PiCompany:
    """Confirm a ``suggested`` row, correcting it first where asked. ``None`` keeps the
    stored role, figure or date. A figure or date that differs from the stored one is the
    manager's own, so the row stops claiming the Form D floor: ``funding_source_url`` is
    cleared, and the filings stay in ``evidence``. ``clear_funding`` drops the figure, its
    date and its source link whatever ``funding_usd`` and ``funding_as_of`` say (the form
    posts them prefilled), for a figure the manager judges wrong. Recorded as reviewed by
    ``reviewer_id`` now. Commits, then re-exports."""
    row = await _load_row(db, user_id, company_id)
    if row.status != "suggested":
        raise CompanyValidationError(
            f"{row.company_name} is not waiting for review (it is {row.status})."
        )
    role = row.pi_role if pi_role is None else _check_role(pi_role)
    if clear_funding:
        new_usd, new_as_of = None, None
        row.funding_source_url = None
    else:
        new_usd = row.funding_usd if funding_usd is None else funding_usd
        new_as_of = row.funding_as_of if funding_as_of is None else funding_as_of
    if not clear_funding and (new_usd, new_as_of) != (row.funding_usd, row.funding_as_of):
        _check_funding(new_usd, new_as_of)
        row.funding_source_url = None
    row.pi_role = role
    row.funding_usd, row.funding_as_of = new_usd, new_as_of
    row.status = "confirmed"
    row.reviewed_by_user_id = reviewer_id
    row.reviewed_at = datetime.now(UTC)
    await db.commit()
    await export_companies_file(db, user_id)
    return row


async def reject_company(
    db: AsyncSession, *, user_id: uuid.UUID, company_id: uuid.UUID, reviewer_id: uuid.UUID | None
) -> PiCompany:
    """Mark a ``suggested`` row rejected. It stays in the table, unlisted, so discovery
    skips its normalized name for good (§7.5). Commits, then re-exports."""
    row = await _load_row(db, user_id, company_id)
    if row.status != "suggested":
        raise CompanyValidationError(
            f"{row.company_name} is not waiting for review (it is {row.status})."
        )
    row.status = "rejected"
    row.reviewed_by_user_id = reviewer_id
    row.reviewed_at = datetime.now(UTC)
    await db.commit()
    await export_companies_file(db, user_id)
    return row


def _one_line(text: str) -> str:
    """``text`` with every run of whitespace, line breaks included, folded to one space."""
    return " ".join((text or "").split())


def _as_of(row: PiCompany) -> date:
    stamp = row.reviewed_at or row.created_at
    return stamp.astimezone(UTC).date()


def render_companies_markdown(rows: Sequence[PiCompany]) -> str:
    """The hub's file for one PI's confirmed rows (non-empty; PiCompany objects or the
    export's column rows, read by attribute), in the order given.

    It carries no fence of its own: ``retrieve_profile`` wraps the whole text in
    ``staff_company_record`` tags (§7.3). Every value is folded onto one line, so a name or
    link cannot start a heading or a second entry. The heading's date is the latest review
    date (creation date where a row has none), the rule the assessment page's label uses
    (§7.4).
    """
    as_of = max(_as_of(row) for row in rows)
    lines = [
        f"## Companies (staff-confirmed, as of {as_of.isoformat()})",
        "",
        "Company ties Blackbird staff confirmed from public sources. A funding figure is as "
        "recorded; one from SEC Form D filings is a floor, not the total raised.",
        "",
    ]
    for row in rows:
        role = PI_COMPANY_ROLE_LABELS.get(row.pi_role, row.pi_role).lower()
        lines.append(f"- {_one_line(row.company_name)}: {role}")
        funding = format_funding(row.funding_usd, row.funding_as_of, row.funding_source_url)
        if funding:
            lines.append(f"  - Funding: {funding}")
        lines.append(f"  - Source: {_one_line(row.source_url)}")
        if row.funding_source_url:
            lines.append(f"  - Funding source: {_one_line(row.funding_source_url)}")
    return "\n".join(lines) + "\n"


async def _export_rows(db: AsyncSession, user_id: uuid.UUID) -> list:
    """The confirmed rows as plain column tuples (attribute access by column name), in the
    file's order. Not ORM objects: the identity map would hand a second read the values
    of the first, and the export compares the two to catch a concurrent write."""
    rows = await db.execute(
        select(
            PiCompany.id, PiCompany.company_name, PiCompany.pi_role, PiCompany.funding_usd,
            PiCompany.funding_as_of, PiCompany.funding_source_url, PiCompany.source_url,
            PiCompany.reviewed_at, PiCompany.created_at,
        )
        .where(PiCompany.user_id == user_id, PiCompany.status == "confirmed")
        .order_by(PiCompany.normalized_name, PiCompany.id)
    )
    return list(rows.all())


#: How many times one export writes the file before it stops chasing concurrent writers.
_EXPORT_PASSES = 3


async def export_companies_file(db: AsyncSession, user_id: uuid.UUID) -> Path | None:
    """Write ``COMPANIES_DIR/<agent_id>.md`` from the PI's confirmed rows (atomic
    replace), or remove it when there are none (§7.2).

    Two writers can commit in one order and write the file in the other, leaving it one
    write behind. So after each write the rows are read again, and a changed list is
    written again, up to ``_EXPORT_PASSES`` writes; one still changing after that is
    logged and left to the next write.

    Returns the path written; None when nothing was written: the PI has no AgentRegistry
    row (a no-op; the profile publish writes the file once the agent exists), the agent
    id fails the file-name check, there is no confirmed row, the write failed, or the
    rows kept changing. A filesystem error is logged, never raised: the change it
    follows is already committed. Reads only; never commits (the profile publish calls it inside the
    pipeline's transaction).
    """
    agent_id = (
        await db.execute(select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user_id))
    ).scalar_one_or_none()
    if not agent_id:
        return None
    if not _SAFE_AGENT_ID.fullmatch(agent_id):
        logger.error("Companies export skipped for user %s: unsafe agent_id %r", user_id, agent_id)
        return None
    path = COMPANIES_DIR / f"{agent_id}.md"
    rows = await _export_rows(db, user_id)
    for _ in range(_EXPORT_PASSES):
        try:
            if rows:
                COMPANIES_DIR.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, render_companies_markdown(rows))
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Companies export for agent %s failed: %s", agent_id, exc)
            return None
        latest = await _export_rows(db, user_id)
        if latest == rows:
            break
        rows = latest
    else:
        logger.warning(
            "Companies export for agent %s: the confirmed rows still changed after %d "
            "writes; the next write repairs the file", agent_id, _EXPORT_PASSES,
        )
        return None
    return path if rows else None


async def move_companies_file(
    db: AsyncSession, *, user_id: uuid.UUID, old_agent_id: str
) -> Path | None:
    """After a committed agent rename: remove ``COMPANIES_DIR/<old_agent_id>.md`` and
    export under the PI's current agent id. Best effort, like the export: a filesystem
    error is logged, never raised, and an unsafe old id is left alone."""
    if _SAFE_AGENT_ID.fullmatch(old_agent_id or ""):
        try:
            (COMPANIES_DIR / f"{old_agent_id}.md").unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Companies file of renamed agent %s not removed: %s", old_agent_id, exc)
    return await export_companies_file(db, user_id)
