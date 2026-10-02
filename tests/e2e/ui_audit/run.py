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
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from tests.e2e.ui_audit.env import REPO_ROOT, harness_env, isolated_workdir, new_secret
from tests.e2e.ui_audit.harness import AXE_PATH, AXE_SHA256, AXE_URL, Harness
from tests.e2e.ui_audit.postgres import ThrowawayPostgres, free_port


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


def _wait_http(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
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
                                env=self.env(), check=True, capture_output=True, text=True)
        self.ids = json.loads(seeded.stdout.strip().splitlines()[-1])
        self.server = subprocess.Popen(
            [py, "-m", "tests.e2e.ui_audit.serve", "--port", str(self.port)],
            cwd=self.work, env=self.env())
        _wait_http(self.base_url + "/api/health")

    def down(self) -> None:
        if self.server:
            self.server.terminate()
            self.server.wait(timeout=20)
        self.pg.stop()


async def _run(stack: Stack, args) -> dict:
    os.chdir(stack.work)  # this process too: src.config must never see the repo's .env
    os.environ.update(stack.env())
    from playwright.async_api import async_playwright

    from src.main import SESSION_COOKIE

    report: dict = {"phase": args.phase, "browser": args.browser, "base_url": stack.base_url}
    async with async_playwright() as p:
        launcher = getattr(p, args.browser)
        browser = await launcher.launch(executable_path=args.executable or None)
        axe = load_axe()
        h = Harness(base_url=stack.base_url, ids=stack.ids, browser=browser,
                    cookie_name=SESSION_COOKIE, secret_key=stack.secret, axe_source=axe,
                    env=stack.env())
        if args.command in ("crawl", "all"):
            from tests.e2e.ui_audit.crawl import crawl

            report["crawl"] = await crawl(h)
        if args.command in ("journeys", "all"):
            module = importlib.import_module(f"tests.e2e.ui_audit.journeys_phase{args.phase}")
            report["journeys"] = {}
            for journey in module.JOURNEYS:
                try:
                    report["journeys"][journey.__name__] = await journey(h)
                except Exception as exc:  # noqa: BLE001 - a crash is a failed journey
                    report["journeys"][journey.__name__] = {"ok": False, "error": str(exc)}
        await browser.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.ui_audit.run")
    parser.add_argument("command", choices=("serve", "crawl", "journeys", "all"))
    parser.add_argument("--phase", type=int, default=0)
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="chromium")
    parser.add_argument("--executable", default="")
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir())
                                             / f"uiaudit-{int(time.time())}.json"))
    args = parser.parse_args()
    os.chdir(REPO_ROOT)
    stack = Stack()
    try:
        stack.up()
        if args.command == "serve":
            print(json.dumps({"base_url": stack.base_url, "ids": stack.ids,
                              "secret_key": stack.secret}), flush=True)
            stack.server.wait()
            return
        report = asyncio.run(_run(stack, args))
    finally:
        stack.down()
    Path(args.out).write_text(json.dumps(report, indent=1))
    failed = report.get("crawl", {}).get("violations", []) + [
        name for name, r in report.get("journeys", {}).items() if not r.get("ok")]
    print(json.dumps({"report": args.out, "failed": failed}, indent=1))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
