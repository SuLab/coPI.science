"""The landscape_* models (migration 0077): no simulation-run column (K3), and every JSONB column
stores Python None as SQL NULL, the rule tests/unit/test_json_none_as_null.py holds for nullable
columns, here for every landscape JSON column."""
import sqlalchemy as sa

import src.models  # noqa: F401  — populates Base.metadata with every table
from src.database import Base


def _landscape_tables() -> list[sa.Table]:
    return [t for name, t in sorted(Base.metadata.tables.items()) if name.startswith("landscape_")]


def test_no_simulation_run_column():
    tables = _landscape_tables()
    assert len(tables) == 42
    offenders = [t.name for t in tables if "simulation_run_id" in t.columns]
    assert offenders == []


def test_jsonb_none_as_null():
    json_columns = [
        (t.name, c.name, c)
        for t in _landscape_tables()
        for c in t.columns
        if isinstance(c.type, sa.JSON)
    ]
    # Guards against the walk becoming a no-op (the models carry dozens of JSONB columns).
    assert len(json_columns) >= 40
    offenders = [f"{t}.{n}" for t, n, c in json_columns if c.type.none_as_null is not True]
    assert offenders == []
