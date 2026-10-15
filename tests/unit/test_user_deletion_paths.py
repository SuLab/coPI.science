"""The deletion teardown's archived-copy directory is the one persona_lifecycle archives to
(spec 2026-10-15 §10 R5.21). Read from the sources, not the module attributes, which
tests/conftest.py monkeypatches (the pattern of tests/unit/test_pi_companies.py:281-287)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_orphaned_dir_constants_agree():
    services = ROOT / "src" / "services"
    line = 'ORPHANED_DIR = Path("profiles/private/orphaned")'
    assert line in (services / "persona_lifecycle.py").read_text(encoding="utf-8")
    assert f"_{line}" in (services / "user_deletion.py").read_text(encoding="utf-8")
