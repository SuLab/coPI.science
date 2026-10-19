"""Pure helpers of scripts/dev/talent_conduct_replay.py.

The script is loaded by path (scripts/ is not a package), as in
tests/unit/test_opus55_replay.py. Nothing here reaches the API or a database.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "talent_conduct_replay", ROOT / "scripts" / "dev" / "talent_conduct_replay.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


R = _load()


def test_split_round_trips_the_consult_header_format():
    question, context = "Can this lab execute?", "PI: yes.\n\nHub: how?\nPI: with a postdoc."
    content = f"## Question from the hub\n\n{question}\n\n## What the PI has said\n\n{context}"
    assert R.split_consult_message(content) == (question, context)


def test_split_returns_none_for_other_messages():
    assert R.split_consult_message("just a message") is None
    assert R.split_consult_message("## Question from the hub\n\nno context header") is None
    assert R.split_consult_message("## What the PI has said\n\nctx") is None


def test_flag_conduct_returns_the_matched_term():
    assert R.flag_conduct("That candor is itself evidence of rigor") == ["candor"]


def test_flag_conduct_lowercases_orders_and_dedupes():
    text = "Volunteered? Unprompted. The PI VOLUNTEER-ed it, an Unprompted concession"
    assert R.flag_conduct(text) == ["volunteer", "unprompted", "concession"]


def test_flag_conduct_empty_when_no_term():
    assert R.flag_conduct("Bandwidth is the binding constraint.") == []
