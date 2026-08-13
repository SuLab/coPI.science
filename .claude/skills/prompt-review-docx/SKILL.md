---
name: prompt-review-docx
description: >-
  Render this repo's prompt-set docs (docs/specs/*-bot-prompts.md) into styled
  Word (.docx) files for non-computational reviewers — each agent prompt shown
  as rendered, formatted text inside a labeled light-blue box instead of a raw
  code block, with the source file path captioned above each box so edits round-
  trip cleanly back to the prompts. Use when asked to build or regenerate the
  review docx, produce a Word/Blackbird-staff version of the prompts, or turn the
  prompt-set markdown into review-friendly boxed documents.
---

# Prompt-set → Word review docs

Turns the prompt-set markdown (`docs/specs/*-bot-prompts.md`, which embed each
agent prompt in a fenced code block) into a Word document where every prompt is
**rendered formatted text in a colored box**, captioned with its exact repo path.
This is for Blackbird scientists who won't read raw markdown/code but should be
able to comment on and edit the prompts.

## Build

```bash
# from the repo root
.claude/skills/prompt-review-docx/build.sh                 # rebuild all *bot-prompts.md
.claude/skills/prompt-review-docx/build.sh docs/specs/2026-08-07-pi-bot-prompts.md
```

Output is written next to each source as `<name>.docx` (these are **gitignored**
— the `.md` is the source of truth). pandoc is fetched to a local `.cache/` on
first run if it isn't already installed; otherwise only `python3` is needed.

## Input convention (already met by the prompt-set docs)

- Each prompt is wrapped in a **four-or-more backtick** fence (```` ```markdown ````
  / ```` ```text ````) — four, because the prompt body itself contains
  three-backtick ```` ```json ```` blocks. Everything inside becomes one boxed card.
- Each prompt is preceded by a `` *Source: `path/to/prompt.md`* `` line; it stays
  as the caption above the box. This label is what makes the round-trip 1:1.

Any other markdown (intros, section headings, the `*Source:*` labels) renders
normally, outside the boxes.

## Getting review comments back into the repo

Each box maps to exactly one prompt file (labeled). Reviewers use **Track Changes
+ comments** in Word (or Suggesting mode if you upload to Google Docs). To apply:
open the file named in a box's caption and make the same edit. Because the
literal plumbing — the `{curly_brace}` placeholders, the output-format JSON, and
the `<slack_message>` / `<assessment_json>` tags — is kept **verbatim** (monospace
inside the box), nothing gets silently reformatted.

## How it works (and the two non-obvious bits)

1. `transform.py` — unwraps each fenced prompt into a pandoc fenced div
   `::: {.promptbody custom-style="PromptBody"}`, and converts the prompt's own
   headings to **bold** paragraphs and its list items to standalone paragraphs
   (`•` / `N.`). *Why:* Word only shades plain paragraphs, so native lists/
   headings would leave holes in the box.
2. `pandoc … -f markdown-raw_html+fenced_divs` — renders it. `-raw_html` keeps
   the literal `<…>` tags instead of eating them as HTML. No `--toc` (a Word TOC
   field renders empty until updated — confusing for reviewers; Word's Navigation
   Pane covers structure).
3. `postprocess.py` — bakes the light-blue fill + blue left bar onto every
   `PromptBody` paragraph as **direct formatting**. *Why direct, not a style:*
   pandoc matches `custom-style` by style *name* and will otherwise create an
   empty `PromptBody` style, so a reference-doc style silently does nothing.

To change the box color, edit `FILL` / `BAR` in `postprocess.py`.

## Optional: preview without Word

```bash
cd .claude/skills/prompt-review-docx && npm i docx-preview jszip puppeteer
node preview.js ../../../docs/specs/2026-08-07-pi-bot-prompts.docx /tmp/preview.png 1800
```

Renders the `.docx` to a PNG via docx-preview in headless Chromium — an
approximation of Word, good enough to confirm the boxes show.
