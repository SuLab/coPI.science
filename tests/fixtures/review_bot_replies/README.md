# Review bot: real unparseable `claude-opus-5` replies

These three files are verbatim (truncated) excerpts of real replies from
`claude-opus-5`, captured while running the review bot
(`src/services/review_bot.py`) against 12 production assessments during the
2026-09-03 live evaluation (Task 8 of
`docs/plans/2026-09-02-review-pipeline-test-and-hardening-plan.md`). 3 of the
12 replies embedded an unescaped `"` inside a JSON string value (for example,
quoting a phrase like `"independent validation"` or `"a demonstration, not a
measurement"` without escaping it), which makes the reply invalid JSON:
`json.loads` fails, and `src/services/json_extract.py::extract_json` cannot
repair it — by design, it finds objects that are already well-formed, it does
not fix broken ones. In all three cases the model had chosen
`"target": "rubric"` as the object's first key, so `_parse_model_output`
degrading straight to `("out_of_scope", raw)` mislabelled a real,
recoverable suggestion.

Each file here is truncated to `JSONDecodeError.pos + 200` characters of the
original captured payload — long enough to (a) still start with
`{"target": "rubric"` / `{"target":"rubric"`, (b) still fail `json.loads` for
the same reason the original did, and (c) stay small. They are kept as
regression fixtures for
`tests/unit/test_review_bot_edges.py::test_real_unparseable_opus_replies_keep_their_declared_target`
so the `_LEADING_TARGET_RE` recovery path is exercised against real model
output, not just hand-written malformed JSON.

- `unparseable_rubric_1.txt` — from `baseline_scientific_gap_0`: the object
  actually closes validly after `target`/`suggestion` (an earlier unescaped
  quote elsewhere in the full reply causes the parser to see the rest as
  `Extra data`).
- `unparseable_rubric_2.txt` — from `baseline_scientific_gap_2`: an unescaped
  `"` inside a quoted phrase inside `rationale` (`"a demonstration, not a
  measurement"`) breaks the string; the parse error lands at end-of-file.
- `unparseable_rubric_3.txt` — from `transcript_unavailable_0`: an unescaped
  `"` around `"independent validation"` inside `rationale` breaks the string
  one delimiter early.
