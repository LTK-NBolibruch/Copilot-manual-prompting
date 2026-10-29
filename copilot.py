"""Browser automation for Microsoft 365 Copilot Chat.

CopilotSession keeps ONE Edge instance open and serves prompts one at a time
from a dedicated worker thread (Playwright's sync API is thread-bound).

Streaming (submit(prompt, on_delta=callable)) has two modes, set by
config.STREAM_MODE:

  markdown (default)
      The reply's HTML is split into top-level blocks (paragraph, list, table,
      code block, heading...). Every block except the last is finished, so it
      is converted to markdown and sent. The block Copilot is still writing is
      held back until the next block starts or the reply ends.

  text
      Plain text is sent as it is typed (finer-grained, no formatting).
      Sending pauses whenever Copilot reformats text that was already sent.

Reasoning: text from Copilot's reasoning ("thinking") section, found with
config.REASONING_SEL, is sent first, wrapped in <THINK_TAG> ... </THINK_TAG>
(config.THINK_TAG, default "think"), and kept out of the answer text.

Completeness: when the reply finishes, whatever the client has not received
yet is always sent, even if Copilot re-rendered earlier text (see remainder()).
Future.result() is the full reply; Future.streamed_text is exactly what was
sent through on_delta; Future.truncated is True if the reply hit REPLY_TIMEOUT.
"""
import difflib
import logging
import os
import queue
import threading
import time
from concurrent.futures import Future

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import config

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


# Reads the last reply: its plain text (for completion detection) and its
# top-level blocks as cleaned HTML (for markdown conversion).
SNAPSHOT_JS = r"""
([sel, noise, rsel, rBefore]) => {
  const pick = e => (e.innerText || '').trim() || (e.textContent || '').trim();  // collapsed panels: textContent
  // Reasoning: matches added since the prompt was sent, outermost only.
  let reasoning = '', rEls = [];
  if (rsel) {
    const all = [...document.querySelectorAll(rsel)].slice(rBefore);
    rEls = all.filter(e => !all.some(o => o !== e && o.contains(e)));
    reasoning = rEls.map(pick).filter(Boolean).join('\n\n');
  }
  const m = document.querySelectorAll(sel);
  if (!m.length) return {reasoning, text: '', blocks: []};
  const el = m[m.length - 1];
  // The last "reply" element is itself reasoning -> the answer hasn't started.
  if (rsel && el.closest(rsel)) return {reasoning, text: '', blocks: []};
  // Answer text without any reasoning nested inside the reply element.
  let text = el.innerText || '';
  rEls.filter(r => el.contains(r)).forEach(r => { text = text.replace(r.innerText || '', ''); });
  const c = el.cloneNode(true);
  if (noise) c.querySelectorAll(noise).forEach(n => n.remove());
  if (rsel) c.querySelectorAll(rsel).forEach(n => n.remove());
  // Descend through single-child wrapper divs to reach the real block list.
  const BLOCK = /^(P|UL|OL|TABLE|PRE|H[1-6]|BLOCKQUOTE|HR)$/;
  let root = c;
  while (root.children.length === 1 &&
         ![...root.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) &&
         !BLOCK.test(root.children[0].tagName)) {
    root = root.children[0];
  }
  const blocks = [];
  root.childNodes.forEach(n => {
    if (n.nodeType === 1) blocks.push(n.outerHTML);
    else if (n.nodeType === 3 && n.textContent.trim()) blocks.push(n.textContent);
  });
  return {reasoning, text: text.trim(), blocks};
}
"""


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
        return config.REPLY_TIMEOUT + config.START_TIMEOUT + config.INPUT_TIMEOUT + 120

    def ask(self, prompt: str, timeout: float | None = None) -> str:
        return self.submit(prompt).result(timeout=timeout or self.result_timeout())

    def submit(self, prompt: str, on_delta=None) -> Future:
        """Queue a prompt without blocking; returns a Future.

        on_delta(text) is called from the worker thread with each new piece
        of the reply while it is being written. Keep it fast (e.g. queue.put).
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("Empty prompt provided.")
        fut: Future = Future()
        fut.streamed_text = ""
        fut.truncated = False
        self._q.put((prompt, fut, on_delta))
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
        log.info("Browser started (visible=%s, stream mode=%s)", self.show_window, self._stream_mode())

        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                prompt, fut, on_delta = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    t0 = time.time()
                    fut.set_result(self._ask(page, prompt, fut, on_delta))
                    log.info("Prompt answered in %.1fs", time.time() - t0)
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
            page.wait_for_url(lambda url: not _is_login_url(url) and "m365.cloud.microsoft" in url,
                              timeout=config.LOGIN_TIMEOUT * 1000)
        except PWTimeout:
            raise LoginRequired("Sign-in was not completed in time.")
        log.info("Sign-in complete - session saved to %s", config.PROFILE_DIR)
        page.wait_for_load_state("domcontentloaded")
        if "/chat" not in page.url:
            page.goto(config.CHAT_URL, wait_until="domcontentloaded")

    def _ask(self, page, prompt: str, fut: Future, on_delta=None) -> str:
        page.goto(config.CHAT_URL, wait_until="domcontentloaded")  # fresh chat per prompt
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
        before = page.locator(config.REPLY_SEL).count()
        self._r_before = page.locator(config.REASONING_SEL).count() if config.REASONING_SEL else 0
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

        # 1) wait for a NEW reply bubble, or for Copilot to start reasoning
        try:
            page.wait_for_function(
                "([sel, n, rsel, rn]) => document.querySelectorAll(sel).length > n ||"
                " (rsel && document.querySelectorAll(rsel).length > rn)",
                arg=[config.REPLY_SEL, before, config.REASONING_SEL, self._r_before],
                timeout=config.START_TIMEOUT * 1000,
            )
        except PWTimeout:
            raise CopilotError("Copilot did not start replying in time.")

        # 2) wait for it to finish, streaming deltas if requested
        return self._wait_for_completion(page, fut, on_delta)

    # ---------- reply reading ----------
    def _snapshot(self, page) -> tuple[str, str, list[str]]:
        """(reasoning text, answer text, answer blocks as HTML)."""
        snap = page.evaluate(SNAPSHOT_JS, [config.REPLY_SEL, config.NOISE_SEL,
                                           config.REASONING_SEL, getattr(self, "_r_before", 0)])
        return snap.get("reasoning") or "", snap.get("text") or "", snap.get("blocks") or []

    def _block_md(self, html: str) -> str:
        md = self._md_cache.get(html)
        if md is None:
            md = _to_md(html, heading_style="ATX", bullets="-").strip() if html.lstrip().startswith("<") \
                else html.strip()
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

    def _final_text(self, plain: str, blocks_html: list[str], use_md: bool) -> str:
        """Markdown of the whole reply, falling back to plain text if the
        conversion lost content (e.g. a block the snapshot script missed)."""
        if not use_md:
            return plain
        md = BlockStreamer.SEP.join(self._blocks_md(blocks_html))
        if _alnum_count(md) < 0.9 * _alnum_count(plain) - 60:
            log.warning("Markdown conversion has %d of %d letters/digits of the reply - using plain text. "
                        "Check COPILOT_NOISE_SEL / the reply HTML.", _alnum_count(md), _alnum_count(plain))
            return plain
        return md or plain

    def _wait_for_completion(self, page, fut: Future, on_delta=None) -> str:
        streaming = on_delta is not None
        mode = self._stream_mode() if streaming else None
        interval = config.STREAM_POLL_INTERVAL if streaming else config.POLL_INTERVAL
        with_reasoning = config.INCLUDE_REASONING and bool(config.REASONING_SEL)
        tracker, blocks = DeltaTracker(), BlockStreamer()
        last_text, last_blocks, last_reasoning = None, [], None
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
            reasoning, text, blocks_html = self._snapshot(page)
            now = time.time()
            if text != last_text or reasoning != last_reasoning:
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
                    self._emit(fut, on_delta, tracker.feed(text))
                elif mode == "markdown":
                    self._emit(fut, on_delta, blocks.update(self._blocks_md(blocks_html)))

            generating = page.locator(config.STOP_SEL).count() > 0
            stop_seen = stop_seen or generating
            idle = now - last_change
            last_text, last_blocks, last_reasoning = text, blocks_html, reasoning

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

        if not last_text:
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

        plain = (last_text or "").strip()
        use_md = (self.markdown or mode == "markdown") and _to_md is not None and mode != "text"
        answer = self._final_text(plain, last_blocks, use_md)
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
