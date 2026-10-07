"""Surfaces removed in Phase 1 of the web UI remediation (spec §6.4) stay removed."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

GRAPH_PATHS = (
    "/cabo-graph",
    "/scripps-graph",
    "/schultz-alumni-pilot",
    "/schultz-group-alumni",
)


def _routes() -> set[tuple[str, str]]:
    from src.main import create_app

    return {
        (method, route.path)
        for route in create_app().routes
        for method in (getattr(route, "methods", None) or ())
    }


def test_the_graph_pages_are_gone():
    """D9: the four unauthenticated graph pages, their template and their nginx
    rate-limit block (A-13, M-05)."""
    paths = {path for _, path in _routes()}
    assert not set(GRAPH_PATHS) & paths
    assert not (ROOT / "templates" / "cabo_graph.html").exists()
    nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    assert "cabo-graph" not in nginx
    assert "req_graph" not in nginx


def test_the_markdown_factory_has_no_graph_profile():
    js = (ROOT / "static" / "js" / "markdown.js").read_text(encoding="utf-8")
    assert '"graph"' not in js


def test_the_sankey_script_and_plotly_are_gone():
    """D10."""
    assert not (ROOT / "scripts" / "build_cabo_sankey.py").exists()
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    assert not [d for d in deps if d.lower().startswith("plotly")]


def test_posthog_is_gone():
    """D13 / A-12: no snippet, no middleware, no setting, no /ingest proxy."""
    import src.main as main_mod
    from src.config import Settings

    assert not hasattr(main_mod, "PostHogContextMiddleware")
    assert not hasattr(main_mod, "AgentBadgeMiddleware")
    assert "posthog_api_key" not in Settings.model_fields
    names = [m.cls.__name__ for m in main_mod.create_app().user_middleware]
    assert "PostHogContextMiddleware" not in names
    for rel in (
        "templates/base.html",
        "templates/assessments/_chat_drawer.html",
        "nginx/nginx.conf",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8").lower()
        assert "posthog" not in text, rel
        assert "ph-no-capture" not in text, rel


def test_connect_slack_is_gone():
    """D16 (A-08, D-03, D-04, D-05): no route, no banner, no Slack lookup by email."""
    from src.services import slack_web

    paths = {path for _, path in _routes()}
    assert "/agent/{agent_id}/delegates/connect-slack" not in paths
    for name in ("lookup_user_by_email", "lookup_user_by_email_async", "get_user_info"):
        assert not hasattr(slack_web, name), name
    for rel in (
        "src/routers/agent_page.py",
        "src/routers/invite.py",
        "templates/agent/dashboard.html",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for needle in ("lookup_user_by_email", "connect-slack", "delegate_has_slack"):
            assert needle not in text, (rel, needle)


def test_the_delegate_slack_ids_column_is_kept():
    """D16 keeps the column and its data, unread."""
    from src.models import AgentRegistry

    assert "delegate_slack_ids" in AgentRegistry.__table__.columns
