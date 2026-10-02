"""Vendored front-end scripts (spec §6.2, M-04): the files are the ones the manifest
names, byte for byte, and the pages load them from /static/vendor/."""

import base64
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "static" / "vendor"
MANIFEST = VENDOR / "MANIFEST.md"

_ROW = re.compile(
    r"^\| `(?P<file>[^`]+)` \| (?P<package>[a-z]+) \| (?P<version>\d+\.\d+\.\d+) \| "
    r"(?P<url>https://\S+) \| `(?P<integrity>sha512-[A-Za-z0-9+/=]+)` \| "
    r"`(?P<sha384>sha384-[A-Za-z0-9+/=]+)` \|$",
    re.M,
)

#: The SRI the old jsDelivr tag for marked 12.0.2 carried (templates/_head_assets.html
#: before this change). The vendored file must be that same file.
MARKED_CDN_SRI = "sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi"


def _rows() -> dict[str, dict[str, str]]:
    return {
        m["package"]: m.groupdict()
        for m in _ROW.finditer(MANIFEST.read_text(encoding="utf-8"))
    }


def test_manifest_lists_exactly_the_vendored_files():
    listed = {r["file"] for r in _rows().values()}
    on_disk = {p.name for p in VENDOR.iterdir() if p.name != "MANIFEST.md"}
    assert listed == on_disk
    assert set(_rows()) == {"marked", "dompurify"}


def test_every_vendored_file_matches_its_recorded_sha384():
    for row in _rows().values():
        digest = hashlib.sha384((VENDOR / row["file"]).read_bytes()).digest()
        assert row["sha384"] == "sha384-" + base64.b64encode(digest).decode(), row["file"]


def test_versions_are_the_pinned_ones():
    rows = _rows()
    assert rows["marked"]["version"] == "12.0.2"
    assert rows["marked"]["file"] == "marked-12.0.2.min.js"
    assert rows["dompurify"]["version"].startswith("3.4.")
    assert rows["dompurify"]["file"] == f"purify-{rows['dompurify']['version']}.min.js"


def test_marked_is_the_file_the_cdn_pin_named():
    assert _rows()["marked"]["sha384"] == MARKED_CDN_SRI


def test_head_assets_load_the_vendored_files_before_the_renderer():
    head = (ROOT / "templates" / "_head_assets.html").read_text(encoding="utf-8")
    rows = _rows()
    marked = f'<script src="/static/vendor/{rows["marked"]["file"]}"></script>'
    purify = f'<script src="/static/vendor/{rows["dompurify"]["file"]}"></script>'
    renderer = '<script src="/static/js/markdown.js"></script>'
    assert marked in head and purify in head and renderer in head
    assert head.index(marked) < head.index(renderer)
    assert head.index(purify) < head.index(renderer)
    assert "cdn.jsdelivr.net" not in head
