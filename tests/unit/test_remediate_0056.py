import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts/migrate/remediate_0056.py"
_spec = importlib.util.spec_from_file_location("remediate_0056", _PATH)
r = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = r
_spec.loader.exec_module(r)


def test_jobs_keep_the_oldest_and_supersede_the_rest():
    rows = [("u1", "generate_profile", ["j-old", "j-mid", "j-new"])]
    assert r.plan_job_remediation(rows) == [("j-mid", "j-old"), ("j-new", "j-old")]


def test_provisions_keep_the_newest():
    rows = [("agent-1", ["p-newest", "p-older", "p-oldest"])]
    assert r.plan_provision_remediation(rows) == ["p-older", "p-oldest"]


def test_last_error_text():
    assert r.superseded_note("j-old") == "duplicate — superseded by j-old"


def test_a_live_worker_leaves_processing_rows_alone():
    plan = [("j-mid", "j-old"), ("j-new", "j-old")]
    statuses = {"j-mid": "processing", "j-new": "pending"}
    assert r.split_for_live_worker(plan, statuses, True) == ([("j-new", "j-old")], [("j-mid", "j-old")])
    assert r.split_for_live_worker(plan, statuses, False) == (plan, [])

