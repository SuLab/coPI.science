import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_only_the_factory_constructs_jinja2templates():
    hits = []
    for p in (REPO / "src").rglob("*.py"):
        if "__pycache__" in p.parts or p == REPO / "src/web/templating.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "Jinja2Templates":
                hits.append(str(p.relative_to(REPO)))
    assert hits == []


def test_factory_registers_every_global_filter_and_test():
    from src.web.templating import make_templates

    env = make_templates().env
    assert {"key_point_sections", "md_citations", "plain_citations", "band_class", "band_label",
            "staff_only_verdict_fields"} <= set(env.globals)
    assert "ts" in env.filters
    assert "truncated_stop" in env.tests


def test_each_call_returns_a_new_instance():
    from src.web.templating import make_templates

    assert make_templates() is not make_templates()


def test_head_assets_are_included_not_repeated():
    marked = 'src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"'
    # cabo_graph.html switches to the include in companion Task 115.
    skip = {"_head_assets.html", "cabo_graph.html"}
    offenders = [str(p.relative_to(REPO)) for p in (REPO / "templates").rglob("*.html")
                 if marked in p.read_text(encoding="utf-8") and p.name not in skip]
    assert offenders == []
