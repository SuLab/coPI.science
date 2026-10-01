import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts/lib/mutation_harness.sh"
SCRIPTS = ["mutate_cohorts.sh", "mutate_slack_mirror.sh", "mutate_system.sh"]


def test_every_script_sources_the_lib_and_defines_no_copy():
    for name in SCRIPTS:
        text = (ROOT / "scripts" / name).read_text()
        assert "scripts/lib/mutation_harness.sh" in text or 'lib/mutation_harness.sh"' in text
        for fn in ("secure_override_dir() {", "copy_is_safe() {"):
            assert fn not in text, f"{name} still defines {fn}"


def test_inert_is_scored_separately():
    script = f"""
set -u
source "{LIB}"
killed=0 survived=0 errors=0 broken_inert=0 inert_ok=0 fail=0; SURVIVORS=()
log=$(mktemp); echo 'FAILED tests/x.py::t' > "$log"
mh_score 1 "$log" "M1 real" 0
mh_score 0 "$log" "M2 INERT control" 1
mh_score 0 "$log" "M3 real" 0
mh_report demo
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
    assert "killed 1/2 real mutants" in out
    assert "inert controls: 1/1 survived" in out
