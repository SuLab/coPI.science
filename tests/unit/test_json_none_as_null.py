"""Drift alarm: every nullable JSON/JSONB column must set ``none_as_null=True``.

SQLAlchemy's ``JSON`` type defaults ``none_as_null=False``, which persists Python
``None`` as the JSON scalar ``null`` rather than as SQL NULL. That gives "absent"
TWO physical encodings in one column, and the ORM cannot tell them apart — both
decode back to ``None`` — so the damage is invisible from Python and lands
entirely on SQL-level readers, where ``WHERE col IS NULL`` silently misses every
row that took the other encoding.

That has now happened twice on the same mechanism:

  * ``opportunity_assessments.missing_domains`` documents three states and spells
    the middle one NULL. 15 rows (every row written since 2026-08-19) held the
    JSONB ``null`` while 18 older rows held a true SQL NULL, so the obvious query
    for "verified panels" counted every recent verified row as unverified —
    inverting the single number the panel instrumentation exists to produce.
    Migration 0031 normalized those 15 rows and set ``none_as_null=True``.
  * ``assessment_drops.raw_verdict``, added by 0035 six weeks later, reintroduced
    it verbatim: 15 SQL NULL and 2 JSONB ``null`` in production, so "which
    refusals kept their verdict?" returned exactly the 2 rows that kept nothing.

0031 fixed one column and pinned it with a test about that column. The absence of
THIS test — a walk over the metadata rather than a per-column assertion — is why
0035 could add a new one and why 11 of the 13 nullable JSON columns were still
double-encoded a month later. Migration 0036 normalized the data on all eleven;
this test is what stops a twelfth.

Scoped deliberately to NULLABLE columns. On a NOT NULL column there is no SQL
NULL to collapse onto, so ``none_as_null=True`` would convert a ``None`` write
from "stores the JSON scalar null" into an IntegrityError — a behaviour change
with its own argument, not this one.
"""

import sqlalchemy as sa

import src.models  # noqa: F401  — populates Base.metadata with every table
from src.database import Base


def _nullable_json_columns() -> list[tuple[str, str, sa.Column]]:
    """Every nullable JSON/JSONB column in the mapped schema.

    ``sqlalchemy.types.JSON`` is the base class of both ``postgresql.JSON`` and
    ``postgresql.JSONB``, and ``none_as_null`` lives on it, so one isinstance
    check covers all three spellings this codebase uses.
    """
    found = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        for col in table.columns:
            if isinstance(col.type, sa.JSON) and col.nullable:
                found.append((table_name, col.name, col))
    return found


def test_every_nullable_json_column_uses_none_as_null():
    offenders = [
        f"{table}.{name}"
        for table, name, col in _nullable_json_columns()
        if col.type.none_as_null is not True
    ]
    assert not offenders, (
        "these nullable JSON/JSONB columns store Python None as the JSON scalar "
        "`null` instead of SQL NULL, giving 'absent' two encodings that no SQL "
        "reader can reconcile: " + ", ".join(offenders) + ". Pass "
        "`none_as_null=True` to the column's type, and add a data-only migration "
        "in the shape of 0031/0036 to normalize any rows already written."
    )


def test_the_walk_actually_found_json_columns():
    """Guard against the check above quietly becoming a no-op.

    If a refactor renames the type, moves the flag, or breaks the model imports
    that populate ``Base.metadata``, the offender list goes empty and the test
    above passes for the wrong reason. 13 nullable JSON/JSONB columns existed
    when 0036 landed; the floor is deliberately loose (a column may legitimately
    be dropped) but non-zero.
    """
    assert len(_nullable_json_columns()) >= 10
