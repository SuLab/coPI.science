#!/usr/bin/env bash
# Mutation / repro experiments for the RCA. Runs ONLY in /home/ubuntu/cc-rca (a private clone
# at 398bc4d). Every mutation is reverted with `git checkout --` before the next one.
set -u
cd /home/ubuntu/cc-rca
PY=/home/ubuntu/blackbird-copi-science/.venv-test/bin/python
LOG=/home/ubuntu/cc-rca-repro.log; : > "$LOG"
say(){ echo -e "\n######## $*" | tee -a "$LOG"; }
run(){ echo "\$ $*" >> "$LOG"; timeout 300 "$@" >> "$LOG" 2>&1; local rc=$?; echo "=> exit $rc" | tee -a "$LOG"; }
restore(){ git checkout -q -- . ; test -z "$(git status --porcelain)" || { echo "RESTORE FAILED" | tee -a "$LOG"; exit 3; }; }
N20="tests/unit/test_reply_lane.py::test_thread_lock_then_agent_lock_does_not_deadlock_against_an_agent_lock_only_caller"
CTRL20="tests/unit/test_lock_registry.py::test_eviction_never_splits_mutual_exclusion_for_a_late_arrival"
N21="tests/unit/test_roles.py::test_visible_body_hides_the_verdict_the_sidecar_still_carries"
P="-q -p no:cacheprovider"

say "E0 baselines (unmodified)"
run $PY -m pytest $P "$N20" "$CTRL20" "$N21"
run $PY -m pytest $P tests/unit/test_specialist_floor.py -k "test_floor_arithmetic and (route or pass)" -v
run $PY -m pytest $P tests/unit/test_specialist_floor.py::test_route_to_incubation_owes_a_panel
run $PY -m pytest $P tests/unit/test_post_types.py tests/unit/test_post_type_enforcement.py

say "E1 I20-A: slow mock replaced by raise (line 877 only) -> expect PASS if never reached"
sed -i '877s/        await asyncio.sleep(0.05)/        raise AssertionError("I20: _update_agent_memory reached")/' tests/unit/test_reply_lane.py
sed -n 877p tests/unit/test_reply_lane.py >> "$LOG"
run $PY -m pytest $P "$N20"
restore

say "E2 I20-B: any contended acquire deadlocks -> node expect PASS, control expect FAIL"
sed -i '63i\                if lock.locked(): await asyncio.Event().wait()' src/agent/locks.py
sed -n 60,65p src/agent/locks.py >> "$LOG"
run $PY -m pytest $P "$N20"
run $PY -m pytest $P "$CTRL20"
restore

say "E3 I23-A: route-to-incubation exempt again -> route row expect PASS, control expect FAIL"
sed -i '921s/frozenset({"pass"})/frozenset({"pass", "route-to-incubation"})/' src/agent/specialists.py
sed -n 921p src/agent/specialists.py >> "$LOG"
run $PY -m pytest $P tests/unit/test_specialist_floor.py -k "test_floor_arithmetic and route" -v
run $PY -m pytest $P tests/unit/test_specialist_floor.py::test_route_to_incubation_owes_a_panel
restore

say "E4 I23-B: floor always armed -> route row expect FAIL, pass row expect PASS"
sed -i 's/^        armed = thread.floor_armed if thread is not None else bool(self._specialist_consults)$/        armed = True/' src/agent/simulation.py
grep -n "^        armed = True$" src/agent/simulation.py >> "$LOG"
run $PY -m pytest $P tests/unit/test_specialist_floor.py -k "test_floor_arithmetic and (route or pass)" -v
restore

SENT=" When concluding, state which gating criteria are met or not met, the red flags, and your recommendation (advance / conditional / pass) in the visible reply."
say "E5 I21-B1: inline-verdict instruction added OUTSIDE the checked slice (line 128) -> expect PASS"
sed -i "128s|concrete screening judgement\.|concrete screening judgement.$SENT|" prompts/roles/scout_hub/phase4-thread-reply.md
sed -n 128p prompts/roles/scout_hub/phase4-thread-reply.md >> "$LOG"
run $PY -m pytest $P "$N21"
restore
say "E6 I21-B2: same sentence INSIDE the slice (end of line 142) -> expect FAIL"
sed -i "142s|\$|$SENT|" prompts/roles/scout_hub/phase4-thread-reply.md
sed -n 142p prompts/roles/scout_hub/phase4-thread-reply.md >> "$LOG"
run $PY -m pytest $P "$N21"
restore

say "E7 I21-A: rendered CONCLUDE prompt asks for the inline verdict (before the sidecar section)"
run $PY -c 'from src.agent.agent import Agent; from src.agent.state import ThreadState
a=Agent("blackbird","BlackbirdBot","Blackbird Labs",role="scout_hub")
t=ThreadState(thread_id="t1",channel="general",other_agent_id="wang",message_count=11)
_,m=a.build_phase4_prompt(thread=t,thread_history=[{"sender":"WangBot","content":"pitch"}],other_agent_name="WangBot",other_agent_lab="Wang")
c=m[0]["content"]; pre=c[:c.index("### Concluding with an Opportunity Assessment: the sidecar")]
print({w:(w in pre) for w in ("not met","red flags","advance","route-to-incubation","confidence label")})
print("contradiction present:", "none of it may appear anywhere in" in c)'

say "E8 I12: alias emptied -> expect only test_the_retired_idea_name_still_resolves to fail"
sed -i '84s/{"idea": "idea_crosslab"}/{}/' src/agent/post_types.py
sed -n 84p src/agent/post_types.py >> "$LOG"
run $PY -m pytest $P tests/unit/test_post_types.py tests/unit/test_post_type_enforcement.py
restore

say "E9 I2/I4 sizing threshold + E10 I3 compare_row_counts"
run $PY -c '
import importlib.util, sys
from pathlib import Path
def load(n,f):
    s=importlib.util.spec_from_file_location(n, Path("scripts/migrate")/f); m=importlib.util.module_from_spec(s); sys.modules[n]=m; s.loader.exec_module(m); return m
pf=load("copi_migrate_preflight","preflight.py"); po=load("copi_migrate_postflight","postflight.py")
for r in (410_416, 410_417):
    print(r, pf.estimate_lock_window_ms(r), pf.sizing_status(r, pf.estimate_lock_window_ms(r)[1]))
print("POST_0019_STARTS", pf.POST_0019_STARTS, "DEFAULT_TARGET", pf.DEFAULT_TARGET)
print("CHAIN_CREATED_TABLES", sorted(po.CHAIN_CREATED_TABLES))
print(pf.compare_row_counts({"users":3},{"users":3,"assessment_chat_turns":0,"assessment_chat_usage":0},expected_new=po.CHAIN_CREATED_TABLES))
'

say "E11 I1: alembic upgrade 0027 from 0050 on a throwaway postgres"
docker rm -fv cc-rca-pg >/dev/null 2>&1
docker run -d --name cc-rca-pg -e POSTGRES_USER=copi -e POSTGRES_PASSWORD=copi -e POSTGRES_DB=rca -p 127.0.0.1:55620:5432 postgres:15 >/dev/null
for _ in $(seq 1 60); do docker exec cc-rca-pg pg_isready -U copi -q >/dev/null 2>&1 && break; sleep 1; done; sleep 2
export DATABASE_URL="postgresql+asyncpg://copi:copi@127.0.0.1:55620/rca"
run $PY -m alembic upgrade 0050
run $PY -m alembic current
run $PY -m alembic upgrade 0027
run $PY -m alembic current
unset DATABASE_URL
docker rm -fv cc-rca-pg >/dev/null 2>&1 && echo "container removed" | tee -a "$LOG"

say "E12 I7a: M12b target string present in pubmed.py?"
run grep -c -F '    """Make a rate-limited, identified GET request to NCBI."""' src/services/pubmed.py

say "final clone status"; git status --porcelain | tee -a "$LOG"; echo "(empty = clean)" | tee -a "$LOG"
