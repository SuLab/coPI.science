"""Static text/structure checks over nginx/nginx.conf — no nginx binary
needed for these. The full `nginx -t` syntax check is a manual verification
step, noted in Task 27.12's Deploy note once all nginx tasks have landed."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _nginx_conf() -> str:
    return (REPO_ROOT / "nginx" / "nginx.conf").read_text()


def _server_blocks(text: str) -> list[str]:
    """Split nginx.conf into individual `server { ... }` blocks by brace depth.
    Used by Tasks 27.10-27.12 to check one vhost at a time."""
    blocks = []
    i = 0
    while True:
        i = text.find("server {", i)
        if i == -1:
            break
        depth, j = 0, i
        while True:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blocks.append(text[i : j + 1])
        i = j + 1
    return blocks


def _https_block(text: str, server_name: str) -> str:
    for block in _server_blocks(text):
        if f"server_name {server_name};" in block and "listen 443 ssl" in block:
            return block
    raise AssertionError(f"no HTTPS server block for {server_name!r}")


def test_no_stale_nextjs_comments():
    text = _nginx_conf()
    assert "Next.js" not in text, "nginx.conf still describes the backend as Next.js"


def test_dead_next_static_cache_block_is_removed():
    text = _nginx_conf()
    assert "/_next/static/" not in text
    assert "proxy_cache_valid" not in text, "proxy_cache_valid with no proxy_cache_path is inert"


def test_devel_and_blackbird_https_vhosts_are_rate_limited():
    text = _nginx_conf()
    for name in ("devel.copi.science", "blackbird.copi.science"):
        block = _https_block(text, name)
        assert "limit_conn conn_perip" in block, f"{name} has no connection cap"
        assert "limit_req zone=req_general" in block, f"{name} has no request-rate cap"


def test_blackbird_https_vhost_has_the_same_tls_hardening_as_the_others():
    block = _https_block(_nginx_conf(), "blackbird.copi.science")
    for directive in ("ssl_ciphers", "ssl_stapling on", "ssl_stapling_verify on", "resolver "):
        assert directive in block, f"blackbird vhost is missing {directive!r}"
