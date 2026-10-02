# Vendored front-end scripts

Served from `/static/vendor/` by `templates/_head_assets.html` (spec §6.2, M-04). Each file
is the package's published file as jsDelivr serves it from npm
(`https://cdn.jsdelivr.net/npm/<package>@<version>/<path>`), unchanged. The `source` tarball's
sha512 was checked against the registry's `dist.integrity` (recorded below). `sha384` is the
hash of the file itself, and `tests/unit/test_vendored_assets.py` recomputes every one; for
marked it equals the SRI the old jsDelivr tag carried.

marked stays on 12.x. Later majors replace the positional renderer API (`html(html, block)`,
`image(href, title, text)`, `link(href, title, text)`) that `static/js/markdown.js` uses.
To update DOMPurify, re-run the fetch in Task 1A-5 of
`docs/plans/2026-10-01-web-ui-remediation-phase-1.md`. Commit the new file, this table and
the `_head_assets.html` change together.

| file | package | version | source | tarball integrity | sha384 |
|---|---|---|---|---|---|
| `marked-12.0.2.min.js` | marked | 12.0.2 | https://registry.npmjs.org/marked/-/marked-12.0.2.tgz | `sha512-qXUm7e/YKFoqFPYPa3Ukg9xlI5cyAtGmyEIzMfW//m6kXwCy2Ps9DYf5ioijFKQ8qyuscrHoY04iJGctu2Kg0Q==` | `sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi` |
| `purify-3.4.16.min.js` | dompurify | 3.4.16 | https://registry.npmjs.org/dompurify/-/dompurify-3.4.16.tgz | `sha512-sqo+pNp3qRhCIpbgRi1y8Tgk27Bo2Ry7w0dC1NBeNTdZChWjz9Xb/KOoZbRP/R6pQZ80Qw8YhXw13hWWBbMRnQ==` | `sha384-a7SzOxErzJ3ZpQz0zJ32d67dSitNzPcbfybc/ykU9KJhMgZkwqfSxlhhdJRS+XGL` |
