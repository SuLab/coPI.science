"""Pure-logic tests for scripts/backup/copi_backup.py.

No database, no Docker, no network: everything here exercises decision logic —
config parsing, dump-name round-tripping, the retention selector, the row-count
comparison, and the status document. The Docker-facing half is covered by
scripts/backup/failure_injection.sh, which runs on the host and is not part of
scripts/ci.sh.

Like scripts/migrate, scripts/backup is a script and not an importable package
(there is no ``__init__.py`` anywhere under ``scripts/``), so the module is loaded
by path. Registering it in ``sys.modules`` BEFORE ``exec_module`` is load-bearing
rather than tidiness: ``@dataclass`` resolves its annotations through
``sys.modules[cls.__module__]``, and this module defines five dataclasses.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "backup" / "copi_backup.py"


def _load():
    spec = importlib.util.spec_from_file_location("copi_backup", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["copi_backup"] = module
    spec.loader.exec_module(module)
    return module


cb = _load()


def test_parse_stacks_reads_four_colon_separated_fields():
    stacks = cb.parse_stacks("copi-python:copi-python-postgres-1:copi:copi")
    assert len(stacks) == 1
    assert stacks[0].name == "copi-python"
    assert stacks[0].container == "copi-python-postgres-1"
    assert stacks[0].db == "copi"
    assert stacks[0].user == "copi"


def test_parse_stacks_handles_multiline_and_indentation():
    raw = """copi-python:copi-python-postgres-1:copi:copi
        copi-blackbird:copi-blackbird-postgres-1:copi:copi"""
    stacks = cb.parse_stacks(raw)
    assert [s.name for s in stacks] == ["copi-python", "copi-blackbird"]


def test_parse_stacks_rejects_wrong_field_count():
    with pytest.raises(cb.ConfigError, match="expected 4 colon-separated fields"):
        cb.parse_stacks("copi-python:copi-python-postgres-1:copi")


def test_parse_stacks_rejects_duplicate_stack_names():
    raw = "a:c1:db:u\na:c2:db:u"
    with pytest.raises(cb.ConfigError, match="duplicate stack name"):
        cb.parse_stacks(raw)


def test_parse_stacks_rejects_empty():
    with pytest.raises(cb.ConfigError, match="no stacks configured"):
        cb.parse_stacks("   \n  ")


def test_load_config_applies_documented_defaults():
    cfg = cb.load_config({"STACKS": "a:c:d:u"})
    assert cfg.retention_count == 5
    assert cfg.retention_unverified == 2
    assert cfg.verify_image == "postgres:15"
    assert cfg.free_space_factor == 3
    assert cfg.offsite_cmd == ""


def test_load_config_clamps_retention_count_to_at_least_one():
    # A configured 0 would let the pruner empty the directory; the floor is structural.
    cfg = cb.load_config({"STACKS": "a:c:d:u", "RETENTION_COUNT": "0"})
    assert cfg.retention_count == 1


def test_load_config_rejects_non_integer_retention():
    with pytest.raises(cb.ConfigError, match="RETENTION_COUNT"):
        cb.load_config({"STACKS": "a:c:d:u", "RETENTION_COUNT": "five"})


def test_load_config_splits_mail_recipients_on_whitespace():
    cfg = cb.load_config({"STACKS": "a:c:d:u", "MAIL_TO": "x@e.com  y@e.com\nz@e.com"})
    assert cfg.mail_to == ["x@e.com", "y@e.com", "z@e.com"]
