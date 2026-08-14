"""Control for the live_api skip logic.

Lives here, not in conftest.py: pytest does not collect tests from conftest, so the
first version of this control was never run — which is precisely the silent-disable
failure it exists to detect.
"""

import os

import pytest


@pytest.mark.live_api
def test_live_api_marker_actually_runs_when_configured():
    assert os.environ.get("LIVE_API_TESTS") == "1"


@pytest.mark.live_api
def test_api_budget_enforces_a_ceiling(api_budget):
    """The budget must actually count. A no-op fixture would let a looping test hammer
    a provider until the IP is throttled for everyone."""
    before = sum(api_budget.counts.values())
    api_budget.wait("orcid")
    assert sum(api_budget.counts.values()) == before + 1
