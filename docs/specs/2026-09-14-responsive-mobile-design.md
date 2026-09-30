# Responsive (mobile-through-desktop) web UI — design

Date: 2026-09-14. Status: approved in brainstorming; adversarial audit findings folded in (see "Audit corrections").

## Goal

Every page rendered from `templates/` (except the excluded standalone
visualizations) is fully usable on screens from 320 CSS px wide through desktop,
adapting automatically by viewport width. Desktop keeps the current visual
identity with a light consistency refresh. Tailwind moves from the v3 Play CDN to a
vendored, compiled Tailwind v4 stylesheet. Mobile correctness is enforced by
Playwright structural assertions in the existing `./scripts/ci.sh` gate.

## Decisions (from brainstorming, 2026-09-14)

| Question | Decision |
|---|---|
| Scope | All pages first-class, admin included. `cabo_graph.html`, `static/talk_viz/`, `data/**/*.html` excluded. |
| Tailwind | Vendor compiled **v4.3.3** via the standalone Linux binary (no Node). Drop the Play CDN. |
| Interactivity | CSS-first (`<details>`, `<dialog>`); vanilla JS only for Escape/outside-tap close and scroll-into-view. No Alpine, no htmx. |
| Design | Same look, light refresh: shared Jinja macros normalize spacing, type, buttons, cards, tables. |
| Devices | 320 px and up. Test matrix 320 / 390 / 768 / 1280. |
| Verification | Playwright structural assertions in CI. Screenshots written for human review, never compared or committed. |
| Delivery | One branch, staged commits, single merge. |
| Approach | A: component macro layer + per-page migration pass. |

## Evidence base (fetched 2026-09-14)

- Tailwind v4.3.3 current; Play CDN "not intended for production"; standalone CLI
  binaries published per release; container queries in core; default breakpoints
  sm 640 / md 768 / lg 1024 / xl 1280 / 2xl 1536; browser floor Safari 16.4,
  Chrome 111, Firefox 128. (tailwindcss.com/docs: play-cdn, tailwind-cli,
  responsive-design, compatibility, upgrade-guide)
- WCAG 2.2 SC 2.5.8 (AA) 24×24 px targets; 2.5.5 (AAA) 44×44. Apple HIG 44 pt,
  Material 48 dp. SC 1.4.10 Reflow at 320 px, data tables excepted. SC 1.4.4 text
  to 200 %. `maximum-scale`/`user-scalable=no` is a WCAG failure. (w3.org/WAI,
  MDN viewport meta)
- Responsive tables: keep `<table>` semantics inside a focusable, labelled scroll
  region; CSS card-stacking breaks screen-reader semantics. (Roselli 2020, updated
  2025-09)
- `<details>` is the zero-JS disclosure primitive; `<dialog>` is the only primitive
  with free focus trapping; Popover API is Baseline since 2025-01. (MDN)
- `svh`/`dvh` supported 95 %; `env(safe-area-inset-*)` needs `viewport-fit=cover`;
  `interactive-widget` not in Safari, progressive enhancement only. (MDN, caniuse)
- Playwright **Python has no `to_have_screenshot`**; screenshot comparison is a
  Node test-runner feature. `page.screenshot(animations="disabled")` and
  `reduced_motion="reduce"` for determinism. (playwright.dev/python)
- Core Web Vitals: LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1. (web.dev/vitals)

## Current state (measured 2026-09-14)

- FastAPI + Jinja2, 37 templates (31 extend `base.html`), 6 220 lines. **Each of the
  eight routers builds its own `Jinja2Templates(directory="templates")` at import**
  (`admin.py:54`, `public.py:31`, `agent_page.py:35`, `invite.py:17`, `auth.py:21`,
  `profile.py:18`, `settings.py:22`, `onboarding.py:23`); there is no shared env or
  globals, and template context is assembled by five copies of `_template_context`. Tailwind v3 Play CDN
  (`cdn.tailwindcss.com`) loaded in `base.html`, `unsubscribe.html`,
  `cabo_graph.html`. No JS framework. `static/js/markdown.js` is the only owned JS.
- Only `landing.html` has meaningful responsive utilities (29). Header nav is a
  fixed horizontal row (4 links + name + Sign out) with no collapse. Admin has 21
  `<table>` elements in 12 pages, no scroll wrappers. Four class attributes build
  Tailwind class names by interpolation (`admin/discussions.html:58,60,121`,
  `admin/jobs.html:19`, pattern `text-{{ color }}-600`); they only work because the
  Play CDN scans the live DOM. Live check of copi.science at
  390 px: landing renders acceptably; authenticated pages untested (no login path).
- `.gitignore` ignores `static/` wholesale; `static/js/markdown.js` and one PNG are
  force-tracked. The Dockerfile `COPY . .` copies whatever is in the build context;
  `.dockerignore` does not exclude `static/`. Dev `docker-compose.yml` bind-mounts the
  repo root over `/app`; prod mounts only `profiles/`, `prompts/`, `data/`. nginx
  proxies everything to `app` with no `/static` alias, so no host copy is needed.
- `flash_message` is referenced only in `base.html`; no router sets it (dead
  branch). `impersonation_banner` is set by profile, onboarding, settings routers.
- Test venv `.venv-test` has Playwright 1.62 with Chromium 1234 installed.
  `tests/e2e/session.py::forge_session_cookie` forges a valid session cookie.
  `tests/conftest.py` provides a testcontainers Postgres engine and an httpx ASGI
  client; there is no HTTP server fixture yet.
- v3→v4 impact in templates: ~71 `shadow-sm`, ~30 bare `rounded`, 16 `ring`, 6
  `outline-none`, 15 `flex-shrink-0`, 14 `flex-grow`, 14 bare `border`/`divide`
  without an explicit color.

## Section 1 — Foundation: Tailwind v4 build pipeline

**Source (tracked):** `assets/css/app.css` (new directory; `static/` is
gitignored, so the input cannot live there).

```css
@import "tailwindcss" source(none);
@source "../../templates";
@theme { /* default breakpoints kept; brand tokens: --color-brand-* = indigo scale */ }
@layer base {
  /* v3 compat: default border/divide color */
  *, ::after, ::before, ::backdrop, ::file-selector-button {
    border-color: var(--color-gray-200, currentColor);
  }
  @media (prefers-reduced-motion: reduce) {
    *, ::before, ::after { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
  }
}
```

Component classes live in macros as utilities; `@layer components` only for
selectors macros cannot carry (e.g. sticky first-column shadow, `.prose-md`
markdown rules currently inline in `dashboard.html`).

**Build:** Dockerfile gains a stage that downloads
`tailwindcss-linux-x64` for the pinned release tag, verifies a pinned sha256,
and runs `tailwindcss -i assets/css/app.css -o static/css/app.min.css --minify`.
The runtime stage copies `static/css/app.min.css`. Build fails closed on checksum
mismatch or download failure. `scripts/build-css.sh` runs the same command on the
host (downloads the binary to `.cache/` if absent) with an optional `--watch`.
`static/css/app.min.css` stays gitignored.

**Shared templating (prerequisite):** new `src/templating.py` exporting one
`templates = Jinja2Templates(directory="templates")` with
`templates.env.globals["css_version"]` set at import to the sha256 prefix of
`static/css/app.min.css` (or `"missing"` with a logged error if absent). All eight
routers import this object instead of constructing their own. No behaviour change
otherwise; the five `_template_context` copies are left alone (out of scope).

**Wiring:** `base.html` drops the CDN `<script>` and the `[x-cloak]` rule; adds
`<link rel="stylesheet" href="/static/css/app.min.css?v={{ css_version }}">`.

**Safelist for interpolated classes:** the four `text-{{ color }}-600`-style sites
are rewritten so each status maps to a complete class string in the template
(`{% set tone = {'ok': 'text-green-600', ...} %}`) — no `@source inline()`
safelist, so the compiled CSS contains only classes that appear literally.

**Docker ordering:** `.dockerignore` gains `static/css/` so a stale host build can
never enter the image; the built CSS is copied *after* `COPY . .` and after the
`RUN mkdir -p ... static` line.

**Dev and host runs:** because dev compose mounts the repo over `/app`, the image
CSS is shadowed; `scripts/build-css.sh` must be run on the host for dev and for
the Playwright tier. The tier (Section 5) fails, not skips, when the built file is
absent, so unstyled HTML can never pass the layout assertions.
`unsubscribe.html` and `admin/discussions_export.html` get the same link (both are
standalone documents outside `base.html`). `cabo_graph.html` keeps its CDN script
(excluded page, unchanged).

**Migration v3→v4:** run the official upgrade tool once over `templates/`, review
the diff, then hand-fix the 14 bare `border`/`divide` sites (add `border-gray-200`
where the compat rule is insufficient) and check the renamed utilities. Inline
`<style>` blocks (landing carousel, dashboard markdown) are plain CSS and unaffected.

**Verify:** new fast pytest module `tests/unit/test_static_assets.py`: no template
under `templates/` except `cabo_graph.html` references a Tailwind CDN URL; the
viewport meta has no `maximum-scale` or `user-scalable`; when
`static/css/app.min.css` exists it is < 60 KB gzipped. Desktop 1280 screenshots
before/after for human review of v4 drift.

## Section 2 — Layout shell and navigation (`base.html`)

- Viewport meta: `width=device-width, initial-scale=1, viewport-fit=cover`.
- Header becomes `<header><nav aria-label="Primary">`. At `md` and up the layout
  is unchanged. Below `md`: brand + a `<details class="md:hidden">` whose
  `<summary aria-label="Menu">` is a 44×44 hamburger; the panel is a full-width
  block under the header with links stacked at ≥ 44 px height, user name and
  Sign out at the bottom. No hand-rolled ARIA (the UA exposes the disclosure
  state). One script (≤ 15 lines) closes the panel on Escape and on outside tap.
  Active link gets `aria-current="page"`. The agent badge renders inline after the
  label on mobile.
- Admin sub-nav (8 links): below `lg` it becomes a horizontally scrolling strip
  (`overflow-x-auto`, `whitespace-nowrap`, scroll-snap, edge gradient hint) and a
  3-line script scrolls the active tab into view on load.
- Impersonation banner wraps to two rows below `sm`; button ≥ 44 px tall. The
  unused flash block gets the same treatment but is not tested (no router sets it).
- `<main>` keeps `px-4 sm:px-6 lg:px-8`, uses `py-6 md:py-8`. `<body>` gets
  `padding-bottom: env(safe-area-inset-bottom)`. Footer text wraps.
- Hover-only affordances rely on Tailwind v4's `hover:` variant, which already
  gates on `@media (hover: hover)`.
- Out of scope: bottom tab bar, PWA manifest, dark mode.

## Section 3 — Component macros and data tables

New file `templates/_components.html`, imported `with context`. Macros:

| Macro | Behaviour |
|---|---|
| `page_header(title, subtitle=None)` + caller slot | `flex-col gap-3 sm:flex-row sm:items-center sm:justify-between`; actions wrap; h1 never truncates; `text-2xl md:text-3xl`. |
| `card(padding='md')` + caller | `rounded-xl border border-gray-200 bg-white shadow-xs`. |
| `stat_grid()` / `stat(value, label, tone)` | `grid-cols-2 lg:grid-cols-4`. Replaces `admin/agents.html` `grid-cols-4`. |
| `button(label, variant, size='md', type='button', href=None, full=False)` | variants primary/secondary/danger/ghost; `min-h-11` (44 px) on touch via `[@media(hover:none)]:` variant; `full` → `w-full sm:w-auto`. |
| `field(label, name, type='text', value='', help=None, error=None, **attrs)` | stacked label; `text-base` on mobile (prevents iOS zoom below 16 px); passes `inputmode`, `autocomplete`, `required`. |
| `badge(text, tone)` | pill. |
| `data_table(caption, id)` + caller for `<thead>/<tbody>` | `<div role="region" aria-labelledby="{id}-cap" tabindex="0" class="overflow-x-auto rounded-xl border focus-visible:ring-2">` around `<table class="min-w-full text-sm">` with `<caption id="{id}-cap" class="sr-only">`. First `th`/`td` sticky-left with background and a shadow once scrolled. Real table semantics kept; no CSS card-stacking. |
| `tag_pill(text, name)` | remove button ≥ 24 px visual, 44 px hit area via padding. |
| `disclosure(summary)` + caller | `<details>` accordion. |

Rules applied during migration:

- Existing `hidden md:table-cell` column hiding is kept only where the row links
  to a detail page containing the hidden data; otherwise the column stays and the
  region scrolls.
- Rows with `onclick="location.href=..."` get a real `<a>` in the first cell that
  covers the row (`after:absolute after:inset-0`), so keyboard and tap work and the
  inline `onclick` is removed.
- JS toggles that carry state (dashboard review/reopen tabs) stay as JS; pure
  show/hide accordions become `disclosure`.
- No fixed px widths except icons/avatars; `max-w-*` only on containers.
- Every interactive control ≥ 24×24 always and ≥ 44 px tall at phone widths.
- Long tokens (ORCID, emails, URLs, JSON) get `break-all` or `truncate` + `title`.

## Section 4 — Page migration groups

Groups touch disjoint files and can be implemented in parallel.

- **G1 Public/auth (6):** `landing`, `login`, `access_pending`, `unsubscribe`,
  `invite/accept`, `invite/error`. Landing: carousel slide width `78%` →
  `clamp()` so arrows stay inside the viewport at 320; `.term` tooltips become
  tap-to-toggle (`<details>`-free: `:focus-within` shows the tip, `tabindex="0"` on
  the term) under `(hover: none)`; hero `min-h-svh`.
- **G2 Researcher profile (6):** `profile/view`, `profile/edit`,
  `profile/delete_account`, `onboarding/profile_review`,
  `onboarding/private_profile`, `settings`. Tag editors → `tag_pill`; 2-col grids
  → 1 col below `md`; settings toggle rows stack below `sm`.
- **G3 Agent (7):** `agent/dashboard`, `conversations`, `public_profile`,
  `listing`, `profile`, `request`, `_thread_replies`. Dashboard header →
  `page_header`; rating radio grid 2-col at 320 (`grid-cols-2 md:grid-cols-4`
  already, verify at 320); proposal accordion → `disclosure`; tab buttons stack
  full-width below `sm`; discussion region `max-h-[60svh]`. Conversations: compose
  form sticky-bottom on mobile with safe-area padding; `text-base` textarea.
- **G4 Admin lists (7):** `admin/users`, `agents`, `jobs`, `activity`,
  `discussions`, `access_requests`, `waitlist`. All tables → `data_table`; filters
  wrap; impersonation form stacks; stat grids → `stat_grid`.
- **G5 Admin detail + cohorts (9):** `admin/user_detail`, `agent_detail`,
  `activity_detail`, `llm_calls`, `cohorts`, `cohort_detail`, `cohort_topology`,
  `_cohort_gate_banner`, `discussions_export` (print document: stylesheet link
  only). Key/value panels → `<dl>` grid, 1 col below `sm`; `pre`/`code` blocks
  `overflow-x-auto` + `whitespace-pre-wrap` for JSON.
- **Excluded:** `cabo_graph.html`, `static/talk_viz/`, `data/`.

Per-page acceptance (enforced by Section 5 tests):

1. No horizontal document overflow at 320/390/768/1280.
2. Every visible `a`/`button`/`input`/`select`/`textarea` ≥ 24×24; at 320/390 also
   ≥ 44 px tall, except inline text links inside paragraphs (allow-listed by a
   `data-inline-link` attribute or by being a descendant of `p`, `li`, `td`).
3. `h1` in the viewport on load; the primary action (marked `data-testid="primary"`
   on pages that have one) has its bounding box inside the viewport width.
4. Tables: enclosing region has `tabindex="0"` and a caption.
5. No console errors.
6. No computed font size under 12 px; body copy ≥ 14 px at phone widths.

## Section 5 — Test harness and rollout

**Harness:** new tier `tests/responsive/`, run by the existing pytest invocation in
`./scripts/ci.sh` (no new step). Its `conftest.py` is **sync-scoped**: it takes the
session-scoped sync `engine`/`pg_url` fixtures from `tests/conftest.py`, and seeds
data by running the async factories under its own `asyncio.run()` with a
**committing** session (the shared `db_session` fixture is a rolled-back
transaction and is not visible to another connection). Seed: one admin, one
researcher with an active agent and two proposals (one unreviewed), one
pending-access user, one cohort; explicit teardown truncates what it created.
Settings are pinned before app import: `ENVIRONMENT=development`,
`SECRET_KEY=<test value>`, `ALLOW_HTTP_SESSIONS=true`, `DATABASE_URL=<pg_url>`,
and `get_settings.cache_clear()` is called. uvicorn runs in a background thread on
`127.0.0.1:<free port>`. Auth uses `forge_session_cookie` from
`tests/e2e/session.py`. Browser: sync Playwright Chromium from `.venv-test`; one
context per viewport — 320×568, 390×844 (`is_mobile=True`,
`device_scale_factor=2`), 768×1024, 1280×800 — all with `reduced_motion="reduce"`.
If the Chromium binary is missing the tier skips with an explicit reason (pytest
reports the skip count; `ci.sh` is not modified for this). If
`static/css/app.min.css` is missing the tier **fails** with instructions to run
`scripts/build-css.sh`.

**Page matrix:** ~33 routes × 4 viewports, parametrized; each test runs checks 1–6
above. Mobile-only tests: the nav `<details>` opens, shows all links, closes on
Escape; the admin sub-nav scrolls the active tab into view. A zero-JS smoke test
repeats the nav test with `java_script_enabled=False`. Screenshots are written to
`tests/responsive/_out/<route>-<width>.png` (gitignored) for human review only.

**Static checks:** `tests/unit/test_static_assets.py` (Section 1) plus: every
template extending `base.html` has no raw `<table` outside the `data_table` macro
(grep-based; no allow-list needed, `discussions_export.html` has no table).

**CI:** add `tests/responsive` as an element of the `LINT_TARGETS` array in
`scripts/ci.sh`; `COV_MIN` unchanged. The in-thread uvicorn *is* measured by
coverage (`concurrency = ["thread", "greenlet"]`), so the floor can only rise;
re-measure after the first run and consider raising it.

**Rollout:** single branch, staged commits: (1) foundation, (2) shell + components,
(3) G1–G5 in parallel, (4) harness. Merge → rebuild `app` and `worker` images (CSS is
baked at build) → manual smoke on a real iPhone and an Android phone. Rollback is
`git revert` of the merge and rebuild; no DB migration involved.

**Risks and mitigations:**

- v4 visual drift on desktop → before/after 1280 screenshots reviewed by a human.
- Standalone binary download at Docker build needs network → pinned URL + sha256,
  fail closed. No committed CSS fallback: `static/` stays gitignored, so the only
  way CSS reaches the image is the build stage.
- iOS Safari not covered by Chromium CI → manual device pass before merge;
  `interactive-widget` not relied on.
- Coverage floor: measured after first run; never lowered.
- Templating refactor touches all eight routers; it is a mechanical import swap
  verified by the existing integration tests, done as its own commit before any
  template change.
- Prod image bake: `docker compose ... up -d --build app worker` and
  `--profile agent build agent` per CLAUDE.md after merge.

## Audit corrections (2026-09-14)

Adversarial audit against the repo found three blocking defects and five gaps in
the first draft; all are folded in above:

1. No shared Jinja environment existed, so `css_version` needed `src/templating.py`
   and an import swap in eight routers (Section 1).
2. Four interpolated Tailwind class names would vanish under a compiled build;
   they are rewritten to literal class strings (Section 1).
3. The async, rolled-back `db_session` fixture cannot feed a sync Playwright tier;
   the harness seeds via a committing session under `asyncio.run()` (Section 5).
4. `.dockerignore` and COPY ordering, dev bind-mount shadowing, missing-CSS
   fail-not-skip, dead `flash_message`, settings pinning for the in-thread server,
   table census (21 in 12), `LINT_TARGETS` array wording, coverage direction.
