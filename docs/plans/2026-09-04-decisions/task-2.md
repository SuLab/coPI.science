# Task 2 — the tracked decisions log and the live-Slack isolation harness

## Ruling

Task 2 is a **FIX**, but four design choices inside it were the executor's to make, and the live tier
is the one place in this branch where a wrong choice reaches production Slack. They are recorded here
so Task 35 (which runs the tier) and any future operator can see what the harness does and does not
prove.

1. **One decisions file per task, plus a `README.md` stating the convention.** A single shared file is
   a file five parallel groups all write to — the condition that produced four mixed-attribution
   commits on this branch. `sdd-commit`'s `flock` serialises the git index, not two agents editing one
   file. See `README.md` for the headings every file uses.
2. **The preflight refuses; it does not warn, and it does not skip.** Concretely: an *absent*
   production credential is a refusal (not a pass), `auth.test` raising or timing out is a refusal, an
   `ok` response with no `team_id` is a refusal, and "no fixture token is set" is a refusal rather
   than a vacuous "every token present is fine".
3. **Checks 1-2 gate check 3.** Check 3 is the only one that talks to the network, and it is not run
   at all until the environment has been proved free of production credentials — a token is never
   sent anywhere from an environment we cannot vouch for. The un-run check reports `REFUSE — NOT
   ATTEMPTED`, so gating can never be mistaken for passing.
4. **The runner blanks a *derived* key list, and never sources `.env`.** `.env` holds live production
   bot tokens and a live app-config pair, so `scripts/run_live_slack.sh` exports every production
   Slack credential var **empty** (names from
   `live_slack_preflight.py --print-blank-keys`, derived from `Settings`' own fields — 127 vars today,
   so an agent added to `config.py` next month is covered too) and then makes the preflight re-prove
   the result through the real settings object. Empty, not unset: see the measurement below. The
   tier's own `SLACK_TEST_*` credentials come from the operator's environment; the documented way to
   supply them is a file holding nothing else (`set -a; . ~/.copi-test.env; set +a`), never `.env`.

**Scope note.** The plan names four production credentials
(`SLACK_BOT_TOKEN_CRAVATT`, `SLACK_BOT_TOKEN_WISEMAN`, `SLACK_CONFIG_TOKEN`,
`SLACK_CONFIG_REFRESH_TOKEN`) and check 1 asserts exactly those. One addition beyond the plan's
words: check 2's token mapping is widened by `production_token_map()` to include
`slack_bot_token_grantbot`, because `Settings.get_slack_tokens()` — the mapping the plan's check 2
names — is a hand-written dict literal that **omits `grantbot`** (measured: 125 `slack_bot_token_*`
fields, 124 mapping entries). Grantbot posts funding calls into the production workspace, so without
the union a live `SLACK_BOT_TOKEN_GRANTBOT` would pass every check the plan specifies.

**Do not "simplify" this back to the four named keys.** That is not a tidy-up; it re-opens a hole
straight into the production workspace, and it is a hole in the *plan's* check list, not in this
implementation. The branch owner reviewed and kept the widening on 2026-09-04. It is pinned by
`test_production_token_map_covers_the_field_get_slack_tokens_forgets` and
`test_the_blank_key_list_names_every_slack_credential_field_and_no_values`; if `get_slack_tokens()`
is ever fixed to include `grantbot`, the first of those fails loudly and tells you to re-read
`production_token_map`'s docstring rather than delete anything.

## Evidence

### The empirical claim the whole design rests on: empty overrides `.env`, absent does not

Measured with a **fabricated** `.env` in a scratch directory (never the repo's):

```
$ printf 'SLACK_BOT_TOKEN_LOTZ=xoxb-from-the-dotenv-file\n' > .env      # scratch dir
$ python -c 'from src.config import Settings; t=Settings().get_slack_tokens()["lotz"]; print(t=="", len(t))'
False 25          # var ABSENT  -> pydantic-settings falls through to the file
$ SLACK_BOT_TOKEN_LOTZ= python -c '...same...'
True 0            # var PRESENT and EMPTY -> overrides the file
```

That is why check 1 demands *present and empty* and why the runner exports rather than unsets.

### Refusal transcript A — a bare operator environment (nothing blanked, no tier credentials)

```
$ env -i PATH=... HOME=... PYTHONPATH=. .venv-test/bin/python scripts/live_slack_preflight.py
live-Slack preflight — the only permitted workspace is T0BMVSBMEC8 (copi-test)
  [REFUSE] 1. production Slack credentials blanked
           SLACK_BOT_TOKEN_CRAVATT is ABSENT from the environment, so pydantic-settings falls
           through to .env and the live value wins — export it as empty; SLACK_BOT_TOKEN_WISEMAN
           is ABSENT ...; SLACK_CONFIG_TOKEN is ABSENT ...; SLACK_CONFIG_REFRESH_TOKEN is ABSENT ...
  [REFUSE] 2. no usable production bot token resolves
           get_slack_tokens() still resolves 2 usable token(s) for agent id(s) <elided> —
           blank those SLACK_BOT_TOKEN_* vars too
  [REFUSE] 3. every fixture token resolves to T0BMVSBMEC8 (copi-test)
           NOT ATTEMPTED — checks 1-2 did not prove the environment isolated, so no token was
           sent to Slack. Fix those first; an un-run check refuses.
  [REFUSE] 4. the tier's own environment is complete
           SLACK_TEST_WORKSPACE is missing or empty — tests/conftest.py would skip all
           live_slack tests and report success; SLACK_TEST_PI_USER_ID ...; SLACK_TEST_BOT_TOKEN_SU ...
REFUSING to run the live Slack tier: 4 of 4 checks could not prove isolation from production Slack.
exit=1
```

Check 2 is the load-bearing line: run from this checkout with no blanking, **two** production bot
tokens are reachable through `.env`. (Agent ids elided here; the script prints them, since an agent id
is not a credential.)

### Refusal transcript B — the runner itself, deliberately-bad environment

```
$ env -i PATH=... HOME=... VENV_PY=.venv-test/bin/python bash scripts/run_live_slack.sh
==> blanking every production Slack credential in this process's environment
    127 credential vars exported empty (names only; no value read)
==> live-Slack isolation preflight
  [  OK  ] 1. production Slack credentials blanked
           SLACK_BOT_TOKEN_CRAVATT present and empty; SLACK_BOT_TOKEN_WISEMAN present and empty;
           SLACK_CONFIG_TOKEN present and empty; SLACK_CONFIG_REFRESH_TOKEN present and empty
  [  OK  ] 2. no usable production bot token resolves
           0 usable of 124 configured agent ids; env_token() None for all
  [REFUSE] 3. every fixture token resolves to T0BMVSBMEC8 (copi-test)
           no SLACK_TEST_BOT_TOKEN_* is set, so there is nothing to prove — refusing rather than
           passing vacuously
  [REFUSE] 4. the tier's own environment is complete
           SLACK_TEST_WORKSPACE is missing or empty — ...
REFUSING to run the live Slack tier: 2 of 4 checks could not prove isolation from production Slack.
REFUSING to run the live Slack tier: the preflight did not prove isolation.
Nothing was sent to Slack and pytest was not started.
exit=1
```

The blanking works: the same `.env` that made check 2 refuse in transcript A yields **0 usable
tokens** here. `pytest` was never started (pinned by
`test_the_runner_aborts_before_pytest_when_the_preflight_refuses`, which asserts the pytest banner
never appears).

### Refusal transcript C — one leaky credential, tier credentials otherwise complete

With `SLACK_CONFIG_TOKEN` set to a fabricated 37-character value and fabricated `SLACK_TEST_*`
credentials present:

```
  [REFUSE] 1. production Slack credentials blanked
           SLACK_CONFIG_TOKEN is still SET (len=37) — it must be empty
  [  OK  ] 2. no usable production bot token resolves
  [REFUSE] 3. every fixture token resolves to T0BMVSBMEC8 (copi-test)
           NOT ATTEMPTED — checks 1-2 did not prove the environment isolated ...
  [  OK  ] 4. the tier's own environment is complete
           SLACK_TEST_WORKSPACE present (len=1); SLACK_TEST_PI_USER_ID present (len=12);
           SLACK_TEST_BOT_TOKEN_SU present (len=18)
exit=1
```

This is the gating rule in action: the tier's own environment was complete, and still nothing was
sent to Slack, because check 1 had not passed. **Only lengths and presence are ever printed** — no
transcript in this file, and no line the script can emit, contains a token.

### Tests, and their mutation control

`tests/unit/test_live_slack_preflight.py` — **24 passed**
(`.venv-test/bin/python -m pytest tests/unit/test_live_slack_preflight.py -q -p no:cacheprovider`).
Red first: before the script existed the file failed at collection with
`ModuleNotFoundError: No module named 'scripts.live_slack_preflight'`.

Because "the tests pass" is weak evidence for a refusal harness, each isolation assertion was
inverted one at a time in a scratch copy of the script (shadow `scripts/` package on `PYTHONPATH`;
the working tree was never modified) and the real test file was run against it. **13 of 13 mutants
killed**, including: absent production key treated as blanked; non-empty production token accepted;
fixture team id never compared; `auth.test` exception / not-ok / missing-`team_id` treated as fine;
the network call not gated on checks 1-2; incomplete tier env accepted; a usable `.env` token
accepted; `env_token()` resolution accepted; the token map narrowed back to `get_slack_tokens()`
(grantbot invisible); the blank-key list narrowed to the four named credentials; and the vacuous pass
with no fixture token present. The first run left **one survivor** — the vacuous pass — which is why
`test_refuses_rather_than_passing_vacuously_with_no_fixture_token_at_all` exists.

### What this harness does NOT prove

- **No positive control was run.** Proving the preflight *accepts* a good environment needs the three
  real `copi-test` bot tokens and one `auth.test` round trip. Task 2 deliberately did not do that: the
  fixture tokens live in `.env`, which this task may not read values from, and a real Slack call is
  Task 35's step. So check 3's success path is covered by unit tests with a stubbed `auth.test`
  (including the foreign-team refusal, `T0PRODUCTIONXX`), not by a live call. **Task 35 must paste the
  preflight's output for the run it actually performs** — that is the first live exercise of check 3.
- **It proves reachability, not intent.** It shows the process holds no production credential and that
  every fixture token belongs to `T0BMVSBMEC8`. It cannot tell whether a test *inside* the tier does
  something destructive to the `copi-test` workspace.
- **`ANTHROPIC_API_KEY` is out of its scope** — that is Task 35 Step 1's spend decision.

**Task 35: paste check 3's first live pass into this section**, under a
`### Check 3's first live pass (Task 35)` heading — the preflight's own output for the run you
performed, credentials never included (the script prints lengths and team ids only). That transcript
is the missing positive control named above.

## Consequence a closing comment must state

`#27`'s closing comment (and Task 35's record) must say that the live Slack tier is now runnable
**only** through `scripts/run_live_slack.sh`, which refuses to start unless
`scripts/live_slack_preflight.py` proves, in one process, that the four production credentials are
present-and-empty, that no usable production bot token resolves through `Settings` (grantbot
included), that every fixture token resolves via Slack's own `auth.test` to `T0BMVSBMEC8`, and that
the tier's own environment is complete so the 61 tests cannot silently skip. It must also state the
limit above: the accept path has never been exercised against a real workspace, so **Task 35's
transcript is the first evidence that check 3 passes** rather than merely refuses.

Separately, `#27`'s comment must record the process fix this task exists for: v1 of the plan wrote six
rulings into `.superpowers/`, which is gitignored, so they could not be committed and no post-merge
reader could ever see them. All rulings for this plan are tracked under
`docs/plans/2026-09-04-decisions/`.
