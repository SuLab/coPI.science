# Workspace web UI

The shared server-rendered UI has one canonical route set, `/workspace/...`. Role
prefixes are compatibility endpoints, not separate implementations or navigation.

## Effective-role permissions

| Capability | Reviewer | Manager | Admin |
|---|---|---|---|
| PI research, publications, grants, industry evidence and scores, confirmed companies | Yes | Yes | Yes |
| Global assessments, feedback/status, assessment chat | Yes | Yes | Yes |
| PI operational information and all PI management commands | No | Yes | Yes |
| Assignments, Slack bot directory, discussions, activity, prompt-change queue | No | Yes | Yes |
| All-role account/agent management, raw verdicts/opinions, LLM diagnostics, discussion exports | No | No | Yes |

These are capability ceilings, not bypasses of business rules. Feedback edits are
author-only; deletion of another person's feedback is admin-only. Existing profile
versions, lifecycle gates, locks and action-specific impersonation refusals apply.
Chat is refused during impersonation. Email verification on canonical PI pages is
staff-only, PI-target-only and refused during impersonation. The retained admin
verification endpoint supports all user roles under its original safeguards.

Permissions derive from the **effective** user. The real actor is used for the
impersonation banner, Stop and audit attribution, never inherited privileges.
Reviewer assignments remain workflow metadata: `assignment=mine` selects rows
assigned to the effective user but never authorizes access. Default/global access
and access to unassigned assessment details, feedback and chat remain intact.

Reviewer PI pages exclude contact/account/onboarding data, jobs, bot operations,
drafts/revisions, pending companies and the staff prompt queue at the page-data
boundary, not merely through hidden HTML. Industry percentile, raw score, evidence
count remain visible; component breakdowns, score-used tenure, scorer version,
scoring reasons and coverage/job diagnostics are staff-only. Run configuration is
projected to the rubric version label for reviewers, not arbitrary operational JSON.
Safe generated assessment-chat questions
are distinct from the staff prompt-change queue.

## Implementation boundaries

- `src/services/web_permissions.py`: pure effective-role capabilities, also used by
  chat without changing its ordered JSON errors.
- `src/web/identity.py`, `navigation.py`, `page_context.py`: safe identity/chrome and
  shared navigation/context. Server dependencies remain authoritative.
- `src/web/urls.py`: relative same-app named routes and feature-specific return-state
  allowlists, including repeated discussion filters.
- `src/web/presentation.py`: recursive typed/allowlisted page records. Internal
  service models and chat builders keep their existing contracts.
- `src/services/directory.py`: lean research/account loaders and batched query models;
  assignment SQL scope is applied before limits/counts, without duplicate joins.
- `src/routers/workspace/`: feature-owned PI directory/profile/evidence/bot commands
  and assessment/activity/discussion/prompt pages.
- `src/routers/manager.py`: guarded GET adapters and the same canonical POST
  callables. All 28 old POST suffixes are retained without pre-execution redirects.
- `src/routers/admin/`: retained admin features and guarded shared-page adapters.
- `templates/workspace/`, neutral feature partials and assessment assets: one UI
  per shared page. `templates/pis/_bot_action.html` shares PI bot lifecycle controls
  between PI detail and the bot directory; account/full-agent pages remain separate.

## Compatibility contracts

Legacy GETs validate authorization, UUIDs and resources before redirecting. Existing
admin registrations remain. PI admin account URLs return to canonical PI detail's
`#account`; non-PI account pages do not query unused PI enrichment tables. All-user
and all-agent registries remain admin-only with their existing controls.

`/admin/agents/slack/callback` keeps its registered URL and completes initiator/state
validation and token exchange before returning. `/admin/impersonate/stop` remains
available under every effective role; there is no blanket admin-router gate.

Legacy admin discussion exports preserve non-empty truthiness (including `false`),
the `html` format branch, uncapped rows and historical proposal reviews. Legacy
manager exports remain ignored; canonical non-empty exports are admin-only.
`/reviews/...` and `/assessment-chat/...` remain role-neutral.

## Verification

Use the unweakened `scripts/ci.sh` gate and the isolated browser harness documented
in `tests/e2e/ui_audit/README.md`. Consolidation regressions are in
`test_workspace_contract.py`, `test_workspace_data_contract.py`,
`test_workspace_permissions_matrix.py`, `test_workspace_presentation.py` and
`test_workspace_browser_oracles.py`. Browser phases 0–4 protect earlier workflows;
phase 5 checks canonical routes, reviewer privacy/scores, assignment/global access,
feedback return state, fragments, exports and effective-role impersonation in
Chromium and Firefox. Local image verification checks tracked source identity and
real image-backed HTTP behavior against a disposable database, without running a
worker, simulation or paid external API.
