"""DB-08: at head, the models declare every object the migrations create. alembic's
compare_metadata reports an object the DB has and the models lack as remove_*; an
object the models declare and the DB lacks as add_*. Both must be empty."""
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import src.models  # noqa: F401
from src.database import Base

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_CHECKED = {"add_index", "remove_index", "add_constraint", "remove_constraint",
            "add_column", "remove_column", "add_table", "remove_table"}

#: (op, table, column) the models cannot declare. `users.is_admin` is a legacy column the
#: migrations left in place; the model's `is_admin` is a read-only hybrid over `user_role`
#: (src/models/user.py), so a mapped Column of that name would clash with it.
_KNOWN_LEGACY = {("remove_column", "users", "is_admin")}


def _describe(op, item):
    if op in ("add_column", "remove_column"):
        return (op, item[2], item[3].name)
    return None


async def test_models_and_migrations_agree(engine):
    def _diff(sync_conn):
        return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)

    async with engine.connect() as conn:
        raw = await conn.run_sync(_diff)
    ops = []
    for entry in raw:
        for item in (entry if isinstance(entry, list) else [entry]):
            op = item[0] if isinstance(item, (tuple, list)) else str(item)
            if op in _CHECKED and _describe(op, item) not in _KNOWN_LEGACY:
                ops.append(f"{op}: {str(item)[:200]}")
    assert ops == [], "\n".join(ops)
