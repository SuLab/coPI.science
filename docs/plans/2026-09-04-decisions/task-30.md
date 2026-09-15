# Task 30 — container memory: nginx's arithmetic, and the agent's unmeasured cap

Serves #27 I5. The issue's `Fix:` clause, verbatim from
`docs/plans/2026-09-02-close-issues-20-27-evidence/issues/issue_27.md`:

> *Fix:* reconcile the nginx template across all three vhosts; add container resource limits.

Three parts. **30a** is a FIX (over-implementation R9). **30b** and **30c** are DECIDE.

---

## Ruling

### 30a — nginx: size the zones, not the cap

`88d1b25` set `mem_limit: 128m` against "40 MiB of shared zones (three `limit_*_zone 10m` +
`ssl_session_cache shared:SSL:10m`)". `1a430c0` then split those three zones into eight — one set per
vhost — and revisited neither the cap nor the comment. Eight zones at 10m plus the one `shared:SSL`
segment the three vhosts declare under a single name is **90 MiB declared into a 128 MiB cgroup**,
inside the limit only while the zones stayed empty. Two figures were stale, in two files:
`docker-compose.prod.yml` said 40 MiB, and `nginx/nginx.conf`'s own header said "8x10m = 80m", which
omits the SSL zone entirely.

**Chosen: shrink the zones to their populations and leave `mem_limit` at 128m.**

Options considered and rejected:

- **Raise `mem_limit` to ≥192m** (the audit's suggestion). Rejected: it accepts 10m as the right size
  for a zone nothing ever derived, and it spends 64 MiB of a ~3.7 GB host that also runs the
  blackbird stack — to hold rate-limiter state for an SSH-tunnelled dev vhost. The seven-service sum
  is a tested ceiling (`test_the_copi_python_mem_limits_sum_comfortably_under_the_host_total`,
  ≤ 3072m); spending against it needs a reason better than "the previous value was copied".
- **Pin `worker_processes`.** Not available, and an earlier draft of the plan was wrong to offer it:
  `grep -rn worker_processes nginx/ docker-compose*.yml` returns nothing, the mounted file is a
  `conf.d` snippet, and `worker_processes` is a main-context directive that cannot be set from one.
  48 is the image's stock `auto` on the 48-core host and is not ours to pin.
- **Revert the zone split.** Rejected: the split is #27 Minor 16's fix for one IP's burst against
  devel consuming the primary site's whole budget. The split was right; only its sizing was copied.

New sizes, by what each zone's population can actually be (1m ≈ 16k IPv4 states):

| zone | was | now | why |
|---|---|---|---|
| `req_general_main` | 10m | **10m** | the public site — genuinely internet-scale, unchanged |
| `req_general_devel` | 10m | **1m** | an SSH-tunnelled dev vhost |
| `req_general_blackbird` | 10m | **4m** | the second stack, low traffic |
| `req_graph_main` | 10m | **4m** | four routes |
| `req_graph_blackbird` | 10m | **1m** | four routes on the low-traffic vhost |
| `conn_perip_main` | 10m | **4m** | `limit_conn` state lives only while an IP holds an OPEN connection, so the population is concurrent connections, not distinct clients ever seen. 4m ≈ 64k states is above the ~24k client connections stock `worker_connections` (1024) × 48 workers can hold at all |
| `conn_perip_devel` | 10m | **1m** | as above |
| `conn_perip_blackbird` | 10m | **2m** | as above |
| `ssl_session_cache shared:SSL` | 10m | **10m** | one segment for all three vhosts; TLS resumption, unchanged |

**27m of rate/conn zones + 10m SSL = 37 MiB.** Budget: 37 + 48 workers × 1.0 MiB + 16 MiB headroom
= **101 MiB into 128m**.

Neither file restates the other's figure any more. `nginx.conf` states the total it declares;
`docker-compose.prod.yml` states only the terms nginx.conf cannot know, in one machine-readable line
(`nginx-mem-budget: zones + 48 workers x 1.0 MiB + 16 MiB headroom`); and
`tests/unit/test_nginx_config.py` sums the declarations and checks the whole budget against
`mem_limit`. `EXPECTED_MEM` in `tests/unit/test_deploy_compose.py` needed no change, because this
fixes the arithmetic by sizing the zones rather than by moving the cap.

### 30b — the agent's cap: **keep 768m**, now measured

D19 shipped `agent: mem_limit 768m` as an explicitly *unmeasured* value ("revisit after a week of
`docker stats`"). It is the cap on the one process whose `SIGKILL` loses data — the DB, not Slack, is
the durable store, an OOM kill skips the shutdown flush, and `stop_grace_period: 30s` cannot mitigate
that (it applies to SIGTERM).

**Measured peak: 226 MiB.** Steady state over 318 turns: 185 MiB. That is **3.4× headroom** under the
existing cap. Method and numbers in `## Evidence`.

Options considered and rejected:

- **Raise it.** Rejected: nothing in the measurement asks for more. The measured worst case uses 29 %
  of the cap, the host is ~3.7 GB and also runs the blackbird stack, and the seven-service sum
  (2432 MiB) is a tested ceiling.
- **Remove the cap, the way D24 removed it for `postgres`.** Rejected, and the D24 analogy does not
  transfer. D24's reason was specific: a cgroup cap on a ~2.3 GB database also caps its **page
  cache**, and an OOM-killed backend restarts the whole cluster. The agent has no page cache to
  protect. What removing the cap actually changes is *who chooses the victim*: instead of the agent's
  own cgroup killing the agent, the kernel OOM killer picks a process on a 3.7 GB host — and the
  candidate with the largest RSS on that host is `postgres`, which D24 deliberately left uncapped and
  which is the durable store the whole failure mode is about. Uncapping trades a contained SIGKILL
  for an uncontained one.
- **Add `mem_reservation` (the audit's suggested minimal fix).** Rejected: `mem_reservation` is a
  soft limit. It changes reclaim priority under host pressure; it does not prevent the OOM kill,
  which is the failure mode named. With 3.4× measured headroom it would be a knob added for the
  appearance of action, which is the exact failure pattern this branch exists to stop.

**Residual, which the deploy note must carry:** the measurement runs with Slack **off**, so the one
term it cannot cover is `_rebuild_state_from_slack`. Reading the code bounds it anyway: that pass
iterates **channels** (10 in the copy), not the roster — `get_full_channel_history(ch_id)` one channel
at a time, deduplicated against `_known_slack_ts` and appended into the same `MessageLog` the DB
rebuild already filled. The audit's "per-agent Slack history for the whole roster, with no bound in
the code" overstates it. The other named unbounded term, R5's LLM-call-log re-queue, is bounded in
the source: `LLM_LOG_REQUEUE_MAX_ROWS = 1000` (`src/agent/simulation.py:237`), whose own comment sizes
it at "~30 MB" against this very `mem_limit`. 226 + 30 + a channel history is not near 768.

### 30c — the fourth `./prompts` mount: **drop it**, and state the tension for the other three

v1 of the plan recorded this as "kept by design"; that is rejected. `audit-over-implementation.md`
records that #27 I5 named `./prompts:/app/prompts` a **defect** — "silently shadowing the image's
prompts" — on `app`, `agent` and `grantbot`. `3354904` added a fourth, on `worker`, and
`test_deploy_compose.py::test_worker_mounts_prompts_like_app_and_agent_do` pinned the widening, which
is how a defect stops looking like one.

`3354904`'s own commit message states the rationale: the mount closes "a split-brain where an
unrebuilt worker silently ran stale prompt text **after a host-side prompt edit**". **Decision D33
forbids that edit** — no file under `prompts/` may change. The mount's entire purpose is a workflow
that is not allowed to happen, and prompts are already baked into the image by the Dockerfile's
`COPY . .`.

**Chosen: revert the widening.** `worker`'s mount is removed and its pin deleted, replaced by
`test_the_prompts_bind_mount_did_not_spread_to_a_fourth_service`, which pins the *set* of services
that still shadow prompts — so adding a fifth trips, and removing one of the three forces this file
to be updated in the same commit.

**And the tension is stated, because both rules cannot govern silently.** The three pre-existing
mounts remain. They are a **residual, not an endorsement**: under D33 they carry no benefit (nobody
may edit `prompts/`) and one cost (an un-reviewed, un-gated write path into model-facing text on
three production services, invisible to `scripts/ci.sh`, which lints the checkout and not the host's
bind mounts). Removing them changes deployed behaviour and belongs with the runbook — deploy note 15
already tells the operator that `prompts/` is git-tracked and read-only at runtime, which is the same
conclusion arrived at from the other direction. **This must appear in #27's closing comment** (see
`## Consequence a closing comment must state`).

---

## Evidence

### 30a — nginx

Red first, at `f4a2f80`'s parent, with the budget line added but the zones still at 10m:

```
E  AssertionError: nginx.conf declares 90 MiB of shared memory (SSL:10m,
   conn_perip_blackbird:10m, conn_perip_devel:10m, conn_perip_main:10m,
   req_general_blackbird:10m, req_general_devel:10m, req_general_main:10m,
   req_graph_blackbird:10m, req_graph_main:10m) + 48 workers x 1 MiB
   + 16 MiB headroom = 154 MiB, into mem_limit: 128m
E  AssertionError: nginx.conf's comment claims 80m of shared memory, but its
   declarations sum to 90m
E  AssertionError: docker-compose.prod.yml restates nginx.conf's zone total
   ('40 MiB of shared zones')
```

After: `tests/unit/test_nginx_config.py tests/unit/test_deploy_compose.py` → **26 passed**.

**`nginx -t` on the rendered output.** `envsubst` via the image's own
`/docker-entrypoint.d/20-envsubst-on-templates.sh`, `DOMAIN=copi.science`, throwaway self-signed certs
at the two `live/` paths, and `--add-host app|blackbird-app|host.docker.internal:127.0.0.1` so the
upstream names resolve:

```
$ docker run --rm --add-host app:127.0.0.1 --add-host blackbird-app:127.0.0.1 \
    --add-host host.docker.internal:127.0.0.1 -e DOMAIN=copi.science \
    -v .../nginx/nginx.conf:/etc/nginx/templates/default.conf.template:ro \
    -v <scratch>/certs:/etc/letsencrypt:ro --entrypoint /bin/sh nginx:1.27-alpine \
    -c '/docker-entrypoint.d/20-envsubst-on-templates.sh; nginx -t'
48                                     # nproc — the stock `worker_processes auto` count
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

(The only other output is three `"ssl_stapling" ignored, issuer certificate not found` warnings — an
artifact of the throwaway self-signed certs, not of the config.)

**"One segment per name" is measured, not assumed.** The `ssl_session_cache shared:SSL:10m` line
appears three times in the rendered config. Raising one of them to `20m` in a scratch copy makes
nginx refuse to start:

```
[emerg] the size 20971520 of shared memory zone "SSL" conflicts with already
        declared size 10485760 in /etc/nginx/conf.d/default.conf:357
```

That is why the test keys zones by **name** and counts `SSL` once.

**Idle footprint.** `docker stats --no-stream` on that container: **36.05 MiB**, corroborating the
plan's 36.48 MiB figure and the 1.0 MiB-per-worker allowance.

**Merged compose render** (`docker compose --env-file <dummies> --profile agent -f
docker-compose.prod.yml -f docker-compose.override.yml config`; a render, never a `up`): nginx
`mem_limit: 134217728` = 128 MiB.

### 30b — the agent

**No container was run, and nothing reached Slack or Anthropic.** The measurement drives the real
`SimulationEngine` in-process from a harness
(`<scratch>/task30/measure_agent_memory.py`), against a **private clone of the disposable production
copy**, with:

- the four production Slack credentials exported **present and empty**;
- `SLACK_ENABLED=false`, so `src/agent/main.py`'s transport branch is the `NullTransport` one and no
  `AgentSlackClient` is ever constructed;
- `socket.socket.connect`/`connect_ex` patched to **raise on any non-loopback address**, so no code
  path could reach Slack, Anthropic or anything else even by accident. Final count of refused
  non-loopback connects in the reported runs: **0**;
- `src.services.llm.get_anthropic_client` replaced by a fake returning a canned ~3.5 KB text block —
  so the whole prompt-assembly path runs and **zero tokens were billed**;
- `src.agent.agent.PROFILES_DIR` redirected to a scratch tree, so the shared checkout's `profiles/`
  is never written;
- profiles materialised for the real roster through the app's own exporter
  (`src/services/profile_export.py`): **53 public files, 295,183 bytes; 41 private files, 59,081
  bytes** — the same shape and size the production bind mount holds.

The database is a clone: `copi-prodtest-db` (55434) → `copi_mem30` → `pg_dump` → a private
`postgres:15` container on `127.0.0.1:55437`, then `alembic upgrade head` (0028 → **0029**, which had
just landed; without it the rebuild fails on `agent_messages.pi_inbound_state`). The clone exists so
nothing this harness writes can disturb the tasks sharing 55434. Contents: **53 active agents, 8460
`agent_messages`, 10 `agent_channels`, 251 `grantbot_posted_foas`**.

| run | turns | log entries hydrated | fake LLM calls | peak RSS | `ru_maxrss` |
|---|---|---|---|---|---|
| resume (the normal restart path) | 53 | 1346 | 27 | **112.39 MiB** | 112.53 MiB |
| `--reset-cursors` (every agent re-reads every post) | 53 | 1365 | 149 | **225.79 MiB** | 225.55 MiB |
| `--reset-cursors`, six sweeps | 318 | 1448 | 270 | **212.90 MiB** | 212.64 MiB |

Phase breakdown of the heaviest run: interpreter start 21 MiB → imports 85 MiB → profiles 93 MiB →
`engine.start()` rebuild **110 MiB** → first sweep peak **213 MiB** → then **184.8 MiB flat** at the
end of sweeps 2, 3, 4, 5 and 6. **318 turns with no growth after the second sweep** is the evidence
that the turn loop does not leak; the 226 MiB figure is the transient peak of the first full sweep and
is the number the cap must cover.

(The log hydrates 1346–1448 of the 8460 rows because `REBUILD_WINDOW_S` is 14 days plus the full
history of undecided threads — B2's bound, working. The count differs slightly between runs because
each run persists its own new messages into the clone.)

**Incidental finding, not fixed here (`src/agent/simulation.py` is Group A's file):**
`SimulationEngine.start()` calls `_backfill_foa_cache`, which POSTs to `https://api.grants.gov/v1/api/search2`
**once per row of `grantbot_posted_foas`** — 251 on this copy. With the network unavailable,
`src/services/http_retry.py` spends 0.5 s + 1 s + 2 s of backoff on each before giving up, i.e. **~15
minutes of startup sleeping** before turn 1. On a healthy host it is 251 sequential outbound HTTP
calls at every start. The harness stubs it (the cache it fills is 251 small dicts, far below this
measurement's resolution). Worth an issue; it is not this task's file.

### 30c — the prompts mount

Red first:

```
E  AssertionError: services bind-mounting ./prompts:/app/prompts are
   ['agent', 'app', 'grantbot', 'worker']; expected ['agent', 'app', 'grantbot'].
```

After the mount is removed, the merged `docker compose … config` render shows `worker` binding only
`profiles`, while `app`/`agent`/`grantbot` still bind `prompts`. The seven-service `mem_limit` sum is
unchanged at **2432 MiB** (256 + 384 + 512 + 768 + 256 + 128 + 128), so
`test_the_copi_python_mem_limits_sum_comfortably_under_the_host_total` still holds.

---

## Consequence a closing comment must state

**For #27's closing comment (Task 33 Step 6 already reserves a slot for "Task 30c's ruling on the
prompts mount"):**

> `./prompts:/app/prompts` was removed from `worker`, reverting `3354904`'s widening of a mount #27 I5
> had itself called a defect, along with the test that pinned it. **The same mount remains on `app`,
> `agent` and `grantbot`, and is still a defect.** It is left in place deliberately: removing it
> changes deployed behaviour and belongs with the runbook. Readers should know the tension it creates
> with Decision D33 — D33 forbids any change under `prompts/`, while the mount exists precisely so an
> operator can change `prompts/` with no rebuild, no review and no gate. Under D33 the mount has no
> remaining benefit and one cost: an un-gated write path into model-facing text on three production
> services, invisible to `scripts/ci.sh`. Removing the last three is a follow-up.

**For deploy note 18** — `docs/plans/2026-09-02-close-issues-20-27.md` is **Task 33's file, not this
task's**, so this task did not edit it. Note 18 currently ends "Measure `agent-run` with `docker stats
--no-stream` during a turn before tightening." That sentence is now discharged, and Task 33 should
replace it with:

> `agent`'s 768m is no longer unmeasured (Task 30b): peak **226 MiB** over a full 53-agent
> `--reset-cursors` sweep and **185 MiB** steady over 318 turns, against a clone of the production
> database with Slack off and the LLM faked — 3.4× headroom, and flat after the second sweep, so the
> turn loop does not leak. Kept at 768m; **not** uncapped, because uncapping hands the choice of OOM
> victim to the kernel on a 3.7 GB host, where the largest-RSS candidate is the uncapped `postgres`.
> The one term the offline measurement cannot cover is `_rebuild_state_from_slack`; it iterates
> channels (10), not the roster, so it is bounded by channel history rather than by the 53-agent
> roster. **On the first live `agent-run` after this deploy, still take one `docker stats --no-stream`
> during a turn and compare against 226 MiB.** A live figure above ~500 MiB means the Slack term is
> larger than the code reading suggests and the cap must be revisited.
>
> nginx's 128m is unchanged, but is now *correct* rather than coincidentally survivable (Task 30a):
> `nginx.conf` declared 90 MiB of shared zones into it; the zones are now sized to their populations
> and total 37 MiB, and a test derives the sum from the file rather than restating it. Seven-service
> sum is unchanged at 2432 MiB.
