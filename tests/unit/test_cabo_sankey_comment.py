"""build_cabo_sankey.py's DEFAULT_START comment must not read like an open
TODO — --start has taken a real argparse value since before issue #26 was
filed (git log -S"add_argument.*--start" -- scripts/build_cabo_sankey.py).
"""

from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "build_cabo_sankey.py"
).read_text()


def test_default_start_comment_mentions_the_start_flag():
    line = next(
        line for line in SCRIPT.splitlines() if 'DEFAULT_START = "2026-05-01"' in line
    )
    idx = SCRIPT.splitlines().index(line)
    comment = SCRIPT.splitlines()[idx - 1]
    assert "--start" in comment
