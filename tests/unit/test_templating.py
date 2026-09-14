"""Tests for the shared Jinja environment in src/templating.py."""
import hashlib
import logging
from pathlib import Path

from src.templating import css_version, templates

ROUTERS_DIR = Path(__file__).resolve().parents[2] / "src" / "routers"


def test_css_version_missing_file_logs_error(tmp_path, caplog):
    missing = tmp_path / "x.css"

    with caplog.at_level(logging.ERROR):
        result = css_version(missing)

    assert result == "missing"
    assert any(
        record.levelno == logging.ERROR and str(missing) in record.getMessage()
        for record in caplog.records
    )


def test_css_version_returns_sha256_prefix(tmp_path):
    css_file = tmp_path / "app.min.css"
    content = b"body { color: red; }"
    css_file.write_bytes(content)

    result = css_version(css_file)

    assert result == hashlib.sha256(content).hexdigest()[:12]


def test_routers_import_templates_from_shared_module():
    router_files = sorted(ROUTERS_DIR.glob("*.py"))
    assert router_files, "expected router files to exist"

    checked_any = False
    for path in router_files:
        source = path.read_text()
        if "templates." not in source:
            continue
        checked_any = True
        assert "from src.templating import templates" in source, path
        assert "Jinja2Templates(" not in source, path

    assert checked_any, "expected at least one router referencing templates."


def test_templates_env_has_css_version_global():
    value = templates.env.globals["css_version"]
    assert isinstance(value, str)
    assert value
