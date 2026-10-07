import asyncio
import json

import pytest

from tests.e2e.ui_audit import run as browser_run
from tests.e2e.ui_audit.harness import expected_http_error
from tests.e2e.ui_audit.run import _phase5_regressions


def _report(*, violations=(), overflow=(), axe=None):
    return {
        "crawl": {"violations": list(violations), "overflow_375": list(overflow), "axe": axe or {}}
    }


def test_phase5_baseline_rejects_added_axe_overflow_and_network(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(_report()))

    assert not _phase5_regressions(_report(axe={"color-contrast": 1}), baseline)["ok"]
    assert not _phase5_regressions(
        _report(overflow=[["reviewer", "/workspace/assessments", 8]]), baseline
    )["ok"]
    network = "reviewer 1280 /workspace/assessments: unexpected network 404 GET http://test/missing"
    assert not _phase5_regressions(_report(violations=[network]), baseline)["ok"]


def test_phase5_baseline_normalizes_legacy_paths_but_never_waives_network(tmp_path):
    baseline = tmp_path / "baseline.json"
    legacy = "admin 1280 /admin/assessments: HTTP 500"
    baseline.write_text(json.dumps(_report(violations=[legacy])))
    assert _phase5_regressions(
        _report(violations=["admin 1280 /workspace/assessments: HTTP 500"]), baseline
    )["ok"]

    network = "admin 1280 /workspace/assessments: request failed GET http://test/static"
    baseline.write_text(json.dumps(_report(violations=[network])))
    assert not _phase5_regressions(_report(violations=[network]), baseline)["ok"]


def test_intentional_network_negatives_do_not_waive_positive_or_asset_errors():
    args = ('admin', 'http://test', {'bad_uuid': 'missing'})
    assert expected_http_error('http://test/workspace/assessments/missing', 404, 'GET', *args)
    assert not expected_http_error('http://test/workspace/assessments/real', 404, 'GET', *args)
    assert not expected_http_error('http://test/static/missing.js', 404, 'GET', *args)
    assert not expected_http_error('http://test/workspace/pis', 403, 'GET', *args)
    assert not expected_http_error('http://test/workspace/pis', 500, 'GET', *args)


def test_axe_defects_and_tool_failure_cannot_hide_under_aggregate_baseline(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(_report(axe={"scrollable-region-focusable": 3})))
    assert not _phase5_regressions(
        _report(axe={"scrollable-region-focusable": 1}), baseline
    )["ok"]
    assert not _phase5_regressions(_report(axe={"axe-error": 0}), baseline)["ok"]


@pytest.mark.parametrize("failure", ["orphan", "shutdown", None])
def test_browser_gate_rejects_async_errors_including_shutdown(monkeypatch, failure):
    logged = []
    monkeypatch.setattr(asyncio.BaseEventLoop, "default_exception_handler",
                        lambda _loop, context: logged.append(context["message"]))

    async def fake_run(_stack, _args):
        async def fail():
            if failure == "shutdown":
                try:
                    await asyncio.Event().wait()
                finally:
                    raise RuntimeError("SHUTDOWN_NEGATIVE_CONTROL")
            raise RuntimeError("ORPHAN_NEGATIVE_CONTROL")

        if failure:
            asyncio.create_task(fail())
            await asyncio.sleep(0)
        return {"journeys": {"healthy": {"ok": True}}}

    monkeypatch.setattr(browser_run, "_run", fake_run)
    report = browser_run._run_with_exception_capture(None, None)
    assert bool(report["asyncio_errors"]) == bool(failure)
    assert bool(logged) == bool(failure)
    assert browser_run._failures(report) == (["asyncio_errors"] if failure else [])
