"""Several independent Copilot endpoints.

v16 deliberately DOES NOT use several tabs in one Edge. Each session gets:
  * its own normal Edge process
  * its own copied profile directory under SESSION_PROFILE_ROOT
  * one Playwright CDP connection
  * one API port

This costs more memory than tabs, but avoids repeated connect_over_cdp() calls attaching to every
existing tab. That was the source of unreliable second/third-session startup.
"""
import argparse
import atexit
import logging
import os
import shutil
import threading
import time

import config
import procs
import server
from conversations import ConversationStore
from copilot import BrowserHost, CopilotSession, SessionCancelled

log = logging.getLogger("manager")
_PROFILE_LOCK = threading.Lock()
_LOCK_NAMES = {"SingletonCookie", "SingletonLock", "SingletonSocket", "DevToolsActivePort"}


def next_port(instances):
    return max((i.port for i in instances), default=config.PORT - 1) + 1


def session_profile(port):
    return os.path.abspath(os.path.join(config.SESSION_PROFILE_ROOT, f"session-{port}"))


def seed_profile(port):
    """Create a persistent session profile from the signed-in base profile, once.

    Existing session profiles are reused, so each keeps its own refreshed sign-in. Runtime lock,
    cache and crash files are excluded. The base profile must not be in use while first copies are
    made; manager.py never opens it.
    """
    dst = session_profile(port)
    if os.path.isdir(dst):
        return dst
    src = os.path.abspath(config.PROFILE_DIR)
    with _PROFILE_LOCK:
        if os.path.isdir(dst):
            return dst
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            return dst
        tmp = dst + ".copying"
        shutil.rmtree(tmp, ignore_errors=True)

        def ignore(path, names):
            ignored = set()
            for n in names:
                low = n.lower()
                if n in _LOCK_NAMES or low in {"lockfile", "runningchromeversion"}:
                    ignored.add(n)
                elif low in {"cache", "code cache", "gpucache", "dawncache", "grshadercache",
                             "shadercache", "browsermetrics", "crashpad"}:
                    ignored.add(n)
            return ignored

        shutil.copytree(src, tmp, ignore=ignore)
        os.replace(tmp, dst)
        log.info("Created isolated browser profile for port %d at %s", port, dst)
    return dst


class Instance:
    def __init__(self, number, port, show=False):
        self.number, self.port, self.show = number, port, show
        self.state, self.error, self.started_at = "off", "", 0.0
        self.host = self.session = self.httpd = None
        self.removed = False
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    @property
    def url(self):
        return f"http://{config.HOST}:{self.port}/v1"

    @property
    def profile(self):
        return session_profile(self.port)

    def start(self, store, wait=False):
        if self.removed or self.state in ("starting", "on", "stopping"):
            return
        self.state, self.error, self.started_at = "starting", "", time.time()
        self._cancel = threading.Event()
        t = threading.Thread(target=self._start, args=(store,), daemon=True, name=f"start-{self.port}")
        t.start()
        if wait:
            t.join()

    def _start(self, store):
        with self._lock:
            httpd = host = session = None
            try:
                httpd = server.make_server(self.port, None, store, serve=False)
                if self._cancel.is_set():
                    raise SessionCancelled("Start cancelled")
                profile = seed_profile(self.port)
                if self._cancel.is_set():
                    raise SessionCancelled("Start cancelled")
                host = BrowserHost(show_window=self.show, profile_dir=profile)
                self.host = host
                host.wait_ready(self._cancel)
                session = CopilotSession(show_window=host.visible, cdp_url=host.cdp_url,
                                         name=str(self.port), cancel=self._cancel,
                                         is_visible=lambda: host.visible, reuse_existing=True)
                if self._cancel.is_set():
                    raise SessionCancelled("Start cancelled")
                httpd.RequestHandlerClass.session = session
                server.serve_in_background(httpd)
                self.httpd, self.session, self.state = httpd, session, "on"
                log.info("Session %d on %s (isolated Edge PID %s, profile %s)",
                         self.number, self.url, host.pid, profile)
                return
            except SessionCancelled:
                self.error = ""
                self.state = "removed" if self.removed else "off"
            except OSError as e:
                self.error = f"port {self.port} unavailable ({e.strerror or e})"
                self.state = "error"
            except Exception as e:
                self.error = str(e)
                self.state = "error"
                log.exception("Session %d could not start", self.number)
            finally:
                if self.state != "on":
                    if session:
                        session.abort()
                    if httpd:
                        httpd.server_close()
                    if host:
                        host.stop(final=True)
                    self.host = self.session = self.httpd = None

    def cancel_start(self):
        self._cancel.set()

    def stop(self, force=False, wait=False):
        if self.state == "starting":
            self._cancel.set()
            return
        if self.state not in ("on", "error"):
            return
        self.state = "stopping"
        t = threading.Thread(target=self._stop, args=(force,), daemon=True, name=f"stop-{self.port}")
        t.start()
        if wait:
            t.join()

    def _stop(self, force):
        with self._lock:
            try:
                if self.httpd:
                    self.httpd.shutdown(); self.httpd.server_close()
                if self.session:
                    if force or not self.session.close(timeout=config.STOP_TIMEOUT):
                        self.session.abort()
                if self.host:
                    self.host.stop(final=True)
            finally:
                self.httpd = self.session = self.host = None
                self.state = "removed" if self.removed else "off"

    def remove(self):
        self.removed = True
        self._cancel.set()
        self.stop(force=True)
        if self.state not in ("starting", "stopping"):
            self.state = "removed"

    def set_visible(self, store):
        """Switch between a visible Edge and true headless Edge.

        Windows keeps normal off-screen windows on the taskbar. Edge cannot change between headed
        and headless after launch, so Show/Hide performs a controlled restart of this session only.
        The API port is briefly unavailable while its browser restarts.
        """
        if self.state != "on":
            return
        self.show = not bool(self.host and self.host.visible)
        self.state = "stopping"

        def restart():
            self._stop(force=False)
            if not self.removed:
                self.start(store)
        threading.Thread(target=restart, daemon=True, name=f"display-{self.port}").start()

    def status_text(self):
        if self.state == "starting":
            return f"Starting isolated Edge… {int(time.time()-self.started_at)}s (switch off to cancel)"
        if self.state == "on":
            n = self.session.pending if self.session else 0
            pid = self.host.pid if self.host else None
            text = f"On · Edge PID {pid}" if pid else "On"
            if n: text += f" · {n} queued"
            # BrowserHost re-checks its page periodically. CopilotSession.needs_sign_in is only
            # updated when automation calls _ensure_signed_in(), so it can remain True after the
            # user completes a manual sign-in. The host is the live source of truth for the row.
            if self.host and self.host.needs_sign_in: text += " · not signed in"
            return text
        if self.state == "error": return "Error: " + self.error
        return {"off":"Off", "stopping":"Stopping…", "removed":"Removed"}[self.state]


def run_window(store, show=False):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk(); root.title("Copilot sessions · isolated browsers")
    instances, rows, counter = [], {}, [0]
    closing = threading.Event()

    info = tk.Label(root, text=("Each session uses its own Edge process and profile. Hidden sessions run in true "
                                "headless mode, so they do not appear on the Windows taskbar."),
                    anchor="w", justify="left", padx=10, pady=8)
    info.pack(fill="x")
    table = tk.Frame(root, padx=10); table.pack(fill="both", expand=True)
    for c, text in enumerate(("Session", "Port", "Endpoint", "On", "Browser", "Status", "")):
        tk.Label(table, text=text, font=("Segoe UI",9,"bold"), anchor="w").grid(row=0,column=c,sticky="w",padx=5)
    table.columnconfigure(5, weight=1)

    def refresh():
        if closing.is_set(): return
        for inst, r in list(rows.items()):
            r["status"].configure(text=inst.status_text(), fg="#b10e1c" if inst.state=="error" else "black")
            r["toggle"].configure(text="On" if inst.state in ("on","starting") else "Off",
                                  state="disabled" if inst.state=="stopping" else "normal")
            visible = bool(inst.host and inst.host.visible)
            r["browser"].configure(text="Hide & restart" if visible else "Show & restart",
                                   state="normal" if inst.state == "on" else "disabled")
        root.after(500, refresh)

    def toggle(inst):
        if inst.state in ("on","starting"): inst.stop()
        else: inst.start(store)

    def remove(inst):
        inst.remove()
        for w in rows.pop(inst).values(): w.destroy()
        instances.remove(inst)

    def add():
        counter[0]+=1
        inst=Instance(counter[0], next_port(instances), show=show); instances.append(inst)
        row=counter[0]
        widgets={
            "name":tk.Label(table,text=f"Session {inst.number}"),
            "port":tk.Label(table,text=str(inst.port)),
            "url":ttk.Entry(table,width=28),
            "toggle":ttk.Button(table,text="Off",width=5,command=lambda:toggle(inst)),
            "browser":ttk.Button(table,text="Show & restart",width=14,
                                       command=lambda:inst.set_visible(store)),
            "status":tk.Label(table,text="Off",anchor="w"),
            "remove":ttk.Button(table,text="Remove",command=lambda:remove(inst)),
        }
        widgets["url"].insert(0,inst.url); widgets["url"].configure(state="readonly")
        for c,w in enumerate(widgets.values()): w.grid(row=row,column=c,sticky="w",padx=5,pady=3)
        rows[inst]=widgets
        inst.start(store)

    bottom=tk.Frame(root,padx=10,pady=10); bottom.pack(fill="x")
    ttk.Button(bottom,text="Add",command=add).pack(side="left")
    tk.Label(bottom,text=f"Profiles: {os.path.abspath(config.SESSION_PROFILE_ROOT)}",fg="#666").pack(side="left",padx=10)

    def close():
        if closing.is_set(): return
        closing.set(); root.title("Copilot sessions · closing…")
        def work():
            for i in list(instances): i.remove()
            while any(i.state in ("starting","stopping") for i in instances): time.sleep(.2)
            if store: store.close()
            root.after(0,root.destroy)
        threading.Thread(target=work,daemon=True).start()
    root.protocol("WM_DELETE_WINDOW",close)
    add(); refresh(); root.minsize(760,0)
    try: root.mainloop()
    except KeyboardInterrupt:
        closing.set()
        for i in list(instances): i.remove()


def main():
    ap=argparse.ArgumentParser(description="Run independent Copilot API sessions")
    ap.add_argument("--show",action="store_true",help="show each Edge window")
    args=ap.parse_args(); server.setup_logging()
    if config.KILL_ON_EXIT: procs.kill_children_on_exit()
    store=ConversationStore() if config.RESUME_CONVERSATIONS else None
    run_window(store,args.show)

if __name__=="__main__": main()
