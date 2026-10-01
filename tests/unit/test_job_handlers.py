import inspect

import pytest

from src.models import Job
from src.worker import main as worker


def test_registry_covers_every_live_job_type():
    assert set(worker.JOB_HANDLERS) == set(Job.type.type.enums) - {"monthly_refresh"}


def test_monthly_refresh_stays_in_the_enum():
    assert "monthly_refresh" in Job.type.type.enums


@pytest.mark.asyncio
async def test_monthly_refresh_is_retired():
    with pytest.raises(worker.RetiredJobType, match="retired job type: monthly_refresh"):
        await worker.execute_monthly_refresh(None, None)


def test_dispatch_has_no_if_chain():
    src = inspect.getsource(worker.process_job)
    assert 'job.type == "generate_profile"' not in src
    assert "JOB_HANDLERS" in src
