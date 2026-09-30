"""The responsive tier serves vendored copies of marked/DOMPurify in place of
cdn.jsdelivr.net so it runs offline. They must be byte-identical to what
production loads, which the SRI hashes in templates/agent/dashboard.html pin:
if the vendored file drifts, the tier passes against different code than users run.
"""

import base64
import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR = REPO_ROOT / "tests" / "responsive" / "_vendor"
DASHBOARD = REPO_ROOT / "templates" / "agent" / "dashboard.html"

_SCRIPT_RE = re.compile(
    r'<script\s+src="https://cdn\.jsdelivr\.net/npm/(?P<pkg>[^@/]+)@[^"]+"[^>]*'
    r'integrity="sha384-(?P<hash>[^"]+)"',
    re.S,
)


def _pinned() -> dict[str, str]:
    found = {m.group("pkg"): m.group("hash") for m in _SCRIPT_RE.finditer(DASHBOARD.read_text())}
    assert {"marked", "dompurify"} <= set(found), found
    return found


@pytest.mark.parametrize(
    ("pkg", "filename"), [("marked", "marked.min.js"), ("dompurify", "purify.min.js")]
)
def test_vendored_file_matches_the_sri_hash_production_loads(pkg, filename):
    digest = hashlib.sha384((VENDOR / filename).read_bytes()).digest()
    assert base64.b64encode(digest).decode() == _pinned()[pkg], (
        f"{filename} differs from the SRI-pinned {pkg} bundle in dashboard.html"
    )
