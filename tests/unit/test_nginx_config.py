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


CSP_RO = "Content-Security-Policy-Report-Only"


def test_csp_report_only_on_all_three_https_vhosts():
    text = _nginx_conf()
    for name in ("${DOMAIN}", "devel.copi.science", "blackbird.copi.science"):
        block = _https_block(text, name)
        assert CSP_RO in block, f"{name} has no {CSP_RO} header"


def test_no_enforcing_csp_added_at_the_nginx_layer():
    # Report-Only only, by decision — an enforcing CSP from nginx would break
    # base.html's inline PostHog bootstrap and the Tailwind CDN <script>
    # before those are audited (#27 I5).
    text = _nginx_conf()
    assert 'add_header Content-Security-Policy "' not in text


def test_general_timeout_stays_120s():
    # The general reverse-proxy location (and the graph-routes location) must
    # keep the original 120s timeout — Task 27.12 only lengthens the timeout
    # for the Slack-provisioning route, not the whole site (#27 I5-b).
    block = _https_block(_nginx_conf(), "${DOMAIN}")
    general_idx = block.index("location / {")
    general_block = block[general_idx : block.index("}", general_idx) + 1]
    assert "proxy_read_timeout 120s" in general_block
    assert "proxy_send_timeout 120s" in general_block


def test_provisioning_route_gets_a_longer_read_timeout():
    block = _https_block(_nginx_conf(), "${DOMAIN}")
    loc_idx = block.index(r"location ~ ^/admin/agents/")
    loc_block = block[loc_idx : block.index("}", loc_idx) + 1]
    assert "proxy_read_timeout 300s" in loc_block
    assert "proxy_send_timeout 300s" in loc_block
    assert loc_idx < block.index("location / {"), (
        "must precede the catch-all so it isn't shadowed for this path"
    )


def test_provisioning_location_regex_matches_the_real_route_path():
    import re

    block = _https_block(_nginx_conf(), "${DOMAIN}")
    start = block.index("location ~ ^/admin/agents/")
    pattern = block[start:].split("location ~ ", 1)[1].split(" {", 1)[0]
    # src/routers/admin.py:973 -> @router.post("/agents/{agent_id}/slack/provision")
    assert re.search(pattern, "/admin/agents/0a1b2c3d-4e5f-6789-abcd-ef0123456789/slack/provision")


def test_provisioning_location_inherits_same_proxy_headers_and_upstream_as_general():
    block = _https_block(_nginx_conf(), "${DOMAIN}")
    loc_idx = block.index(r"location ~ ^/admin/agents/")
    loc_block = block[loc_idx : block.index("}", loc_idx) + 1]
    assert "proxy_pass http://app;" in loc_block
    for header in (
        "proxy_set_header Host $host;",
        "proxy_set_header X-Real-IP $remote_addr;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_set_header X-Forwarded-Host $host;",
    ):
        assert header in loc_block, f"provisioning location is missing {header!r}"
