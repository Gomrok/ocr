# -*- coding: utf-8 -*-
"""ثبت زمان و مصرف RAM برای متریکس عملکرد (بخش ۲۵)."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import psutil

_PROC = psutil.Process()


class Metrics:
    """متریکس هر صفحه: زمان هر مرحله + اوج RAM + شمارنده‌ها."""

    def __init__(self) -> None:
        self.timings: dict[str, float] = {}
        self.peak_rss_mb = 0.0
        self.peak_tree_rss_mb = 0.0
        self._tree_cpu = {}
        self.tree_io_read_bytes = 0
        self.tree_io_write_bytes = 0
        self._io = {}
        self.ocr_calls = 0
        self.fallbacks = 0
        self.low_conf_regions = 0
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()
        self._monitor_stop = threading.Event()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _monitor(self) -> None:
        while not self._monitor_stop.wait(0.05):
            try:
                rss = _PROC.memory_info().rss / (1024 * 1024)
                if rss > self.peak_rss_mb:
                    self.peak_rss_mb = rss
                tree = [_PROC, *_PROC.children(recursive=True)]
                total = 0
                for process in tree:
                    try:
                        key = (process.pid, process.create_time())
                        total += process.memory_info().rss
                        cpu = process.cpu_times()
                        self._tree_cpu[key] = max(self._tree_cpu.get(key, 0), cpu.user+cpu.system)
                        io = process.io_counters()
                        self._io[key] = (io.read_bytes, io.write_bytes)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                self.peak_tree_rss_mb = max(self.peak_tree_rss_mb, total/1024**2)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    @contextmanager
    def step(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self.timings[name] = round(self.timings.get(name, 0.0)
                                           + time.perf_counter() - t0, 3)

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name, 0) + n)

    def stop(self) -> None:
        self._monitor_stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    def total(self) -> float:
        return round(time.perf_counter() - self._t0, 2)

    def report(self) -> dict:
        self.stop()
        return {
            "timings_seconds": dict(self.timings),
            "total_seconds": self.total(),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            'peak_tree_rss_mb_sampled': round(self.peak_tree_rss_mb, 1),
            'tree_cpu_lifetime_seconds_sampled': round(sum(self._tree_cpu.values()), 3),
            'tree_io_lifetime_read_bytes_sampled': sum(value[0] for value in self._io.values()),
            'tree_io_lifetime_write_bytes_sampled': sum(value[1] for value in self._io.values()),
            'resource_scope': 'application_tree_including_concurrent_jobs',
            'sampling_interval_seconds': .05,
            "ocr_calls": self.ocr_calls,
            "fallbacks": self.fallbacks,
            "low_confidence_regions": self.low_conf_regions,
        }
