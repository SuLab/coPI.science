# Audit of GitHub issues #20–#27 (author: ahueb) vs copi-prod @ 18ba52c — 2026-09-02

Files:
- issues/issue_NN.md            — full issue text as fetched from GitHub (no comments exist on any of them)
- findings/issue_NN.md          — first-wave verification report (one agent per issue; every sub-claim, evidence, snippets run)
- findings/issue_NN_redteam.md  — second-wave adversarial report attacking every first-wave verdict
- ci/ci_run2.log                — full ./scripts/ci.sh output (MIGCHECK_PORT=55433): CI passed, 2030 passed / 120 skipped, 69.27 %

Repo facts: branch copi-prod, HEAD 18ba52c, clean tree, == origin/copi-prod. main is stale at b7edcbc (the original
audit baseline). copi-prod contains merged PRs #30, #31, #32, #36 and 52 later commits (backup tooling, cohort seeding).
No commit since the issues' re-verification tip b1d54da references any of #20–#27 or any COR-/PR item.
