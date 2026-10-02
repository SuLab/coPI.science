"""Spec §7.3: the admin split keeps the route set and never lets a parameterised
route shadow a literal sibling."""

import pathlib
import re

from src.routers import admin

EXPECTED_ROUTES = {
    ("GET", ""), ("GET", "/users"), ("GET", "/users/{user_id}"),
    ("POST", "/users/{user_id}/delete"), ("POST", "/users/{user_id}/role"),
    ("POST", "/users/{user_id}/verify-email"),
    ("GET", "/jobs"),
    ("GET", "/activity"), ("GET", "/activity/{run_id}"), ("GET", "/activity/{run_id}/llm-calls"),
    ("GET", "/activity/{run_id}/llm-calls/{call_id}/bodies"),
    ("GET", "/discussions"),
    ("GET", "/agents"), ("GET", "/agents/{agent_id}"), ("POST", "/agents/{agent_id}/ensure-spoke"),
    ("POST", "/agents/{agent_id}/approve"), ("POST", "/agents/{agent_id}/reject"),
    ("POST", "/agents/{agent_id}/slack/provision"), ("GET", "/agents/slack/callback"),
    ("POST", "/agents/{agent_id}/link"), ("POST", "/agents/{agent_id}/role"),
    ("GET", "/assessments"), ("GET", "/assessments/{assessment_id}"),
    ("POST", "/impersonate"), ("POST", "/impersonate/stop"),
    ("GET", "/access-requests"), ("POST", "/access-requests/{user_id}/approve"),
    ("POST", "/access-requests/{user_id}/deny"), ("POST", "/access-allowlist/add"),
    ("POST", "/access-allowlist/{entry_id}/remove"),
    ("GET", "/cohorts"), ("POST", "/cohorts/create"), ("GET", "/cohorts/topology"),
    ("POST", "/cohorts/topology"), ("POST", "/cohorts/ensure-star-spokes"),
    ("GET", "/cohorts/{cohort_id}"), ("POST", "/cohorts/{cohort_id}/delete"),
    ("POST", "/cohorts/{cohort_id}/add-agent"), ("POST", "/cohorts/{cohort_id}/remove-agent"),
    ("GET", "/simulation"), ("POST", "/simulation/start"), ("POST", "/simulation/stop"),
    ("POST", "/simulation/finalize-run"),
    ("POST", "/simulation/announce-settings"), ("POST", "/simulation/announce-template"),
}


def _routes():
    return [(i, m, r.path) for i, r in enumerate(admin.router.routes)
            for m in sorted(getattr(r, "methods", ())) if m != "HEAD"]


def test_the_admin_route_set_is_unchanged():
    assert {(m, p) for _i, m, p in _routes()} == EXPECTED_ROUTES
    assert len(_routes()) == 45


def test_literal_routes_are_registered_before_parameterised_siblings():
    def shadows(param_path: str, literal_path: str) -> bool:
        a, b = param_path.split("/"), literal_path.split("/")
        return param_path != literal_path and len(a) == len(b) and all(
            x == y or (x.startswith("{") and x.endswith("}")) for x, y in zip(a, b, strict=True)
        )

    problems = []
    for i, m, p in _routes():
        for j, m2, p2 in _routes():
            if m == m2 and shadows(p, p2) and i < j:
                problems.append(f"{m} {p} is registered before its literal sibling {p2}")
    assert problems == [], "\n".join(problems)


def test_admin_modules_log_as_src_routers_admin():
    pkg = pathlib.Path(admin.__file__).parent
    for path in sorted(pkg.glob("*.py")):
        names = re.findall(r"getLogger\(([^)]*)\)", path.read_text(encoding="utf-8"))
        assert all(n == '"src.routers.admin"' for n in names), (path.name, names)
