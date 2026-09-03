# Review bot: real unparseable `claude-opus-5` replies

These three files are verbatim (truncated) excerpts of real replies from
`claude-opus-5`, captured while running the review bot
(`src/services/review_bot.py`) against 12 production assessments during the
2026-09-03 live evaluation (Task 8 of
`docs/plans/2026-09-02-review-pipeline-test-and-hardening-plan.md`). 3 of the
12 replies were invalid JSON, for three different reasons: a premature
object close, an unterminated object at end of reply, and an unescaped `"`
inside a string value. The recovery is deliberately cause-agnostic — it
reads only the leading `target` key — because the failure surface is
broader than any one of these. In all three cases the model had chosen
`"target": "rubric"` as the object's first key, so `_parse_model_output`
degrading straight to `("out_of_scope", raw)` mislabelled a real,
recoverable suggestion. `src/services/json_extract.py::extract_json` cannot
repair any of the three — by design, it finds objects that are already
well-formed, it does not fix broken ones.

They are kept as regression fixtures for
`tests/unit/test_review_bot_edges.py::test_real_unparseable_opus_replies_keep_their_declared_target`
so the `_LEADING_TARGET_RE` recovery path is exercised against real model
output, not just hand-written malformed JSON.

| fixture | source | `json.loads` error | position | true cause |
|---|---|---|---|---|
| `unparseable_rubric_1.txt` | `baseline_scientific_gap_0` | `Extra data` | 1802 (not EOF) | premature object close — the model wrote `…is the direct target."},"rationale":"…`, closing the object one key early (after `suggestion`), so `rationale` is trailing data outside the object |
| `unparseable_rubric_2.txt` | `baseline_scientific_gap_2` | `Expecting ',' delimiter` | 4201 = EOF | unterminated object — the reply just ends after `…a dimension anchor."` with no closing brace at all, despite `stop_reason=end_turn` |
| `unparseable_rubric_3.txt` | `transcript_unavailable_0` | `Expecting ',' delimiter` | 984 (not EOF) | unescaped `"` inside a string value — `do not use the words "independent validation" in \`rationale\`` |

Truncation: the rule is `min(len(payload), JSONDecodeError.pos + 200)`
characters of the original captured payload — long enough to (a) still
start with `{"target": "rubric"` / `{"target":"rubric"`, (b) still fail
`json.loads` for the same reason the original did, and (c) stay small.
**Fixture 2 is untruncated** — its `pos` (4201) is exactly `len(payload)`
(EOF), so `pos + 200` exceeds the original length and `min()` returns the
payload exactly as captured; there is no tail past the model's own stopping
point to cut. Fixture 3 (`pos` 984) is a genuine `pos + 200` = 1184-character
cut. **Fixture 1 is truncated to 2200 characters, not 2002** (`pos + 200`):
at 2002 chars the file's first `}` falls at offset 1801, and
`extract_json`'s last-resort brace-matching branch parses `raw[0:1802]` as
a VALID object — so a fixture cut there would exercise the pre-existing
success path instead of the recovery under test. 2200 chars is past the
file's *second* `}` (offset 2178 — the closing brace of the inline
`{rubric}` placeholder, which lies inside the `rationale` value, not
`suggestion`: the premature close at offset ~1802 already ended the
object's only two real keys, `target` and `suggestion`, before `rationale`
begins at offset 1803) and short of its true final `}` (offset 3614), so
`extract_json` still fails on it exactly as it does on the untruncated
original. Do not "normalize" this to the documented rule — `assert body ==
raw` will fail.
