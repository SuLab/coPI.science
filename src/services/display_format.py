"""Human-facing number and time formatting for the admin pages.

Every rule the /admin/simulation charts and tables apply to a figure lives
here. Pure functions, no I/O, no locale dependence (month names are a
tuple, not strftime('%b')). All times render in UTC — the simulation, the DB
and the Slack markers all speak UTC; a mixed-zone page is worse than a single
labelled one.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def money(value: Decimal | float | int, *, floor: bool = False) -> str:
    text = f"${Decimal(str(value)):,.2f}"
    return f"≥ {text}" if floor else text


def count(value: int) -> str:
    return f"{int(value):,}"


def compact(value: int | float) -> str:
    v = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= limit:
            return f"{v / limit:.1f}{suffix}"
    return f"{v:.0f}"


def duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600:02d}h"


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def hour_label(dt: datetime) -> str:
    u = _utc(dt)
    return f"{_MONTHS[u.month - 1]} {u.day} {u.hour:02d}:{u.minute:02d}"


def timestamp(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return _utc(dt).strftime("%Y-%m-%d %H:%M UTC")


def epoch_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M")


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def percent(fraction: float) -> str:
    return f"{fraction * 100:.0f}%"


def whole(v: float) -> str:
    return f"{v:g}"
