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
