"""Finding and ending leftover browser processes (procs.py).

The integration tests start real stand-in processes named "msedge" and "node" (Python under that
name) with Edge-like command lines, so the real process listing and ending code runs. Windows-only
parts (PowerShell listing, taskkill, job object) are covered by parsing tests or not at all."""
import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import procs  # noqa: E402
from procs import Proc, classify  # noqa: E402

PROFILE = "/home/u/app/playwright_profile"
UDD = f"--user-data-dir={PROFILE}"


def P(pid, ppid, name, cmd=""):
    return Proc(pid, ppid, name, cmd)


def kinds(groups):
    return sorted((g.kind, g.pid, len(g.pids)) for g in groups)


# ---------------------------------------------------------------- classifying (synthetic)
def test_classify_kinds():
    ps = [
        P(1, 0, "init"), P(500, 1, "python.exe"), P(900, 1, "explorer.exe"),
        # this manager's Edge (started by the manager's python 500) with two helpers
        P(10, 500, "msedge.exe", f"msedge.exe --remote-debugging-port=1 {UDD}"),
        P(11, 10, "msedge.exe", f"msedge.exe --type=renderer {UDD}"),
        P(12, 10, "msedge.exe", "msedge.exe --type=gpu-process"),          # helper without the flag
        # left over from a crash: parent 400 is gone
        P(20, 400, "msedge.exe", f'msedge.exe "--user-data-dir={PROFILE}"'),
        P(21, 20, "msedge.exe", f"msedge.exe --type=renderer {UDD}"),
        # helpers whose browser process is gone
        P(30, 31, "msedge.exe", f"msedge.exe --type=renderer {UDD}"),
        # someone else's Edge on the same profile (server.py's node driver 600 still running)
        P(600, 500, "node.exe", r"node.exe C:\x\playwright\driver\cli.js run-driver"),
        P(40, 600, "msedge.exe", f"msedge.exe --remote-debugging-pipe {UDD}"),
        # everyday Edge (different profile) and a plain Edge: never listed
        P(50, 900, "msedge.exe", "msedge.exe --user-data-dir=/home/u/AppData/Edge/User Data"),
        P(51, 900, "msedge.exe", "msedge.exe"),
        # Playwright drivers: orphaned (parent 401 gone) vs owned by a running python
        P(60, 401, "node.exe", r"node.exe C:\x\Playwright\driver\package\cli.js run-driver"),
        P(61, 500, "node.exe", r"node.exe C:\x\playwright\driver\package\cli.js run-driver"),
        P(62, 402, "node.exe", "node.exe some-other-tool.js"),
    ]
    g = classify(ps, PROFILE, our_pid=10)
    assert kinds(g) == [("driver", 60, 1), ("in-use", 40, 1), ("leftover", 20, 2), ("leftover", 30, 1),
                        ("ours", 10, 3)]
    used = next(x for x in g if x.kind == "in-use")
    assert used.owner == "node.exe (PID 600)"
    # without knowing our PID, our own Edge counts as in use by its (running) python
    assert ("in-use", 10, 3) in kinds(classify(ps, PROFILE))


def test_quoted_paths_with_spaces():
    prof = "/home/u/My Apps/profile"
    ps = [P(10, 99, "msedge.exe", f'msedge.exe --remote-debugging-port=1 "--user-data-dir={prof}" x'),
          P(11, 98, "msedge.exe", f'msedge.exe --user-data-dir="{prof}" --type=renderer')]
    assert kinds(classify(ps, prof)) == [("leftover", 10, 1), ("leftover", 11, 1)]
    assert procs.profile_of(f'--user-data-dir="{prof}"') == procs.norm_path(prof)
    assert procs.profile_of(f"--user-data-dir={PROFILE}/../playwright_profile x") == procs.norm_path(PROFILE)
    assert procs.profile_of(r'"C:\Program Files\msedge.exe" --user-data-dir=/p') == procs.norm_path("/p")


def test_reparented_to_init_counts_as_leftover():
    ps = [P(1, 0, "systemd"), P(20, 1, "msedge", f"msedge {UDD}")]
    assert kinds(classify(ps, PROFILE)) == [("leftover", 20, 1)]


def test_describe_and_views():
    s = procs.Scan([procs.Group("leftover", 1, [1, 2, 3]), procs.Group("leftover", 4, [4]),
                    procs.Group("in-use", 5, [5], "python.exe (PID 9)"), procs.Group("driver", 6, [6]),
                    procs.Group("ours", 7, [7])])
    assert s.describe() == ("2 leftover Edge windows (4 processes); Edge opened on this profile by python.exe "
                            "(PID 9) - is server.py or ask.py running?; 1 orphaned Playwright driver")
    assert [g.pid for g in s.blocking] == [1, 4, 5]
    assert [g.pid for g in s.stale] == [1, 4, 6]
    assert s.ours.pid == 7


def test_parse_powershell():
    one = '{"p":10,"pp":4,"n":"msedge.exe","c":"msedge.exe --user-data-dir=x"}'
    assert procs.parse_powershell(one) == [Proc(10, 4, "msedge.exe", "msedge.exe --user-data-dir=x")]
    many = '[{"p":1,"pp":0,"n":"System","c":""},{"p":2,"pp":1,"n":"node.exe","c":null}]'
    assert procs.parse_powershell(many) == [Proc(1, 0, "System", ""), Proc(2, 1, "node.exe", "")]
    assert procs.parse_powershell("") == []


# ---------------------------------------------------------------- real processes (not Windows)
pytestmark_real = pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="POSIX only")
SLEEP = "import time; time.sleep(60)"
SPAWN = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', "
         "'--type=renderer', sys.argv[1]]); time.sleep(60)")


@pytest.fixture()
def fakes(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("msedge", "node"):
        os.symlink(sys.executable, bindir / name)       # Python, but listed as "msedge" / "node"
    prof = str(tmp_path / "profile")
    started = []

    def run(name, *args, orphan=False):
        exe = str(bindir / name)
        if orphan:                                       # parent exits at once: like a crashed manager
            line = " ".join(subprocess.list2cmdline([a]) for a in (exe, *args))
            subprocess.run(["sh", "-c", f"{line} >/dev/null 2>&1 &"], check=True)
        else:
            started.append(subprocess.Popen([exe, *args]))
    yield run, prof, started
    for p in started:
        p.kill()
    for p in procs.list_processes():
        if p.name in ("msedge", "node") and str(tmp_path) in p.cmd:
            try:
                os.kill(p.pid, 9)
            except OSError:
                pass


def wait_until(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        r = cond()
        if r:
            return r
        time.sleep(0.1)
    return cond()


@pytestmark_real
@pytest.mark.parametrize("use_psutil", [True, False])
def test_find_and_end_real_processes(fakes, monkeypatch, tmp_path, use_psutil):
    if not use_psutil:
        monkeypatch.setitem(sys.modules, "psutil", None)           # ps fallback
    run, prof, started = fakes
    udd = f"--user-data-dir={prof}"
    run("msedge", "-c", SPAWN, udd, orphan=True)                   # leftover main + renderer
    run("msedge", "-c", SLEEP, "--remote-debugging-pipe", udd)     # in use by this (running) test
    run("node", "-c", SLEEP, str(tmp_path / "playwright/driver/cli.js"), "run-driver", orphan=True)

    def ready():
        s = procs.scan(prof)
        leftover = [g for g in s.of("leftover") if len(g.pids) == 2]
        drivers = [g for g in s.of("driver") if str(tmp_path) in next(
            (p.cmd for p in procs.list_processes() if p.pid == g.pid), "")]
        return (s, leftover, drivers) if leftover and drivers and s.of("in-use") else None
    found = wait_until(ready)
    if not found:
        s = procs.scan(prof)
        pytest.skip(f"orphans weren't re-parented to init here: {s.groups}")
    s, leftover, drivers = found
    assert s.of("in-use")[0].owner.endswith(f"(PID {os.getpid()})")
    assert "1 leftover Edge window (2 processes)" in s.describe(leftover)

    left = procs.end_groups(prof, pids=[leftover[0].pid, drivers[0].pid], wait=5)
    assert left == []
    s2 = procs.scan(prof)
    assert not s2.of("leftover") and s2.of("in-use")             # the in-use one was left alone
    assert all(g.pid != drivers[0].pid for g in s2.of("driver"))


@pytestmark_real
def test_wait_profile_free(fakes):
    run, prof, started = fakes
    run("msedge", "-c", SLEEP, f"--user-data-dir={prof}")
    assert wait_until(lambda: procs.scan(prof).of("in-use"))
    assert procs.wait_profile_free(prof, timeout=0.3) is False
    started[0].kill()
    started[0].wait()
    assert procs.wait_profile_free(prof, timeout=3) is True


def test_kill_children_on_exit_is_windows_only():
    if os.name != "nt":
        assert procs.kill_children_on_exit() is False
