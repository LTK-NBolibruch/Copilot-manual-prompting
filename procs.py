"""Find and end the browser processes this app leaves behind.

Edge identifies its profile on the command line (--user-data-dir=...), so every Edge process
using the app's profile (config.PROFILE_DIR) can be found, whoever started it. They're grouped by
their main browser process and classified:

    ours      started by this manager, now
    leftover  its parent is gone - left over from a crash, Ctrl+C or a killed Python
    in-use    started by a program that is still running (server.py / ask.py, or Edge opened by hand)
    driver    an orphaned Playwright driver (node.exe ... playwright ... run-driver) whose Python is gone

Only processes using THIS profile are ever touched - never your everyday Edge.

Process lists come from psutil if it's installed, else PowerShell (Windows) or ps (elsewhere).

kill_children_on_exit() (Windows): puts this Python process in a job object that ends every
process it started - Edge, Playwright drivers - when Python exits, even if it crashes or is killed.
"""
import json
import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger("procs")

EDGE_NAMES = {"msedge.exe", "msedge", "microsoft-edge", "microsoft-edge-stable"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)       # no console flashing under pythonw


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    cmd: str


@dataclass
class Group:
    kind: str                     # ours | leftover | in-use | driver
    pid: int                      # main process
    pids: list = field(default_factory=list)   # main + everything under it
    owner: str = ""               # parent that's still running, e.g. "python.exe (PID 1234)"

    @property
    def what(self):
        return {"ours": "Edge (this manager)", "leftover": "Edge left over from an earlier run",
                "in-use": "Edge used by another program", "driver": "Orphaned Playwright driver"}[self.kind]


@dataclass
class Scan:
    groups: list = field(default_factory=list)
    when: float = 0.0
    error: str = ""

    def of(self, *kinds):
        return [g for g in self.groups if g.kind in kinds]

    @property
    def ours(self):
        g = self.of("ours")
        return g[0] if g else None

    @property
    def blocking(self):
        """Edge processes that would stop a new Edge from starting on the profile."""
        return self.of("leftover", "in-use")

    @property
    def stale(self):
        """Safe to end without asking twice: nothing running owns them."""
        return self.of("leftover", "driver")

    def describe(self, groups=None) -> str:
        groups = self.groups if groups is None else groups
        parts = []
        left = [g for g in groups if g.kind == "leftover"]
        used = [g for g in groups if g.kind == "in-use"]
        drv = [g for g in groups if g.kind == "driver"]
        n = lambda gs: sum(len(g.pids) for g in gs)          # noqa: E731
        if left:
            parts.append(f"{len(left)} leftover Edge {'window' if len(left) == 1 else 'windows'} "
                         f"({n(left)} processes)")
        if used:
            parts.append("Edge opened on this profile by " + ", ".join(g.owner for g in used)
                         + " - is server.py or ask.py running?")
        if drv:
            parts.append(f"{len(drv)} orphaned Playwright {'driver' if len(drv) == 1 else 'drivers'}")
        return "; ".join(parts)


# ---------------------------------------------------------------- listing
def list_processes() -> list:
    """All processes: pid, parent pid, name, and the command line for Edge / node processes."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        out = []
        for p in psutil.process_iter(["pid", "ppid", "name"]):
            info = p.info
            name = info.get("name") or ""
            cmd = ""
            if _wanted(name):
                try:
                    cmd = subprocess.list2cmdline(p.cmdline())
                except Exception:
                    pass
            out.append(Proc(info["pid"], info.get("ppid") or 0, name, cmd))
        return out
    if os.name == "nt":
        return _list_powershell()
    return _list_ps()


def _wanted(name: str) -> bool:
    n = (name or "").lower()
    return n in EDGE_NAMES or n in ("node.exe", "node")


def _list_powershell() -> list:
    script = ("Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{ p=$_.ProcessId; "
              "pp=$_.ParentProcessId; n=$_.Name; c=$(if ($_.Name -in 'msedge.exe','node.exe') "
              "{ $_.CommandLine } else { '' }) } } | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    return parse_powershell(r.stdout)


def parse_powershell(text: str) -> list:
    data = json.loads(text or "[]")
    if isinstance(data, dict):
        data = [data]
    return [Proc(int(d["p"]), int(d.get("pp") or 0), d.get("n") or "", d.get("c") or "") for d in data]


def _list_ps() -> list:
    r = subprocess.run(["ps", "-ww", "-eo", "pid=,ppid=,comm=,args="], capture_output=True, text=True, timeout=30)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3:
            pid, ppid, name = int(parts[0]), int(parts[1]), parts[2]
            out.append(Proc(pid, ppid, name, parts[3] if len(parts) > 3 and _wanted(name) else ""))
    return out


# ---------------------------------------------------------------- classifying
# --user-data-dir=C:\p   --user-data-dir="C:\p q"   "--user-data-dir=C:\p q" (how Windows quotes it)
_UDD = (re.compile(r'"--user-data-dir=([^"]+)"', re.I), re.compile(r'--user-data-dir="([^"]+)"', re.I),
        re.compile(r'--user-data-dir=([^\s"]+)', re.I))


def norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p.strip().strip('"'))))


def profile_of(cmd: str):
    for rx in _UDD:
        m = rx.search(cmd or "")
        if m:
            return norm_path(m.group(1))
    return None


def is_edge(p: Proc) -> bool:
    return (p.name or "").lower() in EDGE_NAMES


def is_driver(p: Proc) -> bool:
    c = (p.cmd or "").lower()
    return (p.name or "").lower() in ("node.exe", "node") and "playwright" in c and "run-driver" in c


def classify(procs: list, profile_dir: str, our_pid=None) -> list:
    by_pid = {p.pid: p for p in procs}
    kids = {}
    for p in procs:
        kids.setdefault(p.ppid, []).append(p.pid)

    def alive(pid):
        return pid > 1 and pid in by_pid          # orphans are re-parented to pid 1 outside Windows

    def tree(pid):
        out, todo = [], [pid]
        while todo:
            x = todo.pop()
            if x in out:
                continue
            out.append(x)
            todo.extend(kids.get(x, []))
        return out

    target = norm_path(profile_dir)
    on_profile = [p for p in procs if is_edge(p) and profile_of(p.cmd) == target]
    ours = set(tree(our_pid)) if our_pid and our_pid in by_pid else set()
    groups, covered = [], set(ours)
    if ours:
        groups.append(Group("ours", our_pid, sorted(ours)))
    for p in on_profile:                                  # main browser processes
        if "--type=" in p.cmd or p.pid in covered:
            continue
        t = tree(p.pid)
        covered.update(t)
        if alive(p.ppid) and not is_edge(by_pid[p.ppid]):
            par = by_pid[p.ppid]
            groups.append(Group("in-use", p.pid, t, f"{par.name} (PID {par.pid})"))
        else:
            groups.append(Group("leftover", p.pid, t))
    loose = [p.pid for p in on_profile if p.pid not in covered]   # helpers whose browser is gone
    if loose:
        t = sorted({x for pid in loose for x in tree(pid)})
        covered.update(t)
        groups.append(Group("leftover", loose[0], t))
    for p in procs:
        if is_driver(p) and not alive(p.ppid) and p.pid not in covered:
            t = tree(p.pid)
            covered.update(t)
            groups.append(Group("driver", p.pid, t))
    return groups


def scan(profile_dir: str, our_pid=None) -> Scan:
    try:
        return Scan(classify(list_processes(), profile_dir, our_pid), time.time())
    except Exception as e:
        log.warning("Could not list processes: %s", e)
        return Scan([], time.time(), str(e))


# ---------------------------------------------------------------- ending
def end_tree(pids: list):
    """End a main process and everything under it (pids[0] is the main process)."""
    if not pids:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pids[0]), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        for pid in pids[1:]:                              # helpers that were re-parented elsewhere
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NO_WINDOW)
        return
    for pid in reversed(pids):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def end_groups(profile_dir: str, kinds=("leftover", "driver"), pids=None, our_pid=None, wait=10.0) -> list:
    """Re-scan, then end the groups of the given kinds (or with main PID in `pids`) and wait for
    them to go. Re-scanning first means a PID reused since the last scan is never ended.
    Returns the PIDs still running afterwards."""
    found = scan(profile_dir, our_pid)
    targets = [g for g in found.groups if g.kind != "ours" and
               (g.pid in pids if pids is not None else g.kind in kinds)]
    for g in targets:
        log.info("Ending %s, PID %d (%d processes)%s", g.what.lower(), g.pid, len(g.pids),
                 f", owned by {g.owner}" if g.owner else "")
        end_tree(g.pids)
    want = {pid for g in targets for pid in g.pids}
    deadline = time.time() + wait
    while want and time.time() < deadline:
        running = {p.pid for p in list_processes()}
        want &= running
        if want:
            time.sleep(0.3)
    if want:
        log.warning("Still running after ending them: PIDs %s", sorted(want))
    return sorted(want)


def wait_profile_free(profile_dir: str, our_pid=None, timeout=10.0) -> bool:
    """Wait until no Edge process uses the profile (after closing ours)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = scan(profile_dir, our_pid)
        if s.error or not s.of("ours", "leftover", "in-use"):
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------- Windows: end children on exit
_job = None


def kill_children_on_exit() -> bool:
    """Windows: put this Python process in a job object with KILL_ON_JOB_CLOSE. Processes started
    from now on (Edge, Playwright drivers) join it, and Windows ends them all when Python exits -
    even if Python crashes or is ended from Task Manager. Returns False where unsupported."""
    global _job
    if os.name != "nt" or _job is not None:
        return _job is not None
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000           # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        _job = job              # keep the handle open for Python's lifetime
        log.info("Child processes (Edge, Playwright drivers) will be ended when this program exits")
        return True
    except Exception as e:
        log.warning("Could not arrange for child processes to end with this program (%s); "
                    "leftover Edge processes will be detected at the next start instead.", e)
        return False
