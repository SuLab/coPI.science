# Simulation Control Panel — Requirements & Design Inputs

Status: REQUIREMENTS CAPTURE, pre-plan. The implementation plan
(`2026-08-XX-simulation-control-panel.md`, to be written) consumes this
document; nothing here is implemented. Gathered 2026-08-29 in conversation
with the operator, with every environmental claim re-verified fresh that day
(citations inline). The plan itself must re-verify anything marked OPEN.

## 1. Operator requirements (as stated)

1. A new admin page for simulation control: **start and stop** the simulation
   from the web UI.
2. **Control the run-start Slack announcement feature** (the 2026-08-29
   `run_marker` work): toggle/choose channels, customize the message.
3. **Input any other simulation settings**, e.g. the duration of a simulation
   run (`--max-runtime` today).
4. The panel must also **show statistics about the simulation run and its
   current progress** — all four brainstormed groups were selected:
   lifecycle+funnel, liveness+health, volume+tokens, specialists+per-agent.
5. **Dollar cost estimation**, with a price table built from the current
   Anthropic docs.
6. Plot types and analytical perspectives per the 2026-08-29 brainstorm and
   two-track literature review (§5–§8 below).
7. The plan must be confirmed with a deep, comprehensive adversarial analysis
   before implementation.

## 2. Verified environmental constraints (2026-08-29)

- **No container in this stack mounts the Docker socket** (read of the
  working-tree `docker-compose.prod.yml`: app/worker/agent mount only
  profiles/prompts/data). The web tier cannot start/stop containers.
  Any start/stop control therefore needs an in-process control plane.
- The `agent` service is a one-off (`profiles: [agent]`, run via
  `docker compose run`, no restart policy). Host has systemd (a
  `copi-backup.service` exists — observed in **failed** state, unrelated;
  flagged to operator) and empty crontabs for ubuntu and root.
- The engine already polls the DB every ~30s (`ROSTER_POLL_INTERVAL = 30.0`,
  `src/agent/simulation.py:245`) — the natural carrier for control commands
  and heartbeat writes. `SimulationEngine.request_stop()` is the graceful
  in-process stop (flips a flag; flush runs in the main loop's finally).
- `app_settings` KV (`src/models/provisioning.py:13`) is the established
  durable-runtime-config store the web tier can write;
  `cohort_audit_events` (`src/models/cohort.py:116`) is the audit-trail
  precedent for admin actions.
- Admin surface: `src/routers/admin.py` (`_DB`/`_ADMIN` Depends singletons at
  :91-92 — required by the ruff B008 ratchet), templates under
  `templates/admin/`, all POSTs behind the Origin guard and
  `get_admin_user`.
- Standing operator preference: **never auto-start the simulation.** The
  control design must be explicit-command-based — a supervisor process must
  come up IDLE after container recreate/host reboot; only a fresh, explicit
  admin command starts a run.
- Candidate architecture (to be confirmed in the plan with alternatives):
  run the agent service as an always-on supervisor (`up -d agent`) whose
  process idles until an explicit command row appears; start = spawn the run
  in-process, stop = `request_stop()` (strictly gentler than
  `docker stop -t 420`). Rebuild-for-code-changes keeps today's flow.

## 3. Control surface (inputs)

- Start (fresh vs resume), stop (graceful), with confirmation steps.
- Run settings at start: `max_runtime` minutes (0 = indefinite),
  fresh-start flag; expose other CLI-equivalent flags deliberately, not
  wholesale (`--all-agents`, `--reset-cursors` are sharp tools — plan decides
  which to expose and with what warnings).
- Announcement controls: enable/disable, channel list (today
  `RUN_START_ANNOUNCE_CHANNELS` env — plan must decide DB override vs env),
  message template (today `prompts/run_start_announcement.md`, bind-mounted
  rw into the app container — plan must weigh web-editing the file vs a DB
  override, incl. the render-dry-run validation the never-raise fallback
  already enables).
- Every control action writes an audit row (actor, action, payload) per the
  cohort_audit_events pattern.

## 4. Stats panel — operational ("Live" tab)

DB-derivable today unless marked HEARTBEAT (new plumbing: an engine-written
status row on the ~30s poll cadence; page treats >~2min staleness as "engine
not responding", which also gates the start/stop buttons' honesty):

- **KPI row / tiles:** total run cost (hero), $/hour burn rate, cache
  hit-rate meter (`cache_read/(input+cache_read)`), run progress meter
  (elapsed vs planned duration), headlines owed
  (`summary_posted_at IS NULL`, should be 0), interviews concluded,
  last-activity/stall banner (HEARTBEAT).
- **Lifecycle:** run id/status/fresh-vs-resumed, rubric version+hash stamp,
  git commit/branch/dirty from `.build_info.json`, prompt-set stamps,
  run-start announcement record (posted channels / failures).
- **Trends:** cumulative cost line; tokens/hour stacked area by token class
  (input/output/cache-read/cache-write); hub-vs-labs emphasis line.
- **Magnitude bars:** cost by agent / model / phase; consults per specialist
  domain; drops by reason.
- **Funnel:** opened → in-flight → concluded → announced; unvetted-panel
  count.
- **Specialists:** blocking/gap/adequate as a diverging stacked bar per
  domain (ordered scale centered on `gap`); panel fan-out per interview.
- **Per-agent table:** role, status/muted, messages, calls, last activity;
  active threads + rate-limiter allowance utilization (HEARTBEAT).
- **Interview timeline:** Gantt-style bar per interview thread (open →
  conclusion), colored by outcome, verdict/headline event marks — the truest
  "current progress" view; rows link to existing assessment detail pages
  (session-replay-lite).
- **Latency:** P50/P95/P99 per phase from `llm_call_logs.call_stats`
  (never means — `latency_ms` on the row is NOT a turn sum, per the model's
  own comment).
- **Error taxonomy:** stop-reason breakdown (normal / max_tokens / refusal /
  tool-error) trended per specialist and per phase, plus flush failures,
  poll errors, Slack refusals, `assessment_drops` with `unwritable_row`
  surfaced loudest.
- **Burn ratio:** hub:lab token ratio over time (runaway-coordination
  alarm — Anthropic multi-agent writeup).

## 5. Dollar cost estimation

Price table fetched 2026-08-29 from
`platform.claude.com/docs/en/about-claude/pricing` ($/MTok; the code uses the
**5-minute cache TTL** — `src/services/llm.py:147-148` — so cache-write =
1.25× input):

| Model | Input | Output | Cache write (5m) | Cache read |
|---|---|---|---|---|
| claude-opus-5 | 5.00 | 25.00 | 6.25 | 0.50 |
| claude-opus-4-6 | 5.00 | 25.00 | 6.25 | 0.50 |
| claude-sonnet-5 | 2.00 | 10.00 | 2.50 | 0.20 |
| claude-sonnet-4-6 | 3.00 | 15.00 | 3.75 | 0.30 |
| claude-haiku-4-5 | 1.00 | 5.00 | 1.25 | 0.10 |
| claude-fable-5 | 10.00 | 50.00 | 12.50 | 1.00 |

(The four models above the haiku row are exactly the distinct values in
production `llm_call_logs` as of 2026-08-29.)

Rules:
- Cost per `llm_call_logs` row =
  `input×in + output×out + cache_read×read + cache_creation×write`; the
  token columns are documented on the model as "correct as billing totals".
- The price table ships **versioned with an as-of date** (Sonnet 5's launch
  pricing was later made permanent — prices move); refreshing it is an edit
  to one data file.
- An unknown model renders **"unpriced"** and is surfaced — never a silent $0.
- Rows pre-`0036` have NULL cache columns → those runs display as "≥ $X"
  floors, labeled.
- No batch/fast-mode/inference-geo multipliers apply (plan must verify no
  `inference_geo` in the client construction).

## 6. Analysis tab (research-grade; second cut)

From the academic review (generative agents / ABM / LLM-as-judge /
peer-review science / survival analysis):

- **Panel agreement:** raw agreement % AND Krippendorff's alpha (or Gwet's
  AC1) across specialist domains per assessment; hub-vs-specialist agreement
  over time. Always both — skewed labels suppress kappa (peer-review
  literature's kappa paradox).
- **Disagreement as signal:** entropy of the specialist vote per assessment
  vs final band and vs human-reviewer overturn rate.
- **Judge-bias checks:** `weighted_score` vs reply length, tool-round count,
  conclusion ordinal — a positive trend is a bias flag, not idea quality.
- **Calibration:** hub confidence label vs `assessment_reviews`
  approve/disapprove rate per bucket (reliability diagram; data exists since
  `0039`).
- **Survival:** Kaplan-Meier interview survival by ordinal, stratified by end
  mode (CONCLUDE / timeout / shutdown-rescue), open threads **censored**.
- **Rater drift:** rolling-window score distributions within one rubric
  regime.
- **Weight sensitivity:** perturb the six rubric weights ±X%, count band
  flips.
- **Cross-run distributions:** box/violin of scores per run under the same
  rubric regime — one run is one stochastic draw.

Standing rules the pitfalls impose on EVERY view: longitudinal score plots
are regime-segmented by `rubric_version` stamp; single-run numbers labeled as
one draw; timeout/shutdown-ended threads are censored observations, not
failures; `total_api_calls`' 2026-08-22 unit change is rendered inline
wherever that column appears.

## 7. Conventions & vocabulary

- Metric naming follows **OpenTelemetry GenAI semantic conventions**
  (`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`, span
  kinds for inference/tool/agent) — names only; no OTel plumbing.
- Charts follow the dataviz skill's procedure (form → color-by-job →
  validated palette → mark specs → hover layer → accessibility): one axis,
  never dual-axis; categorical hues in fixed order; sequential for
  magnitude; diverging for the blocking/gap/adequate scale; series ≥2 always
  legended; tables as the accessibility fallback. Server-rendered page —
  charts as inline SVG or a minimal JS layer, no new frontend stack.

## 8. OPEN items the plan phase must verify (do not assume)

1. Join key for **cost-per-interview**: how `llm_call_logs` rows map to an
   interview thread (channel+phase+agent exist; thread attribution TBD —
   check `call_stats` / `messages_json` / any thread column).
2. Heartbeat storage design: new table vs `app_settings` vs `simulation_runs`
   column; write cadence and payload schema; staleness threshold.
3. Control-plane schema: command table (explicit, acked commands — never
   desired-state that could auto-start after reboot) + who consumes it
   (supervisor entrypoint vs main-loop poll) + `docker-compose.prod.yml`
   change needed for an always-on agent service (⚠️ that file is the
   never-commit working-tree edit — coordinate with the operator).
4. Whether announce settings move to DB (app_settings read at engine startup
   with env fallback) — interacts with `get_settings()` lru_cache.
5. Template editing surface: file-write via the rw prompts mount vs DB
   override; dry-run validation flow.
6. Chart rendering approach for a Jinja page (inline SVG generated
   server-side vs small chart lib) and the auto-refresh mechanism.
7. Which sharp CLI flags (`--all-agents`, `--reset-cursors`) are exposed.
8. Analysis-tab computations that need scipy/etc. — the image has no such
   dependency today; alpha/kappa/KM can be implemented dependency-free, but
   the plan must decide and cost it.

## 9. Sources (literature review, 2026-08-29)

Industry: OpenTelemetry GenAI semconv; Langfuse cost-management docs;
LangSmith observability; Helicone LLM-cost guide; AgentOps; Braintrust
eval guide; W&B Weave; Arize agent-observability; Anthropic "How we built
our multi-agent research system"; OpenAI Agents SDK tracing; "Seeing the
Whole Elephant" (arXiv:2604.22708).

Academic: Park et al. 2023 (arXiv:2304.03442); Vezhnevets et al. 2023
Concordia (arXiv:2312.03664); Grimm et al. ODD protocol; JASSS 18(4)/4 and
19(1)/5 (ABM output & sensitivity analysis); Zheng et al. 2023 LLM-as-judge;
Wang et al. 2023 "LLMs are not Fair Evaluators"; Bretschneider et al.
(PMC3642182) and Patat et al. (arXiv:1805.06981) on reviewer agreement;
Kaplan-Meier methodology (PMC5045282); "Find the Conversation Killers"
(arXiv:1712.08636).
