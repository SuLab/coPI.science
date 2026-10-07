"""CLI for the browser harness.

    python -m tests.e2e.ui_audit.run serve
    python -m tests.e2e.ui_audit.run all --phase 0 [--browser firefox] [--executable PATH]

``all`` starts a throwaway Postgres, migrates, seeds, serves, runs the crawl and the
phase's journeys, writes a JSON report and tears everything down. Exit status 1 when
any crawl invariant or journey fails.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from tests.e2e.ui_audit.env import REPO_ROOT, harness_env, isolated_workdir, new_secret
from tests.e2e.ui_audit.harness import AXE_PATH, AXE_SHA256, AXE_URL, Harness
from tests.e2e.ui_audit.postgres import ThrowawayPostgres, free_port


def _phase5_regressions(report: dict, baseline_path: Path) -> dict:
    """Compare observable UI failures; a baseline is not an allowlist."""
    if not baseline_path.exists():
        return {"ok": False, "error": f"missing baseline: {baseline_path}", "new": ["baseline"]}
    baseline = json.loads(baseline_path.read_text())
    current_crawl = report.get("crawl", {})
    baseline_crawl = baseline.get("crawl", {})

    def canonical(value: object) -> str:
        return (str(value).replace("/manager/", "/workspace/")
                .replace("/admin/assessments", "/workspace/assessments")
                .replace("/admin/activity", "/workspace/activity")
                .replace("/admin/discussions", "/workspace/discussions"))

    current = {
        "violations": {canonical(row) for row in current_crawl.get("violations", [])},
        "overflow": {tuple(canonical(item) for item in row)
                     for row in current_crawl.get("overflow_375", [])},
        "axe": current_crawl.get("axe", {}),
    }
    previous = {
        "violations": {canonical(row) for row in baseline_crawl.get("violations", [])},
        "overflow": {tuple(canonical(item) for item in row)
                     for row in baseline_crawl.get("overflow_375", [])},
        "axe": baseline_crawl.get("axe", {}),
    }
    # An aggregate baseline cannot identify individual nodes/pages. Requiring
    # no WCAG findings avoids masking new defects behind removed old findings.
    # A tool failure must fail even if a legacy report recorded zero nodes.
    added_axe = {rule: count for rule, count in current["axe"].items()
                 if count > 0 or rule == "axe-error"}
    new = {
        "violations": sorted(current["violations"] - previous["violations"]),
        "overflow": sorted(current["overflow"] - previous["overflow"]),
        "axe": added_axe,
    }
    hard_network = sorted(value for value in current["violations"]
                          if "request failed" in value or "unexpected network" in value)
    return {"ok": not any(new.values()) and not hard_network,
            "new": new, "hard_network": hard_network}


def load_axe() -> str:
    """The pinned axe-core bundle, downloaded to AXE_PATH on first use."""
    import hashlib

    if not AXE_PATH.exists():
        with urllib.request.urlopen(AXE_URL, timeout=30) as r:  # noqa: S310 - pinned CDN
            AXE_PATH.write_bytes(r.read())
    data = AXE_PATH.read_bytes()
    if hashlib.sha256(data).hexdigest() != AXE_SHA256:
        AXE_PATH.unlink()
        raise RuntimeError(f"{AXE_PATH} does not match the pinned sha256; deleted it")
    return data.decode()


def _wait_http(url: str, timeout: float = 300, proc: subprocess.Popen | None = None) -> None:
    """Poll ``url`` until it answers 200. Generous: importing the app over a slow
    mount (sshfs) can take minutes. Fails fast if ``proc`` has exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"the app server exited with {proc.returncode} before {url} came up")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - localhost only
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


class Stack:
    """Postgres + migrated schema + seed + app, all isolated from .env."""

    def __init__(self) -> None:
        self.pg = ThrowawayPostgres()
        self.port = free_port()
        self.base_url = f"http://localhost:{self.port}"
        self.secret = new_secret()
        self.work = isolated_workdir()
        self.server: subprocess.Popen | None = None
        self.ids: dict = {}

    def env(self) -> dict[str, str]:
        return harness_env(database_url=self.pg.url, base_url=self.base_url,
                           secret_key=self.secret)

    def up(self) -> None:
        self.pg.start()
        py = sys.executable
        subprocess.run([py, "-m", "alembic", "upgrade", "head"], cwd=self.work,
                       env=self.env(), check=True, capture_output=True)
        seeded = subprocess.run([py, "-m", "tests.e2e.ui_audit.seed"], cwd=self.work,
                                env=self.env(), capture_output=True, text=True)
        if seeded.returncode:
            raise RuntimeError(f"isolated browser seed failed:\n{seeded.stderr}")
        self.ids = json.loads(seeded.stdout.strip().splitlines()[-1])
        self.server = subprocess.Popen(
            [py, "-m", "tests.e2e.ui_audit.serve", "--port", str(self.port)],
            cwd=self.work, env=self.env())
        _wait_http(self.base_url + "/api/health", proc=self.server)

    def down(self) -> None:
        try:
            if self.server and self.server.poll() is None:
                self.server.terminate()
                try:
                    self.server.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.server.kill()
        finally:
            self.pg.stop()
            shutil.rmtree(self.work, ignore_errors=True)


async def _run(stack: Stack, args) -> dict:
    os.chdir(stack.work)  # this process too: src.config must never see the repo's .env
    os.environ.update(stack.env())
    from playwright.async_api import async_playwright

    from src.config import get_settings
    from src.main import session_cookie_name

    report: dict = {"phase": args.phase, "browser": args.browser, "base_url": stack.base_url}
    async with async_playwright() as p:
        launcher = getattr(p, args.browser)
        browser = await launcher.launch(executable_path=args.executable or None)
        axe = load_axe()
        h = Harness(base_url=stack.base_url, ids=stack.ids, browser=browser,
                    cookie_name=session_cookie_name(get_settings()),
                    secret_key=stack.secret, axe_source=axe, env=stack.env())
        if args.command in ("crawl", "all"):
            from tests.e2e.ui_audit.crawl import crawl

            report["crawl"] = await crawl(h)
        await browser.close()
        if args.command in ("journeys", "all"):
            module = importlib.import_module(f"tests.e2e.ui_audit.journeys_phase{args.phase}")
            report["journeys"] = {}
            selected = [j for j in module.JOURNEYS
                        if not args.only or j.__name__ in args.only]
            for journey in selected:
                # A fresh browser per journey: one browser crash must not fail every
                # journey after it (observed 2026-10-01, headless shell SIGILL).
                h.browser = await launcher.launch(executable_path=args.executable or None)
                try:
                    report["journeys"][journey.__name__] = await journey(h)
                except Exception as exc:  # noqa: BLE001 - a crash is a failed journey
                    report["journeys"][journey.__name__] = {"ok": False, "error": str(exc)}
                finally:
                    try:
                        await h.browser.close()
                    except Exception:  # noqa: BLE001 - already gone
                        pass
        if args.phase == 4:
                audit = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "scripts/persona_file_audit.py"), "--check"],
                    cwd=Path.cwd(), env=h.env, capture_output=True, text=True,
                )
                report["persona_audit"] = {"ok": audit.returncode == 0,
                                            "output": audit.stdout + audit.stderr}
        if args.phase == 5:
            report["baseline_comparison"] = _phase5_regressions(
                report, Path(args.baseline))
    return report


def _run_with_exception_capture(stack: Stack, args) -> dict:
    """Fail the gate on orphaned tasks, including errors during loop shutdown."""
    errors = []
    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        previous_handler = loop.get_exception_handler()

        def record_error(event_loop, context):
            error = context.get("exception")
            errors.append({"message": context.get("message", "asyncio error"),
                           "exception": repr(error) if error is not None else None})
            if previous_handler is not None:
                previous_handler(event_loop, context)
            else:
                event_loop.default_exception_handler(context)

        loop.set_exception_handler(record_error)
        report = runner.run(_run(stack, args))
    report["asyncio_errors"] = errors
    return report


def _failures(report: dict) -> list:
    failed = list(report.get("crawl", {}).get("violations", [])) + [
        name for name, result in report.get("journeys", {}).items() if not result.get("ok")]
    for gate in ("persona_audit", "baseline_comparison"):
        if report.get(gate, {}).get("ok") is False:
            failed.append(gate)
    if report.get("asyncio_errors"):
        failed.append("asyncio_errors")
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.ui_audit.run")
    parser.add_argument("command", choices=("serve", "crawl", "journeys", "all"))
    parser.add_argument("--phase", type=int, default=0)
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="chromium")
    parser.add_argument("--executable", default="")
    parser.add_argument("--only", action="append", default=[],
                        help="run only this journey (repeatable), e.g. journey_raw_html_shapes")
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir())
                                             / f"uiaudit-{int(time.time())}.json"))
    parser.add_argument("--baseline", default="/tmp/workspace-ui-baseline.json",
                        help="phase-5 baseline report; new failures always fail")
    args = parser.parse_args()
    os.chdir(REPO_ROOT)
    # SIGTERM (a `timeout`, a closed terminal) must still reach the finally below,
    # so the throwaway container and the app process are always cleaned up.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    stack = Stack()
    try:
        stack.up()
        if args.command == "serve":
            print(json.dumps({"base_url": stack.base_url, "ids": stack.ids,
                              "secret_key": stack.secret}), flush=True)
            stack.server.wait()
            return
        report = _run_with_exception_capture(stack, args)
    finally:
        stack.down()
    Path(args.out).write_text(json.dumps(report, indent=1))
    failed = _failures(report)
    print(json.dumps({"report": args.out, "failed": failed}, indent=1))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
