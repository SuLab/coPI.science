// Assessment chat drawer — docs/specs/2026-09-24-assessment-chat-design.md §8.3.
//
// Talks only to the three URLs the drawer partial writes into
// window.ASSESSMENT_CHAT, so this file names no route of its own. The rules it
// keeps, each from the spec:
//   * questions, labels, cited text and every status line go in as textContent;
//   * model text reaches innerHTML only through DOMPurify with the chat-only
//     profile below (no img/svg/iframe/form/style; https hrefs only), and not at
//     all when marked or DOMPurify is missing — the answer is then plain text;
//   * nothing is clickable while an answer streams; afterwards a link survives
//     only when its rendered href is EXACTLY an allowed URL, verbatim or as
//     marked's own encoding of it — never decoded (D20, SEC-1a);
//   * error text comes from the fixed table below, never from the server.
(function () {
  "use strict";

  const cfg = window.ASSESSMENT_CHAT;
  const drawer = document.getElementById("assessment-chat");
  if (!cfg || !drawer) {
    return;
  }

  // Citation markers: two private-use code points around a per-page random nonce
  // and the citation number (RSEC-1/RSEC-2). The server strips private-use code
  // points and references to them from model text (spec §5.5), and markdownFor
  // strips them again, but neither can see every way marked may reassemble one
  // (an escape after a raw-block tag, a reference split across two segments).
  // The model never sees the nonce, so no text it writes can produce a marker
  // placeCitations accepts; any other private-use character left in the rendered
  // text is removed there.
  const MARK_OPEN = String.fromCharCode(0xE000);
  const MARK_CLOSE = String.fromCharCode(0xE001);
  const MARK_NONCE = makeNonce();
  const MARK_SPLIT = new RegExp("(" + MARK_OPEN + MARK_NONCE + "\\d+" + MARK_CLOSE + ")");
  const PRIVATE_USE_CLASS = "[" + MARK_OPEN + "-" + String.fromCharCode(0xF8FF) + "]";
  const PRIVATE_USE_ANY = new RegExp(PRIVATE_USE_CLASS);
  const PRIVATE_USE_ALL = new RegExp(PRIVATE_USE_CLASS, "g");
  // A thin space between a segment's text and its citation markers (B1). Without
  // it, marked's GFM autolink swallows the markers onto a bare URL's href, and
  // inside "**bold**" text the closing "**" no longer closes, leaving literal
  // asterisks visible. marked treats U+2009 as ordinary whitespace, which
  // prevents both.
  const MARK_GAP = String.fromCharCode(0x2009);
  const MAIN_PAD = "xl:pr-[30rem]";
  const POLL_MS = 5000;
  const QUESTION_CLASS = "ml-8 whitespace-pre-wrap rounded-lg bg-indigo-50 px-3 py-2 text-sm text-gray-900";
  const ANSWER_CLASS = "rounded-lg border border-gray-200 px-3 py-2";

  const PURIFY = {
    ALLOWED_TAGS: ["p", "br", "strong", "em", "b", "i", "code", "pre", "blockquote", "ul", "ol", "li", "a", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td"],
    // No `title` (R2SEC-2): a link title spanning a citation boundary would carry a
    // citation marker — nonce included — into a tooltip, where placeCitations
    // (which walks text nodes only) never sees it.
    ALLOWED_ATTR: ["href"],
    ALLOWED_URI_REGEXP: /^https:/i,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false
  };

  const ERRORS = {
    disabled: "The assessment chat is switched off.",
    impersonating: "The chat is unavailable while impersonating.",
    not_found: "This assessment no longer exists.",
    forbidden: "Your account can no longer use the chat. Reload the page.",
    unsupported_media_type: "The request was refused. Reload the page and try again.",
    invalid_question: "A question must not be empty and must fit the character limit shown below the box.",
    model_unpriced: "The chat is misconfigured (its model has no price). Tell an administrator.",
    prompt_missing: "The chat is misconfigured (its instructions are missing). Tell an administrator.",
    conversation_full: "This conversation is full. Clear it to start a new one.",
    daily_limit: "You have used today's questions. The limit resets 24 hours after your oldest question.",
    daily_spend_limit: "You have reached today's spending limit for the chat.",
    global_spend_limit: "The chat has reached today's spending limit for everyone.",
    answer_in_progress: "An answer is still being written. Wait for it to finish.",
    upstream_rate_limited: "The model is busy. Try again in a minute.",
    upstream_overloaded: "The model is overloaded. Try again in a minute.",
    upstream_error: "The model could not be reached. Try again.",
    upstream_bad_request: "The model rejected the request. Tell an administrator.",
    unexpected_stop: "The model stopped before finishing an answer. Try again.",
    timeout: "The answer took too long and was stopped.",
    empty_answer: "The model returned no answer. Try rephrasing.",
    storage_error: "The answer could not be saved. Try again.",
    busy: "The chat is busy. Try again in a moment.",
    session_ended: "Your session has ended — reload the page.",
    network: "The connection was lost. Reopen the chat to see whether the answer was saved.",
    unknown: "Something went wrong. Try again."
  };

  const els = {
    log: drawer.querySelector("[data-chat-log]"),
    input: drawer.querySelector("[data-chat-input]"),
    send: drawer.querySelector("[data-chat-send]"),
    clear: drawer.querySelector("[data-chat-clear]"),
    counter: drawer.querySelector("[data-chat-counter]"),
    usage: drawer.querySelector("[data-chat-usage]"),
    error: drawer.querySelector("[data-chat-error]"),
    live: drawer.querySelector("[data-chat-live]"),
    notice: drawer.querySelector("[data-chat-verdict-notice]"),
    starters: drawer.querySelector("[data-chat-starters]"),
    close: drawer.querySelector("[data-chat-close]")
  };
  const openers = Array.from(document.querySelectorAll("[data-chat-open]"));
  const main = document.querySelector("main");
  const state = { turns: [], limits: null, busy: false, loaded: false, open: false, opener: null, poll: 0, historySeq: 0 };

  // SW-11: elements this file itself made `inert` while the drawer is a
  // full-screen overlay on a narrow viewport, so close can undo exactly them
  // (and only them — an already-inert element is left alone).
  let inertedSiblings = [];
  let mobileModalActive = false;

  function makeNonce() {
    const bytes = new Uint8Array(8);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes).map(function (b) { return (b < 16 ? "0" : "") + b.toString(16); }).join("");
  }

  // The citation number of a real marker (one carrying this page's nonce), or null.
  function markerNumber(part) {
    if (part.length <= MARK_NONCE.length + 2 || part.charAt(0) !== MARK_OPEN
        || part.charAt(part.length - 1) !== MARK_CLOSE
        || part.slice(1, 1 + MARK_NONCE.length) !== MARK_NONCE) {
      return null;
    }
    const inner = part.slice(1 + MARK_NONCE.length, -1);
    return /^\d+$/.test(inner) ? inner : null;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function codePoints(text) {
    return Array.from(text).length;
  }

  function errorText(code) {
    return Object.prototype.hasOwnProperty.call(ERRORS, code) ? ERRORS[code] : ERRORS.unknown;
  }

  function showError(code) {
    els.error.textContent = errorText(code);
    els.error.hidden = false;
  }

  function clearError() {
    els.error.textContent = "";
    els.error.hidden = true;
  }

  function hasStreaming() {
    return state.turns.some(function (t) { return t.status === "streaming"; });
  }

  function setBusy(busy) {
    state.busy = busy;
    els.send.disabled = busy || hasStreaming();
    els.clear.disabled = busy;
  }

  function updateCounter() {
    const max = state.limits ? state.limits.max_question_chars : 4000;
    els.counter.textContent = codePoints(els.input.value) + " / " + max;
  }

  function updateUsage() {
    els.usage.textContent = state.limits
      ? state.limits.questions_used_24h + " of " + state.limits.daily_limit + " questions used today"
      : "";
  }

  // ---- rendering ---------------------------------------------------------

  // SEC-2: strip anything that could pass for one of our own citation markers
  // before it ever reaches the markdown string — literal private-use code
  // points, and numeric character references (decimal or hex, "x"/"X", any
  // number of leading zeros, trailing ";" optional) that decode into the same
  // range. Every other reference is left alone. Built from character codes,
  // not literal private-use characters or "\u" escapes, so an editor cannot
  // silently mangle the range this guards.
  const PRIV_LO = 0xE000;
  const PRIV_HI = 0xF8FF;
  // Digit runs bounded as on the server: a private-use value has at most five
  // significant decimal or four hex digits.
  const NCR_RE = /&#(?:0*([0-9]{1,7})|[xX]0*([0-9A-Fa-f]{1,6}));?/g;

  function stripForgedMarkers(text) {
    let out = "";
    Array.from(text).forEach(function (ch) {
      const code = ch.codePointAt(0);
      if (code < PRIV_LO || code > PRIV_HI) {
        out += ch;
      }
    });
    // Repeat until nothing changes: removing one reference can join its
    // neighbours into another ("&#&#xE000;57344;" becomes "&#57344;"). Capped
    // as on the server: nesting deeper than that is adversarial, and then no
    // reference survives at all.
    for (let pass = 0; pass < 8; pass++) {
      const next = out.replace(NCR_RE, function (match, dec, hex) {
        const value = dec !== undefined ? parseInt(dec, 10) : parseInt(hex, 16);
        return value >= PRIV_LO && value <= PRIV_HI ? "" : match;
      });
      if (next === out) {
        return out;
      }
      out = next;
    }
    return out.split("&#").join("");
  }

  function hostOf(href) {
    try {
      return new URL(href).host;
    } catch (e) {
      return "";
    }
  }

  function markdownFor(turn) {
    return (turn.segments || []).map(function (seg) {
      const text = stripForgedMarkers(String(seg.text || ""));
      const cites = seg.cites || [];
      if (!cites.length) {
        return text;
      }
      const marks = cites.map(function (n) { return MARK_OPEN + MARK_NONCE + String(n) + MARK_CLOSE; }).join("");
      const gap = /\s$/.test(text) ? "" : MARK_GAP;
      return text + gap + marks;
    }).join("");
  }

  function textFallback(markdown) {
    // Drop a MARK_GAP immediately before a marker first, so the fallback does
    // not double-space where markdownFor added one; real markers become " [n]"
    // and any other private-use character is removed.
    return markdown
      .split(MARK_GAP + MARK_OPEN).join(MARK_OPEN)
      .split(MARK_SPLIT)
      .map(function (part) {
        const n = markerNumber(part);
        return n !== null ? " [" + n + "]" : part.replace(PRIVATE_USE_ALL, "");
      })
      .join("");
  }

  // SEC-1b: a private marked instance, built once, whose "raw HTML" renderer
  // returns the HTML HTML-escaped as visible text instead of passing it
  // through. The page's own prose rendering keeps using the global
  // window.marked, whose options this never touches.
  let sanitizingMarked = null;

  function getSanitizingMarked() {
    if (sanitizingMarked) {
      return sanitizingMarked;
    }
    if (!window.marked || !window.marked.Marked) {
      return null;
    }
    const instance = new window.marked.Marked();
    instance.use({
      tokenizer: {
        // marked's own `tag` tokenizer switches the lexer into a raw-block state
        // after <code>, <pre>, <kbd> or <script>, in which later text is emitted
        // UNESCAPED, so an escaped entity decodes after all (RSEC-1, reproduced
        // with marked 12.0.2). Raw HTML is rendered as text below, so that state
        // is never wanted: same match, never entering it.
        tag: function (src) {
          const cap = this.rules.inline.tag.exec(src);
          if (cap) {
            return { type: "html", raw: cap[0], inLink: false, inRawBlock: false, block: false, text: cap[0] };
          }
          return undefined;
        }
      },
      renderer: {
        html: function (html) {
          return String(html)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/[\"]/g, "&quot;")
            .replace(/[']/g, "&#39;");
        }
      }
    });
    sanitizingMarked = instance;
    return sanitizingMarked;
  }

  // The ONE place model text becomes markup. Returns false when it fell back to text.
  function renderBody(container, markdown) {
    const sanitizer = getSanitizingMarked();
    if (!sanitizer || !window.DOMPurify) {
      container.textContent = textFallback(markdown);
      return false;
    }
    container.innerHTML = window.DOMPurify.sanitize(sanitizer.parse(markdown), PURIFY);
    return true;
  }

  // SEC-1a: an allowed link survives only when its href is byte-identical to
  // an allowed URL, or to marked's own re-encoding of one — never decoded.
  // Decoding the candidate (the old safeDecode path) let a differently-encoded
  // href match an allowed URL it was not identical to; this never decodes.
  function markedHref(u) {
    try {
      return encodeURI(u).replace(/%25/g, "%");
    } catch (e) {
      return u;
    }
  }

  // Runs BEFORE the citation markers become anchors, so the citation superscripts
  // are never subject to it.
  function passLinks(root, allowedList, final) {
    const allowedHrefs = new Set();
    (allowedList || []).forEach(function (u) {
      allowedHrefs.add(u);
      allowedHrefs.add(markedHref(u));
    });
    Array.from(root.querySelectorAll("a")).forEach(function (a) {
      const href = a.getAttribute("href") || "";
      if (final && href && allowedHrefs.has(href)) {
        a.setAttribute("rel", "noopener noreferrer nofollow");
        a.setAttribute("target", "_blank");
        const host = hostOf(href);
        if (host) {
          a.after(el("span", "text-gray-600", " (" + host + ")"));
        }
        return;
      }
      const text = final && href ? a.textContent + " (" + href + ")" : a.textContent;
      a.replaceWith(document.createTextNode(text));
    });
  }

  // SW-12: a marker whose text sits inside an <a>, <code> or <pre> must not
  // become a nested link there (a link inside a link, or a link inside code,
  // is both invalid markup and unclickable in practice). Its citation is
  // instead spliced in right after the OUTERMOST such ancestor, in the order
  // the markers appeared — tracked per ancestor across text nodes so a second
  // marker in the same ancestor lands after the first, not before it.
  function placeCitations(root, turnKey) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      if (PRIVATE_USE_ANY.test(walker.currentNode.nodeValue)) {
        nodes.push(walker.currentNode);
      }
    }
    const lastInserted = new Map(); // outer ancestor -> node already placed after it
    nodes.forEach(function (node) {
      let outer = null;
      let p = node.parentNode;
      while (p && p !== root) {
        if (p.tagName === "A" || p.tagName === "CODE" || p.tagName === "PRE") {
          outer = p;
        }
        p = p.parentNode;
      }
      const fragment = document.createDocumentFragment();
      const deferred = [];
      node.nodeValue.split(MARK_SPLIT).forEach(function (part) {
        const inner = markerNumber(part);
        if (inner !== null) {
          const sup = el("sup");
          const link = el("a", "text-indigo-700", "[" + inner + "]");
          link.setAttribute("href", "#chat-src-" + turnKey + "-" + inner);
          // The entry may be in a collapsed Sources list: open it before the jump.
          link.addEventListener("click", function () { revealSources(turnKey); });
          sup.appendChild(link);
          if (outer) {
            deferred.push(sup);
          } else {
            fragment.appendChild(sup);
          }
        } else if (part) {
          // Anything private-use that is not a real marker was forged: drop it.
          const clean = part.replace(PRIVATE_USE_ALL, "");
          if (clean) {
            fragment.appendChild(document.createTextNode(clean));
          }
        }
      });
      node.replaceWith(fragment);
      if (outer && deferred.length) {
        let anchor = lastInserted.has(outer) ? lastInserted.get(outer) : outer;
        deferred.forEach(function (sup) {
          anchor.parentNode.insertBefore(sup, anchor.nextSibling);
          anchor = sup;
        });
        lastInserted.set(outer, anchor);
      }
    });
  }

  function showInPage(anchor) {
    const target = document.getElementById(anchor);
    if (!target) {
      return;
    }
    let node = target;
    while (node) {
      if (node.tagName === "DETAILS") {
        node.open = true;
      }
      node = node.parentElement;
    }
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    target.classList.add("ring-2", "ring-amber-400");
    window.setTimeout(function () {
      target.classList.remove("ring-2", "ring-amber-400");
    }, 2500);
    if (window.matchMedia("(max-width: 767px)").matches) {
      closeDrawer();
    }
  }

  // Turns whose Sources list is open. The log is rebuilt on every streamed frame
  // and every history load, so the state lives here, keyed by turn, not in the DOM.
  const openSources = new Set();

  function showSources(toggle, list, open) {
    list.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.firstChild.textContent = open ? "▾" : "▸";
  }

  function revealSources(turnKey) {
    openSources.add(turnKey);
    const toggle = document.getElementById("chat-sources-toggle-" + turnKey);
    const list = document.getElementById("chat-sources-" + turnKey);
    if (toggle && list) {
      showSources(toggle, list, true);
    }
  }

  function renderSources(container, turn, turnKey) {
    const cites = turn.citations || [];
    if (!cites.length) {
      return;
    }
    const box = el("div", "mt-2 border-t border-gray-100 pt-2");
    // Collapsed by default, behind a button rather than <details>: the page's
    // Expand/Collapse-all script toggles every <details> in the document.
    const toggle = el("button", "flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-gray-600 hover:text-gray-900");
    toggle.type = "button";
    toggle.id = "chat-sources-toggle-" + turnKey;
    toggle.setAttribute("aria-controls", "chat-sources-" + turnKey);
    const caret = el("span");
    caret.setAttribute("aria-hidden", "true");
    toggle.appendChild(caret);
    toggle.appendChild(document.createTextNode("Sources (" + cites.length + ")"));
    const list = el("ol", "mt-1 space-y-2 text-sm");
    list.id = "chat-sources-" + turnKey;
    cites.forEach(function (c) {
      const item = el("li", "flex items-start justify-between gap-2 rounded border border-gray-200 p-2");
      item.id = "chat-src-" + turnKey + "-" + c.n;
      item.appendChild(el("span", "font-medium text-gray-800", "[" + c.n + "] " + (c.label || "the record")));
      if (c.anchor) {
        const button = el("button", "shrink-0 text-indigo-700 hover:underline", "Show in page");
        button.type = "button";
        button.addEventListener("click", function () { showInPage(c.anchor); });
        item.appendChild(button);
      }
      list.appendChild(item);
    });
    toggle.addEventListener("click", function () {
      const open = list.hidden;
      if (open) {
        openSources.add(turnKey);
      } else {
        openSources.delete(turnKey);
      }
      showSources(toggle, list, open);
    });
    showSources(toggle, list, openSources.has(turnKey));
    box.appendChild(toggle);
    box.appendChild(list);
    container.appendChild(box);
  }

  function renderAnswer(container, turn, final, turnKey) {
    container.replaceChildren();
    const body = el("div", "md-content text-sm leading-relaxed text-gray-800");
    container.appendChild(body);
    if (renderBody(body, markdownFor(turn))) {
      passLinks(body, turn.allowed_links, final);
      placeCitations(body, turnKey);
    }
    renderSources(container, turn, turnKey);
  }

  function turnNode(turn, windowStart) {
    const wrap = el("section", "space-y-2");
    if (windowStart) {
      wrap.appendChild(el("p", "text-sm italic text-gray-600", "Earlier turns are no longer part of the conversation the model sees."));
    }
    wrap.appendChild(el("p", QUESTION_CLASS, turn.question));
    const answer = el("div", ANSWER_CLASS);
    wrap.appendChild(answer);
    if (turn.status === "refused") {
      answer.appendChild(el("p", "text-sm text-amber-800",
        "The model declined to answer this" + (turn.refusal_category ? " (category: " + turn.refusal_category + ")" : "") + ". Try rephrasing."));
    } else if (turn.status === "failed") {
      answer.appendChild(el("p", "text-sm text-red-700", turn.error_code ? errorText(turn.error_code) : "This answer failed."));
    } else if (turn.status === "interrupted") {
      answer.appendChild(el("p", "text-sm text-gray-600", "This answer was interrupted before it finished."));
    } else if (turn.status === "streaming") {
      answer.appendChild(el("p", "text-sm text-gray-600", "Still being answered — this updates when it finishes."));
    } else {
      renderAnswer(answer, turn, true, turn.id);
    }
    const notes = [];
    if (turn.status === "truncated") {
      notes.push("Answer cut off.");
    }
    if (turn.fallback_used && turn.served_by_model) {
      notes.push("Answered by " + turn.served_by_model + ".");
    }
    if (turn.record_changed) {
      notes.push("The record changed after this answer.");
    }
    notes.forEach(function (note) { wrap.appendChild(el("p", "text-sm text-gray-600", note)); });
    return wrap;
  }

  function render() {
    els.log.replaceChildren();
    const firstIn = state.turns.findIndex(function (t) { return t.in_window; });
    const olderLeftOut = firstIn > 0 && state.turns.slice(0, firstIn).some(function (t) {
      return (t.status === "complete" || t.status === "truncated") && !t.in_window;
    });
    state.turns.forEach(function (turn, i) {
      els.log.appendChild(turnNode(turn, olderLeftOut && i === firstIn));
    });
    els.starters.hidden = state.turns.length > 0;
    els.notice.hidden = !(state.limits && state.limits.verdict_may_change);
    updateUsage();
    updateCounter();
    setBusy(state.busy);
    els.log.scrollTop = els.log.scrollHeight;
  }

  // ---- network -----------------------------------------------------------

  function isJson(resp) {
    return (resp.headers.get("content-type") || "").indexOf("application/json") === 0;
  }

  const TERMINAL_CODES = { session_ended: true, not_found: true, disabled: true, forbidden: true, impersonating: true };

  function schedulePoll() {
    if (state.poll) {
      window.clearTimeout(state.poll);
      state.poll = 0;
    }
    if (state.open && !state.busy && hasStreaming()) {
      state.poll = window.setTimeout(loadHistory, POLL_MS);
    }
  }

  // SW-1/SW-4/SW-9/SW-10: returns true only when it actually replaced
  // state.turns and re-rendered; false on every error path and on a stale
  // response (its historySeq token no longer current, or an ask() is busy).
  async function loadHistory() {
    const token = ++state.historySeq;
    function stale() {
      return token !== state.historySeq || state.busy;
    }
    // RS-5: a stale failure must not paint an error over a live answer. RS-7: a
    // refusal that would only repeat (signed out, gone, switched off, not
    // allowed) stops polling; anything transient keeps it going.
    function fail(code) {
      if (stale()) {
        return false;
      }
      showError(code);
      if (!TERMINAL_CODES[code]) {
        schedulePoll();
      }
      return false;
    }
    let resp;
    try {
      resp = await fetch(cfg.historyUrl, { credentials: "same-origin", headers: { Accept: "application/json" } });
    } catch (e) {
      return fail("network");
    }
    if (resp.redirected) {
      return fail("session_ended");
    }
    if (!isJson(resp)) {
      return fail("unknown");
    }
    let data;
    try {
      data = await resp.json();
    } catch (e) {
      return fail("unknown");
    }
    if (!resp.ok) {
      return fail(data.error || "unknown");
    }
    if (stale()) {
      return false;
    }
    state.turns = data.turns || [];
    state.limits = data;
    state.loaded = true;
    render();
    schedulePoll();
    return true;
  }

  function handleFrame(frame, handlers) {
    let name = "message";
    let data = "";
    frame.split("\n").forEach(function (line) {
      if (line.indexOf("event: ") === 0) {
        name = line.slice(7);
      } else if (line.indexOf("data: ") === 0) {
        data += line.slice(6);
      }
    });
    if (!data || !Object.prototype.hasOwnProperty.call(handlers, name)) {
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(data);
    } catch (e) {
      return;
    }
    handlers[name](parsed);
  }

  // fetch + ReadableStream, not EventSource: EventSource cannot POST.
  async function readStream(resp, handlers) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      buffer += decoder.decode(chunk.value, { stream: true });
      let cut = buffer.indexOf("\n\n");
      while (cut !== -1) {
        handleFrame(buffer.slice(0, cut), handlers);
        buffer = buffer.slice(cut + 2);
        cut = buffer.indexOf("\n\n");
      }
    }
  }

  async function ask() {
    const question = els.input.value.trim();
    if (!question || state.busy || hasStreaming()) {
      return;
    }
    const max = state.limits ? state.limits.max_question_chars : 4000;
    if (codePoints(question) > max) {
      showError("invalid_question");
      return;
    }
    clearError();
    setBusy(true);
    state.historySeq++;  // SW-1: invalidate any history GET already in flight
    els.starters.hidden = true;

    const live = { segments: [], citations: [], allowed_links: [] };
    const wrap = el("section", "space-y-2");
    wrap.appendChild(el("p", QUESTION_CLASS, question));
    const answer = el("div", ANSWER_CLASS);
    const status = el("p", "text-sm text-gray-600", "Sending…");
    const body = el("div");
    answer.appendChild(status);
    answer.appendChild(body);
    wrap.appendChild(answer);
    els.log.appendChild(wrap);
    els.log.scrollTop = els.log.scrollHeight;

    let turnKey = "live";
    let finished = false;
    let terminalErrorCode = null;
    let frame = 0;

    function paint() {
      frame = 0;
      renderAnswer(body, live, false, turnKey);
      els.log.scrollTop = els.log.scrollHeight;
    }

    function schedulePaint() {
      if (!frame) {
        frame = window.requestAnimationFrame(paint);
      }
    }

    function abandon(code) {
      wrap.remove();
      els.input.value = question;
      setBusy(false);
      render();
      showError(code);
    }

    let resp;
    try {
      resp = await fetch(cfg.askUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ question: question })
      });
    } catch (e) {
      abandon("network");
      return;
    }
    if (resp.redirected) {
      abandon("session_ended");
      return;
    }
    if ((resp.headers.get("content-type") || "").indexOf("text/event-stream") !== 0) {
      let code = "unknown";  // SW-10: only a redirect (handled above) means session_ended
      if (isJson(resp)) {
        try {
          code = (await resp.json()).error || "unknown";
        } catch (e) {
          code = "unknown";
        }
      }
      abandon(code);
      return;
    }
    els.input.value = "";
    updateCounter();

    function segment(i) {
      if (!live.segments[i]) {
        live.segments[i] = { text: "", cites: [] };
      }
      return live.segments[i];
    }

    const handlers = {
      turn: function (d) {
        turnKey = d.turn_id;
        status.textContent = "Thinking…";
      },
      status: function (d) {
        status.textContent = d.state === "answering" ? "Answering…" : "Thinking…";
      },
      text: function (d) {
        segment(d.seg).text += d.text;
        schedulePaint();
      },
      citation: function (d) {
        const seg = segment(d.seg);
        const n = d.citation.n;
        if (seg.cites.indexOf(n) === -1) {
          seg.cites.push(n);
        }
        if (!live.citations.some(function (c) { return c.n === n; })) {
          live.citations.push(d.citation);
        }
        schedulePaint();
      },
      notice: function (d) {
        if (d.kind === "fallback") {
          status.textContent = "Switching to " + (d.to_model || "another model") + "…";
        }
      },
      done: function (d) {
        finished = true;
        state.historySeq++;  // SW-1: invalidate any GET still in flight from mid-stream
        state.turns = state.turns.filter(function (t) { return t.id !== d.turn.id; });
        state.turns.push(d.turn);
        if (state.limits) {
          state.limits.questions_used_24h = d.questions_used_24h;
          state.limits.daily_limit = d.daily_limit;
        }
        els.live.textContent = "";
        window.setTimeout(function () { els.live.textContent = "Answer complete"; }, 50);
      },
      error: function (d) {
        finished = true;
        terminalErrorCode = d.code;
        showError(d.code);
      }
    };

    try {
      await readStream(resp, handlers);
    } catch (e) {
      // Dropped mid-stream; handled uniformly below via `finished`.
    }
    if (frame) {
      window.cancelAnimationFrame(frame);
    }
    setBusy(false);

    if (finished && els.error.hidden) {
      render();  // `done` carried the canonical turn
      await loadHistory();  // SW-9: older turns' in_window flags become canonical too
      return;
    }

    // SW-6: the stream ended without `done` — either a server `error` event
    // (already shown above) or the connection dropped mid-stream. Either way
    // the live wrap must not survive saying "Answering…", so it comes down
    // before the reload, not after.
    wrap.remove();
    render();
    if (els.error.hidden) {
      terminalErrorCode = "network";
      showError("network");
    }
    const applied = await loadHistory();
    if (applied && terminalErrorCode === "network") {
      clearError();  // the canonical history now shows what actually happened
    }
  }

  async function clearConversation() {
    if (!window.confirm("Delete this whole conversation? This cannot be undone.")) {
      return;
    }
    clearError();
    setBusy(true);
    let resp;
    try {
      resp = await fetch(cfg.clearUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: "{}"
      });
    } catch (e) {
      setBusy(false);
      showError("network");
      return;
    }
    setBusy(false);
    if (resp.redirected) {
      showError("session_ended");
      return;
    }
    if (!isJson(resp)) {
      showError("unknown");
      return;
    }
    let data;
    try {
      data = await resp.json();
    } catch (e) {
      showError("unknown");
      return;
    }
    if (!resp.ok) {
      showError(data.error);
      return;
    }
    await loadHistory();
  }

  // ---- drawer ------------------------------------------------------------

  // SW-11: on a narrow viewport the drawer covers the whole screen but is not
  // otherwise a modal, so the page behind it stays reachable to a screen
  // reader or sequential Tab. `inert` on every element outside the drawer's
  // own ancestor chain closes that gap; only elements this file itself made
  // inert are recorded, and only those are undone on close.
  function setInertForModal() {
    inertedSiblings = [];
    let node = drawer;
    while (node && node !== document.body) {
      const parent = node.parentNode;
      if (parent) {
        Array.from(parent.children).forEach(function (sibling) {
          if (sibling !== node && !sibling.hasAttribute("inert")) {
            sibling.setAttribute("inert", "");
            inertedSiblings.push(sibling);
          }
        });
      }
      node = parent;
    }
  }

  function clearInertForModal() {
    inertedSiblings.forEach(function (sibling) {
      sibling.removeAttribute("inert");
    });
    inertedSiblings = [];
  }

  function openDrawer(opener) {
    if (state.open) {  // SW-1: reopening (or a second opener click) is a no-op
      return;
    }
    state.opener = opener || null;
    state.open = true;
    drawer.classList.remove("hidden");
    drawer.classList.add("flex");
    openers.forEach(function (b) { b.setAttribute("aria-expanded", "true"); });
    if (main) {
      main.classList.add(MAIN_PAD);
    }
    mobileModalActive = window.matchMedia("(max-width: 767px)").matches;
    if (mobileModalActive) {
      drawer.setAttribute("role", "dialog");
      drawer.setAttribute("aria-modal", "true");
      setInertForModal();
    }
    clearError();
    els.input.focus();
    loadHistory();
  }

  function closeDrawer() {
    state.open = false;
    drawer.classList.add("hidden");
    drawer.classList.remove("flex");
    openers.forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    if (main) {
      main.classList.remove(MAIN_PAD);
    }
    if (mobileModalActive) {
      drawer.removeAttribute("role");
      drawer.removeAttribute("aria-modal");
      clearInertForModal();
      mobileModalActive = false;
    }
    if (state.poll) {
      window.clearTimeout(state.poll);
      state.poll = 0;
    }
    if (state.opener) {
      // preventScroll: on a narrow screen "Show in page" closes the drawer after
      // scrolling to its target, and a plain focus() would scroll straight back.
      state.opener.focus({ preventScroll: true });
    }
  }

  openers.forEach(function (button) {
    button.addEventListener("click", function () { openDrawer(button); });
  });
  els.close.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && state.open && !event.isComposing) {
      closeDrawer();
    }
  });
  els.input.addEventListener("input", updateCounter);
  els.input.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    if (event.isComposing || event.keyCode === 229) {  // SW-7: Safari's IME-confirming Enter
      return;
    }
    event.preventDefault();
    ask();
  });
  els.send.addEventListener("click", ask);
  els.clear.addEventListener("click", clearConversation);
  Array.from(drawer.querySelectorAll("[data-chat-starter]")).forEach(function (button) {
    button.addEventListener("click", function () {
      els.input.value = button.textContent.trim();
      updateCounter();
      els.input.focus();
    });
  });
  updateCounter();
})();
