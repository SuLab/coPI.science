"""Lab identity (spec 2026-10-30 §5.2 "Lab identity"; X8, D49, F5).

A lab key names the senior person behind a record. It takes the first that exists of
``orcid:<id>``, ``oa:<OpenAlex author id>``, ``nih:<RePORTER PI profile id>`` and
``name:<surname>|<first initial>``. Keys are never used for filtering.

``landscape_labs`` holds one row per key seen on a qualifying record. A row whose
``alias_of`` is NULL is canonical; an alias row points at its canonical key, and the
canonical row lists its aliases. Unification only ever moves a key under a higher-ranked
one, or joins a name key to the one lab that carries that name; it never splits labs.
Documents store the canonical key at the time they were written; later unifications are
read through ``alias_of`` (D64).

Nothing here commits; callers own the transaction.
"""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping.sources import DocumentRecord
from src.models.landscape import LandscapeLab

#: Key prefixes, highest-ranked first (§5.2 table).
_RANK = {"orcid": 0, "oa": 1, "nih": 2, "name": 3}
#: Marks a recorded name collision in a ``name:`` lab's ``aliases`` list: the entry after
#: the prefix is one of the labs the name matched (§5.2; counted by the metrics).
COLLISION_PREFIX = "collision:"
#: How far ``alias_of`` is followed; chains are flattened on every unification, so a
#: longer chain means a cycle written by hand.
_MAX_HOPS = 10
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "md", "phd", "mph", "dr"})
_TOKEN = re.compile(r"[a-z][a-z'\-]*")


def _fold(text: str) -> str:
    """Lowercase ASCII letters of `text`, accents removed."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def name_key_of(name: str | None) -> str | None:
    """``name:<surname>|<first initial>`` for a personal name, or None when the name has no
    surname and given name to take them from. "Surname, Given" and "Given Middle Surname"
    are both read; trailing suffixes (Jr, III, MD, PhD) are dropped."""
    if not name or not name.strip():
        return None
    folded = _fold(name)
    if "," in folded:
        surname_part, given_part = folded.split(",", 1)
        surnames = _TOKEN.findall(surname_part)
        givens = [t for t in _TOKEN.findall(given_part) if t.strip("'-") not in _SUFFIXES]
        surname = surnames[-1] if surnames else None
        given = givens[0] if givens else None
    else:
        tokens = [t for t in _TOKEN.findall(folded) if t.strip("'-") not in _SUFFIXES]
        if len(tokens) < 2:
            return None
        surname, given = tokens[-1], tokens[0]
    if not surname or not given:
        return None
    surname = re.sub(r"[^a-z\-]", "", surname).strip("-")
    return f"name:{surname}|{given[0]}" if surname else None


def _id_keys(person: dict) -> list[str]:
    """The id-based keys of one senior name, highest-ranked first."""
    keys = []
    for field, prefix in (("orcid", "orcid"), ("openalex_author_id", "oa"),
                          ("nih_pi_profile_id", "nih")):
        value = person.get(field)
        if value is not None and str(value).strip():
            keys.append(f"{prefix}:{str(value).strip()}")
    return keys


def person_keys(person: dict) -> list[str]:
    """Every key one senior name carries, highest-ranked first: its id keys, then its name
    key."""
    keys = _id_keys(person)
    if nk := name_key_of(person.get("name")):
        keys.append(nk)
    return keys


def lab_key(rec: DocumentRecord) -> str | None:
    """The record's own lab key (§5.2 table): the highest-ranked key of its most senior
    name, before any unification. None when the record names no one usable."""
    for person in rec.senior_names or []:
        keys = person_keys(person)
        if keys:
            return keys[0]
    return None


def _rank(key: str) -> int:
    return _RANK.get(key.split(":", 1)[0], len(_RANK))


def _columns_for(key: str) -> dict:
    prefix, _, value = key.partition(":")
    column = {
        "orcid": "orcid", "oa": "openalex_author_id", "nih": "reporter_profile_id",
    }.get(prefix)
    return {column: value[:60]} if column else {}


async def _ensure(db: AsyncSession, key: str, display_name: str | None = None) -> LandscapeLab:
    """The lab row for `key`, inserted when missing; fills a missing display name."""
    await db.execute(
        pg_insert(LandscapeLab)
        .values(lab_key=key, display_name=(display_name or None) and display_name[:255],
                **_columns_for(key))
        .on_conflict_do_nothing(index_elements=["lab_key"])
    )
    lab = (await db.execute(
        select(LandscapeLab).where(LandscapeLab.lab_key == key)
        .execution_options(populate_existing=True)
    )).scalar_one()
    if display_name and not lab.display_name:
        lab.display_name = display_name[:255]
    return lab


async def _canonical(db: AsyncSession, key: str) -> str:
    """The canonical key `key` resolves to (itself when it has no row or no ``alias_of``)."""
    current = key
    for _ in range(_MAX_HOPS):
        target = (await db.execute(
            select(LandscapeLab.alias_of).where(LandscapeLab.lab_key == current)
        )).scalar_one_or_none()
        if not target:
            return current
        current = target
    return current


def _with(items: list | None, *new: str) -> list:
    out = list(items or [])
    for item in new:
        if item not in out:
            out.append(item)
    return out


async def _alias(db: AsyncSession, alias_key: str, target_key: str) -> None:
    """Put the canonical lab `alias_key` (and everything aliased to it) under `target_key`,
    itself canonical. Chains stay one hop long."""
    if alias_key == target_key:
        return
    moved = (await db.execute(
        select(LandscapeLab.lab_key).where(LandscapeLab.alias_of == alias_key)
    )).scalars().all()
    await db.execute(
        update(LandscapeLab).where(LandscapeLab.alias_of == alias_key)
        .values(alias_of=target_key, updated_at=func.now())
    )
    alias_row = await _ensure(db, alias_key)
    target_row = await _ensure(db, target_key)
    alias_row.alias_of = target_key
    target_row.aliases = _with(
        target_row.aliases, alias_key, *moved,
        *[a for a in alias_row.aliases or [] if not a.startswith(COLLISION_PREFIX)],
    )
    if not target_row.display_name and alias_row.display_name:
        target_row.display_name = alias_row.display_name
    await db.flush()


async def _unify(db: AsyncSession, a: str, b: str) -> str:
    """Join the labs of keys `a` and `b` under the higher-ranked canonical key."""
    ca, cb = await _canonical(db, a), await _canonical(db, b)
    if ca == cb:
        return ca
    keep, drop = (ca, cb) if _rank(ca) <= _rank(cb) else (cb, ca)
    await _alias(db, drop, keep)
    return keep


async def resolve_lab_aliases(
    db: AsyncSession, keys: list[str], *, display_name: str | None = None,
) -> str:
    """One record carrying several keys for one person (§5.2 "One record, two keys"): make
    a lab row for each, put every key under the highest-ranked one's canonical key, and
    return that canonical key. `display_name` fills a lab's missing display name.

    With several keys, pass only those that identify the person (``orcid:``, ``oa:``,
    ``nih:``): a ``name:`` key is shared by namesakes and is joined only through
    `unify_name_key` or `unify_grant_pi`. A single key, a name key included, just ensures
    its row and returns its canonical key. RAISES ValueError on no keys."""
    if not keys:
        raise ValueError("resolve_lab_aliases needs at least one key")
    ordered = sorted(dict.fromkeys(keys), key=_rank)
    for key in ordered:
        await _ensure(db, key, display_name)
    canonical = await _canonical(db, ordered[0])
    for key in ordered[1:]:
        canonical = await _unify(db, canonical, key)
    await db.flush()
    return canonical


async def unify_grant_pi(
    db: AsyncSession, *, grant_key: str, paper_key: str, grant_name: str, paper_name: str,
) -> bool:
    """Join a grant PI's lab with a paper's senior lab when RePORTER links that grant to
    that paper and the two names agree on surname and first initial (§5.2 "Cross-record
    links"). Returns True when the two keys now share a canonical lab."""
    grant_nk, paper_nk = name_key_of(grant_name), name_key_of(paper_name)
    if grant_nk is None or grant_nk != paper_nk:
        return False
    await _ensure(db, grant_key, grant_name)
    await _ensure(db, paper_key, paper_name)
    await _unify(db, grant_key, paper_key)
    await db.flush()
    return True


async def _labs_named(db: AsyncSession, nk: str) -> list[str]:
    """Canonical labs with an id-based key whose display name has name key `nk`."""
    # Display names keep their accents, so the comparison is made on folded names here.
    rows = (await db.execute(
        select(LandscapeLab.lab_key, LandscapeLab.display_name).where(
            LandscapeLab.alias_of.is_(None),
            LandscapeLab.display_name.is_not(None),
            ~LandscapeLab.lab_key.startswith("name:"),
        )
    )).all()
    return sorted(key for key, display in rows if name_key_of(display) == nk)


async def unify_name_key(db: AsyncSession, name_key: str) -> str:
    """The canonical key for a patent inventor's or JHTV contact's name key (§5.2).

    The name joins an existing lab only when exactly one lab with an id-based key carries
    that name. With none it stays its own ``name:`` lab; with two or more it also stays
    its own lab, and the collision is recorded on that lab's row as
    ``COLLISION_PREFIX + <matching key>`` entries in ``aliases``. A name key already joined
    to a lab keeps that lab."""
    existing = await _canonical(db, name_key)
    if existing != name_key:
        return existing
    matches = await _labs_named(db, name_key)
    if len(matches) == 1:
        await _ensure(db, name_key)
        await _alias(db, name_key, matches[0])
        return matches[0]
    lab = await _ensure(db, name_key)
    if len(matches) > 1:
        lab.aliases = _with(lab.aliases, *(COLLISION_PREFIX + m for m in matches))
    await db.flush()
    return name_key
