# Help content and screenshots

Reference detail behind the rules in the root `CLAUDE.md`, which keeps only what
every session needs. Dated statements hold as of the date they give; re-measure a
count or a line number before relying on it.

The Help tab (`/help`) is open to admins, managers and reviewers; PIs get 403. It holds
reference pages, a reviewer tutorial and a manager tutorial, and reviewers never see the
staff-audience pages. One registry, `src/web/help_pages.py`, lists every page, tutorial
step, audience and section anchor; the content is Jinja under `templates/help/pages/` and
`templates/help/tutorials/`, and the screenshots are PNGs under `help_assets/shots/`, served
only to signed-in staff and reviewers. Design:
`docs/specs/2026-10-14-help-and-onboarding-design.md`.

## When a page changes, update its help

The help text describes the pages as they are, so a change to one of these pages is not
done until its help is updated in the same change:

| Page that changed | Help to update |
|---|---|
| `templates/workspace/pis.html`, `pi_detail.html` | `pis` (`directory`, `detail`); for staff cards, `managing-pis` and `pi-evidence`; manager steps 3–6, reviewer step 8 |
| `templates/pis/_profile_review.html` (the staff Corpus and profile review card on the PI page) | `managing-pis` (`paper-review`, `finding-papers`), the glossary's candidate, unanchored and stages terms; manager step 5 |
| `templates/workspace/assessments.html`, `assessment_detail.html`, the assessment card and chat | `assessments`; reviewer steps 3–7, manager step 8 |
| `templates/workspace/slack_bots.html` | `managing-pis#slack-bots`; manager step 7 |
| `templates/workspace/staff.html` | `staff-accounts`; manager step 9 |
| `templates/workspace/discussions.html`, `activity.html`, `activity_detail.html` | `discussions-activity`; manager step 10 |
| `templates/workspace/prompt_suggestions.html`, `prompt_suggestion_detail.html` | `prompt-suggestions`; manager step 10 |
| `templates/settings.html` | `getting-started#settings` (and `#microsoft`); reviewer step 2, manager step 2 |
| `templates/login.html`, `templates/access_pending.html`, `templates/profile/delete_account.html` | `getting-started` (`signing-in`, `access-problems`) |

For every one of them, also check:

- the **glossary**: a renamed label, badge or status gets its new term, and the old wording
  stays listed under it ("old label: see …");
- **troubleshooting**: a new, changed or removed error banner, flash or refusal message;
- the **tutorial step** that shows the page, and its screenshots (below);
- the page's "Help with this page" link, if its target section moved or was renamed. Its
  anchor must be one of the page's `anchors` in the registry.

A new help page or step needs a registry entry whose template name is a literal string:
`tests/unit/test_reachability.py` counts a template as rendered only when `src/` names it
in full.

## Content rules

Every help page and tutorial step keeps these rules (spec §6.3):

1. **Every fact comes from reading the current code**, not from the spec, a plan or memory.
2. **Name the UI text exactly**: button labels, headings, badge words, as the page prints
   them today.
3. **State no value that comes from settings or the rubric**: band thresholds, dimension
   titles and weights, chat question, length, turn and cost limits, page sizes, the
   prompt-suggestion batch size, label strings. Say where the page shows the value instead
   (the assessments header prints the bands; the chat drawer prints its usage). These
   change without a template change, so a stated number would go stale silently.
4. **The only numbers allowed** are the 1–5 overall rating and the 10,000-character comment
   limit; `tests/unit/test_help_stated_constants.py` pins both to the code that enforces
   them. Every other number is left out, including the session lifetime, the re-sign-in
   window for Microsoft linking, the Regenerate cooldown, the roster sync interval and the
   count of Workspace tabs: point to the UI text that shows it, or describe it without the
   number.
5. **Role-only sections** on an all-audience page sit in
   `{% if permissions.staff %}…{% endif %}`. A reviewer reads a manager-only feature only
   as "a manager can…", with no instructions.
6. **Microsoft sign-in content** sits in `{% if microsoft_sign_in %}…{% endif %}`.
7. **Warn before real effects.** Every manager action with real, visible or permanent
   effects is introduced by `{% call careful() %}…{% endcall %}`, which says what happens
   and whether the UI can undo it (spec Appendix A.2 lists the ones it cannot).
8. **Admin-only features** appear only where they bound what a manager can do, as "only an
   admin can…". They are never described.
9. **No real names**, in content or screenshots. Invent examples; never copy them from code
   comments or tests, which name real PIs.
10. **No repository internals**: no file paths, pointers to code or tests, test counts, or
    audit and verification history. Pages explain how to use the system and what its data
    means.

Templates also pass the rendered-page gate (one `<main>` and one `<h1>`, no skipped heading
level, every `<nav>` named, no `text-gray-300`/`text-gray-400`, no inline `on…=` handler,
named form controls), and the enforced CSP allows no inline script. Help links and
in-content links are checked by rendering every help page and linked page as each role,
with Microsoft sign-in on and off (`tests/integration/test_help_links_render.py`).

## Screenshots

Screenshots come from a seeded throwaway instance with invented data, never from
production. The shot list, with each shot's audience, capture account, clip and alt text,
is `tests/e2e/help_shots/shots.py`; the tool and its prerequisites are described in
`tests/e2e/help_shots/README.md`.

| Command | Needs | Does |
|---|---|---|
| `python -m tests.e2e.help_shots.capture [--only ID]… [--executable PATH]` | Docker and a Chromium headless shell, so a workstation; the host cannot run it | Regenerates the PNGs and their `manifest.json` entries |
| `python -m tests.e2e.help_shots.capture --check` | neither | Rehashes each shot's sources and lists stale and missing shots; exit 1 if any. `scripts/ci.sh` does not run it |

- **Capture is the last step before a deploy.** Each shot's hashed sources include
  `templates/base.html` and `static/css/app.css`, so any template or class change after a
  capture marks every shot stale. Capture after review and its fixes; a fix made after the
  capture means a full recapture.
- **Commit the PNGs and `manifest.json` together.** `tests/unit/test_help_shots.py` fails
  on a figure with no manifest entry or no PNG, a PNG with no entry, or a size mismatch.
- **`help_assets` is in the pre-build path list** (root `CLAUDE.md`, and "Running the Agent
  Simulation" in `docs/operations/host-and-simulation.md`): the builder's `git clean -ffdx`
  would drop an untracked PNG while the tracked manifest that names it still ships.
- An `all`-audience shot is captured as a reviewer account and a `staff` shot as a manager
  account, so a reviewer's screenshot never shows staff-only controls.

## The PI-detail error sentences

Every `error` code a manager or reviewer can reach the PI detail page with has its own
sentence in `templates/pis/_detail_errors.html` (or `templates/_profile_errors.html`), so
none falls back to "Something went wrong saving changes." and troubleshooting can name it.
`tests/unit/test_pi_detail_error_codes.py` collects the codes the PI routers pass to the
PI-detail redirects by scanning them, plus a hand-kept set of the codes the services behind
those routes return. **A code added only inside a service is not caught**: add it to that
set, give it a sentence, and add it to troubleshooting.
