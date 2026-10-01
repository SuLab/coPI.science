import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.services.runs import latest_run_id, runs_ordered
from tests import factories

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


async def test_order_breaks_ties_by_id(db_session):
    t = datetime(2100, 1, 1, tzinfo=timezone.utc)  # later than any other row in the DB
    a = await factories.make_simulation_run(db_session, id=uuid.UUID(int=1), started_at=t)
    b = await factories.make_simulation_run(db_session, id=uuid.UUID(int=2), started_at=t)
    runs = await runs_ordered(db_session)
    assert [r.id for r in runs[:2]] == [b.id, a.id]
    assert await latest_run_id(db_session) == b.id


def test_no_inline_run_ordering_left():
    offenders = []
    for rel in ("src/services/directory.py", "src/services/assessment_chat.py",
                "src/services/simulation_view.py", "src/routers/admin/simulation.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        if "SimulationRun.started_at.desc()" in text:
            offenders.append(rel)
    assert offenders == []
