# Open findings register

Every audit finding that is not closed in the same change lives here, one row
each, until it is `fixed`, `refuted` or `deferred` by a named decision. An
audit's own prose is not a tracking system: S1 (C2 below) was rated CRITICAL on
2026-08-17 and was still open five weeks later because nothing listed it
(RCA `docs/audits/2026-09-24-comment-cleanup-rca/README.md`, §8 cause 6).

**Rules.**
- A new audit appends its findings here **in the same commit** as the audit.
- Closing a finding means setting `status` and `evidence` in the commit that
  closes it. Do not delete rows.
- `id` is `<audit date>/<finding id>`.
- `status` is one of `open`, `fixed`, `refuted`, `deferred`.
- `evidence` is a commit hash or a repo `path:line` that shows the status. A
  `fixed` row must cite at least one backticked commit or `path:line`, and each
  must resolve (`tests/unit/test_open_findings_register.py`). Host measurements
  are quoted with their date.
- `owner / decision` names a person, a decision ID (the `D` numbers of
  `docs/plans/2026-09-25-rca-remediation-plan.md` §2) or "owner". `open` and
  `deferred` rows must name one.
- No cell may contain a `|` character, and no row may quote a secret.
- **This file lists open weaknesses of the live deployment, and `origin` is public
  (L9).** Keep row evidence general: no paths to dumps, no host-key details, no
  counts or names an attacker could use. Do not push this branch until C2 and the
  SSH item are closed, or scrub the audit records it cites first.

| id | source | severity | status | evidence | owner / decision |
|---|---|---|---|---|---|
| 2026-08-17/C1 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:30` | critical | open | Host 2026-09-25: the root filesystem is below 80% used (it was 96% on 2026-08-17), and both Postgres volumes still live on it. | D1, D2 (reclaim after deploy); owner (disk policy) |
| 2026-08-17/C2 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:63` | critical | open | This is RCA S1. Fixed in the tree by P1 (`Dockerfile:21`, `.dockerignore:14`); set `fixed` with the landing commit once plan §7 step 6 verifies the built images and D1 removes the pre-fix ones. | P1, plan §7, D1, D3 |
| 2026-08-17/H3 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:101` | high | open | Host 2026-09-25: branch `blackbird` is 5 commits ahead of `origin/blackbird`, so the deployed commit exists on no remote. | owner |
| 2026-08-17/H4 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:125` | high | open | Per-deploy rollback tags are taken today. Closes when plan §7 tags `rollback-post-rca` from the running images and D1 retires the pre-fix tags; the procedure is `docs/plans/2026-09-25-rca-remediation-plan.md:2550`. | owner; D1 prunes the pre-fix tags |
| 2026-08-17/H5 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:144` | high | open | Host 2026-09-25: no scheduled dump or backup job exists, and every dump sits on this host's own disk. No offsite copy. | owner |
| 2026-08-17/M6 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:156` | medium | fixed | Normal operation no longer uses the one-off container: the supervisor service replaces it (`CLAUDE.md:457`). Host 2026-09-25: no `blackbird-agent-run` container exists. The CLI emergency path can still leave one behind; plan §7 step 11 removes any exited one. | owner |
| 2026-08-17/M7 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:196` | medium | open | Kept uncommitted by decision on 2026-09-25; the committed file still names the web service `app` (`docker-compose.prod.yml:25`). Same item as 2026-09-25/D5. | D5 |
| 2026-08-17/M8 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:221` | medium | open | Host 2026-09-25: the running nginx and certbot belong to the other deployment on this host. This repo's `nginx`/`certbot` services (`docker-compose.prod.yml:112`, `:146`) do not run. | owner |
| 2026-08-17/L9 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:246` | low | open | `origin` is the public `git@github.com:SuLab/coPI.science.git` (host 2026-09-25), and the published deploy log is still tracked (`docs/audits/2026-08-17-user-account-types/deploy-log.md:1`). This register is public too once pushed. | owner |
| 2026-08-17/L10 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:260` | low | open | Host 2026-09-25: the working-tree compose file has healthchecks for postgres (`:11`), blackbird-app (`:54`) and nginx (`:149`), none in the worker service (`:67-99`). The committed file's worker (`docker-compose.prod.yml:59`) has none either. | owner |
| 2026-08-17/L11 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:268` | low | open | Host 2026-09-25: a non-production database still lives on the production Postgres instance. | owner |
| 2026-08-17/L12 | `docs/audits/2026-08-17-user-account-types/final-infra-audit.md:274` | low | open | Host 2026-09-25: `profiles/` and `data/` are root-owned (`backups/` is ubuntu-owned). The `profiles/private/blackbird.md` half is closed: archived on 2026-08-20 (`CLAUDE.md:1850`). | D4 (non-root user needs the chown); owner |
| 2026-09-25/D4 | `docs/plans/2026-09-25-rca-remediation-plan.md:62` | medium | deferred | S1's second half: the images still run as root (the final stage, `Dockerfile:28`, sets no `USER`). Needs a fixed UID, a chown of host `profiles/` and `data/`, and an inventory of write paths. | D4 |
| 2026-09-25/D5 | `docs/plans/2026-09-25-rca-remediation-plan.md:63` | medium | open | RCA §8 cause 5 (`docs/audits/2026-09-24-comment-cleanup-rca/README.md:393`): `blackbird-app` exists only in the uncommitted compose file, so no test can check committed text against the deployed service names. Open by decision. | D5 |
| 2026-09-25/D17 | `docs/plans/2026-09-25-rca-remediation-plan.md:75` | low | deferred | Existing bots keep the scopes they were installed with until reinstalled (`src/services/slack_provisioning.py:27`). Host 2026-09-25: the active and most inactive agent rows hold such tokens. The reinstall procedure is undetermined. | D17 |
| 2026-09-25/R-slack-scope-check | `docs/plans/2026-09-25-rca-remediation-plan.md:2642` | low | fixed | Checked against Slack's method reference before fan-out (D13): no called method needs `groups:read`; `auth.test`, `auth.revoke` and `chat.getPermalink` need no scope (`src/services/slack_provisioning.py:26`). | D13 |
| 2026-09-25/R-I9 | `docs/plans/2026-09-25-rca-remediation-plan.md:2644` | low | open | Production-data question (RCA §9): threads with more than one decision row. Unmeasured. | owner |
| 2026-09-25/R-I13 | `docs/plans/2026-09-25-rca-remediation-plan.md:2644` | low | open | Production-data question (RCA §9): legacy `outcome='proposal'` rows. Unmeasured. | owner |
| 2026-09-25/R-I19 | `docs/plans/2026-09-25-rca-remediation-plan.md:2644` | low | open | Production-data question (RCA §9): how many historical `private` / `slack_dm` revision rows exist. Unmeasured. | owner |
| 2026-09-25/R-I16 | `docs/plans/2026-09-25-rca-remediation-plan.md:2644` | low | open | Production-data question (RCA §9): superseded rows with a NULL `raw_verdict`. Unmeasured. | owner |
| 2026-09-25/R-hub-compliance | `docs/plans/2026-09-25-rca-remediation-plan.md:2648` | low | open | Hub compliance with the fixed scout_hub prompt set (1.7.1, now 1.8.0) is unmeasured until a run. | owner |
| 2026-09-25/R-I35 | `docs/plans/2026-09-25-rca-remediation-plan.md:2650` | low | open | org1's `agent-run` StopTimeout is org1's to set; recorded only. | org1 (owner to notify) |
| 2026-09-25/R-I34 | `docs/plans/2026-09-25-rca-remediation-plan.md:2651` | low | deferred | This repo's nginx is not live: on 2026-09-25 no `copi-blackbird` nginx container exists. Re-check if the project ever adds one. | owner |
| 2026-09-25/R-I22 | `docs/plans/2026-09-25-rca-remediation-plan.md:2653` | low | deferred | Commits `3dcc00a..c62af04` do not import; history cannot be fixed. Skip them when bisecting. | owner |
| 2026-09-25/R-dead-code | `tests/unit/test_no_dead_src_symbols.py:73` | low | open | The P15 dead-code scan of the integrated tree (2026-09-25) found 13 definitions in `src/` with no caller outside `tests/`, none named by the RCA: four in `src/agent/channels.py`, two in `src/agent/ids.py`, and one each in `src/agent/simulation.py`, `src/routers/profile.py`, `src/services/llm.py`, `src/services/pi_inbox.py` and `src/services/validators.py`, plus two in `src/services/patents.py`. Each is allowlisted with its reason in the guard. Delete them (with their tests) and drop the entries. | owner |
| 2026-09-25/R-admin-token-form | security re-audit of the RCA remediation, 2026-09-25 | low | open | The admin agent-edit form stores any string as the bot token (`src/routers/admin.py:1118`, `slack_bot_token.strip() or None`), so a user token pasted there is kept. PI deletion no longer revokes such a value (`src/services/user_deletion.py:189`), but the engine would still try to use it. Validate the field with `is_valid_token`. | owner |
| 2026-09-25/R-enrichment-fixwave | independent architecture audit (session d9), 2026-09-25 | medium | open | The final fix wave of `feat/pi-external-enrichment` (0fb613c..1000f89) never reached `blackbird`, and `merge_grant_titles` does not exist. `src/services/grant_enrichment.py:123` REPLACES `grant_titles` with the RePORTER-derived titles, while CLAUDE.md says the enrichment "supplements (never replaces)" the ORCID-fundings seed. Merge that wave as its own change, or correct CLAUDE.md. | owner |
| 2026-09-25/R-openalex-burst | RCA remediation deploy, 2026-09-25 | low | open | `company_funder_ids` sends one unpaced OpenAlex `/institutions` request per funder in a tight loop (`src/services/industry_sources/openalex_industry.py:113`), and the worker retries a failed job at once, with no backoff. So a PI with many funders trips OpenAlex's rate limit, and all three attempts 429 within seconds. Barbara Slusher's `industry_evidence` job went `dead` twice on 2026-09-25, and her industry score is unscored until then. It is manager-only and does not gate activation. Pace the loop (or batch the ids) and back off on 429. | owner |
| 2026-09-25/R-ssh-host-key | `docs/audits/2026-09-24-comment-cleanup-rca/README.md:414` | medium | open | An SSH host-key discrepancy was observed on 2026-09-24 (RCA §10). Verify the host key out of band before trusting any new one. | owner |
