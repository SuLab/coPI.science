"""Module-scoped, synchronous fixtures for the responsive Playwright tier.

Everything here is sync: Playwright's sync API and pytest's regular (non-asyncio)
fixture machinery are the point — this tier drives a real browser against a real
uvicorn subprocess, not the ASGI-transport/rolled-back-transaction harness the
rest of the suite uses (see ``tests/conftest.py``).
"""

import asyncio
import concurrent.futures
import contextlib
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from src.config import get_settings
from tests import factories  # noqa: F401  (re-exported for local helpers, if needed)
from tests.e2e.session import COOKIE_NAME, forge_session_cookie
from tests.responsive import seed

REPO_ROOT = Path(__file__).resolve().parents[2]
_VENDOR_DIR = Path(__file__).resolve().parent / "_vendor"
_OUT_DIR = Path(__file__).resolve().parent / "_out"

# RESPONSIVE_SERVER_ROOT=<other checkout> runs uvicorn from that tree (same test DB),
# and RESPONSIVE_SCREENSHOT_ONLY=1 skips the assertions — together they produce the
# "before" screenshot set for the human desktop-drift review:
#   git worktree add /tmp/before <base-commit>
#   RESPONSIVE_SERVER_ROOT=/tmp/before RESPONSIVE_SCREENSHOT_ONLY=1 \
#     RESPONSIVE_OUT_SUBDIR=before pytest tests/responsive -k 1280
SCREENSHOT_ONLY = os.environ.get("RESPONSIVE_SCREENSHOT_ONLY") == "1"
OUT_SUBDIR = os.environ.get("RESPONSIVE_OUT_SUBDIR", "")

VIEWPORTS = {
    320: dict(viewport={"width": 320, "height": 568}),
    390: dict(viewport={"width": 390, "height": 844}, is_mobile=True, device_scale_factor=2),
    768: dict(viewport={"width": 768, "height": 1024}),
    1280: dict(viewport={"width": 1280, "height": 800}),
}


@pytest.fixture(scope="module", autouse=True)
def _css_built():
    # Checked against the tree the server actually runs from. A RESPONSIVE_SERVER_ROOT
    # baseline may predate the compiled build (it still styles itself via the CDN), so
    # the guard is relaxed only in screenshot-only mode.
    root = Path(os.environ.get("RESPONSIVE_SERVER_ROOT", REPO_ROOT))
    if not (root / "static" / "css" / "app.min.css").exists() and not SCREENSHOT_ONLY:
        pytest.fail(f"compiled stylesheet missing under {root}: run scripts/build-css.sh")


def _run_in_fresh_loop(coro):
    """Run ``coro`` to completion on its own event loop in a worker thread.

    pytest-asyncio (``asyncio_mode = "auto"``) can leave an event loop running in the
    main thread while module-scoped sync fixtures are set up, and ``asyncio.run``
    refuses to nest. A throwaway thread has no running loop, so ``asyncio.run`` is
    legal there; the asyncpg connections are created and closed inside that loop.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@pytest.fixture(scope="module")
def seeded(_migrated):
    """Seed the migrated DB, yield the ids dict, truncate on teardown.

    Runs its own ``asyncio.run`` loop against ``_migrated`` (the DSN), separate
    from any event loop pytest-asyncio manages elsewhere in the suite.
    """
    ids = _run_in_fresh_loop(seed.run(_migrated))
    yield ids
    _run_in_fresh_loop(seed.teardown(_migrated))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(seeded, _migrated, tmp_path_factory):
    """Run the app as a uvicorn subprocess against the seeded DB.

    Declares ``seeded`` so pytest tears the server down before ``seed.teardown``
    truncates the tables it wrote (a live child holding an open transaction would
    otherwise make the TRUNCATE hang).
    """
    port = _free_port()
    # uvicorn writes one access line per request; a PIPE nobody drains fills after
    # 64 KiB and blocks the child, so log to a file and read it only on failure.
    log_path = tmp_path_factory.mktemp("responsive") / "uvicorn.log"
    log_file = log_path.open("w")
    env = {
        **os.environ,
        # SLACK_ENABLED=false is the real guard: Settings also reads .env from the
        # child's cwd, so filtering SLACK_* out of os.environ would prove nothing.
        "DATABASE_URL": _migrated,
        "ENVIRONMENT": "development",
        "ALLOW_HTTP_SESSIONS": "true",
        "SECRET_KEY": get_settings().secret_key,
        "POSTHOG_API_KEY": "",
        "SLACK_ENABLED": "false",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.environ.get("RESPONSIVE_SERVER_ROOT", str(REPO_ROOT)),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    import time

    deadline = time.monotonic() + 30
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            r = httpx.get(f"{base_url}/api/health", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)

    if not ready:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)
        proc.kill()
        log_file.close()
        output = log_path.read_text()
        tail = "\n".join(output.splitlines()[-40:])
        pytest.fail(f"server did not become healthy within 30s; last output:\n{tail}")

    # Sanity: prove the child is on the seeded (test) DB, not the dev DB — a
    # dev-DB child would 302 on this unknown admin id.
    resp = httpx.get(
        f"{base_url}/admin/users",
        cookies={COOKIE_NAME: forge_session_cookie(seeded["admin_id"])},
        follow_redirects=False,
    )
    if resp.status_code != 200:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)
        proc.kill()
        log_file.close()
        tail = "\n".join(log_path.read_text().splitlines()[-40:])
        pytest.fail(
            "server sanity check failed: GET /admin/users -> "
            f"{resp.status_code} (location={resp.headers.get('location')!r}); "
            f"child is likely not on the seeded test database. Last output:\n{tail}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_file.close()


@pytest.fixture(scope="module")
def pw():
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    p = sync_api.sync_playwright().start()
    yield p
    p.stop()


@pytest.fixture(scope="module")
def browser(pw):
    try:
        b = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        # Only a missing browser binary is a legitimate skip; any other launch failure
        # (crash, missing shared library, misconfiguration) must fail the gate.
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            pytest.skip(f"Chromium not installed: {msg.splitlines()[0]}")
        raise
    yield b
    b.close()


def _route_vendor(route, request):
    """Serve vendored marked/DOMPurify in place of cdn.jsdelivr.net."""
    url = request.url
    if "marked" in url:
        path = _VENDOR_DIR / "marked.min.js"
    elif "dompurify" in url or "purify" in url:
        path = _VENDOR_DIR / "purify.min.js"
    else:
        route.continue_()
        return
    route.fulfill(
        status=200,
        content_type="application/javascript",
        body=path.read_bytes(),
    )


def make_context(browser, base_url, width, who, ids):
    """Build one browser context for (width, who), wired for offline CDN + auth."""
    kwargs = dict(VIEWPORTS[width])
    kwargs["reduced_motion"] = "reduce"
    context = browser.new_context(**kwargs)
    context.route("https://cdn.jsdelivr.net/**", _route_vendor)
    if who is not None:
        user_id = ids[f"{who}_id"]
        context.add_cookies(
            [{"name": COOKIE_NAME, "value": forge_session_cookie(user_id), "url": base_url}]
        )
    return context


@pytest.fixture(scope="module")
def contexts(browser, server, seeded):
    """Cache of one context per (viewport, who), closed at module teardown."""
    cache: dict[tuple[int, str | None], object] = {}
    yield cache
    for ctx in cache.values():
        ctx.close()


def get_context(contexts, browser, server, seeded, width, who):
    key = (width, who)
    if key not in contexts:
        contexts[key] = make_context(browser, server, width, who, seeded)
    return contexts[key]
