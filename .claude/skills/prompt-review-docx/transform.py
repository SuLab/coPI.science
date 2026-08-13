#!/usr/bin/env python3
"""Rewrite a prompt-set doc so each fenced prompt block renders as a boxed,
formatted "card" instead of a monospace code dump.

Input convention (what docs/specs/*-bot-prompts.md already follow):
  - Each prompt is wrapped in a FOUR-or-more backtick fence (```` ```markdown /
    ```text ````), because the prompt body itself may contain three-backtick
    ```json blocks. Three-backtick fences are treated as inner code and left
    verbatim.
  - Each prompt is preceded by a "*Source: `path`*" label line (left as-is; it
    becomes the caption above the box).

For each fenced prompt block we:
  - drop the outer fence and wrap the body in a pandoc fenced div
    `::: {.promptbody custom-style="PromptBody"}` … `:::`
  - convert the prompt's own ATX headings to **bold** paragraphs and its list
    items to standalone paragraphs (`•` / `N.`). This matters because Word only
    shades PLAIN paragraphs via a style/direct-formatting; native lists/headings
    would leave holes in the colored box. Tables and ```code``` blocks stay
    native (they render richly and sit inside the card).

The shading itself is applied afterwards by postprocess.py (direct formatting),
so pandoc's reference doc does not need a custom style.
"""
import re, sys

src, out = sys.argv[1], sys.argv[2]
lines = open(src, encoding="utf-8").read().splitlines()
res, i = [], 0
outer = re.compile(r"^````+\s*[\w-]*\s*$")   # 4+ backticks = prompt block boundary
inner = re.compile(r"^\s*```")               # 3-backtick = inner code fence
h = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
bullet = re.compile(r"^(\s*)[-*]\s+(.*)$")
num = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")

while i < len(lines):
    if outer.match(lines[i]):
        i += 1
        body, in_code = [], False
        while i < len(lines) and not outer.match(lines[i]):
            ln = lines[i]
            if inner.match(ln):
                in_code = not in_code
                body.append(ln)
            elif in_code:
                body.append(ln)                                   # verbatim in code
            elif h.match(ln):
                if body and body[-1].strip(): body.append("")
                body.append(f"**{h.match(ln).group(2)}**")        # heading -> bold para
                body.append("")
            elif bullet.match(ln):
                if body and body[-1].strip(): body.append("")     # separate each item
                m = bullet.match(ln); body.append(f"{m.group(1)}• {m.group(2)}")
            elif num.match(ln):
                if body and body[-1].strip(): body.append("")
                m = num.match(ln); body.append(f"{m.group(1)}{m.group(2)}\\. {m.group(3)}")
            else:
                body.append(ln)
            i += 1
        i += 1
        res.append('::: {.promptbody custom-style="PromptBody"}')
        res.append("")
        res.extend(body)
        res.append("")
        res.append(":::")
    else:
        res.append(lines[i]); i += 1

open(out, "w", encoding="utf-8").write("\n".join(res) + "\n")
print("transform:", out)
