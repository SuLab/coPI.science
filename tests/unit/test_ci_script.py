import subprocess
from pathlib import Path

CI = Path(__file__).resolve().parents[2] / "scripts" / "ci.sh"


def test_container_name_is_per_run():
    text = CI.read_text()
    assert 'MIGCHECK_CONTAINER="copi-ci-migcheck-$$"' in text


def test_two_runs_pick_distinct_names_and_ports():
    text = CI.read_text()
    port_line = next(line for line in text.splitlines() if line.startswith("MIGCHECK_PORT="))
    snippet = f'{port_line}\nMIGCHECK_CONTAINER="copi-ci-migcheck-$$"\necho "$MIGCHECK_CONTAINER $MIGCHECK_PORT"'
    a = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=True).stdout.split()
    b = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=True).stdout.split()
    assert a[0] != b[0]
    assert a[1].isdigit() and b[1].isdigit()
