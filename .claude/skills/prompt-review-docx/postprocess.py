#!/usr/bin/env python3
"""Bake the colored box directly onto every prompt paragraph.

pandoc marks each prompt paragraph with pStyle="PromptBody" (from the
custom-style div in transform.py). We inject a light-blue shading + a blue
left border as DIRECT paragraph formatting — which Word always renders,
independent of whether the "PromptBody" style itself carries formatting.
Consecutive PromptBody paragraphs share identical borders, so Word merges
them into one continuous box; tables / code blocks between them sit as their
own native elements inside the card.

Tune the two colors here if you want a different box.
"""
import zipfile, re, sys

src, dst = sys.argv[1], sys.argv[2]

FILL = "DCE9FB"     # box background (light blue)
BAR = "2F5C99"      # left accent bar (blue)
EDGE = "9DBBE6"     # thin surrounding border

# schema order inside <w:pPr> is pStyle, pBdr, shd, … so insert right after pStyle
DIRECT = (
    f'<w:pBdr>'
    f'<w:top w:val="single" w:sz="6" w:space="1" w:color="{EDGE}"/>'
    f'<w:left w:val="single" w:sz="24" w:space="8" w:color="{BAR}"/>'
    f'<w:bottom w:val="single" w:sz="6" w:space="1" w:color="{EDGE}"/>'
    f'<w:right w:val="single" w:sz="6" w:space="4" w:color="{EDGE}"/>'
    f'</w:pBdr>'
    f'<w:shd w:val="clear" w:color="auto" w:fill="{FILL}"/>'
)

zin = zipfile.ZipFile(src)
doc = zin.read('word/document.xml').decode('utf-8')
pat = re.compile(r'(<w:pStyle w:val="PromptBody"\s*/>)')
n = len(pat.findall(doc))
doc = pat.sub(r'\1' + DIRECT, doc)

with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
    for item in zin.namelist():
        data = doc.encode('utf-8') if item == 'word/document.xml' else zin.read(item)
        zout.writestr(item, data)
zin.close()
print(f"postprocess: shaded {n} paragraphs -> {dst}")
