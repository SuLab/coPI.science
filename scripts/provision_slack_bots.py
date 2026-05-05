#!/usr/bin/env python3
"""
Provision Slack apps for all LabBots that don't yet have a bot token.

How it works
------------
1. Reads PILOT_LABS to find bots without SLACK_BOT_TOKEN_<ID> in .env
2. Creates a Slack app for each via the Manifest API (apps.manifest.create)
3. Starts a local OAuth callback server on --port (default 8888)
4. Prints authorize URLs — a workspace admin clicks each one in a browser
5. Each click redirects back here; the code is exchanged for an xoxb- token
6. Tokens are appended to .env as SLACK_BOT_TOKEN_<AGENT_ID>

Prerequisites (one-time, done by a workspace admin in a browser)
-----------------------------------------------------------------
  1. Go to https://api.slack.com/apps
  2. Click "Your App Configuration Tokens" → "Generate Token" for your workspace
  3. Copy both the token (xoxe-...) and the refresh token
  4. Add to .env:
       SLACK_CONFIG_TOKEN=xoxe-...
       SLACK_CONFIG_REFRESH_TOKEN=xoxe-...

Usage
-----
  # From project root:
  python scripts/provision_slack_bots.py

  # Custom port or env file:
  python scripts/provision_slack_bots.py --port 9000 --env-file .env

  # Preview what would be created without calling any APIs:
  python scripts/provision_slack_bots.py --dry-run

  # Re-run the OAuth step without recreating apps (useful if the server was
  # interrupted midway — re-uses credentials saved in .provision_state.json):
  python scripts/provision_slack_bots.py --skip-create
"""

import argparse
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from dotenv import dotenv_values, set_key
from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLACK_API = "https://slack.com/api"
CALLBACK_PATH = "/oauth/callback"
STATE_FILE = Path(".provision_state.json")

# All scopes the bots actually use — derived from AgentSlackClient + routers/podcast
BOT_SCOPES = [
    "channels:history",   # conversations.history / conversations.replies
    "channels:join",      # conversations.join
    "channels:manage",    # conversations.create
    "channels:read",      # conversations.list
    "chat:write",         # chat.postMessage
    "groups:history",     # threads in private channels
    "groups:read",        # conversations.list private
    "im:history",         # poll_dm_messages
    "im:write",           # conversations.open (DMs)
    "users:read",         # users.info
    "users:read.email",   # users.lookupByEmail
]

console = Console()


# ---------------------------------------------------------------------------
# Parse PILOT_LABS from source without importing the module
# (avoids pulling in SQLAlchemy and other heavy dependencies)
# ---------------------------------------------------------------------------

def load_pilot_labs() -> list[dict]:
    import ast
    src = Path(__file__).parent.parent / "src" / "agent" / "simulation.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "PILOT_LABS"
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError("PILOT_LABS not found in src/agent/simulation.py")


# ---------------------------------------------------------------------------
# Slack API helpers
# ---------------------------------------------------------------------------

def lookup_team_id(existing_env: dict) -> str | None:
    """Call auth.test on the first valid bot token to get the workspace team_id."""
    for key, val in existing_env.items():
        if (
            key.upper().startswith("SLACK_BOT_TOKEN_")
            and val
            and val.startswith("xoxb-")
            and not val.startswith("xoxb-placeholder")
        ):
            resp = httpx.post(
                f"{SLACK_API}/auth.test",
                headers={"Authorization": f"Bearer {val}"},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                return data.get("team_id")
    return None


def rotate_config_token(refresh_token: str) -> tuple[str, str]:
    """Rotate the app-config token. Returns (new_access_token, new_refresh_token)."""
    resp = httpx.post(
        f"{SLACK_API}/tooling.tokens.rotate",
        data={"refresh_token": refresh_token},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"tooling.tokens.rotate failed: {data.get('error')}")
    return data["token"], data["refresh_token"]


def create_app(
    config_token: str,
    agent_id: str,
    bot_name: str,
    pi_name: str,
    redirect_uri: str,
    max_rate_limit_retries: int = 5,
) -> dict:
    """
    Create one Slack app via the Manifest API.
    Returns a dict with app_id, client_id, client_secret, oauth_url.
    Retries on rate-limit responses only; all other errors raise immediately.
    """
    manifest = {
        "display_information": {
            "name": bot_name,
            "description": f"LabBot agent for {pi_name}",
        },
        "features": {
            "bot_user": {
                "display_name": bot_name,
                "always_online": False,
            }
        },
        "oauth_config": {
            "redirect_urls": [redirect_uri],
            "scopes": {"bot": BOT_SCOPES},
        },
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }
    for attempt in range(max_rate_limit_retries):
        resp = httpx.post(
            f"{SLACK_API}/apps.manifest.create",
            headers={"Authorization": f"Bearer {config_token}"},
            json={"manifest": manifest},
            timeout=20,
        )
        data = resp.json()
        if data.get("ok"):
            creds = data["credentials"]
            return {
                "agent_id": agent_id,
                "bot_name": bot_name,
                "pi_name": pi_name,
                "app_id": data["app_id"],
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "oauth_url": data["oauth_authorize_url"],
            }
        if data.get("error") == "ratelimited":
            wait = int(data.get("retry_after", 0) or resp.headers.get("Retry-After", 60))
            console.print(f"  [yellow]rate limited — waiting {wait}s before retrying {bot_name}…[/yellow]")
            time.sleep(wait)
        else:
            detail = data.get("errors") or data.get("error", "unknown")
            raise RuntimeError(f"apps.manifest.create failed: {detail}")
    raise RuntimeError(f"apps.manifest.create: still rate-limited after {max_rate_limit_retries} retries")


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> str:
    """Exchange a temporary OAuth code for a bot token. Returns xoxb-... string."""
    resp = httpx.post(
        f"{SLACK_API}/oauth.v2.access",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"oauth.v2.access failed: {data.get('error')}")
    token = data.get("access_token", "")
    if not token.startswith("xoxb-"):
        raise RuntimeError(f"Unexpected token format: {token[:20]}...")
    return token


# ---------------------------------------------------------------------------
# OAuth callback HTTP server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """
    Handles GET /oauth/callback?code=...&state=<agent_id>
    Exchanges the code for a token and writes it to .env.
    """

    # Shared state injected before server starts
    pending: dict = {}       # agent_id -> {bot_name, client_id, client_secret}
    received: dict = {}      # agent_id -> xoxb-token
    env_file: str = ".env"
    redirect_uri: str = ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._html(404, "<h2>404 Not found</h2>")
            return

        params = dict(urllib.parse.parse_qsl(parsed.query))
        code = params.get("code")
        error = params.get("error")
        agent_id = params.get("state")

        if error:
            self._html(400, f"<h2>Slack returned an error: {error}</h2>")
            return

        if not code or not agent_id:
            self._html(400, "<h2>Missing code or state parameter</h2>")
            return

        info = self.pending.get(agent_id)
        if not info:
            self._html(400, f"<h2>Unknown agent_id in state: {agent_id!r}</h2>")
            return

        if agent_id in self.received:
            self._html(200, f"<h2>{info['bot_name']} already installed — duplicate callback ignored.</h2>")
            return

        try:
            token = exchange_code(
                info["client_id"], info["client_secret"], code, self.redirect_uri
            )
        except Exception as exc:
            console.print(f"[red]Token exchange failed for {agent_id}: {exc}[/red]")
            self._html(500, f"<h2>Token exchange failed: {exc}</h2>")
            return

        env_key = f"SLACK_BOT_TOKEN_{agent_id.upper()}"
        set_key(self.env_file, env_key, token, quote_mode="never")
        self.received[agent_id] = token

        remaining = len(self.pending) - len(self.received)
        console.print(f"[green]✓[/green] [bold]{info['bot_name']}[/bold] → {env_key}")
        self._html(200, f"""
            <h2 style="color:green">✅ {info['bot_name']} installed!</h2>
            <p>Token written to .env as <code>{env_key}</code></p>
            <p><b>{remaining}</b> bot(s) remaining. You may close this tab.</p>
        """)

    def _html(self, code: int, body: str):
        content = (
            "<html><body style='font-family:sans-serif;padding:2em;max-width:600px'>"
            + body
            + "</body></html>"
        ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass  # suppress default access log noise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--port", type=int, default=8888,
        help="Local port for the OAuth callback server (default: 8888)",
    )
    parser.add_argument(
        "--env-file", default=".env",
        help="Path to the .env file that will receive the new tokens (default: .env)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show which bots need tokens; make no API calls",
    )
    parser.add_argument(
        "--skip-create", action="store_true",
        help=f"Skip app creation and reuse credentials from {STATE_FILE}",
    )
    parser.add_argument(
        "--team-id",
        help="Slack workspace team ID (e.g. T012AB3CD) to pin OAuth URLs to the right workspace. "
             "Auto-detected from an existing bot token if not provided.",
    )
    args = parser.parse_args()

    redirect_uri = f"http://localhost:{args.port}{CALLBACK_PATH}"

    # -----------------------------------------------------------------------
    # 1. Determine which bots are missing tokens
    # -----------------------------------------------------------------------
    pilot_labs = load_pilot_labs()
    existing_env = dotenv_values(args.env_file)

    team_id = args.team_id
    if not team_id and not args.dry_run:
        team_id = lookup_team_id(existing_env)
        if team_id:
            console.print(f"Detected workspace team ID: [cyan]{team_id}[/cyan]")
        else:
            console.print("[yellow]Could not detect team ID — OAuth links may open the wrong workspace.[/yellow]")
            console.print("  Pass --team-id T... to fix this.")

    tokenized = {
        k[len("SLACK_BOT_TOKEN_"):].lower()
        for k, v in existing_env.items()
        if k.upper().startswith("SLACK_BOT_TOKEN_")
        and v
        and not v.startswith("xoxb-placeholder")
    }

    missing = [lab for lab in pilot_labs if lab["id"] not in tokenized]

    if not missing:
        console.print("[green]All bots already have tokens. Nothing to do.[/green]")
        return

    t = Table(title=f"{len(missing)} bot(s) need Slack tokens", show_lines=True)
    t.add_column("agent_id", style="cyan")
    t.add_column("Bot name")
    t.add_column("PI")
    for lab in missing:
        t.add_row(lab["id"], lab["name"], lab["pi"])
    console.print(t)

    if args.dry_run:
        console.print("[yellow]--dry-run active: no API calls made.[/yellow]")
        return

    # -----------------------------------------------------------------------
    # 2. Obtain / rotate config token
    # -----------------------------------------------------------------------
    config_token = existing_env.get("SLACK_CONFIG_TOKEN", "").strip()
    refresh_token = existing_env.get("SLACK_CONFIG_REFRESH_TOKEN", "").strip()

    if not config_token:
        console.print("\n[bold red]SLACK_CONFIG_TOKEN is not set in .env[/bold red]")
        console.print(
            "  1. Open https://api.slack.com/apps in a browser\n"
            "  2. Click 'Your App Configuration Tokens'\n"
            "  3. Click 'Generate Token' for your workspace\n"
            "  4. Copy the token (xoxe-...) and refresh token into .env:\n"
            "       SLACK_CONFIG_TOKEN=xoxe-...\n"
            "       SLACK_CONFIG_REFRESH_TOKEN=xoxe-...\n"
        )
        sys.exit(1)

    if refresh_token:
        console.print("Rotating config token...")
        try:
            config_token, new_refresh = rotate_config_token(refresh_token)
            set_key(args.env_file, "SLACK_CONFIG_TOKEN", config_token, quote_mode="never")
            set_key(args.env_file, "SLACK_CONFIG_REFRESH_TOKEN", new_refresh, quote_mode="never")
            console.print("[green]Config token rotated and saved.[/green]")
        except Exception as exc:
            console.print(f"[yellow]Token rotation failed ({exc}); using existing token.[/yellow]")

    # -----------------------------------------------------------------------
    # 3. Start OAuth callback server (before app creation so URLs work immediately)
    # -----------------------------------------------------------------------
    _CallbackHandler.pending = {}
    _CallbackHandler.received = {}
    _CallbackHandler.env_file = args.env_file
    _CallbackHandler.redirect_uri = redirect_uri

    server = HTTPServer(("localhost", args.port), _CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    console.print(f"\n[bold]OAuth callback server running on http://localhost:{args.port}[/bold]")
    console.print(
        "\n[bold yellow]Open each URL in a browser while signed into the workspace.[/bold yellow]\n"
        "Each approval redirects back here and saves the token to .env automatically.\n"
    )

    # -----------------------------------------------------------------------
    # 4. Create apps (or load previous run's state) and print URLs as they appear
    # -----------------------------------------------------------------------
    def _oauth_url(app: dict) -> str:
        extra = {"state": app["agent_id"], "redirect_uri": redirect_uri}
        if team_id:
            extra["team"] = team_id
        return app["oauth_url"] + "&" + urllib.parse.urlencode(extra)

    created: list[dict] = []
    if args.skip_create:
        if not STATE_FILE.exists():
            console.print(f"[red]--skip-create: {STATE_FILE} not found. Run without that flag first.[/red]")
            server.shutdown()
            sys.exit(1)
        all_state: list[dict] = json.loads(STATE_FILE.read_text())
        missing_ids = {lab["id"] for lab in missing}
        created = [a for a in all_state if a["agent_id"] in missing_ids]
        console.print(f"Loaded {len(created)} app credential(s) from {STATE_FILE}\n")
        for i, app in enumerate(created, 1):
            _CallbackHandler.pending[app["agent_id"]] = {
                "bot_name": app["bot_name"],
                "client_id": app["client_id"],
                "client_secret": app["client_secret"],
            }
            console.print(f"  [cyan]{i:2d}.[/cyan] [bold]{app['bot_name']}[/bold] ({app['pi_name']})")
            console.print(f"      {_oauth_url(app)}\n")
    else:
        failed_count = 0
        for i, lab in enumerate(missing):
            try:
                app = create_app(config_token, lab["id"], lab["name"], lab["pi"], redirect_uri)
                created.append(app)
                _CallbackHandler.pending[app["agent_id"]] = {
                    "bot_name": app["bot_name"],
                    "client_id": app["client_id"],
                    "client_secret": app["client_secret"],
                }
                console.print(f"  [green]{i+1:2d}.[/green] [bold]{app['bot_name']}[/bold] (app {app['app_id']})")
                console.print(f"      {_oauth_url(app)}\n")
            except Exception as exc:
                console.print(f"  [red]failed[/red]  {lab['name']}: {exc}")
                failed_count += 1
            # Slack's Manifest API allows ~10 req/min; 12s between calls stays well under
            if i < len(missing) - 1:
                time.sleep(12)

        if created:
            STATE_FILE.write_text(json.dumps(created, indent=2))
        if failed_count:
            console.print(f"[yellow]{failed_count} app(s) failed to create — fix errors and re-run.[/yellow]")

    if not created:
        console.print("[red]No apps available for OAuth. Exiting.[/red]")
        server.shutdown()
        sys.exit(1)

    # -----------------------------------------------------------------------
    # 5. Wait for all OAuth callbacks
    # -----------------------------------------------------------------------
    console.print(f"Waiting for {len(created)} installation(s)…  (Ctrl-C to stop early)\n")
    try:
        while len(_CallbackHandler.received) < len(created):
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    finally:
        server.shutdown()

    done = len(_CallbackHandler.received)
    total = len(created)
    console.print(f"\n[bold]Finished: {done}/{total} token(s) saved to {args.env_file}[/bold]")

    if done < total:
        outstanding = [a["bot_name"] for a in created if a["agent_id"] not in _CallbackHandler.received]
        console.print(f"[yellow]Still missing: {', '.join(outstanding)}[/yellow]")
        console.print(f"Re-run with [bold]--skip-create[/bold] to retry without recreating the apps.")
    else:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        console.print(f"[green]All done! Restart the agent container to pick up the new tokens.[/green]")
        console.print("  docker rm -f agent-run")
        console.print("  docker compose up -d --build app worker")
        console.print("  docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0")


if __name__ == "__main__":
    main()
