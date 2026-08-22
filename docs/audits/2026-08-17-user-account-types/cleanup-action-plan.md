# Proposed cleanup actions — FOR ADVERSARIAL REVIEW BEFORE EXECUTION

Host runs TWO production stacks: `copi-blackbird` (this repo) and `copi-python`
(org1, serving copi.science, including a container named `agent-run`).
Current disk: 61G total, ~13G free (80% used).
Nothing below has been executed. Every command is stated exactly as it would run.

## A. Dangling images — ONLY the 40 positively attributed to blackbird

I attributed every one of the 50 dangling images by reading the `COMPOSE_PROJECT_NAME`
baked into each image's `.env` (the Dockerfile does `COPY . .`, so each image carries the
build context's `.env`).

- **40 images** report `COMPOSE_PROJECT_NAME=copi-blackbird` -> ours.
- **10 images** report nothing (no such key, or no `.env`) -> **UNKNOWN, treated as org1's.**
  Six of those ten are dated 2026-08-14, which is exactly when org1's current images were
  built (`copi-python-agent` and `copi-python-grantbot` created 08-14; `copi-python-app`
  and `copi-python-worker` created 08-15). So the UNKNOWN set plausibly contains **org1's
  immediate-previous builds — their rollback path.**

**Proposed:** remove ONLY the 40 blackbird-attributed IDs, explicitly, by ID.
**NOT** `docker image prune -f` — that would take all 50 including org1's.

    docker rmi <40 explicit IDs>

Blackbird IDs (by created date):
- 2026-08-05: 4ffa6f4599d2 6081a30abadb 61098b6c301c 7f5c539a7dbb 81d8874b70c0
  8425e59d3289 ab142cf90be3 bb2c5def2832 c39d9a10aebe f6331869d947 fb63df83e26a fcbc708fe3d4
- 2026-08-06: 10562675d1c2 255ccbf85da2 2b9e7def2a4f 3319cbfa5a27 34548998ae7b 62299da4ed73
  6af4cb0202c3 8a31fb8c37b7 a64d911a1502 c170b53e8a4f
- 2026-08-07: 0084191056bf 4b26bda000a6 4dcac5b12270 7ddafb12160f b1309c0fc7fa c434ed870834
  c5465ca0b198 c8ba4eb5c734
- 2026-08-14: 42a5d25b3aee 4c4fc786e2fc 6c4a0a46bb99 742f40d006f1 78f6031c1144 8c1eac2994f1
  9172c2f7c961 b50d5a734df6 ecbdf48bbf89
- 2026-08-15: 15582b083371

UNKNOWN / DO NOT TOUCH: 22d319b4d310 2d95781adfcc d8b77a4ac62e f9537ee230be (08-06),
27c25d765aa3 3363365987e6 360e1437e7f0 80d60a82ff99 899d86f4e151 baaa86aadb8d (08-14).

**Protected by tags (must survive):** `copi-blackbird-blackbird-app:{rollback-pre0028,post0028,5961bc5}`,
`copi-blackbird-worker:{rollback-pre0028,post0028,5961bc5}`, `copi-blackbird-agent:{rollback-pre0028,5961bc5}`.

## B. BuildKit cache

    docker builder prune -af

Claimed safe: cache is not referenced by any image or container at runtime; the only cost is
that the next build on EITHER stack is slower. A plain `docker builder prune` already ran
earlier and freed 2.89 GB. An audit measured ~18,505 MB remaining as reclaimable.

## C. Claude CLI old versions

`/home/ubuntu/.local/share/claude/versions/` holds 2.1.227 (291M), 2.1.232 (309M),
2.1.233 (310M), 2.1.234 (314M). `/home/ubuntu/.local/bin/claude` resolves to **2.1.234**,
which is the CLI the operator is actively using RIGHT NOW.

**Proposed:** remove 2.1.227 and 2.1.232 only (~600 MB); KEEP 2.1.233 as a fallback and
2.1.234 as the live version. (A more aggressive option removes 2.1.233 too, for ~910 MB.)

## D. System caches

    sudo journalctl --vacuum-size=200M      # currently 438.8M
    sudo apt-get clean                       # /var/cache/apt currently 192M
    rm -rf /home/ubuntu/.cache/pip           # currently 173M

## E. Explicitly NOT doing

- No `docker volume prune` / `docker volume rm` of any kind.
- No `docker system prune`.
- No deletion of any backup: org1's `/home/ubuntu/copi-backups` (1.4G) is theirs, and an
  audit found our own dumps are each distinct restore points, not redundant.
- No removal of `.venv-test` (scripts/ci.sh aborts without it).
- No removal of the 4 stale TAGGED images (a tag implies intent; owner decision).
- Nothing touching any `copi-python` container, image, volume, or directory.

## Questions the review must answer

1. Is the `.env`-based attribution sound? Could a blackbird-attributed image actually be
   needed, or an UNKNOWN one actually be ours?
2. Does `docker rmi` on those 40 IDs risk removing a layer shared with a tagged/running
   image? (It should refuse, but confirm.)
3. Does `docker builder prune -af` touch anything org1 needs beyond rebuild speed?
4. Is removing CLI versions safe while the CLI is mid-session? Does the running process hold
   open file handles that make deletion unsafe or merely reclaim-deferred?
5. Does `journalctl --vacuum-size` destroy diagnostic history either stack may need?
6. Any ordering hazard, or anything here that is irreversible in a way not stated?
