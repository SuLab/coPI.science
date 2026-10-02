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
        #: True only once THIS object's `docker run` succeeded; stop() never touches a
        #: same-named container another run owns.
        self.started = False

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://copi:copi@127.0.0.1:{self.port}/{DB_NAME}"

    def start(self) -> None:
        subprocess.run(
            # --shm-size: Docker's 64 MB /dev/shm default can crash a Postgres backend
            # under the crawl's parallel load ("database system is in recovery mode").
            ["docker", "run", "-d", "--rm", "--name", self.name, "--label", "uiaudit=1",
             "--shm-size=256m",
             "-e", "POSTGRES_USER=copi", "-e", "POSTGRES_PASSWORD=copi",
             "-e", f"POSTGRES_DB={DB_NAME}", "-p", f"127.0.0.1:{self.port}:5432",
             "postgres:15"],
            check=True, capture_output=True,
        )
        self.started = True
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                # Over TCP: the image's temporary init server answers the Unix
                # socket before the real server listens on 5432.
                ["docker", "exec", self.name, "pg_isready", "-h", "127.0.0.1", "-U", "copi",
                 "-d", DB_NAME],
                capture_output=True,
            )
            if ready.returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError(f"{self.name} did not become ready")

    def stop(self) -> None:
        if self.started:
            subprocess.run(["docker", "stop", self.name], capture_output=True)
            self.started = False
