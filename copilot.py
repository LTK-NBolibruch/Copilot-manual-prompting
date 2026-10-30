"""Browser automation for Microsoft 365 Copilot Chat.

CopilotSession serves prompts one at a time from a dedicated worker thread
(Playwright's sync API is thread-bound). It either launches its own Edge
(single-server use: server.py, ask.py), or - given cdp_url - opens its own TAB
in a browser started by BrowserHost, so several sessions can run at once with
one browser profile and one sign-in (manager.py).

BrowserHost starts a NORMAL Edge process (not one launched by Playwright) with
remote debugging on a local port; sessions attach to it. Copilot only loads one
chat at a time in a Playwright-launched Edge, but several in a normal one.

Sessions can be cancelled while starting (cancel=Event) and aborted at any
time (abort()): waits inside the worker check for that at least once a second.

Models: model_names() lists the models in Copilot's model picker, including
models inside its sub-menus (read from the web UI at startup and every
MODELS_REFRESH_MINUTES, or taken from config.MODELS). submit(..., model=name)
selects that model in the picker - opening its sub-menu first if needed -
before the prompt is sent; model=None keeps the current selection.

Resuming: submit(..., resume={"url", "reply_hash", "prompt"}) reopens an
existing Copilot chat and sends only resume["prompt"] (the new user message),
after checking that the chat's last reply matches resume["reply_hash"]. If the
chat can't be opened or is in a different state (e.g. the client regenerated
an earlier reply), a new chat is started with the full prompt instead.

Finding the reply: before a prompt is sent, every reply and reasoning element
already on the page is marked. Afterwards, only unmarked elements that come
after the marked ones - and after your own message bubble, which is recognised
by its text or USER_MSG_SEL - count as the new reply.

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
    model           the model that answered (requested name, or what the picker showed)
"""
import difflib
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import Future

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import config
import procs
from conversations import conversation_url, fingerprint, normalize
from models import checkmark_key, option_name, picker_label

log = logging.getLogger("copilot")

try:
    from markdownify import markdownify as _to_md
except ImportError:  # optional dependency
    _to_md = None

# Keep tabs that aren't in front running at full speed (several sessions = several tabs).
BROWSER_ARGS = ["--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding"]


class CopilotError(RuntimeError):
    """Base error for automation failures."""


class PromptTooLong(CopilotError):
    """The prompt exceeds what Copilot's chat box will accept."""


class LoginRequired(CopilotError):
    """The saved session has expired; sign in again with --show."""


class SessionCancelled(CopilotError):
    """The session was cancelled while starting, or removed."""


LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com", "device.login")


def _is_login_url(url: str) -> bool:
    return any(h in url for h in LOGIN_HOSTS)


def _on_login_page(page) -> bool:
    return _is_login_url(page.url)


def _launch_edge(pw, show_window, extra_args=()):
    return pw.chromium.launch_persistent_context(
        user_data_dir=config.PROFILE_DIR,
        channel=config.BROWSER_CHANNEL,
        headless=not show_window,
        args=BROWSER_ARGS + list(extra_args) + (["--start-maximized"] if show_window else []),
        no_viewport=show_window,
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_edge() -> str:
    """Path of msedge.exe (config.EDGE_PATH, else the usual install locations / PATH)."""
    if config.EDGE_PATH:
        return config.EDGE_PATH
    candidates = []
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            candidates.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    for name in ("msedge", "microsoft-edge", "microsoft-edge-stable"):
        found = shutil.which(name)
        if found:
            return found
    raise CopilotError("Could not find Microsoft Edge (msedge.exe). Set COPILOT_EDGE_PATH to its full path.")


def edge_command(port: int, visible: bool) -> list[str]:
    """Command line for a normal Edge with remote debugging, using the saved profile."""
    args = [find_edge(), f"--remote-debugging-port={port}",
            f"--user-data-dir={os.path.abspath(config.PROFILE_DIR)}",
            "--no-first-run", "--no-default-browser-check", *BROWSER_ARGS]
    if visible:
        args.append("--window-size=1280,900")
    elif config.EDGE_HIDDEN == "headless":
        args.append("--headless=new")
    else:
        args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
    return args + [config.CHAT_URL]


def wait_for_devtools(port: int, proc, timeout: float, cancel=None):
    """Wait until Edge answers on its debugging port. Raises CopilotError with a useful message."""
    in_use = (f"Another Edge is probably already using the profile {os.path.abspath(config.PROFILE_DIR)} "
              f"(for example left over from a crash). End the msedge.exe processes in Task Manager, "
              f"then click Retry.")
    deadline = time.time() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            raise SessionCancelled("Browser start cancelled.")
        rc = proc.poll()
        if rc is not None:
            raise CopilotError(f"Edge closed right after starting (exit code {rc}). {in_use}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return
        except Exception:
            pass
        if time.time() > deadline:
            raise CopilotError(f"Edge did not open its debugging port within {timeout:.0f}s. {in_use}")
        time.sleep(0.25)


def _end_process(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()


class BrowserHost:
    """A normal Edge (started with subprocess, NOT by Playwright) with the saved profile and remote
    debugging on a local port. CopilotSessions attach to it with cdp_url=host.cdp_url, each in its
    own tab. The host keeps the tab Edge opened (on Copilot) for signing in, and watches whether the
    browser is signed in.

    Before starting Edge it checks for Edge processes already using the profile (config.STALE_EDGE):
    with "ask", it waits in .blocked until end_stale_and_start() or stop(). A background monitor
    re-scans the processes every PROCESS_SCAN_SECONDS into .scan, for the manager window.

    start() / stop() / restart(); wait_ready(); set_visible(); end_stale_and_start(); end_stale();
    .error, .needs_sign_in, .visible, .blocked, .scan, .pid
    """

    def __init__(self, show_window=False, start=True, monitor=True):
        self.visible = show_window
        self.headless = False
        self.cdp_port = None
        self.ready = threading.Event()
        self.error = None
        self.needs_sign_in = False
        self.blocked = None                 # procs.Scan while waiting for leftovers to be ended
        self.busy = ""                      # e.g. "Ending leftover processes..."
        self.scan = None                    # latest procs.Scan
        self.proc = None
        self._cmds: "queue.Queue" = queue.Queue()
        self._thread = None
        self._monitor_stop = threading.Event()
        self._rescan = threading.Event()
        if monitor:
            threading.Thread(target=self._monitor, daemon=True, name="process-monitor").start()
        if start:
            self.start()

    @property
    def pid(self):
        p = self.proc
        return p.pid if p is not None and p.poll() is None else None

    def rescan(self):
        """Ask the monitor to re-scan now."""
        self._rescan.set()

    def _monitor(self):
        while not self._monitor_stop.is_set():
            self.scan = procs.scan(config.PROFILE_DIR, self.pid)
            self._rescan.wait(config.PROCESS_SCAN_SECONDS)
            self._rescan.clear()

    def end_stale_and_start(self):
        """While blocked: end the leftover / in-use Edge processes and start."""
        self._cmds.put(("end_stale",))

    def end_stale(self, pids=None):
        """While running: end leftover Edge processes and orphaned drivers (or the groups whose main
        PID is in `pids`), in the background. Never ends this manager's own Edge."""
        def run():
            self.busy = "Ending processes…"
            try:
                procs.end_groups(config.PROFILE_DIR, pids=pids, our_pid=self.pid)
            finally:
                self.busy = ""
                self.rescan()
        threading.Thread(target=run, daemon=True, name="end-processes").start()

    @property
    def cdp_url(self):
        return f"http://127.0.0.1:{self.cdp_port}"

    @property
    def can_show(self):
        return self.ready.is_set() and not self.error and not self.headless

    def start(self):
        self.ready.clear()
        self.error, self.needs_sign_in = None, False
        self.cdp_port = free_port()
        self.headless = not self.visible and config.EDGE_HIDDEN == "headless"
        self._cmds = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="browser-host")
        self._thread.start()

    def stop(self, timeout=15, final=False):
        if final:
            self._monitor_stop.set()
            self._rescan.set()
        if self._thread is None:
            return
        last = self.scan
        ours = set(last.ours.pids) if last is not None and last.ours else set()
        if self.proc is not None:
            ours.add(self.proc.pid)
        self._cmds.put(("stop",))
        self._thread.join(timeout=timeout)
        _end_process(self.proc)
        if self.proc is not None:           # helpers that outlive the main process
            found = procs.scan(config.PROFILE_DIR)
            mine = [g.pid for g in found.groups if set(g.pids) & ours]
            if mine:
                procs.end_groups(config.PROFILE_DIR, pids=mine, wait=5)
            if not procs.wait_profile_free(config.PROFILE_DIR, timeout=10):
                log.warning("Edge processes are still using the profile after stopping")
        self.proc = None
        self.rescan()

    def restart(self):
        self.stop()
        self.start()

    def wait_ready(self, cancel=None, timeout=None) -> None:
        """Wait for the browser; raises its error, SessionCancelled, or CopilotError on timeout."""
        limit = timeout if timeout is not None else config.BROWSER_START_TIMEOUT + 30
        deadline = time.time() + limit
        while not self.ready.wait(0.2):
            if cancel is not None and cancel.is_set():
                raise SessionCancelled("Cancelled while waiting for the browser.")
            if self.blocked is not None or self.busy:   # waiting for you: no time limit
                deadline = time.time() + limit
            if time.time() > deadline:
                raise CopilotError("The browser did not start in time.")
        if self.error:
            raise self.error

    def set_visible(self, visible: bool):
        """Move the browser window on- or off-screen (not available in headless mode)."""
        if not self.can_show:
            return
        self.visible = visible
        self._cmds.put(("window", visible))

    def _check_stale(self) -> bool:
        """Before starting Edge: deal with Edge processes already using the profile.
        False = stop was requested while waiting."""
        if config.STALE_EDGE == "ignore":
            return True
        found = procs.scan(config.PROFILE_DIR)
        if found.error or not found.blocking:
            return True
        if config.STALE_EDGE != "end":
            self.blocked = found
            self.scan = found
            log.warning("Not starting Edge yet: %s. End them from the manager window "
                        "(or set COPILOT_STALE_EDGE=end to do it automatically).", found.describe())
            while True:
                try:
                    cmd = self._cmds.get(timeout=2)
                except queue.Empty:               # ended some other way (Task Manager, Processes list)?
                    found = procs.scan(config.PROFILE_DIR)
                    if not found.error and not found.blocking:
                        self.blocked = None
                        log.info("The profile is free now - starting Edge")
                        return True
                    self.blocked = found
                    continue
                if cmd[0] == "stop":
                    self.blocked = None
                    return False
                if cmd[0] == "end_stale":
                    break
        self.blocked = None
        self.busy = "Ending processes…"
        try:
            left = procs.end_groups(config.PROFILE_DIR, kinds=("leftover", "in-use", "driver"))
        finally:
            self.busy = ""
            self.rescan()
        if left:
            raise CopilotError(f"Could not end the Edge processes using the profile (PIDs {left}). "
                               f"End them in Task Manager, then click Retry.")
        return True

    def _run(self):
        try:
            if not self._check_stale():
                return
            self.proc = subprocess.Popen(
                edge_command(self.cdp_port, self.visible), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))  # Ctrl+C in the console
            wait_for_devtools(self.cdp_port, self.proc, config.BROWSER_START_TIMEOUT)  # doesn't reach Edge
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_url, timeout=config.BROWSER_START_TIMEOUT * 1000)
                ctx = browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                time.sleep(2)                            # let a sign-in redirect happen
                self._check_sign_in(page)
                log.info("Browser started for sessions (Edge PID %d, %s, DevTools port %d)%s", self.proc.pid,
                         "visible" if self.visible else ("headless" if self.headless else "off-screen"),
                         self.cdp_port, " - NOT SIGNED IN" if self.needs_sign_in else "")
                self.ready.set()
                self.rescan()
                while True:
                    try:
                        cmd = self._cmds.get(timeout=3)
                    except queue.Empty:
                        if self.proc.poll() is not None:
                            raise CopilotError("Edge was closed. Click Retry to start it again.")
                        if page.is_closed():             # the user closed the sign-in tab
                            page = ctx.new_page()
                            page.goto(config.CHAT_URL, wait_until="domcontentloaded")
                        self._check_sign_in(page)
                        continue
                    if cmd[0] == "stop":
                        try:
                            browser.new_browser_cdp_session().send("Browser.close")
                        except Exception:
                            pass
                        break
                    if cmd[0] == "end_stale":          # not blocked any more: nothing to do
                        continue
                    if cmd[0] == "window":
                        try:
                            self._set_window(page, cmd[1])
                        except Exception as e:
                            log.warning("Could not %s the browser window: %s", "show" if cmd[1] else "hide", e)
        except Exception as e:
            self.error = e if isinstance(e, CopilotError) else CopilotError(f"Could not start Edge: {e}")
            log.error("%s", self.error)
        finally:
            self.ready.set()
            _end_process(self.proc)

    def _check_sign_in(self, page):
        url = page.url or ""
        if _is_login_url(url):
            self.needs_sign_in = True
        elif "cloud.microsoft" in url:
            self.needs_sign_in = False

    def _set_window(self, page, visible):
        cdp = page.context.new_cdp_session(page)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        bounds = {"left": 80, "top": 60, "width": 1280, "height": 900} if visible else \
            {"left": -32000, "top": -32000}
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": bounds})
        if visible:
            page.bring_to_front()
            if "/chat" not in (page.url or "") and not _is_login_url(page.url or ""):
                page.goto(config.CHAT_URL, wait_until="domcontentloaded")


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

# The model picker button: its aria-label ("Auto, Model Selector") and visible text.
PICKER_JS = r"""
sel => {
  const b = document.querySelector(sel);
  return b ? {aria: b.getAttribute('aria-label') || '', text: b.innerText || ''} : null;
}
"""

# Whether any model menu is open.
MENU_OPEN_JS = r"""
sel => [...document.querySelectorAll(sel)].some(e => e.getClientRects().length > 0)
"""

# Tags an open model menu and returns its options.
#   which = "main": the first visible menu (the picker's menu)
#   which = "sub":  the last visible menu, if it isn't the main one (an open sub-menu); else null
# The menu gets data-relay-menu=<which> and each option data-relay-opt=<index>, so the
# option can be clicked by index afterwards.
MENU_JS = r"""
([menuSel, optSel, subSel, which]) => {
  const vis = e => e.getClientRects().length > 0;
  const menus = [...document.querySelectorAll(menuSel)].filter(vis);
  if (!menus.length) return null;
  const menu = which === 'main' ? menus[0] : menus[menus.length - 1];
  if (which === 'sub' && (menus.length < 2 || menu.getAttribute('data-relay-menu') === 'main')) return null;
  document.querySelectorAll('[data-relay-menu="' + which + '"]').forEach(m => m.removeAttribute('data-relay-menu'));
  menu.setAttribute('data-relay-menu', which);
  const opts = [...menu.querySelectorAll(optSel)].filter(o => o.closest(menuSel) === menu);
  return opts.map((o, i) => {
    o.setAttribute('data-relay-opt', String(i));
    const c = o.querySelector('.fui-MenuItem__content');
    const k = o.querySelector('svg[data-testid^="checkmark-"]');
    let sub = false;
    try { sub = !!(subSel && o.matches(subSel)); } catch (e) {}
    return {content: c ? c.innerText : '', text: o.innerText || o.textContent || '',
            checked: o.getAttribute('aria-checked') === 'true' || o.getAttribute('aria-selected') === 'true',
            disabled: o.getAttribute('aria-disabled') === 'true',
            submenu: sub, key: k ? k.getAttribute('data-testid').slice('checkmark-'.length) : ''};
  });
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


def configured_models() -> list[str]:
    return [m.strip() for m in config.MODELS.split(",") if m.strip()]


class CopilotSession:
    def __init__(self, show_window=False, markdown=True, discover_models=True, cdp_url=None, name=None,
                 cancel=None, is_visible=None, start_timeout=None):
        """cdp_url: connect to a BrowserHost and work in a new tab of it, instead of launching Edge.
        name: shown in log lines (thread name copilot-<name>).
        cancel: threading.Event - setting it while the session starts makes __init__ raise SessionCancelled.
        is_visible: callable - whether the browser window can be seen now (for signing in).
        start_timeout: seconds to wait for the tab/browser (default config.SESSION_START_TIMEOUT)."""
        self.show_window = show_window
        self._is_visible = is_visible
        self.markdown = markdown and _to_md is not None
        self.discover_models = discover_models
        self.cdp_url = cdp_url
        self.name = name or "worker"
        self.needs_sign_in = False
        self._aborted = False
        self._md_cache: dict[str, str] = {}
        self._warned_no_stop = False
        self._prompt_norm = ""            # your current prompt, normalized (to recognise its bubble)
        self._models = {"names": configured_models(), "current": None, "fetched": 0.0,
                        "source": "config" if configured_models() else "none",
                        "paths": {}, "keys": {}}   # name -> sub-menu trigger names / internal key
        self._models_lock = threading.Lock()
        self._models_refresh: Future | None = None
        if _to_md is None:
            log.warning("markdownify is not installed - replies will be PLAIN TEXT with no formatting. "
                        "Run: pip install markdownify")
        elif config.STREAM_MODE == "text":
            log.warning("COPILOT_STREAM_MODE=text - streamed replies will be plain text with no formatting.")
        self._q: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._startup_error = None
        self._start_timeout = start_timeout or config.SESSION_START_TIMEOUT
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"copilot-{self.name}")
        self._thread.start()
        deadline = time.time() + self._start_timeout + 5
        while not self._ready.wait(0.2):
            if cancel is not None and cancel.is_set():
                self.abort()
                raise SessionCancelled("Start cancelled.")
            if time.time() > deadline:
                self.abort()
                raise CopilotError(f"The browser {'tab' if cdp_url else 'window'} did not open within "
                                   f"{self._start_timeout}s.")
        if self._startup_error:
            raise self._startup_error
        if cancel is not None and cancel.is_set():
            self.abort()
            raise SessionCancelled("Start cancelled.")

    # ---------- public API ----------
    @staticmethod
    def result_timeout() -> float:
        """How long a caller should wait for one reply (queue wait not included)."""
        return (config.REPLY_TIMEOUT + config.START_TIMEOUT + config.INPUT_TIMEOUT
                + config.RESUME_LOAD_TIMEOUT + 2 * config.MODEL_TIMEOUT + 120)

    def ask(self, prompt: str, timeout: float | None = None, model: str | None = None) -> str:
        return self.submit(prompt, model=model).result(timeout=timeout or self.result_timeout())

    def submit(self, prompt: str, on_delta=None, resume=None, model: str | None = None) -> Future:
        """Queue a prompt without blocking; returns a Future.

        on_delta(text) is called from the worker thread with each new piece
        of the reply while it is being written. Keep it fast (e.g. queue.put).
        resume={"url", "reply_hash", "prompt"} continues an existing chat (see module doc).
        model: a name from model_names() to select first, or None to keep the current selection.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("Empty prompt provided.")
        fut: Future = Future()
        fut.streamed_text, fut.truncated, fut.answer_plain = "", False, ""
        fut.chat_url, fut.resumed, fut.model = None, False, None
        self._q.put(("prompt", prompt, fut, on_delta, resume, model))
        return fut

    def model_names(self, wait: float = 0.0) -> list[str]:
        """Model names from the picker (or config.MODELS). Refreshes in the background when
        stale; waits up to `wait` seconds if the list has never been read."""
        with self._models_lock:
            m = self._models
            if m["source"] == "config":
                return list(m["names"])
            stale = time.time() - m["fetched"] > config.MODELS_REFRESH_MINUTES * 60
            if stale and self.discover_models and (self._models_refresh is None or self._models_refresh.done()):
                self._models_refresh = Future()
                self._q.put(("models", self._models_refresh))
            pending = self._models_refresh
            never = not m["fetched"]
        if never and wait and pending is not None:
            try:
                pending.result(timeout=wait)
            except Exception:
                pass
        with self._models_lock:
            return list(self._models["names"])

    def model_keys(self) -> dict:
        """{name: Copilot's internal key} for models whose option shows one (e.g. "Gpt_5_6_Chat")."""
        with self._models_lock:
            return dict(self._models["keys"])

    @property
    def current_model(self):
        with self._models_lock:
            return self._models["current"]

    @property
    def pending(self) -> int:
        return self._q.qsize()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def close(self, timeout=15):
        """Finish the prompts already queued, then close the tab / browser. Returns False if that
        didn't happen within `timeout` (use abort() then)."""
        self._q.put(None)
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def abort(self):
        """Stop now: the prompt in progress fails within about a second, queued prompts fail, and the
        tab / browser is closed. Doesn't wait."""
        self._aborted = True
        self._q.put(None)

    @property
    def aborted(self):
        return getattr(self, "_aborted", False)

    def _check_abort(self):
        if self.aborted:
            raise SessionCancelled("The session was stopped.")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- worker thread ----------
    def _open_page(self, pw):
        """(page, cleanup) - a tab in the BrowserHost, or a browser of our own."""
        if self.cdp_url:
            last = None
            deadline = time.time() + self._start_timeout
            while True:                 # the host's DevTools port can take a moment to answer
                try:
                    browser = pw.chromium.connect_over_cdp(
                        self.cdp_url, timeout=max(1000, (deadline - time.time()) * 1000))
                    break
                except Exception as e:
                    last = e
                if self.aborted or time.time() > deadline:
                    raise last
                time.sleep(1)
            if not browser.contexts:
                raise CopilotError("The shared browser has no profile context.")
            page = browser.contexts[0].new_page()
            return page, page.close     # close only our tab; the host owns the browser
        ctx = _launch_edge(pw, self.show_window)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return page, ctx.close

    def _run(self):
        pw = None
        try:
            pw = sync_playwright().start()
            page, cleanup = self._open_page(pw)
        except Exception as e:
            self._startup_error = CopilotError(f"Could not start browser{' tab' if self.cdp_url else ''}: {e}")
            self._ready.set()
            if pw:
                pw.stop()
            return
        self._ready.set()
        try:
            if self.aborted:            # cancelled while the tab was opening
                return
            log.info("%s started (visible=%s, stream mode=%s, markdown=%s)",
                     "Browser tab" if self.cdp_url else "Browser", self._visible(), self._stream_mode(),
                     "on" if _to_md else "OFF")
            if self.discover_models and self._models["source"] != "config":
                self._refresh_models(page)
            while True:
                item = self._q.get()
                if item is None or self.aborted:
                    break
                if item[0] == "models":
                    fut = item[1]
                    if fut.set_running_or_notify_cancel():
                        fut.set_result(self._refresh_models(page))
                    continue
                _, prompt, fut, on_delta, resume, model = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    t0 = time.time()
                    fut.set_result(self._ask(page, prompt, fut, on_delta, resume, model))
                    log.info("Prompt answered in %.1fs%s%s", time.time() - t0,
                             " (resumed chat)" if fut.resumed else "",
                             f" by model {fut.model!r}" if fut.model else "")
                except SessionCancelled as e:
                    fut.set_exception(e)
                except Exception as e:
                    self._screenshot(page)
                    log.exception("Prompt failed")
                    fut.set_exception(e)
        finally:
            self._fail_queued()
            try:
                cleanup()
            except Exception:
                pass
            pw.stop()
            log.info("%s closed", "Browser tab" if self.cdp_url else "Browser")

    def _fail_queued(self):
        """Fail prompts still waiting in the queue when the session stops."""
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return
            if item and item[0] == "prompt":
                fut = item[2]
            elif item and item[0] == "models":
                fut = item[1]
            else:
                continue
            if fut.set_running_or_notify_cancel():
                if item[0] == "models":
                    fut.set_result([])
                else:
                    fut.set_exception(SessionCancelled("The session was stopped before this prompt ran."))

    def _visible(self) -> bool:
        f = getattr(self, "_is_visible", None)
        return bool(f()) if f else self.show_window

    def _stream_mode(self):
        if config.STREAM_MODE == "markdown" and _to_md is None:
            return "text"  # markdownify not installed
        return config.STREAM_MODE

    def _screenshot(self, page):
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            path = os.path.join(config.LOG_DIR, f"fail_{self.name}_{int(time.time())}.png")
            page.screenshot(path=path, full_page=True)
            log.info("Saved failure screenshot: %s", path)
        except Exception:
            pass

    def _ensure_signed_in(self, page):
        """If redirected to a Microsoft sign-in page, wait for the user (window visible) or fail fast."""
        try:  # sign-in redirects can happen a moment after the page loads
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        if not _on_login_page(page):
            self.needs_sign_in = False
            return
        self.needs_sign_in = True
        if not self._visible():
            raise LoginRequired("Not signed in - " + ("click Show browser in the manager and sign in."
                                                       if getattr(self, "cdp_url", None) else
                                                       "run once with --show and complete sign-in."))
        log.warning("Waiting up to %d min for you to sign in to Microsoft 365 in the browser window...",
                    config.LOGIN_TIMEOUT // 60)
        if not getattr(self, "cdp_url", None):
            print(f"\n>>> Please sign in to Microsoft 365 in the browser window "
                  f"(waiting up to {config.LOGIN_TIMEOUT // 60} min)...", flush=True)
        deadline = time.time() + config.LOGIN_TIMEOUT
        while _is_login_url(page.url or "") or "cloud.microsoft" not in (page.url or ""):
            self._check_abort()
            if time.time() > deadline:
                raise LoginRequired("Sign-in was not completed in time.")
            page.wait_for_timeout(1000)
        self.needs_sign_in = False
        log.info("Sign-in complete - session saved to %s", config.PROFILE_DIR)
        page.wait_for_load_state("domcontentloaded")
        if "/chat" not in page.url:
            page.goto(config.CHAT_URL, wait_until="domcontentloaded")

    # ---------- model picker ----------
    def _read_current_model(self, page):
        try:
            b = page.evaluate(PICKER_JS, config.MODEL_PICKER_SEL)
        except Exception:
            return None
        name = picker_label(b.get("aria", ""), b.get("text", "")) if b else None
        if name:
            with self._models_lock:
                self._models["current"] = name
        return name

    def _menu(self, page, which):
        return page.evaluate(MENU_JS, [config.MODEL_MENU_SEL, config.MODEL_OPTION_SEL,
                                       config.MODEL_SUBMENU_SEL, which])

    def _wait_menu(self, page, which, timeout=None) -> list[dict] | None:
        """Options of the main menu or the open sub-menu, waiting for them to render."""
        deadline = time.time() + (timeout if timeout is not None else config.MODEL_TIMEOUT)
        while True:
            self._check_abort()
            opts = self._menu(page, which)
            if opts:
                return opts
            if time.time() > deadline:
                return None
            time.sleep(0.2)

    def _click_option(self, page, which, idx):
        page.locator(f'[data-relay-menu="{which}"] [data-relay-opt="{idx}"]').first.click(
            timeout=config.MODEL_TIMEOUT * 1000)

    def _open_model_menu(self, page) -> list[dict]:
        """Open the picker and return the main menu's options (raw); raises CopilotError if it can't."""
        self._close_menu(page)
        try:
            page.locator(config.MODEL_PICKER_SEL).first.click(timeout=config.MODEL_TIMEOUT * 1000)
        except PWTimeout:
            raise CopilotError("Could not click the model picker - check COPILOT_MODEL_PICKER_SEL.")
        opts = self._wait_menu(page, "main")
        if not opts:
            raise CopilotError("The model menu did not open or had no options - check COPILOT_MODEL_MENU_SEL / "
                               "COPILOT_MODEL_OPTION_SEL.")
        return opts

    def _open_submenu(self, page, idx) -> list[dict]:
        """Open the sub-menu of main-menu option `idx` and return its options; raises CopilotError."""
        loc = page.locator(f'[data-relay-menu="main"] [data-relay-opt="{idx}"]').first
        attempts = (lambda: loc.click(timeout=config.MODEL_TIMEOUT * 1000),
                    lambda: loc.hover(timeout=config.MODEL_TIMEOUT * 1000),
                    lambda: (loc.focus(), page.keyboard.press("ArrowRight")))
        for attempt in attempts:
            try:
                attempt()
            except Exception:
                continue
            opts = self._wait_menu(page, "sub", timeout=config.MODEL_TIMEOUT / 3)
            if opts:
                return opts
        raise CopilotError("A model sub-menu did not open - check COPILOT_MODEL_SUBMENU_SEL / "
                           "COPILOT_MODEL_MENU_SEL.")

    def _close_menu(self, page):
        try:
            for _ in range(3):
                if not page.evaluate(MENU_OPEN_JS, config.MODEL_MENU_SEL):
                    break
                page.keyboard.press("Escape")
                time.sleep(0.1)
        except Exception:
            pass

    def _refresh_models(self, page) -> list[str]:
        """Read the model list from the picker, including sub-menus. Never raises; keeps the old
        list on failure."""
        try:
            if "/chat" not in (page.url or ""):
                page.goto(config.CHAT_URL, wait_until="domcontentloaded")
            self._ensure_signed_in(page)
            page.locator(config.MODEL_PICKER_SEL).first.wait_for(state="visible",
                                                                 timeout=config.MODEL_TIMEOUT * 3000)
            current = self._read_current_model(page)
            main = self._open_model_menu(page)
            names, paths, keys, failed = [], {}, {}, []

            def add(o, path):
                n = option_name(o)
                if n and n not in paths:
                    names.append(n)
                    paths[n] = path
                    if checkmark_key(o):
                        keys[n] = checkmark_key(o)

            triggers = []
            for o in main:
                if o.get("submenu"):
                    triggers.append(option_name(o))
                else:
                    add(o, [])
            for trigger in triggers:
                try:
                    main = self._open_model_menu(page)
                    idx = next(i for i, o in enumerate(main) if option_name(o) == trigger)
                    for o in self._open_submenu(page, idx):
                        if not o.get("submenu"):
                            add(o, [trigger])
                except Exception as e:
                    failed.append(f"{trigger} ({e})")
            self._close_menu(page)
            if not names:
                raise CopilotError("The model menu had no options - check COPILOT_MODEL_OPTION_SEL.")
            with self._models_lock:
                self._models.update(names=names, paths=paths, keys=keys, fetched=time.time(), source="picker",
                                    current=current or self._models["current"])
            log.info("Models in the picker: %s (selected: %s)", ", ".join(
                f"{' > '.join(paths[n] + [n])}" for n in names), current)
            if failed:
                log.warning("Could not read these model sub-menus: %s", "; ".join(failed))
            return names
        except Exception as e:
            self._close_menu(page)
            with self._models_lock:
                self._models["fetched"] = time.time()   # don't retry on every request
                names = list(self._models["names"])
            log.warning("Could not read the model list from the picker (%s: %s). %s", type(e).__name__, e,
                        "Keeping the previous list." if names else
                        "Only 'm365-copilot' (current selection) will be listed; set COPILOT_MODELS to list "
                        "models manually.")
            return names

    def _find_option(self, page, name, path):
        """Open the menus along `path` and return (which menu, index, option) for `name`, or None.
        Without a known path, searches the main menu and then every sub-menu."""
        main = self._open_model_menu(page)
        lname = name.lower()
        if not path:
            for i, o in enumerate(main):
                if not o.get("submenu") and option_name(o).lower() == lname:
                    return "main", i, o
            if path is not None:           # known top-level model that isn't there
                return None
        triggers = [option_name(o) for o in main if o.get("submenu")] if not path else path[:1]
        for trigger in triggers:
            main = self._open_model_menu(page)
            idx = next((i for i, o in enumerate(main) if option_name(o).lower() == trigger.lower()), None)
            if idx is None:
                continue
            sub = self._open_submenu(page, idx)
            for i, o in enumerate(sub):
                if option_name(o).lower() == lname:
                    return "sub", i, o
        return None

    def _is_selected(self, page, name, path) -> bool:
        """Whether `name` is the selected model: the picker button shows its name, or (when the
        button shows a shorter label) its option is checked in the menu."""
        label = self._read_current_model(page)
        if not label or normalize(label) not in normalize(name):
            return False
        with self._models_lock:
            others = [n for n in self._models["names"] if n.lower() != name.lower()]
        ambiguous = any(normalize(label) in normalize(o) for o in others)
        if label.lower() == name.lower() and not ambiguous:
            return True
        # The label could mean several models (e.g. "Think deeper" for a sub-menu model): check the menu.
        try:
            found = self._find_option(page, name, path)
            return bool(found and found[2].get("checked"))
        finally:
            self._close_menu(page)

    def _select_model(self, page, name: str):
        """Select `name` in the picker (opening its sub-menu if needed) unless it's already selected.
        Raises CopilotError if it can't."""
        with self._models_lock:
            path = self._models["paths"].get(name)
            if path is None:   # model list from COPILOT_MODELS or stale: search all menus
                path = next((p for n, p in self._models["paths"].items() if n.lower() == name.lower()), None)
        before = self._read_current_model(page)
        if self._is_selected(page, name, path):
            return
        found = self._find_option(page, name, path)
        if not found:
            self._close_menu(page)
            with self._models_lock:
                listed = ", ".join(self._models["names"]) or "none found"
            raise CopilotError(f"Model {name!r} is not in the model picker (listed: {listed}).")
        which, idx, opt = found
        if opt.get("disabled"):
            self._close_menu(page)
            raise CopilotError(f"Model {name!r} is disabled in the model picker.")
        self._click_option(page, which, idx)
        deadline = time.time() + config.MODEL_TIMEOUT
        while time.time() < deadline:
            label = self._read_current_model(page)
            if label and label.lower() == name.lower():
                break
            if label and label != before and normalize(label) in normalize(name):
                break                       # button shows a shorter label for this model
            time.sleep(0.2)
        self._close_menu(page)
        if self._is_selected(page, name, path):
            log.info("Selected model %r (was %r)", name, before)
            return
        raise CopilotError(f"Clicked model {name!r} but the picker still shows {self._read_current_model(page)!r}.")

    # ---------- chats ----------
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
            self._check_abort()
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

    def _ask(self, page, prompt: str, fut: Future, on_delta=None, resume=None, model=None) -> str:
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

        if model:
            self._select_model(page, model)

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
            self._check_abort()
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
        fut.model = model or self._read_current_model(page)
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
            stem = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + f"-{self.name}")
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
            self._check_abort()
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


def get_copilot_response(prompt_text, show_window=False, model=None):
    """Backwards-compatible one-shot helper (starts and stops a browser)."""
    with CopilotSession(show_window=show_window, discover_models=False) as s:
        return s.ask(prompt_text, model=model)
