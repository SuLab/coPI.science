# Help screenshots

The Help pages' figures (`templates/help/`, `{{ figure(request, "<id>", …) }}`) are PNGs in
`help_assets/shots/`, described by `help_assets/shots/manifest.json`
(spec `docs/specs/2026-10-14-help-and-onboarding-design.md` §8). This directory makes them.

| File | What it is |
|---|---|
| `shots.py` | The shot list: id, audience, the seeded account it is taken as, the URL, the steps that open a disclosure or the chat drawer, the element to clip to, the alt text and the templates it shows. |
| `seed.py` | The demo data: invented people, labs and companies only. |
| `serve.py` | The app with a fake assessment-chat model and stubbed outbound fetchers. |
| `capture.py` | Takes the shots, or checks that the committed ones are current. |

## Prerequisites

- **Docker**, for the throwaway `postgres:15` the stack migrates and seeds.
- **A Chromium headless shell**:
  `.venv-test/bin/python -m playwright install chromium-headless-shell`, or pass
  `--executable` to a local `chrome-headless-shell`.
- **A venv with the project's dependencies**, including the `[dev]` extra (Playwright).

Run it on a **workstation**, not on the production host: the host lacks Chromium's system
libraries and packages are not installed there (the same constraint as
`tests/e2e/ui_audit/README.md`). The checkout may be a mount of the host; the tool writes
only `help_assets/shots/`.

## Commands

```bash
# Capture every shot (rewrites manifest.json, removes PNGs no shot names)
.venv-test/bin/python -m tests.e2e.help_shots.capture --executable /path/to/chrome-headless-shell
# Recapture some shots; every other manifest entry is kept as it is
.venv-test/bin/python -m tests.e2e.help_shots.capture --only chat-drawer --only staff-preview
# Which shots are stale or missing? No Docker, no browser; exit 1 if any
.venv-test/bin/python -m tests.e2e.help_shots.capture --check
```

A shot is stale when any file it depends on changed: the depicted templates and everything
they extend, include or import (`base.html` too), `static/css/app.css`, the rubric,
`seed.py`, or the shot's own entry in `shots.py` (its fields and the source of its `path`
and `prepare` functions). `--check` is not part of `scripts/ci.sh`;
`tests/unit/test_help_shots.py` is, and it checks the PNGs against the manifest, the
templates and `shots.py`.

Each shot is a 1280×900 viewport, or the element named by its `clip`; never a full-page
capture. The PNGs together must stay under 4 MB, so give a shot of a long page a `clip`.
`all`-audience shots are taken as a seeded **reviewer** (the prompt shot as `new_reviewer`,
which has not answered the first-sign-in prompt), `staff` shots as a seeded **manager**.

## Outbound recorder

`serve.py` stubs every outbound fetcher a shot can reach (its docstring lists them). As a
second line, `capture.py` starts a local listener on `127.0.0.1` and points `HTTP_PROXY`,
`HTTPS_PROXY` and `ALL_PROXY` at it in the serve process's environment only. The listener
records every connection attempt and answers none. If it saw any, the capture prints them,
writes nothing, and exits 1: a missed stub would otherwise show up only as a quietly
degraded page (for example the Staff page's "fetch failed" refusal) in a committed shot.

Like the browser harness, the stack never reads the repository's `.env`, sends no email,
cannot reach AWS credentials and has no Slack token.

## When to run it

Capture is the **last step before a deploy** that changes a depicted page, the stylesheet,
the rubric or the seed: run `--check`, recapture what it lists, and review the new PNGs.
Commit the PNGs and `manifest.json` **together**, in one commit; the manifest records the
commit each shot was taken at.
