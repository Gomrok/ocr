"""Run one document in its own bounded process and stream page progress."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

import psutil


class WorkerStopped(RuntimeError):
    pass


def run_isolated(source, output, scratch, request, progress):
    """Own the worker and its children, including timeout and exception exits."""
    started = time.monotonic()
    peak, stopped = 0, None
    stop_watch = threading.Event()
    budget = request["ram_mib"] * 2**20
    timeout = min(6 * 3600, 120 + 180 * request["pages"])
    with tempfile.TemporaryDirectory(prefix="ocr-worker-", dir=scratch) as directory:
        env = dict(os.environ, OCR_NATIVE_WORKERS=str(request["native_workers"]),
                   OMP_THREAD_LIMIT=str(request["omp_threads"]), OCR_TEMP_DIR=directory,
                   TEMP=directory, TMP=directory, TMPDIR=directory)
        with tempfile.TemporaryFile(dir=directory) as errors, subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-u", "-m", "ocr_pipeline.job_worker"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as process:
            root = psutil.Process(process.pid)

            def successors():
                try:
                    return root.children(recursive=True)
                except psutil.Error:
                    return []

            def terminate():
                children = successors()
                if process.poll() is None:
                    process.kill()
                for child in children:
                    try:
                        child.kill()
                    except psutil.Error:
                        continue
                if children:
                    psutil.wait_procs(children, timeout=3)

            def watch():
                nonlocal peak, stopped
                while not stop_watch.wait(.10):
                    if process.poll() is not None:
                        return
                    total = 0
                    for child in [root, *successors()]:
                        try:
                            total += child.memory_info().rss
                        except psutil.Error:
                            continue
                    peak = max(peak, total)
                    if total > budget:
                        stopped = "ram_budget_exhausted"
                    elif time.monotonic() - started > timeout:
                        stopped = "worker_timeout"
                    if stopped:
                        terminate()
                        return

            watcher = threading.Thread(target=watch, name="ocr-worker-budget")
            watcher.start()
            try:
                caller = psutil.Process()
                process.stdin.write(json.dumps(dict(request, source=str(source), output=str(output),
                                                   parent_pid=caller.pid, parent_started=caller.create_time())))
                process.stdin.close()
                for line in process.stdout:
                    progress(json.loads(line))
                if process.wait() or stopped:
                    errors.seek(0)
                    detail = errors.read(700).decode("utf-8", errors="replace")
                    raise WorkerStopped(stopped or detail or "worker_interrupted")
                result = json.loads((output / (source.stem + ".json")).read_text("utf-8"))
                if request["mode"] == "compare":
                    result["comparison"] = json.loads((output / "comparison.json").read_text("utf-8"))
            finally:
                stop_watch.set()
                if process.poll() is None:
                    terminate()
                watcher.join()
    result["worker_usage"] = {
        "elapsed_seconds": round(time.monotonic() - started, 3), "peak_tree_rss_mib": round(peak / 2**20, 2),
        "ram_budget_mib": request["ram_mib"], "native_workers": request["native_workers"],
        "omp_threads": request["omp_threads"], "max_render_mp": request["render_budget_mp"],
    }
    return result


@contextmanager
def _document_lock(directory):
    """An interrupted service and its replacement must never write one document together."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".worker-lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _stop_if_parent_exits(request, stop):
    try:
        watched = psutil.Process(request["parent_pid"])
        while not stop.wait(.2):
            if not watched.is_running() or watched.create_time() != request["parent_started"]:
                break
        else:
            return
    except psutil.Error:
        pass
    for child in reversed(psutil.Process().children(recursive=True)):
        try:
            child.kill()
        except psutil.Error:
            continue
    # Atomic page checkpoints survive and the OS releases the document lock.
    os._exit(74)


def main():
    import cv2
    from .pipeline import process_pdf

    cv2.setNumThreads(1)
    request = json.load(sys.stdin)
    settings = request["candidate_settings"]
    options = None
    if request["mode"] != "legacy":
        from .precision.profiles import Options
        options = Options(profile=settings.get("profile", "persian"), dpi=settings.get("candidate_dpi", 300),
                          handwriting=settings.get("handwriting", False))

    def progress(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    stop_parent_watch = threading.Event()
    watch = threading.Thread(target=_stop_if_parent_exits, args=(request, stop_parent_watch), daemon=True)
    watch.start()
    output = Path(request["output"])
    try:
        with _document_lock(output):
            process_pdf(Path(request["source"]), output, dpi=request["dpi"],
                        force_tesseract=True, progress_cb=progress, mode=request["mode"],
                        candidate_options=options, handwriting=request["handwriting"],
                        resume=True, keep_masters=False, render_budget_mp=request["render_budget_mp"])
    finally:
        stop_parent_watch.set()
        watch.join()


if __name__ == "__main__":
    main()
