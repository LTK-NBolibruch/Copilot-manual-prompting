"""BrowserHost: starting a normal Edge with remote debugging, its error messages, and the check
for leftover Edge processes before starting. Edge is simulated (no browser)."""
import os
import queue
import sys
import threading
import time
from concurrent.futures import Future

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import copilot  # noqa: E402
import procs  # noqa: E402
from copilot import CopilotError, SessionCancelled, edge_command, find_edge, wait_for_devtools  # noqa: E402


@pytest.fixture(autouse=True)
def edge_path(monkeypatch, tmp_path):
    exe = tmp_path / "msedge.exe"
    exe.write_text("")
    monkeypatch.setattr(config, "EDGE_PATH", str(exe))
    monkeypatch.setattr(config, "PROFILE_DIR", str(tmp_path / "profile"))
    return str(exe)


class Proc:
    def __init__(self, rc=None):
        self.rc = rc

    def poll(self):
        return self.rc


def test_find_edge_uses_setting(edge_path):
    assert find_edge() == edge_path


def test_find_edge_not_found(monkeypatch):
    monkeypatch.setattr(config, "EDGE_PATH", "")
    for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(copilot.shutil, "which", lambda n: None)
    with pytest.raises(CopilotError, match="COPILOT_EDGE_PATH"):
        find_edge()


def test_edge_command_is_a_normal_edge(edge_path, monkeypatch):
    monkeypatch.setattr(config, "EDGE_HIDDEN", "offscreen")
    cmd = edge_command(9333, visible=False)
    assert cmd[0] == edge_path and "--remote-debugging-port=9333" in cmd
    assert f"--user-data-dir={os.path.abspath(config.PROFILE_DIR)}" in cmd
    assert "--window-position=-32000,-32000" in cmd and cmd[-1] == config.CHAT_URL
    assert not any("enable-automation" in a or "headless" in a for a in cmd)    # what Playwright adds
    assert "--window-position=-32000,-32000" not in edge_command(9333, visible=True)
    monkeypatch.setattr(config, "EDGE_HIDDEN", "headless")
    assert "--headless=new" in edge_command(9333, visible=False)


def test_edge_command_matches_process_scan(edge_path):
    """The profile in the command line is found again by the process scan."""
    cmd = " ".join(edge_command(9333, visible=False))
    assert procs.profile_of(cmd) == procs.norm_path(config.PROFILE_DIR)


def test_wait_for_devtools_ready(monkeypatch):
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_open(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("not yet")
        return Resp()
    monkeypatch.setattr(copilot.urllib.request, "urlopen", fake_open)
    wait_for_devtools(9333, Proc(), timeout=5)
    assert calls[-1] == "http://127.0.0.1:9333/json/version"


def test_edge_handing_off_to_a_running_edge_is_explained(monkeypatch):
    monkeypatch.setattr(copilot.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(CopilotError, match="already using the profile"):
        wait_for_devtools(9333, Proc(rc=0), timeout=5)


def test_devtools_timeout_is_explained(monkeypatch):
    monkeypatch.setattr(copilot.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(CopilotError, match="did not open its debugging port"):
        wait_for_devtools(9333, Proc(), timeout=0.3)


def test_wait_for_devtools_cancel(monkeypatch):
    monkeypatch.setattr(copilot.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    ev = threading.Event()
    ev.set()
    with pytest.raises(SessionCancelled):
        wait_for_devtools(9333, Proc(), timeout=5, cancel=ev)


class DeadPopen:
    def __init__(self, *a, **k):
        self.pid = 1

    def poll(self):
        return 0


def test_host_reports_startup_error(monkeypatch):
    monkeypatch.setattr(copilot.subprocess, "Popen", DeadPopen)
    monkeypatch.setattr(copilot.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(procs, "scan", lambda *a, **k: procs.Scan([], time.time()))
    host = copilot.BrowserHost(monitor=False)
    with pytest.raises(CopilotError, match="already using the profile"):
        host.wait_ready(timeout=5)
    assert host.error and not host.can_show
    host.stop(timeout=5)


def test_session_start_timeout_and_cancel(monkeypatch):
    """A session whose tab never opens: __init__ gives up after start_timeout, or on cancel."""
    def never_ready(self):
        while not self._aborted:
            threading.Event().wait(0.05)
    monkeypatch.setattr(copilot.CopilotSession, "_run", never_ready)
    with pytest.raises(CopilotError, match="did not open within"):
        copilot.CopilotSession(cdp_url="http://x", start_timeout=0.01)
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    with pytest.raises(SessionCancelled):
        copilot.CopilotSession(cdp_url="http://x", start_timeout=30, cancel=ev)


def test_abort_fails_queued_prompts():
    s = copilot.CopilotSession.__new__(copilot.CopilotSession)
    s._q, s._aborted = queue.Queue(), False
    f = Future()
    s._q.put(("prompt", "hi", f, None, None, None))
    s.abort()
    s._fail_queued()
    with pytest.raises(SessionCancelled):
        f.result(timeout=1)


# ---------------------------------------------------------------- leftover processes before starting
class FakeProcs:
    """Stands in for procs.scan / procs.end_groups: `groups` are on the profile until ended."""

    def __init__(self, groups):
        self.groups = list(groups)
        self.ended = []

    def scan(self, profile_dir, our_pid=None):
        return procs.Scan(list(self.groups), time.time())

    def end_groups(self, profile_dir, kinds=("leftover", "driver"), pids=None, our_pid=None, wait=10.0):
        gone = [g for g in self.groups if (g.pid in pids if pids is not None else g.kind in kinds)]
        self.ended += [g.pid for g in gone]
        self.groups = [g for g in self.groups if g not in gone]
        return []


@pytest.fixture()
def blocked_host(monkeypatch):
    fake = FakeProcs([procs.Group("leftover", 4120, [4120, 4121]), procs.Group("driver", 77, [77])])
    monkeypatch.setattr(procs, "scan", fake.scan)
    monkeypatch.setattr(procs, "end_groups", fake.end_groups)
    started = []

    def popen(*a, **k):
        started.append(a)
        return DeadPopen()
    monkeypatch.setattr(copilot.subprocess, "Popen", popen)
    monkeypatch.setattr(copilot.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    hosts = []

    def make():
        hosts.append(copilot.BrowserHost(monitor=False))
        return hosts[-1]
    yield fake, started, make
    for h in hosts:            # stop them while the stand-ins are still in place: a host left waiting
        h.stop(timeout=5)      # would start a real Edge once the test's fake leftovers "disappear"


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.02)
    return cond()


def test_ask_mode_waits_then_ends_and_starts(blocked_host, monkeypatch):
    fake, started, make = blocked_host
    monkeypatch.setattr(config, "STALE_EDGE", "ask")
    host = make()
    assert wait_for(lambda: host.blocked is not None)
    assert not started and "1 leftover Edge window (2 processes)" in host.blocked.describe()
    host.end_stale_and_start()
    assert wait_for(lambda: started)                       # Edge launched only after ending them
    assert fake.ended == [4120, 77] and host.blocked is None


def test_ask_mode_continues_when_ended_elsewhere(blocked_host, monkeypatch):
    fake, started, make = blocked_host
    monkeypatch.setattr(config, "STALE_EDGE", "ask")
    host = make()
    assert wait_for(lambda: host.blocked is not None)
    fake.groups = [g for g in fake.groups if g.kind == "driver"]   # ended in Task Manager; drivers don't block
    assert wait_for(lambda: started, timeout=6) and host.blocked is None and fake.ended == []


def test_ask_mode_stop_while_blocked(blocked_host, monkeypatch):
    fake, started, make = blocked_host
    monkeypatch.setattr(config, "STALE_EDGE", "ask")
    host = make()
    assert wait_for(lambda: host.blocked is not None)
    host.stop(timeout=5)
    assert host.blocked is None and not started and not host._thread.is_alive()


def test_end_mode_ends_automatically(blocked_host, monkeypatch):
    fake, started, make = blocked_host
    monkeypatch.setattr(config, "STALE_EDGE", "end")
    make()
    assert wait_for(lambda: started) and fake.ended == [4120, 77]


def test_ignore_mode_starts_anyway(blocked_host, monkeypatch):
    fake, started, make = blocked_host
    monkeypatch.setattr(config, "STALE_EDGE", "ignore")
    make()
    assert wait_for(lambda: started) and fake.ended == []


def test_wait_ready_has_no_time_limit_while_blocked(blocked_host, monkeypatch):
    fake, started, make = blocked_host
    monkeypatch.setattr(config, "STALE_EDGE", "ask")
    host = make()
    assert wait_for(lambda: host.blocked is not None)
    ev = threading.Event()
    threading.Timer(0.6, ev.set).start()
    with pytest.raises(SessionCancelled):                   # not "did not start in time" after 0.1 s
        host.wait_ready(cancel=ev, timeout=0.1)
