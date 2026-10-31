"""Open-access full text for S6 (spec §6.2, D6, D41; plan Task 5.1).

Routes, tried in order:

1. PMC (``pmcid`` known): the article's metadata JSON in the ``pmc-oa-opendata`` bucket, read
   over anonymous HTTPS (no AWS SDK), then its JATS XML; the BioC API is the alternate route
   when the bucket's XML cannot be read.
2. Europe PMC ``fullTextXML``, when its ``license`` is in `COMMERCIAL_LICENSES`.
3. bioRxiv / medRxiv JATS, from the ``details`` endpoint's ``license`` and ``jatsxml``.
4. Otherwise the summary.

A route ends in one of three ways: full text; a final "summary only" (a license outside
`COMMERCIAL_LICENSES`, an author manuscript or a retraction, D6), which no later route
overrides; or nothing, and the next route is tried. A transient error (HTTP 429/5xx or a
transport failure, still failing after 3 retries) on a route makes the result
``fetch_failed`` unless a later route yields full text, so a failed fetch is never recorded
as "summary only" (D41).

Every request accepts only ``gzip`` and ``deflate``, and every response body is read up to
`MAX_BODY_BYTES` and dropped past it, counted after a bounded decode of that coding (any other
coding, or a truncated compressed body, is not read); an error status's body is never read,
so only the status decides. Redirects are followed by hand, at most `_MAX_REDIRECTS`, only to
``https`` URLs on the route's own hosts, never reading a redirect's body; the preprint JATS
fetch, whose URL comes from the ``details`` record, must itself be ``https`` on
`_PREPRINT_HOSTS`. A dropped or malformed body, a refused redirect or a request httpx cannot
make (a bad URL, a host that is not valid IDNA, a bad redirect ``Location`` or coding) leaves
the route with nothing.
"""
from __future__ import annotations

import gzip
import logging
import re
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import evidence
from src.models.landscape import LandscapeText
from src.services.http_pacing import TRANSIENT_STATUSES, Pacer, with_retries
from src.services.identifiers import is_preprint_doi, normalize_doi, normalize_pmid

logger = logging.getLogger(__name__)

#: The licenses under which full text is read (D6), in `_normalize_license`'s forms.
COMMERCIAL_LICENSES = frozenset({"CC0", "CC BY", "CC BY-SA", "CC BY-ND"})
#: Longest stored text, in characters; a longer one is cut at a section boundary (§6.2).
MAX_CHARS = 200_000
SECTION_SEPARATOR = "\n\n"

PMC_BUCKET_URL = "https://pmc-oa-opendata.s3.amazonaws.com"
_PMC_S3_PREFIX = "s3://pmc-oa-opendata/"
BIOC_URL = (
    "https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json/{pmcid}/unicode"
)
EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPEPMC_TEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
BIORXIV_DETAILS_URL = "https://api.biorxiv.org/details/{server}/{doi}"
#: The hosts each route may be redirected to (`_allowed`): an exact name, or with a leading
#: dot a domain and its subdomains. The bucket's is exact: on S3 a subdomain is another bucket.
_PMC_HOSTS = ("pmc-oa-opendata.s3.amazonaws.com",)
_BIOC_HOSTS = (".ncbi.nlm.nih.gov",)
_EUROPEPMC_HOSTS = (".ebi.ac.uk",)
_PREPRINT_HOSTS = (".biorxiv.org", ".medrxiv.org")
#: Most bytes of one response body read, both as received and after content decoding; a
#: longer body is dropped. JATS or BioC markup carrying `MAX_CHARS` of text is a small
#: fraction of this.
MAX_BODY_BYTES = 16 * 1024 * 1024
#: Redirects followed by one fetch, each target checked before it is requested.
_MAX_REDIRECTS = 5
#: Content codings read, with their zlib ``wbits`` (None: no decoding).
_CODINGS: dict[str, int | None] = {
    "identity": None, "gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS,
}
#: The codings a request accepts: those `_BoundedDecoder` reads. httpx's default also offers
#: ``br`` and ``zstd`` when their decoders are installed, which would then be refused.
_ACCEPT_ENCODING = "gzip, deflate"
#: Errors that mean this request cannot be made or read, whatever the retry: the route has
#: nothing. ``UnicodeError`` is idna's ``IDNAError``, which httpx raises reading a host that
#: is not valid IDNA (a redirect to ``https://xn--a-/``). Transient transport errors
#: (`evidence.RETRYABLE_TRANSPORT`) are not here.
_UNREADABLE = (
    httpx.UnsupportedProtocol, httpx.TooManyRedirects, httpx.DecodingError, httpx.InvalidURL,
    UnicodeError,
)
#: What reading an upstream body of the wrong shape raises: JSON whose fields are not the
#: documented types, JSON or JATS nested past the recursion limit, a number too long to
#: convert. A route that raises one has nothing.
_MALFORMED = (ValueError, TypeError, LookupError, AttributeError, RecursionError)
#: Headers describing the transferred body, not the decoded bytes a capped read keeps.
_TRANSFER_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})

# One pacer per upstream. BioC and Europe PMC follow their settings; the bucket and the
# bioRxiv API publish no limit, so they get fixed, modest intervals.
_BIOC_PACER = Pacer(lambda: 1.0 / get_settings().landscape_bioc_max_rps)
_EUROPEPMC_PACER = Pacer(lambda: 1.0 / get_settings().landscape_europepmc_max_rps)
_S3_PACER = Pacer(0.2)
_BIORXIV_PACER = Pacer(1.0)

Basis = Literal["full_text", "summary", "fetch_failed"]


@dataclass
class FullText:
    """What S6 reads for one document.

    ``text`` is the fetched full text when ``basis`` is ``full_text``, and otherwise the
    document's title and summary. ``sections`` holds ``(label, start, end)`` character spans
    into ``text``. ``route`` (``pmc``, ``europepmc`` or ``biorxiv``) is set only for full
    text; ``license`` is the normalized license that decided the basis, when one was read.
    ``truncated`` means the text was cut at `MAX_CHARS`."""

    text: str
    sections: list[tuple[str, int, int]]
    basis: Basis
    route: str | None
    license: str | None
    truncated: bool


class _TransientFetch(Exception):
    """A request still failing transiently after its retries (D41)."""


class _Unread(Exception):
    """A response not read: a body over `MAX_BODY_BYTES` or in an unread or corrupt coding,
    or a redirect refused (not ``https``, off the route's hosts, or past `_MAX_REDIRECTS`)."""


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client()


def _normalize_license(value: Any) -> str | None:
    """``cc by``, ``cc_by_nc_nd``, ``CC-BY 4.0`` and the like as ``CC BY``, ``CC BY-NC-ND``;
    ``cc0`` as ``CC0``. Text that is not a Creative Commons code is returned stripped."""
    if not isinstance(value, str) or not value.strip():
        return None
    tokens = re.sub(r"[\s_-]+", " ", value.strip().lower()).split()
    while tokens and re.fullmatch(r"\d+(\.\d+)*", tokens[-1]):
        tokens.pop()  # a version: "cc by 4.0"
    if tokens[:1] == ["cc0"] or tokens[:2] == ["cc", "0"]:
        return "CC0"
    if tokens[:1] == ["cc"] and len(tokens) > 1:
        return "CC " + "-".join(t.upper() for t in tokens[1:])
    return value.strip()


def _pmcid(value: Any) -> str | None:
    digits = re.fullmatch(r"(?:PMC)?(\d+)", str(value or "").strip(), re.IGNORECASE)
    return f"PMC{digits.group(1)}" if digits else None


# --- HTTP -------------------------------------------------------------------------------


def _allowed(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h)) for h in hosts)


def _https_host(url: Any) -> str:
    """``url``'s host when it is an ``https`` URL, else "" (no host is allowlisted)."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return ""
    try:
        return httpx.URL(url).host
    except (httpx.InvalidURL, UnicodeError):
        return ""


class _BoundedDecoder:
    """Decodes one body's content coding from its raw bytes, never holding more than
    `MAX_BODY_BYTES` of output: zlib's ``max_length`` stops each step one byte past what is
    left, so a small compressed read cannot expand past the cap before it is checked."""

    def __init__(self, resp: httpx.Response):
        self.url = resp.request.url
        codings = [
            c.strip().lower() for c in resp.headers.get("content-encoding", "").split(",")
            if c.strip() and c.strip().lower() != "identity"
        ] or ["identity"]
        if len(codings) > 1 or codings[0] not in _CODINGS:
            raise _Unread(f"content-encoding {', '.join(codings)} from {self.url}")
        self.coding = codings[0]
        wbits = _CODINGS[self.coding]
        self.inflater = None if wbits is None else zlib.decompressobj(wbits)
        self.started = False
        self.raw = 0
        self.size = 0

    def _keep(self, out: bytes) -> bytes:
        self.size += len(out)
        if self.size > MAX_BODY_BYTES:
            raise _Unread(f"body over {MAX_BODY_BYTES} bytes from {self.url}")
        return out

    def _inflate(self, data: bytes) -> bytes:
        try:
            return self.inflater.decompress(data, MAX_BODY_BYTES - self.size + 1)
        except zlib.error:
            if self.started or self.coding != "deflate":
                raise
            # Some servers send "deflate" without the zlib wrapper; httpx reads both.
            self.inflater = zlib.decompressobj(-zlib.MAX_WBITS)
            return self.inflater.decompress(data, MAX_BODY_BYTES - self.size + 1)

    def decode(self, chunk: bytes) -> list[bytes]:
        self.raw += len(chunk)
        if self.raw > MAX_BODY_BYTES:
            raise _Unread(f"body over {MAX_BODY_BYTES} bytes from {self.url}")
        if self.inflater is None:
            return [self._keep(chunk)]
        out = [self._keep(self._inflate(chunk))]
        self.started = True
        while self.inflater.unconsumed_tail:
            out.append(self._keep(self._inflate(self.inflater.unconsumed_tail)))
        return out

    def flush(self) -> bytes:
        """The decoder's last bytes; `_Unread` for a compressed body that ended before its
        stream did (truncated). A gzip body's later members are ignored, as httpx does."""
        if self.inflater is None:
            return b""
        out = self._keep(self.inflater.flush())
        if self.started and not self.inflater.eof:
            raise _Unread(f"truncated {self.coding} body from {self.url}")
        return out


async def _read_capped(resp: httpx.Response) -> httpx.Response:
    """``resp`` (sent with ``stream=True``) read raw and decoded by `_BoundedDecoder`, as a
    loaded response with the decoded body; `_Unread` past `MAX_BODY_BYTES`, for a coding
    other than ``gzip``, ``deflate`` or ``identity``, or for a corrupt compressed body.
    Closes ``resp`` either way."""
    if resp.is_stream_consumed:
        # A response a transport built in memory (httpx.MockTransport's) arrives read and
        # decoded already; only its size is left to check. A network response never does.
        await resp.aclose()
        if len(resp.content) > MAX_BODY_BYTES:
            raise _Unread(f"body over {MAX_BODY_BYTES} bytes from {resp.request.url}")
        return resp
    chunks: list[bytes] = []
    try:
        decoder = _BoundedDecoder(resp)
        async for chunk in resp.aiter_raw():
            chunks.extend(decoder.decode(chunk))
        chunks.append(decoder.flush())
    except zlib.error as exc:
        raise _Unread(f"corrupt compressed body from {resp.request.url}: {exc}") from exc
    finally:
        await resp.aclose()
    headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _TRANSFER_HEADERS]
    return httpx.Response(
        resp.status_code, headers=headers, content=b"".join(chunks), request=resp.request
    )


async def _fetch(client: httpx.AsyncClient, url: str, params: dict[str, str] | None,
                 hosts: tuple[str, ...]) -> httpx.Response:
    """One GET with its body capped. Up to `_MAX_REDIRECTS` redirects are followed by hand,
    each only to an ``https`` URL on a host in ``hosts``; a redirect's body is never read. An
    error status returns with an empty body, unread. `_Unread` for a refused redirect, before
    any request to its target, or past the redirect limit."""
    request = client.build_request(
        "GET", url, params=params, headers={"Accept-Encoding": _ACCEPT_ENCODING},
    )
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            resp = await client.send(request, stream=True, follow_redirects=False)
        except httpx.RemoteProtocolError as exc:
            # httpx parses a redirect's Location even when not following it; a Location it
            # cannot parse will never parse, so it is not retried like a transport error.
            if str(exc).startswith("Invalid URL in location header"):
                raise _Unread(f"redirect from {url} to an invalid URL") from exc
            raise
        target = resp.next_request
        if target is None:
            if resp.status_code >= 400:
                # Only the status is used (a retry, else nothing), so a body the decoder would
                # refuse cannot turn a transient error into "no text" (D41).
                await resp.aclose()
                return httpx.Response(resp.status_code, request=resp.request)
            return await _read_capped(resp)
        await resp.aclose()
        if target.url.scheme != "https":
            raise _Unread(f"redirect from {url} to a {target.url.scheme} URL")
        if not _allowed(target.url.host, hosts):
            raise _Unread(f"redirect from {url} off the allowlist to {target.url.host}")
        request = target
    raise _Unread(f"more than {_MAX_REDIRECTS} redirects from {url}")


async def _get(client: httpx.AsyncClient, url: str, pacer: Pacer,
               params: dict[str, str] | None = None, *,
               hosts: tuple[str, ...]) -> httpx.Response | None:
    """GET, paced and retried, its body read up to `MAX_BODY_BYTES`. None for a
    non-transient error status, a body `_fetch` or `_read_capped` refuses, or an
    `_UNREADABLE` httpx error: the route has nothing. `_TransientFetch` when the retries end
    on a transient status or error."""
    try:
        resp = await with_retries(
            lambda: _fetch(client, url, params, hosts), attempts=evidence.ATTEMPTS,
            backoff=evidence.backoff, retry_statuses=TRANSIENT_STATUSES,
            retry_exceptions=evidence.RETRYABLE_TRANSPORT, pacer=pacer,
        )
    except evidence.RETRYABLE_TRANSPORT as exc:
        raise _TransientFetch(f"{type(exc).__name__} from {url}") from exc
    except _Unread as exc:
        logger.warning("full text: %s", exc)
        return None
    except _UNREADABLE as exc:
        logger.warning("full text: %s from %s: %s", type(exc).__name__, url, exc)
        return None
    if resp.status_code in TRANSIENT_STATUSES:
        raise _TransientFetch(f"HTTP {resp.status_code} from {url}")
    return resp if resp.status_code < 400 else None


def _json(resp: httpx.Response | None) -> Any:
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# --- assembling ------------------------------------------------------------------------


def _assemble(pairs: list[tuple[str, str]]) -> tuple[str, list[tuple[str, int, int]], bool]:
    """Join sections with `SECTION_SEPARATOR`, keeping whole sections up to `MAX_CHARS`.
    Only a first section longer than the cap is cut inside itself."""
    parts: list[str] = []
    spans: list[tuple[str, int, int]] = []
    pos = 0
    for label, body in pairs:
        start = pos + (len(SECTION_SEPARATOR) if parts else 0)
        end = start + len(body)
        if end > MAX_CHARS:
            if not parts:
                parts.append(body[:MAX_CHARS])
                spans.append((label, 0, MAX_CHARS))
            return SECTION_SEPARATOR.join(parts), spans, True
        parts.append(body)
        spans.append((label, start, end))
        pos = end
    return SECTION_SEPARATOR.join(parts), spans, False


def _full_text(pairs: list[tuple[str, str]], route: str, license: str | None) -> FullText:
    text, spans, truncated = _assemble(pairs)
    return FullText(text, spans, "full_text", route, license, truncated)


def _summary(doc: Any, basis: Basis, license: str | None) -> FullText:
    pairs = [(label, value.strip()) for label, value in
             (("title", doc.title), ("summary", doc.summary_text)) if value and value.strip()]
    text, spans, truncated = _assemble(pairs)
    return FullText(text, spans, basis, None, license, truncated)


# --- JATS ------------------------------------------------------------------------------

_BREAK = "\x00"
#: Subtrees left out of section text: references, floats (captions are gathered apart),
#: supplementary material and footnotes.
_SKIP_TAGS = frozenset({
    "ref-list", "fig", "fig-group", "table-wrap", "table-wrap-group", "table",
    "supplementary-material", "fn-group", "graphic", "media",
})
_BLOCK_TAGS = frozenset({
    "p", "title", "sec", "list-item", "disp-quote", "label", "caption", "def-item", "boxed-text",
})
#: Body sections dropped by their ``sec-type`` or title.
_DROP_SECTION_WORDS = (
    "reference", "bibliograph", "supplementary", "acknowledg", "funding", "competing",
    "conflict of interest", "author contribution",
)
#: Canonical labels by ``sec-type`` or title words, first match wins.
_SECTION_WORDS = (
    ("introduction", ("intro", "background")),
    ("results", ("result",)),
    ("discussion", ("discussion", "conclusion")),
    ("methods", ("method", "materials", "procedure")),
)


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    return next((c for c in el if _local(c.tag) == name), None)


def _walk(el: ET.Element, out: list[str]) -> None:
    block = _local(el.tag) in _BLOCK_TAGS
    if block:
        out.append(_BREAK)
    if el.text:
        out.append(el.text)
    for child in el:
        if _local(child.tag) not in _SKIP_TAGS:
            _walk(child, out)
        if child.tail:
            out.append(child.tail)
    if block:
        out.append(_BREAK)


def _flat(el: ET.Element | None) -> str:
    """The element's text without skipped subtrees: one line per block, spaces collapsed."""
    if el is None:
        return ""
    out: list[str] = []
    _walk(el, out)
    lines = (re.sub(r"\s+", " ", seg).strip() for seg in "".join(out).split(_BREAK))
    return "\n".join(line for line in lines if line)


def _section_label(sec: ET.Element) -> str | None:
    """A body section's canonical label, ``body`` when unrecognized, None when dropped."""
    probe = f"{(sec.get('sec-type') or '').lower()} {_flat(_child(sec, 'title')).lower()}"
    if any(word in probe for word in _DROP_SECTION_WORDS):
        return None
    for label, words in _SECTION_WORDS:
        if any(word in probe for word in words):
            return label
    return "body"


def _body_pairs(body: ET.Element | None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for child in body if body is not None else ():
        tag = _local(child.tag)
        if tag in _SKIP_TAGS:
            continue
        label = _section_label(child) if tag == "sec" else "body"
        text = _flat(child) if label else ""
        if text:
            pairs.append((label, text))
    return pairs


def _captions(*containers: ET.Element | None) -> str:
    lines = []
    for container in containers:
        for el in container.iter() if container is not None else ():
            if _local(el.tag) in ("fig", "table-wrap"):
                caption = " ".join(t for t in (_flat(_child(el, "label")),
                                               _flat(_child(el, "caption"))) if t)
                if caption:
                    lines.append(caption)
    return "\n".join(lines)


def _merge_runs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Join consecutive sections that share a label (a results section in several parts)."""
    merged: list[tuple[str, str]] = []
    for label, text in pairs:
        if merged and merged[-1][0] == label:
            merged[-1] = (label, merged[-1][1] + "\n" + text)
        else:
            merged.append((label, text))
    return merged


def sections_from_jats(xml: str) -> list[tuple[str, str]]:
    """``(label, text)`` pairs from a JATS article, in reading order: ``title``,
    ``abstract``, the body sections (``introduction``, ``results``, ``discussion``,
    ``methods``, or ``body`` when unrecognized), then ``captions`` for every figure and
    table. Back matter, reference lists and supplementary material are dropped. Malformed
    XML gives an empty list."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    front = _child(root, "front")
    meta = _child(front, "article-meta")
    title = _flat(_child(_child(meta, "title-group"), "article-title"))
    abstract = "\n".join(
        t for t in (_flat(a) for a in (meta if meta is not None else ())
                    if _local(a.tag) == "abstract") if t)
    body = _child(root, "body")
    pairs = [("title", title), ("abstract", abstract)] + _body_pairs(body)
    pairs.append(("captions", _captions(body, _child(root, "floats-group"))))
    return _merge_runs([(label, text) for label, text in pairs if text])


# --- BioC -----------------------------------------------------------------------------

_BIOC_LABELS = {
    "TITLE": "title", "ABSTRACT": "abstract", "INTRO": "introduction", "METHODS": "methods",
    "RESULTS": "results", "DISCUSS": "discussion", "CONCL": "discussion",
}


def _bioc_passages(data: Any) -> list[dict]:
    collection = data[0] if isinstance(data, list) and data else data
    if not isinstance(collection, dict):
        return []
    documents = collection.get("documents") or []
    return (documents[0].get("passages") or []) if documents else []


def _sections_from_bioc(data: Any) -> list[tuple[str, str]]:
    """The BioC passages under the same labels as `sections_from_jats`; reference, table
    body and other passages are dropped, and captions are gathered at the end."""
    pairs: list[tuple[str, str]] = []
    captions: list[str] = []
    for passage in _bioc_passages(data):
        infons = passage.get("infons") or {}
        text = re.sub(r"\s+", " ", str(passage.get("text") or "")).strip()
        if not text:
            continue
        if "caption" in str(infons.get("type") or ""):
            captions.append(text)
            continue
        label = _BIOC_LABELS.get(str(infons.get("section_type") or "").upper())
        if label:
            pairs.append((label, text))
    if captions:
        pairs.append(("captions", "\n".join(captions)))
    return _merge_runs(pairs)


# --- routes ----------------------------------------------------------------------------


def _s3_https(url: Any) -> str | None:
    """The anonymous HTTPS URL of a ``s3://pmc-oa-opendata/…`` object, without its query."""
    if not isinstance(url, str) or not url.startswith(_PMC_S3_PREFIX):
        return None
    return f"{PMC_BUCKET_URL}/{url[len(_PMC_S3_PREFIX):].split('?', 1)[0]}"


async def _pmc_metadata(client: httpx.AsyncClient, pmcid: str) -> dict | None:
    """The newest version's metadata JSON, found by listing ``metadata/<pmcid>.``."""
    listing = await _get(client, f"{PMC_BUCKET_URL}/", _S3_PACER,
                         params={"list-type": "2", "prefix": f"metadata/{pmcid}."},
                         hosts=_PMC_HOSTS)
    if listing is None:
        return None
    versions = [int(v) for v in
                re.findall(rf"<Key>metadata/{pmcid}\.(\d+)\.json</Key>", listing.text)]
    if not versions:
        return None
    meta = _json(await _get(client, f"{PMC_BUCKET_URL}/metadata/{pmcid}.{max(versions)}.json",
                            _S3_PACER, hosts=_PMC_HOSTS))
    return meta if isinstance(meta, dict) else None


async def _pmc_pairs(client: httpx.AsyncClient, meta: dict, pmcid: str) -> list[tuple[str, str]]:
    """The bucket's JATS, else BioC. A transient bucket failure re-raises only when BioC
    has nothing either."""
    cloud_error: _TransientFetch | None = None
    pairs: list[tuple[str, str]] = []
    url = _s3_https(meta.get("xml_url"))
    if url is not None:
        try:
            resp = await _get(client, url, _S3_PACER, hosts=_PMC_HOSTS)
            pairs = sections_from_jats(resp.text) if resp is not None else []
        except _TransientFetch as exc:
            cloud_error = exc
    if not pairs:
        pairs = _sections_from_bioc(_json(await _get(
            client, BIOC_URL.format(pmcid=pmcid), _BIOC_PACER, hosts=_BIOC_HOSTS)))
    if not pairs and cloud_error is not None:
        raise cloud_error
    return pairs


async def _pmc_route(client: httpx.AsyncClient, doc: Any) -> FullText | None:
    pmcid = _pmcid(doc.pmcid)
    meta = await _pmc_metadata(client, pmcid) if pmcid else None
    if meta is None:
        return None
    license = _normalize_license(meta.get("license_code"))
    if meta.get("is_retracted") or meta.get("is_manuscript") or license not in COMMERCIAL_LICENSES:
        return _summary(doc, "summary", license)
    pairs = await _pmc_pairs(client, meta, pmcid)
    return _full_text(pairs, "pmc", license) if pairs else None


def _europepmc_query(doc: Any) -> str | None:
    pmcid, doi, pmid = _pmcid(doc.pmcid), normalize_doi(doc.doi), normalize_pmid(doc.pmid)
    if pmcid:
        return f"PMCID:{pmcid}"
    if doi:
        return f'DOI:"{doi}"'
    return f"EXT_ID:{pmid} AND SRC:MED" if pmid else None


def _europepmc_declines(record: dict, license: str | None) -> bool:
    """Retracted, an author manuscript, or a license outside the set (D6)."""
    pub_types = ((record.get("pubTypeList") or {}).get("pubType")) or []
    if any(str(t).lower() == "retracted publication" for t in pub_types):
        return True
    if any(record.get(k) == "Y" for k in ("authMan", "epmcAuthMan", "nihAuthMan")):
        return True
    return license is not None and license not in COMMERCIAL_LICENSES


async def _europepmc_route(client: httpx.AsyncClient, doc: Any) -> FullText | None:
    query = _europepmc_query(doc)
    if query is None:
        return None
    data = _json(await _get(client, EUROPEPMC_SEARCH_URL, _EUROPEPMC_PACER, params={
        "query": query, "resultType": "core", "format": "json", "pageSize": "1"},
        hosts=_EUROPEPMC_HOSTS))
    result_list = data.get("resultList") if isinstance(data, dict) else None
    results = (result_list.get("result") or []) if isinstance(result_list, dict) else []
    if not results:
        return None
    record = results[0]
    license = _normalize_license(record.get("license"))
    if _europepmc_declines(record, license):
        return _summary(doc, "summary", license)
    pmcid = _pmcid(record.get("pmcid"))
    if license is None or pmcid is None or record.get("inEPMC") != "Y" \
            or record.get("isOpenAccess") != "Y":
        return None
    resp = await _get(client, EUROPEPMC_TEXT_URL.format(pmcid=pmcid), _EUROPEPMC_PACER,
                      hosts=_EUROPEPMC_HOSTS)
    pairs = sections_from_jats(resp.text) if resp is not None else []
    return _full_text(pairs, "europepmc", license) if pairs else None


def _preprint_doi(doc: Any) -> str | None:
    """A bioRxiv/medRxiv DOI without a version suffix; None for anything else."""
    doi = normalize_doi(doc.doi)
    if doi is None or not doi.lower().startswith("10.1101/") or not is_preprint_doi(doi):
        return None
    return re.sub(r"v\d+$", "", doi, flags=re.IGNORECASE)


async def _preprint_text(client: httpx.AsyncClient, doc: Any, record: dict) -> FullText | None:
    license = _normalize_license(record.get("license"))
    if license not in COMMERCIAL_LICENSES:
        return _summary(doc, "summary", license)
    url = record.get("jatsxml")
    if not _allowed(_https_host(url), _PREPRINT_HOSTS):
        return None
    resp = await _get(client, url, _BIORXIV_PACER, hosts=_PREPRINT_HOSTS)
    pairs = sections_from_jats(resp.text) if resp is not None else []
    return _full_text(pairs, "biorxiv", license) if pairs else None


async def _preprint_route(client: httpx.AsyncClient, doc: Any) -> FullText | None:
    doi = _preprint_doi(doc)
    if doi is None:
        return None
    for server in ("biorxiv", "medrxiv"):
        data = _json(await _get(client, BIORXIV_DETAILS_URL.format(
            server=server, doi=quote(doi, safe="/")), _BIORXIV_PACER, hosts=_PREPRINT_HOSTS))
        records = (data.get("collection") or []) if isinstance(data, dict) else []
        if records:
            # The details endpoint lists versions oldest first.
            return await _preprint_text(client, doc, records[-1])
    return None


_ROUTES = (_pmc_route, _europepmc_route, _preprint_route)


async def fetch_full_text(doc) -> FullText:
    """The full text of a `LandscapeDocument` by the §6.2 routes, else its summary.

    A document flagged retracted is summary-only without a request. The basis is
    ``fetch_failed`` when a route failed transiently and no route produced full text or a
    final "summary only"; the caller retries such a document later (D41). A route whose
    upstream body is malformed has nothing. Never raises for an upstream failure."""
    if doc.is_retracted:
        return _summary(doc, "summary", _normalize_license(doc.license))
    failed = False
    async with _make_client() as client:
        for route in _ROUTES:
            try:
                result = await route(client, doc)
            except _TransientFetch as exc:
                logger.warning("full text: %s failed transiently: %s", route.__name__, exc)
                failed = True
                continue
            except _MALFORMED as exc:
                logger.warning("full text: %s read a malformed response: %s: %s",
                               route.__name__, type(exc).__name__, str(exc)[:200])
                continue
            if result is not None:
                return result
    return _summary(doc, "fetch_failed" if failed else "summary", None)


async def store_full_text(db: AsyncSession, document_id, result: FullText) -> None:
    """Upsert the document's `LandscapeText` row from ``result``, counting the attempt.

    Only full text is stored (gzip); a summary or failed fetch stores the basis, license and
    attempt with no text, since the summary already lives on the document. A stored full text
    is never replaced by a later non-full-text result. Writes in ``db``'s transaction; the
    caller commits."""
    full = result.basis == "full_text"
    values = {
        "text_gz": gzip.compress(result.text.encode("utf-8")) if full else None,
        "sections": [list(span) for span in result.sections] if full else None,
        "text_basis": result.basis,
        "license": result.license,
        "route": result.route,
        "truncated": result.truncated if full else False,
        "fetched_at": func.now(),
    }
    stmt = insert(LandscapeText).values(document_id=document_id, fetch_attempts=1, **values)
    await db.execute(stmt.on_conflict_do_update(
        constraint="uq_landscape_texts_document_id",
        set_={**values, "fetch_attempts": LandscapeText.fetch_attempts + 1,
              "updated_at": func.now()},
        where=or_(LandscapeText.text_basis != "full_text", stmt.excluded.text_basis == "full_text"),
    ))
