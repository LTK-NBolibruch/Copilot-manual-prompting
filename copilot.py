"""Browser automation for Microsoft 365 Copilot Chat.

CopilotSession keeps ONE Edge instance open and serves prompts one at a time
from a dedicated worker thread (Playwright's sync API is thread-bound).

Streaming: pass on_delta=callable to submit(). While Copilot is writing, the
worker polls the reply and calls on_delta(new_text) with each newly appended
piece. Only text that strictly extends what was already sent is emitted; if
Copilot rewrites earlier text (re-rendering into a table, code block, adding
citation markers...), emission pauses and resumes only if the reply again
extends what was sent. The final result (Future.result()) is always the full
reply; Future.streamed_text holds exactly what was emitted.
"""
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


class DeltaTracker:
    """Turns successive snapshots of a growing reply into append-only deltas.

    feed(snapshot) returns the new text to emit (or "").
    If the snapshot no longer starts with what was already sent, nothing is
    emitted (diverged=True) until a later snapshot extends the sent text again.
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


class CopilotSession:
    def __init__(self, show_window=False, markdown=True):
        self.show_window = show_window
        self.markdown = markdown and _to_md is not None
        self._q: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="copilot-worker")
        self._thread.start()
        self._ready.wait(timeout=120)
        if self._startup_error:
            raise self._startup_error

    # ---------- public API ----------
    def ask(self, prompt: str, timeout: float | None = None) -> str:
        return self.submit(prompt).result(timeout=timeout or config.REPLY_TIMEOUT + config.START_TIMEOUT + 60)

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
        log.info("Browser started (visible=%s)", self.show_window)

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

        # 1) wait for a NEW reply bubble
        try:
            page.wait_for_function(
                "([sel, n]) => document.querySelectorAll(sel).length > n",
                arg=[config.REPLY_SEL, before],
                timeout=config.START_TIMEOUT * 1000,
            )
        except PWTimeout:
            raise CopilotError("Copilot did not start replying in time.")

        # 2) wait for it to finish, streaming deltas if requested
        return self._wait_for_completion(page, fut, on_delta)

    def _wait_for_completion(self, page, fut: Future, on_delta=None) -> str:
        streaming = on_delta is not None
        interval = config.STREAM_POLL_INTERVAL if streaming else config.POLL_INTERVAL
        tracker = DeltaTracker()
        last, stable_since = None, None
        deadline = time.time() + config.REPLY_TIMEOUT

        while time.time() < deadline:
            text = self._last_reply_text(page)

            if streaming:
                delta = tracker.feed(text)
                if delta:
                    fut.streamed_text = tracker.sent
                    try:
                        on_delta(delta)
                    except Exception:  # never let a client problem kill the worker
                        log.exception("on_delta callback failed")

            generating = page.locator(config.STOP_SEL).count() > 0
            if text and text == last and not generating:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= config.STABLE_SECONDS:
                    break
            else:
                stable_since = None
            last = text
            time.sleep(interval)
        else:
            if not last:
                raise CopilotError("Timed out waiting for Copilot's reply.")
            log.warning("Reply timeout reached - returning partial text.")

        plain = (last or "").strip()
        if streaming:
            # Final catch-up: emit any tail that still extends what was sent.
            delta = tracker.feed(last or "")
            if delta:
                fut.streamed_text = tracker.sent
                try:
                    on_delta(delta)
                except Exception:
                    log.exception("on_delta callback failed")
            if tracker.diverged:
                log.info("Final reply differs from streamed text (Copilot re-rendered it)")
            # Streaming clients receive plain text; return it so result == what they saw where possible.
            return plain

        if self.markdown:
            html = page.evaluate(
                "sel => { const m = document.querySelectorAll(sel); return m.length ? m[m.length-1].innerHTML : ''; }",
                config.REPLY_SEL,
            )
            return _to_md(html, heading_style="ATX").strip()
        return plain

    @staticmethod
    def _last_reply_text(page) -> str:
        return page.evaluate(
            "sel => { const m = document.querySelectorAll(sel); return m.length ? m[m.length-1].innerText : ''; }",
            config.REPLY_SEL,
        )


def get_copilot_response(prompt_text, show_window=False):
    """Backwards-compatible one-shot helper (starts and stops a browser)."""
    with CopilotSession(show_window=show_window) as s:
        return s.ask(prompt_text)
