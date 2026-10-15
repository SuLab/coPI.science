# Web UI browser harness

Reproduces the 2026-10-01 web UI audit (`docs/audits/2026-10-01-web-ui/`) against a
throwaway local instance and gates each remediation phase (spec §9).

```bash
# once per machine: a browser for Playwright (about 150 MB)
.venv-test/bin/python -m playwright install chromium-headless-shell
# Phase gate: crawl + the phase's journeys; exit 1 on any failure
.venv-test/bin/python -m tests.e2e.ui_audit.run all --phase 0
# Consolidation gate: all old phases first, then canonical workspace phase 5.
for phase in 0 1 2 3 4 5; do
  .venv-test/bin/python -m tests.e2e.ui_audit.run all --phase "$phase" --browser chromium \
    --out "/tmp/workspace-ui-phase${phase}.json" || exit 1
done
.venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 5 --browser firefox \
  --out /tmp/workspace-ui-firefox.json
# Help tab and onboarding (phase 6): capture help_assets/shots/ first
# (tests/e2e/help_shots/README.md), or every help page with a figure fails
.venv-test/bin/python -m tests.e2e.ui_audit.run all --phase 6 --browser chromium \
  --out /tmp/workspace-ui-phase6.json
# try Firefox too (B-01); report it as unverified if the download fails
.venv-test/bin/python -m playwright install firefox && \
  .venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0 --browser firefox
# keep an instance up for manual browsing (prints base URL, seed ids, secret)
.venv-test/bin/python -m tests.e2e.ui_audit.run serve
```

Where to run it: the production host lacks Chromium's system libraries (`libatk-1.0`, …)
and packages are not installed there, so run it from a workstation that mounts the checkout,
with an environment that has the project and `playwright` installed, passing
`--executable` to a local `chrome-headless-shell`. The same flag selects a real local
Firefox binary when `/usr/bin/firefox` is only a Snap launcher. Check the installed
browser rather than relying on a package revision or a path existing.

Phase 5 compares console, page-error, network and narrow-layout regressions with an
explicit baseline report (`--baseline`). It also requires **zero current WCAG axe
findings and no axe-tool errors**: aggregate baseline counts cannot prove which
nodes failed, and therefore cannot safely waive a newly affected node. Phase 4's
persona-file audit is mandatory and affects the exit status. Phases 1 and 2 include
their 1b and 2b journey groups. Intentional permission/resource negatives are classified
by role, method and exact target separately; asset failures and positive-path 404s
are never waived.

Phase 6 (`journeys_phase6.py`, spec `docs/specs/2026-10-14-help-and-onboarding-design.md`
§10.3) covers the Help tab: the reviewer and manager tutorials walked from the
first-sign-in prompt to Finish, every workspace page's "Help with this page" link landing
on its anchor, "Don't show again" returning to the same URL, and every help page, tutorial
step and prompt-bearing workspace page at 375 px with no horizontal scroll and zero axe
findings. Each journey creates its own accounts, so their order does not matter. The
seeded manager and reviewer have no dismissal row, so phases 0–5 now render the prompt on
their workspace pages, where it must add nothing to any report.

The Help change's phase-5 baseline is the report of the same command at `3bafb292`, the
commit before it. Produce it from a separate `git worktree` or export of that commit,
never by checking out the mounted checkout (it holds the uncommitted production compose
edit). Then the feature commit's phase-5 report must be **equal** to it on
`crawl.violations`, `crawl.overflow_375` and `crawl.axe`: run.py fails only on additions,
so also compare the three by hand. Nothing new is ever baselined: a difference caused by
the prompt, the Help nav item or a relabelled control is a defect to fix.

What it touches: one Docker container `uiaudit-pg-<pid>` (postgres:15, 127.0.0.1, removed
at exit), one local uvicorn on a free port, a temp working directory with its own empty
`profiles/` (profile exports and deletions never reach the repo's `profiles/`, which is the
live agent's directory). It never reads the
repo's `.env`, sends no email (allowlist set to an undeliverable address), cannot reach
AWS credentials, has no Slack token, and answers the assessment chat from
`tests/fakes.FakeAsyncAnthropic`. It needs network once for the axe-core bundle from
jsDelivr and, until Phase 1 vendors them, for the CDN scripts the pages load.
