# Task 24 — the remaining #23 test-and-comment gaps (R5, R6, R8, R9)

## Ruling

Task 24 is a FIX, but four calls were the executor's to make. They are recorded here because two of
them contradict the plan's own evidence text, and one of them is an instruction another group has to
carry out.

### 1. The R5 assertion went in `tests/unit/test_delegates.py`, and R5's premise is wrong

The plan corrected v1's file name (`tests/unit/test_delegate_slack_ids.py` does not exist) and left
the choice between `tests/unit/test_delegates.py` and a new file. **Chosen: `test_delegates.py`** —
it already owns the invite/delegate surface (`TestInviteRouter` imports `_accept_invitation`), so a
new file would have split one subject across three.

**But closure-23's R5 finding is a grep artifact, not a coverage hole.** `d0d9b3d`, the commit that
added the log line, shipped a behavioural test in the same commit:
`tests/integration/test_agent_page.py::test_accepting_an_invitation_with_no_bot_token_skips_the_sync_and_logs`,
still present at HEAD (`:968`), asserting `"no slack bot token" in r.getMessage().lower()`. The
audit grepped for the *full* log line, `"skipping delegate Slack-ID sync"`, which no test spelled
out — hence 0 hits. So R5 was never "a sub-PR that shipped without a test"; #23's DoD clause was
already met for V10b.

The new test is still worth its lines, for two reasons that are not "the audit said so":
- it asserts the **whole** message — the agent slug *and* the delegate's e-mail *and* the phrase an
  operator actually greps for — so the next person who runs closure-23's grep gets a hit;
- it is a unit test. The existing one costs a Postgres container to observe a pure in-Python
  `if/else`.

### 2. R8's tests protect `ed734f3` and `2d804b0`, not `b845ab5`

R8 says `_execute_retrieve_abstract` / `_execute_retrieve_full_text` "have no call-site test". The
underlying fact (closure-23's own wording) is that after the COR-30 refactor (`b845ab5`) **nothing in
`src/` calls those two helpers** — production goes `execute_tool` -> `_format_*_result`, while
`tests/unit/test_retrieve_tools_authors.py` still drives the orphans. So the #29 content guarantees
(author list, truncation at 20, DOI, SEC-14 fences) were pinned on a path production never runs.

The two new tests therefore assert that content **through `execute_tool`**. They are green against
`b845ab5~1` — the COR-30 refactor deliberately did not change the rendered text — and red against
`ed734f3~1` (no author list) and `2d804b0~1` (no DOI). That is the honest attribution: they are a
call-site copy of `ed734f3`'s and `2d804b0`'s guarantees, not a second test for COR-30.

**Deliberately not added:** a `test_a_successful_full_text_fetch_spends_the_budget` to mirror the
abstract case. It is a real symmetry gap in the file, but it is green against `b845ab5~1` (the
pre-fix code charged on success too), i.e. exactly the vacuous-assert pattern `## Definition of done`
records as **F14** for `test_prompt_hygiene.py`. Left as a note rather than a green test.

### 3. R9 — `simulation.py` was NOT touched. One instruction for Group A.

Group A owns `src/agent/simulation.py` exclusively and had uncommitted work in it throughout this
task (`git status --porcelain src/agent/simulation.py` -> ` M` at `bcef8be`). The docstring
correction is written out below instead of applied.

**Both citations in the plan are stale.** `_bot_uid_map` is defined at **`simulation.py:4737`** and
the false sentence is at **`:4741-4743`** (measured at `bcef8be`). The plan's `:4678` / `:4673` and
v1's `:4643` all land inside `_resolve_service_bot_uids`, a different method whose docstring already
says the *opposite* ("Deliberately NOT reusing grantbot.py's SuBot-token fallback"). Anchor on the
text, not the line number — Group A is editing this file.

**The falsifying commit is `dd82c91`** (`fix(grantbot): refuse to post under SuBot's Slack identity
with no dedicated token (#23 COR-26c)`), which added the `Do NOT fall back to SuBot's token` branch
at `src/agent/grantbot.py:570`. It is **not** `f1c28d1`, which is the *simulation-side* half of
COR-26c and changed only how grantbot's own uid is resolved.

> **Instruction for Group A (fold into Task 10's commit).** In `src/agent/simulation.py`, in
> `_bot_uid_map`'s docstring, replace these three lines:
>
> ```
>         never override a roster entry. The collision is real, not theoretical:
>         grantbot falls back to SuBot's token when its own is missing, and posts
>         made that way are su's — the roster answer is the true one.
> ```
>
> with:
>
> ```
>         never override a roster entry. The collision was real until `dd82c91`
>         (#23 COR-26c) stopped grantbot borrowing SuBot's token; posts already
>         made that way are still su's, so the roster answer stays the true one.
> ```
>
> Documentation only — the `setdefault` ordering it justifies is still correct, and the historical
> rows it protects are still in the production database. No test changes.

## Evidence

Every test below was proven red against an untouched export of the pre-fix code
(`git archive <commit>~1 | tar -x -C <scratch>/pre-<commit>`), with the new test file copied into the
export. **`src/` was never modified.** All four files pass at HEAD:

```
.venv-test/bin/python -m pytest tests/unit/test_delegates.py tests/contract/test_orcid_contract.py \
  tests/contract/test_grants_contract.py tests/unit/test_tools_budget.py -q -p no:cacheprovider
-> 52 passed
```

| test | protects | pre-fix export | failure message |
|---|---|---|---|
| `test_no_bot_token_logs_the_skipped_delegate_slack_id_sync` | `d0d9b3d` (V10b) | `d0d9b3d~1` = `df010e1` | `AssertionError: ['Delegate … accepted invitation for agent unconfigured']` — the only record logged |
| `test_fetch_orcid_record_retries_a_transient_503_then_succeeds` | `188196f` (COR-29a) | `2e6ab0c` | `httpx.HTTPStatusError: Server error '503 Service Unavailable' for url 'https://pub.orcid.org/v3.0/…/record'` |
| `test_fetch_orcid_record_gives_up_after_the_retry_budget` | `188196f` | `2e6ab0c` | `AssertionError: assert 1 == 4` (`route.call_count`) |
| `test_fetch_orcid_works_returns_the_works_after_a_transient_429` | `188196f` | `2e6ab0c` | `AssertionError: assert 1 == 2` (`route.call_count`); the 429 degraded to `[]` |
| `test_fetch_orcid_grants_retries_a_transport_error_then_succeeds` | `188196f` | `2e6ab0c` | `AssertionError: assert [] == ['R01 Big Grant']` |
| `test_search_opportunities_retries_a_transient_503_then_succeeds` | `9f9e559` (COR-29a) | `afba7e8` | `httpx.HTTPStatusError: Server error '503 Service Unavailable' for url 'https://api.grants.gov/v1/api/search2'` |
| `test_search_opportunities_gives_up_after_the_retry_budget` | `9f9e559` | `afba7e8` | `AssertionError: assert 1 == 4` (`route.call_count`) |
| `test_search_for_researchers_no_longer_drops_a_lab_on_one_transient_500` | `9f9e559` | `afba7e8` | `AssertionError: assert 1 == 2`; the lab's opportunities were swallowed to `[]` |
| `test_fetch_opportunity_detail_retries_a_transport_error_then_succeeds` | `9f9e559` | `afba7e8` | `respx.models.SideEffectError` / `httpx.ConnectTimeout: boom` |
| `test_execute_tool_abstract_answer_carries_the_authors_and_doi` | `ed734f3`, `2d804b0` | `fabc1e4`, `a90e4a3` | `AssertionError: assert ('<paper_authors>' in 'Title: …')` / `assert ('<paper_doi>' in 'Title: …')` |
| `test_execute_tool_full_text_answer_carries_the_authors_doi_and_methods` | `ed734f3`, `2d804b0` | `fabc1e4`, `a90e4a3` | same two, on the full-text answer |

Two notes on how the red runs were staged, so they can be re-run:

- The contract files' autouse `_no_retry_backoff` fixture does `monkeypatch.setattr(<module>,
  "_RETRY_BACKOFF", 0)`, and that constant does not exist pre-fix — the fixture would have errored
  with `AttributeError` and masked the real failure. The scratch copies use `raising=False` (one
  `sed`); nothing else differs from the committed file.
- `tests/unit/test_tools_budget.py` did not exist before `b845ab5`, so its first three (COR-30) tests
  fail in the `ed734f3~1` / `2d804b0~1` exports for an unrelated reason. The red runs select only the
  two new tests with `-k carries_the_authors`.

**No real network.** Both contract files run every request through `@respx.mock`, whose default
`assert_all_mocked=True` raises on any unmatched request rather than letting it out; the eight new
tests complete in well under a second each, and `_RETRY_BACKOFF = 0` plus an absent `Retry-After`
header makes every backoff `asyncio.sleep(0)`.

**No prompt text touched (D33).** All four modified files are under `tests/`; no file under
`prompts/` and no `src/` module is in this task's diff.

## Consequence a closing comment must state

- **#23's DoD clause is met** — but the closing comment should say *why* R5 looked unmet: the audit's
  grep was for the full log line, while `d0d9b3d`'s own test asserts a lowercased substring of it.
  Reporting R5 as "a sub-PR shipped without a test" would be wrong in the issue comment.
- `_execute_retrieve_abstract` / `_execute_retrieve_full_text` are **still dead code in `src/`**.
  This task pinned the shipped path instead of deleting them, because deleting them would also delete
  `tests/unit/test_retrieve_tools_authors.py`'s subject and that file is not in Task 24's ownership.
  Carry it as a residual: either delete both helpers and re-point that file at `execute_tool`, or
  keep them and accept two renderings of the same answer. **-> suggest a follow-up issue.**
- The `_bot_uid_map` docstring fix is **not in this commit**. If Group A's Task 10 commit does not
  contain it, #23's R9 is still open and the closing comment must say so.
