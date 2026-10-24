"""Own native worker trees, including Windows virtual-environment launchers."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
import uuid
from ctypes import wintypes

import psutil

_JOB_ENV = "GOMROK_WORKER_JOB"
_SAFE_ENV = {
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "SYSTEMDRIVE",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
}


def worker_environment(extra=None):
    """Do not give document parsers session keys or cloud credentials."""
    env = {key: value for key, value in os.environ.items() if key.upper() in _SAFE_ENV}
    env.update(extra or {})
    return env


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IOLimits(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IOLimits),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": (wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        "OpenJobObjectW": (
            wintypes.HANDLE,
            [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR],
        ),
        "SetInformationJobObject": (
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
        ),
        "AssignProcessToJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        "IsProcessInJob": (
            wintypes.BOOL,
            [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
        ),
        "TerminateJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        "GetCurrentProcess": (wintypes.HANDLE, []),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, arguments
    return kernel


def _assign(kernel, job, process):
    # The parent and child may enroll concurrently. A process already in this
    # exact job is safe; membership of a different inherited job is not enough.
    if not kernel.AssignProcessToJobObject(job, process):
        member = wintypes.BOOL()
        if (
            not kernel.IsProcessInJob(process, job, ctypes.byref(member))
            or not member.value
        ):
            raise OSError("Native worker containment failed")


def enter_worker_job():
    """Called before importing a native decoder/model, closing the spawn race."""
    if os.name != "nt":
        return
    name = os.environ.get(_JOB_ENV)
    if not name:
        raise OSError("Native worker containment is required")
    kernel = _kernel()
    handle = kernel.OpenJobObjectW(0x0001 | 0x0004, False, name)
    if not handle:
        raise OSError("Native worker containment is unavailable")
    try:
        _assign(kernel, handle, kernel.GetCurrentProcess())
    finally:
        kernel.CloseHandle(handle)


class ProcessGuard:
    """A single owned subprocess tree with bounded lifetime and aggregate memory.

    Windows uses a Job Object (commit ceiling and kill-on-close); other platforms
    use a new process group plus sampled aggregate RSS. No global PID matching.
    """

    def __init__(self, memory_bytes):
        self.limit = memory_bytes
        self.process = None
        self.failure = None
        self._job = None
        self._kernel = None
        self._known = {}
        self._stop = threading.Event()
        self._lock = threading.RLock()

    def start(self, command, *, env=None, stdin=subprocess.DEVNULL):
        environment = worker_environment(env)
        try:
            if os.name == "nt":
                self._kernel = _kernel()
                name = "Local\\GomrokOCR-" + uuid.uuid4().hex
                self._job = self._kernel.CreateJobObjectW(None, name)
                if not self._job:
                    raise OSError("Cannot create native worker containment")
                limits = _ExtendedLimits()
                limits.basic.flags = 0x0200 | 0x2000  # JOB_MEMORY | KILL_ON_JOB_CLOSE
                limits.job_memory = self.limit
                if not self._kernel.SetInformationJobObject(
                    self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                ):
                    raise OSError("Cannot set native worker memory budget")
                environment[_JOB_ENV] = name
            self.process = subprocess.Popen(
                command,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt",
            )
            if self._job:
                # Popen's Windows handle is stable even if its PID is reused.
                _assign(self._kernel, self._job, int(self.process._handle))
            threading.Thread(
                target=self._watch, daemon=True, name="ocr-process-budget"
            ).start()
            return self.process
        except (OSError, psutil.Error):
            self.close()
            raise OSError("Native worker could not start safely") from None

    def _sample(self):
        with self._lock:
            if not self.process:
                return
            root = psutil.Process(self.process.pid)
            for process in [root] + root.children(recursive=True):
                self._known[process.pid] = process
            total = 0
            for pid, process in list(self._known.items()):
                try:
                    # psutil Process identity includes create time.
                    if process.is_running():
                        total += process.memory_info().rss
                    else:
                        self._known.pop(pid, None)
                except psutil.NoSuchProcess:
                    self._known.pop(pid, None)
            if total > self.limit:
                raise MemoryError("Native worker memory budget exceeded")

    def _watch(self):
        while not self._stop.wait(0.2):
            try:
                self._sample()
            except psutil.NoSuchProcess:
                if self.process and self.process.poll() is not None:
                    return
            except (psutil.Error, MemoryError):
                self.failure = "memory_or_supervision_failure"
                self.close()
                return

    def check(self):
        if self.failure:
            raise RuntimeError("Native worker memory or supervision budget failed")
        try:
            self._sample()
        except psutil.NoSuchProcess:
            # A successful finite worker can exit before its output is consumed.
            if self.process and self.process.poll() is None:
                raise RuntimeError("Native worker disappeared") from None
        except (psutil.Error, MemoryError):
            self.failure = "memory_or_supervision_failure"
            self.close()
            raise RuntimeError(
                "Native worker memory or supervision budget failed"
            ) from None

    def close(self):
        with self._lock:
            self._stop.set()
            process, self.process = self.process, None
            job, self._job = self._job, None
            if job:
                self._kernel.TerminateJobObject(job, 1)
                self._kernel.CloseHandle(job)
            if process:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    for child in list(self._known.values()):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    if process.poll() is None:
                        process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("Native worker did not stop") from None
            self._known.clear()


def run_worker(command, *, memory_bytes, seconds, output_limit):
    """Read bounded output without blocking the HTTP process on native work."""
    guard = ProcessGuard(memory_bytes)
    process = guard.start(command)
    output, errors = [], []
    complete = threading.Event()

    def read():
        try:
            output.append(process.stdout.read(output_limit + 1))
        except OSError as exc:
            errors.append(exc)
        finally:
            complete.set()

    reader = threading.Thread(target=read, daemon=True, name="ocr-bounded-output")
    reader.start()
    deadline = time.monotonic() + seconds
    try:
        while not complete.wait(0.1):
            guard.check()
            if time.monotonic() >= deadline:
                raise RuntimeError("Native worker timed out")
        guard.check()
        if errors or not output or len(output[0]) > output_limit:
            raise RuntimeError("Invalid native worker output")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Native worker timed out")
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Native worker timed out") from None
        return code, output[0]
    finally:
        guard.close()
        reader.join(timeout=2)
        process.stdout.close()
