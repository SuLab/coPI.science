# Cohort System Specification

**Superseded.** This file described cohort system v1, which was never the design
that shipped. It disagreed with the implementation on the migration filename
(`0023_add_cohorts.py` vs the shipped `0022`), the table count (2 vs 3 — it
omitted `cohort_audit_events`), and turn selection (min-heap plus a global
semaphore vs the reactive/proactive weighted selector in
`Simulation._select_agent`). It described none of the mechanisms that shipped:
`cohort_isolation_enabled`, `cohort_default_policy`, thread grandfathering,
gate preflight, topology snapshots, audit events, or reactive scheduling.

**The current specification is [`specs/cohort-system-v2.md`](cohort-system-v2.md).**

31 comments across 16 files in `src/`, `tests/`, `scripts/`, `alembic/` and
`templates/` cite it as `.notes/cohort-system-v2.md §N`, the path it was written
at before it was promoted. **Read those as `specs/cohort-system-v2.md §N`** — the
section numbering is identical, and all 15 distinct cited sections (§2, §4.2, §5,
§5.1, §5.2, §6, §6.2, §7, §8, §9, §10.3, §12, §13.1, §14, §15) resolve to real
headings in the tracked copy.

`.notes/` stays ignored, so the tracked copy under `specs/` is the only one that
ships. If you edit the spec, edit this one.
