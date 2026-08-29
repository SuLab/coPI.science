"""Capture git identity into .build_info.json — run at IMAGE BUILD time.

Invoked by the Dockerfile in the one layer where a git binary exists (installed
and purged in the same RUN). Fails LOUDLY on any git error: a build that cannot
say what it built should not succeed silently. See src/services/build_info.py
for the runtime reader and the fallback order.
"""
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def main() -> int:
    try:
        info = {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty_files": len(
                _git("status", "--porcelain", "--untracked-files=no").splitlines()
            ),
            "generated_at": datetime.now(UTC).isoformat(),
            "generator": "scripts/write_build_info.py",
        }
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"write_build_info: git failed: {exc}", file=sys.stderr)
        return 1
    (ROOT / ".build_info.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8"
    )
    print(f"write_build_info: {info['commit'][:12]} on {info['branch']}, "
          f"{info['dirty_files']} dirty file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
