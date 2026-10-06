"""Repair scripts write personas through §4.3 (spec 2026-10-05 §4.3, P31)."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _calls(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    return {getattr(n.func, "id", getattr(n.func, "attr", None))
            for n in ast.walk(tree) if isinstance(n, ast.Call)}


def test_repair_scripts_record_and_write_after_commit():
    for rel in ("scripts/audit_pub_dois.py", "scripts/grants_remediation.py"):
        calls = _calls(rel)
        assert "export_profile_to_markdown" not in calls, rel
        assert {"reexport_persona", "write_persona_files"} <= calls, rel
