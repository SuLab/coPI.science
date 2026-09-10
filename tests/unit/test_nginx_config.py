"""Static text/structure checks over nginx/nginx.conf — no nginx binary
needed for these. The full `nginx -t` syntax check is a manual verification
step, noted in Task 27.12's Deploy note once all nginx tasks have landed."""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

GRAPH_LOCATION = r"location ~ ^/(cabo-graph|scripps-graph|schultz-alumni-pilot|schultz-group-alumni)$"


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
            assert j < len(text), (
                "_server_blocks: ran off the end of the file looking for the "
                f"closing '}}' of the 'server {{' block starting at offset {i} "
                "— unbalanced braces in nginx.conf"
            )
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

# The templates the app actually serves load scripts from these origins:
# Tailwind's Play CDN (base.html, needs 'unsafe-eval' — it JIT-compiles utility
# classes in the browser) and jsdelivr (cabo_graph.html, agent/dashboard.html,
# admin/discussions.html, admin/discussions_export.html). PostHog is reverse-
# proxied through /ingest (see the `location /ingest/` blocks below), so the
# browser never contacts the posthog hosts directly for scripts.
EXPECTED_SCRIPT_SRC = (
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
    "https://cdn.tailwindcss.com https://cdn.jsdelivr.net"
)

VHOSTS = ("${DOMAIN}", "devel.copi.science", "blackbird.copi.science")


def test_csp_report_only_on_all_three_https_vhosts():
    text = _nginx_conf()
    for name in VHOSTS:
        block = _https_block(text, name)
        assert CSP_RO in block, f"{name} has no {CSP_RO} header"


def _csp_ro_line(block: str) -> str:
    idx = block.index(CSP_RO)
    line_start = block.rindex("\n", 0, idx) + 1
    line_end = block.index("\n", idx)
    return block[line_start:line_end]


CSP_REPORT_URI = "report-uri /api/csp-report"


def test_csp_report_only_policy_content():
    text = _nginx_conf()
    for name in VHOSTS:
        block = _https_block(text, name)
        line = _csp_ro_line(block)
        for expected in (
            "default-src 'self'",
            EXPECTED_SCRIPT_SRC,
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            CSP_REPORT_URI,
            "always",
        ):
            assert expected in line, f"{name}'s {CSP_RO} line is missing {expected!r}"


def test_csp_report_only_lines_are_byte_identical_across_vhosts():
    text = _nginx_conf()
    csp_lines = {_csp_ro_line(_https_block(text, name)) for name in VHOSTS}
    assert len(csp_lines) == 1, f"{CSP_RO} lines differ across vhosts: {csp_lines}"


# The enforcing header (audit 2026-09-08 RC-5, #27 I5). Report-Only alone collected
# nothing usable: it had no report-uri/report-to, so violations went nowhere, and
# nothing was ever actually blocked. Scoped to directives that cannot break rendering
# regardless of what base.html's inline PostHog bootstrap or the Tailwind CDN <script>
# do (frame-ancestors/base-uri/form-action/object-src never affect same-origin script
# or style execution) — default-src/script-src/style-src stay Report-Only only until
# those are audited for nonces/hashes.
CSP_ENFORCING = "Content-Security-Policy"
EXPECTED_ENFORCING_POLICY = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"


def _enforcing_csp_line(block: str) -> str:
    # Find a `Content-Security-Policy` header that is NOT the "-Report-Only" one —
    # `CSP_RO` is a superstring of `CSP_ENFORCING`, so a naive `.index(CSP_ENFORCING)`
    # would just find the Report-Only header's own header name.
    for match in re.finditer(r'add_header\s+["\']?Content-Security-Policy["\']?\s', block):
        line_start = block.rindex("\n", 0, match.start()) + 1
        line_end = block.index("\n", match.start())
        line = block[line_start:line_end]
        if CSP_RO not in line:
            return line
    raise AssertionError("no enforcing Content-Security-Policy header found in block")


def test_csp_enforcing_header_present_on_all_three_https_vhosts():
    text = _nginx_conf()
    for name in VHOSTS:
        block = _https_block(text, name)
        line = _enforcing_csp_line(block)
        assert EXPECTED_ENFORCING_POLICY in line, (
            f"{name}'s enforcing {CSP_ENFORCING} line is missing the expected policy: {line!r}"
        )
        assert "always" in line


def test_csp_enforcing_header_carries_only_the_non_breaking_directives():
    # An enforcing header that ALSO restricted default-src/script-src/style-src would
    # break the Tailwind CDN <script> and the inline PostHog bootstrap immediately —
    # exactly what keeping those Report-Only-only is meant to avoid.
    text = _nginx_conf()
    for name in VHOSTS:
        line = _enforcing_csp_line(_https_block(text, name))
        for must_not_appear in ("default-src", "script-src", "style-src", "connect-src"):
            assert must_not_appear not in line, (
                f"{name}'s enforcing {CSP_ENFORCING} line unexpectedly restricts "
                f"{must_not_appear!r}, which would break rendering: {line!r}"
            )


def test_csp_enforcing_lines_are_byte_identical_across_vhosts():
    text = _nginx_conf()
    lines = {_enforcing_csp_line(_https_block(text, name)) for name in VHOSTS}
    assert len(lines) == 1, f"enforcing {CSP_ENFORCING} lines differ across vhosts: {lines}"


def test_csp_enforcing_header_never_restricts_form_action():
    # Opus review of RC-5 (2026-09-08): Chromium (and Firefox) apply `form-action` to
    # the REDIRECT a form submission's response returns, not just the form's own
    # `action` attribute. The admin Provision button
    # (templates/admin/agent_detail.html:107-129) POSTs to
    # /admin/agents/{id}/slack/provision (src/routers/admin.py:998-1023), which 302s to
    # Slack's OAuth consent screen at https://slack.com/oauth/v2/authorize
    # (src/services/admin_provisioning.py:216) -- a same-origin `form-action 'self'`
    # enforced at the edge would block that redirect and break provisioning outright.
    # `form-action` stays in the Report-Only header (still collects violation data);
    # the enforcing header must never carry it.
    text = _nginx_conf()
    for name in VHOSTS:
        line = _enforcing_csp_line(_https_block(text, name))
        assert "form-action" not in line, (
            f"{name}'s enforcing {CSP_ENFORCING} line restricts form-action, which "
            "would break the admin Slack-provisioning POST-then-redirect flow"
        )


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


def test_provisioning_location_regex_matches_the_real_route_path():
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


def test_blackbird_vhost_has_req_graph_limit_for_collaboration_graph_routes():
    # Minor follow-up to #27 I5: blackbird serves the same collaboration-graph
    # routes as the primary vhost (via its own blackbird_app upstream), so it
    # needs the same req_graph rate limit — not just the general one.
    block = _https_block(_nginx_conf(), "blackbird.copi.science")
    assert GRAPH_LOCATION in block, "blackbird vhost has no collaboration-graph location block"
    loc_idx = block.index(GRAPH_LOCATION)
    loc_block = block[loc_idx : block.index("}", loc_idx) + 1]
    assert "limit_req zone=req_graph" in loc_block
    assert "proxy_pass http://blackbird_app;" in loc_block


# #27 Minor 16: req_general/req_graph/conn_perip were single zones shared across
# all three vhosts, so one IP's burst against devel or blackbird consumed the
# primary site's budget (and vice versa). Each vhost must get its own zone.
# SEC2-4 (audit 2026-09-08): /api/csp-report is public and unauthenticated
# (like the collaboration-graph routes above) but had no rate limit tighter
# than the whole-site general zone, big enough that a burst of forged reports
# could still fill the request pool ahead of it.
def test_csp_report_zone_is_declared():
    text = _nginx_conf()
    assert re.search(
        r"limit_req_zone\s+\$binary_remote_addr\s+zone=\S*csp_report\S*:\d+[kmg]\s+rate=\S+;",
        text,
    ), "expected a dedicated limit_req_zone for /api/csp-report"


def test_csp_report_location_present_and_rate_limited_on_all_three_vhosts():
    text = _nginx_conf()
    for name in VHOSTS:
        block = _https_block(text, name)
        assert "location = /api/csp-report {" in block, (
            f"{name} has no dedicated /api/csp-report location"
        )
        loc_idx = block.index("location = /api/csp-report {")
        loc_block = block[loc_idx : block.index("}", loc_idx) + 1]
        assert re.search(r"limit_req zone=\S*csp_report\S*", loc_block), (
            f"{name}'s /api/csp-report location does not use the dedicated zone"
        )


def test_csp_report_location_proxy_settings_match_general_location():
    # "Keep proxy settings identical to location /" (SEC2-4) -- same upstream,
    # headers and timeouts as the vhost's own general reverse proxy.
    text = _nginx_conf()
    for name in VHOSTS:
        block = _https_block(text, name)
        general_idx = block.index("location / {")
        general_block = block[general_idx : block.index("}", general_idx) + 1]
        csp_idx = block.index("location = /api/csp-report {")
        csp_block = block[csp_idx : block.index("}", csp_idx) + 1]
        for line in (
            "proxy_pass",
            "proxy_http_version",
            "proxy_set_header Host",
            "proxy_set_header X-Real-IP",
            "proxy_set_header X-Forwarded-For",
            "proxy_set_header X-Forwarded-Proto",
            "proxy_set_header X-Forwarded-Host",
            "proxy_connect_timeout",
            "proxy_send_timeout",
            "proxy_read_timeout",
        ):
            general_line = next(
                (ln.strip() for ln in general_block.splitlines() if ln.strip().startswith(line)),
                None,
            )
            csp_line = next(
                (ln.strip() for ln in csp_block.splitlines() if ln.strip().startswith(line)),
                None,
            )
            assert general_line is not None, f"{name}'s general location is missing {line!r}"
            assert general_line == csp_line, (
                f"{name}'s csp-report location diverges from location / for {line!r}: "
                f"{csp_line!r} != {general_line!r}"
            )


def test_rate_limit_zones_are_declared_per_vhost_not_shared():
    text = _nginx_conf()
    general_zones = set(re.findall(r"limit_req_zone\s+\$binary_remote_addr\s+zone=(req_general\S*?):", text))
    graph_zones = set(re.findall(r"limit_req_zone\s+\$binary_remote_addr\s+zone=(req_graph\S*?):", text))
    conn_zones = set(re.findall(r"limit_conn_zone\s+\$binary_remote_addr\s+zone=(conn_perip\S*?):", text))
    assert len(general_zones) == 3, f"expected 3 distinct req_general zones (one per vhost): {general_zones}"
    assert len(conn_zones) == 3, f"expected 3 distinct conn_perip zones (one per vhost): {conn_zones}"
    # devel serves no collaboration-graph routes; ${DOMAIN} and blackbird do.
    assert len(graph_zones) == 2, f"expected 2 distinct req_graph zones (main + blackbird): {graph_zones}"


def test_each_vhost_uses_only_its_own_rate_limit_zones():
    text = _nginx_conf()
    for name in VHOSTS:
        block = _https_block(text, name)
        used_general = set(re.findall(r"limit_req zone=(req_general\S*?)\s", block))
        used_conn = set(re.findall(r"limit_conn (conn_perip\S*?)\s", block))
        assert len(used_general) == 1, f"{name} must use exactly one req_general zone: {used_general}"
        assert len(used_conn) == 1, f"{name} must use exactly one conn_perip zone: {used_conn}"

    devel_general = set(
        re.findall(r"limit_req zone=(req_general\S*?)\s", _https_block(text, "devel.copi.science"))
    )
    main_general = set(
        re.findall(r"limit_req zone=(req_general\S*?)\s", _https_block(text, "${DOMAIN}"))
    )
    blackbird_general = set(
        re.findall(r"limit_req zone=(req_general\S*?)\s", _https_block(text, "blackbird.copi.science"))
    )
    assert devel_general != main_general
    assert devel_general != blackbird_general
    assert main_general != blackbird_general


# ---------------------------------------------------------------------------
# Shared-memory arithmetic (#27 I5, over-implementation R9)
#
# `88d1b25` sized nginx's cgroup against "40 MiB of shared zones (three
# limit_*_zone 10m + ssl_session_cache)"; `1a430c0` then split those three
# zones into eight, one set per vhost, and revisited neither the cap nor the
# comment. Both files ended up carrying a stale total — docker-compose.prod.yml
# said 40 MiB and nginx.conf's own header said "8x10m = 80m", which omits the
# SSL zone entirely. The tests below DERIVE the total from the declarations
# instead of restating it, so no future zone edit can leave a figure behind.
# ---------------------------------------------------------------------------

COMPOSE_PROD = REPO_ROOT / "docker-compose.prod.yml"

# The one machine-readable line in docker-compose.prod.yml's nginx comment.
# `zones` is deliberately symbolic: it is summed from nginx.conf here, because
# a number copied into the file that does not declare the zones is exactly what
# went stale twice.
BUDGET_RE = re.compile(
    r"nginx-mem-budget:\s*zones\s*\+\s*(\d+)\s*workers\s*x\s*([\d.]+)\s*MiB"
    r"\s*\+\s*(\d+)\s*MiB\s*headroom"
)

_SIZE_MULTIPLIER = {"k": 1 / 1024, "m": 1.0, "g": 1024.0}


def _declared_shm_zones(text: str) -> dict[str, float]:
    """Every shared-memory segment nginx.conf asks the master to allocate, in MiB.

    Keyed by zone NAME, because nginx allocates one segment per name: all three
    vhosts declare `ssl_session_cache shared:SSL:10m`, and that is one 10 MiB
    segment, not three. The rate-limit zones are the opposite case — since
    `1a430c0` each vhost declares its own name, so each is its own segment.
    """
    zones: dict[str, float] = {}
    for name, size, unit in re.findall(
        r"limit_(?:req|conn)_zone\s+\S+\s+zone=(\w+):(\d+)([kmg])", text
    ):
        zones[name] = int(size) * _SIZE_MULTIPLIER[unit.lower()]
    for name, size, unit in re.findall(r"ssl_session_cache\s+shared:(\w+):(\d+)([kmg])", text):
        zones[name] = int(size) * _SIZE_MULTIPLIER[unit.lower()]
    return zones


def _nginx_mem_limit_mib() -> int:
    limit = yaml.safe_load(COMPOSE_PROD.read_text())["services"]["nginx"]["mem_limit"]
    return int(limit[:-1]) * (1024 if limit[-1] in "gG" else 1)


def test_declared_shared_memory_zones_fit_inside_the_nginx_memory_limit():
    zones = _declared_shm_zones(_nginx_conf())
    compose = COMPOSE_PROD.read_text()

    budget = BUDGET_RE.search(compose)
    assert budget, (
        "docker-compose.prod.yml's nginx service must carry a machine-readable "
        "'nginx-mem-budget: zones + N workers x X MiB + Y MiB headroom' line, so "
        "mem_limit can be checked against nginx.conf's actual declarations"
    )
    workers, per_worker, headroom = (
        int(budget.group(1)), float(budget.group(2)), int(budget.group(3))
    )

    zone_total = sum(zones.values())
    required = zone_total + workers * per_worker + headroom
    limit = _nginx_mem_limit_mib()
    assert required <= limit, (
        f"nginx.conf declares {zone_total:g} MiB of shared memory "
        + "(" + ", ".join(f"{n}:{s:g}m" for n, s in sorted(zones.items())) + ") "
        + f"+ {workers} workers x {per_worker:g} MiB + {headroom} MiB headroom "
        f"= {required:g} MiB, into mem_limit: {limit}m"
    )


def test_the_zone_total_stated_in_nginx_conf_matches_what_it_declares():
    text = _nginx_conf()
    stated = re.search(r"=\s*(\d+)m of shared memory", text)
    assert stated, "nginx.conf's zone header must state the total it declares"
    total = sum(_declared_shm_zones(text).values())
    assert float(stated.group(1)) == total, (
        f"nginx.conf's comment claims {stated.group(1)}m of shared memory, but its "
        f"declarations sum to {total:g}m"
    )


def test_compose_does_not_restate_a_zone_total_it_does_not_declare():
    # docker-compose.prod.yml carried "40 MiB of shared zones (three
    # limit_*_zone 10m + ssl_session_cache shared:SSL:10m)" for as long as
    # nginx.conf declared eight zones. The budget line must name `zones`
    # symbolically and let this file sum them.
    compose = COMPOSE_PROD.read_text()
    stale = re.search(r"\d+\s*MiB of shared zones", compose)
    assert not stale, (
        f"docker-compose.prod.yml restates nginx.conf's zone total ({stale.group(0)!r}); "
        "it goes stale the next time a zone is added, split or resized"
    )
