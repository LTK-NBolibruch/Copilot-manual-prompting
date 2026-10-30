"""Several sessions at once (manager.py): ports, on/off, concurrency, shared conversations.
The browser and tabs are simulated; the window itself (tkinter) is not tested here."""
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import manager  # noqa: E402
import procs  # noqa: E402
import server  # noqa: E402
from conversations import ConversationStore  # noqa: E402
from copilot import SessionCancelled  # noqa: E402
from test_server import SYSTEM, FakeSession  # noqa: E402


class FakeHost:
    cdp_url, error, visible, headless, needs_sign_in = "http://127.0.0.1:1", None, False, False, False
    blocked, busy, scan, pid = None, "", None, None

    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()
        self.stopped = False
        self.ended = []

    can_show = property(lambda self: self.ready.is_set() and not self.error)

    def wait_ready(self, cancel=None, timeout=None):
        while not self.ready.wait(0.05):
            if cancel is not None and cancel.is_set():
                raise SessionCancelled("cancelled")
        if self.error:
            raise self.error

    def set_visible(self, v):
        self.visible = v

    def restart(self):
        self.error = None

    def rescan(self):
        pass

    def end_stale(self, pids=None):
        self.ended.append(pids)

    def end_stale_and_start(self):
        self.blocked = None
        self.ready.set()

    def stop(self, timeout=15, final=False):
        self.stopped = True


def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


@pytest.fixture()
def env(tmp_path, monkeypatch):
    chats = {}                                   # one Copilot account: chats visible to every tab
    made = []

    def fake_session(show_window=False, cdp_url=None, name=None, cancel=None, **kw):
        s = FakeSession(chats=chats, delay=0.4)
        s.name = name
        made.append(s)
        return s
    monkeypatch.setattr(manager, "CopilotSession", fake_session)
    monkeypatch.setattr(config, "STRICT_MODELS", False)
    store = ConversationStore(str(tmp_path / "c.db"))
    yield FakeHost(), store, made, chats
    store.close()


def post(port, messages, out=None, key=None):
    data = json.dumps({"model": "m365-copilot", "messages": messages}).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data,
                               {"Content-Type": "application/json"})
    with urllib.request.urlopen(r) as resp:
        text = json.load(resp)["choices"][0]["message"]["content"]
    if out is not None:
        out[key] = text
    return text


def test_next_port_increments(monkeypatch):
    monkeypatch.setattr(config, "PORT", 5000)
    assert manager.next_port([]) == 5000
    a, b = manager.Instance(1, 5000), manager.Instance(2, 5001)
    assert manager.next_port([a]) == 5001
    assert manager.next_port([a, b]) == 5002
    assert manager.next_port([b, a]) == 5002


def test_start_stop_toggle(env):
    host, store, made, _ = env
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store, wait=True)
    assert inst.state == "on" and inst.status_text() == "On" and made[0].name == str(port)
    assert post(port, [{"role": "user", "content": "hi"}]).endswith(".")
    inst.stop(wait=True)
    assert inst.state == "off" and not made[0].alive
    with pytest.raises(OSError):
        post(port, [{"role": "user", "content": "hi"}])
    inst.start(host, store, wait=True)            # switch back on: a new tab
    assert inst.state == "on" and len(made) == 2
    assert post(port, [{"role": "user", "content": "hi"}])
    inst.stop(wait=True)


def test_port_in_use_shows_error(env):
    host, store, made, _ = env
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    inst = manager.Instance(1, blocker.getsockname()[1])
    inst.start(host, store, wait=True)
    assert inst.state == "error" and "unavailable" in inst.status_text() and not made
    blocker.close()
    inst.start(host, store, wait=True)            # can be switched on again once the port is free
    assert inst.state == "on"
    inst.stop(wait=True)


def test_browser_failure_shows_error(env):
    host, store, made, _ = env
    host.error = RuntimeError("Could not start browser: profile in use")
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store, wait=True)
    assert inst.state == "error" and "profile in use" in inst.error
    s = socket.socket()
    s.bind(("127.0.0.1", port))                   # port was released again
    s.close()


def test_sessions_answer_concurrently(env):
    host, store, made, _ = env
    insts = [manager.Instance(i + 1, p) for i, p in enumerate(free_ports(3))]
    for i in insts:
        i.start(host, store, wait=True)
    out, t0 = {}, time.time()
    threads = [threading.Thread(target=post, args=(i.port, [{"role": "user", "content": f"hi {i.port}"}], out,
                                                   i.port)) for i in insts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    assert len(out) == 3 and elapsed < 1.0, elapsed       # 3 x 0.4 s replies in parallel, not 1.2 s+
    for i in insts:
        i.stop(wait=True)


def test_conversation_continues_on_another_port(env):
    host, store, made, chats = env
    a, b = (manager.Instance(i + 1, p) for i, p in enumerate(free_ports(2)))
    a.start(host, store, wait=True)
    b.start(host, store, wait=True)
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = post(a.port, msgs)
    msgs += [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    post(b.port, msgs)                            # shared store: session b resumes a's chat
    assert made[1].sent_prompt == "Q2" and len(chats) == 1
    a.stop(wait=True)
    b.stop(wait=True)


def test_same_chat_never_continued_twice_at_once(env):
    host, store, made, chats = env
    a, b = (manager.Instance(i + 1, p) for i, p in enumerate(free_ports(2)))
    a.start(host, store, wait=True)
    b.start(host, store, wait=True)
    msgs = [SYSTEM, {"role": "user", "content": "Q1"}]
    a1 = post(a.port, msgs)
    follow = msgs + [{"role": "assistant", "content": a1}, {"role": "user", "content": "Q2"}]
    out = {}
    threads = [threading.Thread(target=post, args=(p, follow, out, p)) for p in (a.port, b.port)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    resumed = [s for s in made if s.last_resume is not None]
    assert len(resumed) == 1                      # only one request continued the chat
    assert len(chats) == 2                        # the other started a new one
    assert store._busy == set()
    a.stop(wait=True)
    b.stop(wait=True)


# ---------------------------------------------------------------- cancel / remove / errors
class SlowStart:
    """A session whose tab never opens, until cancelled."""

    def __init__(self, show_window=False, cdp_url=None, name=None, cancel=None, **kw):
        while not cancel.wait(0.05):
            pass
        raise SessionCancelled("Start cancelled.")


def wait_state(inst, state, timeout=3):
    end = time.time() + timeout
    while inst.state != state and time.time() < end:
        time.sleep(0.02)
    return inst.state


def test_cancel_a_stuck_start_frees_the_port(env, monkeypatch):
    host, store, made, _ = env
    monkeypatch.setattr(manager, "CopilotSession", SlowStart)
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store)
    assert wait_state(inst, "starting") == "starting" and "cancel" in inst.status_text()
    inst.cancel_start()
    assert wait_state(inst, "off") == "off"
    s = socket.socket()
    s.bind(("127.0.0.1", port))            # port released
    s.close()


def test_switching_off_while_starting_cancels(env, monkeypatch):
    host, store, made, _ = env
    monkeypatch.setattr(manager, "CopilotSession", SlowStart)
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store)
    time.sleep(0.1)
    inst.stop()                            # the switch calls stop() when turned off
    assert wait_state(inst, "off") == "off"


def test_cancel_while_waiting_for_browser(env):
    host, store, made, _ = env
    host.ready.clear()                     # browser still starting
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store)
    time.sleep(0.1)
    inst.cancel_start()
    assert wait_state(inst, "off") == "off" and not made


def test_remove_while_starting(env, monkeypatch):
    host, store, made, _ = env
    monkeypatch.setattr(manager, "CopilotSession", SlowStart)
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store)
    time.sleep(0.1)
    inst.remove()
    assert wait_state(inst, "removed") == "removed"
    inst.start(host, store)                # a removed session can't be switched on again
    assert inst.state == "removed"


def test_remove_aborts_a_reply_in_progress(env):
    host, store, made, _ = env
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store, wait=True)
    aborted = []
    made[0].abort = lambda: aborted.append(True)
    threading.Thread(target=lambda: post(port, [{"role": "user", "content": "hi"}]), daemon=True).start()
    time.sleep(0.1)
    t0 = time.time()
    inst.remove(wait=True)
    assert time.time() - t0 < 1.0 and aborted and inst.state == "removed"
    with pytest.raises(OSError):
        post(port, [{"role": "user", "content": "hi"}])


def test_stop_aborts_after_stop_timeout(env, monkeypatch):
    host, store, made, _ = env
    monkeypatch.setattr(config, "STOP_TIMEOUT", 0.2)
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store, wait=True)
    aborted = []
    made[0].close = lambda timeout=15: False     # reply still running when the time is up
    made[0].abort = lambda: aborted.append(True)
    inst.stop(wait=True)
    assert inst.state == "off" and aborted


def test_not_signed_in_shows_in_status(env):
    host, store, made, _ = env
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store, wait=True)
    made[0].needs_sign_in = True
    assert inst.status_text() == "On - not signed in"
    inst.stop(wait=True)


# ---------------------------------------------------------------- leftover processes in the window
def scan_with(*groups):
    return procs.Scan(list(groups), time.time())


def test_session_waits_while_browser_blocked(env):
    host, store, made, _ = env
    host.ready.clear()
    host.blocked = scan_with(procs.Group("leftover", 4120, [4120, 4121]))
    (port,) = free_ports(1)
    inst = manager.Instance(1, port)
    inst.start(host, store)
    time.sleep(0.1)
    assert inst.state == "starting" and inst.status_text().startswith("Waiting for the browser")
    host.end_stale_and_start()
    assert wait_state(inst, "on") == "on"
    inst.stop(wait=True)


def test_browser_text_and_warning_bar():
    host = FakeHost()
    host.blocked = scan_with(procs.Group("leftover", 10, [10, 11, 12]),
                             procs.Group("driver", 20, [20]))
    text, bad = manager.browser_text(host)
    assert bad and "not started" in text
    w, label, action = manager.warning_bar(host)
    assert "1 leftover Edge window (3 processes)" in w and "1 orphaned Playwright driver" in w
    assert label == "End them and start" and action == host.end_stale_and_start

    host.blocked = None
    host.pid = 4120
    host.scan = scan_with(procs.Group("ours", 4120, [4120, 4121, 4122]), procs.Group("driver", 20, [20]))
    text, bad = manager.browser_text(host)
    assert not bad and text.endswith("Edge PID 4120, 3 processes")
    w, label, action = manager.warning_bar(host)
    assert "orphaned Playwright driver" in w and label == "End them"
    action()
    assert host.ended == [[20]]

    host.scan = scan_with(procs.Group("ours", 4120, [4120]))
    assert manager.warning_bar(host)[0] == ""
    host.busy = "Ending processes…"
    assert manager.warning_bar(host) == ("Ending processes…", None, None)


def test_in_use_edge_is_explained():
    host = FakeHost()
    host.ready.clear()
    host.blocked = scan_with(procs.Group("in-use", 30, [30, 31], "python.exe (PID 9)"))
    w, label, _ = manager.warning_bar(host)
    assert "python.exe (PID 9)" in w and "server.py or ask.py" in w and label == "End them and start"
