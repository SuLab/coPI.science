# Assessment chat

Reference detail behind the rules in the root `CLAUDE.md`, which keeps only what
every session needs. Dated statements hold as of the date they give; re-measure a
count or a line number before relying on it.

`/admin/assessments/{id}` and `/manager/assessments/{id}` carry an "Ask about this
assessment" drawer: admin, manager and reviewer can ask questions about THAT assessment
and get short, cited, streamed answers from `claude-opus-5-5`
(`settings.llm_assessment_chat_model`). Design and evidence:
`docs/specs/2026-09-24-assessment-chat-design.md`; plan:
`docs/plans/2026-09-24-assessment-chat-plan.md`.

- **The record is the page, per tier.** `src/services/assessment_chat_record.py` builds
  five citation documents — the verdict, the interview transcript, the specialist panel
  findings, the human reviews, and the rubric definitions the page shows — from
  `build_assessment_detail(admin_view=False)`. Admin and manager share the `staff` tier;
  a reviewer is the `reviewer` tier and never receives `STAFF_ONLY_VERDICT_FIELDS`
  (strengths, risks, competitive landscape, evidence maturity): the page gates those in
  the TEMPLATE, so the record gates them itself.
  `tests/integration/test_assessment_chat_parity.py` fails if a record quotes anything
  its tier's page does not render. No tier ever gets `raw_verdict`, a specialist's
  `raw_opinion`, `context_excerpt`, anything from `llm_call_logs`, or another
  assessment. If the page ever hides a further field (e.g. `score_rationale`) from
  reviewers, add it to `STAFF_ONLY_VERDICT_FIELDS` in the same change.
- **No tools.** The model sees only the record and the conversation; what a tier may not
  see is not in the request at all.
- **Prompt:** `prompts/assessment-chat.md`, read per question (bind-mounted: an edit
  applies to the next question, no restart). Missing file: the ask route answers 503.
- **Persistence (migration `0051`).** `assessment_chat_turns` holds content, private per
  (assessment, user, tier); it CASCADEs from the assessment — so the engine superseding a
  provisional verdict deletes that verdict's chats — and from the user.
  `assessment_chat_usage` is the content-free ledger (tokens per model, no text); every
  FK is SET NULL, so it survives Clear, assessment deletion and user deletion, and the
  caps count it. `delete_user_account` needed no change.
- **"Private" means private from other users of the app, admins included** — every chat
  route refuses an impersonated session, reads too. It does NOT mean private from
  operators: the database, its dumps (`~/backups-blackbird`) and the local audit copy
  all hold the text, and Clear does not reach copies.
- **Limits.** Settings (`src/config.py`; the 24 h windows are rolling and counted from
  the ledger): 100 questions per user; $20 per user and $100 in total, priced by
  `src/services/llm_pricing.py` (an unpriced chat model makes the ask route answer 503,
  because it would blind the ceilings); 50 turns per conversation; 4 000-character
  questions. Constants in `src/services/assessment_chat.py`, not settings: a ledger row
  with no recorded usage counts the $2.50 `SPEND_RESERVE_USD`, and so does — as a floor —
  a row whose usage is `partial` (the answer ended before the API's closing usage
  report, so its output count is a placeholder); 150 000 characters of replayed
  history (`HISTORY_REPLAY_MAX_CHARS`); a 240 s deadline (`DEADLINE_SECONDS`);
  `streaming` rows older than 300 s swept to `interrupted` by the next chat request of
  any user (`STALE_AFTER_SECONDS`); and `max_tokens` 12 000, a literal in
  `build_request` for the non-streaming scan. One answer in flight per user (refused
  before anything else, and enforced by a partial unique index). Only the two dollar
  checks and the insert run under a transaction-scoped advisory lock, so concurrent
  asks cannot all pass the $100 ceiling together; an ask that cannot take it within
  5 s (`SPEND_LOCK_WAIT_SECONDS`) is refused 503 `busy`.
- **Links (D20).** A link in an answer is clickable only if its URL is, token for token,
  one the record quotes. The server stores such a bare URL as a `<…>` autolink and turns
  every other URL, e-mail address, raw HTML tag and reference definition into inline
  code; the drawer keeps an `<a>` only when its href EXACTLY equals an allowed URL or
  marked's own encoding of it (nothing is decoded), renders raw HTML as text, and
  marks citations with a random per-page nonce the model never sees, so no text it
  writes can forge one. A link that
  spans a citation boundary makes the server merge that answer's citations into one
  segment (one WARNING, `a link spanned a citation boundary`).
- **Refusals**, in order, all JSON `{"error": code}` with `no-store`: an impersonated
  session (403 `impersonating`), a role other than admin/manager/reviewer (403
  `forbidden`), the kill switch (503 `disabled`), an unknown or malformed id (404
  `not_found`), a POST that is not JSON (415); then the ask's own checks (the limits
  above, and 503 `busy` for the ceiling lock). On shutdown the web process waits up to
  8 s for answers still being written (`drain_live_tasks`, via `create_app`'s
  lifespan); anything longer is swept to `interrupted` later.
- **Streaming through org1's nginx.** Answers are SSE with `X-Accel-Buffering: no` and
  a `: ping` every 15 s, because the blackbird vhost buffers proxied responses and ends
  a read after 120 s of silence (`/home/ubuntu/copi-python/nginx/nginx.conf`, org1's
  file — not ours to edit).
- **Fallbacks:** every request sends `fallbacks: "default"` (beta
  `server-side-fallback-2026-07-01`); a fallen-back answer names the model that served
  it, and a refused one is shown as refused and never replayed.
- **Kill switch:** `ASSESSMENT_CHAT_ENABLED=false` in `.env`, then
  `$DC up -d --force-recreate blackbird-app` — the button disappears and the routes
  answer 503.
- **Not logged to `llm_call_logs`** (like the review bot); the ledger is the cost
  record. Operator SQL:

      -- questions and tokens per day and model
      SELECT date_trunc('day', created_at) AS day, model, served_by_model, status, count(*),
             sum(input_tokens), sum(output_tokens), sum(cache_read_input_tokens),
             sum(cache_creation_input_tokens)
      FROM assessment_chat_usage GROUP BY 1, 2, 3, 4 ORDER BY 1 DESC;
      -- refusals by category (no content)
      SELECT refusal_category, count(*) FROM assessment_chat_turns
      WHERE status = 'refused' GROUP BY 1;

**Tildes render literally** (2026-09-29, P0-12): the chat's private marked instance
disables GFM strikethrough like the detail pages do, so "~30%" is not turned into a
deleted span.

**Verdict revisions (B6).** A turn records the verdict revision it was asked against.
When the hub updates the verdict, earlier turns stay visible below a "Verdict updated after
this point" divider but leave replay and the turn cap; turns from before `0054` (NULL)
count as revision 1, so their conversations continue unchanged.

**History under the spend lock (LC-07).** `prepare_turn` re-reads the history under the
spend lock and builds the request from those turns, so a turn another request of the same
user committed meanwhile is included; with no concurrent writer the request is
byte-identical to before. Clear takes the same lock before its streaming check.

**Busy input (LC-09).** The question textarea is disabled while an answer streams.

**One markdown factory (LC-02).** `window.createSanitizingMarked(profile)` in
`static/js/markdown.js` builds the private marked instance for the `page`, `chat` and
`graph` profiles; every profile treats "~" as literal text.

**Drawer layout (2026-10-01).** The drawer overlays the page at every width:
opening it never reflows the page, which stays scrollable and clickable behind it
(below `md` it is full screen and the page behind is `inert`, as before). From `md`
up its left edge is a resize handle — drag it, press ←/→ while it has focus, or
double-click to reset to 28rem — bounded to 320 px … 75% of the window. The chosen
width is kept per browser in `localStorage["assessment-chat-width"]`, a number and
nothing else.

## Opening questions (2026-10-05, migration `0059`)

The drawer opens on up to four questions about THIS assessment, and each question that
is about one page section also appears as an "Ask:" button at the end of that section
(brief, evidence, the ask, verdict, panel, gating, red flags, rationale, scores). A click
SENDS the question: it counts toward the daily caps like a typed one, and while an
answer is still being written it is refused with the usual message. The aim is more and
better human reviews, so the questions point at what a reviewer has to judge.

- **Generated sets.** `src/services/assessment_chat_suggestions.py`. While the worker
  has no job it generates one set per (assessment, tier, verdict revision), newest
  assessment first, one model call at a time (`generate_due`, at most every 30 s), so a
  job waits behind at most one call. The request is that tier's chat record (the same
  five documents, citations off), `prompts/assessment-chat-suggestions.md` (read per
  generation; an edit applies to the next set and keeps the existing ones) and a numbered
  list of the Verdict document's blocks, answered as structured JSON. A question is kept
  only if it names a listed block (that block's page anchor places it inline), is one
  plain line of 10–200 characters with no link or markup, and repeats no block or
  question; fewer than two kept and the set fails. The staff tier is always generated;
  the reviewer tier only once a `reviewer` account exists. Model:
  `llm_assessment_chat_suggestions_model` (`claude-opus-5`, with `fallbacks: "default"`;
  not the chat's 5.5, whose biology classifier declined every production chat question),
  about $0.50 a set at the record's ~90 k input tokens.
- **The template set.** Until a set is `ready`, and whenever none can be, the page builds
  questions from fields every tier's page shows: a not-met (else unconfirmed) gate, the
  lowest-scored dimension, the first red flag, the recommendation, the ask — padded with
  the drawer's original three generic questions when the verdict has fewer than two.
- **Failures.** A row is written BEFORE each call (`failed`/`in_progress`, attempts + 1,
  priced at the reserve), so a paid attempt is counted even if the worker dies before
  storing its result. A failed set is retried after 30 minutes, at most three attempts
  per revision; a 429 or 5xx answer (never billed) is retried without using one up; a
  refusal is never retried. One call is limited to 180 s with no SDK retry, so a job
  waits behind at most that. A record that raises while being built is skipped (one
  ERROR in the worker log) until the worker restarts. To regenerate a set, delete its
  row; the next idle sweep redoes it.
- **Limits and switches.** `ASSESSMENT_CHAT_SUGGESTIONS_DAILY_USD_LIMIT` (default 30):
  rolling 24 h over the suggestion rows' own usage (an attempt whose cost is unknown —
  in flight, timed out, cut off — counts the chat's $2.50 reserve), separate from the
  chat's ceilings. An unpriced model or a missing prompt file stops generation with one WARNING.
  `ASSESSMENT_CHAT_SUGGESTIONS_ENABLED=false`, or the chat's own kill switch, stops
  generation; both are `.env` settings, so recreate the worker (and `blackbird-app` for
  the chat switch). The page then shows the template set (or, with the chat off, nothing).
- **Measurement, content-free.** `assessment_chat_opens` has one row per drawer opening,
  with `opened_via` = `bubble`, `link` (the list page's `#chat`) or `inline`; its foreign
  keys SET NULL, like the ledger's. `assessment_chat_usage.question_origin` says where each
  question came from: `typed`, `drawer_generated`, `drawer_template`, `inline_generated`,
  `inline_template` (NULL before `0059`). Neither counts an impersonated session.

      -- openings and questions per day, by entry point and origin
      SELECT date_trunc('day', created_at) AS day, opened_via, count(*)
      FROM assessment_chat_opens GROUP BY 1, 2 ORDER BY 1 DESC;
      SELECT date_trunc('day', created_at) AS day, question_origin, count(*)
      FROM assessment_chat_usage GROUP BY 1, 2 ORDER BY 1 DESC;
      -- human reviews written after their author asked the chat about that assessment
      SELECT count(*) FILTER (WHERE asked) AS after_chat, count(*) AS reviews FROM (
        SELECT EXISTS (SELECT 1 FROM assessment_chat_usage u
                       WHERE u.user_id = r.reviewer_user_id
                         AND u.assessment_id = r.assessment_id
                         AND u.created_at < r.created_at) AS asked
        FROM assessment_reviews r) t;
      -- generation outcomes
      SELECT context_tier, status, error_code, count(*), sum(attempts)
      FROM assessment_chat_suggestions GROUP BY 1, 2, 3;
