"""templates/base.html renders valid JS in the posthog.identify block.

DOC-5 (issue #26): the object literal at base.html:20 is missing its closing
`}` before the call's `)` — a JS SyntaxError on every logged-in page of any
deployment with POSTHOG_API_KEY set (the earlier posthog.init <script> is a
separate tag and is unaffected, so the failure is silent, not a page crash).
Renders through the app's own Jinja2Templates instance (same construction as
every router) so a future template edit is caught the same way a route would
hit it.

COR-16 (issue #22): the fix above interpolated ``current_user.name``/``.email``
straight into single-quoted JS string literals. Jinja2's HTML autoescaping
turns a plain apostrophe into ``&#39;``, which parses fine but silently
corrupts the identified value (PostHog sees ``O&#39;Brien``, not
``O'Brien``). Worse, a value ending in a bare backslash (nothing HTML-special
about it) escapes the closing quote and produces an outright JS SyntaxError.
Both are fixed by piping the values through ``|tojson``, which JSON-encodes
before Jinja can HTML-escape and is marked template-safe.
"""

import json
import re
import shutil
import subprocess
import types

import pytest
from fastapi.templating import Jinja2Templates

_TEMPLATE = Jinja2Templates(directory="templates").env.get_template("base.html")
_SCRIPT_RE = re.compile(r"<script>(posthog\.identify\([^<]*)</script>")


def _render(*, name="Andrew Su", email):
    request = types.SimpleNamespace(
        state=types.SimpleNamespace(posthog_api_key="phc_test123")
    )
    current_user = types.SimpleNamespace(
        id="1111-2222", name=name, email=email, is_admin=False,
    )
    return _TEMPLATE.render(
        request=request,
        current_user=current_user,
        impersonation_banner=None,
        active_page=None,
        flash_message=None,
    )


@pytest.mark.parametrize("email", ["a@b.org", None])
def test_posthog_identify_script_is_balanced_and_parses(email, tmp_path):
    html = _render(email=email)
    m = _SCRIPT_RE.search(html)
    assert m, "posthog.identify <script> block not found in rendered base.html"
    script = m.group(1)

    assert script.count("{") == script.count("}"), script
    assert script.count("(") == script.count(")"), script

    if shutil.which("node"):
        js_file = tmp_path / "identify.js"
        js_file.write_text(script + ";\n")
        result = subprocess.run(
            ["node", "--check", str(js_file)], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


def test_posthog_identify_preserves_apostrophe_in_name_and_email(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not available to evaluate the rendered script")

    html = _render(name="O'Brien", email="o'brien@example.org")
    m = _SCRIPT_RE.search(html)
    assert m, "posthog.identify <script> block not found in rendered base.html"
    script = m.group(1)

    js_file = tmp_path / "identify.js"
    js_file.write_text(
        "var __captured;\n"
        "var posthog = {identify: function(id, props) { __captured = props; }};\n"
        f"{script}\n"
        "console.log(JSON.stringify(__captured));\n"
    )
    result = subprocess.run(["node", str(js_file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    captured = json.loads(result.stdout)
    assert captured["name"] == "O'Brien", captured
    assert captured["email"] == "o'brien@example.org", captured
