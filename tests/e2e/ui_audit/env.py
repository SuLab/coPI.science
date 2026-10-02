"""Process isolation for the harness.

``src.config`` reads ``.env`` from the CURRENT WORKING DIRECTORY, and the host's
checkout holds production secrets there. Every harness process therefore runs in a
temporary directory that contains symlinks to the code-side directories the app
resolves relative to its cwd (templates, static, prompts, profiles, alembic) and no
``.env`` at all; the settings it needs come from explicit environment variables,
which pydantic-settings prefers over a dotenv file anyway.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Relative paths the app READS from its cwd (Jinja2Templates("templates"),
#: StaticFiles("static"), the prompt files, alembic.ini). Never "profiles": the web app
#: WRITES and DELETES there (src/services/profile_export.py PROFILES_DIR,
#: src/services/user_deletion.py _PUBLIC_DIR/_MEMORY_DIR), and the repo's profiles/ is the
#: live agent's mounted directory. The harness gets its own empty profiles/ instead.
LINKED = ("templates", "static", "prompts", "alembic", "alembic.ini")

#: The throwaway database's name; seed.py refuses any other.
DB_NAME = "copi_uiaudit"


def isolated_workdir() -> Path:
    """A fresh temp directory with symlinks to the code-side paths and no .env."""
    work = Path(tempfile.mkdtemp(prefix="uiaudit-"))
    for name in LINKED:
        target = REPO_ROOT / name
        if target.exists():
            (work / name).symlink_to(target)
    # A private, empty profiles tree: profile exports and account deletions in a journey
    # or a manual `serve` session land here, never in the live agent's files.
    (work / "profiles" / "public").mkdir(parents=True)
    (work / "profiles" / "memory").mkdir(parents=True)
    assert not (work / ".env").exists()
    return work


def harness_env(*, database_url: str, base_url: str, secret_key: str) -> dict[str, str]:
    """Environment for every harness subprocess. Starts from a minimal copy of the
    caller's environment (PATH, HOME, locale, Docker/Playwright paths), never the
    whole thing, so no production credential exported in a shell leaks in."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "DOCKER_HOST",
            "PLAYWRIGHT_BROWSERS_PATH", "XDG_RUNTIME_DIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(
        PYTHONPATH=str(REPO_ROOT),
        DATABASE_URL=database_url,
        BASE_URL=base_url,
        ENVIRONMENT="development",
        ALLOW_HTTP_SESSIONS="true",
        SECRET_KEY=secret_key,
        ANTHROPIC_API_KEY="",
        # Every send is suppressed: the allowlist names one undeliverable address.
        OUTBOUND_EMAIL_ALLOWLIST="nobody@invalid.example",
        # boto3 must not find the host's instance-role credentials.
        AWS_EC2_METADATA_DISABLED="true",
        AWS_ACCESS_KEY_ID="uiaudit-invalid",
        AWS_SECRET_ACCESS_KEY="uiaudit-invalid",
        AWS_DEFAULT_REGION="us-east-1",
    )
    return env


def new_secret() -> str:
    return "uiaudit-" + secrets.token_hex(16)
