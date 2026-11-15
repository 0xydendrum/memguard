"""memguard - run heavy local jobs one at a time, and only when memory can hold them.

Created by 0xydendrum. PolyForm Noncommercial License 1.0.0.

VRAM and system RAM are budgeted as one pool. The NVIDIA driver's CUDA sysmem fallback (on by
default; check it with `memguard-sysmem`; verified with a 14 GiB-on-12 GiB allocation on an RTX 3060) lets GPU
allocations spill into system RAM instead of failing. So a VRAM shortfall becomes extra RAM use
(at PCIe speed, roughly 10x slower than VRAM) rather than a crash. A job starts only when

    free RAM - reserve >= ram + max(0, vram - free VRAM)

The reserve (default 6 GiB) keeps the PC responsive: in the incident that started this tool, four
background jobs were killed when free RAM fell to about 5 GiB.

Heavy jobs also take a machine-wide lock with a first-come-first-served queue, so they never
overlap. The job runs inside a Windows job object, so if memguard is killed the whole process
tree dies with it, and no orphan keeps running (and holding memory) after its lock is gone.

  memguard [--ram GiB] [--vram GiB] [--reserve GiB] [--lock NAME | --no-lock] [--max-wait MIN]
           [--no-spill] [--no-kill-tree] -- command args...
  memguard --check [--ram GiB] [--vram GiB] [--max-wait MIN]   # no lock; exit 0 = fits now
  memguard --status

--ram / --vram are what the job itself will add on top of current use. Long-lived servers
(llama-server) should not run under the lock; start them after `memguard --check` instead.
Exit code: the job's own; 3 if memory never fitted within --max-wait; 2 for usage errors.
"""
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from memguard import __author__, __version__  # noqa: F401  (single source for both)

if sys.platform != "win32":
    sys.exit("memguard is Windows-only: it uses Windows job objects, file locks and the NVIDIA driver's "
             "Windows sysmem fallback.")
import msvcrt  # noqa: E402  (Windows-only modules, imported after the platform check)
from ctypes import wintypes  # noqa: E402

GIB = 1 << 30
HELD_ENV = "MEMGUARD_HELD"


def state_dir():
    """Folder for locks, queue tickets and holder files. Every memguard that should share a queue must use
    the same folder; set MEMGUARD_STATE to choose it (default %LOCALAPPDATA%\\memguard)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(os.environ.get("MEMGUARD_STATE") or os.path.join(base, "memguard"))

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
k32.CreateJobObjectW.restype = wintypes.HANDLE
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)]
ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
ntdll.NtResumeProcess.restype = ctypes.c_long


# ---------------------------------------------------------------- memory

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram():
    """Free and total physical RAM, free and total commit (RAM + pagefile), all in GiB."""
    m = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
    if not k32.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (m.ullAvailPhys / GIB, m.ullTotalPhys / GIB, m.ullAvailPageFile / GIB, m.ullTotalPageFile / GIB)


def vram():
    """(free, total) GiB of dedicated VRAM on GPU 0, or (None, None) when nvidia-smi can't be read."""
    try:
        out = subprocess.run(["nvidia-smi", "-i", "0", "--query-gpu=memory.free,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=30,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        free, total = (float(x) for x in out.strip().split(","))
        return free / 1024, total / 1024
    except Exception:
        return None, None


def plan(need_ram, need_vram, reserve, allow_spill):
    free_ram, total_ram, _, _ = ram()
    free_v, total_v = vram() if need_vram > 0 else (None, None)
    spill = max(0.0, need_vram - (free_v or 0.0)) if need_vram > 0 else 0.0  # unreadable VRAM -> assume none free
    room = free_ram - reserve
    p = {"free_ram": free_ram, "total_ram": total_ram, "free_vram": free_v, "total_vram": total_v,
         "spill": spill, "room": room, "need": need_ram + spill}
    p["fits"] = room >= p["need"] and (allow_spill or spill == 0)
    too_big_for_ram = need_ram + spill + reserve > total_ram
    too_big_to_spill = spill > total_ram / 2  # Windows caps CUDA's shared (spilled) memory at half of RAM
    too_big_for_gpu = not allow_spill and total_v is not None and need_vram > total_v
    p["never"] = too_big_for_ram or too_big_to_spill or too_big_for_gpu
    return p


def describe(p, need_ram, need_vram, reserve):
    v = "unknown" if p["free_vram"] is None else f"{p['free_vram']:.1f}"
    s = f"need {need_ram:.1f} GiB RAM"
    if need_vram > 0:
        s += f" + {need_vram:.1f} GiB VRAM (free {v})"
        if p["spill"] > 0:
            s += f", {p['spill']:.1f} GiB of it spills into RAM"
    return s + f"; RAM free {p['free_ram']:.1f} GiB, reserve {reserve:.1f}"


# ---------------------------------------------------------------- processes

def proc_start(pid):
    """Creation time of a running process, 'unknown' if it exists but can't be queried, None if it's gone."""
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "unknown" if ctypes.get_last_error() == 5 else None  # 5 = access denied: alive
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # 259 = STILL_ACTIVE
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
            return "unknown"
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        k32.CloseHandle(h)


def alive(info):
    st = proc_start(info["pid"])
    return st is not None and (st == "unknown" or info.get("start") in (None, "unknown", st))


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                  "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


JOB_EXTENDED_LIMITS, JOB_ACCOUNTING, JOB_PROCESS_IDS, KILL_ON_JOB_CLOSE = 9, 1, 3, 0x2000


def kill_on_close_job():
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, JOB_EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def job_leftovers(job):
    """Image names of processes still in the job, ignoring the conhost.exe that Windows attaches to a hidden
    console (memguard creates one when it has no console itself). None if the list can't be read."""
    class PIDS(ctypes.Structure):
        _fields_ = [("assigned", wintypes.DWORD), ("listed", wintypes.DWORD), ("ids", ctypes.c_size_t * 512)]
    info = PIDS()
    if not k32.QueryInformationJobObject(job, JOB_PROCESS_IDS, ctypes.byref(info), ctypes.sizeof(info), None):
        return None
    names = []
    for pid in info.ids[:info.listed]:
        if proc_start(pid) is None:  # exited between the listing and now (often the console host): not a leftover
            continue
        name = f"pid {pid}"
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf, n = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                name = os.path.basename(buf.value)
            k32.CloseHandle(h)
        if name.lower() != "conhost.exe":
            names.append(name)
    return names


def job_query(job, cls, struct):
    info = struct()
    ok = k32.QueryInformationJobObject(job, cls, ctypes.byref(info), ctypes.sizeof(info), None)
    return info if ok else None


# ---------------------------------------------------------------- lock + queue

class Lock:
    """A byte-range lock on state/<name>.lock. Windows drops it when the holder exits or is killed,
    and file locks work across logon sessions (desktop, SSH) and elevation, unlike Local\\ mutexes."""

    def __init__(self, name):
        state = state_dir()
        state.mkdir(parents=True, exist_ok=True)
        self.name, self.path, self.fd = name, state / f"{name}.lock", None
        self.holder = state / f"{name}.holder.json"
        self.queue = state / f"{name}.queue"
        self.queue.mkdir(exist_ok=True)

    def try_acquire(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT)  # not inherited by children (PEP 446)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            self.holder.unlink(missing_ok=True)
            try:
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None

    def is_free(self):
        if self.fd is not None:
            return False
        if self.try_acquire():
            os.lseek(self.fd, 0, 0)
            msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            os.close(self.fd)
            self.fd = None
            return True
        return False

    def tickets(self):
        """Live queue tickets, oldest first; tickets of dead processes are removed."""
        out = []
        for f in sorted(self.queue.glob("*.json")):
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if alive(t):
                out.append((f, t))
            else:
                f.unlink(missing_ok=True)
        return out

    def holder_info(self):
        try:
            h = json.loads(self.holder.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return h if alive(h) else None


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def clock(t):
    return time.strftime("%I:%M %p", time.localtime(t)).lstrip("0")


def say(msg):
    print(f"memguard: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- commands

def status(lock_name):
    free_ram, total_ram, free_commit, total_commit = ram()
    free_v, total_v = vram()
    print(f"RAM    {free_ram:5.1f} / {total_ram:.1f} GiB free   (commit: {free_commit:.1f} / {total_commit:.1f} GiB free)")
    if free_v is None:
        print("VRAM   unreadable (nvidia-smi failed)")
    else:
        print(f"VRAM   {free_v:5.1f} / {total_v:.1f} GiB free   (overflow spills into RAM; check: memguard-sysmem)")
    lk = Lock(lock_name)
    h = lk.holder_info()
    if lk.is_free():
        print(f"lock '{lock_name}': free")
    elif h:
        mins = (time.time() - h["since"]) / 60
        print(f"lock '{lock_name}': held by pid {h['pid']} since {clock(h['since'])} ({mins:.0f} min): {h['cmd']}")
    else:
        print(f"lock '{lock_name}': held (holder unknown)")
    q = lk.tickets()
    print(f"queue: {len(q)} waiting")
    for i, (_, t) in enumerate(q, 1):
        print(f"  {i}. pid {t['pid']} since {clock(t['queued'])}, ram {t['ram']} vram {t['vram']}: {t['cmd']}")
    return 0


def wait_for_memory(a, deadline, waiting_on):
    """Poll until the plan fits; returns the plan, or None on timeout / never-fits."""
    last = 0.0
    while True:
        p = plan(a.ram, a.vram, a.reserve, not a.no_spill)
        if p["fits"]:
            return p
        if p["never"]:
            say("this can never fit on this PC: " + describe(p, a.ram, a.vram, a.reserve))
            return None
        if deadline is not None and time.time() >= deadline:
            say("gave up waiting: " + describe(p, a.ram, a.vram, a.reserve))
            return None
        if time.time() - last >= 60:
            say(f"waiting for memory{waiting_on}: " + describe(p, a.ram, a.vram, a.reserve))
            last = time.time()
        time.sleep(10)


def run(a, cmd):
    held = [x for x in os.environ.get(HELD_ENV, "").split(",") if x]
    cmd_text = subprocess.list2cmdline(cmd)
    deadline = None if a.max_wait is None else time.time() + a.max_wait * 60
    lock = None
    if a.lock and a.lock in held:
        say(f"already inside lock '{a.lock}', running directly")
        a.lock = None
    elif a.lock:
        lock = Lock(a.lock)
        me = {"pid": os.getpid(), "start": proc_start(os.getpid()), "cmd": cmd_text, "cwd": os.getcwd(),
              "queued": time.time(), "ram": a.ram, "vram": a.vram}
        ticket = lock.queue / f"{time.time_ns():020d}-{os.getpid()}.json"
        write_json(ticket, me)
        last = 0.0
        try:
            while True:
                q = lock.tickets()
                first = q and q[0][0] == ticket
                if first and lock.try_acquire():
                    break
                if deadline is not None and time.time() >= deadline:
                    say(f"gave up waiting for lock '{a.lock}'")
                    return 3
                if time.time() - last >= 60:
                    pos = next((i for i, (f, _) in enumerate(q, 1) if f == ticket), "?")
                    h = lock.holder_info()
                    who = f"pid {h['pid']}: {h['cmd']}" if h else "unknown"
                    say(f"queued for lock '{a.lock}' (position {pos} of {len(q)}); running now: {who}")
                    last = time.time()
                time.sleep(2)
        finally:
            ticket.unlink(missing_ok=True)
        write_json(lock.holder, dict(me, since=time.time()))

    try:
        p = wait_for_memory(a, deadline, f" (holding lock '{a.lock}')" if lock else "")
        if p is None:
            return 3
        say(f"start {cmd_text} | " + describe(p, a.ram, a.vram, a.reserve))
        if p["spill"] > 0:
            say(f"warning: ~{p['spill']:.1f} GiB of the VRAM need goes to system RAM, so the GPU part runs slower")
        env = dict(os.environ)
        if a.lock:
            env[HELD_ENV] = ",".join(held + [a.lock])
        return launch(cmd, env, not a.no_kill_tree, a.reserve)
    finally:
        if lock:
            lock.release()


def launch(cmd, env, kill_tree, reserve):
    exe = shutil.which(cmd[0]) or cmd[0]
    t0 = time.time()
    lowest = [ram()[0]]
    stop = threading.Event()

    def watch():
        warned = False
        while not stop.wait(5):
            free = ram()[0]
            lowest[0] = min(lowest[0], free)
            if free < reserve / 2 and not warned:
                say(f"warning: free RAM is down to {free:.1f} GiB while the job runs")
                warned = True

    threading.Thread(target=watch, daemon=True).start()
    job = kill_on_close_job() if kill_tree else None
    flags = 0x4 if job else 0  # 0x4 = CREATE_SUSPENDED
    # An orphaned memguard (its parent shell was killed) has no console, and console programs such as Git Bash then
    # die at start with 0xC0000142 (DLL init failed). Seen 2026-11-09: a queued job failed when its turn came.
    # Give the child a hidden console of its own in that case.
    # The new hidden console also brings its own std handles, so pass ours on explicitly, or the job's output
    # (e.g. a queue script redirected to a log) goes into that invisible console and every echo fails (seen
    # 2026-11-09: three overnight gate logs held only memguard's lines, and each job "exited 1" on its last echo).
    std = {}
    if not k32.GetConsoleWindow():
        flags |= 0x08000000  # CREATE_NO_WINDOW
        std["stdin"] = subprocess.DEVNULL
        for name in ("stdout", "stderr"):
            try:
                std[name] = getattr(sys, name).fileno()
            except (AttributeError, OSError, ValueError):
                std[name] = subprocess.DEVNULL
    try:
        proc = subprocess.Popen([exe] + cmd[1:], env=env, creationflags=flags, **std)
    except OSError as e:
        say(f"cannot start {cmd[0]}: {e}")
        return 2
    if job:
        # assign before the first instruction runs, so every grandchild lands in the job too
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            say(f"note: couldn't put the job in a kill-on-close group ({ctypes.WinError(ctypes.get_last_error())})")
        ntdll.NtResumeProcess(int(proc._handle))
    # keep the PC awake while the job runs (2026-11-11: idle sleep with the GPU busy hung the PC into a hard power-off).
    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED on this (waiting) thread; the display may still turn off. Cleared at exit.
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    except Exception:
        pass
    try:
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            try:
                rc = proc.wait(timeout=10)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                proc.kill()
                rc = 130
    finally:
        stop.set()
        peak = ""
        if job:
            acct = job_query(job, JOB_ACCOUNTING, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
            if acct and acct.ActiveProcesses:
                left = job_leftovers(job)
                if left is None:
                    say(f"stopping {acct.ActiveProcesses} leftover process(es) the job left running")
                elif left:
                    say(f"stopping {len(left)} leftover process(es) the job left running: {', '.join(left)}")
            lim = job_query(job, JOB_EXTENDED_LIMITS, JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
            if lim and lim.PeakJobMemoryUsed:
                peak = f", peak commit {lim.PeakJobMemoryUsed / GIB:.1f} GiB"
            k32.TerminateJobObject(job, 1)
            k32.CloseHandle(job)
    lowest[0] = min(lowest[0], ram()[0])
    say(f"done in {(time.time() - t0) / 60:.1f} min, exit {rc}{peak}, lowest free RAM {lowest[0]:.1f} GiB")
    return rc


def main(argv):
    if "--" in argv:
        i = argv.index("--")
        opts, cmd = argv[:i], argv[i + 1:]
    else:
        opts, cmd = argv, []
    ap = argparse.ArgumentParser(prog="memguard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ram", type=float, default=0.0, help="GiB of system RAM the job adds (default 0)")
    ap.add_argument("--vram", type=float, default=0.0, help="GiB of VRAM the job adds (default 0)")
    ap.add_argument("--reserve", type=float, default=6.0, help="GiB of RAM that must stay free (default 6)")
    ap.add_argument("--lock", default="heavy", help="lock/queue name (default 'heavy')")
    ap.add_argument("--no-lock", action="store_true", help="don't queue; only wait for memory")
    ap.add_argument("--max-wait", type=float, default=None,
                    help="minutes to wait for the lock + memory (default: forever; --check: 0)")
    ap.add_argument("--no-spill", action="store_true", help="require the VRAM need to fit in free VRAM")
    ap.add_argument("--no-kill-tree", action="store_true", help="let the job's processes outlive memguard")
    ap.add_argument("--check", action="store_true", help="only report whether it fits (waits up to --max-wait)")
    ap.add_argument("--status", action="store_true", help="show memory, the lock holder and the queue")
    ap.add_argument("--version", action="version", version=f"memguard {__version__} by {__author__}")
    a = ap.parse_args(opts)
    if a.status:
        return status(a.lock)
    if a.check:
        deadline = time.time() + (a.max_wait or 0) * 60
        p = wait_for_memory(a, deadline, "")
        if p is None:
            return 3
        say("fits: " + describe(p, a.ram, a.vram, a.reserve))
        return 0
    if not cmd:
        ap.error("give the command after --, e.g.  memguard --ram 4 -- python bench.py")
    if a.no_lock:
        a.lock = None
    return run(a, cmd)


def entry():
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        say("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    entry()
