#!/usr/bin/env bash
#
# Mutation check for the subsystems T1–T11 claim to protect: ORCID, PubMed/NCBI, the
# job-queue worker, the profile pipeline, the public graph, onboarding/impersonation/
# profile export, the agent page, and GrantBot's FOA dedup.
#
# Each mutant must be KILLED — at least one test in the named selection must fail with it
# applied. A SURVIVING mutant means the suite does not actually test that behaviour,
# whatever its test names claim. Same discipline as scripts/mutate_cohorts.sh (9/9) and
# scripts/mutate_slack_mirror.sh (4/4), and the same `~~` field delimiter, chosen there
# because one mutation target contains a `|` and kept here because several contain `~`-free
# SQL with pipes and quotes of both kinds.
#
# As of 2026-08-04 mutate_cohorts.sh shares this file's isolation strategy — it was
# converted from in-place editing to copy+provenance, and its 9/9 was re-measured under
# the new strategy and held. mutate_slack_mirror.sh still edits src/ in place; it needs
# live Slack credentials, so it could not be re-verified after a rewrite and was left
# alone with a header warning rather than silently changed.
#
# THE INERT MUTANTS ARE NOT OPTIONAL. Every tier below carries one edit that changes no
# behaviour (a docstring, a comment, a log string) and MUST SURVIVE. Without it a tier
# that is broken for any unrelated reason — a dead credential, a migrated-away column, a
# leftover row — scores 100% and looks sensitive when it is merely failing. Each tier's
# inert mutant is listed FIRST so a broken tier is detected before any money is spent on
# it.
#
# NOTHING IN THIS REPOSITORY IS EVER WRITTEN TO.
# Three agents previously applied mutations by editing src/ in place. A repo guard
# auto-reverted them mid-run and silently corrupted the results: mutants reported as
# SURVIVING would in fact have been killed. So this script copies the tree into the
# container's /tmp, mutates the COPY, runs pytest with the copy as its working directory,
# and proves — by importing `src` and checking `src.__file__` — that the copy is what is
# under test. `git diff --quiet -- src/` is asserted before the first mutant and after the
# last one.
#
# Usage:
#   # offline tiers only (free, no third-party calls):
#   ./scripts/mutate_system.sh
#
#   # + the live ORCID / NCBI / grants.gov tiers (free, but real HTTP):
#   LIVE_API_TESTS=1 ./scripts/mutate_system.sh
#
#   # + the profile-pipeline tier (real Anthropic tokens, ~7 calls total):
#   LIVE_API_TESTS=1 ANTHROPIC_API_KEY=sk-ant-... ./scripts/mutate_system.sh
#
# A tier whose credentials are absent is reported as `skipped`, never as `killed`.
#
# Cost control: every mutant runs against ONLY the test file (and often only the single
# test) that is supposed to kill it, never the whole suite. That is what keeps the
# Anthropic spend at ~7 calls and the NCBI traffic inside the 3 req/s anonymous policy.
#
# MEASURED 2026-08-04, offline tiers only, no credentials present:
#
#   killed 6/6 real mutants        M4, M5 (worker); M7 (graph); M8, M9 (onboarding);
#                                  M10 (agentpage)
#   inert controls 4/4 survived    M12c, M12e, M12f, M12g
#   11 skipped for credentials     orcid: M12a, M1, M1b
#                                  pubmed: M12b, M2, M3
#                                  pipeline: M12d, M6, M6b
#                                  grantbot: M12h, M11
#   src/ clean, exit 0
#
# NO REAL MUTANT SURVIVED ANY TIER THAT COULD BE RUN. The list below is therefore not a
# list of survivors; it is the standing record for the two mutants this script's own
# tiers cannot judge without credentials, plus the resolution of one that used to survive.
# Do not weaken any of them.
#
#   M1b  UNMEASURED as of 2026-08-04 — its tier (orcid) needs LIVE_API_TESTS=1, which was
#        not available, so it reported `skipped`. It was last measured as SURVIVING on
#        2026-07-31 and nothing has changed tests/live_api/test_orcid_live.py since, so
#        treat it as still open: fetch_orcid_profile hardcoded to "Josiah Carberry"
#        survives that file. Its only defence against a constant name is the dated
#        `"Carberry" in prof["name"]` assertion, which a hardcode of the expected value
#        satisfies. Nothing in the live tier compares the parsed name against the record
#        it came from, and the docstring's claimed control ("the parser must NOT return
#        the same thing for a different id") is not implemented — the id it checks is
#        copied from the argument, not parsed. Measured then: the PRE-EXISTING contract
#        test tests/contract/test_orcid_contract.py::
#        test_fetch_orcid_profile_falls_back_to_orcid_when_no_name DOES kill it, so this
#        is a gap in the new tier rather than in the repo. Re-run with LIVE_API_TESTS=1
#        before claiming it either way.
#
#   M6   RESOLVED 2026-08-04. _validate_profile hardwired to `return True` is now KILLED
#        by tests/characterization/test_profile_pipeline_gm.py (3 failed:
#        stores_the_retry_not_the_rejected_first_synthesis,
#        marks_a_profile_that_fails_validation_twice,
#        rerun_that_fails_validation_keeps_the_stored_profile), and M6b (always False) by
#        7 — so the validator's effect is now visible in BOTH directions, which it was
#        not before. An inert docstring edit on the same function survived the same
#        selection (11 passed), so those kills are the mutation and not a red suite.
#        Fix 2 (d311170) is what closed it: the return value now gates step 9 instead of
#        being computed and discarded. The old note here claimed M6 survived the entire
#        offline suite; that stopped being true and nothing updated it.
#
#        CAVEAT, so this is not misread: the kill comes from the OFFLINE characterization
#        file, not from this script's `pipeline` tier, which is
#        tests/integration/test_profile_pipeline_live.py and still needs LIVE_API_TESTS=1
#        plus ANTHROPIC_API_KEY. M6/M6b therefore still report `skipped` in a
#        credential-free run — see the 2026-08-04 measurement above. The evidence was
#        produced with this script's own copy+provenance pattern (tree copied into the
#        container, `src.__file__` asserted under the copy, the mutated module
#        import-checked), not by editing src/. If you want this harness to see M6 by
#        itself, the characterization file has to join a tier whose CREDS are "".
#
# 2026-08-04, the Slack chokepoint mutants — 8515f65's control was a PROVENANCE ARTIFACT.
# That commit recorded, as the most important line in its report, that the four
# chokepoint mutants "run against the offline selection ALL SURVIVED, at exactly 1093",
# and flagged the suspiciously round figure as needing reproduction before belief. It has
# now been reproduced, and the claim does not hold: against
# tests/unit/test_slack_client_contract.py + tests/unit/test_transport.py (81 passed
# unmutated) all four are KILLED, with an inert docstring edit on the same file surviving.
# The pagination mutant was additionally run against the WHOLE offline suite, which is the
# selection the 1093 figure came from: 9 failed / 1158 passed / 120 skipped, the same 9
# tests. So the full suite kills it too — and today's collection is 1158, not 1093, which
# is a second reason that figure describes a run that was not measuring what it claimed.
#
#   pagination            _paginate returns after page 1  ...............  9 failed
#   ts-ordering           the pre-fix list(reversed(...)) in
#                         _conversation_messages  ........................  4 failed
#                         (the stronger `key=_by_ts, reverse=True` variant:  7 failed)
#   splitting             split_for_slack never splits (`if True: return [text]`)  7 failed
#   thread_ts normalise   normalize_inbound_message's self-reference test dead  5 failed
#   INERT control         _conversation_messages docstring reworded  ....  survived
#
# So Fix 4's offline tests do protect all four mechanisms; the earlier "all survived" was
# the failure mode this script's provenance check exists to catch — unmutated code under
# test, every mutant falsely surviving. Which is also why the exact-1093 count was the
# tell: a selection that never loaded the mutant cannot move.
#
# Overridable env:
#   TEST_DATABASE_URL   throwaway asyncpg DSN (default: the copi_a3 scratch database)
#   MUTSYS_SERVICE      compose service to exec into (default: app)
#   MUTSYS_COPY_DIR     where the mutated tree lives inside the container
#   MUTSYS_LOGDIR       where per-mutant pytest logs are kept (default: a mktemp dir)
#   MUTSYS_KEEP_COPY    set to 1 to leave the mutated tree behind for inspection
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+asyncpg://copi:copi@postgres:5432/copi_a3}"
SVC="${MUTSYS_SERVICE:-app}"
COPY="${MUTSYS_COPY_DIR:-/tmp/mutsys}"
LOGDIR="${MUTSYS_LOGDIR:-$(mktemp -d)}"
DC=(docker compose exec -T)

# mkdir, because MUTSYS_LOGDIR is documented as overridable and this script did not
# create it. Measured 2026-08-04: pass a path that does not exist and every `>"$log"`
# redirect fails, which the shell scores as a nonzero exit — i.e. as a KILL — for every
# mutant, inert controls included. The run reported "killed 6/6 real mutants" and
# "inert controls: 0/4 survived", and only that second line distinguished it from a
# perfect score. Any earlier run of this script made with MUTSYS_LOGDIR set to a
# nonexistent directory reported every mutant as killed and is void.
mkdir -p "$LOGDIR" || { echo "ERROR: cannot create log dir $LOGDIR" >&2; exit 1; }

# Deliberately NOT the live database, and asserted rather than assumed: several of these
# suites commit (the worker tests need another connection to see the write, so they cannot
# use the rolled-back session fixture).
case "$TEST_DATABASE_URL" in
  */copi|*/copi\?*)
    echo "ERROR: TEST_DATABASE_URL points at the live 'copi' database. These suites" >&2
    echo "commit. Use a throwaway database." >&2
    exit 1 ;;
esac

# ---------------------------------------------------------------------------
# Rule 1: the working tree is never touched. Check before, and again at the end.
# ---------------------------------------------------------------------------
if ! git diff --quiet -- src/; then
  echo "ERROR: src/ has uncommitted changes." >&2
  echo "This script does not edit src/ — it mutates a copy inside the container — but a" >&2
  echo "dirty tree means the copy would carry changes that are not the mutant, so every" >&2
  echo "result below would be unattributable. Commit or stash first." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Tiers: the selection each mutant is judged against, and what it costs.
#
# CREDS: "" = offline; "live" = needs LIVE_API_TESTS=1; "live+llm" = also real Anthropic.
# ---------------------------------------------------------------------------
declare -A TIER_SELECT=(
  [orcid]="tests/live_api/test_orcid_live.py"
  [pubmed_tool]="tests/live_api/test_pubmed_live.py -k test_ncbi_get_sends_the_required_tool_and_email_parameters"
  [pubmed_doi]="tests/live_api/test_pubmed_live.py -k test_reconcile_pub_doi_separates_a_real_match_from_a_near_miss"
  [pubmed_both]="tests/live_api/test_pubmed_live.py -k 'test_ncbi_get_sends_the_required_tool_and_email_parameters or test_reconcile_pub_doi_separates_a_real_match_from_a_near_miss'"
  [worker]="tests/integration/test_worker.py"
  [pipeline]="tests/integration/test_profile_pipeline_live.py -k test_t41_one_real_orcid_becomes_a_stored_profile_grounded_in_its_works"
  [graph]="tests/integration/test_public_graph.py"
  [onboarding]="tests/integration/test_onboarding_flow.py"
  [agentpage]="tests/integration/test_agent_page.py"
  [grantbot]="tests/integration/test_grantbot_live.py -m 'not real_llm' -k 'test_claim_foa_is_the_dedup_primitive or test_the_claim_not_the_prefilter_is_what_stops_a_repost'"
)
declare -A TIER_CREDS=(
  [orcid]="live"       [pubmed_tool]="live"  [pubmed_doi]="live"  [pubmed_both]="live"
  [worker]=""          [pipeline]="live+llm" [graph]=""           [onboarding]=""
  [agentpage]=""       [grantbot]="live"
)

# tier ~~ file ~~ exact source substring ~~ replacement ~~ label
#
# `\n` in the FROM/TO fields is a newline (see the applier below). Entries containing a
# double quote are single-quoted here and vice versa; no entry needs both.
MUTANTS=(
# --- ORCID (T1) ------------------------------------------------------------------------
'orcid~~src/services/orcid.py~~    """Extract name, affiliation, and email from ORCID record."""~~    """Extract the name, affiliation and email from an ORCID record. [INERT EDIT]"""~~M12a INERT docstring — MUST SURVIVE'
'orcid~~src/services/orcid.py~~    result["name"] = f"{given} {family}".strip() or orcid_id~~    result["name"] = "Ada Lovelace"~~M1 fetch_orcid_profile returns a constant name instead of parsing person.name'
# M1b is the same defect as M1 with the constant chosen to equal today's expected value.
# It is the difference between "the test reads the record" and "the test restates the
# answer". SURVIVES the live tier (see KNOWN SURVIVORS above).
'orcid~~src/services/orcid.py~~    result["name"] = f"{given} {family}".strip() or orcid_id~~    result["name"] = "Josiah Carberry"~~M1b the same hardcode, set to the value the test pins (probes whether the assertion is derived from the live record or merely restated)'
# --- PubMed / NCBI (T2) ----------------------------------------------------------------
'pubmed_both~~src/services/pubmed.py~~    """Make a rate-limited, identified GET request to NCBI."""~~    """Make a rate-limited, identified GET request to NCBI E-utilities. [INERT EDIT]"""~~M12b INERT docstring — MUST SURVIVE'
"pubmed_doi~~src/services/pubmed.py~~    if assigned.lower() == auth.lower():~~    if True:~~M2 reconcile_pub_doi always reports a match, so a PMID keeps whatever DOI it arrived with"
'pubmed_tool~~src/services/pubmed.py~~    params.setdefault("tool", _NCBI_TOOL)~~    pass  # tool= no longer sent~~M3 _ncbi_get stops identifying itself to NCBI (throttle, then IP block)'
# --- worker (T5) -----------------------------------------------------------------------
'worker~~src/worker/main.py~~    logger.info("Job %s completed", job.id)~~    logger.info("Job %s has completed", job.id)~~M12c INERT log string — MUST SURVIVE'
"worker~~src/worker/main.py~~        .with_for_update(skip_locked=True)~~        .with_for_update()~~M4 claim_job drops SKIP LOCKED, so a worker pool serialises behind the slowest job"
'worker~~src/worker/main.py~~            if job.type == "generate_profile":~~            job.status = "completed"; job.completed_at = datetime.now(timezone.utc); await db.commit()\n            if job.type == "generate_profile":~~M5 the job is marked completed and committed BEFORE the work is dispatched'
# --- profile pipeline (T4) -------------------------------------------------------------
"pipeline~~src/services/profile_pipeline.py~~    Validate synthesized profile fields.~~    Validate the synthesized profile fields. [INERT EDIT]~~M12d INERT docstring — MUST SURVIVE"
"pipeline~~src/services/profile_pipeline.py~~def _validate_profile(profile: dict[str, Any]) -> bool:~~def _validate_profile(profile: dict[str, Any]) -> bool:\n    return True~~M6 _validate_profile always returns True, so no profile is ever rejected"
"pipeline~~src/services/profile_pipeline.py~~def _validate_profile(profile: dict[str, Any]) -> bool:~~def _validate_profile(profile: dict[str, Any]) -> bool:\n    return False~~M6b the same function always returns False — the paired control for M6, which shows whether the tier can see validation's effect in EITHER direction"
# --- public graph (T9) -----------------------------------------------------------------
"graph~~src/routers/public.py~~            -- The agent-only proposal for a thread is the FIRST one the bots~~            -- [INERT EDIT] the agent-only proposal for a thread is the FIRST one the bots~~M12e INERT SQL comment inside the mutated query — MUST SURVIVE"
"graph~~src/routers/public.py~~                  AND origin_visibility = 'public'\n                  AND decided_at >= :decided_floor{window_end_clause}~~                  AND decided_at >= :decided_floor{window_end_clause}~~M7 the pairs CTE stops filtering on origin_visibility, so collab_private proposals reach the public graph"
# --- onboarding / impersonation / profile export (T7) ----------------------------------
"onboarding~~src/dependencies.py~~    # Impersonation: admin can view as another user~~    # Impersonation [INERT EDIT]: an admin can view the site as another user~~M12f INERT comment — MUST SURVIVE"
"onboarding~~src/dependencies.py~~    if impersonate_id and session_user.is_admin:~~    if impersonate_id:~~M8 copi-impersonate is honoured for non-admins — any logged-in user can become any other user"
'onboarding~~src/services/profile_export.py~~    path = PROFILES_DIR / f"{agent_id}.md"~~    if profile.private_profile_md:\n        lines.append(profile.private_profile_md)\n    path = PROFILES_DIR / f"{agent_id}.md"~~M9 the PUBLIC profile export appends private_profile_md'
# --- agent page (T8) -------------------------------------------------------------------
'agentpage~~src/routers/agent_page.py~~            "Ignoring duplicate reopen of proposal %s by %s "~~            "Ignoring a duplicate reopen of proposal %s by %s "~~M12g INERT log string — MUST SURVIVE'
"agentpage~~src/routers/agent_page.py~~    if already_reviewed is not None:~~    if False:~~M10 the reopen idempotency guard is gone, so a replayed POST migrates the thread twice"
# --- GrantBot (T10) --------------------------------------------------------------------
'grantbot~~src/agent/grantbot.py~~    """Undo a claim when the Slack post itself failed, so a later run can retry."""~~    """Undo a claim when the Slack post failed, so a later run retries. [INERT EDIT]"""~~M12h INERT docstring — MUST SURVIVE'
"grantbot~~src/agent/grantbot.py~~    return result.rowcount == 1~~    return True~~M11 _claim_foa always reports the claim as won, so two runs post the same FOA"
)

# ---------------------------------------------------------------------------
# Build the mutable copy inside the container and PROVE it is what runs.
# ---------------------------------------------------------------------------
cleanup() {
  if [ "${MUTSYS_KEEP_COPY:-0}" = "1" ]; then
    echo "(left the mutated tree at ${SVC}:${COPY} — MUTSYS_KEEP_COPY=1)"
  else
    "${DC[@]}" "$SVC" rm -rf "$COPY" >/dev/null 2>&1
  fi
}
trap cleanup EXIT

echo "building a throwaway copy of the tree at ${SVC}:${COPY} (the repo is never written to)"
if ! "${DC[@]}" "$SVC" sh -c "
  rm -rf '$COPY' && mkdir -p '$COPY' &&
  tar -C /app \
      --exclude=./.git --exclude=./.venv-test --exclude=./mutants --exclude=./build \
      --exclude=./logs --exclude=./.hypothesis --exclude=./.pytest_cache \
      --exclude=./.ruff_cache --exclude=./.playwright-mcp --exclude=__pycache__ \
      -cf - . | tar -C '$COPY' -xf -
" 2>/dev/null; then
  echo "ERROR: could not copy /app into $COPY inside the '$SVC' container." >&2
  exit 1
fi

# Provenance. `src` is ALSO installed into site-packages in this image, so without this
# check a run could silently be testing /usr/local/.../src or /app/src — i.e. exercising
# unmutated code and reporting every mutant as SURVIVED. That is the failure mode this
# whole script exists to avoid, so it is asserted rather than assumed.
prov=$("${DC[@]}" -w "$COPY" "$SVC" python -c "import src; print(src.__file__)" 2>/dev/null | tr -d '\r')
case "$prov" in
  "$COPY"/src/__init__.py) echo "provenance OK: pytest will import $prov" ;;
  *)
    echo "ERROR: from $COPY, 'import src' resolves to '${prov:-<nothing>}', not" >&2
    echo "$COPY/src/__init__.py. The mutants would not be under test. Refusing to run." >&2
    exit 1 ;;
esac

echo "logs: $LOGDIR"
echo

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
fail=0 killed=0 survived=0 skipped=0 broken_inert=0 inert_ok=0 n=0
declare -a SURVIVORS=()

for m in "${MUTANTS[@]}"; do
  tier="${m%%~~*}"; rest="${m#*~~}"
  file="${rest%%~~*}"; rest="${rest#*~~}"
  from="${rest%%~~*}"; rest="${rest#*~~}"
  to="${rest%%~~*}"; label="${rest#*~~}"
  n=$((n + 1))
  short="${label%% *}"

  inert=0; [[ "$label" == *INERT* ]] && inert=1
  select="${TIER_SELECT[$tier]}"
  creds="${TIER_CREDS[$tier]}"

  # --- credentials gate: a tier we cannot run is `skipped`, never `killed` -------------
  envargs=(-e "TEST_DATABASE_URL=$TEST_DATABASE_URL")
  case "$creds" in
    live|live+llm)
      if [ -z "${LIVE_API_TESTS:-}" ]; then
        echo "skipped   $label  (needs LIVE_API_TESTS=1)"; skipped=$((skipped + 1)); continue
      fi
      envargs+=(-e "LIVE_API_TESTS=$LIVE_API_TESTS") ;;
  esac
  if [ "$creds" = "live+llm" ]; then
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
      echo "skipped   $label  (needs ANTHROPIC_API_KEY — this tier spends real tokens)"
      skipped=$((skipped + 1)); continue
    fi
    envargs+=(-e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY")
  fi

  # --- apply the mutation to the COPY --------------------------------------------------
  if ! "${DC[@]}" -e "FROM=$from" -e "TO=$to" "$SVC" python - "$COPY/$file" <<'PY' 2>&1
import os, pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
frm = os.environ["FROM"].replace("\\n", "\n")
to = os.environ["TO"].replace("\\n", "\n")
if frm not in s:
    sys.stderr.write(f"mutation target not found in {p}:\n{frm!r}\n"); sys.exit(1)
if s.count(frm) != 1:
    sys.stderr.write(f"target occurs {s.count(frm)} times in {p}; it must be unique\n")
    sys.exit(1)
p.write_text(s.replace(frm, to, 1))
PY
  then
    echo "ERROR     $label — target string not found (or not unique); the code moved," >&2
    echo "          fix this script rather than the test." >&2
    fail=1
    "${DC[@]}" "$SVC" cp -- "/app/$file" "$COPY/$file" >/dev/null 2>&1
    continue
  fi

  log="$LOGDIR/$(printf '%02d' "$n")-${short}.log"
  # -x: stop at the first failure. The killer's name is what the report needs, and a
  # narrow selection plus a per-tier inert control is what makes attributing it sound.
  if "${DC[@]}" "${envargs[@]}" -w "$COPY" "$SVC" \
       sh -c "python -m pytest $select -q -x -rf -p no:cacheprovider" >"$log" 2>&1; then
    if [ "$inert" -eq 1 ]; then
      echo "survived (expected)  $label"
      killed=$((killed + 1)); inert_ok=$((inert_ok + 1))
    else
      echo "SURVIVED  $label"
      SURVIVORS+=("$label")
      survived=$((survived + 1)); fail=1
    fi
  else
    killer=$(grep -m1 '^FAILED ' "$log" | sed 's/^FAILED //')
    if [ "$inert" -eq 1 ]; then
      echo "KILLED AN INERT MUTANT  $label" >&2
      echo "          -> ${killer:-see $log}" >&2
      echo "          This tier is failing for a reason that is NOT the mutation, so" >&2
      echo "          every other number for it is meaningless." >&2
      broken_inert=$((broken_inert + 1)); fail=1
    else
      echo "killed    $label"
      echo "          by ${killer:-<no FAILED line; see $log>}"
      killed=$((killed + 1))
    fi
  fi

  # --- restore the copy from the pristine mount, and verify it ------------------------
  "${DC[@]}" "$SVC" cp -- "/app/$file" "$COPY/$file" >/dev/null 2>&1
  if ! "${DC[@]}" "$SVC" cmp -s "/app/$file" "$COPY/$file"; then
    echo "ERROR: $COPY/$file no longer matches /app/$file; the copy is polluted and" >&2
    echo "every result after this point is unattributable. Stopping." >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# The working tree must be exactly as we found it.
# ---------------------------------------------------------------------------
if ! git diff --quiet -- src/; then
  echo >&2
  echo "ERROR: src/ is dirty. This script never writes to src/, so something else did." >&2
  echo "Inspect 'git diff -- src/' before doing anything else." >&2
  exit 1
fi

real_killed=$((killed - inert_ok))
real_total=$((real_killed + survived))
echo
echo "killed ${real_killed}/${real_total} real mutants"
echo "inert controls: ${inert_ok}/$((inert_ok + broken_inert)) survived (all of them must)"
echo "${skipped} skipped for missing credentials"
echo "src/ clean: yes"

if [ "$broken_inert" -gt 0 ]; then
  echo >&2
  echo "AN INERT MUTANT WAS KILLED. Read nothing else in this run as a score: the tier it" >&2
  echo "belongs to is red for an unrelated reason, which makes a broken suite look" >&2
  echo "maximally sensitive. Fix that first, then re-run." >&2
fi
if [ "${#SURVIVORS[@]}" -gt 0 ]; then
  echo >&2
  echo "SURVIVING MUTANTS — each is a behaviour the suite does not protect:" >&2
  for s in "${SURVIVORS[@]}"; do echo "  - $s" >&2; done
  echo "Add the test that kills it. Do not weaken the mutant." >&2
fi
if [ "$fail" -eq 0 ]; then
  echo "every judged mutant was killed and every inert control survived"
fi
exit "$fail"
