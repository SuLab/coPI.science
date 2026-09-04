"""Static assets must revalidate on every load so a redeployed file reaches
browsers immediately.

Root cause of a real incident (2026-09-04): the assessment-detail strikethrough
fix shipped in static/js/markdown.js, but /static was served with only
ETag/Last-Modified and no Cache-Control, so browsers applied heuristic freshness
and kept executing a copy cached before the deploy — the fix did not reach
already-visited clients until a manual hard refresh. `_NoCacheStaticFiles`
(src/main.py) adds `Cache-Control: no-cache`, which forces an ETag revalidation
each load (cheap 304s) and picks up a new asset on the first request after a
deploy.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_static_assets_send_no_cache(client):
    resp = await client.get("/static/js/markdown.js")
    assert resp.status_code == 200
    assert "no-cache" in resp.headers.get("cache-control", "")
