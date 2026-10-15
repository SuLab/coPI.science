"""Capture the Help screenshots, or check that the committed ones are current
(spec docs/specs/2026-10-14-help-and-onboarding-design.md §8.2).

    python -m tests.e2e.help_shots.capture [--only ID]... [--executable PATH]
    python -m tests.e2e.help_shots.capture --check

Capture starts a throwaway stack (Postgres, migrations, this package's seed and serve),
takes every shot in shots.SHOTS at 1280x900 as its seeded account, and writes
``help_assets/shots/<id>.png`` and ``manifest.json`` under the REPOSITORY ROOT. Nothing
there changes unless every shot succeeds and the outbound recorder saw no connection;
exit 1 otherwise. ``--only`` recaptures the named shots and keeps every other manifest
entry; a full capture rewrites the manifest and removes PNGs no shot names.

``--check`` needs neither Docker nor a browser: it rehashes every shot's sources and
exits 1 when a shot is stale or missing (not in the manifest, or no PNG).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from tests.e2e.help_shots.shots import SHOTS, SHOTS_BY_ID, Shot
from tests.e2e.ui_audit.env import REPO_ROOT
from tests.e2e.ui_audit.run import Stack, _wait_http

SHOTS_DIR = REPO_ROOT / "help_assets" / "shots"
MANIFEST = SHOTS_DIR / "manifest.json"
VIEWPORT = {"width": 1280, "height": 900}
#: Hashed into every shot: the stylesheet, the rubric (bands and labels are rendered into
#: shots) and the seed that fills the pages.
COMMON_SOURCES = ("static/css/app.css", "prompts/rubric/blackbird-rubric.toml",
                  "tests/e2e/help_shots/seed.py")


# --------------------------------------------------------------------------- hashing


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def template_closure(paths: tuple[str, ...]) -> list[str]:
    """``paths`` (repo-relative, under templates/) plus every template they extend,
    include or import, recursively, as sorted repo-relative paths. A dynamic reference
    cannot be followed; it is reported on stderr."""
    import jinja2
    from jinja2 import meta

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(REPO_ROOT / "templates"))
    pending = [p.removeprefix("templates/") for p in paths]
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        source, _filename, _uptodate = env.loader.get_source(env, name)
        for ref in meta.find_referenced_templates(env.parse(source)):
            if ref is None:
                print(f"warning: {name} has a dynamic template reference; not hashed",
                      file=sys.stderr)
            else:
                pending.append(ref)
    return sorted("templates/" + name for name in seen)


def shot_entry_hash(shot: Shot) -> str:
    """The shot's own definition: its static fields as sorted JSON plus the source of its
    ``path`` and ``prepare`` callables. Never ``repr(shot)``: a callable's repr holds its
    memory address, which differs on every run."""
    static = json.dumps({"id": shot.id, "audience": shot.audience, "account": shot.account,
                         "clip": shot.clip, "alt": shot.alt, "sources": list(shot.sources)},
                        sort_keys=True)
    return _sha256((static + inspect.getsource(shot.path)
                    + inspect.getsource(shot.prepare)).encode())


def source_hashes(shot: Shot) -> dict[str, str]:
    """The manifest's ``sources`` map for ``shot``: path -> sha256."""
    hashes = {rel: _sha256((REPO_ROOT / rel).read_bytes())
              for rel in (*template_closure(shot.sources), *COMMON_SOURCES)}
    hashes[f"shots.py#{shot.id}"] = shot_entry_hash(shot)
    return dict(sorted(hashes.items()))


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's IHDR chunk."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}


def check() -> int:
    """Print stale and missing shots; 1 when there is any."""
    manifest = load_manifest()
    stale, missing = [], []
    for shot in SHOTS:
        entry = manifest.get(shot.id)
        if entry is None or not (SHOTS_DIR / f"{shot.id}.png").exists():
            missing.append(shot.id)
            continue
        current = source_hashes(shot)
        changed = sorted(k for k in current.keys() | entry.get("sources", {}).keys()
                         if current.get(k) != entry.get("sources", {}).get(k))
        if changed:
            stale.append(f"{shot.id}: {', '.join(changed)}")
    unknown = sorted(set(manifest) - set(SHOTS_BY_ID))
    for label, items in (("missing", missing), ("stale", stale),
                         ("in the manifest but not in shots.py", unknown)):
        for item in items:
            print(f"{label}: {item}")
    if missing or stale or unknown:
        return 1
    print(f"{len(SHOTS)} shots current")
    return 0


# --------------------------------------------------------------------------- capture


class OutboundRecorder:
    """A local listener that the serve process's proxy variables point at. It records
    every connection attempt (with its first request line, e.g. ``CONNECT host:443``)
    and closes it unanswered."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="outbound-recorder",
                                        daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, peer = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                try:
                    head = conn.recv(512)
                except OSError:
                    head = b""
            line = head.split(b"\r\n", 1)[0].decode("latin-1")[:200] or "(no request line)"
            with self._lock:
                self.hits.append(f"{peer[0]}:{peer[1]} {line}")

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._sock.close()


class ShotStack(Stack):
    """ui_audit's stack with this package's seed and serve modules. The proxy variables
    go into the serve process's environment ONLY: the capture process's own readiness
    probe and the alembic and seed subprocesses must not use them."""

    def __init__(self, proxy_url: str) -> None:
        super().__init__()
        self.proxy_url = proxy_url

    def serve_env(self) -> dict[str, str]:
        env = self.env()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[name] = env[name.lower()] = self.proxy_url
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        return env

    def up(self) -> None:
        self.pg.start()
        py = sys.executable
        subprocess.run([py, "-m", "alembic", "upgrade", "head"], cwd=self.work,
                       env=self.env(), check=True, capture_output=True)
        seeded = subprocess.run([py, "-m", "tests.e2e.help_shots.seed"], cwd=self.work,
                                env=self.env(), capture_output=True, text=True)
        if seeded.returncode:
            raise RuntimeError(f"help screenshot seed failed:\n{seeded.stderr}")
        self.ids = json.loads(seeded.stdout.strip().splitlines()[-1])
        self.server = subprocess.Popen(
            [py, "-m", "tests.e2e.help_shots.serve", "--port", str(self.port)],
            cwd=self.work, env=self.serve_env())
        _wait_http(self.base_url + "/api/health", proc=self.server)


async def _capture(stack: ShotStack, shots: list[Shot], executable: str, out: Path) -> dict:
    """Take ``shots`` into ``out``; returns {id: (width, height)}. Raises on any failed
    page load, console error, page error or unexpected network response."""
    os.chdir(stack.work)  # src.config must never see the repo's .env
    os.environ.update(stack.env())
    from playwright.async_api import async_playwright

    from src.config import get_settings
    from src.main import session_cookie_name
    from tests.e2e.ui_audit.harness import Harness

    sizes: dict[str, tuple[int, int]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable or None)
        h = Harness(base_url=stack.base_url, ids=stack.ids, browser=browser,
                    cookie_name=session_cookie_name(get_settings()),
                    secret_key=stack.secret, env=stack.env())
        try:
            for shot in shots:
                ctx, page, log = await h.page(shot.account, width=VIEWPORT["width"])
                try:
                    url = stack.base_url + shot.path(stack.ids)
                    response = await page.goto(url, wait_until="networkidle")
                    if response is None or response.status != 200:
                        raise RuntimeError(f"{shot.id}: GET {url} gave "
                                           f"{response.status if response else 'no response'}")
                    await shot.prepare(page)
                    await page.wait_for_timeout(500)
                    if shot.clip:
                        # An element shot scrolls the page; the detail page's sticky jump
                        # nav would then be painted over the element's middle.
                        await page.add_style_tag(
                            content=".assessment-jump-nav{position:static!important}")
                        data = await page.locator(shot.clip).first.screenshot(
                            animations="disabled")
                    else:
                        # The viewport only, never full_page: long pages would make very
                        # tall figures and break the size cap.
                        data = await page.screenshot(animations="disabled")
                    problems = (log["console"] + log["pageerror"] + log["requestfailed"]
                                + log["unexpected_network"])
                    if problems:
                        raise RuntimeError(f"{shot.id}: {problems[:5]}")
                finally:
                    await ctx.close()
                (out / f"{shot.id}.png").write_bytes(data)
                sizes[shot.id] = png_size(data)
                print(f"captured {shot.id} {sizes[shot.id][0]}x{sizes[shot.id][1]}", flush=True)
        finally:
            await browser.close()
    return sizes


def _commit() -> str:
    done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                          text=True)
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def _install(shots: list[Shot], sizes: dict, out: Path, *, full: bool) -> None:
    """Move the new PNGs into SHOTS_DIR and write their manifest entries."""
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {} if full else load_manifest()
    commit = _commit()
    for shot in shots:
        shutil.copyfile(out / f"{shot.id}.png", SHOTS_DIR / f"{shot.id}.png")
        width, height = sizes[shot.id]
        manifest[shot.id] = {"audience": shot.audience, "alt": shot.alt, "width": width,
                             "height": height, "sources": source_hashes(shot),
                             "commit": commit}
    if full:
        for png in SHOTS_DIR.glob("*.png"):
            if png.stem not in SHOTS_BY_ID:
                png.unlink()
    MANIFEST.write_text(json.dumps(dict(sorted(manifest.items())), indent=2) + "\n",
                        encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.help_shots.capture")
    parser.add_argument("--only", action="append", default=[], metavar="ID",
                        help="recapture only this shot (repeatable)")
    parser.add_argument("--executable", default="",
                        help="path to a local chromium or chrome-headless-shell")
    parser.add_argument("--check", action="store_true",
                        help="report stale and missing shots; no Docker, no browser")
    args = parser.parse_args()
    if args.check:
        if args.only or args.executable:
            parser.error("--check takes no other option")
        sys.exit(check())
    unknown = [i for i in args.only if i not in SHOTS_BY_ID]
    if unknown:
        parser.error(f"unknown shot id(s): {', '.join(unknown)}")
    shots = [SHOTS_BY_ID[i] for i in dict.fromkeys(args.only)] if args.only else list(SHOTS)

    os.chdir(REPO_ROOT)
    # SIGTERM must still reach the finally below, so the container and the server stop.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    recorder = OutboundRecorder()
    recorder.start()
    out = Path(tempfile.mkdtemp(prefix="help-shots-"))
    stack = ShotStack(recorder.url)
    try:
        try:
            stack.up()
            sizes = asyncio.run(_capture(stack, shots, args.executable, out))
        finally:
            stack.down()
            recorder.stop()
            os.chdir(REPO_ROOT)
            # Reported even when the capture failed: a missed stub often shows up first
            # as a degraded page.
            if recorder.hits:
                print("outbound connection attempts (a fetcher is not stubbed):",
                      file=sys.stderr)
                for hit in recorder.hits:
                    print(f"  {hit}", file=sys.stderr)
        if recorder.hits:
            print("nothing was written to help_assets/shots", file=sys.stderr)
            sys.exit(1)
        _install(shots, sizes, out, full=not args.only)
    finally:
        shutil.rmtree(out, ignore_errors=True)
    print(f"wrote {len(shots)} shot(s) and {MANIFEST.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
