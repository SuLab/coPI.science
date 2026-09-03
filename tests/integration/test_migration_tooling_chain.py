import pytest
from sqlalchemy import text

from scripts.migrate import postflight as post

pytestmark = pytest.mark.integration


async def test_postflight_expected_indexes_exist_at_head(engine):
    q = text("select indexname, indexdef from pg_indexes where schemaname = 'public'")
    async with engine.connect() as conn:
        have = {r.indexname: r.indexdef for r in (await conn.execute(q)).all()}
    for name, tail in post.EXPECTED_INDEXES.items():
        assert name in have, name
        assert have[name].endswith(tail), (name, have[name])


async def test_postflight_expected_constraint_defs_at_head(engine):
    # Alias the table-name column `tbl`, not `t`: sqlalchemy.engine.Row has its own
    # legacy `.t` attribute (a `_tuple()` alias, see the 2.0 deprecation warning), which
    # shadows a same-named result column on attribute access and made `r.t` silently
    # return the whole row instead of `rel.relname`.
    q = text(
        "select con.conname n, rel.relname tbl, pg_get_constraintdef(con.oid) d "
        "from pg_constraint con join pg_class rel on rel.oid = con.conrelid "
        "join pg_namespace ns on ns.oid = con.connamespace where ns.nspname = 'public'"
    )
    async with engine.connect() as conn:
        live = {r.n: (r.tbl, r.d) for r in (await conn.execute(q)).all()}
    for name, (table, expect) in post.EXPECTED_CONSTRAINTS.items():
        assert name in live, name
        assert live[name][0] == table, (name, live[name])
        assert expect in live[name][1], (name, live[name][1])
