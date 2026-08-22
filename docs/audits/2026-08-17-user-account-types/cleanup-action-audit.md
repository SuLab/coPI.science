# Adversarial audit of the proposed cleanup actions

Date: 2026-08-18. Auditor ran **no** destructive command. Every number below is
labelled **[measured]** or **[estimated]**.

Host state at audit time: `/` = 61G, 49G used, 13G free (80%). `/var/lib/docker`
≈ 46.2 GB of that — **Docker is essentially the entire disk problem**
(overlay2 43 GB, volumes 3.0 GB, image metadata 128 MB, container logs 65 MB)
**[measured, `du -sm`]**.

---

## 0. Headline findings

1. **The `.env` attribution is sound — and I verified it by a stronger,
   independent method.** Every image built by compose carries the label
   `com.docker.compose.project`. Reading that label on all 50 dangling images
   reproduces the plan's split **exactly**: 40 → `copi-blackbird`, 10 →
   `copi-python`. The plan's "UNKNOWN" set is not unknown; it is **positively
   org1's**, by org1's own build label. No blackbird image is hiding in the 10,
   and no org1 image is hiding in the 40. **[measured]**

2. **No image in the 40 is load-bearing in content terms.** Cross-checked at the
   layer level: after deleting all 40, **zero** layers are lost from any
   protected tag and **zero** from any `copi-python` image. **[measured]**

3. **One of the 40 deserves to be kept anyway, at zero cost: `15582b083371`.**
   It is the *correctly labelled* `copi-blackbird/agent` image of the
   2026-08-15 11:14:25 pre-0028 build. The tag `copi-blackbird-agent:rollback-pre0028`
   currently points at `0c1b4c707981`, which is labelled
   `com.docker.compose.service=blackbird-app` — an alias created deliberately by
   the earlier `safe-to-clean-audit.md:131`. The alias is *content-correct* (all
   11 RootFS layers and the `Cmd` are identical), so nothing is broken today.
   But keeping `15582b083371` costs **0.0 MB** (measured: retaining it changes the
   post-delete layer total by exactly zero bytes) and removes the ambiguity.
   Re-point the tag at it.

4. **Item C is wrong about 2.1.227.** A live `claude` process — **PID 3429916,
   running 5 days 4 hours**, inside a detached `screen`, cwd
   `/home/ubuntu/blackbird-copi-science` — holds an executable (`txt`) handle on
   `/home/ubuntu/.local/share/claude/versions/2.1.227`. Deleting that file will
   **not** reclaim its 291 MB until that process exits, and if that session ever
   execs its own binary by path it breaks. **[measured, `lsof` + `/proc/*/exe`]**

5. **A hazard the plan does not mention at all: this host is memory-starved and
   OOM-killed something 30 minutes before the plan was written.** 3.8 GB RAM,
   129 MB free, swap 1.48G/2.0G (72%) used. Two global OOM kills in 7 days
   (Aug 14 and **Aug 17 23:47:36**, both victims being Claude CLI processes at
   ~2.5 GB RSS). `blackbird-agent-run` exited **137** at 23:56:28. Running a
   30 GB / 563-record `builder prune` on a box in this state is the one step here
   that could plausibly take down a *production* process, because the OOM killer
   picks by badness score and org1's `agent-run`, `copi-python-app` and both
   Postgres containers are all candidates. **[measured]**

---

## A. Delete the 40 dangling images — **APPROVE WITH MODIFICATION**

### Modification (exact)

Run this **first**, then delete only the remaining **39**:

```bash
docker tag 15582b083371 copi-blackbird-agent:rollback-pre0028
docker images copi-blackbird-agent          # confirm the tag moved
```

Then `docker rmi` the 40 IDs **minus `15582b083371`**.

Rationale: `15582b083371` is the only artifact of the pre-0028 build that
actually carries `com.docker.compose.service=agent`. Retaining it is free
(0.0 MB delta, measured), and it makes the agent restore point self-describing
rather than an alias of the app image.

### Verification performed

| Check | Result |
| --- | --- |
| Compose label on all 50 dangling images | 40 `copi-blackbird`, 10 `copi-python` — **identical to the plan's split** [measured] |
| Plan's 40 IDs vs. label-derived blackbird set | exact match; plan correctly excludes `6e436c546efa` (untagged but **not** dangling — it is the `Parent` of `blackbird-test:latest`) [measured] |
| Plan's 10 "UNKNOWN" vs. label-derived org1 set | exact match [measured] |
| Any protected tag losing a layer | **none** — all 8 resolvable protected tags keep all 11 layers [measured] |
| Any `copi-python` image losing a layer | **none** [measured] |
| Any of the 40 in use by a container | none; all show `CONTAINERS 0` [measured] |
| Any of the 40 being a `Parent` of a kept image | none — only `blackbird-test:latest` has a `Parent`, and it is `6e436c546efa`, not in the 40 [measured] |

### On the plan's question 2 ("`docker rmi` should refuse")

The premise is wrong, though the outcome is safe. `docker rmi` does **not**
refuse on the grounds of shared layers. It removes the image *record* and then
decrements layer refcounts, deleting only layers no other image references. It
refuses in exactly three cases: (a) the image is used by a container, (b) the
image has a dependent child image, (c) the image has multiple tags and `-f` was
not given. **None of the three applies to any of the 40** (verified above), so
the operation will proceed silently — do not treat "it didn't refuse" as
evidence of anything. The real protection is the layer-set analysis above.

### Rollback capability after this step

**Blackbird — preserved.** All six intended restore tags resolve to complete
images:

| Tag | Image | Complete after delete |
| --- | --- | --- |
| `copi-blackbird-blackbird-app:rollback-pre0028` | `0c1b4c707981` | yes |
| `copi-blackbird-blackbird-app:post0028` | `77ecf8d080bb` | yes |
| `copi-blackbird-blackbird-app:5961bc5` (=`latest`) | `30030f052d81` | yes |
| `copi-blackbird-worker:rollback-pre0028` | `0574ca572380` | yes |
| `copi-blackbird-worker:post0028` | `8c97a76dab3f` | yes |
| `copi-blackbird-worker:5961bc5` (=`latest`) | `ce945f038bfe` | yes |
| `copi-blackbird-agent:rollback-pre0028` | `0c1b4c707981` (alias; see modification) | yes |
| `copi-blackbird-agent:5961bc5` (=`latest`) | `b67f1e40dea8` | yes |

Note for completeness: **`copi-blackbird-agent:post0028` does not exist.** The
plan does not claim it does, but the asymmetry is worth knowing — an app/worker
roll-forward to `:post0028` has no matching agent image and would have to run
the agent on `:5961bc5`.

**Org1 — preserved.** Untouched: 4 `:latest` images, 4 `:pre-35ce7ea` rollback
tags, and all 10 of their dangling images. No org1 image loses a layer.

### Irreversibility the plan does not flag

After this step the **oldest blackbird image on the box is 2026-08-15**
(`rollback-pre0028`). Every image from 2026-08-05 through 2026-08-14 — the whole
star-topology and hub-budget work — is destroyed permanently. Rebuilding from git
is possible but **not byte-identical** (pip re-resolves), and combined with item B
it becomes a fully cold rebuild. This is acceptable given the tagged restore
points, but it should be a conscious decision, not a side effect.

### Expected reclaim

**8.88 GB [measured]** — computed as the sum of `layerdb/sha256/<chainID>/size`
over the 60 layers referenced *only* by the 40 (equivalently, the 39 after the
modification: the delta is 0.0 MB). Total image-layer footprint drops from
**15.96 GB → 7.09 GB**. The 15.96 GB figure reconciles exactly with
`docker system df`'s Images TOTAL SIZE, which validates the method.

Ignore `docker system df`'s "12.65 GB reclaimable" — it is an overestimate for
exactly the reason seen earlier in the session.

**Caveat on timing:** overlay2 is 43 GB while image layers are only 15.96 GB, so
~27 GB of overlay2 belongs to BuildKit snapshots. BuildKit can hold leases on
snapshots that are *also* image layers — which is the real explanation for the
earlier "195 images pruned, only 984 MB freed" result. **Run B before A** and the
full 8.88 GB should land; run A alone and part of it may be deferred.

---

## B. `docker builder prune -af` — **APPROVE WITH MODIFICATION**

### Modification (exact)

```bash
docker builder prune -af --filter until=24h
```

Two reasons for `--filter until=24h`:

- It preserves the ~1.9 GB of cache from today's two blackbird builds
  (`post0028` 4h ago, `5961bc5` 3h ago), which is exactly the cache an operator
  mid-deploy-cycle will want. `-af` throws it away for no gain — it is 6% of the
  total.
- It bounds the work. See the memory point below.

### What `builder prune` actually removes — answered precisely

It removes **BuildKit cache records and their snapshots only**. It does not
touch images, containers, volumes, networks, or the image layer store. Layers
referenced by an image are held by the image store's own lease and are not
eligible for BuildKit GC, so **no currently-tagged image can become unusable**.
No daemon restart, no container restart, no effect on either stack's running
processes. Org1's only loss is that its next `docker compose build` is a cold
build.

One residual, low-probability cost worth stating: the build cache is the only
local record of the exact pip wheels that went into the current images. Once
pruned, a rebuild re-resolves from PyPI and may not reproduce them byte-for-byte.
This is precisely why the tagged rollback images matter — and they survive.

### The hazard the plan missed

This is a 3.8 GB box with 129 MB free and 72% of swap consumed, which
OOM-killed a process at 23:47 today. A prune across 563 records / 30 GB is a
sustained daemon-side memory and IO load. If a global OOM fires mid-prune the
victim is chosen by badness score, and org1's `agent-run`, `copi-python-app`,
and both Postgres containers are all live candidates. **Check `free -m` before
starting; if `available` is under ~1 GB, close the stale 5-day `screen` session
(PID 3429916) first.** An interrupted `builder prune` is itself safe and
resumable — the concern is collateral damage to production processes, not to the
prune.

### Expected reclaim

**24–27 GB [measured ceiling, estimated actual].** The hard bound is
`overlay2 43 GB − image layers 15.96 GB ≈ 27 GB` **[measured]**. Docker's own
per-record sum says 30.57 GB total / 15.69 GB "reclaimable"; the plan's
"~18,505 MB" came from the same unreliable source. Both are estimates and both
disagree with each other — trust the 27 GB ceiling. With `--filter until=24h`,
expect **~25 GB**.

Age distribution **[measured, docker's own record sizes — estimates]**:
4 months 8.85 GB · 3 months 3.92 GB · 2 months 2.26 GB · 11 days 4.07 GB ·
10–12 days 3.19 GB · 2–3 days 4.94 GB · **today 1.88 GB** (the part `until=24h`
preserves).

**This is the single largest and lowest-risk item in the plan.** It should run
first, not second.

---

## C. Delete Claude CLI versions — **APPROVE WITH MODIFICATION**

### Modification (exact)

Delete **2.1.232 only**:

```bash
rm /home/ubuntu/.local/share/claude/versions/2.1.232
```

**Do NOT delete 2.1.227 yet.** Keep 2.1.233 (fallback) and 2.1.234 (live).

### Why

`lsof` and `/proc/<pid>/exe` show three live CLI processes:

| PID | Version | Age | Context |
| --- | --- | --- | --- |
| 2473703 | 2.1.234 | 3h47m | interactive |
| 2758001 | 2.1.234 | 1h35m | interactive (this session) |
| **3429916** | **2.1.227** | **5d 04h** | **detached `screen` → bash → claude, cwd = this repo** |

Consequences for 2.1.227:

- On Linux, `rm` of a running binary succeeds and the process keeps working —
  but the inode is retained, so **the 291 MB is not reclaimed** until PID 3429916
  exits. The plan's "~600 MB" is really **309 MB now**.
- The CLI spawns children by exec'ing its own version path (visible in `lsof` as
  a process whose `argv[0]` is `.../versions/2.1.234`). If the 5-day session does
  that after deletion it gets `ENOENT` mid-session.

So: close that `screen` session first (it is also idle at 65 MB RSS but sitting
in swap on a memory-starved box), *then* delete 2.1.227 to recover the full
600 MB.

### Fallback risk — cleared

`/home/ubuntu/.local/bin/claude` is a **plain symlink** to
`.../versions/2.1.234`, with no shell-script fallback logic. `~/.claude.json`
has `installMethod=native`, `autoUpdates=false`,
`autoUpdatesProtectedForNative=true`, and no pinned-version key. Nothing can
fall back to a deleted sibling. **[measured]**

### Expected reclaim

**309 MB [measured]** now; **600 MB [measured]** once PID 3429916 exits and
2.1.227 is removed.

---

## D. System caches — **APPROVE WITH MODIFICATION**

### Modifications (exact)

1. **Capture the OOM forensics before vacuuming** (they are from *today* and
   survive a 200M vacuum, but this is free insurance and there is an open
   incident):
   ```bash
   sudo journalctl -k --since "7 days ago" --no-pager > /home/ubuntu/blackbird-copi-science/logs/kernel-oom-2026-08-18.log
   docker logs blackbird-agent-run > /home/ubuntu/blackbird-copi-science/logs/blackbird_run_exit137.log 2>&1
   ```
2. **Make the journal reclaim durable.** `journalctl --vacuum-size` is a
   **one-shot**, not a policy. `/etc/systemd/journald.conf` is empty (`[Journal]`
   with no keys), so the default `SystemMaxUse` applies —
   `min(10% of 61G, 4G)` = **4 GB**. The journal will simply regrow. If a
   200 MB ceiling is wanted, set it:
   ```
   [Journal]
   SystemMaxUse=200M
   ```
   then `sudo systemctl restart systemd-journald`.

`apt-get clean` and `rm -rf ~/.cache/pip` are safe as written: both are pure
caches, no build is in flight, and `.cache/pip` is owned by `ubuntu`.

### What the journal vacuum actually destroys — answered

**Neither stack's application logs are in journald.** All 11 containers use the
`json-file` log driver **[measured]**, so both apps, both Postgres instances,
nginx, certbot and both agent runs log to
`/var/lib/docker/containers/*/​*-json.log` (65 MB total), which
`journalctl --vacuum-size` does not touch.

What is lost is host-level history: kernel, systemd, dockerd, sshd,
unattended-upgrades. Vacuuming 439 MB → 200 MB removes the oldest **archived**
files first; the active `system.journal` (Aug 18) is never removed. Expect to
lose roughly **2026-02-25 → 2026-07-02**, retaining July and August. Both stacks'
deploy-relevant history (blackbird's 08-05→08-17 work, org1's 08-14/08-15 builds)
and **today's OOM evidence** are retained.

200 MB is a defensible floor given that. **It is irreversible** — vacuumed
journal files are gone, there is no undo, and the plan does not say so.

### Expected reclaim

| | |
| --- | --- |
| journal 438.8 M → 200 M | **~239 MB [measured]** — *not durable without `SystemMaxUse`* |
| `/var/cache/apt` | **192 MB [measured]** |
| `~/.cache/pip` | **173 MB [measured]** |
| **Total D** | **~604 MB [measured]** |

---

## E. Things the plan omits

- **Memory pressure during the operation** (see B). The only realistic path from
  this cleanup to a production outage.
- **`docker builder prune` should precede `docker rmi`**, not follow it —
  BuildKit leases are what made the earlier image prune under-deliver.
- **`~/.claude/projects` is 179 MB** and `.venv-test` is 477 MB. Neither is
  proposed for deletion and neither should be (`ci.sh` needs `.venv-test`), but
  they are the next-largest non-Docker items if more is needed.
- **Org1's containers have no log rotation** — `json-file` with no `max-size` /
  `max-file`, versus blackbird's `10m × 5`. Currently harmless (65 MB total) but
  it is an unbounded growth path on a disk that just hit 80%. Not this cleanup's
  job; worth reporting to org1.
- **`blackbird-agent-run` (exited 137)** is the only record of the run that died
  at 23:56. Nothing in the plan deletes it, but save its logs before any Docker
  operation — a mistyped `docker system prune` would take it.
- Disk-pressure-during-operation is **not** a concern: every step is a deletion
  and none needs scratch space.

---

## Recommended execution order, with verification gates

**Step 0 — safety net (no deletions).**
```bash
cd /home/ubuntu/blackbird-copi-science
docker logs blackbird-agent-run > logs/blackbird_run_exit137.log 2>&1
sudo journalctl -k --since "7 days ago" --no-pager > logs/kernel-oom-2026-08-18.log
docker tag 15582b083371 copi-blackbird-agent:rollback-pre0028
df -h / ; free -m
```
*Gate:* `docker images copi-blackbird-agent` shows `rollback-pre0028` on
`15582b083371`. `free -m` available ≥ 1 GB — if not, close the 5-day `screen`
session (PID 3429916) and re-check.

**Step 1 — build cache (biggest win, lowest risk).**
```bash
docker builder prune -af --filter until=24h
```
*Gate:* `df -h /` (expect ~25 GB freed); `docker ps` shows **all 11** containers
in the same state as before, including `agent-run` Up and both Postgres healthy;
`docker inspect agent-run --format '{{index .Config.Labels "com.docker.compose.project"}}'`
still `copi-python`.

**Step 2 — the 39 images (40 minus `15582b083371`).**
```bash
docker rmi <39 IDs>
```
*Gate:* every protected tag still inspects clean —
```bash
for t in copi-blackbird-blackbird-app:{rollback-pre0028,post0028,5961bc5,latest} \
         copi-blackbird-worker:{rollback-pre0028,post0028,5961bc5,latest} \
         copi-blackbird-agent:{rollback-pre0028,5961bc5,latest}; do
  docker inspect "$t" >/dev/null && echo "OK $t" || echo "MISSING $t"
done
docker images | grep -c copi-python      # must be unchanged (13)
```
Plus: both sites still serve, and `docker ps` unchanged.

**Step 3 — CLI.**
```bash
rm /home/ubuntu/.local/share/claude/versions/2.1.232
```
*Gate:* `claude --version` still reports 2.1.234; `ls versions/` shows 227/233/234.
(2.1.227 only after PID 3429916 is gone.)

**Step 4 — system caches.**
```bash
sudo journalctl --vacuum-size=200M
sudo apt-get clean
rm -rf /home/ubuntu/.cache/pip
```
*Gate:* `journalctl -k --since "24 hours ago" | tail` still returns today's OOM
lines; `df -h /`.

**Step 5 — final.** `df -h /` and `docker system df`. Expect **/ at roughly
22–25% used, ~14–16 GB used, ~45 GB free.**

---

## Total realistic reclaim

| Item | Reclaim | Basis |
| --- | --- | --- |
| B — build cache (`until=24h`) | ~25 GB | **measured ceiling 27 GB**, estimated actual |
| A — 39 dangling images | 8.88 GB | **measured** (exclusive-layer sum) |
| D — journal + apt + pip | 0.60 GB | **measured** |
| C — CLI 2.1.232 | 0.31 GB | **measured** |
| **Total** | **~32–35 GB** | |

Post-cleanup `/` should sit near **14–16 GB used of 61 GB**, versus 49 GB today.
The `docker system df` "reclaimable" numbers (12.65 GB images, 15.69 GB cache)
are **both wrong in opposite directions** and should not be quoted.

---

## Answers to the plan's six questions

1. **Sound?** Yes — and independently confirmed by the stronger
   `com.docker.compose.project` **image label**, which reproduces the 40/10 split
   exactly. The 10 "UNKNOWN" are positively org1's, by org1's own build label.
   No blackbird-attributed image is content-load-bearing; `15582b083371` should
   nonetheless be retained (free) as the correctly-labelled agent restore point.
2. **Shared-layer risk?** No layer is lost from any protected tag or any org1
   image. But `docker rmi` does **not** refuse on shared layers — it refuses only
   for in-use / child-image / multi-tag, none of which apply. The safety comes
   from the layer analysis, not from rmi's behaviour.
3. **`builder prune -af` and org1?** Only rebuild speed, plus a minor loss of
   pip-wheel reproducibility. No image, container, volume or network effect. Add
   `--filter until=24h`. The real risk is OOM collateral damage on a 3.8 GB box,
   not the prune itself.
4. **CLI mid-session?** 2.1.232 is safe and reclaims immediately. **2.1.227 is
   not** — a 5-day `screen` session (PID 3429916) is executing it; deletion is
   reclaim-deferred *and* risks `ENOENT` on self-exec. Keep 2.1.233 and 2.1.234.
5. **Journal?** No application or database logs are in journald (all containers
   use `json-file`). Host history before ~2026-07-02 is lost, irreversibly. 200 M
   is a sane floor but is a **one-shot** — set `SystemMaxUse=200M` or it regrows
   to the 4 GB default.
6. **Ordering / irreversibility?** Yes: run **B before A**; tag `15582b083371`
   before anything. Unflagged irreversibles: (a) no blackbird image older than
   2026-08-15 will exist, and with the cache gone a rebuild is cold and not
   byte-identical; (b) vacuumed journal files cannot be recovered.
