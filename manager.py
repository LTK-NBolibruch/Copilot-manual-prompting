"""A small window for running several Copilot API sessions at once.

    python manager.py           # browser off-screen (bring it back with "Show browser")
    python manager.py --show    # browser visible (one window, one tab per session)

The manager starts a NORMAL Edge (not one launched by Playwright - Copilot only loads one chat
at a time in that) with the saved profile, and each session attaches to it in its own tab.

Each row is a session: its own API endpoint (http://127.0.0.1:<port>/v1).
  Add            new session on the next port (5000, 5001, 5002, ...), switched on
  switch         on = tab open and port answering; off = port closed and tab closed
                 clicking it while a session is starting cancels the start
  Remove         stops the session right away (a reply in progress is aborted) and removes the row
  Show browser   brings the browser window on-screen, e.g. to sign in; Hide puts it back
  Retry          restarts the browser after an error
  Processes      lists the Edge processes using the profile and orphaned Playwright drivers;
                 leftovers from crashed runs can be ended there
Before starting Edge, leftover Edge processes on the profile are detected and shown in a bar at
the top, with a button to end them (see COPILOT_STALE_EDGE).

Sessions share conversations.db, so a conversation can continue on any port;
two sessions never continue the same Copilot chat at the same time.

Stop server.py and ask.py first: they use the same browser profile.
"""
import argparse
import atexit
import logging
import threading
import time

import config
import procs
import server
from conversations import ConversationStore
from copilot import BrowserHost, CopilotSession, SessionCancelled

log = logging.getLogger("manager")


def next_port(instances) -> int:
    """Port for a new session: one past the highest so far, starting at config.PORT."""
    return max((i.port for i in instances), default=config.PORT - 1) + 1


class Instance:
    """One session: an API server on `port` plus a tab in the shared browser.

    state: off / starting / on / stopping / error / removed. start(), cancel_start(), stop() and
    remove() return immediately; the work happens in background threads.
    """

    def __init__(self, number, port):
        self.number, self.port = number, port
        self.state, self.error = "off", ""
        self.session = None
        self.httpd = None
        self.host = None
        self.started_at = 0.0
        self.removed = False
        self._cancel = threading.Event()
        self._lock = threading.Lock()       # one start/stop at a time
        self._swap = threading.Lock()       # guards session/httpd handover between threads

    @property
    def url(self):
        return f"http://{config.HOST}:{self.port}/v1"

    @property
    def busy(self):
        return self.state in ("starting", "stopping")

    # ---------- actions
    def start(self, host, store, wait=False):
        if self.removed or self.state in ("starting", "stopping", "on"):
            return
        self.host = host
        self.state, self.error, self.started_at = "starting", "", time.time()
        self._cancel = threading.Event()
        t = threading.Thread(target=self._start, args=(host, store, self._cancel), daemon=True,
                             name=f"start-{self.port}")
        t.start()
        if wait:
            t.join()

    def cancel_start(self):
        if self.state == "starting":
            self._cancel.set()

    def stop(self, wait=False):
        """Switch off: stop taking requests, let a reply in progress finish (up to STOP_TIMEOUT), close the tab."""
        if self.state == "starting":
            return self.cancel_start()
        if self.state != "on":
            return
        self.state = "stopping"
        t = threading.Thread(target=self._stop, daemon=True, name=f"stop-{self.port}")
        t.start()
        if wait:
            t.join()

    def remove(self, wait=False):
        """Stop right away (abort a reply in progress, cancel a start) and mark the row removed."""
        self.removed = True
        self._cancel.set()
        t = threading.Thread(target=self._force_stop, daemon=True, name=f"remove-{self.port}")
        t.start()
        if wait:
            t.join()

    # ---------- background work
    def _start(self, host, store, cancel):
        with self._lock:
            if cancel.is_set():
                self._ended("off")
                return
            try:
                httpd = server.make_server(self.port, None, store, serve=False)   # claim the port first
            except OSError as e:
                self._ended("error", f"port {self.port} unavailable ({e.strerror or e})")
                return
            try:
                host.wait_ready(cancel)
                session = CopilotSession(show_window=host.visible, cdp_url=host.cdp_url, name=str(self.port),
                                         cancel=cancel, is_visible=lambda: host.visible)
            except SessionCancelled:
                httpd.server_close()
                log.info("Session %d (port %d): start cancelled", self.number, self.port)
                self._ended("off")
                return
            except Exception as e:
                httpd.server_close()
                log.error("Session %d (port %d) could not start: %s", self.number, self.port, e)
                self._ended("error", str(e))
                return
            with self._swap:
                if cancel.is_set():                 # cancelled/removed just as the tab opened
                    session.abort()
                    httpd.server_close()
                    self._ended("off")
                    return
                httpd.RequestHandlerClass.session = session
                server.serve_in_background(httpd)
                self.session, self.httpd = session, httpd
                self.state = "on"
            log.info("Session %d on %s", self.number, self.url)

    def _ended(self, state, error=""):
        self.state, self.error = ("removed", "") if self.removed else (state, error)

    def _take(self):
        with self._swap:
            httpd, session = self.httpd, self.session
            self.httpd = self.session = None
        return httpd, session

    def _stop(self):
        with self._lock:
            try:
                httpd, session = self._take()
                if httpd:
                    httpd.shutdown()          # stop taking requests
                    httpd.server_close()
                if session and not session.close(timeout=config.STOP_TIMEOUT):
                    log.warning("Session %d (port %d): reply still running after %ds - aborting it",
                                self.number, self.port, config.STOP_TIMEOUT)
                    session.abort()
            except Exception:
                log.exception("Session %d (port %d): error while stopping", self.number, self.port)
            finally:                          # never leave the row stuck on "Stopping..."
                self._ended("off")
            log.info("Session %d (port %d) stopped", self.number, self.port)

    def _force_stop(self):
        # No self._lock here: a start in progress sees the cancel flag and cleans up after itself.
        try:
            httpd, session = self._take()
            if httpd:
                httpd.shutdown()
                httpd.server_close()
            if session:
                session.abort()
        except Exception:
            log.exception("Session %d (port %d): error while removing", self.number, self.port)
        finally:
            if self.state != "starting":
                self.state = "removed"
        log.info("Session %d (port %d) removed", self.number, self.port)

    # ---------- display
    def status_text(self):
        if self.state == "on":
            s = self.session
            n = s.pending if s else 0
            text = "On" + (f" ({n} queued)" if n else "")
            if s is not None and getattr(s, "needs_sign_in", False):
                text += " - not signed in"
            if s is not None and not s.alive:
                text = "Tab closed - switch off and on again"
            return text
        if self.state == "starting":
            h = self.host
            if h is not None and (getattr(h, "blocked", None) is not None or getattr(h, "busy", "")):
                return "Waiting for the browser - see the bar above  (click the switch to cancel)"
            return f"Starting… {int(time.time() - self.started_at)}s  (click the switch to cancel)"
        if self.state == "error":
            return "Error: " + self.error
        return {"off": "Off", "stopping": "Stopping…", "removed": "Removed"}[self.state]


def browser_text(host) -> tuple:
    """(text, is_problem) for the top line of the window."""
    if host.error:
        return f"Browser: {host.error}", True
    if host.blocked is not None:
        return "Browser: not started yet - see below", True
    if not host.ready.is_set():
        return "Browser: starting…", False
    if host.needs_sign_in:
        return "Browser: not signed in - click Show browser and sign in", True
    where = "visible" if host.visible else ("headless" if host.headless else "off-screen")
    text = f"Browser: ready ({where})"
    pid, scan = host.pid, host.scan
    if pid:
        n = len(scan.ours.pids) if scan is not None and scan.ours and scan.ours.pid == pid else None
        text += f" · Edge PID {pid}" + (f", {n} processes" if n else "")
    return text, False


def warning_bar(host):
    """(text, button label or None, action or None) for the bar under the top line; text '' = hide."""
    if host.busy:
        return host.busy, None, None
    if host.blocked is not None:
        return (f"Edge isn't started yet: {host.blocked.describe(host.blocked.blocking + host.blocked.of('driver'))}. "
                "They may be left over from a crash, and a new Edge can't use the profile while they run.",
                "End them and start", host.end_stale_and_start)
    scan = host.scan
    if scan is not None and scan.stale and host.ready.is_set():
        groups = scan.stale
        return (f"Found {scan.describe(groups)} using the profile.", "End them",
                lambda: host.end_stale(pids=[g.pid for g in groups]))
    return "", None, None


# ---------------------------------------------------------------- window (tkinter)
def run_window(host, store):
    import tkinter as tk
    from tkinter import ttk

    class Toggle(tk.Canvas):
        """A small on/off switch."""
        W, H = 40, 22

        def __init__(self, master, command):
            super().__init__(master, width=self.W, height=self.H, highlightthickness=0, bd=0, bg=master["bg"],
                             cursor="hand2")
            self.on, self.enabled, self.command = False, True, command
            self.bind("<Button-1>", lambda _e: self.enabled and self.command(not self.on))
            self.draw()

        def set(self, on, enabled=True):
            if (on, enabled) != (self.on, self.enabled):
                self.on, self.enabled = on, enabled
                self.configure(cursor="hand2" if enabled else "watch")
                self.draw()

        def draw(self):
            self.delete("all")
            w, h, r = self.W, self.H, self.H // 2
            track = ("#0f6cbd" if self.on else "#8a8a8a") if self.enabled else "#c8c8c8"
            self.create_oval(1, 1, h - 1, h - 1, fill=track, outline=track)
            self.create_oval(w - h + 1, 1, w - 1, h - 1, fill=track, outline=track)
            self.create_rectangle(r, 1, w - r, h - 1, fill=track, outline=track)
            x = w - r if self.on else r
            self.create_oval(x - r + 4, 4, x + r - 4, h - 4, fill="white", outline="white")

    root = tk.Tk()
    root.title("Copilot sessions")
    bg = root["bg"]
    instances, rows = [], {}
    counter = [0]
    closing = threading.Event()
    dialog = {"win": None}

    top = tk.Frame(root, bg=bg, padx=10, pady=8)
    top.pack(fill="x")
    browser_label = tk.Label(top, text="Browser: starting…", bg=bg, anchor="w", justify="left", wraplength=520)
    browser_label.pack(side="left")
    retry_button = ttk.Button(top, text="Retry", command=lambda: host.restart())
    show_button = ttk.Button(top, text="Show browser", command=lambda: host.set_visible(not host.visible))
    procs_button = ttk.Button(top, text="Processes…", command=lambda: open_processes())
    procs_button.pack(side="right")
    show_button.pack(side="right", padx=(0, 6))

    warn = tk.Frame(root, bg="#fff4ce", padx=10, pady=6)
    warn_label = tk.Label(warn, text="", bg="#fff4ce", anchor="w", justify="left", wraplength=480)
    warn_label.pack(side="left", fill="x", expand=True)
    warn_button = ttk.Button(warn, text="")
    warn_shown = [False]

    table = tk.Frame(root, bg=bg, padx=10)
    table.pack(fill="both", expand=True)
    for col, text in enumerate(("Session", "Port", "Endpoint", "On", "Status", "")):
        tk.Label(table, text=text, bg=bg, font=("Segoe UI", 9, "bold"), anchor="w").grid(
            row=0, column=col, sticky="w", padx=6, pady=(0, 4))
    table.columnconfigure(4, weight=1)

    def toggle(inst, on):
        if closing.is_set():
            return
        if on:
            inst.start(host, store)
        elif inst.state == "starting":
            inst.cancel_start()
        else:
            inst.stop()
        refresh_row(inst)

    def remove(inst):
        if closing.is_set():
            return
        inst.remove()
        for w in rows.pop(inst)["widgets"]:
            w.destroy()
        instances.remove(inst)

    def add():
        if closing.is_set():
            return
        counter[0] += 1
        inst = Instance(counter[0], next_port(instances))
        instances.append(inst)
        r = counter[0]          # grid row; rows are never reused
        url = tk.StringVar(value=inst.url)
        switch = Toggle(table, lambda on, i=inst: toggle(i, on))
        status = tk.Label(table, text="", bg=bg, anchor="w")
        widgets = [
            tk.Label(table, text=f"Session {inst.number}", bg=bg, anchor="w"),
            tk.Label(table, text=str(inst.port), bg=bg, anchor="w"),
            ttk.Entry(table, textvariable=url, state="readonly", width=28),
            switch, status,
            ttk.Button(table, text="Remove", width=8, command=lambda i=inst: remove(i)),
        ]
        for col, w in enumerate(widgets):
            w.grid(row=r, column=col, sticky="w", padx=6, pady=3)
        rows[inst] = {"switch": switch, "status": status, "widgets": widgets, "url": url}
        toggle(inst, True)

    bottom = tk.Frame(root, bg=bg, padx=10, pady=10)
    bottom.pack(fill="x")
    add_button = ttk.Button(bottom, text="Add", command=add)
    add_button.pack(side="left")
    tk.Label(bottom, text="Each session is its own endpoint and browser tab.", bg=bg, fg="#666").pack(
        side="left", padx=10)

    # ---------- processes dialog
    def open_processes():
        if dialog["win"] is not None:
            try:
                dialog["win"].lift()
                return
            except tk.TclError:
                dialog["win"] = None
        win = tk.Toplevel(root)
        win.title("Browser processes")
        dialog["win"] = win
        tk.Label(win, text=f"Edge processes using the profile {procs.norm_path(config.PROFILE_DIR)}, "
                           "and orphaned Playwright drivers. Your everyday Edge isn't listed or touched.",
                 anchor="w", justify="left", wraplength=560, padx=10, pady=8).pack(fill="x")
        cols = ("pid", "what", "count", "owner")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=8, selectmode="extended")
        for c, title, width in zip(cols, ("PID", "What", "Processes", "Started by"), (70, 260, 80, 180)):
            tree.heading(c, text=title)
            tree.column(c, width=width, anchor="w")
        tree.pack(fill="both", expand=True, padx=10)
        note = tk.Label(win, text="", anchor="w", fg="#666", padx=10)
        note.pack(fill="x")
        buttons = tk.Frame(win, padx=10, pady=8)
        buttons.pack(fill="x")
        kinds = {}

        def fill():
            if dialog["win"] is not win:
                return
            scan = host.scan
            tree.delete(*tree.get_children())
            kinds.clear()
            if scan is None:
                note.configure(text="Scanning…")
            elif scan.error:
                note.configure(text=f"Could not list processes: {scan.error}")
            else:
                for g in scan.groups:
                    kinds[str(g.pid)] = g.kind
                    tree.insert("", "end", iid=str(g.pid),
                                values=(g.pid, g.what, len(g.pids), g.owner or ("this manager" if g.kind == "ours"
                                                                                else "(no longer running)")))
                ago = int(time.time() - scan.when)
                note.configure(text=(host.busy or ("Nothing else is using the profile." if not scan.stale and
                                                   not scan.blocking else "")) + f"   Checked {ago}s ago.")
            win.after(1000, fill)

        def end_selected():
            sel = [i for i in tree.selection() if kinds.get(i) != "ours"]
            if not sel:
                note.configure(text="Select leftover processes to end. To restart this manager's Edge, use Retry.")
                return
            host.end_stale(pids=[int(i) for i in sel])

        ttk.Button(buttons, text="Refresh", command=host.rescan).pack(side="left")
        ttk.Button(buttons, text="End selected", command=end_selected).pack(side="left", padx=6)
        ttk.Button(buttons, text="End all leftovers",
                   command=lambda: host.end_stale(pids=[g.pid for g in (host.scan.stale if host.scan else [])])
                   ).pack(side="left")

        def close_dialog():
            dialog["win"] = None
            win.destroy()
        ttk.Button(buttons, text="Close", command=close_dialog).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", close_dialog)
        host.rescan()
        fill()

    # ---------- refresh
    def refresh_row(inst):
        w = rows.get(inst)
        if not w:
            return
        w["switch"].set(inst.state in ("on", "starting"), enabled=inst.state != "stopping")
        text = inst.status_text()
        bad = inst.state == "error" or "not signed in" in text or "closed" in text
        w["status"].configure(text=text, fg="#b10e1c" if bad else "black")

    def refresh():
        if closing.is_set():
            return
        text, bad = browser_text(host)
        browser_label.configure(text=text, fg="#b10e1c" if bad else "black")
        if host.error:
            retry_button.pack(side="right", padx=(0, 6))
        else:
            retry_button.pack_forget()
        show_button.configure(text="Hide browser" if host.visible else "Show browser",
                              state="normal" if host.can_show else "disabled")
        wtext, wlabel, waction = warning_bar(host)
        if wtext:
            warn_label.configure(text=wtext)
            if wlabel:
                warn_button.configure(text=wlabel, command=waction)
                warn_button.pack(side="right", padx=(8, 0))
            else:
                warn_button.pack_forget()
            if not warn_shown[0]:
                warn.pack(fill="x", after=top)
                warn_shown[0] = True
        elif warn_shown[0]:
            warn.pack_forget()
            warn_shown[0] = False
        for inst in instances:
            refresh_row(inst)
        root.after(500, refresh)

    def shutdown(graceful=True):
        for inst in list(instances):
            inst.stop() if graceful else inst.remove()
            inst.cancel_start()
        for inst in list(instances):
            while inst.state in ("starting", "stopping"):
                time.sleep(0.2)
        host.stop(final=True)
        if store is not None:
            store.close()

    def close():
        if closing.is_set():
            return
        closing.set()
        root.title("Copilot sessions - closing…")
        add_button.configure(state="disabled")
        browser_label.configure(text="Closing: finishing replies in progress…", fg="black")

        def run():
            shutdown(graceful=True)
            root.after(0, root.destroy)
        threading.Thread(target=run, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", close)
    add()                       # first session on config.PORT
    refresh()
    root.minsize(680, 0)
    try:
        root.mainloop()
    except KeyboardInterrupt:   # Ctrl+C in the console: stop everything quickly and cleanly
        log.info("Ctrl+C - stopping all sessions and the browser")
        closing.set()
        shutdown(graceful=False)


def main():
    ap = argparse.ArgumentParser(description="Run several Copilot API sessions, one browser tab each")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    args = ap.parse_args()
    server.setup_logging()
    if config.KILL_ON_EXIT:
        procs.kill_children_on_exit()
    store = ConversationStore() if config.RESUME_CONVERSATIONS else None
    host = BrowserHost(show_window=args.show)
    atexit.register(lambda: host.stop(5, final=True))     # never leave Edge running on the profile
    run_window(host, store)


if __name__ == "__main__":
    main()
