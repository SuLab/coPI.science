# Cohort Seeding — Design

**Date:** 2026-08-18
**Status:** Approved, not implemented
**Scope:** Populate three cohorts on the `copi` instance from a tracked manifest;
repoint `/scripps-graph` node selection at one of them. The interaction gate stays
**off**; the running simulation is not touched.

---

## 0. Why

`specs/cohort-system-v2.md` shipped the cohort gate in full — migration `0022`, the
`compute_gates` service, the admin UI, the audit trail — and it has never been used
on `copi`. The `cohorts` table is empty. The only production use is the `blackbird`
instance, which runs 62 star-shaped `hub-<pi>` cohorts under
`policy="isolated"`, seeded by direct SQL with no `created`/`agent_added` audit rows.

Three groupings recur throughout the history and have never been recorded as data:
the Cabo retreat roster, the Schultz alumni/reunion batches, and the Scripps
investigator set. Today they exist only as a deleted `PILOT_LABS` literal, a set of
gitignored TSVs, and a hardcoded Python set. This design turns them into rows.

## 1. Decisions taken

| Question | Decision |
|---|---|
| Purpose | Thematic isolation, **staged** — memberships now, gate later |
| Membership shape | **Literal and overlapping**, faithful to the history |
| Active roster | **Unchanged** at 33 agents |
| `cohort_isolation_enabled` | Stays `False` |
| Source of truth | Tracked manifest + idempotent seed script |

### 1.1 The isolation consequence, stated plainly

All 33 currently-active agents are Scripps- or Calibr-affiliated, so
`scripps-investigators` is a superset of the live roster. Measured with the real
`src/services/cohorts.compute_gates`:

| Active roster the gate sees | Ordered pairs blocked | Unrestricted | Silenced |
|---|---|---|---|
| current 33 agents | **0 / 1056 = 0.0%** | 0 | 0 |
| union of the three cohorts (122 agents) | **6786 / 14762 = 46.0%** | 0 | 0 |

Enabling the flag against today's roster would compute a gate that blocks nothing.
Isolation becomes real only when the historical rosters are reactivated. That is
deliberately **out of scope here** and is recorded in the manifest so the next
person does not have to rediscover it.

A mutually-exclusive alternative (precedence cabo → schultz → scripps) was measured
at 66.3% blocked and rejected: it reduces `scripps-investigators` to a 13-member
residual that no longer means "Scripps investigators".

## 2. The three cohorts

148 membership rows across 122 distinct agents. Names satisfy
`_COHORT_NAME_RE = ^[a-z0-9-]{1,48}$` (`src/routers/admin.py:1389`).

| Cohort | Members | Active | Derivation |
|---|---|---|---|
| `cabo-retreat` | 34 | 15 | `PILOT_LABS` @ `0ef4741` — 20 Scripps + 14 UCSF attendees |
| `schultz-reunion` | 77 | 10 | `newuserlist01–04.tsv` — alumni pilot ∪ reunion attendees |
| `scripps-investigators` | 37 | **33** | `users.institution ~ scripps\|calibr`, frozen 2026-08-18 |

`scripps-investigators` contains **every currently-active agent**. That is not a
mistake in the derivation — it is the fact that makes §1.1 true, and it is why the
gate must stay off.

Overlaps are real and intentional: `cabo ∩ schultz` = 2 (`su`, `lairson`),
`cabo ∩ scripps` = 16, `schultz ∩ scripps` = 10. `su` and `lairson` are in all three.

Five agents belong to no cohort — `azumaya kim moore nomura yeager`, all `inactive`,
all post-Cabo additions from 2026-05-01. Harmless while the gate is off. If
isolation is ever enabled under `policy="open"` while any of them is active, each
becomes a universal bridge (`compute_gates` adds uncohorted agents to every
cohorted agent's mate set). Recorded here so that is a decision, not a surprise.

### 2.1 Schultz scope

`schultz-reunion` is the **union** of the alumni pilot (`newuserlist01/02`, the
Jun 1–4 window) and the reunion attendees (`newuserlist03/04`, the Jun 5–10 window),
not batch 3 alone. Three reasons:

1. Peter Schultz is seeded in `newuserlist02`, not `newuserlist03`. Batch 3 alone
   excludes the reunion host, whom `simulation.py:218` exempts from the Phase-5
   unreviewed-proposal block precisely because he is the host.
2. The empirical Jun 5–10 window has 75 participants against 42 seeded in batch 3 —
   every pilot agent was still active and took part. Jaccard against batch 3 alone
   is 0.52.
3. The two windows share one `simulation_run_id` (`5c69230d`, started 2026-03-29,
   never ended). They were never separate runs.

### 2.2 Known data defects, carried not fixed

These are recorded in the manifest as comments and left for a separate change:

- `hogenesch` — `newuserlist03.tsv` says "Cincinnati Children's Hospital Medical
  Center"; `users.institution` says "Scripps Research Institute". The DB value is
  what puts him in `scripps-investigators`.
- Two seeded reunion attendees have no `AgentRegistry` row and are therefore
  unrepresentable: ORCIDs `0000-0002-1472-0944` and `0000-0002-5974-1562` (named in
  the gitignored `data/cohorts/newuserlist03.tsv`). The second is a surname
  near-collision with the existing `jwang` agent, who is a **different person** —
  resolve by ORCID, never by surname.
- `eppinger` is `pending` with no Slack token; it joins `schultz-reunion` but could
  not post if activated.
- Eight cabo members have an empty `users.institution`: `capra forli grotjahn
  larabell manglik santi seiple ward`.

## 3. The manifest

`cohorts.json`, repo root, alongside the existing tracked `orcids.txt`.

**JSON, not YAML.** PyYAML is importable in the container (6.0.3, transitive) but is
absent from `pyproject.toml`; depending on an undeclared transitive dependency in a
script that mutates production data is not worth the syntax.

**Agent IDs only.** No names, ORCIDs, emails or institutions. `.gitignore:101-103`
puts third-party personal data under the ignored `data/` tree because this repo is
public. Agent IDs are already public in `src/config.py` and `src/routers/public.py`.
Each cohort carries `description` and `source` strings so the derivation is
auditable without shipping the underlying lists.

```json
{
  "_comment": "Seeded by scripts/seed_cohorts.py. cohort_isolation_enabled is
               False: these memberships are recorded, not enforced. On the current
               33-agent active roster the gate would block 0% of pairs because
               scripps-investigators contains all of them. Isolation becomes
               meaningful (46.0% blocked) only if the 122-agent union is activated.",
  "cohorts": {
    "cabo-retreat": {
      "description": "Cabo retreat attendees, Apr 27 - May 7 2026",
      "source": "PILOT_LABS @ 0ef4741",
      "members": ["badran", "briney", "capra", "craik", "echeverria"]
    }
  }
}
```

`members` is the complete list per cohort — 34 / 77 / 37 entries, 148 rows total
across 122 distinct agents. Elided above for readability only.

## 4. The seed script

`scripts/seed_cohorts.py`, following the established `scripts/set_cohort_active.py`
pattern: argparse, `--dry-run` default-off, async engine from `get_settings()`,
plan printed before any write.

Behaviour:

1. **Validate first.** Every `agent_id` in the manifest is resolved against
   `AgentRegistry`. Any unknown ID **aborts the whole run** before writing. This is
   not defensive padding: `compute_gates`' docstring records that a membership row
   naming a non-roster agent still lands in every cohort-mate's allowed-sender set —
   verified live with 56 such memberships. A typo would create a phantom sender that
   no admin screen lists.
2. **Idempotent upsert.** Create missing cohorts; insert missing memberships; leave
   existing rows alone. Running twice is a no-op.
3. **Report, do not prune.** Memberships in the DB but absent from the manifest are
   printed as warnings. `--prune` is required to delete them.
4. **Audit every mutation** via `record_cohort_audit_event` — `created` per cohort,
   `agent_added` per membership. Blackbird's cohorts have no such trail; that is the
   failure mode being avoided.
5. `--dry-run` prints the full plan and writes nothing.

## 5. The `/scripps-graph` fix

`_SCRIPPS` (`src/routers/public.py:94`) serves two unrelated jobs. Only one is wrong.

**Broken — node selection.** Line 632, the `scripps_only` branch, filters agents by
`_SCRIPPS`. The set is stale by nine Scripps/Calibr PIs — `alanjary bollong
chatterjee diercks droujinine good hogenesch mcnamara yliu` — every one of them
invisible on the Scripps graph today. Repoint this at
`cohort_memberships` for `scripps-investigators`.

**Correct — coloring.** `_institution_for` (line 186) uses `_SCRIPPS`/`_UCSF` to
bucket nodes for `/cabo-graph`'s legacy legend. That is a *historical* Scripps/UCSF
split for the Apr 27 – May 7 window and is accurate for it. **Do not change it** —
`/cabo-graph` is a published historical view and repointing its coloring at today's
institutions would silently redraw it.

Consequences to handle:

- On `/scripps-graph` every node is Scripps by definition, so `institution_of` must
  return `"Scripps"` for that view rather than falling through `_institution_for` and
  coloring the nine new agents as `"Other"`. Legend stays empty, color map stays
  `_LEGACY_COLOR_MAP` — the rendered page is unchanged apart from having the right
  nodes.
- `/scripps-graph` must not break before the cohort exists. If the
  `scripps-investigators` cohort is absent, fall back to `_SCRIPPS` and log a
  warning. This makes the route order-independent of the seeding.
- `_cached_graph_payload` keys on kwargs and holds a TTL cache; cohort-backed
  selection makes the payload depend on DB state that can now change between runs.
  The existing TTL is the mitigation; no cache-key change is needed.
- Add a comment at `_SCRIPPS` recording that it is a Cabo-window historical map, not
  the current Scripps roster, so the two jobs cannot be re-conflated.

### 5.1 Not a superset swap — deploying this drops four PIs

Repointing node selection at `scripps-investigators` is **not** a strict superset of
`_SCRIPPS`. It also **drops four PIs who are in `_SCRIPPS` today**: `forli`,
`grotjahn`, `seiple`, `ward`. All four are genuine Scripps/UCSF-window Cabo
attendees, but none of them is in the `scripps-investigators` cohort, because that
cohort was derived from `users.institution ~ 'scripps|calibr'` (§2's derivation
rule) and all four have an **empty** `users.institution` — the same defect §2.2
already records for eight cabo members, connected here for the first time to its
consequence on this specific route.

Measured against the current data: **29 nodes / 105 edges today → 33 nodes / 132
edges after this fix is deployed.** The delta is not a clean "+9 selector
additions, -4 selector drops" arithmetic, because `_build_graph_payload` only
keeps a row in `nodes` when its computed `degree` is `> 0` (`public.py:794`) —
selection into the cohort or into `_SCRIPPS` is necessary but not sufficient to
appear on the rendered graph; a PI must also have at least one in-window edge.
The PI comparing this graph to last week's screenshot will see some familiar
names gone alongside the new ones, not a purely additive change.

Of the nine agents the cohort newly makes eligible for selection, only **seven**
actually gain a rendered node: `bollong chatterjee diercks good hogenesch
mcnamara yliu`. The other two do not render despite being selected, because the
`degree > 0` filter drops them: `alanjary` has zero `thread_decisions` rows at
all, and `droujinine`'s only `thread_decisions` row has `outcome='no_proposal'`
— neither has a qualifying proposal for the edges query to draw a link from, so
neither appears in `nodes`, not even as an isolated dot.

**This membership question — whether `forli grotjahn seiple ward` should be added
back to `scripps-investigators`, or whether their empty `users.institution` should
be fixed instead (§2.2) — is unresolved and pending a decision.** It is not
resolved by this document and `cohorts.json`'s membership lists are not to be
edited to pre-empt it.

**Deployment status:** this fix is committed but **not yet deployed**. The running
`app` container bakes `src/` into the image at build time (§8) rather than
bind-mounting it, so `/scripps-graph` continues to serve node selection from the
stale `_SCRIPPS` set — the pre-fix 29/105 numbers above, not the post-fix 33/132
ones — until the image is rebuilt and the container recreated.

## 6. What does not change

No roster edits. No `.env` change. No `agent-run` restart. With
`cohort_isolation_enabled` false, `_recompute_allowed_sender_ids` takes its
early-return path and every agent's gate stays `None`. Membership rows are read on
the 30-second roster-sync tick, so the app needs no restart either.

## 7. Testing

`tests/integration/test_cohort_seed.py`, TDD:

- the manifest parses and every `agent_id` resolves against `AgentRegistry`;
- seeding twice produces the same row counts (idempotence);
- an unknown `agent_id` aborts before any write;
- `created` and `agent_added` audit events are written;
- `compute_gates` over the seeded topology with `isolation_enabled=False` returns
  `None` for every agent — the gate really is inert;
- `/scripps-graph` selects from the cohort when present and falls back to `_SCRIPPS`
  when absent.

Gate is `./scripts/ci.sh` — alembic sanity, ruff, full pytest with the branch-coverage
floor. Per `CLAUDE.md`, run pytest in the container with an explicit
`TEST_DATABASE_URL` against a scratch database, never `copi`.

## 8. Deployment

Only `profiles/` and `prompts/` are bind-mounted into `copi-python-app-1`, so a new
script and manifest need `docker cp` (or an image rebuild) before they can run.
Sequence: copy in → `--dry-run` → review the plan → apply → verify in
`/admin/cohorts` and `/admin/cohorts/topology` → confirm the gate banner still
reports isolation disabled.

## 9. Out of scope

- Enabling `cohort_isolation_enabled`. Requires recreating `agent-run` (settings are
  `@lru_cache`d) and, to be meaningful, reactivating the 122-agent union.
- Reactivating any agent.
- Fixing the data defects in §2.2.
- `specs/cohort-system-v2.md` §16 step 7 prescribes two cohorts covering the whole
  roster for a first enablement, not three. Revisit that when isolation is turned on.
