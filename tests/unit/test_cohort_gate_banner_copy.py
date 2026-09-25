"""The cohort-gate banner's operator instructions.

The partial is rendered on its own, with a bare Jinja environment, for both
isolation states. It must never name the other deployment's unprefixed
simulation container (stopping that container stops org1's production run), and
it must say that a settings change needs a container RECREATE once
/admin/simulation shows no live run: ``.env`` is resolved at container creation,
so a restart keeps the old environment.
"""

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[2]
FOREIGN_CONTAINER = re.compile(r"(?<![\w-])agent-run(?![\w-])")


def _render(isolation_enabled: bool) -> str:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"))
    gate = {
        "preflight_error": None,
        "isolation_enabled": isolation_enabled,
        "default_policy": "open",
        "summary": {"gated": 0, "total": 0, "isolated": [], "unrestricted": []},
        "bot_names": {},
        "snapshot": None,
    }
    return env.get_template("admin/_cohort_gate_banner.html").render(gate=gate)


@pytest.mark.parametrize("isolation_enabled", [False, True])
def test_banner_never_names_the_other_deployments_container(isolation_enabled):
    html = _render(isolation_enabled)
    assert "Cohort isolation is" in html  # the partial actually rendered
    assert not FOREIGN_CONTAINER.search(html)


@pytest.mark.parametrize("isolation_enabled", [False, True])
def test_banner_says_recreate_not_restart(isolation_enabled):
    html = _render(isolation_enabled)
    assert "--force-recreate" in html
    assert "/admin/simulation" in html


def test_the_isolation_off_notice_itself_says_recreate():
    """The OFF notice is its own sentence, outside the shared settings list, so the
    two parametrized checks above would still pass if only it went back to "restart".
    """
    html = _render(False)
    start = html.index("Cohort isolation is OFF")
    notice = html[start : html.index("</div>", start)]
    assert "<em>recreating</em>" in notice
    assert "/admin/simulation" in notice
    assert "a restart keeps the old environment" in notice
