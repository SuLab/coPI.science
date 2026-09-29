# Opus 5 → Opus 5.5 migration — Phase 0 result: NO-GO

Plan: `docs/plans/2026-09-28-opus-5-5-migration-plan.md` (Task 0, D2 gate 1).
Tool: `scripts/dev/opus55_replay.py` (tests: `tests/unit/test_opus55_replay.py`).
Run: 2026-09-28 15:42–15:50 UTC, one-off `blackbird-app` container off the deployed image
(anthropic 1.8.0) with the tree's `src/` and `scripts/` mounted read-only:

    docker compose -f docker-compose.prod.yml run --rm -T --no-deps -e PYTHONPATH=/app \
      -v "$PWD/src:/app/src:ro" -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/docs:/app/docs" \
      blackbird-app python scripts/dev/opus55_replay.py --apply

READ ONLY database transactions, no `llm_call_logs` callback, nothing enqueued or posted. The
container was stopped at 44 completed calls (see "Early stop") and removed (`--rm`; no leftover
`copi-blackbird` container afterwards).

## Evidence files (this directory)

| File | What it holds |
|---|---|
| `records-20260928T154222Z.jsonl` | one line per completed call — metrics only, no prompt or response text |
| `analysis-20260928.json` | `opus55_replay.py --analyse` over the JSONL |
| `probes-shape-and-rate-limits-20260928.json` | Step 1 shape probe and Step 7 rate-limit headers, re-run at 16:13 UTC (the in-run copies were lost when the run was stopped — the report was written only at the end; the script now writes them first) |
| `echo-probe-20260928.json` | Step 6 echo check, verbatim stdout |
| `nofallback-control-20260928.jsonl` + `-refs.json` | the no-fallback control, verbatim stdout, and the rows it used |
| `probe-scripts/` | the three runners behind the last three files |

## Verdict

**Gate 1 fails on every route.** Of 37 requests sent to `claude-opus-5-5` — 31 replayed
production requests plus 6 rebuilt inputs (3 profile-synthesis contexts, 3 review-bot eval
cases) — it answered **1** itself (a rebuilt review-bot case, `band_recommendation_mismatch`).
The other **36 were declined by its safety classifier with category `bio`** and answered by the
pinned fallback, `claude-opus-5`. None was lost, and on every route parse success was at least
the stored Opus 5 baseline.

Served by 5.5: 2.7 % overall (one-sided 95 % upper bound 12.2 %), against the plan's
pre-registered ≥ 80 % per route. Per D2, a gate-1 no-go stops the plan before Phase 1: **no
application code was changed.** A migration on these terms would be an Opus 5 deployment that
pays for a declined 5.5 attempt first on nearly every call.

| Route (arm A, effort as planned) | n | served by 5.5 | rescued by Opus 5 | lost | decline category | parse ok / baseline ok |
|---|---|---|---|---|---|---|
| consult (all `budget` persona, see Limits) | 7 | 0 | 7 | 0 | bio ×7 | 7 / 7 |
| new_post (pi_lab) | 7 | 0 | 7 | 0 | bio ×7 | 7 / 7 |
| thread_reply first request, pi_lab | 6 | 0 | 6 | 0 | bio ×6 | 6 / 6 |
| thread_reply first request, hub | 6 | 0 | 6 | 0 | bio ×6 | 6 / 6 |
| hub thread_reply final (sidecar rows) | 5 | 0 | 5 | 0 | bio ×5 | 5 / 5 |
| profile synthesis (rebuilt) | 3 | 0 | 3 | 0 | bio ×3 | 3 / 3 |
| review bot (eval cases) | 3 | 1 | 2 | 0 | bio ×2 | 3 / 2 |
| **all 5.5 calls** | **37** | **1** | **36** | **0** | **bio ×36** | |

- Of the 36 declines, 33 came before any output and 3 after some 5.5 output (238, 53 and 453
  tokens). The non-streaming response omits that partial output and the fallback answers from
  scratch (docs C18): all 36 rescued responses start with the `fallback` block.
- No sticky-routed turn (C16) appeared.
- **The decline is content-driven, not caused by the fallback parameter.** Three declined rows
  (one consult, one new_post, one pi_lab first request; `nofallback-control-refs.json`), re-sent
  on `claude-opus-5-5` through the non-beta client with no `fallbacks` and no betas, each came
  back `stop_reason: "refusal"`, category `bio`, empty content, 0 output tokens. On 5.5,
  `thinking: disabled` is a 400 (plan E14), so adaptive thinking plus an effort level is the only
  shape that can be sent at all. The assessment chat's 4-of-4 fallbacks (streaming, `"default"`
  form; plan E19) corroborate this independently.
- Opus 5 control arm (today's request shape): 7 consults, all served, max output 2,970 tokens.

## What else Phase 0 established (reusable if 5.5's classifier changes)

- **The array-form fallback's `thinking: disabled` override works in real rescues:** all 19
  rescued single calls ran on Opus 5 with 0 thinking tokens. The `adaptive` override for
  tool-carrying calls cannot be told apart from the request's own inherited setting (14 of 17
  such rescues thought), and the `effort: high` override is not observable in the response.
- **Echo rule (C15), live:** one assessment-chat record ("summarize this assessment",
  non-streaming, one dummy tool) was declined `bio` and rescued. Its content — fallback block
  first — was echoed with `model_dump(by_alias=True)` (`from`, not `from_`) in a follow-up sent
  with `prefix_mismatch_behavior: "error"`, and the API accepted it (200). The follow-up was
  itself declined and rescued again, so this proves the echoed blocks pass the binding check; it
  does not show 5.5 continuing the conversation.
- **Rate limits** (response headers, both models, identical): 20,000 requests/min, 10 M input
  and 2 M output tokens/min, per model.
- **Latency and `max_tokens` (Task 5):** not computable. 5.5 served one call, so its latency
  slope has no data (the Opus 5 control fit rests on 2 points). Task 5's clause (ii) fails on
  every route by construction; it is moot under the no-go.

## Cost

Recorded: $9.99 across the 44 completed calls (5.5 path incl. Opus 5 rescues $9.57, control arm
$0.41); echo check $1.09; no-fallback control, 3 calls, all declined before any output (≤ $0.15 even if their input is billed); shape and
rate-limit probes $0.002. Up to 4 requests were in flight when the run was stopped (by queue
order: 2 hub finals, 1 hub and 1 pi_lab first request); they are not in the records and could
have billed up to ≈ $5 between both attempts. **≈ $11–16 in total** against D1's $55 cap. The
Anthropic Console has the exact figure.

## Early stop (a deviation from the plan)

The run was stopped at 44 of ≈ 249 calls. The stop rule was not pre-registered in the plan; it
was applied on a futility argument against the plan's own D2 gate-1 thresholds, which are caps
at the planned sample sizes:

- **Already failed arithmetically:** profile (0 of 3; needs 3), review (1 of 3; needs 3),
  new_post (0 of 7 with 3 left; at most 3 of 10, needs 8), hub final (0 of 5; at most 5 of 10,
  needs 8), each thread_reply first request (0 of 6; at most 14 of 20, needs 16), and the
  `budget` persona (0 of 7; at most 8 of 15, needs 9).
- **Still open alone:** only the consult route in aggregate (at most 113 of 120). It is gated
  jointly with the per-persona rule, and it shares its setting (`llm_agent_model_opus`) with
  thread_reply and new_post, which had already failed.

So each setting was a no-go, and so was every D6 staged-rollout option: `llm_profile_model`
(profile), `llm_review_model` (review) and `llm_agent_model_opus` (thread_reply and new_post).
The remaining ≈ 205 calls (≈ $25) could not change that.

## Limits of this evidence

- **Sample skew.** The consult queue was ordered persona by persona, so all 7 arm-A consults
  are `budget`, and the per-persona bounds above hold for that persona only. The script now
  interleaves personas. Replayed rows by run: from `bf6da580` (HEAD's prompts: hub v1.8.0,
  rubric v3.5.0) — 5 hub finals and 2 new_posts; from `75ca77f9` (hub v1.7.0, rubric v3.4.0) — 7
  consults, 5 new_posts and all 12 first requests. Contrary to Step 3's "`bf6da580` first", the
  first run merged both runs by date; the script now prefers `bf6da580`. Every stratum, from both
  runs, declined `bio`.
- **Strata that never ran:** arm B (medium-effort consults), the forced-final shape, the hub
  first-request half of the Opus 5 control arm, and 7 of the 8 personas.
- **Rebuilt inputs.** Profile-synthesis contexts came from stored rows (no live ORCID/PMC fetch,
  no lab website, today's grant titles; contexts of 53,767–59,186 characters). The review cases
  are eval inputs, not production traffic.

## What this means for the owner

1. **Keep all three settings on `claude-opus-5`.** This is today's configuration; nothing changed.
2. **The assessment chat is already on `claude-opus-5-5`**, and on this evidence it is effectively
   an Opus 5 feature that also pays for a declined 5.5 attempt. Its ledger records a pre-output
   decline as `billed: false`, but the docs pass reported, without a verbatim quote, that `bio`
   pre-output declines are billed. Check that against the Console.
3. **To re-test after 5.5's biology classifier changes**, re-run Task 0 (`--apply`). A full run is
   ≈ 250 calls, ≈ $37 and about an hour at concurrency 4. It still has no automatic stop-when-
   decided rule: a no-go was visible here after about 40 calls, and a GO needs the full sample.
