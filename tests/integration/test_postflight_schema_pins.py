"""R1.18: every object postflight pins reads back, as pinned, on a database the real
alembic chain built (the 2026-10-15 publication-handling design, DM-3/SCR-14)."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_MIGRATE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "migrate"


def _load(module_name: str, filename: str):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _MIGRATE_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("copi_migrate_preflight", "preflight.py")
po = _load("copi_migrate_postflight", "postflight.py")


@pytest.mark.parametrize("check", [
    "check_expected_tables", "check_expected_columns",
    "check_expected_indexes", "check_expected_constraints",
])
async def test_every_pinned_object_reads_back_on_the_migrated_test_database(db_session, check):
    title, status, detail, *_ = await getattr(po, check)(db_session)
    assert status == po.PASS, f"{title}: {detail}"
