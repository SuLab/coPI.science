# Web UI browser harness

Reproduces the 2026-10-01 web UI audit (`docs/audits/2026-10-01-web-ui/`) against a
throwaway local instance and gates each remediation phase (spec §9).

```bash
# once per machine: a browser for Playwright (about 150 MB)
.venv-test/bin/python -m playwright install chromium-headless-shell
# Phase gate: crawl + the phase's journeys; exit 1 on any failure
.venv-test/bin/python -m tests.e2e.ui_audit.run all --phase 0
# try Firefox too (B-01); report it as unverified if the download fails
.venv-test/bin/python -m playwright install firefox && \
  .venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0 --browser firefox
# keep an instance up for manual browsing (prints base URL, seed ids, secret)
.venv-test/bin/python -m tests.e2e.ui_audit.run serve
```

What it touches: one Docker container `uiaudit-pg-<pid>` (postgres:15, 127.0.0.1, removed
at exit), one local uvicorn on a free port, a temp working directory with its own empty
`profiles/` (profile exports and deletions never reach the repo's `profiles/`, which is the
live agent's directory). It never reads the
repo's `.env`, sends no email (allowlist set to an undeliverable address), cannot reach
AWS credentials, has no Slack token, and answers the assessment chat from
`tests/fakes.FakeAsyncAnthropic`. It needs network once for the axe-core bundle from
jsDelivr and, until Phase 1 vendors them, for the CDN scripts the pages load.
