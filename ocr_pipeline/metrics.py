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
            except Exception:  # noqa: BLE001
                pass

    @contextmanager
    def step(self, name: str):
        t0 = time.perf_counter()
        yield
        with self._lock:
            self.timings[name] = round(self.timings.get(name, 0.0)
                                       + time.perf_counter() - t0, 3)

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name, 0) + n)

    def stop(self) -> None:
        self._monitor_stop.set()

    def total(self) -> float:
        return round(time.perf_counter() - self._t0, 2)

    def report(self) -> dict:
        self.stop()
        return {
            "timings_seconds": dict(self.timings),
            "total_seconds": self.total(),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "ocr_calls": self.ocr_calls,
            "fallbacks": self.fallbacks,
            "low_confidence_regions": self.low_conf_regions,
        }
