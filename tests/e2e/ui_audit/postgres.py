"""A throwaway postgres:15 container, named uiaudit-pg-<pid> on a free 127.0.0.1 port.

Only containers this module created (by that exact name) are ever removed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time

from tests.e2e.ui_audit.env import DB_NAME


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ThrowawayPostgres:
    def __init__(self) -> None:
        self.name = f"uiaudit-pg-{os.getpid()}"
        self.port = free_port()

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://copi:copi@127.0.0.1:{self.port}/{DB_NAME}"

    def start(self) -> None:
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.name, "--label", "uiaudit=1",
             "-e", "POSTGRES_USER=copi", "-e", "POSTGRES_PASSWORD=copi",
             "-e", f"POSTGRES_DB={DB_NAME}", "-p", f"127.0.0.1:{self.port}:5432",
             "postgres:15"],
            check=True, capture_output=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", self.name, "pg_isready", "-U", "copi", "-d", DB_NAME],
                capture_output=True,
            )
            if ready.returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError(f"{self.name} did not become ready")

    def stop(self) -> None:
        subprocess.run(["docker", "stop", self.name], capture_output=True)
