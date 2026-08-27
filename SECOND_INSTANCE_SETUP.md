# Deploying the Blackbird instance (2nd, isolated CoPI stack)

Runbook for standing up a **second, fully isolated** CoPI/LabAgent deployment
alongside the existing production stack, on the **same host**, sharing the
existing nginx TLS edge.

- **This clone:** `/home/ubuntu/blackbird-copi-science` (org2 / "blackbird")
- **Existing prod clone:** `/home/ubuntu/copi-python` (org1, DO NOT disrupt)
- **Subdomain:** `blackbird.copi.science` (subdomain of the existing domain)
- **Compose project name:** `copi-blackbird` (set via `.env`)
- **Email sender:** reused — `noreply@copi.science` (same SES identity / instance IAM role)

## How isolation works (mental model)

Docker Compose namespaces containers, the default network, **and named volumes**
by project name. Because this stack sets `COMPOSE_PROJECT_NAME=copi-blackbird`,
it gets its own `copi-blackbird_postgres`, `copi-blackbird-app-1`, its own
`copi-blackbird_pgdata` volume, and its own network — **zero data shared** with
org1. The **only** true host-level collision is nginx binding `:80/:443`, so
this stack runs **without its own nginx/certbot** and reuses org1's edge.

---

## ⚠️ Host-specific gotchas (read first)

1. **`docker compose` may be broken for the *existing* project** on this host
   (invalid-project error). Run *this* stack from *this* directory (it's a fresh
   project, so it should work), and touch the *existing* stack only via plain
   `docker` / `docker exec` against container names (e.g. `copi-python-nginx-1`).
2. **NEVER use `--remove-orphans`** — it has previously killed the prod nginx and
   certbot containers.
3. `.env` changes require `docker compose ... up -d --force-recreate` (not
   `restart`) to take effect. Agent **code** changes need a container restart.
4. Do not stop/recreate `copi-python-*` containers. All commands below that touch
   the existing stack are additive (a live `network connect` + an nginx reload).

---

## Step 0 — Prerequisites / assumptions

- [ ] `git` clone already present at `/home/ubuntu/blackbird-copi-science` (done).
- [ ] `.env` already moved into this folder (done) — Step 2 finalizes it.
- [ ] DNS: an `A` record for `blackbird.copi.science` pointing at the **same
      public IP** as `copi.science` (the host's EC2 public IP). Create this
      early — cert issuance in Step 6 needs it resolving.
- [ ] The EC2 **instance IAM role** grants SES send for `noreply@copi.science`
      and S3 access (reused as-is). **Note:** its CloudWatch permission is scoped
      to org1's existing log groups only — it CANNOT create new ones, so blackbird
      uses the local `json-file` log driver instead (see Step 3a).

---

## Step 1 — Put this clone on its own branch

The second org should track its own branch, not `main`.

```bash
cd /home/ubuntu/blackbird-copi-science
git checkout -b blackbird        # or track an existing remote branch if one exists
git branch --show-current        # -> blackbird
```

---

## Step 2 — Finalize `.env`

The `.env` here was seeded from a template and still has placeholders. Edit it:

- [ ] `COMPOSE_PROJECT_NAME=copi-blackbird`  (template shipped `copi-org2` — change it)
- [ ] `DOMAIN=blackbird.copi.science`
- [ ] `BASE_URL=https://blackbird.copi.science`
- [ ] `ORCID_REDIRECT_URI=https://blackbird.copi.science/auth/callback`
- [ ] `ANTHROPIC_API_KEY=` — reuse org1's key or a new one
- [ ] `NCBI_API_KEY=` — reuse or new
- [ ] `ORCID_CLIENT_ID` / `ORCID_CLIENT_SECRET` — reuse org1's ORCID app **and add
      the redirect URI above to it**, or register a new ORCID app
- [ ] `SLACK_CONFIG_TOKEN` / `SLACK_CONFIG_REFRESH_TOKEN` — the new Slack
      workspace's config-token pair (needed only for provisioning bots via the
      Manifest API; rotated pair then lives in the `app_settings` DB table)
- [ ] `POSTHOG_API_KEY=` — reuse, new project, or leave blank to disable

**Already set for you (do not reuse across orgs):** `SECRET_KEY` and
`POSTGRES_PASSWORD` were freshly generated; `DATABASE_URL` already embeds the new
password; `SES_SENDER_EMAIL=noreply@copi.science` and `AWS_REGION=us-east-2` are
reused as intended; `ENVIRONMENT=production`, `ALLOW_HTTP_SESSIONS=false`.

> Note: you do **not** need the ~125 `SLACK_BOT_TOKEN_*` fields. Per `CLAUDE.md`,
> `AgentRegistry.slack_bot_token` in the DB is authoritative — agents are
> onboarded via the admin UI / self-service signup after the stack is up.

Sanity check no placeholders remain:

```bash
grep -nE '<REPLACE|<SUBDOMAIN' .env    # should print nothing
```

---

## Step 3 — Edit `docker-compose.prod.yml` in THIS clone

Two edits, both in `/home/ubuntu/blackbird-copi-science/docker-compose.prod.yml`.

### 3a. Logging — use the local `json-file` driver, NOT CloudWatch

The template ships `driver: awslogs` with `/copi/<service>` groups. **This does
not work for blackbird:** the EC2 instance role `copi-ec2-ses-role` is scoped to
org1's *existing* log groups and cannot create any new group/stream — every new
prefix (`/copi-blackbird/*` and `/copi/blackbird-*` alike) is denied with
`AccessDeniedException: logs:CreateLogStream`, so the containers fail to start.
Unless you can amend that IAM policy, switch blackbird's logging to the local
`json-file` driver (rotated). Replace each service's `logging:` block with:

```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
        tag: <service>
```

`docker logs copi-blackbird-*` still works; logs just don't ship to CloudWatch.

### 3b. Join a shared edge network so nginx can reach this app

Add a shared external network to the app service so the existing nginx can proxy
to it by a stable name — no host ports, no public exposure.

> ⚠️ **CRITICAL: the app service MUST NOT be named `app`.** Docker Compose adds
> the *service name* as a network alias on **every** network the service joins,
> including `copi-edge`. org1's nginx (also on `copi-edge`) has
> `upstream app { server app:8000; }`, so a service named `app` on `copi-edge`
> hijacks that name and **all copi.science traffic gets proxied to THIS stack**
> (wrong ORCID app, session logout via a different SECRET_KEY). Name the service
> `blackbird-app` and pin `container_name` so `docker exec copi-blackbird-app-1`
> still works.

Rename the service and attach it to both networks:

```yaml
  blackbird-app:                       # NOT "app" — see warning above
    container_name: copi-blackbird-app-1
    # ...build/command/env_file/environment as before...
    networks:
      - default
      - copi-edge
```

The service name `blackbird-app` becomes its alias on `copi-edge` automatically;
no explicit `aliases:` block is needed. Update the (unused) `nginx` service's
`depends_on: app` → `depends_on: blackbird-app` so the config still validates.
Verify after bring-up that `copi-edge` never exposes a bare `app` alias:

```bash
docker inspect copi-blackbird-app-1 \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{.Aliases}}{{println}}{{end}}'
docker exec copi-python-nginx-1 getent hosts app          # must be org1's 172.18.x
```

At the very bottom of the file (top level, next to `volumes:`), add:

```yaml
networks:
  copi-edge:
    external: true
```

> Leave all other services on the implicit `default` network only. Only
> `blackbird-app` needs to be reachable from the shared edge.

Create the shared network once (idempotent):

```bash
docker network create copi-edge 2>/dev/null || true
```

---

## Step 4 — Bring up the Blackbird stack (no nginx/certbot)

Run from this directory. Start only the four app-tier services — **not** nginx or
certbot (org1's edge serves TLS for both).

```bash
cd /home/ubuntu/blackbird-copi-science
docker compose -f docker-compose.prod.yml up -d --build postgres blackbird-app worker grantbot
docker ps --filter "name=copi-blackbird" --format 'table {{.Names}}\t{{.Status}}'
```

**If `docker compose` errors on this host**, fall back to explicit project flag,
or build+run each container with `docker` directly:

```bash
docker compose -p copi-blackbird -f docker-compose.prod.yml up -d --build postgres blackbird-app worker grantbot
```

---

## Step 5 — Run database migrations

```bash
docker exec copi-blackbird-app-1 alembic upgrade head
# verify:
docker exec copi-blackbird-app-1 alembic current
```

Quick app health check from inside the container (not yet public):

```bash
docker exec copi-blackbird-app-1 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read())"
```

---

## Step 6 — Wire the existing nginx to serve `blackbird.copi.science`

All of this touches the **existing** stack additively (no recreate of prod
containers).

### 6a. Attach the running prod nginx to the shared network (live, no restart)

```bash
docker network connect copi-edge copi-python-nginx-1
docker exec copi-python-nginx-1 getent hosts blackbird-app   # should resolve
```

> Durability note: a live `network connect` is lost if `copi-python-nginx-1` is
> ever recreated. To make it permanent, also add `copi-edge` (external) to the
> **nginx** service in `/home/ubuntu/copi-python/docker-compose.prod.yml` so a
> future redeploy re-attaches it automatically.

### 6b. Add the ACME (HTTP) vhost, reload, then issue the cert

Edit `/home/ubuntu/copi-python/nginx/nginx.conf` and add an **HTTP-only** server
block first (model on the existing `devel.copi.science` block near the bottom):

```nginx
upstream blackbird_app {
    server blackbird-app:8000;
}

server {
    listen 80;
    listen [::]:80;
    server_name blackbird.copi.science;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / {
        return 301 https://$host$request_uri;
    }
}
```

Reload nginx and issue the certificate via the existing certbot container:

```bash
docker exec copi-python-nginx-1 nginx -t && docker exec copi-python-nginx-1 nginx -s reload

docker exec copi-python-certbot-1 certbot certonly --webroot \
  -w /var/www/certbot -d blackbird.copi.science \
  --email asu@scripps.edu --agree-tos --no-eff-email
```

Confirm the cert landed at `/etc/letsencrypt/live/blackbird.copi.science/`.

### 6c. Add the HTTPS vhost and reload

Append the HTTPS server block to the same file:

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name blackbird.copi.science;

    ssl_certificate     /etc/letsencrypt/live/blackbird.copi.science/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/blackbird.copi.science/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:10m;
    ssl_session_tickets off;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    client_max_body_size 10m;

    location / {
        proxy_pass http://blackbird_app;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_connect_timeout 60s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
        proxy_buffering on;
        proxy_buffer_size 4k;
        proxy_buffers 8 4k;
    }
}
```

```bash
docker exec copi-python-nginx-1 nginx -t && docker exec copi-python-nginx-1 nginx -s reload
```

> The nginx.conf is bind-mounted into the container as a template and re-rendered
> on reload — editing the file on disk + `nginx -s reload` is sufficient; no
> container recreate needed.

---

## Step 7 — Verify end to end

```bash
curl -sSI https://blackbird.copi.science/api/health      # 200 via TLS
curl -sS  https://blackbird.copi.science/api/health      # {"status":"ok"}
```

- [ ] HTTPS resolves with a valid Let's Encrypt cert for `blackbird.copi.science`
- [ ] `/api/health` returns `{"status":"ok"}`
- [ ] org1 (`https://copi.science`) still serves normally (regression check)
- [ ] `docker logs copi-blackbird-worker-1` shows the poll loop, no crash
- [ ] CloudWatch shows `/copi-blackbird/*` log groups

---

## Step 8 — Onboard org2's agents (post-deploy, no restart)

1. Log in as an admin on `https://blackbird.copi.science`.
2. Seed PIs / generate profiles (see `CLAUDE.md` → "Adding New PIs"):
   `docker exec copi-blackbird-app-1 python -m src.cli seed-profiles --file new_orcids.txt`
3. Provision Slack bots via **/admin/agents → Provision** (needs the
   `SLACK_CONFIG_TOKEN` pair set in Step 2 and the public `BASE_URL`).
4. Activating an agent (`status='active'` + token in `AgentRegistry`) is picked up
   live by `_sync_roster_from_db` — no container restart.
5. Run the agent simulation as a one-off container when ready:
   ```bash
   docker compose -p copi-blackbird -f docker-compose.prod.yml --profile agent \
     run -d --name blackbird-agent-run agent python -m src.agent.main --budget 0
   ```

---

## Rollback / teardown

Tear down **only** the blackbird stack (leaves org1 untouched):

```bash
cd /home/ubuntu/blackbird-copi-science
docker compose -p copi-blackbird -f docker-compose.prod.yml down      # NEVER add --remove-orphans
# destroy its data volume too (irreversible):
docker volume rm copi-blackbird_pgdata
```

Undo the nginx wiring on the existing stack:

```bash
# remove the two blackbird server blocks + upstream from copi-python/nginx/nginx.conf, then:
docker exec copi-python-nginx-1 nginx -t && docker exec copi-python-nginx-1 nginx -s reload
docker network disconnect copi-edge copi-python-nginx-1
```

---

## Summary of what's isolated vs shared

| Concern | Blackbird | Shared with org1 |
|---|---|---|
| Postgres data (`pgdata` volume) | ✅ own | — |
| Containers / compose network | ✅ own (`copi-blackbird_*`) | `copi-edge` (app↔nginx only) |
| `SECRET_KEY`, DB password | ✅ own (fresh) | — |
| Slack workspace + bot tokens | ✅ own (DB-stored) | — |
| Domain | `blackbird.copi.science` | shares nginx + certbot edge |
| Email sender / SES identity | — | ✅ `noreply@copi.science` |
| AWS instance IAM role (SES/S3/logs) | — | ✅ shared |
| CloudWatch log groups | ✅ `/copi-blackbird/*` | — |
