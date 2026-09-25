#!/usr/bin/env bash
set -u
cd /home/ubuntu/cc-rca
PY=/home/ubuntu/blackbird-copi-science/.venv-test/bin/python
LOG=/home/ubuntu/cc-rca-repro2.log; : > "$LOG"
say(){ echo -e "\n######## $*" | tee -a "$LOG"; }
run(){ echo "\$ $*" >> "$LOG"; timeout 900 "$@" >> "$LOG" 2>&1; local rc=$?; echo "=> exit $rc" | tee -a "$LOG"; }
restore(){ git checkout -q -- . ; test -z "$(git status --porcelain)" || { echo "RESTORE FAILED" | tee -a "$LOG"; exit 3; }; }
P="-q -p no:cacheprovider"
N24="tests/integration/test_cohort_admin.py::test_pi_facing_thread_view_is_never_cohort_filtered"
N29="tests/integration/test_concurrent_web_writes.py::test_concurrent_proposal_reviews_do_not_500"

say "R0 baselines"
run $PY -m pytest $P "$N24" "$N29"

say "R1 I24b: root_posts = [] (discussions view lists nothing) -> expect PASS if vacuous"
sed -i 's/^    root_posts = roots_result.scalars().all()$/    root_posts = []/' src/services/directory.py
grep -n "^    root_posts = \[\]$" src/services/directory.py >> "$LOG"
run $PY -m pytest $P "$N24"
restore

say "R2 I29: lost-race branch returns a 500 Response -> expect PASS if response unasserted"
python3 - <<'PYEOF'
p="src/routers/agent_page.py"; s=open(p).read()
old="""        await db.rollback()
        return RedirectResponse(
            url=f"/agent/{agent_id}/dashboard", status_code=302
        )"""
assert s.count(old)==1, s.count(old)
s=s.replace(old, """        await db.rollback()
        from fastapi.responses import Response
        return Response(status_code=500)""")
open(p,"w").write(s); print("mutated")
PYEOF
grep -n "return Response(status_code=500)" src/routers/agent_page.py >> "$LOG"
run $PY -m pytest $P "$N29"
restore

say "R3 M2: consult's own truncation retry not booked (drop on_retry=on_api_call) -> which tests fail?"
grep -n "on_retry=on_api_call," src/agent/tools.py >> "$LOG"
sed -i 's/^\( *\)on_retry=on_api_call,$/\1# on_retry removed (mutation)/' src/agent/tools.py
grep -n "on_retry removed (mutation)" src/agent/tools.py >> "$LOG"
run $PY -m pytest $P -x --no-header tests/unit tests/integration/test_specialist_consult_capture.py
restore

say "R4 I37: ruff counts"
run $PY -m ruff check src/routers/admin.py --select B008 --output-format=concise --quiet
echo "admin.py B008 count: $($PY -m ruff check src/routers/admin.py --select B008 --output-format=concise --quiet | grep -c .)" | tee -a "$LOG"
echo "src B008 count: $($PY -m ruff check src --select B008 --output-format=concise --quiet | grep -c .)" | tee -a "$LOG"
echo "src total findings (ci ratchet, ceiling 231): $($PY -m ruff check src --output-format=concise --quiet | grep -c .)" | tee -a "$LOG"

say "R5 I36: is a new static/js file ignored?"
run git check-ignore -v static/js/new_asset.js

say "final clone status"; git status --porcelain | tee -a "$LOG"; echo "(empty = clean)" | tee -a "$LOG"
