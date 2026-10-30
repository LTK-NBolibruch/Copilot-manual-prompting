"""Browser automation for Microsoft 365 Copilot Chat.

CopilotSession keeps ONE Edge instance open and serves prompts one at a time
from a dedicated worker thread (Playwright's sync API is thread-bound).

Resuming: submit(..., resume={"url", "reply_hash", "prompt"}) reopens an
existing Copilot chat and sends only resume["prompt"] (the new user message),
after checking that the chat's last reply matches resume["reply_hash"]. If the
chat can't be opened or is in a different state (e.g. the client regenerated
an earlier reply), a new chat is started with the full prompt instead.

Finding the reply: before a prompt is sent, every reply and reasoning element
already on the page is marked. Afterwards, only unmarked elements that come
after the marked ones - and after your own message bubble, which is recognised
by its text or USER_MSG_SEL - count as the new reply. (Counting elements was
not reliable: your own message, lazily loaded chat history and re-rendered
wrappers all change the count.)

Formatting: the reply's HTML is cleaned in the browser (Copy buttons, icons,
citation markers and reasoning removed; ARIA-role tables/lists/headings and
inline-styled bold/italic turned into real HTML elements) and converted to
markdown with markdownify.

Streaming (submit(prompt, on_delta=callable)) has two modes, set by
config.STREAM_MODE:

  markdown (default)
      The reply is split into top-level blocks (paragraph, list, table, code
      block, heading...). Every block except the last is finished, so it is
      converted to markdown and sent. The block Copilot is still writing is
      held back until the next block starts or the reply ends.

  text
      Plain text is sent as it is typed (finer-grained, no formatting).
      Sending pauses whenever Copilot reformats text that was already sent.

Reasoning: text from Copilot's reasoning ("thinking") section, found with
config.REASONING_SEL, is sent first, wrapped in <THINK_TAG> ... </THINK_TAG>
(config.THINK_TAG, default "think"), and kept out of the answer text.

Completeness: when the reply finishes, whatever the client has not received
yet is always sent, even if Copilot re-rendered earlier text (see remainder()).

Future attributes set by the worker:
    result()        full reply (tagged reasoning + answer)
    streamed_text   exactly what was sent through on_delta
    truncated       True if the reply hit REPLY_TIMEOUT
    answer_plain    the answer's cleaned page text (used to fingerprint the chat)
    chat_url        the Copilot conversation URL, if one was detected
    resumed         True if an existing chat was continued
"""
import difflib
import logging
import os
import queue
import re
import threading
import time
from concurrent.futures import Future

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import config
from conversations import conversation_url, fingerprint, normalize

log = logging.getLogger("copilot")

try:
    from markdownify import markdownify as _to_md
except ImportError:  # optional dependency
    _to_md = None


class CopilotError(RuntimeError):
    """Base error for automation failures."""


class PromptTooLong(CopilotError):
    """The prompt exceeds what Copilot's chat box will accept."""


class LoginRequired(CopilotError):
    """The saved session has expired; sign in again with --show."""


LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com", "device.login")


def _is_login_url(url: str) -> bool:
    return any(h in url for h in LOGIN_HOSTS)


def _on_login_page(page) -> bool:
    return _is_login_url(page.url)


RELAY_MARK = "data-relay-old"

# Pure helpers shared by SNAPSHOT_JS and the tests (tests run them in node).
PICK_JS = r"""
const relayNorm = s => (s || '').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
// Is normalized text c your own prompt p (normalized, possibly truncated) shown back in the chat?
const relayIsEcho = (c, p) => {
  if (!c || !p) return false;
  if (c === p) return true;
  if (p.length >= 20 && c.length >= 20) return c.includes(p.slice(0, 200)) || p.startsWith(c);
  return c.endsWith(p) && c.length - p.length <= 12;      // short prompt with a label like "You said"
};
// items: reply-selector matches in document order: {isNew, inUser, inReasoning, n (normalized text)}.
// Returns {idx: the new reply (-1 if none yet), start: first index after your own message bubble}.
const relayPickReply = (items, p) => {
  let start = 0;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.isNew && (it.inUser || relayIsEcho(it.n, p))) { start = i + 1; break; }
  }
  for (let i = items.length - 1; i >= start; i--) {
    const it = items[i];
    if (!it.isNew || it.inUser || it.inReasoning || relayIsEcho(it.n, p)) continue;
    return {idx: i, start};
  }
  return {idx: -1, start};
};
"""

# Marks every reply/reasoning element currently on the page as old.
MARK_JS = r"""
([sel, rsel, mark]) => {
  const r = [...document.querySelectorAll(sel)];
  r.forEach(e => e.setAttribute(mark, '1'));
  const q = rsel ? [...document.querySelectorAll(rsel)] : [];
  q.forEach(e => e.setAttribute(mark, '1'));
  return {replies: r.length, reasoning: q.length};
}
"""

# Reads the reply and returns:
#   found     - whether a reply element was identified
#   reasoning - reasoning text (sent in think tags)
#   text      - the answer's visible text (completion detection, plain-text fallback)
#   clean     - the answer's text without buttons/citations/reasoning (content check, chat fingerprint)
#   blocks    - the answer's top-level blocks as cleaned, semantic HTML (markdown conversion)
# o.newOnly: true while waiting for a new reply (marked elements and your own message are skipped);
#            false to read the last reply already in a chat (reopening a stored conversation).
SNAPSHOT_JS = r"""
([sel, noise, rsel, usel, o]) => {
  """ + PICK_JS + r"""
  const MARK = o.mark, FOLLOWING = 4;
  const pick = e => (e.innerText || '').trim() || (e.textContent || '').trim();  // collapsed panels: textContent
  const after = (a, e) => !a || (!a.contains(e) && !!(a.compareDocumentPosition(e) & FOLLOWING));
  const all = [...document.querySelectorAll(sel)];
  const marked = all.filter(e => e.hasAttribute(MARK));
  const lastOld = marked.length ? marked[marked.length - 1] : null;
  const items = all.map(e => ({
    isNew: !e.closest('[' + MARK + ']') && after(lastOld, e),
    inUser: !!(usel && e.closest(usel)),
    inReasoning: !!(rsel && e.closest(rsel)),
    n: relayNorm(e.textContent),
  }));

  let el = null, reasoning = '', anchor = lastOld;
  if (o.newOnly) {
    const r = relayPickReply(items, o.prompt);
    if (r.start > 0) anchor = all[r.start - 1];            // your own message bubble
    if (r.idx >= 0) el = all[r.idx];
    if (rsel) {
      const rNew = [...document.querySelectorAll(rsel)].filter(e => !e.closest('[' + MARK + ']') && after(anchor, e));
      reasoning = rNew.filter(e => !rNew.some(x => x !== e && x.contains(e))).map(pick).filter(Boolean).join('\n\n');
    }
  } else {
    for (let i = all.length - 1; i >= 0 && !el; i--) if (!items[i].inUser && !items[i].inReasoning) el = all[i];
  }
  const res = {found: !!el, reasoning, text: '', clean: '', blocks: [], total: all.length, old: marked.length};
  if (!el) return res;

  // Answer text without any reasoning nested inside the reply element.
  let text = el.innerText || '';
  if (rsel) el.querySelectorAll(rsel).forEach(r => { const t = r.innerText || ''; if (t) text = text.replace(t, ''); });

  const c = el.cloneNode(true);
  c.removeAttribute(MARK);
  c.querySelectorAll('[' + MARK + ']').forEach(n => n.removeAttribute(MARK));
  if (noise) c.querySelectorAll(noise).forEach(n => n.remove());
  if (rsel) c.querySelectorAll(rsel).forEach(n => n.remove());

  // Turn ARIA-role structure and inline-styled emphasis into real HTML, so the
  // markdown conversion recognises them.
  const swap = (n, tag) => {
    if (n.tagName === tag.toUpperCase()) return;
    const r = document.createElement(tag);
    for (const a of ['class', 'colspan', 'rowspan']) if (n.hasAttribute(a)) r.setAttribute(a, n.getAttribute(a));
    r.append(...n.childNodes);
    n.replaceWith(r);
  };
  const unwrap = n => n.replaceWith(...n.childNodes);
  const each = (q, f) => [...c.querySelectorAll(q)].forEach(f);
  each('[role="heading"]', n => swap(n, 'h' + Math.min(6, Math.max(1, parseInt(n.getAttribute('aria-level')) || 3))));
  each('[role="table"], [role="grid"]', n => swap(n, 'table'));
  each('[role="rowgroup"]', unwrap);
  each('[role="row"]', n => swap(n, 'tr'));
  each('[role="columnheader"]', n => swap(n, 'th'));
  each('[role="cell"], [role="gridcell"], [role="rowheader"]', n => swap(n, 'td'));
  each('[role="list"]', n => swap(n, n.closest('ol') ? 'ol' : 'ul'));
  each('[role="listitem"]', n => swap(n, 'li'));
  each('span[style*="font-weight"]', n => {
    const w = (n.style.fontWeight || '').toLowerCase();
    if (w === 'bold' || w === 'bolder' || parseInt(w) >= 600) swap(n, 'strong');
  });
  each('span[style*="italic"]', n => swap(n, 'em'));

  // Descend through single-child wrapper divs to reach the real block list.
  const BLOCK = /^(P|UL|OL|TABLE|PRE|H[1-6]|BLOCKQUOTE|HR)$/;
  let root = c;
  while (root.children.length === 1 &&
         ![...root.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) &&
         !BLOCK.test(root.children[0].tagName)) {
    root = root.children[0];
  }
  root.childNodes.forEach(n => {
    if (n.nodeType === 1) res.blocks.push(n.outerHTML);
    else if (n.nodeType === 3 && n.textContent.trim()) res.blocks.push(n.textContent);
  });
  res.text = text.trim();
  res.clean = (c.textContent || '').trim();
  return res;
}
"""


# ---------------------------------------------------------------- markdown conversion
_LANG_RE = re.compile(r"(?:language|lang)-([\w+#.-]+)")


def _code_language(el) -> str:
    """Language for a fenced code block, from class="language-xxx" on <pre> or its <code>."""
    nodes = [el] + list(el.find_all("code") if hasattr(el, "find_all") else [])
    for n in nodes:
        for cls in (n.get("class") or []):
            m = _LANG_RE.match(cls)
            if m:
                return m.group(1)
    return ""


def html_to_md(html: str) -> str:
    if not html.lstrip().startswith("<"):
        return html.strip()
    return _to_md(html, heading_style="ATX", bullets="-", escape_underscores=False,
                  code_language_callback=_code_language).strip()


# ---------------------------------------------------------------- text alignment
def _norm(s: str):
    """Letters and digits only, lower-cased, plus each one's index in s.

    Comparing on this ignores whitespace, punctuation and markdown syntax, so
    plain text, half-rendered text and markdown of the same words line up.
    """
    idx = [i for i, ch in enumerate(s) if ch.isalnum()]
    return "".join(s[i].lower() for i in idx), idx


def _rest_after(sent: str, text: str, covered: int, tidx) -> str:
    """Part of `text` after its first `covered` normalized chars, without
    re-sending punctuation/markup that `sent` already ended with."""
    pos = tidx[covered - 1] + 1 if covered else 0
    rest = text[pos:]
    # Skip closing markup (e.g. "**", "|") and punctuation the client already has,
    # so "trains." followed by "trains**. Next" continues with " Next".
    tail_marks = set(sent[max((i for i, ch in enumerate(sent) if ch.isalnum()), default=-1) + 1:].strip())
    skippable = tail_marks | set("*_`|~")
    stripped = rest.lstrip(" \t")
    k = 0
    while k < len(stripped) and stripped[k] in skippable:
        k += 1
    return stripped[k:] if k else rest


def extend(sent: str, text: str):
    """What to append to `sent` so the client has `text`, if `text` continues
    `sent` (exactly, or ignoring formatting). None if it doesn't."""
    if not sent:
        return text
    if text.startswith(sent):
        return text[len(sent):]
    sn, _ = _norm(sent)
    tn, tidx = _norm(text)
    if sn and tn.startswith(sn):
        return _rest_after(sent, text, len(sn), tidx)
    return None


def remainder(sent: str, final: str):
    """What the client still needs after receiving `sent` to have all of `final`.

    Tries an exact or formatting-insensitive continuation first. If Copilot
    rewrote earlier text, aligns the end of `sent` inside `final` and returns
    everything after that point. None only if the two can't be aligned at all.
    """
    rest = extend(sent, final)
    if rest is not None:
        return rest
    sn, _ = _norm(sent)
    fn, fidx = _norm(final)
    if not sn or not fn:
        return None
    tail = sn[-3000:]
    sm = difflib.SequenceMatcher(None, tail, fn, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if not blocks or sum(b.size for b in blocks) < 0.5 * len(tail):
        return None
    last = blocks[-1]
    return _rest_after(sent, final, last.b + last.size, fidx)


class DeltaTracker:
    """Plain-text mode: turns successive snapshots of a growing reply into append-only deltas.

    feed(snapshot) returns the new text to emit (or ""). If the snapshot no
    longer starts with what was already sent, nothing is emitted
    (diverged=True) until a later snapshot extends the sent text again.
    """

    def __init__(self):
        self.sent = ""
        self.diverged = False

    def feed(self, snapshot: str) -> str:
        if not snapshot:
            return ""
        if snapshot.startswith(self.sent):
            delta = snapshot[len(self.sent):]
            self.diverged = False
            if delta:
                self.sent = snapshot
            return delta
        if not self.diverged:
            log.debug("Reply text was rewritten mid-stream; pausing deltas")
        self.diverged = True
        return ""


class BlockStreamer:
    """Markdown mode: sends finished blocks as markdown.

    update(blocks_md) holds back the last (unfinished) block and returns the
    markdown that continues what was already sent. If Copilot changed text
    that was already sent so that it no longer lines up, sending pauses
    (diverged=True); the end-of-reply remainder() catches up.
    """

    SEP = "\n\n"

    def __init__(self):
        self.sent = ""
        self.diverged = False

    def update(self, blocks_md: list[str], final: bool = False) -> str:
        ready = blocks_md if final else blocks_md[:-1]
        text = self.SEP.join(ready)
        if not text:
            return ""
        delta = extend(self.sent, text)
        if delta is None:
            self.diverged = True
            return ""
        self.diverged = False
        self.sent += delta
        return delta


def _alnum_count(s: str) -> int:
    return sum(ch.isalnum() for ch in s)


def think_open() -> str:
    return f"<{config.THINK_TAG}>\n"


def think_close() -> str:
    return f"\n</{config.THINK_TAG}>\n\n"


def wrap_reasoning(reasoning: str) -> str:
    """Reasoning wrapped in <THINK_TAG> ... </THINK_TAG> tags, or '' if there is none."""
    reasoning = (reasoning or "").strip()
    return think_open() + reasoning + think_close() if reasoning else ""


class CopilotSession:
    def __init__(self, show_window=False, markdown=True):
        self.show_window = show_window
        self.markdown = markdown and _to_md is not None
        self._md_cache: dict[str, str] = {}
        self._warned_no_stop = False
        self._prompt_norm = ""            # your current prompt, normalized (to recognise its bubble)
        if _to_md is None:
            log.warning("markdownify is not installed - replies will be PLAIN TEXT with no formatting. "
                        "Run: pip install markdownify")
        elif config.STREAM_MODE == "text":
            log.warning("COPILOT_STREAM_MODE=text - streamed replies will be plain text with no formatting.")
        self._q: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="copilot-worker")
        self._thread.start()
        self._ready.wait(timeout=120)
        if self._startup_error:
            raise self._startup_error

    # ---------- public API ----------
    @staticmethod
    def result_timeout() -> float:
        """How long a caller should wait for one reply (queue wait not included)."""
        return (config.REPLY_TIMEOUT + config.START_TIMEOUT + config.INPUT_TIMEOUT
                + config.RESUME_LOAD_TIMEOUT + 120)

    def ask(self, prompt: str, timeout: float | None = None) -> str:
        return self.submit(prompt).result(timeout=timeout or self.result_timeout())

    def submit(self, prompt: str, on_delta=None, resume=None) -> Future:
        """Queue a prompt without blocking; returns a Future.

        on_delta(text) is called from the worker thread with each new piece
        of the reply while it is being written. Keep it fast (e.g. queue.put).
        resume={"url", "reply_hash", "prompt"} continues an existing chat (see module doc).
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("Empty prompt provided.")
        fut: Future = Future()
        fut.streamed_text, fut.truncated, fut.answer_plain = "", False, ""
        fut.chat_url, fut.resumed = None, False
        self._q.put((prompt, fut, on_delta, resume))
        return fut

    @property
    def pending(self) -> int:
        return self._q.qsize()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def close(self):
        self._q.put(None)
        self._thread.join(timeout=15)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- worker thread ----------
    def _run(self):
        try:
            pw = sync_playwright().start()
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=config.PROFILE_DIR,
                channel=config.BROWSER_CHANNEL,
                headless=not self.show_window,
                args=["--start-maximized"] if self.show_window else [],
                no_viewport=self.show_window,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        except Exception as e:
            self._startup_error = CopilotError(f"Could not start browser: {e}")
            self._ready.set()
            return
        self._ready.set()
        log.info("Browser started (visible=%s, stream mode=%s, markdown=%s)",
                 self.show_window, self._stream_mode(), "on" if _to_md else "OFF")

        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                prompt, fut, on_delta, resume = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    t0 = time.time()
                    fut.set_result(self._ask(page, prompt, fut, on_delta, resume))
                    log.info("Prompt answered in %.1fs%s", time.time() - t0,
                             " (resumed chat)" if fut.resumed else "")
                except Exception as e:
                    self._screenshot(page)
                    log.exception("Prompt failed")
                    fut.set_exception(e)
        finally:
            ctx.close()
            pw.stop()

    def _stream_mode(self):
        if config.STREAM_MODE == "markdown" and _to_md is None:
            return "text"  # markdownify not installed
        return config.STREAM_MODE

    def _screenshot(self, page):
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            path = os.path.join(config.LOG_DIR, f"fail_{int(time.time())}.png")
            page.screenshot(path=path, full_page=True)
            log.info("Saved failure screenshot: %s", path)
        except Exception:
            pass

    def _ensure_signed_in(self, page):
        """If redirected to a Microsoft sign-in page, wait for the user (visible mode) or fail fast."""
        try:  # sign-in redirects can happen a moment after the page loads
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        if not _on_login_page(page):
            return
        if not self.show_window:
            raise LoginRequired("Not signed in - run once with --show and complete sign-in.")
        print(f"\n>>> Please sign in to Microsoft 365 in the browser window "
              f"(waiting up to {config.LOGIN_TIMEOUT // 60} min)...", flush=True)
        try:
            page.wait_for_url(lambda url: not _is_login_url(url) and "cloud.microsoft" in url,
                              timeout=config.LOGIN_TIMEOUT * 1000)
        except PWTimeout:
            raise LoginRequired("Sign-in was not completed in time.")
        log.info("Sign-in complete - session saved to %s", config.PROFILE_DIR)
        page.wait_for_load_state("domcontentloaded")
        if "/chat" not in page.url:
            page.goto(config.CHAT_URL, wait_until="domcontentloaded")

    def _open_existing(self, page, resume) -> bool:
        """Open a stored chat and check its last reply is the one we recorded.

        True: the chat is ready to continue. False: fall back to a new chat.
        LoginRequired propagates.
        """
        url = resume["url"]
        try:
            page.goto(url, wait_until="domcontentloaded")
            self._ensure_signed_in(page)
            page.wait_for_function("sel => document.querySelectorAll(sel).length > 0",
                                   arg=config.REPLY_SEL, timeout=config.RESUME_LOAD_TIMEOUT * 1000)
        except LoginRequired:
            raise
        except Exception as e:
            log.info("Could not reopen %s (%s) - starting a new chat", url, type(e).__name__)
            return False
        if conversation_url(page.url) != conversation_url(url):
            log.info("Reopening %s landed on %s - starting a new chat", url, page.url)
            return False

        # Let the chat finish rendering, then compare its last reply.
        prev, deadline = None, time.time() + 10
        while time.time() < deadline:
            text = self._snapshot(page, new_only=False)["clean"]
            if text and text == prev:
                break
            prev = text
            time.sleep(0.5 if config.POLL_INTERVAL >= 0.5 else config.POLL_INTERVAL)
        if fingerprint(prev or "") != resume["reply_hash"]:
            log.info("Chat %s has moved on or changed since it was recorded (client regenerated or edited "
                     "a reply?) - starting a new chat", url)
            return False
        return True

    def _ask(self, page, prompt: str, fut: Future, on_delta=None, resume=None) -> str:
        resumed = False
        if resume:
            resumed = self._open_existing(page, resume)
        if resumed:
            prompt = resume["prompt"].strip()
            fut.resumed = True
        else:
            page.goto(config.CHAT_URL, wait_until="domcontentloaded")  # fresh chat
            self._ensure_signed_in(page)

        try:  # dismiss "Try the app" popover if present
            page.locator(config.POPOVER_SEL).first.click(timeout=2500)
        except PWTimeout:
            pass

        box = page.locator(config.INPUT_SEL).last
        try:
            box.wait_for(state="visible", timeout=config.INPUT_TIMEOUT * 1000)
        except PWTimeout:
            if _on_login_page(page):
                self._ensure_signed_in(page)
                box.wait_for(state="visible", timeout=config.INPUT_TIMEOUT * 1000)
            else:
                raise CopilotError("Chat input not found - selectors may need updating (see config.py).")

        box.click()
        # Mark everything already in the chat so it can never be mistaken for the new reply.
        marked = page.evaluate(MARK_JS, [config.REPLY_SEL, config.REASONING_SEL, RELAY_MARK]) or {}
        self._prompt_norm = normalize(prompt)[:2000]
        log.debug("Marked %s existing reply and %s reasoning element(s)", marked.get("replies"),
                  marked.get("reasoning"))
        page.keyboard.insert_text(prompt)  # whole prompt at once, still fires input events
        page.wait_for_timeout(500)
        # Copilot's chat box has its own character limit and silently cuts off longer input.
        entered = page.evaluate(
            "el => (el.value !== undefined ? el.value : el.innerText).length", box.element_handle())
        if entered < len(prompt.strip()) * 0.98:
            raise PromptTooLong(
                f"Copilot accepted only {entered} of {len(prompt)} characters - its chat box limit was "
                f"exceeded. Shorten the prompt (system prompt + history + message).")

        page.keyboard.press("Enter")
        page.evaluate(
            "sel => { const b = document.querySelector(sel); if (b && !b.disabled) b.click(); }",
            config.SEND_SEL,
        )

        # 1) wait for the NEW reply (or Copilot's reasoning) to appear
        deadline = time.time() + config.START_TIMEOUT
        while True:
            snap = self._snapshot(page)
            if snap["found"] or snap["reasoning"]:
                break
            if time.time() > deadline:
                raise CopilotError("Copilot did not start replying in time (no new reply found after your "
                                   "message - check COPILOT_REPLY_SEL).")
            time.sleep(0.25)

        # 2) wait for it to finish, streaming deltas if requested
        result = self._wait_for_completion(page, fut, on_delta)
        fut.chat_url = conversation_url(page.url)
        if not fut.chat_url:
            log.info("No conversation URL found after the reply (%s); this chat can't be resumed", page.url)
        return result

    # ---------- reply reading ----------
    def _snapshot(self, page, new_only=True) -> dict:
        """{found, reasoning, text, clean, blocks} for the reply (see SNAPSHOT_JS)."""
        snap = page.evaluate(SNAPSHOT_JS, [config.REPLY_SEL, config.NOISE_SEL, config.REASONING_SEL,
                                           config.USER_MSG_SEL,
                                           {"newOnly": new_only, "prompt": self._prompt_norm,
                                            "mark": RELAY_MARK}]) or {}
        text = snap.get("text") or ""
        return {"found": snap.get("found", bool(text)), "reasoning": snap.get("reasoning") or "",
                "text": text, "clean": snap.get("clean") or text, "blocks": snap.get("blocks") or []}

    def _block_md(self, html: str) -> str:
        md = self._md_cache.get(html)
        if md is None:
            md = html_to_md(html)
            if len(self._md_cache) > 2000:
                self._md_cache.clear()
            self._md_cache[html] = md
        return md

    def _blocks_md(self, blocks_html: list[str]) -> list[str]:
        return [md for md in (self._block_md(h) for h in blocks_html) if md]

    def _emit(self, fut, on_delta, delta):
        if not delta:
            return
        fut.streamed_text += delta
        try:
            on_delta(delta)
        except Exception:  # never let a client problem kill the worker
            log.exception("on_delta callback failed")

    def _final_text(self, plain: str, clean: str, blocks_html: list[str], use_md: bool) -> str:
        """Markdown of the whole reply. Falls back to plain text only if the
        conversion clearly lost content compared with the cleaned reply text
        (the same text the markdown was made from, minus buttons/citations)."""
        if not use_md:
            return plain
        md = BlockStreamer.SEP.join(self._blocks_md(blocks_html))
        if not md:
            return plain
        have, want = _alnum_count(md), _alnum_count(clean)
        if have < 0.8 * want - 60:
            log.warning("Markdown conversion has %d of %d letters/digits of the reply - using plain text. "
                        "Set COPILOT_DEBUG_REPLIES=1 and send logs/replies/ to diagnose.", have, want)
            return plain
        return md

    def _debug_dump(self, blocks_html, answer, plain):
        if not config.DEBUG_REPLIES:
            return
        try:
            d = os.path.join(config.LOG_DIR, "replies")
            os.makedirs(d, exist_ok=True)
            stem = os.path.join(d, time.strftime("%Y%m%d-%H%M%S"))
            with open(stem + ".html", "w", encoding="utf-8") as f:
                f.write("\n<!-- block -->\n".join(blocks_html))
            with open(stem + ".md", "w", encoding="utf-8") as f:
                f.write(answer)
            with open(stem + ".txt", "w", encoding="utf-8") as f:
                f.write(plain)
            log.info("Saved reply debug files: %s.{html,md,txt}", stem)
        except Exception:
            log.exception("Could not save reply debug files")

    def _wait_for_completion(self, page, fut: Future, on_delta=None) -> str:
        streaming = on_delta is not None
        mode = self._stream_mode() if streaming else None
        interval = config.STREAM_POLL_INTERVAL if streaming else config.POLL_INTERVAL
        with_reasoning = config.INCLUDE_REASONING and bool(config.REASONING_SEL)
        tracker, blocks = DeltaTracker(), BlockStreamer()
        last = {"reasoning": None, "text": None, "clean": "", "blocks": []}
        reasoning_best, r_sent = "", ""          # longest reasoning seen / reasoning streamed
        think = "none"                           # none -> open -> closed
        stop_seen, ended = False, "timeout"
        t_start = time.time()
        last_change = t_start
        deadline = t_start + config.REPLY_TIMEOUT

        def emit(piece):
            self._emit(fut, on_delta, piece)

        def close_think():
            nonlocal think
            if think == "open":
                emit(think_close())
                think = "closed"

        while time.time() < deadline:
            snap = self._snapshot(page)
            reasoning, text, blocks_html = snap["reasoning"], snap["text"], snap["blocks"]
            now = time.time()
            if text != last["text"] or reasoning != last["reasoning"]:
                last_change = now
            if len(reasoning) > len(reasoning_best):
                reasoning_best = reasoning      # panels may collapse/shrink once done

            if streaming and with_reasoning and reasoning and think != "closed":
                d = extend(r_sent, reasoning)
                if d:
                    if think == "none":
                        emit(think_open())
                        think = "open"
                    r_sent += d
                    emit(d)

            if streaming and text.strip():
                close_think()                   # the answer has started: reasoning phase is over
                if mode == "text":
                    emit(tracker.feed(text))
                elif mode == "markdown":
                    emit(blocks.update(self._blocks_md(blocks_html)))

            generating = page.locator(config.STOP_SEL).count() > 0
            stop_seen = stop_seen or generating
            idle = now - last_change
            last = snap

            if text:
                if not generating and idle >= (config.STABLE_SECONDS if stop_seen
                                               else config.STABLE_SECONDS_NO_STOP):
                    ended = "stable"
                    break
                if generating and idle >= config.STUCK_SECONDS:
                    log.warning("Reply unchanged for %.0fs but a Stop button is still visible - treating it "
                                "as finished. Check COPILOT_STOP_SEL.", idle)
                    ended = "stuck"
                    break
            time.sleep(interval)

        if not last["text"]:
            raise CopilotError("Timed out waiting for Copilot's reply.")
        if ended == "timeout":
            fut.truncated = True
            log.warning("Reply still changing after COPILOT_REPLY_TIMEOUT=%ss - returning it as truncated.",
                        config.REPLY_TIMEOUT)
        if not stop_seen and not self._warned_no_stop:
            self._warned_no_stop = True
            log.warning("No Stop button seen while Copilot was writing - COPILOT_STOP_SEL may be wrong. "
                        "Waiting %ss of no change instead of %ss to decide a reply is finished.",
                        config.STABLE_SECONDS_NO_STOP, config.STABLE_SECONDS)

        plain = (last["text"] or "").strip()
        fut.answer_plain = (last["clean"] or plain).strip()
        use_md = (self.markdown or mode == "markdown") and _to_md is not None and mode != "text"
        answer = self._final_text(plain, last["clean"], last["blocks"], use_md)
        self._debug_dump(last["blocks"], answer, plain)
        reasoning_final = reasoning_best.strip() if with_reasoning else ""
        final = wrap_reasoning(reasoning_final) + answer

        if streaming:
            # Reasoning: finish it, or send it whole if it was never streamed and no answer went out yet.
            answer_sent = tracker.sent if mode == "text" else blocks.sent
            if reasoning_final and think == "none" and not answer_sent:
                emit(wrap_reasoning(reasoning_final))
                think = "closed"
            elif think == "open":
                d = extend(r_sent, reasoning_final)
                if d:
                    emit(d)
                close_think()
            elif reasoning_final and think == "none":
                log.info("Reasoning text appeared only after the answer started; it was not streamed.")

            rest = remainder(answer_sent, answer)
            if rest is None:
                log.warning("Streamed text could not be aligned with the final reply; sending the full final "
                            "reply after a separator so nothing is lost.")
                rest = ("\n\n---\n\n" if answer_sent else "") + answer
            elif tracker.diverged or blocks.diverged:
                log.info("Copilot re-rendered part of the reply after it was sent; sent the remaining %d chars",
                         len(rest))
            emit(rest)

        log.info("Reply finished (%s) after %.1fs: %d chars (reasoning %d)%s", ended, time.time() - t_start,
                 len(answer), len(reasoning_final),
                 f", streamed {len(fut.streamed_text)}" if streaming else "")
        return final


def get_copilot_response(prompt_text, show_window=False):
    """Backwards-compatible one-shot helper (starts and stops a browser)."""
    with CopilotSession(show_window=show_window) as s:
        return s.ask(prompt_text)
